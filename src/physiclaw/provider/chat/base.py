"""Chat interface, history/cache behavior and token accounting."""

import logging
import os
import time
from typing import Any, ClassVar, Literal, Protocol

from physiclaw.contract.dto import (
    USAGE_CALL_TURN,
    AssistantMessage,
    CollapsePolicy,
    ImageBlock,
    Message,
    SystemMessage,
    Thinking,
    ToolResultMessage,
    Usage,
    UsageCall,
    usage_event,
)
from physiclaw.contract.plugin import EventSink
from physiclaw.provider.provider_base import BaseAPIProvider

log = logging.getLogger(__name__)


def token_count(value: Any) -> int:
    """An optional usage counter: accept integers and proxy-stringified integers.

    Malformed accounting must not discard an otherwise usable model reply.
    Booleans, fractions, negatives, and missing counts are unreported (zero).
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        return 0


class ChatProvider(Protocol):
    @property
    def api_kind(self) -> Literal["chat"]: ...

    model: str
    COLLAPSE: CollapsePolicy

    async def chat(
        self,
        history: list[Message],
        tools: list[dict],
        *,
        purpose: UsageCall = USAGE_CALL_TURN,
        thinking: Thinking | None = None,
    ) -> AssistantMessage: ...

    def serialize_history(self, history: list[Message]) -> list[dict]:
        """Convert engine DTOs into the wire-format messages this provider
        will send, with cache-control markers applied. Engine calls this
        for trace logging; `chat()` calls the same internally before
        POSTing — the two calls produce equivalent fresh wire so the log
        faithfully captures what hits the API."""
        ...

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
    """Wire-shape cache-marker mechanics — *how* a `cache_control`
    attaches to a wire entry. The `serialize_history` template owns
    *where* markers go (system entry at index 0, latest superseded
    tool_result, and the two moving tail anchors around the live
    screen); each wire shape ships one `CacheMarkers` subclass saying
    how its entries carry the marker, and a provider picks its factory
    once via the `CACHE_MARKERS` class attribute.

    All hooks return a marked shallow copy; caller-held dicts are
    never mutated. Defaults are identity, which makes the base the
    null object — a provider that must not mark declares
    `NO_CACHE_MARKERS` (e.g. Google, whose shim ignores the field and
    whose implicit cache the on/off wrapping would perturb) — and lets
    a shape override only the placements its cache semantics actually
    reward (Anthropic marks system on the top-level `system` payload
    field in `chat()`, not on a messages entry, and is the only shape
    that needs `mark_tail` — its cache stops at explicit breakpoints,
    while the OpenAI-shape vendors extend over stable prefixes on
    their own).
    """

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

    Subclasses (`OpenAICompatibleProvider`, `AnthropicCompatibleProvider`)
    layer wire-format and request/response flow on top.

    Subclass MUST set:
      - `PROVIDER_ID` — short id, also the registry key.
      - `BASE_URL` — endpoint, e.g. `"https://api.openai.com/v1"`.

    Subclass MAY set:
      - `API_KEY_ENV_VARS` — env vars to consult (in order, first hit
        wins). Defaults to `("<PROVIDER_ID>_API_KEY",)`. Override only
        when a vendor accepts more than one (e.g. Qwen takes both
        `QWEN_API_KEY` and `DASHSCOPE_API_KEY`).
      - `SYSTEM_PROMPT_FRAGMENT` — per-vendor reasoning-convention
        addendum for the system prompt (e.g. Qwen's `<think>...</think>`
        wrapper).

    Subclass MAY override:
      - `_build_client()` if the wire client isn't a stock
        `httpx.AsyncClient` (e.g. Anthropic's `AsyncAnthropic` SDK)
      - `_api_key()` if auth doesn't fit the env-var/config pattern
      - `_missing_key_message()` for a richer error string
      - `_model_env_var()` if the env override isn't `<ID>_MODEL`
      - `chat()` and `serialize_history()` — provided by the wire-shape
        intermediate base; vendors normally don't touch them

    `BASE_URL` is overridable per-instance via `~/.physiclaw/config.toml`'s
    `[providers.<id>] base_url = "..."` so users can point at proxies
    or alt endpoints without code changes.
    """

    api_kind: ClassVar[Literal["chat"]] = "chat"
    CONTENT_TYPE = "application/json"

    # Per-vendor system-prompt addendum; see `system_prompt_fragment()`.
    SYSTEM_PROMPT_FRAGMENT: str = ""

    # Whether the SYSTEM prompt carries the inline `## Tooling` index card.
    # Open-weight models (Qwen, Moonshot) routinely miss tools that appear
    # only in the native `tools=` schema, so the card is a redundant anchor
    # for them — default on. Vendors whose models reliably read the schema
    # set False and save the duplication (~700 tokens per request).
    INLINE_TOOL_INDEX: bool = True

    # Cache-marker factory for this provider's wire shape. Each
    # wire-shape base sets its own (`OpenAICacheMarkers` /
    # `AnthropicCacheMarkers`); a vendor whose endpoint ignores markers
    # resets to `NO_CACHE_MARKERS` — see `GoogleProvider`.
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
    # sessions — see `MoonshotProvider` for the trade-off rationale.
    COLLAPSE: CollapsePolicy = CollapsePolicy(first_at=30, keep=10, interval=20)

    def __init__(
        self,
        model: str | None = None,
        timeout: float = 120.0,
        base_url: str | None = None,
        usage_sink: EventSink | None = None,
    ):
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

    @classmethod
    def _model_env_var(cls) -> str:
        return f"{cls.PROVIDER_ID.upper()}_MODEL"

    @classmethod
    def system_prompt_fragment(cls) -> str:
        """Per-vendor system-prompt addendum (e.g. Qwen's `<think>...</think>`
        wrapper instructions). Default: empty. Declare
        `SYSTEM_PROMPT_FRAGMENT` on the vendor class when the model
        needs an explicit reasoning convention."""
        return cls.SYSTEM_PROMPT_FRAGMENT

    # ---------- DTO → wire (template method, shared across wire shapes) ----------

    def serialize_history(self, history: list[Message]) -> list[dict]:
        """Single-pass DTO history → provider wire-format messages, with
        cache markers attached (via the `CACHE_MARKERS` factory; the
        default `NO_CACHE_MARKERS` is identity) to:
          - the `SystemMessage` at index 0,
          - the latest `ToolResultMessage` flagged `is_superseded`, and
          - two moving tail anchors (both via `mark_tail`, deduped, so
            at most 4 breakpoints total): the last `ToolResultMessage`
            BEFORE the live screen (the deepest point that survives the
            next `drop_stale_screens` rewrite — an entry covering the
            live view dies unread the turn a new screen supersedes it),
            and the last `ToolResultMessage` overall (advances every
            turn, so note-only stretches read for pennies; it dies on
            the next supersede, which the pre-view anchor then backs
            up). Models order a turn's results differently ([note,
            view] vs [view, note]), so "before the live screen" is
            positional, not type-based. The volatile plan/scratchpad
            tails are UserMessages appended after all anchors, outside
            the cached prefix.

        Subclasses implement `_encode_message` (DTO → wire dict, list of
        wire dicts, or `None` to skip). Most encodings are 1:1; the list
        form is for vendor-specific splits — e.g. Google's shim rejects
        `image_url` parts in `role: tool`, so `GoogleProvider` splits a
        ToolResultMessage with images into [text-only tool, synthetic
        user with images]. Anthropic's `SystemMessage` returns `None`
        (system rides outside the messages array). Cache markers attach
        to the first entry of any split; superseded results don't carry
        images, so their list is always single-element."""
        markers = self.CACHE_MARKERS
        out: list[dict] = []
        last_stub_idx: int | None = None
        results: list[tuple[int, bool]] = []  # (out index, carries image)
        for i, msg in enumerate(history):
            entries = self._encode_message(msg)
            if entries is None:
                continue
            if isinstance(entries, dict):
                entries = [entries]
            if not entries:
                continue
            if i == 0 and isinstance(msg, SystemMessage):
                entries[0] = markers.mark_system(entries[0])
            elif isinstance(msg, ToolResultMessage):
                results.append((len(out), _has_image(msg.content)))
                if msg.is_superseded:
                    last_stub_idx = len(out)
            out.extend(entries)
        if last_stub_idx is not None:
            out[last_stub_idx] = markers.mark_stub(out[last_stub_idx])
        for idx in _tail_anchor_indexes(results):
            if idx != last_stub_idx:
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

    async def chat(
        self,
        history: list[Message],
        tools: list[dict],
        *,
        purpose: UsageCall = USAGE_CALL_TURN,
        thinking: Thinking | None = None,
    ) -> AssistantMessage:
        """One model call, whatever asks for it. THE door every call
        passes through — so the account of it (the `usage` event: model,
        purpose, elapsed time, every token bucket, the reply's id, or the
        error class when the call failed) is written here and cannot be
        skipped by a caller. `purpose` names who asked (the turn loop, a
        conductor decision, curation); `thinking` is the caller's word
        for how much hidden thinking it wants, None for the vendor's
        default (the vendor translates a level, see `thinking_params`);
        the wire-shape subclass does the request in `_chat`."""
        t0 = time.perf_counter()
        try:
            asst = await self._chat(history, tools, thinking=thinking)
        except Exception as e:
            self._account(purpose, t0, error=type(e).__name__)
            raise
        self._account(purpose, t0, asst)
        return asst

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
        thinking: Thinking | None = None,
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
