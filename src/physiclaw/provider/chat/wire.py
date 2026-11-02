"""Provider-side message projection and wire encoding.

Adapters translate shared message DTOs without changing caller-owned history.
Each vendor owns the opaque state needed to replay its responses.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

from physiclaw.contract.dto import (
    AssistantMessage,
    ContentBlock,
    ImageBlock,
    OutputResultMessage,
    TextBlock,
    ToolResultMessage,
    UserMessage,
)

log = logging.getLogger(__name__)


# ---------- request-side: tool schema → wire ----------


def tool_to_wire(tool: dict) -> dict:
    """Local tool schema → OpenAI `tools=` wire format."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema")
            or {
                "type": "object",
                "properties": {},
            },
        },
    }


# ---------- DTO → OpenAI history wire ----------


def output_result_to_message(
    result: OutputResultMessage,
) -> ToolResultMessage | UserMessage:
    """Project host feedback into the original reply's conversation protocol."""
    if result.reply.tool_calls:
        if len(result.reply.tool_calls) != 1:
            raise ValueError("Structured feedback requires one output function call")
        return ToolResultMessage(
            result.reply.tool_calls[0].id,
            result.content,
            is_error=result.is_error,
            is_superseded=result.is_superseded,
        )
    label = (
        "Structured output result (error):"
        if result.is_error
        else "Structured output result:"
    )
    content: list[ContentBlock] = (
        [TextBlock(f"{label}\n{result.content}")]
        if isinstance(result.content, str)
        else [TextBlock(label), *result.content]
    )
    return UserMessage(content=content)


def assistant_to_wire(asst: AssistantMessage) -> dict[str, Any]:
    """`AssistantMessage` (DTO) → OpenAI assistant message wire format.
    Drops provider-specific leakage — `reasoning_content` etc. never
    ride along in history."""
    msg: dict[str, Any] = {"role": "assistant", "content": asst.content or ""}
    if asst.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in asst.tool_calls
        ]
    return msg


def tool_result_to_wire(result: ToolResultMessage) -> dict[str, Any]:
    """`ToolResultMessage` (DTO) → OpenAI `role: tool` message. Principle 6:
    `tool_call_id` pairs the result with its originating call."""
    return {
        "role": "tool",
        "tool_call_id": result.tool_call_id,
        "content": user_content_to_openai(result.content),
    }


def tool_result_with_user_images(result: ToolResultMessage) -> list[dict]:
    """Keep the paired tool receipt and move images to a user message."""
    text_blocks: list[ContentBlock] = []
    image_blocks: list[ContentBlock] = []
    if isinstance(result.content, list):
        for b in result.content:
            (image_blocks if isinstance(b, ImageBlock) else text_blocks).append(b)
    if not image_blocks:
        return [tool_result_to_wire(result)]
    return [
        {
            "role": "tool",
            "tool_call_id": result.tool_call_id,
            "content": user_content_to_openai(text_blocks)
            if text_blocks
            else "(image attached after tool results)",
        },
        {"role": "user", "content": user_content_to_openai(image_blocks)},
    ]


def encode_content(
    content: str | list[ContentBlock] | None,
    *,
    image_part: Callable[[ImageBlock], dict],
    empty: str | list[dict],
    label: str,
) -> str | list[dict]:
    """Shared ContentBlock dispatch for every wire shape. Plain strings
    pass through unchanged so text-only messages stay cheap; unknown
    block types are dropped with a warning (tagged `label` so the log
    names the caller); an all-dropped/empty list falls back to `empty`
    (each shape's legal empty content). TextBlock's part is inlined —
    OpenAI and Anthropic agree on `{type: text, text}` — so callers
    supply only what actually differs (`image_part`, `empty`), and
    ImageBlock/scaling changes land here once."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content is not None else ""
    parts: list[dict] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            parts.append(image_part(block))
        else:
            log.warning("%s: dropping unknown block %r", label, type(block).__name__)
    return parts or empty


def anthropic_image_part(block: ImageBlock) -> dict:
    """An `ImageBlock` in Anthropic's base64 image format."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": block.media_type,
            "data": block.data_b64,
        },
    }


def _openai_image_part(block: ImageBlock) -> dict:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{block.media_type};base64,{block.data_b64}"},
    }


def user_content_to_openai(content: str | list[ContentBlock]) -> str | list[dict]:
    """Engine ContentBlocks → OpenAI multipart content array."""
    return encode_content(
        content,
        image_part=_openai_image_part,
        empty="",
        label="user_content_to_openai",
    )
