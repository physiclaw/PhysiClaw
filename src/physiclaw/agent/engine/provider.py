"""Provider adapter — the only place that speaks a provider's wire format.

Principle 2: normalize at the boundary.
  - Request: build wire-format from standard chat messages + tool schemas.
  - Response: parse into `AssistantMessage` (with `ToolCall` list and real
    `finish_reason`). Strip provider-specific fields like Qwen's
    `reasoning_content` before returning — they MUST NOT leak into engine
    history, or re-serializing will break the prefix cache or confuse the
    next turn's model.

Principle 3: preserve the real `finish_reason`. Do not derive it from
content. The engine routes differently on "length" / "content_filter" /
"tool_calls" / "stop".
"""
import base64
import json
import logging
import os
import uuid
from typing import Any, Protocol

import httpx

from physiclaw.agent.engine import compact
from physiclaw.agent.engine.dto import (
    AssistantMessage,
    FinishReason,
    ToolCall,
    ToolResult,
)

log = logging.getLogger(__name__)

# Public registry of provider names that have a working Provider class.
# The launcher accepts these (plus "claude-code" for the external CLI
# subprocess path). Adding a provider: implement the class, register it
# in _PROVIDER_FACTORIES below, and add the name here.
PROVIDER_NAMES = ("qwen",)

# Per-provider endpoint defaults. `claude` is intentionally absent —
# Anthropic isn't OpenAI-compatible and needs a different Provider class.
_PROVIDER_ENDPOINTS: dict[str, tuple[str, str]] = {
    # name: (base_url, default_model)
    "qwen":    ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.6-plus"),
    "kimi":    ("https://api.moonshot.cn/v1",                        "kimi-latest"),
    "chatgpt": ("https://api.openai.com/v1",                         "gpt-4o"),
}


def provider_endpoint(name: str) -> tuple[str, str]:
    """Return (base_url, default_model) for an OpenAI-compatible provider.

    Raises KeyError if the provider isn't OpenAI-compatible (e.g. anthropic
    Claude needs its own Provider class — not in this map).
    """
    try:
        return _PROVIDER_ENDPOINTS[name]
    except KeyError:
        raise KeyError(
            f"no OpenAI-compatible endpoint for provider {name!r}; "
            f"known: {tuple(_PROVIDER_ENDPOINTS)}"
        ) from None


class ProviderError(Exception):
    """Base for provider failures."""


class ProviderTransientError(ProviderError):
    """Transport issue, timeout, 429, or 5xx — worth retrying."""


class ProviderPermanentError(ProviderError):
    """4xx (except 429) — retries will keep failing, fail fast."""


class Provider(Protocol):
    model: str

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> AssistantMessage: ...

    async def aclose(self) -> None: ...


