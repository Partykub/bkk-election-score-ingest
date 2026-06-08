# Hermes Supervisor

This folder contains the first implementation slice for the election-system supervisor role using the official Hermes Agent Docker image.

## Current status

The currently verified local path is:

- Hermes Supervisor in Docker using `runtime-full/`
- Ollama on the host machine with `gemma4:26b`
- LINE gateway enabled in Hermes
- LINE OA traffic reaching Hermes successfully through `ngrok`

Notes on the current local path:

- `hermes/supervisor/.env` is the active local runtime file and is intentionally git-ignored because it contains secrets.
- The helper script `scripts/setup-hermes-supervisor.ps1` now reads `HERMES_SUPERVISOR_RUNTIME_DIR` from `.env` and seeds that runtime path instead of assuming `runtime/`.
- Free `ngrok` domains can show a browser warning page to normal browser-style checks, so local health checks are the most reliable quick validation during development.

## What is included

- `docker-compose.yml` to run the supervisor in gateway mode
- `docker-compose.ec2.yml` to run Hermes and Ollama together on a Docker host such as AWS EC2
- `seed/SOUL.md` to pin the supervisor role and operating rules
- `.env.example` for Docker-level runtime settings such as ports and dashboard auth
- `.env.ec2.example` for the all-in-docker EC2 deployment path
- `../../scripts/setup-hermes-supervisor.ps1` to create the runtime volume and run the interactive Hermes setup wizard
- `../../scripts/start-hermes-supervisor.ps1` to start the container with Docker Compose
- `../../scripts/start-hermes-supervisor-ec2.ps1` and `../../scripts/start-hermes-supervisor-ec2.sh` to start the EC2 all-in-docker stack

## Directory layout

- `runtime/` is the mounted `/opt/data` volume for Hermes. It is intentionally ignored from git because it will contain secrets, sessions, logs, and persistent state.
- `runtime-full/` is the current active runtime used for the local Ollama-based supervisor setup.
- `runtime-ec2/` is the Docker-to-Docker runtime used when Hermes should call an Ollama sidecar service instead of the host machine.
- `ollama/data/` is the persistent model store for the Ollama container in the EC2 deployment path.
- `seed/` contains files that should be copied into `runtime/` before first boot.

## First-time setup

1. Review and edit `.env.example` values, especially dashboard credentials and API server settings.
2. Run `powershell -ExecutionPolicy Bypass -File .\scripts\setup-hermes-supervisor.ps1` from the repository root.
3. The setup script copies `.env.example` to `hermes/supervisor/.env` if needed, reads `HERMES_SUPERVISOR_RUNTIME_DIR` from that file, seeds `SOUL.md` into the configured runtime directory, and launches `nousresearch/hermes-agent setup` against that same runtime path.
4. The Hermes setup wizard writes the agent's own secrets to `<configured-runtime>/.env`.

For the current local setup, `.env.example` and `.env` point Docker Compose at `runtime-full/`, which is configured to use the host's local Ollama server.

## EC2 all-in-docker deployment

If you want to ship this to AWS EC2 and run everything through Docker, use the EC2 compose file instead of pointing Hermes at `host.docker.internal`.

1. Copy `.env.ec2.example` to `.env.ec2` and replace the placeholder credentials.
2. Review `OLLAMA_MODEL` and make sure the EC2 instance has enough RAM or GPU for that model.
3. Start the full stack:

	`docker compose --env-file hermes/supervisor/.env.ec2 -f hermes/supervisor/docker-compose.ec2.yml up -d`

	Or use the helper script on the target host:

	`./scripts/start-hermes-supervisor-ec2.sh`

4. Hermes will talk to Ollama at `http://ollama:11434/v1` over the internal Docker network.
5. The one-shot `ollama-init` container will pull the configured model into the mounted Ollama volume.

Notes:

- This is the right shape when both Hermes and Ollama live on the same EC2 host and you want a single Docker-based deployment workflow.
- If the EC2 host has an NVIDIA GPU, you can add the appropriate GPU flags or Compose GPU settings to the `ollama` service.
- Do not reuse the local `runtime-full/` config on EC2; it points to `host.docker.internal`, which is for the local-host setup.

## Start the supervisor

Run `powershell -ExecutionPolicy Bypass -File .\scripts\start-hermes-supervisor.ps1` from the repository root.

The service starts with:

- gateway mode enabled
- dashboard enabled on port `9119`
- optional OpenAI-compatible API server on port `8642` when `API_SERVER_ENABLED=true`
- LINE webhook listener on port `8646` when `gateway.platforms.line.enabled=true`

## LINE webhook setup

The supervisor is now preconfigured to enable the bundled LINE platform plugin. To finish the setup you must fill the LINE environment variables in `.env` or `.env.ec2`.

The currently verified local combination is `.env` + `ngrok` + LINE OA.

Required secrets:

- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_CHANNEL_SECRET`

Required routing values:

- At least one allowlist: `LINE_ALLOWED_USERS`, `LINE_ALLOWED_GROUPS`, `LINE_ALLOWED_ROOMS`, or `LINE_ALLOW_ALL_USERS=true` for development only
- `LINE_PUBLIC_URL` pointing at your public HTTPS base URL

Webhook details:

- Health check: `https://<your-public-url>/line/webhook/health`
- Webhook URL to register in LINE Developers Console: `https://<your-public-url>/line/webhook`
- Container bind port: `8646` by default

Behavior choices already set in `config.yaml` for LINE:

- `gateway.platforms.line.enabled: true`
- `display.interim_assistant_messages: false`
- `display.platforms.line.tool_progress: off`
- `display.platforms.line.streaming: false`

