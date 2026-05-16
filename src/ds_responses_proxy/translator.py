from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import base64
import binascii
import json
import time
import uuid

import httpx

from .errors import bad_request, error_body
from .state import ResponseStateStore


SUPPORTED_MESSAGE_ROLES = {"system", "developer", "user", "assistant"}


@dataclass
class TranslationResult:
    response_id: str
    deepseek_request: dict
    base_messages: list[dict]
    current_messages: list[dict]
    stream: bool
    client_model: str
    upstream_model: str
    hosted_web_search: bool = False
    custom_tool_names: set[str] | None = None
    compaction_requested: bool = False


def build_deepseek_request(
    payload: dict,
    state_store: ResponseStateStore,
    upstream_model: str = "deepseek-v4-pro",
    image_preprocessor: str | None = None,
    image_preprocessor_endpoint: str | None = None,
    image_preprocessor_api_key: str | None = None,
    image_preprocessor_model: str | None = None,
    image_preprocessor_prompt: str | None = None,
) -> TranslationResult:
    if not isinstance(payload, dict):
        raise bad_request("Request body must be a JSON object.")

    client_model = payload.get("model")
    if not isinstance(client_model, str) or not client_model:
        raise bad_request("`model` is required and must be a non-empty string.")
    if not isinstance(upstream_model, str) or not upstream_model:
        raise bad_request("Configured DeepSeek model must be a non-empty string.")

    base_messages = _load_base_messages(payload, state_store)
    current_messages = []
    compaction_requested = _input_has_compaction_trigger(payload.get("input"))

    instructions = payload.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str):
            raise bad_request("`instructions` must be a string when provided.")
        current_messages.append({"role": "system", "content": instructions})

    structured_output_instruction = _structured_output_instruction(payload)
    if structured_output_instruction is not None:
        current_messages.append({"role": "system", "content": structured_output_instruction})

    tool_usage_instruction = _tool_usage_instruction(payload)
    if tool_usage_instruction is not None:
        current_messages.append({"role": "system", "content": tool_usage_instruction})

    current_messages.extend(
        _input_to_messages(
            payload.get("input"),
            image_preprocessor=image_preprocessor,
            image_preprocessor_endpoint=image_preprocessor_endpoint,
            image_preprocessor_api_key=image_preprocessor_api_key,
            image_preprocessor_model=image_preprocessor_model,
            image_preprocessor_prompt=image_preprocessor_prompt,
        )
    )
    if not current_messages and not base_messages:
        raise bad_request("`input` is required when `previous_response_id` is not provided.")

    messages = _messages_for_upstream(base_messages + current_messages)
    deepseek_request = {
        "model": upstream_model,
        "messages": messages,
    }
    _map_generation_params(payload, deepseek_request)
    hosted_web_search, custom_tool_names = _map_tools(payload, deepseek_request)

    return TranslationResult(
        response_id=_new_response_id(),
        deepseek_request=deepseek_request,
        base_messages=base_messages,
        current_messages=current_messages,
        stream=bool(payload.get("stream", False)),
        client_model=client_model,
        upstream_model=upstream_model,
        hosted_web_search=hosted_web_search,
        custom_tool_names=custom_tool_names,
        compaction_requested=compaction_requested,
    )


def translate_deepseek_response(
    deepseek_response: dict,
    translation: TranslationResult,
    state_store: ResponseStateStore,
) -> dict:
    choice = _first_choice(deepseek_response)
    message = deepcopy(choice.get("message") or {})
    if message.get("role") is None:
        message["role"] = "assistant"

    canonical_messages = translation.base_messages + translation.current_messages + [message]
    state_store.save(
        translation.response_id,
        canonical_messages,
        model=deepseek_response.get("model") or translation.upstream_model,
    )

    output = _message_to_response_output(
        message,
        translation.custom_tool_names,
        compaction_requested=translation.compaction_requested,
    )
    reasoning = _reasoning_item(message)
    if reasoning is not None:
        output.insert(0, reasoning)

    return {
        "id": translation.response_id,
        "object": "response",
        "created_at": deepseek_response.get("created", int(time.time())),
        "status": "completed",
        "model": deepseek_response.get("model") or translation.upstream_model,
        "output": output,
        "output_text": _output_text(output),
        "usage": _translate_usage(deepseek_response.get("usage")),
    }


