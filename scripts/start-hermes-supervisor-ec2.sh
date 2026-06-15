#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
SUPERVISOR_ROOT="$REPO_ROOT/hermes/supervisor"
COMPOSE_PATH="$SUPERVISOR_ROOT/docker-compose.ec2.yml"
COMPOSE_ENV_PATH="$SUPERVISOR_ROOT/.env.ec2"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required but was not found in PATH." >&2
  exit 1
fi

if [ ! -f "$COMPOSE_ENV_PATH" ]; then
  echo "Missing $COMPOSE_ENV_PATH. Copy hermes/supervisor/.env.ec2.example to .env.ec2 and set your values first." >&2
  exit 1
fi

env_value() {
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$COMPOSE_ENV_PATH" | tail -n 1
}

require_value() {
  name=$1
  value=$(env_value "$name")
  case "$value" in
    ""|change-this*|replace-with*)
      echo "$name must be set to a real value in $COMPOSE_ENV_PATH." >&2
      exit 1
      ;;
  esac
  printf '%s' "$value"
}

if [ "$(env_value API_SERVER_ENABLED)" != "true" ]; then
  echo "API_SERVER_ENABLED must be true because ocr-worker calls the Hermes API on port 8642." >&2
  exit 1
fi

API_SERVER_KEY_VALUE=$(require_value API_SERVER_KEY)
OCR_WORKER_HERMES_API_KEY_VALUE=$(require_value OCR_WORKER_HERMES_API_KEY)
if [ "$API_SERVER_KEY_VALUE" != "$OCR_WORKER_HERMES_API_KEY_VALUE" ]; then
  echo "OCR_WORKER_HERMES_API_KEY must exactly match API_SERVER_KEY." >&2
  exit 1
fi

RUNTIME_DIR=$(env_value HERMES_SUPERVISOR_RUNTIME_DIR)
RUNTIME_DIR=${RUNTIME_DIR:-./runtime-ec2}
RUNTIME_CONFIG="$SUPERVISOR_ROOT/$RUNTIME_DIR/config.yaml"
if [ -f "$RUNTIME_CONFIG" ] &&
   grep -Eq '^[[:space:]]*provider:[[:space:]]*custom(:[^[:space:]]+)?[[:space:]]*$' "$RUNTIME_CONFIG" &&
   grep -Eq '^[[:space:]]*base_url:[[:space:]]*https://' "$RUNTIME_CONFIG"; then
  require_value MODEL_API_KEY >/dev/null
  if ! grep -Eq '^[[:space:]]*key_env:[[:space:]]*MODEL_API_KEY[[:space:]]*$' "$RUNTIME_CONFIG"; then
    echo "$RUNTIME_CONFIG must use a named custom provider with key_env: MODEL_API_KEY." >&2
    exit 1
  fi
fi

docker compose --env-file "$COMPOSE_ENV_PATH" -f "$COMPOSE_PATH" up -d
