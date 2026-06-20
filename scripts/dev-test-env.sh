#!/usr/bin/env sh
set -eu

PYTHON_BIN="${PYTHON_BIN:-python3}"
ARCH="$("$PYTHON_BIN" - <<'PY'
import platform
print(platform.machine() or "unknown")
PY
)"
VENV_DIR="${VENV_DIR:-.venv-$ARCH}"

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install \
  -r hermes/results_api/requirements.txt \
  -r hermes/ocr_worker/requirements.txt \
  -r hermes/supervisor/requirements.relay.txt \
  pytest

printf 'Created %s for %s\n' "$VENV_DIR" "$ARCH"
printf 'Activate with: . %s/bin/activate\n' "$VENV_DIR"