def translate_stream_final_response(
    message: dict,
    usage: dict | None,
    translation: TranslationResult,
    state_store: ResponseStateStore,
    created_at: int | None = None,
    model: str | None = None,
) -> dict:
    canonical_messages = translation.base_messages + translation.current_messages + [message]
    state_store.save(translation.response_id, canonical_messages, model=model or translation.upstream_model)
    output = _message_to_response_output(
        message,
        translation.custom_tool_names,
        compaction_requested=translation.compaction_requested,
    )
    reasoning = _reasoning_item(message)
    if reasoning is not None:
        output.insert(0, reasoning)
    return {
        "id": translation.response_id,
        "object": "response",
        "created_at": created_at or int(time.time()),
        "status": "completed",
        "model": model or translation.upstream_model,
        "output": output,
        "output_text": _output_text(output),
        "usage": _translate_usage(usage),
    }


def _load_base_messages(payload: dict, state_store: ResponseStateStore) -> list[dict]:
    previous_response_id = payload.get("previous_response_id")
    if previous_response_id is None:
        return []
    if not isinstance(previous_response_id, str):
        raise bad_request("`previous_response_id` must be a string.")
    state = state_store.get(previous_response_id)
    if state is None:
        raise bad_request(f"Unknown previous_response_id: {previous_response_id}")
    return state.messages


def _input_to_messages(
    input_value: object,
    image_preprocessor: str | None = None,
    image_preprocessor_endpoint: str | None = None,
    image_preprocessor_api_key: str | None = None,
    image_preprocessor_model: str | None = None,
    image_preprocessor_prompt: str | None = None,
) -> list[dict]:
    if input_value is None:
        return []
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if not isinstance(input_value, list):
        raise bad_request("`input` must be a string or an array.")

    messages = []
    pending_reasoning_content = None
    pending_tool_call_message = None

    def flush_pending_tool_calls() -> None:
        nonlocal pending_tool_call_message
        if pending_tool_call_message is not None:
            messages.append(pending_tool_call_message)
            pending_tool_call_message = None

    for item in input_value:
        if not isinstance(item, dict):
            raise bad_request("Every `input` item must be an object.")
        item_type = item.get("type")
        if item_type in (None, "message"):
            flush_pending_tool_calls()
            messages.append(
                _response_message_to_deepseek(
                    item,
                    image_preprocessor=image_preprocessor,
                    image_preprocessor_endpoint=image_preprocessor_endpoint,
                    image_preprocessor_api_key=image_preprocessor_api_key,
                    image_preprocessor_model=image_preprocessor_model,
                    image_preprocessor_prompt=image_preprocessor_prompt,
                )
            )
        elif item_type in ("text", "input_text"):
            flush_pending_tool_calls()
            messages.append(_text_input_item_to_message(item))
        elif item_type in ("mention", "skill"):
            flush_pending_tool_calls()
            messages.append(_structured_reference_item_to_message(item))
        elif item_type in ("function_call", "custom_tool_call"):
            tool_call = _function_call_to_deepseek_tool_call(item)
            if pending_tool_call_message is None:
                pending_tool_call_message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [],
                }
                if pending_reasoning_content:
                    pending_tool_call_message["reasoning_content"] = pending_reasoning_content
            pending_tool_call_message["tool_calls"].append(tool_call)
            pending_reasoning_content = None
        elif item_type in ("function_call_output", "custom_tool_call_output"):
            flush_pending_tool_calls()
            messages.append(_function_output_to_tool_message(item))
        elif item_type == "mcp_tool_call_output":
            flush_pending_tool_calls()
            messages.append(_mcp_tool_output_to_tool_message(item))
        elif item_type == "tool_search_output":
            flush_pending_tool_calls()
            messages.append(_tool_search_output_to_tool_message(item))
        elif item_type == "reasoning":
            pending_reasoning_content = _reasoning_content_from_input_item(item)
            continue
        elif item_type == "compaction_trigger":
            flush_pending_tool_calls()
            messages.append(_compaction_trigger_to_message())
        elif item_type in ("compaction", "compaction_summary", "context_compaction"):
            flush_pending_tool_calls()
            message = _compaction_item_to_message(item)
            if message is not None:
                messages.append(message)
        else:
            raise bad_request(f"Unsupported Codex CLI input item type: {item_type}")
    flush_pending_tool_calls()
    return messages


def _input_has_compaction_trigger(input_value: object) -> bool:
    if not isinstance(input_value, list):
        return False
    return any(isinstance(item, dict) and item.get("type") == "compaction_trigger" for item in input_value)


def _compaction_trigger_to_message() -> dict:
    return {
        "role": "system",
        "content": (
            "Create a context checkpoint compaction summary for the conversation. "
            "Return only the handoff summary text. Include current progress, key decisions, "
            "important constraints, remaining next steps, and critical references needed to continue."
        ),
    }


def _compaction_item_to_message(item: dict) -> dict | None:
    encrypted_content = item.get("encrypted_content")
    if encrypted_content is None:
        return None
    if not isinstance(encrypted_content, str):
        raise bad_request(f"{item.get('type')} items require string `encrypted_content` when provided.")
    summary = _decode_proxy_payload(encrypted_content).get("compaction_summary")
    if not isinstance(summary, str):
        summary = encrypted_content
    return {
        "role": "user",
        "content": f"Previous context compaction summary:\n{summary}",
    }


