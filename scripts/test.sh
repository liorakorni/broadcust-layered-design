#!/bin/bash
# Unit tests. Integration tests are skipped unless RUN_INTEGRATION=1 (they make
# real paid API calls).

set -e
cd "$(dirname "$0")/.."

PY=".venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "No .venv — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest"
  exit 1
fi

# Credentials are only loaded for integration runs, so an ordinary test run
# cannot reach a provider even by accident.
if [[ "$RUN_INTEGRATION" == "1" ]]; then
  if [[ ! -f config.dev.json ]]; then
    echo "RUN_INTEGRATION=1 but config.dev.json is missing."
    exit 1
  fi
  eval "$("$PY" - <<'PY'
import json, shlex
config = json.load(open("config.dev.json"))
for key, value in config.items():
    print(f"export {key.upper()}={shlex.quote(str(value))}")
PY
)"
fi

"$PY" -m pytest "$@"
