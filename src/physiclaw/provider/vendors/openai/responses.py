"""Responses wire items and parsing, independent of agent output semantics."""

import json
from copy import deepcopy
from typing import assert_never

from physiclaw.common.provider_errors import ProviderTransientError
from physiclaw.contract.dto import (
    AssistantMessage,
    ContentBlock,
    FinishReason,
    Message,
    OutputResultMessage,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from physiclaw.provider.chat.structured import strict_json
from physiclaw.provider.chat.usage import token_count
from physiclaw.provider.chat.wire import encode_content, output_result_to_message


def input_content(content: str | list[ContentBlock]) -> list[dict]:
    parts = encode_content(
        content,
        image_part=lambda block: {
            "type": "input_image",
            "image_url": f"data:{block.media_type};base64,{block.data_b64}",
        },
        empty="",
        label="openai.responses",
    )
    if isinstance(parts, str):
        return [{"type": "input_text", "text": parts}]
    return [
        {**part, "type": "input_text"} if part["type"] == "text" else part
        for part in parts
    ]


def encode_message(message: Message, *, model: str) -> dict | list[dict]:
    if isinstance(message, OutputResultMessage):
        return encode_message(output_result_to_message(message), model=model)
    if isinstance(message, (SystemMessage, UserMessage)):
        return {
            "role": "system" if isinstance(message, SystemMessage) else "user",
            "content": input_content(message.content),
        }
    if isinstance(message, ToolResultMessage):
        return {
            "type": "function_call_output",
            "call_id": message.tool_call_id,
            "output": input_content(message.content),
        }
    if isinstance(message, AssistantMessage):
        extra = message.vendor_extra.get("openai", {})
        if extra.get("model") == model and "output" in extra:
            # Keep encrypted reasoning, item IDs and assistant phase together.
            # store:false requires replaying items rather than remote references.
            return deepcopy(extra["output"])
        items = []
        if message.content:
            items.append({"role": "assistant", "content": message.content})
        items.extend(
            {
                "type": "function_call",
                "call_id": call.id,
                "name": call.name,
                "arguments": json.dumps(call.arguments, ensure_ascii=False),
            }
            for call in message.tool_calls
        )
        return items
    assert_never(message)


def parse_usage(raw: dict) -> Usage:
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return Usage()
    inputs = usage.get("input_tokens_details")
    inputs = inputs if isinstance(inputs, dict) else {}
    outputs = usage.get("output_tokens_details")
    outputs = outputs if isinstance(outputs, dict) else {}
    return Usage(
        prompt_tokens=token_count(usage.get("input_tokens")),
        completion_tokens=token_count(usage.get("output_tokens")),
        cached_tokens=token_count(inputs.get("cached_tokens")),
        cache_creation_tokens=token_count(inputs.get("cache_write_tokens")),
        reasoning_tokens=token_count(outputs.get("reasoning_tokens")),
    )


def parse_response(raw: object, *, model: str, structured: bool) -> AssistantMessage:
    if not isinstance(raw, dict):
        raise ProviderTransientError("invalid Responses reply: expected object")
    reply = AssistantMessage(
        content="",
        tool_calls=[],
        finish_reason=FinishReason.STOP,
        usage=parse_usage(raw),
        response_id=str(raw.get("id") or ""),
        response_model=str(raw.get("model") or ""),
        raw=raw,
    )
    status = raw.get("status")
    status = status if isinstance(status, str) else None
    if raw.get("error") or status in {"failed", "cancelled", "queued", "in_progress"}:
        error = ProviderTransientError(f"Responses generation failed: {status}")
        setattr(error, "reply", reply)
        raise error
    if status == "incomplete":
        details = raw.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        reply.finish_reason = {
            "max_output_tokens": FinishReason.LENGTH,
            "content_filter": FinishReason.CONTENT_FILTER,
        }.get(reason if isinstance(reason, str) else "", FinishReason.ERROR)
    elif status != "completed":
        reply.decode_error = "invalid Responses status"

    output = raw.get("output")
    if not isinstance(output, list):
        reply.decode_error = "invalid Responses output: expected items"
        output = []
    for item in output:
        try:
            if not isinstance(item, dict):
                raise ValueError("invalid Responses output item")
            kind = item.get("type")
            if kind == "reasoning":
                continue
            if kind == "message":
                if item.get("role") != "assistant" or not isinstance(
                    item.get("content"), list
                ):
                    raise ValueError("invalid assistant message")
                if (
                    item.get("status") not in (None, "completed")
                    and status == "completed"
                ):
                    raise ValueError("incomplete assistant message")
                for part in item["content"]:
                    if not isinstance(part, dict):
                        reply.decode_error = "invalid assistant content"
                        continue
                    if part.get("type") == "refusal":
                        reply.refused = True
                    elif part.get("type") == "output_text" and isinstance(
                        part.get("text"), str
                    ):
                        # Commentary stays in replay and usage, but only the
                        # final answer belongs to structured output.
                        if not structured or item.get("phase") != "commentary":
                            reply.content += part["text"]
                    else:
                        reply.decode_error = "unsupported assistant content"
            elif kind == "function_call":
                call_id, name = item.get("call_id"), item.get("name")
                if (
                    not isinstance(call_id, str)
                    or not call_id
                    or not isinstance(name, str)
                    or not name
                ):
                    raise ValueError("function call requires name and call_id")
                if any(call.id == call_id for call in reply.tool_calls):
                    raise ValueError("duplicate function call_id")
                if (
                    item.get("status") not in (None, "completed")
                    and status == "completed"
                ):
                    raise ValueError("incomplete function call")
                arguments = item.get("arguments")
                if not isinstance(arguments, str):
                    raise ValueError("function arguments must be JSON text")
                args = strict_json(arguments)
                if not isinstance(args, dict):
                    raise ValueError("function arguments must be an object")
                reply.tool_calls.append(ToolCall(call_id, name, args))
            else:
                raise ValueError(f"unsupported Responses output item: {kind}")
        except (ValueError, RecursionError) as exc:
            reply.decode_error = str(exc)
    if not (reply.content or reply.tool_calls or reply.refused):
        reply.decode_error = reply.decode_error or "missing assistant output"
    if reply.finish_reason == FinishReason.STOP and reply.tool_calls:
        reply.finish_reason = FinishReason.TOOL_CALLS
    if reply.decode_error and not structured:
        error = ProviderTransientError(reply.decode_error)
        setattr(error, "reply", reply)
        raise error
    if status == "completed" and not reply.decode_error:
        reply.vendor_extra["openai"] = {"model": model, "output": deepcopy(output)}
    return reply