def _response_message_to_deepseek(
    item: dict,
    image_preprocessor: str | None = None,
    image_preprocessor_endpoint: str | None = None,
    image_preprocessor_api_key: str | None = None,
    image_preprocessor_model: str | None = None,
    image_preprocessor_prompt: str | None = None,
) -> dict:
    role = item.get("role")
    if role not in SUPPORTED_MESSAGE_ROLES:
        raise bad_request(f"Unsupported message role: {role}")
    mapped_role = "system" if role == "developer" else role
    return {
        "role": mapped_role,
        "content": _content_to_text(
            item.get("content"),
            image_preprocessor=image_preprocessor,
            image_preprocessor_endpoint=image_preprocessor_endpoint,
            image_preprocessor_api_key=image_preprocessor_api_key,
            image_preprocessor_model=image_preprocessor_model,
            image_preprocessor_prompt=image_preprocessor_prompt,
        ),
    }


def _text_input_item_to_message(item: dict) -> dict:
    text = item.get("text")
    if not isinstance(text, str):
        raise bad_request("Text input items require string `text`.")
    return {"role": "user", "content": text}


def _structured_reference_item_to_message(item: dict) -> dict:
    item_type = item.get("type")
    name = item.get("name")
    path = item.get("path")
    if not isinstance(name, str) or not isinstance(path, str):
        raise bad_request(f"{item_type} input items require string `name` and `path`.")
    label = "Mentioned target" if item_type == "mention" else "Selected skill"
    return {"role": "user", "content": f"{label}: {name}\nPath: {path}"}


def _content_to_text(
    content: object,
    image_preprocessor: str | None = None,
    image_preprocessor_endpoint: str | None = None,
    image_preprocessor_api_key: str | None = None,
    image_preprocessor_model: str | None = None,
    image_preprocessor_prompt: str | None = None,
) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise bad_request("Message `content` must be a string or an array of text content items.")
    parts = []
    for part in content:
        if not isinstance(part, dict):
            raise bad_request("Content array entries must be objects.")
        part_type = part.get("type")
        if part_type in ("input_text", "output_text", "text"):
            text = part.get("text")
            if not isinstance(text, str):
                raise bad_request("Text content item requires string `text`.")
            parts.append(text)
        elif part_type == "input_image":
            parts.append(
                _image_content_to_text(
                    part,
                    image_preprocessor,
                    image_preprocessor_endpoint,
                    image_preprocessor_api_key,
                    image_preprocessor_model,
                    image_preprocessor_prompt,
                )
            )
        else:
            raise bad_request(f"Unsupported content item type: {part_type}")
    return "".join(parts)


DEFAULT_IMAGE_PREPROCESSOR_PROMPT = (
    "Extract useful text and visual context from this image for a coding assistant. "
    "Return concise plain text with these sections when applicable: OCR text, UI elements, "
    "layout summary, notable errors or warnings, and uncertainty. Do not invent details."
)


def _image_content_to_text(
    part: dict,
    image_preprocessor: str | None,
    image_preprocessor_endpoint: str | None,
    image_preprocessor_api_key: str | None,
    image_preprocessor_model: str | None,
    image_preprocessor_prompt: str | None,
) -> str:
    if image_preprocessor is None:
        raise bad_request(
            "Image input requires IMAGE_PREPROCESSOR. Supported first-stage values are "
            "'metadata', 'vision_endpoint', 'ocr', or 'openai_compatible'."
        )
    image_url = part.get("image_url") or part.get("url")
    file_id = part.get("file_id")
    if not isinstance(image_url, str) and not isinstance(file_id, str):
        raise bad_request("input_image requires string `image_url`, `url`, or `file_id`.")
    if image_preprocessor == "metadata":
        return _image_metadata_text(image_url=image_url, file_id=file_id)
    if image_preprocessor in ("vision_endpoint", "ocr"):
        if image_preprocessor_endpoint is None:
            raise bad_request(f"IMAGE_PREPROCESSOR={image_preprocessor} requires IMAGE_PREPROCESSOR_ENDPOINT.")
        return _image_endpoint_text(
            image_preprocessor,
            image_preprocessor_endpoint,
            image_url=image_url,
            file_id=file_id,
        )
    if image_preprocessor == "openai_compatible":
        if image_preprocessor_endpoint is None or image_preprocessor_model is None:
            raise bad_request(
                "IMAGE_PREPROCESSOR=openai_compatible requires IMAGE_PREPROCESSOR_ENDPOINT "
                "and IMAGE_PREPROCESSOR_MODEL."
            )
        if not isinstance(image_url, str):
            raise bad_request("IMAGE_PREPROCESSOR=openai_compatible requires input_image `image_url` or `url`.")
        return _image_openai_compatible_text(
            endpoint=image_preprocessor_endpoint,
            api_key=image_preprocessor_api_key,
            model=image_preprocessor_model,
            prompt=image_preprocessor_prompt or DEFAULT_IMAGE_PREPROCESSOR_PROMPT,
            image_url=image_url,
        )
    raise bad_request("Unsupported IMAGE_PREPROCESSOR. Use 'metadata', 'ocr', 'vision_endpoint', or 'openai_compatible'.")


