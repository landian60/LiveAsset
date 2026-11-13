#!/bin/bash
set -euo pipefail
liveasset_root="$(cd "$(dirname "$0")/.." && pwd)"
liveasset_python="${1:-python3}"
"$liveasset_python" -c 'import sys; assert sys.version_info >= (3, 11), "LiveAsset needs Python 3.11+"'
"$liveasset_python" -m venv "$liveasset_root/.venv"
"$liveasset_root/.venv/bin/python" -m pip install 'mcp==1.30.0'
