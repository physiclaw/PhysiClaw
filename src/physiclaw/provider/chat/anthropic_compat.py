"""Anthropic-compatible base — `/v1/messages` wire format via the
official `anthropic` SDK.

OpenClaw's docs warn that Anthropic's OpenAI-compat shim breaks on
multi-round tool calls — every PhysiClaw wake is multi-round, so we
use the native messages endpoint. Vendors speaking this shape (just
`anthropic` today) inherit from `AnthropicCompatibleProvider` and
declare `BASE_URL` plus any auth quirks.

Cache-control marker layout (the *why* — block-level translation rules
live with the functions that emit them). Up to four breakpoints, within
Anthropic's limit of four:
  1. `system` field, sent as `[{type: text, text, cache_control: ephemeral}]`.
  2. Latest stubbed screen-obs `tool_result` — the source DTO has
     `is_superseded=True` (set by `compact.drop_stale_screens`); the
     base `serialize_history` template invokes the `AnthropicCacheMarkers`
     factory to attach `cache_control` to the inner tool_result block.
     No string parsing, no post-pass.
  3.+4. Two moving tail anchors (4 breakpoints total — Anthropic's
     limit). Anthropic caches only up to explicit breakpoints (no
     auto-extension over a stable prefix), so without them everything
     after the stub — the live screen and every turn since — re-bills
     as fresh input on every request; measured ~3–5k tokens/turn on a
     real session. The two anchors split the two failure modes a
     single one can't cover (replay-measured):
       - last `tool_result` BEFORE the live screen: survives the next
         `drop_stale_screens` rewrite — an entry covering the live
         view dies unread the turn a new screen supersedes it, and its
         1.25× write-then-die traffic ate almost the whole saving when
         this was the only anchor.
       - last `tool_result` overall: advances every turn, so note-only
         stretches read at the cached rate; when a supersede kills it,
         the pre-view anchor above backs it up.
     The volatile per-turn tails (plan/scratchpad) are UserMessages
     appended after both, outside the cached prefix. Marking is
     additive (`cache_control` key only), so an entry an anchor has
     moved off serializes byte-identically minus the key and the
     prefix through it still matches.

Signed `thinking` and `redacted_thinking` blocks stay out of public
assistant text. Responses carrying them retain their original content
order in `vendor_extra` for exact replay on tool continuations; the
Anthropic API requires these blocks unchanged. Other providers ignore
this metadata. The raw payload remains available for trace inspection.
"""

import logging
import uuid
from copy import deepcopy
from typing import Any

from physiclaw.contract.dto import (
    AssistantMessage,
    FinishReason,
    Message,
    SystemMessage,
    Thinking,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from physiclaw.provider.chat.base import (
    EPHEMERAL_CACHE_CONTROL,
    THINKING_BUDGETS,
    BaseChatProvider,
    CacheMarkers,
    token_count,
)
from physiclaw.provider.chat.wire import anthropic_image_part, encode_content
from physiclaw.provider.provider_base import (
    ProviderPermanentError,
    ProviderTransientError,
    describe,
)

# `anthropic` SDK is lazy-imported inside `_build_client` and `chat()` so
# `physiclaw --help` (and any session that doesn't pick this provider)
# avoids loading the ~3MB SDK + transitive deps.

log = logging.getLogger(__name__)

# Anthropic requires `max_tokens` on every request. 8192 is comfortable
# for tool-loop turns; bump if responses get truncated.
_DEFAULT_MAX_TOKENS = 8192

# Anthropic rejects empty content arrays; this is the canonical
# fallback for both the content encoder and assistant blocks.
# Treat as immutable — never mutate.
_EMPTY_TEXT_BLOCK = {"type": "text", "text": ""}
_EMPTY_CONTENT = [_EMPTY_TEXT_BLOCK]

_STOP_REASON_MAP: dict[str, FinishReason] = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "tool_use": FinishReason.TOOL_CALLS,
    # `refusal`, `pause_turn`, etc. fall through to STOP.
}