def _image_metadata_text(image_url: object, file_id: object) -> str:
    lines = _image_summary_header("metadata")
    if isinstance(file_id, str):
        lines.append(f"file_id: {file_id}")
    if isinstance(image_url, str):
        lines.append(f"source: {_image_source_label(image_url)}")
        mime, byte_count = _data_url_info(image_url)
        if mime is not None:
            lines.append(f"mime_type: {mime}")
        if byte_count is not None:
            lines.append(f"byte_count: {byte_count}")
    lines.append("visual_content: unavailable")
    return "\n".join(lines) + "\n"


def _image_endpoint_text(preprocessor: str, endpoint: str, image_url: object, file_id: object) -> str:
    request_payload = {"preprocessor": preprocessor}
    if isinstance(image_url, str):
        request_payload["image_url"] = image_url
    if isinstance(file_id, str):
        request_payload["file_id"] = file_id
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(endpoint, json=request_payload)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise bad_request(f"Image preprocessor request failed: {exc}") from exc
    except ValueError as exc:
        raise bad_request("Image preprocessor response must be JSON.") from exc

    if not isinstance(payload, dict):
        raise bad_request("Image preprocessor response must be a JSON object.")
    lines = _image_summary_header(preprocessor)
    if isinstance(file_id, str):
        lines.append(f"file_id: {file_id}")
    if isinstance(image_url, str):
        lines.append(f"source: {_image_source_label(image_url)}")
    ocr = payload.get("ocr") or payload.get("text")
    description = payload.get("description") or payload.get("caption")
    if isinstance(ocr, str) and ocr:
        lines.append("OCR:")
        lines.append(ocr)
    if isinstance(description, str) and description:
        lines.append("Image description:")
        lines.append(description)
    if len(lines) <= 3:
        lines.append("visual_content: unavailable")
    return "\n".join(lines) + "\n"


def _image_openai_compatible_text(
    endpoint: str,
    api_key: str | None,
    model: str,
    prompt: str,
    image_url: str,
) -> str:
    request_payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
        "temperature": 0,
    }
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(endpoint, headers=headers, json=request_payload)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise bad_request(f"OpenAI-compatible image preprocessor request failed: {exc}") from exc
    except ValueError as exc:
        raise bad_request("OpenAI-compatible image preprocessor response must be JSON.") from exc

    content = _openai_compatible_message_content(payload)
    lines = _image_summary_header("openai_compatible")
    lines.append(f"source: {_image_source_label(image_url)}")
    lines.append("Image preprocessor output:")
    lines.append(content or "visual_content: unavailable")
    return "\n".join(lines) + "\n"


def _openai_compatible_message_content(payload: object) -> str:
    if not isinstance(payload, dict):
        raise bad_request("OpenAI-compatible image preprocessor response must be a JSON object.")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise bad_request("OpenAI-compatible image preprocessor response did not contain choices.")
    first = choices[0]
    if not isinstance(first, dict):
        raise bad_request("OpenAI-compatible image preprocessor choice was invalid.")
    message = first.get("message") or {}
    if not isinstance(message, dict):
        raise bad_request("OpenAI-compatible image preprocessor message was invalid.")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _image_summary_header(preprocessor: str) -> list[str]:
    return [
        "\n[Image input converted by proxy]",
        "Mode: image input fallback; DeepSeek receives only this text summary, not the original pixels.",
        "Caution: the summary may be incomplete or inaccurate compared with native multimodal reasoning.",
        f"Image preprocessor: {preprocessor}",
    ]


def _image_source_label(image_url: str) -> str:
    if image_url.startswith("data:"):
        return "data_url"
    return image_url


def _data_url_info(image_url: str) -> tuple[str | None, int | None]:
    if not image_url.startswith("data:"):
        return None, None
    header, separator, data = image_url.partition(",")
    if not separator:
        return None, None
    mime = header[5:].split(";", 1)[0] or None
    if ";base64" not in header:
        return mime, len(data.encode("utf-8"))
    try:
        return mime, len(base64.b64decode(data, validate=True))
    except (binascii.Error, ValueError):
        return mime, None


