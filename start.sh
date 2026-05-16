#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <deepseek_api_key>"
  echo "Optional env: HOST=127.0.0.1 PORT=8000 DS_RESPONSES_CONFIG=config.toml"
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/.venv/bin/activate"

export DEEPSEEK_API_KEY="$1"

uvicorn ds_responses_proxy.app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
