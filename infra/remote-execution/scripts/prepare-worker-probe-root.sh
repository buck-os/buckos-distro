#!/usr/bin/env bash
set -euo pipefail
umask 077
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset CDPATH ENV BASH_ENV PYTHONPATH PYTHONHOME || true

script_dir=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
python=python3

if ! command -v "$python" >/dev/null 2>&1; then
  printf 'error: Python interpreter %s not found\n' "$python" >&2
  exit 1
fi

exec "$python" "$script_dir/prepare_worker_probe_root.py" "$@"