def _function_call_to_deepseek_tool_call(item: dict) -> dict:
    call_id = item.get("call_id") or item.get("id")
    name = item.get("name")
    arguments = item.get("arguments", "{}")
    if item.get("type") == "custom_tool_call":
        arguments = {"input": item.get("input", "")}
    if not isinstance(call_id, str) or not isinstance(name, str):
        raise bad_request("function_call items require string `call_id` and `name`.")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _function_output_to_tool_message(item: dict) -> dict:
    call_id = item.get("call_id")
    output = _function_output_to_text(item.get("output", ""))
    if not isinstance(call_id, str):
        raise bad_request("function_call_output items require string `call_id`.")
    return {"role": "tool", "tool_call_id": call_id, "content": output}


def _function_output_to_text(output: object) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts = []
        for part in output:
            if not isinstance(part, dict):
                parts.append(json.dumps(part, ensure_ascii=False))
                continue
            part_type = part.get("type")
            if part_type in ("input_text", "output_text", "text"):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif part_type == "input_image":
                image_url = part.get("image_url") or part.get("url")
                file_id = part.get("file_id")
                parts.append(_image_metadata_text(image_url=image_url, file_id=file_id).strip())
            else:
                parts.append(json.dumps(part, ensure_ascii=False))
        return "\n".join(part for part in parts if part)
    return json.dumps(output, ensure_ascii=False)


def _mcp_tool_output_to_tool_message(item: dict) -> dict:
    call_id = item.get("call_id")
    if not isinstance(call_id, str):
        raise bad_request("mcp_tool_call_output items require string `call_id`.")
    output = item.get("output", {})
    return {"role": "tool", "tool_call_id": call_id, "content": _mcp_tool_output_to_text(output)}


def _mcp_tool_output_to_text(output: object) -> str:
    if not isinstance(output, dict):
        return _function_output_to_text(output)
    structured = output.get("structuredContent")
    if structured is None:
        structured = output.get("structured_content")
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    content = output.get("content")
    if isinstance(content, list):
        parts = []
        for entry in content:
            if not isinstance(entry, dict):
                parts.append(json.dumps(entry, ensure_ascii=False))
                continue
            entry_type = entry.get("type")
            if entry_type == "text" and isinstance(entry.get("text"), str):
                parts.append(entry["text"])
            elif entry_type == "image":
                mime_type = entry.get("mimeType") or entry.get("mime_type") or "application/octet-stream"
                data = entry.get("data")
                image_url = data if isinstance(data, str) and data.startswith("data:") else None
                if image_url is None and isinstance(data, str):
                    image_url = f"data:{mime_type};base64,{data}"
                parts.append(_image_metadata_text(image_url=image_url, file_id=None).strip())
            else:
                parts.append(json.dumps(entry, ensure_ascii=False))
        return "\n".join(part for part in parts if part)
    return json.dumps(output, ensure_ascii=False)


def _tool_search_output_to_tool_message(item: dict) -> dict:
    call_id = item.get("call_id")
    if not isinstance(call_id, str):
        raise bad_request("tool_search_output items require string `call_id`.")
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": _tool_search_output_to_text(item),
    }


def _tool_search_output_to_text(item: dict) -> str:
    status = item.get("status")
    execution = item.get("execution")
    tools = item.get("tools", [])
    lines = ["[Codex tool_search output]"]
    if isinstance(status, str):
        lines.append(f"status: {status}")
    if isinstance(execution, str):
        lines.append(f"execution: {execution}")
    if isinstance(tools, list):
        if not tools:
            lines.append("tools: []")
        else:
            lines.append("tools:")
            for index, tool in enumerate(tools, start=1):
                if isinstance(tool, dict):
                    name = tool.get("name") or tool.get("title") or "<unnamed>"
                    description = tool.get("description") or ""
                    lines.append(f"{index}. {name} - {description}".rstrip())
                else:
                    lines.append(f"{index}. {json.dumps(tool, ensure_ascii=False)}")
    else:
        lines.append(f"tools: {json.dumps(tools, ensure_ascii=False)}")
    return "\n".join(lines)


def _messages_for_upstream(messages: list[dict]) -> list[dict]:
    upstream = []
    tool_call_history_seen = False
    for message in messages:
        copied = deepcopy(message)
        if copied.get("role") == "assistant" and copied.get("tool_calls"):
            tool_call_history_seen = True
        if copied.get("role") == "assistant" and not copied.get("tool_calls") and not tool_call_history_seen:
            copied.pop("reasoning_content", None)
        upstream.append(copied)
    return upstream


