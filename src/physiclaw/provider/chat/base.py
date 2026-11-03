"""Chat interface, history/cache behavior and token accounting."""

import asyncio
import logging
import os
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from typing import Any, ClassVar, Literal, Protocol

from physiclaw.common.provider_errors import ProviderConfigurationError
from physiclaw.contract.content import turn_metadata_message
from physiclaw.contract.dto import (
    USAGE_CALL_TURN,
    AssistantMessage,
    CollapsePolicy,
    ImageBlock,
    Message,
    OutputResultMessage,
    SystemMessage,
    Thinking,
    ToolResultMessage,
    Usage,
    UsageCall,
    usage_event,
)
from physiclaw.contract.plugin import EventSink, SessionChatProvider
from physiclaw.contract.structured import OutputSpec, StructuredCompletion
from physiclaw.contract.turn import TurnMetadata
from physiclaw.provider.chat.structured import decode_completion
from physiclaw.provider.provider_base import BaseAPIProvider

log = logging.getLogger(__name__)


def token_count_or_none(value: Any) -> int | None:
    """Parse a reported counter without confusing invalid values with zero."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        count = int(value)
    except ValueError:
        return None
    return count if count >= 0 else None


def token_count(value: Any) -> int:
    """An optional usage counter: accept integers and proxy-stringified integers.

    Malformed accounting must not discard an otherwise usable model reply.
    Booleans, fractions, negatives, and missing counts are unreported (zero).
    """
    return token_count_or_none(value) or 0


class ChatProvider(SessionChatProvider, Protocol):
    @property
    def api_kind(self) -> Literal["chat"]: ...

    model: str
    COLLAPSE: CollapsePolicy

    async def aclose(self) -> None: ...


# ---------- shared constants ----------

# The three levels as token budgets, for vendors that bound thinking by
# tokens (Anthropic, Qwen) — one table so "medium" means one thing.
THINKING_BUDGETS: dict[str, int] = {"low": 1024, "medium": 4096, "high": 16384}


# 5-minute TTL prefix-cache marker. DashScope, Moonshot K2, and
# Anthropic all accept `{type: "ephemeral"}` in their respective
# surrounding shapes. Treat as immutable — never mutate.
EPHEMERAL_CACHE_CONTROL = {"type": "ephemeral"}


# ---------- cache-marker factory ----------


class CacheMarkers:
    """Provider-owned marker shapes and retained stub count.

    History traversal supplies the system, superseded results and tail
    anchors. Providers mark only the placements their cache rewards.
    Hooks return shallow copies without mutating caller-owned entries;
    identity defaults disable marking for automatic-cache providers.
    """

    stub_count: int = 1

    def mark_system(self, entry: dict) -> dict:
        return entry

    def mark_stub(self, entry: dict) -> dict:
        return entry

    def mark_tail(self, entry: dict) -> dict:
        return entry


# The null object — identity on all hooks. Declaring this (rather
# than a `None` sentinel) keeps `serialize_history` branch-free.
NO_CACHE_MARKERS = CacheMarkers()


def _has_image(content) -> bool:
    """True iff a tool result's DTO content carries an `ImageBlock` —
    i.e. it is (or was, until superseded) a live screen capture."""
    return isinstance(content, list) and any(isinstance(b, ImageBlock) for b in content)


def _tail_anchor_indexes(results: list[tuple[int, bool]]) -> list[int]:
    """The moving tail anchors for one request, from `serialize_history`'s
    (out-index, carries-image) record of every tool result: the last
    result BEFORE the live screen (survives the next supersede rewrite)
    and the last result overall (advances every turn, dies on the next
    supersede). Deduped; one entry when they coincide or when no live
    screen exists."""
    anchors: list[int] = []
    images = [i for i, has in results if has]
    if images:
        before = [i for i, _ in results if i < images[-1]]
        if before:
            anchors.append(before[-1])
    if results:
        anchors.append(results[-1][0])
    return list(dict.fromkeys(anchors))


class BaseChatProvider(BaseAPIProvider):
    """Chat history, cache behavior and usage accounting over the shared client.

    HTTP and SDK subclasses add native request/response handling. Public
    chat and structured-generation entry points share accounting here.

    Subclass MUST set:
      - `PROVIDER_ID` — short id, also the registry key.
      - `BASE_URL` — endpoint, e.g. `"https://api.openai.com/v1"`.

    Subclass MAY set:
      - `API_KEY_ENV_VARS` — env vars to consult (in order, first hit
        wins). Defaults to `("<PROVIDER_ID>_API_KEY",)`. Override only
        when a vendor accepts more than one (e.g. Qwen takes both
        `QWEN_API_KEY` and `DASHSCOPE_API_KEY`).

    Subclass MAY override:
      - `_build_client()` if the wire client isn't a stock
        `httpx.AsyncClient` (e.g. Anthropic's `AsyncAnthropic` SDK)
      - `_api_key()` if auth doesn't fit the env-var/config pattern
      - `_missing_key_message()` for a richer error string
      - `_model_env_var()` if the env override isn't `<ID>_MODEL`
      - `build_request()`, `_chat()` and `_encode_message()` for native wire
        handling; public call entry points and history traversal stay here

    `BASE_URL` is overridable per-instance via `~/.physiclaw/config.toml`'s
    `[providers.<id>] base_url = "..."` so users can point at proxies
    or alt endpoints without code changes.
    """

    api_kind: ClassVar[Literal["chat"]] = "chat"
    CONTENT_TYPE = "application/json"

    # Whether the SYSTEM prompt carries the inline `## Tooling` index card.
    # Open-weight models (Qwen, Moonshot) routinely miss tools that appear
    # only in the output schema, so the card is a redundant anchor
    # for them — default on. Vendors whose models reliably read the schema
    # set False and save the duplication (~700 tokens per request).
    INLINE_TOOL_INDEX: bool = True

    # Cache-marker factory for this provider's wire shape. Each
    # wire-shape base sets its own (`OpenAICacheMarkers` /
    # `AnthropicCacheMarkers`); a vendor whose endpoint ignores markers
    # resets to `NO_CACHE_MARKERS` — see `GoogleChatProvider`.
    CACHE_MARKERS: CacheMarkers = NO_CACHE_MARKERS

    # Turn-age summary collapse — see `compact.collapse_old_turns`.
    # The policy lives here so vendor-specific tuning (cache mechanics
    # differ per provider) can override it in one place; a vendor
    # declares its own `COLLAPSE = CollapsePolicy(...)`, which validates
    # the triple at import time. The defaults (F=30, K=10, I=20) are an
    # EOQ optimum for vendors with anchored caches (Anthropic/Qwen) and
    # match every shipping provider; Moonshot carries the highest
    # cost-per-collapse (whole-prefix invalidation vs anchored caches)
    # but accepts the tax in exchange for tighter prompts on long
    # sessions — see `MoonshotChatProvider` for the trade-off rationale.
    COLLAPSE: CollapsePolicy = CollapsePolicy(first_at=30, keep=10, interval=20)

    def __init__(
        self,
        model: str | None = None,
        timeout: float = 120.0,
        base_url: str | None = None,
        usage_sink: EventSink | None = None,
        max_output_tokens: int | None = None,
    ):
        if max_output_tokens is not None and (
            type(max_output_tokens) is not int or max_output_tokens < 1
        ):
            raise ProviderConfigurationError(
                "max_output_tokens must be a positive integer"
            )
        self.max_output_tokens = max_output_tokens
        # Model resolution: explicit arg → env override → empty (chat()
        # will surface a clear API error if invoked without a model).
        super().__init__(
            model=model or os.environ.get(self._model_env_var()) or "",
            timeout=timeout,
            base_url=base_url,
        )
        # Every call's account (a `usage` event per call) goes here — the
        # session trace, handed in at construction so no owner can forget
        # to attach it. None (a one-off CLI probe, a rehearsal) reports
        # nothing.
        self.usage_sink = usage_sink
        self._structured_transport = self._select_output_transport()
        self.CACHE_MARKERS = self._select_cache_markers()

    def _select_cache_markers(self) -> CacheMarkers:
        """Select this adapter/model's cache protocol once."""
        return self.CACHE_MARKERS

    def _select_output_transport(self) -> Literal["tool", "native"]:
        """Select once for this adapter/model; never retry with another format."""
        return "tool"

    @property
    def structured_transport(self) -> Literal["tool", "native"]:
        return self._structured_transport

    def _structured_history(
        self, history: list[Message], spec: OutputSpec
    ) -> list[Message]:
        instruction = (
            f"Return one {spec.name} JSON object matching the response schema."
            if self.structured_transport == "native"
            else f"Submit one {spec.name} through the {spec.function_name} function."
        ) + " Do not include standalone prose."
        result = list(history)
        for i, message in enumerate(result):
            if isinstance(message, SystemMessage):
                result[i] = replace(
                    message, content=message.content + "\n\n" + instruction
                )
                break
        else:
            result.insert(0, SystemMessage(instruction))
        return result

    @classmethod
    def _model_env_var(cls) -> str:
        return f"{cls.PROVIDER_ID.upper()}_MODEL"

    # ---------- DTO → wire (template method, shared across wire shapes) ----------

    def serialize_history(
        self, history: list[Message], *, metadata: TurnMetadata | None = None
    ) -> list[dict]:
        """Serialize history and apply the provider's cache policy.

        Candidates are the leading system message, latest `stub_count`
        superseded results, and tail anchors around the live screen.
        Provider hooks choose which candidates to mark; volatile user
        tails and host metadata follow the anchors.

        Encoders may return one entry, several entries, or None to skip
        a message. Split results keep all receipts ahead of their image
        followups so parallel calls stay paired. Markers attach to the
        first entry; superseded results contain no images.
        """
        if metadata is not None:
            # A fresh tail keeps producer metadata visible to the model without
            # mutating its conversation or importing another producer's context.
            history = [*history, turn_metadata_message(metadata)]
        markers = self.CACHE_MARKERS
        out: list[dict] = []
        tool_followups: list[dict] = []
        stub_indexes: list[int] = []
        results: list[tuple[int, bool]] = []  # (out index, carries image)
        for i, msg in enumerate(history):
            is_result = isinstance(msg, (ToolResultMessage, OutputResultMessage))
            if not is_result and tool_followups:
                out.extend(tool_followups)
                tool_followups.clear()
            entries = self._encode_message(msg)
            if entries is None:
                continue
            if isinstance(entries, dict):
                entries = [entries]
            if not entries:
                continue
            if i == 0 and isinstance(msg, SystemMessage):
                entries[0] = markers.mark_system(entries[0])
            elif isinstance(msg, (ToolResultMessage, OutputResultMessage)):
                results.append((len(out), _has_image(msg.content)))
                if msg.is_superseded:
                    stub_indexes.append(len(out))
                tool_followups.extend(entries[1:])
                entries = entries[:1]
            out.extend(entries)
        out.extend(tool_followups)
        marked_stubs = stub_indexes[-markers.stub_count :]
        for idx in marked_stubs:
            out[idx] = markers.mark_stub(out[idx])
        for idx in _tail_anchor_indexes(results):
            if idx not in marked_stubs:
                out[idx] = markers.mark_tail(out[idx])
        return out

    def _encode_message(self, msg: Message) -> dict | list[dict] | None:
        """Encode one DTO into a provider wire dict (or list of dicts
        for vendor splits, or `None` to skip). MUST be implemented by
        wire-shape subclasses."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement _encode_message"
        )

    # ---------- request flow ----------

    @staticmethod
    def _notify_request(
        payload: dict, on_request: Callable[[dict], None] | None
    ) -> None:
        """Diagnostics cannot mutate or prevent the request; cancellation propagates."""
        if on_request is not None:
            try:
                on_request(deepcopy(payload))
            except Exception:
                log.warning("provider request log failed", exc_info=True)

    async def chat(
        self,
        history: list[Message],
        tools: list[dict],
        *,
        metadata: TurnMetadata | None = None,
        purpose: UsageCall = USAGE_CALL_TURN,
        thinking: Thinking | None = None,
        on_request: Callable[[dict], None] | None = None,
    ) -> AssistantMessage:
        """Ordinary chat with usage accounting, also shared by structured calls.

        `purpose` identifies the caller; `thinking` requests a level of hidden
        reasoning, or None for the vendor default. The adapter translates
        those options and sends the request in `_chat`.

        Engine turns always supply metadata. Calls outside the turn loop,
        such as doctor probes and post-session curation, have none.
        """
        t0 = time.perf_counter()
        try:
            asst = await self._chat(
                history,
                tools,
                metadata=metadata,
                thinking=thinking,
                on_request=on_request,
            )
        except (Exception, asyncio.CancelledError) as e:
            self._account(
                purpose, t0, getattr(e, "reply", None), error=type(e).__name__
            )
            raise
        self._account(purpose, t0, asst)
        return asst

    def validate_output_spec(
        self, output_spec: OutputSpec, *, thinking: Thinking | None = None
    ) -> None:
        """Fail before HTTP when the selected adapter cannot represent this schema."""

    def build_request(
        self,
        history: list[Message],
        tools: list[dict],
        *,
        metadata: TurnMetadata | None = None,
        thinking: Thinking | None = None,
        output_spec: OutputSpec | None = None,
    ) -> dict:
        raise NotImplementedError("provider must implement request building")

    async def generate_structured[T](
        self,
        history: list[Message],
        output_spec: OutputSpec[T],
        *,
        metadata: TurnMetadata | None = None,
        purpose: UsageCall = USAGE_CALL_TURN,
        thinking: Thinking | None = None,
        on_request: Callable[[dict], None] | None = None,
    ) -> StructuredCompletion[T]:
        self.validate_output_spec(output_spec, thinking=thinking)
        started = time.perf_counter()
        try:
            reply = await self._chat(
                history,
                [],
                metadata=metadata,
                thinking=thinking,
                output_spec=output_spec,
                on_request=on_request,
            )
        except (Exception, asyncio.CancelledError) as exc:
            self._account(
                purpose, started, getattr(exc, "reply", None), error=type(exc).__name__
            )
            raise
        result = decode_completion(
            reply, output_spec, transport=self.structured_transport
        )
        self._account(
            purpose,
            started,
            reply,
            error=result.failure.kind if result.failure else None,
        )
        return result

    def _account(
        self,
        purpose: UsageCall,
        t0: float,
        asst: AssistantMessage | None = None,
        *,
        error: str | None = None,
    ) -> None:
        """Attempt a usage event for every answered or failed call.

        A missing sink means no recording; a broken sink is logged.
        Zero tokens mean unreported usage, not an absent call.
        """
        if self.usage_sink is None:
            return
        try:
            self.usage_sink.write(
                usage_event(
                    asst.usage if asst is not None else Usage(),
                    call=purpose,
                    model=self.ref,
                    elapsed_ms=int((time.perf_counter() - t0) * 1000),
                    response_id=asst.response_id if asst is not None else "",
                    response_model=asst.response_model if asst is not None else "",
                    error=error,
                )
            )
        except Exception:
            # Preserve either the paid-for reply or the original provider error.
            log.warning(
                "provider usage event write failed",
                exc_info=True,
                extra={"console_message": "Token usage could not be recorded"},
            )

    async def _chat(
        self,
        history: list[Message],
        tools: list[dict],
        *,
        metadata: TurnMetadata | None = None,
        thinking: Thinking | None = None,
        output_spec: OutputSpec | None = None,
        on_request: Callable[[dict], None] | None = None,
    ) -> AssistantMessage:
        raise NotImplementedError(
            f"{type(self).__name__} must inherit from a wire-shape base "
            "(OpenAICompatibleProvider or AnthropicCompatibleProvider)"
        )

    def thinking_params(self, thinking: Thinking) -> dict[str, Any]:
        """This vendor's request fields for a declared thinking level,
        merged into the request body by the wire-shape base. Empty by
        default; a model outside a vendor's table also gets nothing,
        never a guessed field the endpoint may reject."""
        return {}

    async def list_models(self) -> list[dict]:
        """Live model list from the provider's `/v1/models` endpoint.

        Each entry is a dict with at least an `id` field; vendors may
        include `display_name`, `created_at`, `owned_by`, etc. — caller
        normalizes for display. Implemented by the wire-shape bases;
        BaseChatProvider raises so a missing implementation surfaces clearly."""
        raise NotImplementedError(
            f"{type(self).__name__} must inherit from a wire-shape base"
        )
