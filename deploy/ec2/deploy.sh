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

trim_env_value() {
  sed -e 's/^"//' -e 's/"$//'
}

escape_sed_replacement() {
  printf '%s' "$1" | sed -e 's/[\\&#]/\\&/g'
}

update_yaml_key_in_block() {
  local file_path="$1"
  local block_name="$2"
  local key_name="$3"
  local key_value="$4"

  python3 - "$file_path" "$block_name" "$key_name" "$key_value" <<'PY'
from pathlib import Path
import re
import sys

file_path, block_name, key_name, key_value = sys.argv[1:5]
text = Path(file_path).read_text(encoding="utf-8")
block_pattern = re.compile(
    rf'^{re.escape(block_name)}:\n(?:^[ \t].*\n?)*',
    re.MULTILINE,
)
match = block_pattern.search(text)
if not match:
    sys.exit(0)

block_text = match.group(0)
key_pattern = re.compile(rf'^([ \t]*){re.escape(key_name)}:.*$', re.MULTILINE)
if not key_pattern.search(block_text):
    sys.exit(0)

updated_block = key_pattern.sub(
    lambda found: f"{found.group(1)}{key_name}: {key_value}",
    block_text,
    count=1,
)
Path(file_path).write_text(
    text[:match.start()] + updated_block + text[match.end():],
    encoding="utf-8",
)
PY
}

model_api_base="$(
  {
    sed -n 's/^OLLAMA_API_BASE=//p' .env
    sed -n 's/^OPENAI_API_BASE=//p' .env
  } |
    tail -n 1 |
    trim_env_value
)"
hermes_provider="$(
  sed -n 's/^HERMES_PROVIDER=//p' .env |
    tail -n 1 |
    trim_env_value
)"
hermes_model="$(
  sed -n 's/^HERMES_MODEL=//p' .env |
    tail -n 1 |
    trim_env_value
)"
aws_region="$(
  {
    sed -n 's/^AWS_REGION=//p' .env
    sed -n 's/^AWS_DEFAULT_REGION=//p' .env
  } |
    tail -n 1 |
    trim_env_value
)"
hermes_runtime_dir="$(
  sed -n 's/^HERMES_RUNTIME_DIR=//p' .env |
    tail -n 1 |
    trim_env_value
)"

if [[ -n "$hermes_runtime_dir" && -f "$hermes_runtime_dir/config.yaml" ]]; then
  runtime_config="$hermes_runtime_dir/config.yaml"

  if [[ -n "$hermes_provider" ]]; then
    update_yaml_key_in_block "$runtime_config" model provider "$hermes_provider"
  fi

  if [[ -n "$hermes_model" ]]; then
    update_yaml_key_in_block "$runtime_config" model default "$hermes_model"
  fi

  if [[ -n "$model_api_base" ]]; then
    update_yaml_key_in_block "$runtime_config" model base_url "$model_api_base"
  elif [[ "$hermes_provider" == "bedrock" ]]; then
    update_yaml_key_in_block "$runtime_config" model base_url "''"
  fi

  if [[ "$hermes_provider" == "bedrock" && -n "$aws_region" ]]; then
    update_yaml_key_in_block "$runtime_config" bedrock region "$aws_region"
  fi
fi

docker compose --env-file .env config --quiet
docker compose --env-file .env --profile production up -d --build --remove-orphans
docker compose ps
