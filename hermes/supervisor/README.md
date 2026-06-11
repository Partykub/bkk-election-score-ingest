# Hermes Supervisor

This folder contains the first implementation slice for the election-system supervisor role using the official Hermes Agent Docker image.

## Current status

The currently verified local path is:

- Hermes Supervisor in Docker using `runtime-full/`
- Ollama on the host machine with `gemma4:26b`
- LINE gateway enabled in Hermes
- LINE OA traffic reaching Hermes successfully through `ngrok`
- S3 as the canonical store for source manifests, drafts, approvals, and update jobs
- `update-worker` completing approved jobs in `s3_only` mode when no downstream API is configured

Notes on the current local path:

- `hermes/supervisor/.env` is the active local runtime file and is intentionally git-ignored because it contains secrets.
- The helper script `scripts/setup-hermes-supervisor.ps1` now reads `HERMES_SUPERVISOR_RUNTIME_DIR` from `.env` and seeds that runtime path instead of assuming `runtime/`.
- Free `ngrok` domains can show a browser warning page to normal browser-style checks, so local health checks are the most reliable quick validation during development.

## What is included

- `docker-compose.yml` to run the supervisor in gateway mode
- `docker-compose.ec2.yml` to run Hermes, Ollama, and the OCR worker together on a Docker host such as AWS EC2
- `services/intake_server.py` as the active local Python intake service for LINE event persistence, event deduplication, and initial routing classification
- `services/upload_service.py` as the current local storage abstraction for writing metadata into the planned S3-style layout
- top-level `intake_server.py`, `line_webhook_relay.py`, and `upload_service.py` stay as compatibility entrypoints for scripts and tests
- `test_intake_server.py` with focused tests for image intake, text command classification, and duplicate event handling
- `../ocr_worker/__main__.py` as the OCR queue consumer that downloads jobs from SQS, calls the Hermes API, and writes drafts back to S3
- `../ocr_worker/test_worker.py` with focused tests for queue parsing, OCR response normalization, approval prompt delivery, and manifest updates
- `../update_worker/__main__.py` as the deterministic update worker for approved update jobs
- `seed/SOUL.md` to pin the supervisor role and operating rules
- `.env.example` for Docker-level runtime settings such as ports and dashboard auth
- `.env.ec2.example` for the all-in-docker EC2 deployment path
- `../../scripts/setup-hermes-supervisor.ps1` to create the runtime volume and run the interactive Hermes setup wizard
- `../../scripts/start-election-local.ps1` to bring up the local Docker stack and launch the LINE relay for the real intake path
- `../../scripts/start-hermes-supervisor.ps1` to start the container with Docker Compose
- `../../scripts/start-hermes-ocr-worker.ps1` to start the OCR worker container with Docker Compose
- `../../scripts/start-update-worker.ps1` to start the update worker container with Docker Compose
- `../../scripts/start-hermes-supervisor-ec2.ps1` and `../../scripts/start-hermes-supervisor-ec2.sh` to start the EC2 all-in-docker stack

## Workflow summary

The currently implemented flow is:

- LINE image enters through `line_webhook_relay.py`
- supervisor intake persists the source message and enqueues an OCR job
- `ocr-worker` reads the image from S3, calls Hermes OCR, and writes `draft` plus `approval` artifacts back to S3
- the user replies either `ยืนยัน` or `แก้ไข ...`
- a correction creates a new draft revision and a new approval revision
- an approval creates an update job
- `update-worker` completes the update job
  - if `UPDATE_WORKER_TARGET_API_BASE_URL` is set, it POSTs to that API
  - if `UPDATE_WORKER_TARGET_API_BASE_URL` is blank, it completes in `s3_only` mode and S3 remains the final output

Current product assumption:

- downstream display reads the latest approved result from S3 directly

## Directory layout

- `runtime/` is the mounted `/opt/data` volume for Hermes. It is intentionally ignored from git because it will contain secrets, sessions, logs, and persistent state.
- `runtime-full/` is the current active runtime used for the local Ollama-based supervisor setup.
- `runtime-ec2/` is the Docker-to-Docker runtime used when Hermes should call an Ollama sidecar service instead of the host machine.
- `ollama/data/` is the persistent model store for the Ollama container in the EC2 deployment path.
- `seed/` contains files that should be copied into the supervisor runtime before first boot.
- `../ocr_worker/` contains the Python OCR worker service. It is not a second Hermes runtime volume.
- `../update_worker/` contains the deterministic update worker package.

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

If you want the local stack in the shape that is actually used for LINE intake, run this instead:

- `powershell -ExecutionPolicy Bypass -File .\scripts\start-election-local.ps1`

That script:

- starts the Docker Compose stack
- checks whether the LINE relay is already responding
- launches the LINE relay in a separate PowerShell window when needed
- leaves tunnel startup separate so you can choose `ngrok`, `cloudflared`, or `localhost.run`

The service starts with:

- gateway mode enabled
- dashboard enabled on port `9119`
- optional OpenAI-compatible API server on port `8642` when `API_SERVER_ENABLED=true`
- LINE webhook listener on port `8646` when `gateway.platforms.line.enabled=true`
- an `ocr-worker` Python container on the same Docker network
- an optional `update-worker` container available through the `update-worker` Compose profile

Start only the OCR worker container:

- `powershell -ExecutionPolicy Bypass -File .\scripts\start-hermes-ocr-worker.ps1`

Start only the update worker container:

- `powershell -ExecutionPolicy Bypass -File .\scripts\start-update-worker.ps1`

Current OCR worker container behavior:

- consumes OCR jobs from SQS as a dedicated Python worker
- downloads source manifests and `original.bin` from S3
- calls the supervisor's OpenAI-compatible Hermes API to perform OCR and normalization
- writes draft, approval, and OCR job state updates back to S3
- pushes approval prompts back to LINE when OCR finishes successfully

Current update worker behavior:

- resolves `UPDATE_WORKER_*` environment variables through Docker Compose
- consumes approved update jobs from SQS when `UPDATE_WORKER_QUEUE_URL` is configured
- reads update job manifests from S3 and marks them `processing`, then `completed` or `failed`
- POSTs approved payloads to `UPDATE_WORKER_TARGET_API_BASE_URL + /updates` when that variable is set
- if `UPDATE_WORKER_TARGET_API_BASE_URL` is blank, completes the job in `s3_only` mode instead of failing
- keeps the related source message in `approved` state for `s3_only` mode, because S3 is already the canonical result store
- remains an optional Compose profile so it does not affect the default supervisor stack until its queue and target API are configured

## Minimum env for the current local path

Required for the current LINE -> OCR -> approval -> S3 workflow:

- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_CHANNEL_SECRET`
- `LINE_PUBLIC_URL`
- `SUPERVISOR_STORAGE_BACKEND=s3`
- `SUPERVISOR_S3_BUCKET`
- `SUPERVISOR_S3_REGION`
- `SUPERVISOR_S3_PREFIX`
- `SUPERVISOR_OCR_QUEUE_URL`
- `OCR_WORKER_QUEUE_URL`
- `OCR_WORKER_S3_BUCKET`
- `OCR_WORKER_S3_PREFIX`
- `UPDATE_WORKER_QUEUE_URL`
- `UPDATE_WORKER_S3_BUCKET`
- `UPDATE_WORKER_S3_PREFIX`

Optional in the current implementation:

- `UPDATE_WORKER_TARGET_API_BASE_URL`
  - leave blank when S3 is the final output
  - set it only when an external `/updates` API really exists
- `SUPERVISOR_S3_ENDPOINT`
- `SUPERVISOR_OCR_QUEUE_REGION`
- `SUPERVISOR_UPDATE_QUEUE_REGION`

## Local intake implementation slice

The repo now includes a separate Python intake slice for the supervisor role at `hermes/supervisor/services/intake_server.py`.

For local LINE webhook wiring, the repo also includes `hermes/supervisor/services/line_webhook_relay.py`.

Current behavior in this slice:

- accept LINE-style webhook payloads on `POST /line/events`
- classify events into `image`, `approval_command`, `correction_command`, `text`, or unsupported
- persist `source_message` manifests to a local filesystem state root
- persist `source_message` manifests and derived indexes through the configured state backend
- for image events, write local metadata that mirrors the planned S3 layout
- when `LINE_CHANNEL_ACCESS_TOKEN` is available, fetch image content from LINE and persist `original.bin` through the configured upload backend
- create event-level and message-level dedupe indexes
- maintain a latest session pointer per LINE conversation
- without a LINE token in the process environment, image metadata stays in `pending_line_content_fetch` as a fallback

Run it locally from the repository root:

- `python -m hermes.supervisor.intake_server`
- the script preloads `hermes/supervisor/.env` by default; override with `--env-file <path>` when needed

Run the local LINE relay from the repository root:

- `python -m hermes.supervisor.line_webhook_relay`
- or `powershell -ExecutionPolicy Bypass -File .\scripts\start-line-webhook-relay.ps1`

Optional environment variables:

- `SUPERVISOR_HOST`
- `SUPERVISOR_PORT`
- `SUPERVISOR_STATE_ROOT`
- `SUPERVISOR_STORAGE_BACKEND`
- `SUPERVISOR_RELAY_HOST`
- `SUPERVISOR_RELAY_PORT`
- `SUPERVISOR_HERMES_LINE_UPSTREAM_URL`
- `SUPERVISOR_S3_BUCKET`
- `SUPERVISOR_S3_REGION`
- `SUPERVISOR_S3_ENDPOINT`
- `SUPERVISOR_S3_PREFIX`

Default local endpoints:

- health: `GET http://127.0.0.1:8650/health`
- intake: `POST http://127.0.0.1:8650/line/events`
- relay health: `GET http://127.0.0.1:8646/line/webhook/health`
- relay webhook: `POST http://127.0.0.1:8646/line/webhook`

