# Hermes Supervisor

This package contains the LINE intake and relay code that sits beside the
official Hermes Agent container in the production Docker Compose stack.

## Production Path

Production is driven from the repository root:

```bash
docker compose --env-file .env --profile production up -d --build
```

The active AWS deployment uses:

- root [`compose.yaml`](../../compose.yaml)
- [`deploy/ec2/deploy.sh`](../../deploy/ec2/deploy.sh)
- [`deploy/ec2/Caddyfile`](../../deploy/ec2/Caddyfile)
- SSM SecureString `/election/compose-env` for the production `.env`

Do not use the old supervisor-local Compose files. The only supported AWS path is
the root Compose stack.

## Runtime Responsibilities

- `line_webhook_relay.py` receives Caddy-forwarded LINE webhook traffic.
- `services/intake_server.py` validates, deduplicates, persists source manifests,
  downloads LINE images, and enqueues OCR jobs.
- `../ocr_worker/__main__.py` consumes SQS jobs, calls the Hermes API, writes draft
  and approval manifests, and sends approval prompts back to LINE.
- `../results_api/app.py` serves approved results from S3 through `/api/*`.

## Compatibility Entrypoints

The top-level files in this directory intentionally remain as compatibility
entrypoints for Docker commands and tests:

- `intake_server.py`
- `line_webhook_relay.py`
- `upload_service.py`

Their implementation lives under `services/`.

## Model Configuration

The production model endpoint is configured through `.env` values from SSM:

- `HERMES_MODEL`
- `ANTHROPIC_API_KEY`
- `OCR_WORKER_HERMES_MODEL`
- `OCR_WORKER_MODEL_NAME`
- `OLLAMA_API_BASE` or `OPENAI_API_BASE`

The current AWS runtime points Hermes at a remote Ollama-compatible `/v1`
endpoint. The Hermes runtime directory is mounted at `/opt/data` and lives
outside git, so model/provider changes may need both `.env` and runtime
`config.yaml` to be aligned.

For local Claude testing, the root Compose stack can pass `ANTHROPIC_API_KEY`
through to the Hermes container. A minimal local setup is `HERMES_MODEL=claude-sonnet-4-6`
with `ANTHROPIC_API_KEY` set and Ollama/OpenAI-compatible base URLs left empty.
