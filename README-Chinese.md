# codex-ds-api

[English](README.md)

Codex CLI 的 DeepSeek Responses API 适配器。

本项目启动一个本地 FastAPI 服务，接收 Codex CLI 按 OpenAI Responses API 形状发送的请求，将它们转换为 DeepSeek Chat Completions 请求，再把 DeepSeek 的响应转换回 Codex CLI 期望的 Responses/SSE 事件格式。

它不是完整的 OpenAI Responses API 实现。目标是让 Codex CLI 可以在常见代码代理工作流中使用 DeepSeek。

## 功能

- 提供 `POST /v1/responses` 接口，供 Codex CLI 调用。
- 支持 Codex CLI 使用的流式 SSE 响应事件。
- 支持 Codex 模型名到 DeepSeek 模型名的本地映射。
- 支持 `previous_response_id` 会话状态追踪，可使用内存或 SQLite 后端。
- 支持函数工具调用转换。
- 支持自定义/freeform 工具转换，例如 `apply_patch`。
- 支持 Codex CLI 的上下文压缩输入项。
- 支持 DeepSeek `reasoning_content` 在工具调用工作流中的回传。
- 可选：用 MCP 后端替代 Codex `web_search` 工具。
- 可选：将图片输入降级为元数据、外部视觉端点结果或 OpenAI 兼容视觉模型摘要。

## 环境要求

- Python 3.12+
- DeepSeek API Key
- Codex CLI

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 配置

复制示例配置：

```bash
cp config.example.toml config.toml
```

编辑 `config.toml`，至少设置 DeepSeek API Key：

```toml
[deepseek]
api_key = "sk-your-deepseek-key"
base_url = "https://api.deepseek.com"
model = "deepseek-v4-pro"
```

也可以通过环境变量提供 Key：

```bash
export DEEPSEEK_API_KEY=sk-your-deepseek-key
```

较长的 Codex 会话建议使用 SQLite 保存状态：

```toml
[state]
backend = "sqlite"
db_path = ".proxy-state/responses-state.sqlite3"
ttl_seconds = 604800
max_entries = 10000
```

## 启动服务

开发方式启动：

```bash
source .venv/bin/activate
uvicorn ds_responses_proxy.app:app --host 127.0.0.1 --port 8000
```

或使用脚本传入 DeepSeek Key：

```bash
./start.sh sk-your-deepseek-key
```

需要调试日志时：

```bash
./start-debug.sh sk-your-deepseek-key
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

返回：

```json
{"status":"ok"}
```

## 配置 Codex CLI

在 Codex CLI 配置中添加一个 Responses API provider：

```toml
model_provider = "ds-proxy"
model = "gpt-5.5"

[model_providers.ds-proxy]
name = "ds-proxy"
base_url = "http://127.0.0.1:8000/v1"
wire_api = "responses"
requires_openai_auth = true
```

Codex CLI 仍要求存在非空 `OPENAI_API_KEY`，但本代理不会用它调用 DeepSeek。DeepSeek 调用使用 `config.toml` 或 `DEEPSEEK_API_KEY`：

```bash
export OPENAI_API_KEY=not-used-by-proxy
```

## 快速测试

不改 Codex CLI 全局配置时，可以直接用命令行覆盖 provider：

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

## 常用配置

### 模型映射

默认映射见 `config.example.toml`。常见默认值：

```text
gpt-5.5              -> deepseek-v4-pro
gpt-5.4              -> deepseek-v4-pro
gpt-5.4-mini         -> deepseek-v4-flash
gpt-5.3-codex        -> deepseek-v4-pro
gpt-5.3-codex-spark  -> deepseek-v4-flash
gpt-5.2              -> deepseek-v4-pro
```

可在 `config.toml` 覆盖：

```toml
[model.mapping]
"gpt-5.5" = "deepseek-v4-pro"
"gpt-5.4-mini" = "deepseek-v4-flash"
```

如果希望未配置映射的模型直接报错：

```toml
[model]
mapping_strict = true
```

### Web Search

DeepSeek 不直接接收 Codex 的托管 `web_search`。本代理可以把它替换为 MCP 工具调用：

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

MCP 服务器不稳定时，可替换为其他搜索 MCP，并同步修改 `mcp_command`、`mcp_args`、`mcp_tool` 和 `mcp_framing`。

### 图片输入

DeepSeek 上游按文本请求接收内容，因此图片输入需要先转换为文本摘要。可选模式包括：

```toml
[image]
preprocessor = "metadata"
```

或使用外部视觉/OCR 端点：

```toml
[image]
preprocessor = "vision_endpoint"
preprocessor_endpoint = "http://127.0.0.1:9000/describe-image"
```

或使用 OpenAI 兼容视觉端点：

```toml
[image]
preprocessor = "openai_compatible"
preprocessor_endpoint = "http://127.0.0.1:11434/v1/chat/completions"
preprocessor_model = "qwen2.5vl:7b"
preprocessor_api_key = ""
```

## 调试

使用调试脚本：

```bash
./start-debug.sh sk-your-deepseek-key
```

默认写入：

```text
logs/requests.jsonl
logs/sse.log
logs/errors.jsonl
logs/server.log
```

当 Codex CLI 报流式响应错误、工具调用失败或状态续接异常时，优先查看这些日志。

## 更多说明

完整使用指南见 [user-guide-Chinese.md](user-guide-Chinese.md)。