class QwenProvider:
    """DashScope OpenAI-compatible endpoint. Uses native tool_calls."""

    def __init__(
        self,
        model: str | None = None,
        timeout: float = 120.0,
        base_url: str | None = None,
    ):
        from physiclaw.config import qwen_api_key

        key = qwen_api_key()
        if not key:
            raise RuntimeError(
                "Qwen credential not found. Set QWEN_API_KEY (or "
                "DASHSCOPE_API_KEY) in the environment, or "
                "[provider] qwen_api_key in ~/.physiclaw/config.toml."
            )
        endpoint, default_model = provider_endpoint("qwen")
        self.model = model or os.environ.get("QWEN_MODEL", default_model)
        self._client = httpx.AsyncClient(
            base_url=base_url or endpoint,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> AssistantMessage:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = [_tool_to_wire(t) for t in tools]
            # tool_choice "auto" is the OpenAI default; being explicit avoids
            # provider surprises.
            payload["tool_choice"] = "auto"

        try:
            r = await self._client.post("/chat/completions", json=payload)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            raise ProviderTransientError(f"transport: {e}") from e

        if r.status_code == 429 or r.status_code >= 500:
            log.warning("provider HTTP %s (transient): %s", r.status_code, r.text[:200])
            raise ProviderTransientError(f"HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code >= 400:
            log.error("provider HTTP %s (permanent): %s", r.status_code, r.text[:500])
            raise ProviderPermanentError(f"HTTP {r.status_code}: {r.text[:500]}")

        return _parse_response(r.json())

    async def aclose(self) -> None:
        await self._client.aclose()


# Factory dict — populated below after each Provider class is defined.
# Adding a provider: implement the class, register it here, list it in
# PROVIDER_NAMES (and, if OpenAI-compatible, _PROVIDER_ENDPOINTS).
_PROVIDER_FACTORIES: dict[str, type[Provider]] = {}


def make_provider(name: str) -> Provider:
    """name → Provider instance. Credentials come from the chosen
    Provider's own env-var lookup in __init__."""
    factory = _PROVIDER_FACTORIES.get(name)
    if factory is not None:
        return factory()
    if name in PROVIDER_NAMES:
        raise NotImplementedError(
            f"Provider {name!r} listed in PROVIDER_NAMES but not registered "
            f"in _PROVIDER_FACTORIES. Add a Provider subclass and register it."
        )
    raise ValueError(
        f"unknown provider {name!r} (known: {', '.join(PROVIDER_NAMES)})"
    )


_PROVIDER_FACTORIES["qwen"] = QwenProvider


# ---------- wire adapters (one provider, one format; add impls here later) ----------


def _tool_to_wire(tool: dict) -> dict:
    """Normalized tool dict → OpenAI tools= wire format."""
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


def _parse_response(raw: dict) -> AssistantMessage:
    """OpenAI chat completion → AssistantMessage. Strips `reasoning_content`
    and any other provider-specific fields from the returned content."""
    choice = raw.get("choices", [{}])[0]
    message = choice.get("message") or {}
    finish_raw = choice.get("finish_reason") or "stop"

    content = message.get("content") or ""
    # Some Qwen variants put chain-of-thought in `reasoning_content`.
    # Keep it out of the assistant-echo path (principle 2). The engine may
    # log it from `raw` for debugging.
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)

    raw_tool_calls = message.get("tool_calls") or []
    tool_calls: list[ToolCall] = []
    for tc in raw_tool_calls:
        try:
            fn = tc.get("function") or {}
            args_str = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
                if not isinstance(args, dict):
                    args = {"_raw": args}
            except json.JSONDecodeError:
                # Principle 4/5: don't silently drop; pass malformed args
                # through so the validator can flag it as an error on
                # dispatch (and pair a tool_result).
                args = {"_malformed_json": args_str}
            tool_calls.append(ToolCall(
                id=tc.get("id") or f"auto_{uuid.uuid4().hex[:8]}",
                name=fn.get("name") or "",
                arguments=args,
            ))
        except Exception:
            log.exception("failed to parse tool_call: %s", tc)

    return AssistantMessage(
        content=content,
        tool_calls=tool_calls,
        finish_reason=_normalize_finish(finish_raw),
        raw=raw,
    )


def _normalize_finish(r: str) -> FinishReason:
    # OpenAI surfaces: stop, length, tool_calls, content_filter, function_call.
    if r == "function_call":
        return FinishReason.TOOL_CALLS
    try:
        return FinishReason(r)
    except ValueError:
        log.warning("unknown finish_reason %r — treating as stop", r)
        return FinishReason.STOP


# ---------- history-shape adapters (wire format for this provider) ----------


def assistant_to_wire(asst: AssistantMessage) -> dict[str, Any]:
    """AssistantMessage → OpenAI chat wire format. Drops provider-specific
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
    """ToolCall + ToolResult → OpenAI tool-role message. Principle 6:
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


__all__ = [
    "Provider",
    "QwenProvider",
    "ProviderError",
    "ProviderTransientError",
    "ProviderPermanentError",
    "PROVIDER_NAMES",
    "provider_endpoint",
    "make_provider",
    "assistant_to_wire",
    "tool_result_to_wire",
    "blocks_to_tool_content",
]
