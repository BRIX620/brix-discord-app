"""Brix Discord HTTP Interactions service."""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_API_TIMEOUT_SECONDS = 10.0
SIGNATURE_MAX_AGE_SECONDS = 300
DISCORD_MESSAGE_LIMIT = 2000
EPHEMERAL_FLAG = 1 << 6

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("brix")


def get_base_path(value: str | None = None) -> str:
    """Return a normalized, absolute route prefix."""
    base_path = (
        value
        if value is not None
        else os.getenv("BASE_PATH", "/api/discord")
    ).strip()

    if not base_path:
        raise RuntimeError("BASE_PATH cannot be empty.")
    if not base_path.startswith("/"):
        raise RuntimeError("BASE_PATH must start with '/'.")

    normalized = "/" + base_path.strip("/")
    return "" if normalized == "/" else normalized


def ephemeral_responses_enabled(
    value: str | None = None,
) -> bool:
    """Only the explicit string 'true' enables private replies."""
    setting = (
        value
        if value is not None
        else os.getenv("EPHEMERAL_RESPONSES", "true")
    ).strip().lower()

    if setting not in {"true", "false"}:
        raise RuntimeError(
            "EPHEMERAL_RESPONSES must be either 'true' or 'false'."
        )

    return setting == "true"


def build_say_command() -> dict[str, Any]:
    """Return the global /say command for user-installed apps."""
    return {
        "type": 1,
        "name": "say",
        "description": "Post a message as Brix.",
        "integration_types": [1],
        "contexts": [0],
        "options": [
            {
                "type": 3,
                "name": "message",
                "description": "The message to post.",
                "required": True,
                "min_length": 1,
                "max_length": DISCORD_MESSAGE_LIMIT,
            }
        ],
    }


async def load_application_and_register_command(
    client: httpx.AsyncClient,
) -> tuple[str, str]:
    """Look up the application ID/key and register /say."""
    response = await client.get("/applications/@me")
    response.raise_for_status()
    application = response.json()

    application_id = application.get("id")
    verify_key = application.get("verify_key")

    if not isinstance(application_id, str) or not application_id:
        raise RuntimeError("Discord did not return an application ID.")

    if not isinstance(verify_key, str) or len(verify_key) != 64:
        raise RuntimeError(
            "Discord did not return a valid verification key."
        )

    command_response = await client.post(
        f"/applications/{application_id}/commands",
        json=build_say_command(),
    )
    command_response.raise_for_status()

    return application_id, verify_key


def verify_interaction_signature(
    *,
    public_key: VerifyKey,
    signature_hex: str | None,
    timestamp: str | None,
    body: bytes,
    now: float | None = None,
) -> bool:
    """Verify Discord's Ed25519 signature and reject stale requests."""
    if (
        not signature_hex
        or not timestamp
        or not timestamp.isdecimal()
    ):
        return False

    current_time = time.time() if now is None else now

    if abs(current_time - int(timestamp)) > SIGNATURE_MAX_AGE_SECONDS:
        return False

    try:
        signature = bytes.fromhex(signature_hex)
        public_key.verify(timestamp.encode("ascii") + body, signature)
    except (ValueError, UnicodeEncodeError, BadSignatureError):
        return False

    return True


def response_data(
    content: str,
    *,
    ephemeral: bool,
) -> dict[str, Any]:
    """Build an interaction response with mention notifications disabled."""
    data: dict[str, Any] = {
        "content": content,
        "allowed_mentions": {"parse": []},
    }

    if ephemeral:
        data["flags"] = EPHEMERAL_FLAG

    return data


async def send_public_followup(
    *,
    application_id: str,
    interaction_token: str,
    content: str,
) -> None:
    """Send a separate public follow-up to the interaction."""
    url = (
        f"{DISCORD_API_BASE}/webhooks/"
        f"{application_id}/{interaction_token}"
    )

    async with httpx.AsyncClient(
        timeout=DISCORD_API_TIMEOUT_SECONDS,
    ) as client:
        response = await client.post(
            url,
            json={
                "content": content,
                "allowed_mentions": {"parse": []},
            },
        )
        response.raise_for_status()


