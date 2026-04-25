"""OpenAI-compatible wire-format adapters.

Two directions:
  - Request-side (`tool_to_wire`) — local tool schema → OpenAI `tools=`.
  - History-side (`assistant_to_wire`, `tool_result_to_wire`,
    `blocks_to_tool_content`) — engine history dicts → OpenAI roles.

Provider-specific fields (Qwen's `reasoning_content` etc.) are stripped
inside `base.parse_openai_response` before they reach the engine — they
must NEVER appear in the assistant-echo or tool_result that gets
re-serialized back to the wire on the next turn (would break the prefix
cache and confuse the model).
"""
import base64
import json
import logging
from typing import Any

from physiclaw.agent.engine import compact
from physiclaw.agent.engine.dto import AssistantMessage, ToolCall, ToolResult

log = logging.getLogger(__name__)


def tool_to_wire(tool: dict) -> dict:
    """Normalized tool dict → OpenAI `tools=` wire format."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or {
                "type": "object", "properties": {},
            },
        },
    }


def assistant_to_wire(asst: AssistantMessage) -> dict[str, Any]:
    """`AssistantMessage` → OpenAI chat wire format. Drops provider-specific
    leakage — `reasoning_content` etc. never ride along in history."""
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


def tool_result_to_wire(call: ToolCall, result: ToolResult) -> dict[str, Any]:
    """`ToolCall` + `ToolResult` → OpenAI tool-role message. Principle 6:
    matching `tool_call_id` pairs the result with its call."""
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "content": result.content,
    }


def blocks_to_tool_content(blocks: list[dict]) -> str | list[dict]:
    """MCP content blocks → OpenAI tool-role content. Returns a bare string
    when no images are present so common text-only tools stay cheap.

    Every image is re-encoded via `compact.scale_image_bytes` on the way
    through: PNG → JPEG, long edge clamped to MAX_IMAGE_EDGE. Keeps each
    turn's image payload small before it enters history (where the prefix
    cache has to re-transmit it on every subsequent request).
    """
    # Fast path: text-only, single block.
    if len(blocks) == 1 and blocks[0].get("type") == "text":
        return blocks[0]["text"]

    texts: list[str] = []
    images: list[dict] = []
    for b in blocks:
        if b.get("type") == "text":
            texts.append(b["text"])
        elif b.get("type") == "image":
            try:
                raw = base64.b64decode(b["data"])
                scaled, mime = compact.scale_image_bytes(raw)
                new_b64 = base64.b64encode(scaled).decode()
            except Exception:
                log.exception("failed to scale image block; forwarding original")
                new_b64 = b["data"]
                mime = b.get("mime_type") or "image/jpeg"
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{new_b64}"},
            })
    if images:
        parts: list[dict[str, Any]] = []
        if texts:
            parts.append({"type": "text", "text": "\n".join(texts)})
        parts.extend(images)
        return parts
    return "\n".join(texts) if texts else "(empty tool result)"
