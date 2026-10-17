# Brix Discord `/say`

Brix is a small Python 3.12+ service that receives Discord interactions over HTTPS. It uses FastAPI, Uvicorn, HTTPX, and PyNaCl; it does not connect to the Discord Gateway.

When someone runs `/say message: hi`, Brix posts `hi` as a public interaction response by default. Discord does not document an option for hiding the slash-command invocation notice, so Brix does not try to suppress it.

## Run it in Replit on Android

1. Open the Brix project in the Replit Android app.
2. Open the workspace navigation and select **Tools → Secrets**. If the navigation is collapsed on your screen, expand it to find **Tools**.
3. Tap **+ New Secret**. Enter `DISCORD_BOT_TOKEN` as the key and paste your Brix bot token as the value, then save. Do not paste the token into the editor or chat.
4. Return to the project and tap **Run**. The service looks up the Discord application, registers `/say`, then starts the HTTP server. In the workflow logs, wait for `Brix is ready`; startup stops with an error if the token or Discord settings are wrong.
5. The service responds at:
   - `GET /api/discord/`
   - `GET /api/discord/healthz`
   - `POST /api/discord/interactions`

The service handles `/api/discord` itself. Its routes do not depend on a proxy removing or adding that prefix.

## Create and configure the Discord application

1. In the Discord Developer Portal, create an application named **Brix**.
2. Open its **Bot** page and create the bot user needed for the application token. Copy its token into Replit Secrets as `DISCORD_BOT_TOKEN`; do not invite this bot user to servers.
3. In **Installation → Installation Contexts**, enable **User Install** and disable **Guild Install**. Select **Discord Provided Link** under **Install Link**. Under **Default Install Settings**, select `applications.commands` for **User Install** only. Do not select the `bot` scope or bot permissions.
4. In the app's **General Information** settings, leave the **Interactions Endpoint URL** blank until the service has a public HTTPS URL. The first save validates the endpoint with a signed `PING`.
5. The app registers `/say` globally when it starts, using Discord's global application-command endpoint (not a server-specific command endpoint).

The command payload uses `integration_types: [1]` and `contexts: [0]` (`GUILD`). Authorize Brix once to your Discord account, then use `/say` in eligible servers without adding the bot user to each server. No server IDs or per-server installs are used.

### Authorize Brix for your Discord account

After configuring the User Install default scope above, open the official Discord Provided Link:

[Authorize Brix for my Discord account](https://discord.com/oauth2/authorize?client_id=1560212871122657371)

Discord Provided Links read the install context and scopes from the application's default install settings. With the setup above, this link authorizes a **User Install** with `applications.commands`; it does not add the bot user to a server. Use of commands can still depend on Discord's and each server's app/command availability settings.

## Publish and set the endpoint

Discord needs a public HTTPS address and the service must be running for commands to work. A local preview or an offline service is not enough.

1. In Replit, tap **Publish** and choose an always-on **VM** deployment so the interaction server is available when Discord sends requests. If you do not see Publish in the editor, open the project's publishing controls.
2. Ensure `DISCORD_BOT_TOKEN` is available to the **production** deployment's Secrets as well as the development project.
3. Copy the public HTTPS deployment domain from Replit. Do not use a temporary development URL.
4. In the Discord Developer Portal, set **Interactions Endpoint URL** to:

   `https://YOUR-PUBLIC-DOMAIN/api/discord/interactions`

5. Save. Discord sends a signed endpoint-validation `PING`; the server verifies the Ed25519 signature and returns the required `PONG`.
6. Open the [Brix User Install link](https://discord.com/oauth2/authorize?client_id=1560212871122657371) to authorize it to your Discord account, then try `/say` in an eligible server.

If the app is published as private, Discord cannot reach it. The deployment must be publicly accessible to accept Discord's requests.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | Required | Bot token used only to look up the application and register `/say`. Keep it in Replit Secrets. |
| `BASE_PATH` | `/api/discord` | Prefix for all HTTP routes. |
| `EPHEMERAL_RESPONSES` | `false` | Set to exactly `true` to make interaction responses ephemeral. |
| `PORT` | `8000` | Port assigned by Replit. |

The message content is returned without changing Markdown, Unicode, or custom emoji syntax. Every reply sets `allowed_mentions.parse` to an empty list so `@everyone` and `@here` cannot notify a channel. Messages longer than 2,000 characters are rejected.

## Local setup outside Replit

Use Python 3.12 or later:

```sh
python -m pip install -r requirements.txt
python main.py
```

Set `DISCORD_BOT_TOKEN` in your shell environment before starting the server. The endpoint still needs to be available over public HTTPS before Discord can deliver interactions.

## Troubleshooting

- **Startup says the token is missing:** add `DISCORD_BOT_TOKEN` in Replit Secrets, then restart.
- **Discord returns an HTTP error during startup:** check that the token belongs to the Brix bot, that User Install is enabled, and that the app's command registration settings are valid.
- **Discord rejects the endpoint URL:** publish first, make the deployment public, use the exact `/api/discord/interactions` path, and keep the service online while saving.
- **The command is not visible immediately:** global command registration may take a short time to appear. Check that User Install is enabled, the User Install default scope is `applications.commands`, and you authorized Brix to your account.
- **The service does not respond while offline:** Discord cannot deliver interactions to an offline endpoint; keep the published VM running.

## Security and behavior

- Every interaction, including endpoint-validation `PING`, must have a valid Discord Ed25519 signature. Stale signed requests are rejected.
- The bot token is not used to send messages. Interaction responses are returned directly to Discord.
- No Discord Gateway connection, user token, or self-bot is used.
- Discord controls how its client displays the command invocation notice; this app uses a normal public interaction response and does not claim that the notice can be hidden.
