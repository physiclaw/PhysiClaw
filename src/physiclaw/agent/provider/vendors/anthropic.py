"""Anthropic — Claude models via the official `anthropic` SDK.

Distinct from `claude-code` (the subprocess engine that shells out to
the `claude` CLI). This provider calls Anthropic's `/v1/messages`
endpoint directly through `AsyncAnthropic`.

OpenClaw's docs explicitly warn that Anthropic's OpenAI-compat shim
breaks on multi-round tool calls — every PhysiClaw wake is multi-round,
so we must use the native `/v1/messages` path. The SDK handles auth,
retry, beta headers, and request/response typing; we only translate
between OpenAI-shape engine history and Anthropic message blocks.

Translation rules (engine OpenAI shape → Anthropic):
  - `role: "system"` messages collapse into the top-level `system` field.
  - `role: "tool"` messages become `user` messages with a single
    `tool_result` block (matched via `tool_use_id`).
  - Assistant `tool_calls[]` become `tool_use` content blocks.
  - User `image_url` blocks (data URLs) become `image` blocks with a
    `base64` source.

Auth: `ANTHROPIC_API_KEY` env, or `[provider] anthropic_api_key` in
`~/.physiclaw/config.toml`.

Model ref examples:  `anthropic/claude-opus-4-7`,
`anthropic/claude-sonnet-4-6`, `anthropic/claude-haiku-4-5`.
"""
import json
import logging
import uuid

from physiclaw.agent.engine.dto import AssistantMessage, FinishReason, ToolCall
from physiclaw.agent.provider.base import (
    ModelEntry,
    OpenAICompatibleProvider,
    ProviderPermanentError,
    ProviderTransientError,
)

# `anthropic` SDK is lazy-imported inside `_build_client` and `chat()` so
# `physiclaw --help` (and any session that doesn't pick this provider)
# avoids loading the ~3MB SDK + transitive deps.

log = logging.getLogger(__name__)

# Anthropic requires `max_tokens` on every request. 8192 is comfortable
# for tool-loop turns; bump if responses get truncated.
_DEFAULT_MAX_TOKENS = 8192

_STOP_REASON_MAP: dict[str, FinishReason] = {
    "end_turn":      FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "max_tokens":    FinishReason.LENGTH,
    "tool_use":      FinishReason.TOOL_CALLS,
    # `refusal`, `pause_turn`, etc. fall through to STOP.
}


class AnthropicProvider(OpenAICompatibleProvider):
    PROVIDER_ID = "anthropic"
    # Informational only — `AsyncAnthropic` manages the URL itself.
    BASE_URL = "https://api.anthropic.com/v1"
    # API_KEY_ENV_VARS defaults to ("ANTHROPIC_API_KEY",) by convention.
    THINKING_FORMAT = None
    # All Claude 4.x support extended thinking + vision.
    MODELS = (
        ModelEntry("claude-opus-4-7",   context_window=200_000),
        ModelEntry("claude-sonnet-4-6", context_window=200_000),
        ModelEntry("claude-haiku-4-5",  context_window=200_000),
    )

    def _build_client(self, key: str, *, timeout: float, base_url: str | None):
        """Override: use Anthropic's official async SDK instead of httpx
        directly. Lazy-imported so non-anthropic sessions don't pay the
        SDK load cost."""
        from anthropic import AsyncAnthropic
        return AsyncAnthropic(api_key=key, base_url=base_url, timeout=timeout)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> AssistantMessage:
        from anthropic import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            RateLimitError,
        )

        system, am_messages = _split_system(messages)
        payload: dict = {
            "model":      self.model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "messages":   am_messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [_tool_to_anthropic(t) for t in tools]

        try:
            resp = await self._client.messages.create(**payload)
        except (APIConnectionError, APITimeoutError) as e:
            raise ProviderTransientError(f"transport: {e}") from e
        except RateLimitError as e:
            raise ProviderTransientError(f"rate limited: {e}") from e
        except APIStatusError as e:
            text = (e.message or "")[:500]
            if e.status_code >= 500:
                log.warning("anthropic HTTP %s (transient): %s", e.status_code, text)
                raise ProviderTransientError(f"HTTP {e.status_code}: {text}") from e
            log.error("anthropic HTTP %s (permanent): %s", e.status_code, text)
            raise ProviderPermanentError(f"HTTP {e.status_code}: {text}") from e

        return _from_anthropic_response(resp)

    async def aclose(self) -> None:
        # AsyncAnthropic uses .close(), not .aclose() like httpx.
        await self._client.close()


# ---------- request translation (OpenAI shape → Anthropic) ----------


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """System messages collapse into Anthropic's top-level `system` field;
    all other roles become entries in the `messages` array. Returns
    `(system_text, anthropic_messages)`."""
    system_parts: list[str] = []
    am: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            system_parts.append(_flatten_text(content))
        elif role == "user":
            am.append({"role": "user", "content": _to_anthropic_content(content)})
        elif role == "assistant":
            am.append({"role": "assistant", "content": _assistant_blocks(msg)})
        elif role == "tool":
            am.append({
                "role": "user",
                "content": [{
                    "type":         "tool_result",
                    "tool_use_id":  msg.get("tool_call_id", ""),
                    "content":      _to_anthropic_content(content),
                }],
            })
        else:
            log.warning("anthropic: dropping unknown role %r", role)
    return "\n\n".join(p for p in system_parts if p), am


def _flatten_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            (b.get("text", "") if isinstance(b, dict) and b.get("type") == "text" else str(b))
            for b in content
        ]
        return "\n".join(p for p in parts if p)
    return str(content) if content is not None else ""


