from __future__ import annotations

from collections.abc import AsyncIterator
import json
import os
import time
import base64

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import get_settings
from .deepseek import DeepSeekClient
from .errors import error_body
from .state import create_state_store
from .translator import (
    build_deepseek_request,
    translate_deepseek_response,
    translate_stream_final_response,
)
from .web_search import WebSearchRunner, web_search_query

settings = get_settings()
state_store = create_state_store(
    settings.state_backend,
    db_path=settings.state_db_path,
    ttl_seconds=settings.state_ttl_seconds,
    max_entries=settings.state_max_entries,
)
app = FastAPI(title="Codex CLI DeepSeek Adapter")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/responses")
async def create_response(request: Request):
    try:
        payload = await request.json()
        _debug_request(payload)
        client_model = payload.get("model") if isinstance(payload, dict) else None
        upstream_model = (
            settings.resolve_deepseek_model(client_model)
            if isinstance(client_model, str)
            else settings.deepseek_model
        )
        if upstream_model is None:
            return JSONResponse(
                status_code=400,
                content=error_body(f"No DeepSeek model mapping for client model: {client_model}"),
            )
        translation = build_deepseek_request(
            payload,
            state_store,
            upstream_model,
            image_preprocessor=settings.image_preprocessor,
            image_preprocessor_endpoint=settings.image_preprocessor_endpoint,
            image_preprocessor_api_key=settings.image_preprocessor_api_key,
            image_preprocessor_model=settings.image_preprocessor_model,
            image_preprocessor_prompt=settings.image_preprocessor_prompt,
        )
        if translation.hosted_web_search:
            WebSearchRunner(
                settings.web_search_backend,
                settings.web_search_mcp_command,
                settings.web_search_mcp_args,
                settings.web_search_mcp_tool,
                settings.web_search_mcp_framing,
                settings.web_search_max_results,
                settings.web_search_timeout_seconds,
            ).require_configured()
    except Exception as exc:
        return _exception_response(exc)

    if not settings.deepseek_api_key:
        return JSONResponse(
            status_code=500,
            content=error_body("DEEPSEEK_API_KEY is not configured.", "configuration_error"),
        )

    client = DeepSeekClient(settings.deepseek_api_key, settings.deepseek_base_url)
    if translation.stream:
        return StreamingResponse(
            _closing_sse(_responses_sse(client, translation), client),
            media_type="text/event-stream",
        )

    try:
        try:
            if translation.hosted_web_search:
                deepseek_response = await _run_hosted_web_search_loop(client, translation)
            else:
                deepseek_response = await client.create_completion(translation.deepseek_request)
        except httpx.HTTPStatusError as exc:
            return _upstream_error_response(exc)
        except httpx.HTTPError as exc:
            return JSONResponse(status_code=502, content=error_body(str(exc), "upstream_error"))
        except Exception as exc:
            return _exception_response(exc)

        try:
            response = translate_deepseek_response(deepseek_response, translation, state_store)
        except Exception as exc:
            return _exception_response(exc)
        return JSONResponse(content=response)
    finally:
        await client.close()


def _debug_request(payload: object) -> None:
    debug_path = os.getenv("DS_RESPONSES_DEBUG_REQUESTS_PATH")
    if not debug_path:
        return
    with open(debug_path, "a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")


async def _closing_sse(events: AsyncIterator[str], client: DeepSeekClient) -> AsyncIterator[str]:
    try:
        async for event in events:
            yield event
    finally:
        await client.close()


async def _run_hosted_web_search_loop(client: DeepSeekClient, translation) -> dict:
    runner = WebSearchRunner(
        settings.web_search_backend,
        settings.web_search_mcp_command,
        settings.web_search_mcp_args,
        settings.web_search_mcp_tool,
        settings.web_search_mcp_framing,
        settings.web_search_max_results,
        settings.web_search_timeout_seconds,
    )
    request_payload = dict(translation.deepseek_request)
    request_payload.pop("stream_options", None)
    request_payload["stream"] = False
    messages = list(request_payload["messages"])
    request_payload["messages"] = messages
    executed_messages: list[dict] = []
    max_rounds = 4

    for _ in range(max_rounds):
        response = await client.create_completion(request_payload)
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        web_search_calls = [
            call
            for call in message.get("tool_calls") or []
            if (call.get("function") or {}).get("name") == "web_search"
        ]
        if not web_search_calls:
            translation.current_messages.extend(executed_messages)
            return response

        messages.append(message)
        executed_messages.append(message)
        for call in web_search_calls:
            function = call.get("function") or {}
            query = web_search_query(function.get("arguments", "{}"))
            result = await runner.search(query)
            tool_message = {
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": result,
            }
            messages.append(tool_message)
            executed_messages.append(tool_message)

    raise RuntimeError("web_search exceeded the maximum number of tool rounds.")


