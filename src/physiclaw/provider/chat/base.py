"""Chat attempt orchestration, structured decoding and usage accounting."""

import asyncio
import logging
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from typing import Any, ClassVar, Literal

from physiclaw.common.provider_errors import ProviderConfigurationError
from physiclaw.contract.dto import (
    USAGE_CALL_TURN,
    AssistantMessage,
    Message,
    SystemMessage,
    Thinking,
    Usage,
    UsageCall,
    usage_event,
)
from physiclaw.contract.plugin import EventSink
from physiclaw.contract.structured import OutputSpec, StructuredCompletion
from physiclaw.contract.turn import TurnMetadata
from physiclaw.provider.chat.history import (
    NO_CACHE_MARKERS,
    CacheMarkers,
    serialize_history,
)
from physiclaw.provider.chat.structured import decode_completion
from physiclaw.provider.client.base import BaseAPIProvider
from physiclaw.provider.client.configuration import resolve_chat_model

log = logging.getLogger(__name__)


# ---------- shared constants ----------

# The three levels as token budgets, for vendors that bound thinking by
# tokens (Anthropic, Qwen) — one table so "medium" means one thing.
THINKING_BUDGETS: dict[str, int] = {"low": 1024, "medium": 4096, "high": 16384}


class BaseChatProvider(BaseAPIProvider):
    """One chat attempt over an owned transport, with shared usage accounting.

    Protocol adapters implement `build_request()`, `_chat()` and
    `_encode_message()`. Pure history traversal lives in `chat.history`;
    adapters supply native cache markers. Credential resolution and client
    lifetime come from BaseAPIProvider. Callers own context and retry policy.
    """

    api_kind: ClassVar[Literal["chat"]] = "chat"
    CONTENT_TYPE = "application/json"

    # Cache-marker factory for this provider's wire shape. Each
    # wire-shape base sets its own (`OpenAICacheMarkers` /
    # `AnthropicCacheMarkers`); a vendor whose endpoint ignores markers
    # resets to `NO_CACHE_MARKERS` — see `GoogleChatProvider`.
    CACHE_MARKERS: CacheMarkers = NO_CACHE_MARKERS

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
            model=resolve_chat_model(self.PROVIDER_ID, model),
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

    # ---------- DTO → wire (template method, shared across wire shapes) ----------

    def serialize_history(
        self, history: list[Message], *, metadata: TurnMetadata | None = None
    ) -> list[dict]:
        return serialize_history(
            history,
            encode_message=self._encode_message,
            markers=self.CACHE_MARKERS,
            metadata=metadata,
        )

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
        raise NotImplementedError(f"{type(self).__name__} must implement _chat")

    def thinking_params(self, thinking: Thinking) -> dict[str, Any]:
        """This vendor's request fields for a declared thinking level,
        merged into the request body by the wire-shape base. Empty by
        default; a model outside a vendor's table also gets nothing,
        never a guessed field the endpoint may reject."""
        return {}

    async def list_models(self) -> list[dict]:
        """Fetch native model entries, each with at least an `id` field.

        Adapters implement discovery; CLI callers use make_chat_catalog()
        and normalize optional display fields independently of generation.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement list_models")
