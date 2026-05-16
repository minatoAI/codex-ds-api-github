#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <deepseek_api_key>"
  echo "Optional env: HOST=127.0.0.1 PORT=8000 DS_RESPONSES_CONFIG=config.toml LOG_DIR=logs"
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/logs}"
mkdir -p "$LOG_DIR"

source "$SCRIPT_DIR/.venv/bin/activate"

export DEEPSEEK_API_KEY="$1"
export DS_RESPONSES_DEBUG_REQUESTS_PATH="${DS_RESPONSES_DEBUG_REQUESTS_PATH:-$LOG_DIR/requests.jsonl}"
export DS_RESPONSES_DEBUG_SSE_PATH="${DS_RESPONSES_DEBUG_SSE_PATH:-$LOG_DIR/sse.log}"
export DS_RESPONSES_DEBUG_ERRORS_PATH="${DS_RESPONSES_DEBUG_ERRORS_PATH:-$LOG_DIR/errors.jsonl}"

echo "Writing debug logs to $LOG_DIR"
echo "Requests: $DS_RESPONSES_DEBUG_REQUESTS_PATH"
echo "SSE:      $DS_RESPONSES_DEBUG_SSE_PATH"
echo "Errors:   $DS_RESPONSES_DEBUG_ERRORS_PATH"

uvicorn ds_responses_proxy.app:app \
  --host "${HOST:-127.0.0.1}" \
  --port "${PORT:-8000}" \
  2>&1 | tee "$LOG_DIR/server.log"