async def _responses_sse(client: DeepSeekClient, translation) -> AsyncIterator[str]:
    created_at = int(time.time())
    yield _sse("response.created", {"response": _response_created_payload(translation.response_id, created_at)})
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    stream_reasoning = _stream_reasoning_summary_enabled()
    reasoning_item_id = f"rs_{translation.response_id}"
    reasoning_item_added = False
    usage = None
    model = translation.upstream_model
    last_created = created_at
    request_payload = dict(translation.deepseek_request)
    messages = list(request_payload["messages"])
    request_payload["messages"] = messages
    executed_messages: list[dict] = []
    max_rounds = 4
    tool_calls: dict[int, dict] = {}

    try:
        for _ in range(max_rounds):
            round_content_parts: list[str] = []
            round_reasoning_parts: list[str] = []
            tool_calls = {}
            async for line in client.stream_completion(request_payload):
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                if not data:
                    continue
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                usage = chunk.get("usage") or usage
                model = chunk.get("model") or model
                last_created = chunk.get("created") or last_created
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        reasoning_delta = delta["reasoning_content"]
                        reasoning_parts.append(reasoning_delta)
                        round_reasoning_parts.append(reasoning_delta)
                        if stream_reasoning:
                            if not reasoning_item_added:
                                yield _sse(
                                    "response.output_item.added",
                                    {
                                        "response_id": translation.response_id,
                                        "output_index": 0,
                                        "item": _reasoning_stream_item(reasoning_item_id, ""),
                                    },
                                )
                                reasoning_item_added = True
                            yield _sse(
                                "response.reasoning_summary_text.delta",
                                {
                                    "response_id": translation.response_id,
                                    "item_id": reasoning_item_id,
                                    "output_index": 0,
                                    "summary_index": 0,
                                    "delta": reasoning_delta,
                                },
                            )
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                        round_content_parts.append(delta["content"])
                        if not translation.compaction_requested:
                            yield _sse(
                                "response.output_text.delta",
                                {
                                    "response_id": translation.response_id,
                                    "output_index": 0,
                                    "content_index": 0,
                                    "delta": delta["content"],
                                },
                            )
                    _merge_tool_call_deltas(tool_calls, delta.get("tool_calls") or [])

            calls = _final_tool_calls(tool_calls)
            web_search_calls = _web_search_calls(calls) if translation.hosted_web_search else []
            if not web_search_calls or len(web_search_calls) != len(calls):
                break

            assistant_message = {
                "role": "assistant",
                "content": "".join(round_content_parts),
                "tool_calls": web_search_calls,
            }
            if round_reasoning_parts:
                assistant_message["reasoning_content"] = "".join(round_reasoning_parts)
            messages.append(assistant_message)
            executed_messages.append(assistant_message)
            for call in web_search_calls:
                function = call.get("function") or {}
                query = web_search_query(function.get("arguments", "{}"))
                result = await _web_search_runner().search(query)
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": result,
                }
                messages.append(tool_message)
                executed_messages.append(tool_message)
        else:
            raise RuntimeError("web_search exceeded the maximum number of tool rounds.")
    except httpx.HTTPStatusError as exc:
        error = _upstream_error_content(exc)["error"]
        _debug_error("upstream_http_status", translation.response_id, error)
        yield _response_failed_sse(translation.response_id, created_at, error)
        yield "data: [DONE]\n\n"
        return
    except Exception as exc:
        error = error_body(str(exc), "upstream_error")["error"]
        _debug_error("stream_error", translation.response_id, error)
        yield _response_failed_sse(translation.response_id, created_at, error)
        yield "data: [DONE]\n\n"
        return

    text = "".join(content_parts)
    final_message = {
        "role": "assistant",
        "content": text,
    }
    if reasoning_parts:
        final_message["reasoning_content"] = "".join(reasoning_parts)
    calls = _final_tool_calls(tool_calls)
    if calls:
        final_message["tool_calls"] = calls

    translation.current_messages.extend(executed_messages)
    completed = translate_stream_final_response(
        final_message,
        usage,
        translation,
        state_store,
        created_at=last_created,
        model=model,
    )
    output_items = completed["output"]
    reasoning_output_item = next((item for item in output_items if item.get("type") == "reasoning"), None)
    if reasoning_item_added and reasoning_output_item is not None:
        output_items = [item for item in output_items if item.get("type") != "reasoning"]
        completed = {**completed, "output": output_items}
    if reasoning_item_added or reasoning_output_item is not None:
        yield _sse(
            "response.output_item.done",
            {
                "response_id": translation.response_id,
                "output_index": 0,
                "item": reasoning_output_item or _reasoning_stream_item(reasoning_item_id, "".join(reasoning_parts)),
            },
        )
    output_index_offset = 1 if reasoning_item_added or reasoning_output_item is not None else 0
    for index, item in enumerate(output_items, start=output_index_offset):
        yield _sse(
            "response.output_item.added",
            {"response_id": translation.response_id, "output_index": index, "item": item},
        )
    for index, item in enumerate(output_items, start=output_index_offset):
        if item.get("type") != "message":
            continue
        for content_index, content in enumerate(item.get("content") or []):
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                yield _sse(
                    "response.output_text.done",
                    {
                        "response_id": translation.response_id,
                        "output_index": index,
                        "content_index": content_index,
                        "text": content["text"],
                    },
                )
        yield _sse(
            "response.output_item.done",
            {"response_id": translation.response_id, "output_index": index, "item": item},
        )
    for index, item in enumerate(output_items, start=output_index_offset):
        if item.get("type") in ("function_call", "custom_tool_call"):
            yield _sse(
                "response.output_item.done",
                {"response_id": translation.response_id, "output_index": index, "item": item},
            )
    yield _sse("response.completed", {"response_id": completed["id"], "response": completed})
    yield "data: [DONE]\n\n"