def _map_generation_params(payload: dict, request: dict) -> None:
    if "max_output_tokens" in payload:
        request["max_tokens"] = payload["max_output_tokens"]
    for source, target in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stop", "stop"),
        ("stream", "stream"),
        ("stream_options", "stream_options"),
        ("logprobs", "logprobs"),
        ("top_logprobs", "top_logprobs"),
        ("user", "user_id"),
    ):
        if source in payload:
            request[target] = payload[source]

    thinking = payload.get("thinking")
    reasoning = payload.get("reasoning", "__missing__")
    if thinking == {"type": "disabled"} or reasoning is None:
        request["thinking"] = {"type": "disabled"}
    elif isinstance(reasoning, dict) and reasoning.get("effort") == "none":
        request["thinking"] = {"type": "disabled"}
    else:
        request["thinking"] = {"type": "enabled"}
        effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
        if effort in ("xhigh", "max"):
            request["reasoning_effort"] = "max"
        else:
            request["reasoning_effort"] = "high"

    text = payload.get("text")
    if isinstance(text, dict):
        text_format = text.get("format")
        if isinstance(text_format, dict):
            fmt_type = text_format.get("type")
            if fmt_type in ("json_object", "json_schema"):
                request["response_format"] = {"type": "json_object"}
            elif fmt_type not in (None, "text"):
                raise bad_request("Structured JSON schema outputs are not supported by this proxy version.")


def _structured_output_instruction(payload: dict) -> str | None:
    text = payload.get("text")
    if not isinstance(text, dict):
        return None
    text_format = text.get("format")
    if not isinstance(text_format, dict) or text_format.get("type") != "json_schema":
        return None

    schema = text_format.get("schema")
    schema_name = text_format.get("name")
    strict = text_format.get("strict")
    lines = [
        "Structured output requirement:",
        "Return only a valid JSON object. Do not include Markdown, code fences, or explanatory text.",
    ]
    if isinstance(schema_name, str) and schema_name:
        lines.append(f"Schema name: {schema_name}")
    if strict is True:
        lines.append("Strict mode: the JSON object must conform to the provided schema.")
    elif strict is False:
        lines.append("The JSON object should conform to the provided schema when possible.")
    if schema is not None:
        lines.append("JSON schema:")
        lines.append(json.dumps(schema, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines)


def _tool_usage_instruction(payload: dict) -> str | None:
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        return None

    tool_names = _request_tool_names(tools)
    lines = [
        "Codex CLI tool-use guidance for DeepSeek:",
        "You are controlling Codex CLI tools. Use tools proactively for coding tasks instead of guessing from filenames or memory.",
        "For repository questions, first inspect the workspace with `exec_command`; prefer `rg`/`rg --files` for search and file discovery, then read only the relevant file ranges.",
        "When the user mentions a local file or directory path, including a path inserted by Codex CLI @ file search, inspect that path with `exec_command` before answering from its contents.",
        "For edits, use `apply_patch` when it is available. Provide structured edit operations for `apply_patch`; do not use shell heredocs, shell redirection, or ad-hoc commands to write files.",
        "Use parallel tool calls when independent file reads or searches can be done at the same time. Keep commands scoped to the current task and avoid destructive commands unless the user explicitly requested them.",
        "After tool results, continue from the observed output. If a command fails, adjust based on the error instead of repeating the same call.",
    ]
    if tool_names:
        lines.append("Available tool names: " + ", ".join(tool_names))
    return "\n".join(lines)


def _request_tool_names(tools: list) -> list[str]:
    names = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str):
            function = tool.get("function")
            if isinstance(function, dict):
                name = function.get("name")
        if not isinstance(name, str):
            tool_type = tool.get("type")
            if tool_type in ("web_search", "web_search_preview"):
                name = "web_search"
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def _map_tools(payload: dict, request: dict) -> tuple[bool, set[str]]:
    tools = payload.get("tools")
    hosted_web_search = False
    custom_tool_names = set()
    if tools is not None:
        if not isinstance(tools, list):
            raise bad_request("`tools` must be an array.")
        mapped_tools = []
        for tool in tools:
            if not isinstance(tool, dict):
                raise bad_request("Every tool must be an object.")
            tool_type = tool.get("type")
            if tool_type in ("web_search", "web_search_preview"):
                hosted_web_search = True
                mapped_tools.append(_web_search_function_tool())
                continue
            if tool_type == "custom":
                function = _custom_tool_to_function(tool)
                custom_tool_names.add(function["name"])
                mapped_tools.append({"type": "function", "function": function})
                continue
            if tool_type != "function":
                raise bad_request(f"Unsupported tool type: {tool.get('type')}")
            function = tool.get("function")
            if function is None:
                function = {k: v for k, v in tool.items() if k != "type"}
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                raise bad_request("Function tools require a function object with a string name.")
            mapped_tools.append({"type": "function", "function": _normalize_function_tool(function)})
        request["tools"] = mapped_tools
    if "tool_choice" not in payload:
        return hosted_web_search, custom_tool_names
    choice = payload["tool_choice"]
    if choice in ("none", "auto", "required"):
        request["tool_choice"] = choice
        return hosted_web_search, custom_tool_names
    if isinstance(choice, dict):
        if choice.get("type") in ("web_search", "web_search_preview"):
            request["tool_choice"] = {"type": "function", "function": {"name": "web_search"}}
            return hosted_web_search, custom_tool_names
        if choice.get("type") == "function":
            function = choice.get("function") or {}
            name = function.get("name") or choice.get("name")
            if isinstance(name, str):
                request["tool_choice"] = {"type": "function", "function": {"name": name}}
                return hosted_web_search, custom_tool_names
        if choice.get("type") == "custom":
            name = choice.get("name")
            if isinstance(name, str):
                request["tool_choice"] = {"type": "function", "function": {"name": name}}
                return hosted_web_search, custom_tool_names
    raise bad_request("Unsupported `tool_choice` value.")