class AnthropicCacheMarkers(CacheMarkers):
    """Anthropic-shape marker mechanics. Every messages-array anchor —
    the stub and the two moving tails — lands on a `tool_result` entry,
    and Anthropic accepts `cache_control` directly on a `tool_result`
    block, so one mechanic serves them all: annotate the inner block. The
    entry shape from `_encode_message(ToolResultMessage)` is
    `{role: user, content: [tr_block]}`; we shallow-copy the wrapper
    and the inner block so caller-held dicts aren't mutated, and the
    marking is additive so an entry the anchor has moved off serializes
    byte-identically minus the key. `mark_system` stays the identity —
    Anthropic's system rides outside the messages array and is marked
    on the top-level `system` payload field in `chat()`."""

    def mark_stub(self, entry: dict) -> dict:
        return _mark_tool_result(entry)

    def mark_tail(self, entry: dict) -> dict:
        return _mark_tool_result(entry)


class AnthropicCompatibleProvider(BaseChatProvider):
    """Base for providers speaking Anthropic's `/v1/messages` shape via
    `AsyncAnthropic`. See `BaseChatProvider` for the auth declarations
    vendors are expected to set; this class plugs the wire-shape
    encoder (`_encode_message`) and the `AnthropicCacheMarkers` factory
    into the inherited `serialize_history` template, and adds the
    request flow in `chat()`."""

    CACHE_MARKERS: CacheMarkers = AnthropicCacheMarkers()

    def _build_client(self, key: str, *, timeout: float, base_url: str | None):
        """Override: use Anthropic's official async SDK instead of httpx
        directly. Lazy-imported so non-anthropic sessions don't pay the
        SDK load cost.

        The SDK's internal httpx client defaults to ``trust_env=True``, so it
        honours ``HTTP(S)_PROXY`` for this external endpoint — consistent with
        the base ``_build_client`` and the deliberate inverse of the localhost
        clients that bypass the proxy. Don't pass a bespoke ``http_client`` just
        to set that: it would drop the SDK's tuned connection defaults."""
        from anthropic import AsyncAnthropic

        # Explicit arg wins, else the `[providers.anthropic] base_url`
        # config override — previously ignored (the raw arg is always None
        # via make_chat_provider). Deliberately NOT _resolved_base_url(): the
        # class BASE_URL carries a `/v1` suffix (it only satisfies the
        # base-class non-empty check), and the SDK appends `/v1/...`
        # itself — feeding it the class default would double the prefix.
        # None → the SDK's own default endpoint.
        from physiclaw.common.config import provider_base_url_override

        return AsyncAnthropic(
            api_key=key,
            base_url=base_url or provider_base_url_override(self.PROVIDER_ID),
            timeout=timeout,
        )

    async def _close_client(self) -> None:
        # AsyncAnthropic uses .close(), not .aclose() like httpx.
        await self._client.close()

    async def list_models(self) -> list[dict]:
        """Anthropic exposes models via `client.models.list()`. Each
        entry surfaces `id`, `display_name`, `created_at`. Iterated (not
        awaited once) so the SDK auto-paginates: a single await yields
        only page 1 (default 20), and this cache is `models use`'s
        validation source of truth — truncation rejects real models."""
        return [
            {
                "id": m.id,
                "display_name": m.display_name,
                "created_at": str(m.created_at),
            }
            async for m in self._client.models.list(limit=100)
        ]

    # ---------- serialize_history hooks (called by BaseChatProvider) ----------

    def _encode_message(self, msg: Message) -> dict | None:
        if isinstance(msg, SystemMessage):
            # Skipped here — system content rides outside the messages
            # array. `chat()` reads it via `_extract_system_text`.
            return None
        if isinstance(msg, UserMessage):
            return {"role": "user", "content": _content_to_anthropic(msg.content)}
        if isinstance(msg, AssistantMessage):
            return {"role": "assistant", "content": _assistant_blocks(msg)}
        if isinstance(msg, ToolResultMessage):
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id,
                        "content": _content_to_anthropic(msg.content),
                    }
                ],
            }
        log.warning("anthropic: dropping unknown message type %r", type(msg).__name__)
        return None

    # ---------- request flow ----------

    def thinking_params(self, thinking: Thinking) -> dict[str, Any]:
        """Extended thinking is a token budget; `max_tokens` grows by it
        so the answer keeps its whole allowance. "off" sends no block —
        thinking is off unless asked for."""
        budget = THINKING_BUDGETS.get(thinking)
        if budget is None:
            return {}
        return {
            "thinking": {"type": "enabled", "budget_tokens": budget},
            "max_tokens": _DEFAULT_MAX_TOKENS + budget,
        }

    async def _chat(
        self,
        history: list[Message],
        tools: list[dict],
        *,
        thinking: Thinking | None = None,
    ) -> AssistantMessage:
        from anthropic import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            RateLimitError,
        )

        am_messages = self.serialize_history(history)
        system = _extract_system_text(history)
        payload: dict = {
            "model": self.model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "messages": am_messages,
        }
        if thinking is not None:
            payload.update(self.thinking_params(thinking))
        if system:
            # Anthropic's `system` accepts a list of text blocks; a
            # `cache_control` on the trailing block caches the whole
            # system for 5 min — the cross-wake anchor.
            payload["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": EPHEMERAL_CACHE_CONTROL,
                }
            ]
        if tools:
            payload["tools"] = [_tool_to_anthropic(t) for t in tools]

        try:
            resp = await self._client.messages.create(**payload)
        except (APIConnectionError, APITimeoutError) as e:
            raise ProviderTransientError(f"transport: {describe(e)}") from e
        except RateLimitError as e:
            raise ProviderTransientError(f"rate limited: {e}") from e
        except APIStatusError as e:
            text = (e.message or "")[:500]
            if e.status_code >= 500:
                log.debug(
                    "HTTP %s (transient): %s",
                    e.status_code,
                    text,
                    extra={
                        "event": "provider.http_failed",
                        "http_status": e.status_code,
                    },
                )
                raise ProviderTransientError(f"HTTP {e.status_code}: {text}") from e
            log.debug(
                "HTTP %s (permanent): %s",
                e.status_code,
                text,
                extra={"event": "provider.http_failed", "http_status": e.status_code},
            )
            raise ProviderPermanentError(f"HTTP {e.status_code}: {text}") from e

        return _from_anthropic_response(resp)


