# codex-ds-api

[中文](README-Chinese.md)

Codex CLI DeepSeek adapter.

This project runs a local FastAPI server that accepts Codex CLI Responses API requests and translates them to DeepSeek Chat Completions requests. It then translates DeepSeek responses back into the Responses/SSE shape that Codex CLI expects.

It is not a full OpenAI Responses API implementation. The goal is to make Codex CLI usable with DeepSeek for normal coding-agent workflows.

## Features

- `POST /v1/responses` endpoint for Codex CLI.
- Streaming SSE response events used by Codex CLI.
- Codex model name to DeepSeek model name mapping.
- `previous_response_id` state tracking with memory or SQLite backend.
- Function tool translation.
- Custom/freeform tool translation for tools such as `apply_patch`.
- Codex CLI compact item handling.
- DeepSeek `reasoning_content` round-trip support for tool-call workflows.
- Optional MCP-backed web search replacement for Codex `web_search`.
- Optional image input degradation to metadata or an external vision endpoint.

## Requirements

- Python 3.12+
- DeepSeek API key
- Codex CLI

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configure

```bash
cp config.example.toml config.toml
```

Edit `config.toml`:

```toml
[deepseek]
api_key = "sk-your-deepseek-key"
base_url = "https://api.deepseek.com"
model = "deepseek-v4-pro"
```

For longer Codex sessions, SQLite state is recommended:

```toml
[state]
backend = "sqlite"
db_path = ".proxy-state/responses-state.sqlite3"
ttl_seconds = 604800
max_entries = 10000
```

## Run

```bash
source .venv/bin/activate
uvicorn ds_responses_proxy.app:app --host 127.0.0.1 --port 8000
```

Or pass the DeepSeek key through the start script:

```bash
./start.sh sk-your-deepseek-key
```

Debug logs:

```bash
./start-debug.sh sk-your-deepseek-key
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

## Codex CLI Provider

Configure Codex CLI to use this proxy as a Responses API provider:

```toml
model_provider = "ds-proxy"
model = "gpt-5.5"

[model_providers.ds-proxy]
name = "ds-proxy"
base_url = "http://127.0.0.1:8000/v1"
wire_api = "responses"
requires_openai_auth = true
```

Codex CLI still requires a non-empty OpenAI API key value, but this proxy uses `config.toml` or `DEEPSEEK_API_KEY` to call DeepSeek:

```bash
export OPENAI_API_KEY=not-used-by-proxy
```

## Quick Test

```bash
OPENAI_API_KEY=not-used-by-proxy codex exec \
  -c 'model_provider="ds-proxy"' \
  -c 'model_providers.ds-proxy.name="ds-proxy"' \
  -c 'model_providers.ds-proxy.base_url="http://127.0.0.1:8000/v1"' \
  -c 'model_providers.ds-proxy.wire_api="responses"' \
  -c 'model_providers.ds-proxy.requires_openai_auth=true' \
  --model gpt-5.5 \
  "用一句话介绍你自己"
```

See [USAGE.md](USAGE.md) for a short usage guide.