These keep the LINE reply token available longer and align with Hermes' slow-response postback flow.

## Useful commands

- `docker compose --env-file hermes/supervisor/.env -f hermes/supervisor/docker-compose.yml up -d`
- `docker compose --env-file hermes/supervisor/.env.ec2 -f hermes/supervisor/docker-compose.ec2.yml up -d`
- `docker compose --env-file hermes/supervisor/.env -f hermes/supervisor/docker-compose.yml logs -f`
- `docker exec hermes-supervisor hermes gateway status`
- `curl -i https://<your-public-url>/line/webhook/health`
- `powershell -ExecutionPolicy Bypass -File .\scripts\start-line-localhost-run-tunnel.ps1`
- `powershell -ExecutionPolicy Bypass -File .\scripts\start-line-cloudflared-tunnel.ps1`
- `powershell -ExecutionPolicy Bypass -File .\scripts\start-line-ngrok-tunnel.ps1`

## Local tunnel with localhost.run

If you are testing LINE from your local machine, you need a public HTTPS URL while Hermes is running on `localhost:8646`. The repo now includes a helper script for `localhost.run`.

1. Start Hermes locally:

	`powershell -ExecutionPolicy Bypass -File .\scripts\start-hermes-supervisor.ps1`

2. In a second terminal, start the tunnel:

	`powershell -ExecutionPolicy Bypass -File .\scripts\start-line-localhost-run-tunnel.ps1`

3. `localhost.run` will print a temporary public URL such as `https://abc123.localhost.run`.
4. Set `LINE_PUBLIC_URL` in `hermes/supervisor/.env` to that base URL.
5. Register `https://abc123.localhost.run/line/webhook` in the LINE Developers Console.

Notes:

- Yes, you need a tunnel for local development. Without it, LINE cannot reach your machine from the public internet.
- Keep the tunnel terminal open. When the SSH session ends, the public URL stops working.
- For production on EC2 with a real domain and HTTPS, you do not need `localhost.run`.

## Stable tunnel with Cloudflare

If your domain is already managed in Cloudflare, use a managed Cloudflare Tunnel instead of a temporary tunnel. This gives you a stable hostname for LINE webhook configuration.

One-time setup:

1. Install `cloudflared` on the local machine.

	`winget install Cloudflare.cloudflared`

2. Authenticate Cloudflare access in a browser:

	`cloudflared tunnel login`

3. Create a tunnel:

	`cloudflared tunnel create hermes-line`

4. Create a DNS route for your hostname, for example `linebot.example.com`:

	`cloudflared tunnel route dns hermes-line linebot.example.com`

5. Copy `hermes/supervisor/cloudflared/config.example.yml` to `hermes/supervisor/cloudflared/config.yml`.
6. Replace these placeholders in `config.yml`:

- `replace-with-tunnel-id` with the real tunnel ID created by Cloudflare
- `credentials-file` with the real JSON credentials path that `cloudflared tunnel create` printed
- `linebot.example.com` with your real hostname

Run the managed tunnel:

1. Start Hermes locally:

	`powershell -ExecutionPolicy Bypass -File .\scripts\start-hermes-supervisor.ps1`

2. In a second terminal, start Cloudflare Tunnel:

	`powershell -ExecutionPolicy Bypass -File .\scripts\start-line-cloudflared-tunnel.ps1`

3. Set `LINE_PUBLIC_URL` in `hermes/supervisor/.env` to your stable hostname, for example:

	`LINE_PUBLIC_URL=https://linebot.example.com`

4. Register this webhook URL in the LINE Developers Console:

	`https://linebot.example.com/line/webhook`

Notes:

- This is the recommended local-dev path if you need a hostname that does not change every reconnect.
- Keep the `cloudflared` terminal open unless you install it later as a Windows service.
- The repo ignores `hermes/supervisor/cloudflared/config.yml` and any JSON credentials you might place there.

## Local tunnel with ngrok

`ngrok` is the currently verified tunnel path for local LINE OA testing in this repo.

1. Install ngrok and connect it to your account once:

	`ngrok config add-authtoken <your-ngrok-authtoken>`

2. Start Hermes locally:

	`powershell -ExecutionPolicy Bypass -File .\scripts\start-hermes-supervisor.ps1`

3. In a second terminal, start ngrok:

	`powershell -ExecutionPolicy Bypass -File .\scripts\start-line-ngrok-tunnel.ps1`

4. ngrok will show a public HTTPS URL such as `https://abc123.ngrok-free.app`.
5. Set `LINE_PUBLIC_URL` in `hermes/supervisor/.env` to that base URL.
6. Register `https://abc123.ngrok-free.app/line/webhook` in the LINE Developers Console.

Notes:

- Free ngrok tunnels are temporary and the URL can change when you reconnect.
- Keep the ngrok terminal open while testing.
- Normal browser-style checks against free ngrok domains can hit `ERR_NGROK_6024`. LINE webhook delivery can still work, but if you want a quick health check from a browser or PowerShell, prefer `http://localhost:8646/line/webhook/health` locally.
- If your ngrok plan supports reserved domains, you can request one with:

	`powershell -ExecutionPolicy Bypass -File .\scripts\start-line-ngrok-tunnel.ps1 -Hostname https://linebot.example.ngrok.app`

## Important notes

- Do not point any other Hermes container at `hermes/supervisor/runtime`.
- The supervisor role is intentionally lightweight. It should receive events, validate, persist, dedupe, enqueue, and ask for approval. It should not run OCR inline.
- This implementation does not yet include Line webhook adapters, database persistence, queue integration, or OCR/update workers.