# ---------- request translation (DTO → Anthropic blocks) ----------


def _mark_tool_result(entry: dict) -> dict:
    """Attach an ephemeral `cache_control` to the inner tool_result
    block of a `{role: user, content: [tr_block]}` entry, copying both
    layers so caller-held dicts stay untouched."""
    tr_block = entry["content"][0]
    return {
        **entry,
        "content": [{**tr_block, "cache_control": EPHEMERAL_CACHE_CONTROL}],
    }


def _extract_system_text(history: list[Message]) -> str:
    """Concatenate all `SystemMessage` content into the single string
    Anthropic's top-level `system` field expects. Empty when no system
    messages are present."""
    return "\n\n".join(
        m.content for m in history if isinstance(m, SystemMessage) and m.content
    )


def _content_to_anthropic(content) -> str | list[dict]:
    """User / tool-result content (`str` or list of `ContentBlock`) →
    Anthropic content (string or block list). Block dispatch is the
    shared `wire.encode_content`; only the image part shape and the
    empty fallback are ours."""
    return encode_content(
        content,
        image_part=anthropic_image_part,
        empty=_EMPTY_CONTENT,
        label="anthropic",
    )


def _assistant_blocks(msg: AssistantMessage) -> list[dict]:
    """`AssistantMessage` (text + tool_calls) → Anthropic content blocks."""
    if content := msg.vendor_extra.get("anthropic", {}).get("content"):
        # Preserve signed blocks and their order, including redacted thinking.
        # https://platform.claude.com/docs/en/build-with-claude/thinking
        return deepcopy(content)
    blocks: list[dict] = []
    if msg.content:
        blocks.append({"type": "text", "text": msg.content})
    for tc in msg.tool_calls:
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.id or f"auto_{uuid.uuid4().hex[:8]}",
                "name": tc.name,
                "input": tc.arguments,
            }
        )
    if not blocks:
        # Anthropic rejects empty assistant content arrays.
        blocks.append(_EMPTY_TEXT_BLOCK)
    return blocks


