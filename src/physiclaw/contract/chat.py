"""Chat generation contracts, independent of providers and turn producers.

SessionChatProvider is borrowed: callers supply their own conversation and
schemas, and cannot close the host's client. ChatProvider adds owned lifetime
and model identity. Neither interface exposes wire encoding or agent policy.
"""

from collections.abc import Callable
from typing import Literal, Protocol

from physiclaw.contract.dto import (
    USAGE_CALL_TURN,
    AssistantMessage,
    Message,
    Thinking,
    UsageCall,
)
from physiclaw.contract.structured import OutputSpec, StructuredCompletion
from physiclaw.contract.turn import TurnMetadata


class SessionChatProvider(Protocol):
    """Stateless generation over caller-owned messages and output schemas.

    Providers choose the API representation; callers own context, retries and
    execution. `on_request` observes a detached native body for diagnostics,
    never to construct or alter a request; observer errors are logged without
    aborting generation. The host owns the client lifetime.
    """

    @property
    def structured_transport(self) -> Literal["tool", "native"]:
        """Adapter-selected transport, exposed for diagnostics only."""
        ...

    async def chat(
        self,
        history: list[Message],
        tools: list[dict],
        *,
        metadata: TurnMetadata | None = None,
        purpose: UsageCall = USAGE_CALL_TURN,
        thinking: Thinking | None = None,
        on_request: Callable[[dict], None] | None = None,
    ) -> AssistantMessage: ...

    async def generate_structured[T](
        self,
        history: list[Message],
        output_spec: OutputSpec[T],
        *,
        metadata: TurnMetadata | None = None,
        purpose: UsageCall = USAGE_CALL_TURN,
        thinking: Thinking | None = None,
        on_request: Callable[[dict], None] | None = None,
    ) -> StructuredCompletion[T]: ...

    def validate_output_spec(
        self, output_spec: OutputSpec, *, thinking: Thinking | None = None
    ) -> None: ...


class ChatProvider(SessionChatProvider, Protocol):
    @property
    def api_kind(self) -> Literal["chat"]: ...

    model: str

    async def aclose(self) -> None: ...


class ModelCatalog(Protocol):
    """Owned model-discovery client, independent of a selected chat model."""

    async def list_models(self) -> list[dict]: ...

    async def aclose(self) -> None: ...
