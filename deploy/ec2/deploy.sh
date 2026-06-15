#!/usr/bin/env bash
set -euo pipefail

repo_dir="${1:-/opt/election}"
cd "$repo_dir"

if [[ -n "${ENV_PARAMETER:-}" ]]; then
  aws ssm get-parameter \
    --name "$ENV_PARAMETER" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text > .env
  chmod 0600 .env
fi

if [[ ! -f .env ]]; then
  echo "Missing $repo_dir/.env; set ENV_PARAMETER or create the file" >&2
  exit 1
fi

openai_api_base="$(
  sed -n 's/^OPENAI_API_BASE=//p' .env |
    tail -n 1 |
    sed -e 's/^"//' -e 's/"$//'
)"
hermes_runtime_dir="$(
  sed -n 's/^HERMES_RUNTIME_DIR=//p' .env |
    tail -n 1 |
    sed -e 's/^"//' -e 's/"$//'
)"

if [[ -n "$openai_api_base" && -n "$hermes_runtime_dir" &&
      -f "$hermes_runtime_dir/config.yaml" ]]; then
  sed -i \
    "0,/^[[:space:]]*base_url:/s#^[[:space:]]*base_url:.*#  base_url: ${openai_api_base}#" \
    "$hermes_runtime_dir/config.yaml"
fi

docker compose --env-file .env config --quiet
docker compose --env-file .env --profile production up -d --build --remove-orphans
docker compose ps