def _custom_tool_to_function(tool: dict) -> dict:
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        raise bad_request("Custom tools require a non-empty string `name`.")
    description = tool.get("description")
    if description is not None and not isinstance(description, str):
        raise bad_request("Custom tool `description` must be a string when provided.")
    if name == "apply_patch":
        return _apply_patch_function_tool(description)
    function = {
        "name": name,
        "description": description or "Custom tool that accepts a free-form text input.",
        "parameters": {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Free-form input for the custom tool."},
            },
            "required": ["input"],
        },
    }
    return function


def _normalize_function_tool(function: dict) -> dict:
    normalized = deepcopy(function)
    if normalized.get("name") == "exec_command":
        normalized["description"] = _enhance_exec_command_description(normalized.get("description"))
    return normalized


def _enhance_exec_command_description(description: object) -> str:
    base = description if isinstance(description, str) and description else "Run a shell command."
    guidance = (
        "Use this tool to inspect the local workspace and filesystem. When the user refers to "
        "a local file or directory path, including paths inserted by Codex CLI @ file search, "
        "treat that path as a filesystem target and call this tool to list directories or read "
        "files before answering from their contents."
    )
    if guidance in base:
        return base
    return f"{base}\n\n{guidance}"


def _apply_patch_function_tool(description: str | None) -> dict:
    base = description or "Apply a patch to local files."
    return {
        "name": "apply_patch",
        "description": (
            f"{base}\n\n"
            "Provide structured edit operations only. The proxy converts these operations into "
            "Codex apply_patch grammar before the tool is executed. Do not generate raw patch "
            "text, Markdown fences, shell commands, heredocs, `Create new file ...`, "
            "`*** Before File`, or `*** After File`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "description": "Ordered file edit operations to convert into one apply_patch patch.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["add_file", "delete_file", "update_file"],
                                "description": "The edit operation type.",
                            },
                            "path": {"type": "string", "description": "Repository-relative file path."},
                            "content": {
                                "type": "string",
                                "description": "Full file content for add_file. Use plain text, not patch syntax.",
                            },
                            "hunks": {
                                "type": "array",
                                "description": "Replacement hunks for update_file.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "old_text": {
                                            "type": "string",
                                            "description": "Existing text to remove. May contain multiple lines.",
                                        },
                                        "new_text": {
                                            "type": "string",
                                            "description": "Replacement text to add. May contain multiple lines.",
                                        },
                                    },
                                    "required": ["old_text", "new_text"],
                                },
                            },
                        },
                        "required": ["type", "path"],
                    },
                }
            },
            "required": ["operations"],
        },
    }


def _web_search_function_tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web and return concise results relevant to the query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                },
                "required": ["query"],
            },
        },
    }


def _first_choice(deepseek_response: dict) -> dict:
    choices = deepseek_response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise bad_request("DeepSeek response did not contain choices.")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise bad_request("DeepSeek response choice was invalid.")
    return choice


def _message_to_response_output(
    message: dict,
    custom_tool_names: set[str] | None = None,
    compaction_requested: bool = False,
) -> list[dict]:
    output = []
    content = message.get("content")
    if content:
        if compaction_requested:
            output.append(
                {
                    "type": "compaction",
                    "encrypted_content": _encode_proxy_payload({"compaction_summary": content}),
                }
            )
        else:
            output.append(
                {
                    "id": _new_item_id("msg"),
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": content,
                            "annotations": [],
                        }
                    ],
                }
            )
    custom_tool_names = custom_tool_names or set()
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        if function.get("name") in custom_tool_names:
            output.append(_custom_tool_call_output_item(tool_call, function))
        else:
            output.append(
                {
                    "id": tool_call.get("id") or _new_item_id("fc"),
                    "type": "function_call",
                    "call_id": tool_call.get("id") or _new_item_id("call"),
                    "name": function.get("name"),
                    "arguments": function.get("arguments", "{}"),
                    "status": "completed",
                }
            )
    return output


