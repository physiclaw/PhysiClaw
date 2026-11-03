"""OpenAI-compatible base — `/chat/completions` wire format.

Vendors that speak this shape (Qwen/DashScope, OpenRouter, Moonshot,
DeepSeek, Google)
inherit from `OpenAICompatibleProvider` and declare `BASE_URL` plus
any auth quirks. This file owns:

  - the request/response flow (`chat`)
  - DTO → wire serialization (`_encode_message`, delegating block-
    level encoding to `wire.py`; the inherited `serialize_history`
    template assembles the list)
  - default cache-control mechanics (`OpenAICacheMarkers`; system + latest
    stubbed tool_result). Vendor adapters select their own protocol, including
    automatic caching or the separate explicit OpenAI cache markers.
  - response parsing (`_parse_response` + `_parse_usage` methods).
    `_parse_usage` tolerates the known shape drift among OpenAI-compat
    endpoints (top-level `cached_tokens` / `prompt_cache_hit_tokens`
    when the nested location is empty); a vendor whose `usage` shape
    differs beyond that overrides `_parse_usage` on its own class.

Default cache-control layout — two anchors (of the template's four),
chosen so cached bytes survive turns and wakes:

  1. System message (index 0). Session-stable; eligible cached prefixes can
     be reused across wakes while the provider's cache entry remains valid.
  2. Latest superseded screen-obs `tool_result` — found via the typed
     `is_superseded` flag on the source `ToolResultMessage` (set by
     `compact.drop_stale_screens`). Deepest byte-stable point before
     the live multipart image; marking through the image would cache
     bytes that mutate away. Because this anchor MOVES (a newer stub
     lands most screen turns), a stub's wire shape must not depend on
     whether it currently holds the marker: `_encode_message` encodes
     every superseded stub as a one-element text-block list, and
     `mark_stub` adds only the `cache_control` key.

Provider-specific response leakage (Qwen's `reasoning_content`) is
stripped here at parse time so it never re-rides the wire on the next
turn (would break the prefix cache).
"""

import json
import logging
import uuid
from copy import deepcopy
from typing import Any, assert_never