def _merge_tool_call_deltas(tool_calls: dict[int, dict], deltas: list[dict]) -> None:
    for delta in deltas:
        index = delta.get("index", len(tool_calls))
        entry = tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
        if delta.get("id"):
            entry["id"] += delta["id"]
        function = delta.get("function") or {}
        if function.get("name"):
            entry["function"]["name"] += function["name"]
        if function.get("arguments"):
            entry["function"]["arguments"] += function["arguments"]


def _final_tool_calls(tool_calls: dict[int, dict]) -> list[dict]:
    calls = []
    for _, call in sorted(tool_calls.items()):
        if call.get("id") or call.get("function", {}).get("name"):
            calls.append(call)
    return calls


def _web_search_calls(calls: list[dict]) -> list[dict]:
    return [call for call in calls if (call.get("function") or {}).get("name") == "web_search"]


def _web_search_runner() -> WebSearchRunner:
    return WebSearchRunner(
        settings.web_search_backend,
        settings.web_search_mcp_command,
        settings.web_search_mcp_args,
        settings.web_search_mcp_tool,
        settings.web_search_mcp_framing,
        settings.web_search_max_results,
        settings.web_search_timeout_seconds,
    )


def _stream_reasoning_summary_enabled() -> bool:
    return (os.getenv("DS_RESPONSES_STREAM_REASONING_SUMMARY") or "").lower() in {"1", "true", "yes", "on"}


def _reasoning_stream_item(item_id: str, text: str) -> dict:
    payload = json.dumps({"reasoning_content": text}, ensure_ascii=False).encode("utf-8")
    encrypted_content = f"ds_proxy.{base64.urlsafe_b64encode(payload).decode('ascii')}"
    return {
        "id": item_id,
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": text}],
        "encrypted_content": encrypted_content,
    }


def _sse(event: str, data: dict) -> str:
    data = {"type": event, **data}
    chunk = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    debug_path = os.getenv("DS_RESPONSES_DEBUG_SSE_PATH")
    if debug_path:
        with open(debug_path, "a", encoding="utf-8") as file:
            file.write(chunk)
    return chunk


def _response_failed_sse(response_id: str, created_at: int, error: dict) -> str:
    failed = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "failed",
        "error": error,
        "output": [],
        "output_text": "",
    }
    return _sse("response.failed", {"response_id": response_id, "response": failed})


def _response_created_payload(response_id: str, created_at: int) -> dict:
    return {"id": response_id, "object": "response", "created_at": created_at}


def _debug_error(kind: str, response_id: str, error: dict) -> None:
    debug_path = os.getenv("DS_RESPONSES_DEBUG_ERRORS_PATH")
    if not debug_path:
        return
    payload = {
        "created_at": int(time.time()),
        "kind": kind,
        "response_id": response_id,
        "error": error,
    }
    with open(debug_path, "a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _exception_response(exc: Exception) -> JSONResponse:
    status_code = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict) and "message" in detail:
        return JSONResponse(status_code=status_code, content={"error": detail})
    return JSONResponse(status_code=status_code, content=error_body(str(exc)))


def _upstream_error_response(exc: httpx.HTTPStatusError) -> JSONResponse:
    return JSONResponse(status_code=exc.response.status_code, content=_upstream_error_content(exc))


def _upstream_error_content(exc: httpx.HTTPStatusError) -> dict:
    try:
        body = exc.response.json()
    except ValueError:
        body = exc.response.text
    return error_body(f"DeepSeek upstream error: {body}", "upstream_error")
