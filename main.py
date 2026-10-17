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
    base_path = (value if value is not None else os.getenv("BASE_PATH", "/api/discord")).strip()
    if not base_path:
        raise RuntimeError("BASE_PATH cannot be empty.")
    if not base_path.startswith("/"):
        raise RuntimeError("BASE_PATH must start with '/'.")
    normalized = "/" + base_path.strip("/")
    return "" if normalized == "/" else normalized


def ephemeral_responses_enabled(value: str | None = None) -> bool:
    """Only the explicit string 'true' enables private interaction replies."""
    setting = (value if value is not None else os.getenv("EPHEMERAL_RESPONSES", "false"))
    setting = setting.strip().lower()
    if setting not in {"true", "false"}:
        raise RuntimeError("EPHEMERAL_RESPONSES must be either 'true' or 'false'.")
    return setting == "true"


def build_say_command() -> dict[str, Any]:
    """Return the global /say command for Discord User Install."""
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
    """Look up the application's ID/key, then create or update /say."""
    application_response = await client.get("/applications/@me")
    application_response.raise_for_status()
    application = application_response.json()

    application_id = application.get("id")
    verify_key = application.get("verify_key")
    if not isinstance(application_id, str) or not application_id:
        raise RuntimeError("Discord did not return an application ID.")
    if not isinstance(verify_key, str) or len(verify_key) != 64:
        raise RuntimeError("Discord did not return a valid interactions verification key.")

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
    """Verify Discord's Ed25519 signature and reject stale signed requests."""
    if not signature_hex or not timestamp or not timestamp.isdecimal():
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
    data: dict[str, Any] = {
        "content": content,
        # Keep message text intact, but never trigger user, role, @everyone,
        # or @here notifications.
        "allowed_mentions": {"parse": []},
    }
    if ephemeral:
        data["flags"] = EPHEMERAL_FLAG
    return data


def create_app(
    *,
    base_path: str | None = None,
    ephemeral: bool | None = None,
    verify_key_hex: str | None = None,
    register_discord_command: bool = True,
) -> FastAPI:
    """Create the FastAPI application; options allow isolated local tests."""
    route_prefix = get_base_path(base_path)
    private_replies = (
        ephemeral_responses_enabled()
        if ephemeral is None
        else ephemeral
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.ephemeral_responses = private_replies
        application.state.verify_key = (
            VerifyKey(bytes.fromhex(verify_key_hex)) if verify_key_hex else None
        )

        if register_discord_command:
            token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
            if not token:
                raise RuntimeError(
                    "DISCORD_BOT_TOKEN is missing. Add it in Replit Secrets."
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
                # Do not log the request headers or secret.
                status = exc.response.status_code
                logger.error(
                    "Discord setup failed with HTTP %s. Check the bot token, "
                    "application settings, and User Install configuration.",
                    status,
                )
                raise RuntimeError(
                    f"Discord setup failed with HTTP {status}; check the README "
                    "and Discord Developer Portal settings."
                ) from None
            except httpx.HTTPError as exc:
                logger.error("Could not reach Discord during startup: %s", type(exc).__name__)
                raise RuntimeError(
                    "Could not reach Discord during startup; check the network and retry."
                ) from None

            application.state.application_id = application_id
            application.state.verify_key = VerifyKey(bytes.fromhex(application_key))
            logger.info("Brix is ready; the global /say command is registered.")
        elif application.state.verify_key is None:
            raise RuntimeError(
                "A verification key is required when Discord setup is disabled."
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
        signature: str | None = Header(default=None, alias="X-Signature-Ed25519"),
        timestamp: str | None = Header(default=None, alias="X-Signature-Timestamp"),
    ) -> JSONResponse:
        body = await request.body()
        public_key: VerifyKey | None = request.app.state.verify_key
        if public_key is None or not verify_interaction_signature(
            public_key=public_key,
            signature_hex=signature,
            timestamp=timestamp,
            body=body,
        ):
            raise HTTPException(status_code=401, detail="Invalid interaction signature.")

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid interaction payload.")

        # Discord sends this when validating the Interactions Endpoint URL.
        if payload.get("type") == 1:
            return JSONResponse({"type": 1})

        if payload.get("type") != 2:
            return JSONResponse(
                {
                    "type": 4,
                    "data": response_data(
                        "Brix only supports the /say command.",
                        ephemeral=request.app.state.ephemeral_responses,
                    ),
                }
            )

        command = payload.get("data")
        if not isinstance(command, dict) or command.get("name") != "say":
            content = "Brix only supports the /say command."
        else:
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

            if not isinstance(message, str) or not message:
                content = "Please provide a message."
            elif len(message) > DISCORD_MESSAGE_LIMIT:
                content = "Message must be 2,000 characters or fewer."
            else:
                content = message

        return JSONResponse(
            {
                "type": 4,
                "data": response_data(
                    content,
                    ephemeral=request.app.state.ephemeral_responses,
                ),
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
