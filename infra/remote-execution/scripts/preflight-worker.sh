#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
python=${BUCKOS_PREFLIGHT_PYTHON:-python3}

if ! command -v "$python" >/dev/null 2>&1; then
  printf 'FAIL tool-python3 %s not found\n' "$python"
  exit 1
fi

exec "$python" "$script_dir/preflight_worker.py" "$@"