def _custom_tool_call_output_item(tool_call: dict, function: dict) -> dict:
    arguments = function.get("arguments", "{}")
    input_value = arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments or "{}")
            if isinstance(parsed, dict):
                if function.get("name") == "apply_patch" and isinstance(parsed.get("operations"), list):
                    input_value = _apply_patch_operations_to_patch(parsed["operations"])
                elif isinstance(parsed.get("input"), str):
                    input_value = parsed["input"]
        except json.JSONDecodeError:
            input_value = arguments
    return {
        "id": tool_call.get("id") or _new_item_id("ctc"),
        "type": "custom_tool_call",
        "call_id": tool_call.get("id") or _new_item_id("call"),
        "name": function.get("name"),
        "input": input_value,
        "status": "completed",
    }


def _apply_patch_operations_to_patch(operations: list) -> str:
    lines = ["*** Begin Patch"]
    for operation in operations:
        if not isinstance(operation, dict):
            raise bad_request("apply_patch operations must be objects.")
        operation_type = operation.get("type")
        path = operation.get("path")
        if not isinstance(path, str) or not path:
            raise bad_request("apply_patch operations require a non-empty string `path`.")
        if operation_type == "add_file":
            content = operation.get("content", "")
            if not isinstance(content, str):
                raise bad_request("apply_patch add_file operation requires string `content`.")
            lines.append(f"*** Add File: {path}")
            content_lines = content.splitlines()
            if content.endswith("\n"):
                content_lines.append("")
            lines.extend(f"+{line}" for line in content_lines)
            continue
        if operation_type == "delete_file":
            lines.append(f"*** Delete File: {path}")
            continue
        if operation_type == "update_file":
            hunks = operation.get("hunks")
            if not isinstance(hunks, list) or not hunks:
                raise bad_request("apply_patch update_file operation requires non-empty `hunks` array.")
            lines.append(f"*** Update File: {path}")
            for hunk in hunks:
                if not isinstance(hunk, dict):
                    raise bad_request("apply_patch update_file hunks must be objects.")
                old_text = hunk.get("old_text")
                new_text = hunk.get("new_text")
                if not isinstance(old_text, str) or not isinstance(new_text, str):
                    raise bad_request("apply_patch update_file hunks require string `old_text` and `new_text`.")
                lines.append("@@")
                lines.extend(f"-{line}" for line in _patch_text_lines(old_text))
                lines.extend(f"+{line}" for line in _patch_text_lines(new_text))
            continue
        raise bad_request("apply_patch operation type must be add_file, delete_file, or update_file.")
    lines.append("*** End Patch")
    return "\n".join(lines)


def _patch_text_lines(text: str) -> list[str]:
    lines = text.splitlines()
    if text.endswith("\n"):
        lines.append("")
    return lines


def _reasoning_item(message: dict) -> dict | None:
    reasoning_content = message.get("reasoning_content")
    if not reasoning_content:
        return None
    return {
        "id": _new_item_id("rs"),
        "type": "reasoning",
        "summary": [],
        "encrypted_content": _encode_proxy_payload(
            {
                "reasoning_content": reasoning_content,
                "has_tool_calls": bool(message.get("tool_calls")),
            }
        ),
    }


def _encode_proxy_payload(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return f"ds_proxy.{encoded}"


def _decode_proxy_payload(encrypted_content: str) -> dict:
    if not encrypted_content.startswith("ds_proxy."):
        return {}
    encoded = encrypted_content.removeprefix("ds_proxy.")
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _reasoning_content_from_input_item(item: dict) -> str | None:
    encrypted_content = item.get("encrypted_content")
    if not isinstance(encrypted_content, str):
        return None
    payload = _decode_proxy_payload(encrypted_content)
    reasoning_content = payload.get("reasoning_content") if isinstance(payload, dict) else None
    return reasoning_content if isinstance(reasoning_content, str) else None


def _output_text(output: list[dict]) -> str:
    parts = []
    for item in output:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "".join(parts)


def _translate_usage(usage: dict | None) -> dict | None:
    if usage is None:
        return None
    translated = dict(usage)
    if "input_tokens" not in translated and "prompt_tokens" in usage:
        translated["input_tokens"] = usage["prompt_tokens"]
    if "output_tokens" not in translated and "completion_tokens" in usage:
        translated["output_tokens"] = usage["completion_tokens"]
    return translated


def _new_response_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


def _new_item_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"
