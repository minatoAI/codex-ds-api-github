from __future__ import annotations

import asyncio
import json

from .errors import bad_request


class WebSearchRunner:
    def __init__(
        self,
        backend: str | None,
        mcp_command: str,
        mcp_args: list[str],
        mcp_tool: str,
        mcp_framing: str = "jsonl",
        max_results: int = 5,
        timeout_seconds: int = 30,
    ) -> None:
        self.backend = backend or "mcp"
        self.mcp_command = mcp_command
        self.mcp_args = mcp_args
        self.mcp_tool = mcp_tool
        self.mcp_framing = mcp_framing
        self.max_results = max_results
        self.timeout_seconds = timeout_seconds

    def require_configured(self) -> None:
        if self.backend != "mcp":
            raise bad_request("web_search.backend must be 'mcp'.")
        if not self.mcp_command or not self.mcp_tool:
            raise bad_request("web_search MCP requires mcp_command and mcp_tool.")
        if self.mcp_framing not in ("jsonl", "content_length"):
            raise bad_request("web_search.mcp_framing must be 'jsonl' or 'content_length'.")

    async def search(self, query: str) -> str:
        self.require_configured()
        client = MCPStdioClient(
            [self.mcp_command, *self.mcp_args],
            framing=self.mcp_framing,
            timeout_seconds=self.timeout_seconds,
        )
        try:
            await client.start()
            await client.initialize()
            payload = await client.call_tool(
                self.mcp_tool,
                {
                    "query": query,
                    "max_results": self.max_results,
                },
            )
            return _format_mcp_tool_result(payload, max_results=self.max_results)
        finally:
            await client.close()


class MCPStdioClient:
    def __init__(self, command: list[str], framing: str = "jsonl", timeout_seconds: int = 30) -> None:
        self.command = command
        self.framing = framing
        self.timeout_seconds = timeout_seconds
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._read_buffer = b""

    async def start(self) -> None:
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise bad_request(f"Failed to start web_search MCP server: {exc}") from exc

    async def initialize(self) -> None:
        response = await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ds-responses-proxy", "version": "0.1.0"},
            },
        )
        if not isinstance(response, dict):
            raise bad_request("web_search MCP initialize response was invalid.")
        await self.notify("notifications/initialized", {})

    async def call_tool(self, name: str, arguments: dict) -> object:
        return await self.request("tools/call", {"name": name, "arguments": arguments})

    async def request(self, method: str, params: dict) -> object:
        request_id = self._next_id
        self._next_id += 1
        try:
            return await asyncio.wait_for(
                self._request_unbounded(request_id, method, params),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as exc:
            raise bad_request(
                f"web_search MCP {method} timed out after {self.timeout_seconds} seconds."
            ) from exc

    async def _request_unbounded(self, request_id: int, method: str, params: dict) -> object:
        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = await self._read()
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise bad_request(f"web_search MCP {method} failed: {message['error']}")
            return message.get("result")

    async def notify(self, method: str, params: dict) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                process.kill()
                await process.wait()

    async def _write(self, message: dict) -> None:
        if self.process is None or self.process.stdin is None:
            raise bad_request("web_search MCP server is not running.")
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        if self.framing == "content_length":
            header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            self.process.stdin.write(header + body)
        else:
            self.process.stdin.write(body + b"\n")
        await self.process.stdin.drain()

    async def _read(self) -> dict:
        if self.process is None or self.process.stdout is None:
            raise bad_request("web_search MCP server is not running.")
        payload = await self._read_message_bytes()
        try:
            message = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise bad_request(f"web_search MCP server returned invalid JSON: {payload!r}") from exc
        if not isinstance(message, dict):
            raise bad_request("web_search MCP server returned a non-object JSON-RPC message.")
        return message

    async def _read_message_bytes(self) -> bytes:
        while True:
            separator_index = self._read_buffer.find(b"\r\n\r\n")
            if separator_index >= 0:
                header_bytes = self._read_buffer[:separator_index]
                content_length = _content_length(header_bytes)
                if content_length is None:
                    self._read_buffer = self._read_buffer[separator_index + 4:]
                    continue
                message_start = separator_index + 4
                message_end = message_start + content_length
                while len(self._read_buffer) < message_end:
                    self._read_buffer += await self._read_stdout_chunk()
                payload = self._read_buffer[message_start:message_end]
                self._read_buffer = self._read_buffer[message_end:]
                return payload

            newline_index = self._read_buffer.find(b"\n")
            if newline_index >= 0:
                line = self._read_buffer[:newline_index]
                self._read_buffer = self._read_buffer[newline_index + 1:]
                stripped = line.strip()
                if stripped:
                    return stripped

            self._read_buffer += await self._read_stdout_chunk()

    async def _read_stdout_chunk(self) -> bytes:
        if self.process is None or self.process.stdout is None:
            raise bad_request("web_search MCP server is not running.")
        chunk = await self.process.stdout.read(4096)
        if chunk:
            return chunk
        stderr = ""
        if self.process.stderr is not None:
            stderr_bytes = await self.process.stderr.read()
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        detail = f": {stderr}" if stderr else ""
        raise bad_request(f"web_search MCP server exited before responding{detail}")


def _content_length(header_bytes: bytes) -> int | None:
    for line in header_bytes.decode("ascii", errors="ignore").splitlines():
        name, separator, value = line.partition(":")
        if separator and name.lower() == "content-length":
            try:
                return int(value.strip())
            except ValueError as exc:
                raise bad_request("web_search MCP server returned invalid Content-Length.") from exc
    return None


def web_search_query(arguments: str) -> str:
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError as exc:
        raise bad_request(f"web_search arguments must be valid JSON: {exc}") from exc
    query = parsed.get("query") or parsed.get("q")
    if not isinstance(query, str) or not query.strip():
        raise bad_request("web_search requires a non-empty string `query` argument.")
    return query


def _format_mcp_tool_result(payload: object, max_results: int) -> str:
    if not isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False)
    content = payload.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif item.get("type") == "json":
                parts.append(_format_search_payload(item.get("json"), max_results=max_results))
        if parts:
            return "\n\n".join(parts)
    return _format_search_payload(payload, max_results=max_results)


def _format_search_payload(payload: object, max_results: int) -> str:
    results = _extract_results(payload)
    if not results:
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, ensure_ascii=False)
    lines = []
    for index, result in enumerate(results[:max_results], start=1):
        title = result.get("title") or result.get("name") or "Untitled"
        url = result.get("url") or result.get("link") or result.get("href") or ""
        snippet = result.get("content") or result.get("snippet") or result.get("description") or ""
        lines.append(f"{index}. {title}\nURL: {url}\nSnippet: {snippet}".strip())
    return "\n\n".join(lines)


def _extract_results(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "organic_results", "web", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict) and isinstance(value.get("results"), list):
            return [item for item in value["results"] if isinstance(item, dict)]
    return []