def _tool_to_anthropic(tool: dict) -> dict:
    """Local tool schema → Anthropic `tools=` entry. The shape happens to
    match our local format almost verbatim — `input_schema` is the same key."""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": tool.get("input_schema")
        or {"type": "object", "properties": {}},
    }


# ---------- response translation (Anthropic → AssistantMessage) ----------


def _from_anthropic_response(resp) -> AssistantMessage:
    """Anthropic `Message` (Pydantic model) → `AssistantMessage`.

    Public text and tool calls are normalized; signed thinking stays in
    vendor metadata so tool continuations can replay the original blocks.
    The full payload is also preserved on `.raw` for trace inspection."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    blocks = resp.content or []
    if not isinstance(blocks, list):
        raise ProviderTransientError("invalid response: content must be a list")
    for block in blocks:
        bt = getattr(block, "type", None)
        if not isinstance(bt, str) or not bt:
            raise ProviderTransientError("invalid response: malformed content block")
        if bt == "text":
            text = getattr(block, "text", "") or ""
            if not isinstance(text, str):
                raise ProviderTransientError("invalid response: text must be a string")
            text_parts.append(text)
        elif bt == "tool_use":
            call_id = getattr(block, "id", None)
            name = getattr(block, "name", None)
            arguments = getattr(block, "input", None)
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(name, str)
                or not name
                or not isinstance(arguments, dict)
            ):
                # Inventing an ID would break pairing with signed-content replay.
                raise ProviderTransientError("invalid response: malformed tool use")
            tool_calls.append(
                ToolCall(
                    id=call_id,
                    name=name,
                    arguments=arguments,
                )
            )

    stop_raw = getattr(resp, "stop_reason", None) or "end_turn"
    finish = _STOP_REASON_MAP.get(stop_raw, FinishReason.STOP)
    raw_dict = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
    content = raw_dict.get("content") or []
    vendor_extra = {}
    if any(b.get("type") in ("thinking", "redacted_thinking") for b in content):
        vendor_extra["anthropic"] = {"content": deepcopy(content)}

    return AssistantMessage(
        content="\n".join(text_parts),
        tool_calls=tool_calls,
        finish_reason=finish,
        usage=_parse_anthropic_usage(resp),
        response_id=str(getattr(resp, "id", "") or ""),
        response_model=str(getattr(resp, "model", "") or ""),
        raw=raw_dict,
        vendor_extra=vendor_extra,
    )


def _parse_anthropic_usage(resp) -> Usage:
    """Anthropic's `usage` block → normalized `Usage`.

    Anthropic reports the three input components disjointly:
      - `input_tokens`               — fresh, neither cached nor cache-creation
      - `cache_read_input_tokens`    — cache hit
      - `cache_creation_input_tokens` — written to cache (still billed as input)

    Total input is the SUM of all three. Our normalized `Usage` follows
    OpenAI semantics where `prompt_tokens` is the full input and the
    engine derives `new = total - cached - created`, so we sum them
    here. Earlier code took only `input_tokens`, which mis-displayed
    cold turns as "0.6k" when the real total was ~19k."""
    u = getattr(resp, "usage", None)
    if u is None:
        return Usage()
    fresh = token_count(getattr(u, "input_tokens", 0))
    cached = token_count(getattr(u, "cache_read_input_tokens", 0))
    created = token_count(getattr(u, "cache_creation_input_tokens", 0))
    return Usage(
        prompt_tokens=fresh + cached + created,
        completion_tokens=token_count(getattr(u, "output_tokens", 0)),
        cached_tokens=cached,
        cache_creation_tokens=created,
    )
