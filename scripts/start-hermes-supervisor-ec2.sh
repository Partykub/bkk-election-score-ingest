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

docker compose --env-file "$COMPOSE_ENV_PATH" -f "$COMPOSE_PATH" up -d
