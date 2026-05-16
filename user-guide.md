# Usage Guide

[中文](user-guide-Chinese.md)

## 1. Start The Proxy

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Create config:

```bash
cp config.example.toml config.toml
```

Set your DeepSeek key in `config.toml`, or pass it to the start script:

```bash
./start.sh sk-your-deepseek-key
```

The default server address is:

```text
http://127.0.0.1:8000
```

## 2. Point Codex CLI At The Proxy

Use a Codex CLI provider like this:

```toml
model_provider = "ds-proxy"
model = "gpt-5.5"

[model_providers.ds-proxy]
name = "ds-proxy"
base_url = "http://127.0.0.1:8000/v1"
wire_api = "responses"
requires_openai_auth = true
```

Set a placeholder OpenAI key for Codex CLI:

```bash
export OPENAI_API_KEY=not-used-by-proxy
```

## 3. Run A Codex Task

```bash
codex exec --model gpt-5.5 "检查当前项目结构并总结主要文件"
```

If you do not want to edit your Codex config yet, pass provider settings inline:

```bash
OPENAI_API_KEY=not-used-by-proxy codex exec \
  -c 'model_provider="ds-proxy"' \
  -c 'model_providers.ds-proxy.name="ds-proxy"' \
  -c 'model_providers.ds-proxy.base_url="http://127.0.0.1:8000/v1"' \
  -c 'model_providers.ds-proxy.wire_api="responses"' \
  -c 'model_providers.ds-proxy.requires_openai_auth=true' \
  --model gpt-5.5 \
  "检查当前项目结构并总结主要文件"
```

## 4. Model Mapping

Codex model names are mapped locally before calling DeepSeek. The default mapping is in `config.example.toml`.

Common defaults:

```text
gpt-5.5       -> deepseek-v4-pro
gpt-5.4       -> deepseek-v4-pro
gpt-5.4-mini  -> deepseek-v4-flash
gpt-5.2       -> deepseek-v4-pro
```

Override them in `config.toml`:

```toml
[model.mapping]
"gpt-5.5" = "deepseek-v4-pro"
"gpt-5.4-mini" = "deepseek-v4-flash"
```

## 5. State Storage

Codex CLI often continues a session through `previous_response_id`. In-memory state is enough for short tests, but it is lost when the proxy restarts.

For normal use, enable SQLite:

```toml
[state]
backend = "sqlite"
db_path = ".proxy-state/responses-state.sqlite3"
ttl_seconds = 604800
max_entries = 10000
```

## 6. Web Search

Codex `web_search` is not sent directly to DeepSeek. The proxy can replace it with an MCP-backed function call.

Example config:

```toml
[web_search]
backend = "mcp"
mcp_command = "uvx"
mcp_args = ["duckduckgo-mcp-server"]
mcp_tool = "search"
mcp_framing = "jsonl"
max_results = 5
timeout_seconds = 30
```

If the MCP server is unreliable, switch to another MCP search server and update `mcp_command`, `mcp_args`, `mcp_tool`, and `mcp_framing`.

## 7. Debug Logs

Use:

```bash
./start-debug.sh sk-your-deepseek-key
```

It writes:

```text
logs/requests.jsonl
logs/sse.log
logs/errors.jsonl
logs/server.log
```

These logs are useful when Codex CLI reports a stream error or a tool-call failure.