def _to_anthropic_content(content) -> str | list[dict]:
    """OpenAI user/tool-result content → Anthropic content (string or block list)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content is not None else ""
    blocks: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            blocks.append({"type": "text", "text": str(part)})
            continue
        ptype = part.get("type")
        if ptype == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif ptype == "image_url":
            blocks.append(_image_url_to_anthropic(part.get("image_url", {}).get("url", "")))
        else:
            # Unknown block — pass through as JSON text rather than drop.
            blocks.append({"type": "text", "text": json.dumps(part, ensure_ascii=False)})
    return blocks or [{"type": "text", "text": ""}]


def _image_url_to_anthropic(url: str) -> dict:
    """`data:image/jpeg;base64,...` → Anthropic image block."""
    if url.startswith("data:"):
        header, _, b64 = url.partition(",")
        media_type = header[5:].split(";")[0] or "image/jpeg"
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        }
    # Non-data URL — Anthropic accepts URL sources too.
    return {"type": "image", "source": {"type": "url", "url": url}}


def _assistant_blocks(msg: dict) -> list[dict]:
    """OpenAI assistant message (text + tool_calls) → Anthropic content blocks."""
    blocks: list[dict] = []
    text = msg.get("content") or ""
    if isinstance(text, str) and text:
        blocks.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args_str = fn.get("arguments") or "{}"
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else args_str
        except json.JSONDecodeError:
            args = {"_malformed_json": args_str}
        if not isinstance(args, dict):
            args = {"_raw": args}
        blocks.append({
            "type":  "tool_use",
            "id":    tc.get("id") or f"auto_{uuid.uuid4().hex[:8]}",
            "name":  fn.get("name") or "",
            "input": args,
        })
    if not blocks:
        # Anthropic rejects empty assistant content arrays.
        blocks.append({"type": "text", "text": ""})
    return blocks


def _tool_to_anthropic(tool: dict) -> dict:
    """Local tool schema → Anthropic `tools=` entry. The shape happens to
    match our local format almost verbatim — `input_schema` is the same key."""
    return {
        "name":         tool["name"],
        "description":  tool.get("description", ""),
        "input_schema": tool.get("input_schema") or {"type": "object", "properties": {}},
    }


# ---------- response translation (Anthropic → AssistantMessage) ----------


def _from_anthropic_response(resp) -> AssistantMessage:
    """Anthropic `Message` (Pydantic model) → `AssistantMessage`.

    Drops `thinking` blocks and other provider-specific content from the
    assistant-echo path (principle 2) — they would break re-serialization
    to history. The full raw payload is preserved on `.raw` for log-side
    inspection."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in (resp.content or []):
        bt = getattr(block, "type", None)
        if bt == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif bt == "tool_use":
            tool_calls.append(ToolCall(
                id=getattr(block, "id", None) or f"auto_{uuid.uuid4().hex[:8]}",
                name=getattr(block, "name", "") or "",
                arguments=getattr(block, "input", None) or {},
            ))

    stop_raw = getattr(resp, "stop_reason", None) or "end_turn"
    finish = _STOP_REASON_MAP.get(stop_raw, FinishReason.STOP)
    raw_dict = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)

    return AssistantMessage(
        content="\n".join(text_parts),
        tool_calls=tool_calls,
        finish_reason=finish,
        raw=raw_dict,
    )
