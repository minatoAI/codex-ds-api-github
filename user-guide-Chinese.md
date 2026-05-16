# 用户指南

[English](user-guide.md)

本文说明如何安装、配置和使用 `codex-ds-api`，让 Codex CLI 通过本地代理调用 DeepSeek。

## 1. 工作原理

调用链如下：

```text
Codex CLI
  -> http://127.0.0.1:8000/v1/responses
  -> codex-ds-api 本地代理
  -> https://api.deepseek.com/chat/completions
```

代理负责完成几件事：

- 把 Codex CLI 的 Responses API 请求转换为 DeepSeek Chat Completions 请求。
- 把 DeepSeek 的普通响应或流式响应转换回 Codex CLI 需要的 Responses/SSE 格式。
- 保存 `previous_response_id` 对应的会话状态，让 Codex CLI 可以继续上下文。
- 转换函数工具、自定义工具、上下文压缩、推理内容、可选 Web Search 和可选图片摘要。

## 2. 安装

进入项目目录后创建虚拟环境并安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

确认安装后，项目会提供 Python 包 `ds_responses_proxy`，入口服务是 `ds_responses_proxy.app:app`。

## 3. 创建配置

复制配置模板：

```bash
cp config.example.toml config.toml
```

最小配置如下：

```toml
[deepseek]
api_key = "sk-your-deepseek-key"
base_url = "https://api.deepseek.com"
model = "deepseek-v4-pro"
```

`api_key` 也可以不写入文件，改用环境变量：

```bash
export DEEPSEEK_API_KEY=sk-your-deepseek-key
```

如需使用其他配置文件路径：

```bash
export DS_RESPONSES_CONFIG=/path/to/config.toml
```

## 4. 启动代理

方式一：直接用 `uvicorn` 启动。

```bash
source .venv/bin/activate
uvicorn ds_responses_proxy.app:app --host 127.0.0.1 --port 8000
```

方式二：使用启动脚本，并把 DeepSeek Key 作为第一个参数传入。

```bash
./start.sh sk-your-deepseek-key
```

脚本支持这些环境变量：

```bash
HOST=127.0.0.1 PORT=8000 DS_RESPONSES_CONFIG=config.toml ./start.sh sk-your-deepseek-key
```

检查服务是否运行：

```bash
curl http://127.0.0.1:8000/health
```

正常返回：

```json
{"status":"ok"}
```

## 5. 配置 Codex CLI

在 Codex CLI 配置中添加代理 provider：

```toml
model_provider = "ds-proxy"
model = "gpt-5.5"

[model_providers.ds-proxy]
name = "ds-proxy"
base_url = "http://127.0.0.1:8000/v1"
wire_api = "responses"
requires_openai_auth = true
```

Codex CLI 需要非空 `OPENAI_API_KEY`。这里可以设置占位值，因为代理真正使用的是 DeepSeek Key：

```bash
export OPENAI_API_KEY=not-used-by-proxy
```

## 6. 运行一次任务

如果已经写入 Codex CLI 配置：

```bash
codex exec --model gpt-5.5 "检查当前项目结构并总结主要文件"
```

如果暂时不想改全局配置，可在命令中临时指定 provider：

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

## 7. 模型映射

Codex CLI 发出的模型名会在代理内映射为 DeepSeek 模型名。默认映射来自 `config.example.toml`：

```text
gpt-5.5              -> deepseek-v4-pro
gpt-5.4              -> deepseek-v4-pro
gpt-5.4-mini         -> deepseek-v4-flash
gpt-5.3-codex        -> deepseek-v4-pro
gpt-5.3-codex-spark  -> deepseek-v4-flash
gpt-5.2              -> deepseek-v4-pro
```

在 `config.toml` 中覆盖映射：

```toml
[model.mapping]
"gpt-5.5" = "deepseek-v4-pro"
"gpt-5.4-mini" = "deepseek-v4-flash"
```

默认情况下，未命中映射的 Codex 模型会落到 `[deepseek].model`。如果希望严格限制模型名：

```toml
[model]
mapping_strict = true
```

开启后，未配置映射的模型会返回 400 错误。

## 8. 会话状态

Codex CLI 会用 `previous_response_id` 继续上下文。代理支持两种状态后端。

短测试可使用内存：

```toml
[state]
backend = "memory"
ttl_seconds = 604800
max_entries = 10000
```

内存状态在代理重启后会丢失。日常使用建议改为 SQLite：

```toml
[state]
backend = "sqlite"
db_path = ".proxy-state/responses-state.sqlite3"
ttl_seconds = 604800
max_entries = 10000
```

`ttl_seconds` 控制状态保留时间，`max_entries` 控制最多保留多少条响应状态。

## 9. Web Search

Codex 的 `web_search` 不会直接传给 DeepSeek。代理会把它转换成名为 `web_search` 的函数工具，并在本地通过 MCP 服务器执行搜索。

示例：

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

字段含义：

- `backend`：当前支持 `mcp`。
- `mcp_command`：启动 MCP 服务器的命令。
- `mcp_args`：传给 MCP 命令的参数数组。
- `mcp_tool`：调用的 MCP 工具名。
- `mcp_framing`：MCP 消息 framing，支持 `jsonl` 或 `content_length`。
- `max_results`：传给搜索工具的最大结果数。
- `timeout_seconds`：初始化和调用 MCP 工具的超时时间。