Scope note:

- for local development, `line_webhook_relay.py` terminates the public LINE webhook path at the intake persistence slice and avoids forwarding webhook POSTs into Hermes directly
- when `SUPERVISOR_STORAGE_BACKEND=local-mock`, this slice persists to local files as a stand-in for the S3 manifest layout
- when `SUPERVISOR_STORAGE_BACKEND=s3`, the intake slice writes `source_message` manifests, derived indexes, `original.bin`, and `metadata.json` to S3 directly

Approval and correction behavior:

- `ยืนยัน` approves the current draft revision and sends a success reply back to LINE
- `แก้ไข ...` creates a corrected draft revision and sends a fresh approval prompt
- when `LINE_LIFF_CORRECTION_ID` is configured, the correction button opens a LIFF form that submits a correction command back into the same chat
- ambiguous correction text receives a guidance reply instead of silently doing nothing
- approval-like typos such as `ยันยืน` receive a reply telling the user to send `ยืนยัน` exactly

## LINE webhook setup

The supervisor is now preconfigured to enable the bundled LINE platform plugin. To finish the setup you must fill the LINE environment variables in `.env` or `.env.ec2`.

The currently verified local combination is `.env` + `ngrok` + LINE OA.

Required secrets:

- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_CHANNEL_SECRET`

Required routing values:

- At least one allowlist: `LINE_ALLOWED_USERS`, `LINE_ALLOWED_GROUPS`, `LINE_ALLOWED_ROOMS`, or `LINE_ALLOW_ALL_USERS=true` for development only
- `LINE_PUBLIC_URL` pointing at your public HTTPS base URL
- `LINE_LIFF_CORRECTION_ID` when you want the `แก้ไข` button to open a correction form

Webhook details:

- Health check: `https://<your-public-url>/line/webhook/health`
- Webhook URL to register in LINE Developers Console: `https://<your-public-url>/line/webhook`
- LIFF endpoint to register in LINE Developers Console: `https://<your-public-url>/line/liff/correction`
- Container bind port: `8646` by default

Behavior choices already set in `config.yaml` for LINE:

- `gateway.platforms.line.enabled: true`
- `display.interim_assistant_messages: false`
- `display.platforms.line.tool_progress: off`
- `display.platforms.line.streaming: false`

These keep the LINE reply token available longer and align with Hermes' slow-response postback flow.

## Useful commands

- `docker compose --env-file hermes/supervisor/.env -f hermes/supervisor/docker-compose.yml up -d`
- `docker compose --profile update-worker --env-file hermes/supervisor/.env -f hermes/supervisor/docker-compose.yml up -d update-worker`
- `powershell -ExecutionPolicy Bypass -File .\scripts\start-line-webhook-relay.ps1`
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

3. In another terminal, start the LINE relay:

	`powershell -ExecutionPolicy Bypass -File .\scripts\start-line-webhook-relay.ps1`

4. In a third terminal, start ngrok:

	`powershell -ExecutionPolicy Bypass -File .\scripts\start-line-ngrok-tunnel.ps1`

5. ngrok will show a public HTTPS URL such as `https://abc123.ngrok-free.app`.
6. Set `LINE_PUBLIC_URL` in `hermes/supervisor/.env` to that base URL.
7. Register `https://abc123.ngrok-free.app/line/webhook` in the LINE Developers Console.

Notes:

- Free ngrok tunnels are temporary and the URL can change when you reconnect.
- Keep the relay and ngrok terminals open while testing.
- Normal browser-style checks against free ngrok domains can hit `ERR_NGROK_6024`. LINE webhook delivery can still work, but if you want a quick health check from a browser or PowerShell, prefer `http://localhost:8646/line/webhook/health` locally.
- If your ngrok plan supports reserved domains, you can request one with:

	`powershell -ExecutionPolicy Bypass -File .\scripts\start-line-ngrok-tunnel.ps1 -Hostname https://linebot.example.ngrok.app`

## Important notes

- Do not point any other Hermes container at `hermes/supervisor/runtime`.
- The supervisor role is intentionally lightweight. It should receive events, validate, persist, dedupe, enqueue, and ask for approval. It should not run OCR inline.
- This implementation now includes a local supervisor intake slice for persistence, dedupe, S3-backed OCR/update workflows, and dedicated OCR/update workers.
