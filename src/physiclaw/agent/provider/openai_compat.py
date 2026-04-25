"""OpenAI-compatible base — `/chat/completions` wire format.

Vendors that speak this shape (Qwen/DashScope, OpenAI, Moonshot, Google)
inherit from `OpenAICompatibleProvider` and only declare endpoint +
catalog + auth. This file owns:

  - the request/response flow (`chat`)
  - DTO → wire serialization (`serialize_history`, delegating block-
    level encoding to `wire.py`)
  - cache-control marker placement (system + latest stubbed tool_result)
  - response parsing (`parse_openai_response`) including normalized
    finish_reason + Usage extraction

Cache-control marker layout — two anchors, chosen so cached bytes
survive turns AND wakes:

  1. System message (index 0). Session-stable; one cache_creation per
     `cache_key` ever; cache hits the first turn of every wake within
     the 5-min TTL.
  2. Latest superseded screen-obs `tool_result` — found via the typed
     `is_superseded` flag on the source `ToolResultMessage` (set by
     `compact.drop_stale_screens`). Deepest byte-stable point before
     the live multipart image; marking through the image would cache
     bytes that mutate away.

Provider-specific response leakage (Qwen's `reasoning_content`) is
stripped here at parse time so it never re-rides the wire on the next
turn (would break the prefix cache).
"""
import json
import logging
import uuid
from typing import Any

import httpx

from physiclaw.agent.engine.dto import (
    AssistantMessage,
    FinishReason,
    Message,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from physiclaw.agent.provider.provider_base import (
    EPHEMERAL_CACHE_CONTROL,
    BaseProvider,
    ProviderPermanentError,
    ProviderTransientError,
)
from physiclaw.agent.provider.wire import (
    assistant_to_wire,
    tool_result_to_wire,
    tool_to_wire,
    user_content_to_openai,
)

log = logging.getLogger(__name__)


class OpenAICompatibleProvider(BaseProvider):
    """Base for providers that speak the OpenAI `/chat/completions` wire
    format. See `BaseProvider` for the catalog/auth declarations vendors
    are expected to set; this class plugs the wire-shape hooks
    (`_encode_message` / `_mark_system` / `_mark_stub`) into the
    inherited `serialize_history` template, and adds the HTTP request
    flow in `chat()`."""

    async def chat(
        self,
        history: list[Message],
        tools: list[dict],
    ) -> AssistantMessage:
        wire = self.serialize_history(history)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": wire,
        }
        if tools:
            payload["tools"] = [tool_to_wire(t) for t in tools]
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

        return parse_openai_response(r.json())

    # ---------- serialize_history hooks (called by BaseProvider) ----------

    def _encode_message(self, msg: Message) -> dict | None:
        if isinstance(msg, SystemMessage):
            return {"role": "system", "content": msg.content}
        if isinstance(msg, UserMessage):
            return {"role": "user", "content": user_content_to_openai(msg.content)}
        if isinstance(msg, AssistantMessage):
            return assistant_to_wire(msg)
        if isinstance(msg, ToolResultMessage):
            return tool_result_to_wire(msg)
        log.warning("openai serialize: dropping unknown message type %r", type(msg).__name__)
        return None

    def _mark_system(self, entry: dict) -> dict:
        return _with_cache_marker(entry)

    def _mark_stub(self, entry: dict) -> dict:
        return _with_cache_marker(entry)


# ---------- cache marker helper ----------


def _with_cache_marker(entry: dict) -> dict:
    """Shallow-copy `entry` and wrap its string content in a single text
    block carrying an ephemeral cache_control. Caller guarantees string
    content (system message + stubbed tool_result both qualify in the
    OpenAI wire shape)."""
    out = dict(entry)
    out["content"] = [{
        "type": "text", "text": entry["content"],
        "cache_control": EPHEMERAL_CACHE_CONTROL,
    }]
    return out


# ---------- response parsing ----------


def parse_openai_response(raw: dict) -> AssistantMessage:
    """OpenAI chat completion → `AssistantMessage`. Drops provider-specific
    fields (e.g. Qwen's `reasoning_content`) from the returned content so
    they never leak into engine history; the raw dict is preserved on the
    return value for log-side inspection."""
    choice = raw.get("choices", [{}])[0]
    message = choice.get("message") or {}
    finish_raw = choice.get("finish_reason") or "stop"

    content = message.get("content") or ""
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
        usage=_parse_openai_usage(raw),
        raw=raw,
    )


def _parse_openai_usage(raw: dict) -> Usage:
    """OpenAI/DashScope `usage` block → normalized `Usage`. Cache stats
    live under `usage.prompt_tokens_details.cached_tokens` /
    `cache_creation_input_tokens` (DashScope; OpenAI exposes the same
    shape). Missing values default to 0 — readers treat zeros as
    'no data', not '0% cache'."""
    u = (raw or {}).get("usage") or {}
    details = u.get("prompt_tokens_details") or {}
    return Usage(
        prompt_tokens=int(u.get("prompt_tokens", 0) or 0),
        completion_tokens=int(u.get("completion_tokens", 0) or 0),
        cached_tokens=int(details.get("cached_tokens", 0) or 0),
        cache_creation_tokens=int(details.get("cache_creation_input_tokens", 0) or 0),
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