def create_app(
    *,
    base_path: str | None = None,
    ephemeral: bool | None = None,
    verify_key_hex: str | None = None,
    register_discord_command: bool = True,
) -> FastAPI:
    """Create the FastAPI application."""
    route_prefix = get_base_path(base_path)

    private_replies = (
        ephemeral_responses_enabled()
        if ephemeral is None
        else ephemeral
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.ephemeral_responses = private_replies
        application.state.application_id = None
        application.state.verify_key = (
            VerifyKey(bytes.fromhex(verify_key_hex))
            if verify_key_hex
            else None
        )

        if register_discord_command:
            token = os.getenv("DISCORD_BOT_TOKEN", "").strip()

            if not token:
                raise RuntimeError(
                    "DISCORD_BOT_TOKEN is missing. "
                    "Set it in your hosting provider's environment variables."
                )

            try:
                async with httpx.AsyncClient(
                    base_url=DISCORD_API_BASE,
                    headers={"Authorization": f"Bot {token}"},
                    timeout=DISCORD_API_TIMEOUT_SECONDS,
                ) as client:
                    application_id, application_key = (
                        await load_application_and_register_command(client)
                    )

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                logger.error(
                    "Discord setup failed with HTTP %s. "
                    "Check the bot token and application configuration.",
                    status,
                )
                raise RuntimeError(
                    f"Discord setup failed with HTTP {status}."
                ) from None

            except httpx.HTTPError as exc:
                logger.error(
                    "Could not reach Discord during startup: %s",
                    type(exc).__name__,
                )
                raise RuntimeError(
                    "Could not reach Discord during startup."
                ) from None

            application.state.application_id = application_id
            application.state.verify_key = VerifyKey(
                bytes.fromhex(application_key)
            )

            logger.info("Brix is ready; /say is registered.")

        elif application.state.verify_key is None:
            raise RuntimeError(
                "A verification key is required when "
                "Discord setup is disabled."
            )

        yield

    application = FastAPI(
        title="Brix Discord Say",
        description="Discord HTTP Interactions endpoint for Brix.",
        lifespan=lifespan,
    )

    @application.get(f"{route_prefix}/")
    async def root() -> dict[str, str]:
        return {"name": "Brix", "status": "ok"}

    @application.get(f"{route_prefix}/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.post(f"{route_prefix}/interactions")
    async def interactions(
        request: Request,
        signature: str | None = Header(
            default=None,
            alias="X-Signature-Ed25519",
        ),
        timestamp: str | None = Header(
            default=None,
            alias="X-Signature-Timestamp",
        ),
    ) -> JSONResponse:
        body = await request.body()
        public_key: VerifyKey | None = request.app.state.verify_key

        if public_key is None or not verify_interaction_signature(
            public_key=public_key,
            signature_hex=signature,
            timestamp=timestamp,
            body=body,
        ):
            raise HTTPException(
                status_code=401,
                detail="Invalid interaction signature.",
            )

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(
                status_code=400,
                detail="Invalid JSON payload.",
            ) from None

        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400,
                detail="Invalid interaction payload.",
            )

        # Discord's endpoint verification handshake.
        if payload.get("type") == 1:
            return JSONResponse({"type": 1})

        if payload.get("type") != 2:
            return JSONResponse(
                {
                    "type": 4,
                    "data": response_data(
                        "Brix only supports the /say command.",
                        ephemeral=True,
                    ),
                }
            )

        command = payload.get("data")

        if not isinstance(command, dict) or command.get("name") != "say":
            return JSONResponse(
                {
                    "type": 4,
                    "data": response_data(
                        "Brix only supports the /say command.",
                        ephemeral=True,
                    ),
                }
            )

        options = command.get("options", [])
        message = None

        if isinstance(options, list):
            for option in options:
                if (
                    isinstance(option, dict)
                    and option.get("name") == "message"
                ):
                    message = option.get("value")
                    break

        if not isinstance(message, str) or not message.strip():
            return JSONResponse(
                {
                    "type": 4,
                    "data": response_data(
                        "Please provide a message.",
                        ephemeral=True,
                    ),
                }
            )

        if len(message) > DISCORD_MESSAGE_LIMIT:
            return JSONResponse(
                {
                    "type": 4,
                    "data": response_data(
                        "Message must be 2,000 characters or fewer.",
                        ephemeral=True,
                    ),
                }
            )

        application_id = request.app.state.application_id
        interaction_token = payload.get("token")

        if (
            not isinstance(application_id, str)
            or not application_id
            or not isinstance(interaction_token, str)
            or not interaction_token
        ):
            return JSONResponse(
                {
                    "type": 4,
                    "data": response_data(
                        "Brix could not prepare the public message.",
                        ephemeral=True,
                    ),
                }
            )

        # Acknowledge privately first. The follow-up is sent separately.
        # Discord may still associate it with the slash-command interaction.
        return JSONResponse(
            {
                "type": 5,
                "data": {"flags": EPHEMERAL_FLAG},
            }
        )

    return application


app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
    