如果搜索工具返回格式不同，代理会尽量从 `results`、`organic_results`、`web` 或 `items` 中提取标题、链接和摘要；否则会把原始 payload 转成文本。

## 10. 图片输入

DeepSeek 请求最终是文本形式。Codex CLI 传入图片时，代理需要先把图片转换为文本。

只保留图片元数据：

```toml
[image]
preprocessor = "metadata"
```

调用自定义视觉或 OCR 服务：

```toml
[image]
preprocessor = "vision_endpoint"
preprocessor_endpoint = "http://127.0.0.1:9000/describe-image"
```

该端点会收到类似下面的 JSON：

```json
{
  "preprocessor": "vision_endpoint",
  "image_url": "data:image/png;base64,..."
}
```

返回 JSON 可包含 `ocr`、`text`、`description` 或 `caption` 字段。

调用 OpenAI 兼容视觉模型：

```toml
[image]
preprocessor = "openai_compatible"
preprocessor_endpoint = "http://127.0.0.1:11434/v1/chat/completions"
preprocessor_model = "qwen2.5vl:7b"
preprocessor_api_key = ""
preprocessor_prompt = "Extract OCR text, UI elements, layout summary, notable errors, and uncertainty."
```

注意：图片会被转换成文本摘要后再交给 DeepSeek。摘要可能遗漏图片细节，不等价于原生多模态推理。

## 11. 调试日志

启动调试模式：

```bash
./start-debug.sh sk-your-deepseek-key
```

默认日志目录是 `logs`，可通过 `LOG_DIR` 修改：

```bash
LOG_DIR=/tmp/ds-proxy-logs ./start-debug.sh sk-your-deepseek-key
```

调试模式会写入：

```text
logs/requests.jsonl
logs/sse.log
logs/errors.jsonl
logs/server.log
```

也可以单独设置日志文件路径：

```bash
export DS_RESPONSES_DEBUG_REQUESTS_PATH=logs/requests.jsonl
export DS_RESPONSES_DEBUG_SSE_PATH=logs/sse.log
export DS_RESPONSES_DEBUG_ERRORS_PATH=logs/errors.jsonl
```

排查建议：

- Codex CLI 连接失败：检查代理是否启动、`base_url` 是否是 `http://127.0.0.1:8000/v1`。
- 返回 `DEEPSEEK_API_KEY is not configured`：检查 `config.toml` 或 `DEEPSEEK_API_KEY`。
- `Unknown previous_response_id`：如果使用内存状态，确认代理是否重启过；长期会话建议使用 SQLite。
- Web Search 报错：检查 MCP 命令是否可执行、工具名是否正确、framing 是否匹配。
- 图片输入报错：检查 `[image]` 是否配置了 `preprocessor`，以及外部端点是否可用。

## 12. 常用环境变量

```text
DEEPSEEK_API_KEY
DEEPSEEK_BASE_URL
DEEPSEEK_MODEL
DS_RESPONSES_CONFIG
MODEL_MAPPING_STRICT
MODEL_MAPPING_JSON
STATE_BACKEND
STATE_DB_PATH
STATE_TTL_SECONDS
STATE_MAX_ENTRIES
WEB_SEARCH_BACKEND
WEB_SEARCH_MCP_COMMAND
WEB_SEARCH_MCP_ARGS
WEB_SEARCH_MCP_TOOL
WEB_SEARCH_MCP_FRAMING
WEB_SEARCH_MAX_RESULTS
WEB_SEARCH_TIMEOUT_SECONDS
IMAGE_PREPROCESSOR
IMAGE_PREPROCESSOR_ENDPOINT
IMAGE_PREPROCESSOR_API_KEY
IMAGE_PREPROCESSOR_MODEL
IMAGE_PREPROCESSOR_PROMPT
DS_RESPONSES_STREAM_REASONING_SUMMARY
DS_RESPONSES_DEBUG_REQUESTS_PATH
DS_RESPONSES_DEBUG_SSE_PATH
DS_RESPONSES_DEBUG_ERRORS_PATH
```

`WEB_SEARCH_MCP_ARGS` 和 `MODEL_MAPPING_JSON` 需要使用 JSON 字符串，例如：

```bash
export WEB_SEARCH_MCP_ARGS='["duckduckgo-mcp-server"]'
export MODEL_MAPPING_JSON='{"gpt-5.5":"deepseek-v4-pro"}'
```

## 13. 适用边界

本项目适合把 Codex CLI 的常规代码代理请求转发到 DeepSeek。它不适合被当作完整 OpenAI Responses API 服务使用。

已覆盖的重点能力包括：

- 普通文本输入和多轮续接。
- 流式文本输出。
- 函数工具和自定义工具调用。
- Codex 上下文压缩输入项。
- DeepSeek reasoning 内容回传。
- MCP Web Search 替代方案。
- 图片输入的文本化降级。

如果某个 Responses API 字段未被 Codex CLI 常规工作流使用，代理可能不会完整实现它。
