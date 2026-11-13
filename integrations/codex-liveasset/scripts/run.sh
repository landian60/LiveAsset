#!/bin/bash
set -euo pipefail
liveasset_root="$(cd "$(dirname "$0")/.." && pwd)"
liveasset_python="${LIVEASSET_PYTHON:-$liveasset_root/.venv/bin/python}"
if [[ ! -x "$liveasset_python" ]]; then
  liveasset_python=python3
fi
if ! "$liveasset_python" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
  if [[ "${1:-}" == hook ]]; then
    echo '{"systemMessage":"LiveAsset needs Python 3.11+. Run scripts/setup.sh with that Python before enabling its hooks; this turn was not verified."}'
    exit 0
  fi
  echo 'LiveAsset needs Python 3.11+. Run scripts/setup.sh with that Python.' >&2
  exit 1
fi
if [[ "${1:-}" == mcp ]]; then
  shift
  exec "$liveasset_python" "$liveasset_root/scripts/mcp_server.py" "$@"
fi
exec "$liveasset_python" "$liveasset_root/scripts/liveasset.py" "$@"