from physiclaw.common.provider_errors import (
    ProviderTransientError,
)
from physiclaw.contract.dto import (
    AssistantMessage,
    FinishReason,
    Message,
    OutputResultMessage,
    SystemMessage,
    Thinking,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from physiclaw.contract.structured import OutputSpec
from physiclaw.contract.turn import TurnMetadata
from physiclaw.provider.chat.base import (
    EPHEMERAL_CACHE_CONTROL,
    CacheMarkers,
    token_count,
)
from physiclaw.provider.chat.http import HTTPChatProvider
from physiclaw.provider.chat.structured import strict_json
from physiclaw.provider.chat.wire import (
    assistant_to_wire,
    output_result_to_message,
    tool_result_to_wire,
    tool_to_wire,
    user_content_to_openai,
)

log = logging.getLogger(__name__)


class OpenAICacheMarkers(CacheMarkers):
    """OpenAI-shape marker mechanics, split by anchor mobility.

    The system entry is marked on EVERY request, so `mark_system` may
    rewrap its string content into a cache-controlled text block — the
    wrapped shape is simply what every request carries. The stub anchor
    MOVES (a newer stub takes it most screen turns), so `mark_stub`
    must be additive: `_encode_message` guarantees a superseded stub is
    a one-element text-block list, and marking only adds the
    `cache_control` key to a copy of that block. Marked and unmarked
    stubs therefore serialize byte-identically apart from that key —
    the invariant vendors with strict anchor semantics (DashScope/Qwen)
    depend on, and the same contract `AnthropicCacheMarkers` keeps.
    Moonshot K3 is measured indifferent to both the key and the shape —
    see `vendors/moonshot.py`. The template's third hook, `mark_tail`,
    stays identity here: OpenAI-shape caches extend over stable
    prefixes on their own (Moonshot measured; markers add nothing),
    unlike Anthropic's strict-breakpoint cache which needs it."""

    def mark_system(self, entry: dict) -> dict:
        return _with_cache_marker(entry)

    def mark_stub(self, entry: dict) -> dict:
        block = {**entry["content"][0], "cache_control": EPHEMERAL_CACHE_CONTROL}
        return {**entry, "content": [block]}


class OpenAICompatibleProvider(HTTPChatProvider):
    """Base for providers that speak the OpenAI `/chat/completions` wire
    format. See `BaseChatProvider` for the auth declarations vendors are
    expected to set; this class plugs the wire-shape encoder
    (`_encode_message`) and the `OpenAICacheMarkers` factory into the
    inherited `serialize_history` template, and adds the HTTP request
    flow in `chat()`."""

    ENDPOINT = "/chat/completions"
    CACHE_MARKERS: CacheMarkers = OpenAICacheMarkers()
    # Opt in only where the vendor requires this field on tool continuations.
    REPLAY_REASONING_CONTENT = False

    def _request_params(self, *, metadata: TurnMetadata | None) -> dict[str, Any]:
        """Additional vendor request fields; empty for the standard protocol."""
        return {}

    def _output_tool(self, spec: OutputSpec) -> dict:
        return tool_to_wire(spec.tool())

    def _tool_params(
        self, spec: OutputSpec | None, *, thinking: Thinking | None
    ) -> dict:
        return {"tool_choice": "auto"}

    def build_request(
        self,
        history: list[Message],
        tools: list[dict],
        *,
        metadata: TurnMetadata | None = None,
        thinking: Thinking | None = None,
        output_spec: OutputSpec | None = None,
    ) -> dict:
        if output_spec is not None:
            self.validate_output_spec(output_spec, thinking=thinking)
            history = self._structured_history(history, output_spec)
        wire = self.serialize_history(history, metadata=metadata)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": wire,
            **self._request_params(metadata=metadata),
            # The vendor's spelling of a declared thinking level (nothing
            # for the vendor default, or a model the vendor's table omits).
            **(self.thinking_params(thinking) if thinking is not None else {}),
        }
        if output_spec is not None:
            payload["tools"] = [self._output_tool(output_spec)]
        elif tools:
            payload["tools"] = [tool_to_wire(t) for t in tools]
        if "tools" in payload:
            payload.update(self._tool_params(output_spec, thinking=thinking))
        if self.max_output_tokens is not None:
            payload["max_tokens"] = self.max_output_tokens

        return payload

    def _decode_response(self, raw: Any, *, structured: bool) -> AssistantMessage:
        try:
            reply = self._parse_response(raw)
        except ProviderTransientError as exc:
            data = raw if isinstance(raw, dict) else {"unparsed": raw}
            choices = data.get("choices")
            choices = choices if isinstance(choices, list) else []
            choice = choices[0] if choices and isinstance(choices[0], dict) else {}
            message = choice.get("message")
            reply = AssistantMessage(
                "",
                [],
                _normalize_finish(choice.get("finish_reason") or "stop"),
                refused=isinstance(message, dict) and bool(message.get("refusal")),
                usage=self._parse_usage(data),
                response_id=str(data.get("id") or ""),
                response_model=str(data.get("model") or ""),
                raw=data,
                decode_error=str(exc),
            )
            if (
                not structured
                or data.get("error")
                or any(
                    isinstance(c, dict) and c.get("finish_reason") == "error"
                    for c in choices
                )
            ):
                setattr(exc, "reply", reply)
                raise
        return reply

    # ---------- serialize_history hooks (called by BaseChatProvider) ----------

    # Return type stays as wide as `BaseChatProvider`'s contract so vendor
    # overrides (google.py splits one DTO into several wire dicts) conform.
    def _encode_message(self, msg: Message) -> dict | list[dict] | None:
        if isinstance(msg, OutputResultMessage):
            return self._encode_message(output_result_to_message(msg))
        if isinstance(msg, SystemMessage):
            return {"role": "system", "content": msg.content}
        if isinstance(msg, UserMessage):
            return {"role": "user", "content": user_content_to_openai(msg.content)}
        if isinstance(msg, AssistantMessage):
            entry = assistant_to_wire(msg)
            extra = msg.vendor_extra.get(self.PROVIDER_ID, {})
            if (
                self.REPLAY_REASONING_CONTENT
                and extra.get("model") == self.model
                and "reasoning_content" in extra
            ):
                entry["reasoning_content"] = deepcopy(extra["reasoning_content"])
            return entry
        if isinstance(msg, ToolResultMessage):
            wire = tool_result_to_wire(msg)
            # Superseded stubs always ride as a one-element text-block
            # list so their bytes don't depend on whether the stub holds
            # the moving cache anchor this turn (only the `cache_control`
            # key may differ — see `OpenAICacheMarkers`). Non-superseded
            # results keep the plain string/multipart shapes.
            if msg.is_superseded and isinstance(wire["content"], str):
                wire["content"] = [{"type": "text", "text": wire["content"]}]
            return wire
        # Exhaustive on the `Message` Union; runtime guard against future
        # subtypes added without updating the dispatch.
        assert_never(msg)

    # ---------- response parsing (vendor-overridable) ----------

    def _parse_response(self, raw: dict) -> AssistantMessage:
        """Parse a completion, retaining raw data for logs and diagnostics.

        Vendors that enable reasoning replay also retain provider/model-scoped
        metadata; it stays separate from assistant text. Vendors with different
        usage shapes override `_parse_usage`.
        """
        # A gateway's empty/malformed completion is not an assistant STOP.
        # Reject the whole response before any partial tool list can dispatch.
        if not isinstance(raw, dict):
            raise ProviderTransientError("invalid response: expected an object")
        if raw.get("error"):
            raise ProviderTransientError("provider generation failed")
        choices = raw.get("choices")
        if (
            not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], dict)
        ):
            raise ProviderTransientError("invalid response: missing completion choice")
        choice = choices[0]
        if choice.get("finish_reason") == "error":
            raise ProviderTransientError("provider generation failed")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ProviderTransientError("invalid response: missing assistant message")
        finish_raw = choice.get("finish_reason") or "stop"

        content = message.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)

        tool_calls: list[ToolCall] = []
        decode_error = (
            "expected exactly one completion choice" if len(choices) != 1 else None
        )
        calls = message.get("tool_calls")
        if calls is None:
            calls = []
        if not isinstance(calls, list):
            raise ProviderTransientError("invalid response: tool_calls must be a list")
        for tc in calls:
            if not isinstance(tc, dict) or not isinstance(tc.get("function"), dict):
                raise ProviderTransientError("invalid response: malformed tool call")
            fn = tc["function"]
            name = fn.get("name", "")
            ident = tc.get("id", "")
            name = "" if name is None else name
            ident = "" if ident is None else ident
            if not isinstance(name, str) or not isinstance(ident, str):
                raise ProviderTransientError(
                    "invalid response: tool name and id must be strings"
                )
            if not name:
                raise ProviderTransientError("invalid response: missing tool name")
            if ident and any(call.id == ident for call in tool_calls):
                raise ProviderTransientError("invalid response: duplicate tool call ID")
            if not ident:
                decode_error = "missing provider call ID"
            ident = ident or f"auto_{uuid.uuid4().hex[:8]}"
            args_str = fn.get("arguments", "{}")
            try:
                args = strict_json(args_str) if isinstance(args_str, str) else args_str
                if not isinstance(args, dict):
                    decode_error = "tool arguments must be an object"
                    args = {"_raw": args}
            except (ValueError, RecursionError):
                decode_error = "invalid JSON tool arguments"
                # Preserve malformed arguments for the dispatch validator.
                args = {"_malformed_json": args_str}
            tool_calls.append(ToolCall(id=ident, name=name, arguments=args))

        reply = AssistantMessage(
            content=content,
            tool_calls=tool_calls,
            finish_reason=_normalize_finish(finish_raw),
            usage=self._parse_usage(raw),
            response_id=str(raw.get("id") or ""),
            response_model=str(raw.get("model") or ""),
            raw=raw,
            refused=bool(message.get("refusal")),
            decode_error=decode_error,
        )
        if (
            not (reply.content or reply.tool_calls or reply.refused)
            and reply.finish_reason == FinishReason.STOP
        ):
            raise ProviderTransientError("invalid response: missing assistant output")
        if self.REPLAY_REASONING_CONTENT and "reasoning_content" in message:
            reply.vendor_extra[self.PROVIDER_ID] = {
                "model": self.model,
                "reasoning_content": deepcopy(message["reasoning_content"]),
            }
        return reply

    def _parse_usage(self, raw: dict) -> Usage:
        """OpenAI-standard `usage` block → normalized `Usage`. Cache
        stats live under `prompt_tokens_details.cached_tokens` and
        `prompt_tokens_details.cache_write_tokens` (some compatible providers
        call it `cache_creation_input_tokens`); when the
        nested location is empty, two top-of-block spellings are also
        probed — `cached_tokens` (Moonshot K2 sometimes surfaces it
        there) and `prompt_cache_hit_tokens` (DeepSeek's documented
        spelling). Both probes are no-ops for vendors that never emit
        them (standard OpenAI shapes carry no top-level key). Vendors
        whose shape differs beyond that override this method on the
        vendor class."""
        u = (raw or {}).get("usage")
        if not isinstance(u, dict):
            return Usage()
        details = u.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        completion = u.get("completion_tokens_details")
        completion = completion if isinstance(completion, dict) else {}
        cached = token_count(details.get("cached_tokens"))
        if not cached:
            cached = token_count(u.get("cached_tokens")) or token_count(
                u.get("prompt_cache_hit_tokens")
            )
        return Usage(
            prompt_tokens=token_count(u.get("prompt_tokens")),
            completion_tokens=token_count(u.get("completion_tokens")),
            cached_tokens=cached,
            cache_creation_tokens=token_count(
                details.get(
                    "cache_write_tokens", details.get("cache_creation_input_tokens")
                )
            ),
            reasoning_tokens=token_count(completion.get("reasoning_tokens")),
        )


# ---------- cache marker helper ----------


def _with_cache_marker(entry: dict) -> dict:
    """Shallow-copy `entry` and wrap its string content in a single text
    block carrying an ephemeral cache_control. Only for the always-
    marked system entry: the string→list rewrap changes shape between
    marked and unmarked serializations, which a moving anchor must never
    do (see `OpenAICacheMarkers.mark_stub`, which is additive instead)."""
    out = dict(entry)
    out["content"] = [
        {
            "type": "text",
            "text": entry["content"],
            "cache_control": EPHEMERAL_CACHE_CONTROL,
        }
    ]
    return out


# ---------- finish-reason normalization ----------


def _normalize_finish(r: str) -> FinishReason:
    # OpenAI surfaces: stop, length, tool_calls, content_filter, function_call.
    if r == "function_call":
        return FinishReason.TOOL_CALLS
    try:
        return FinishReason(r)
    except ValueError:
        log.info("unknown finish_reason %r — treating as stop", r)
        return FinishReason.STOP
