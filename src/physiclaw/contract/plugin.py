"""One turn-producer protocol for plugins and the default LLM.

Each turn, the engine asks plugins in registration order with one neutral,
immutable TurnInput and TurnMetadata. Each plugin builds its own context and
returns a ProposedTurn or None. The first accepted result wins.
This is arbitration: a plugin produces the turn or returns None to let
the engine ask the next slot. With no plugins at all (config
`[agent] plugins = "none"`), only the default LLM producer remains, which
is the rollback switch.

Each registration declares its display name and stable instance ID.
The engine snapshots that identity and assigns execution grants to the slot.
Qualified macros contributed at setup (`SessionSetup.gated_macros`) resolve
only for their registering plugin. Raw response metadata and call IDs
cannot grant authority or identify another plugin.

Plugins are loaded from dotted `module:factory` paths in
`[agent] plugins` (comma-separated). Config registration is deliberate:
we own every plugin today, so a config string the engine loads blindly
is the whole discovery story. The upgrade trigger to entry-point
discovery is a plugin shipping as a separately installed distribution —
do not build it before then.

The host supplies the same in-process services and runtime input to every
plugin. No plugin receives another producer's conversation or reasoning.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from uuid import uuid4

from physiclaw.contract.decision import ActionCandidate, decision_json
from physiclaw.contract.dto import (
    USAGE_CALL_TURN,
    AssistantMessage,
    FinishReason,
    Message,
    MicroRecord,
    Thinking,
    ToolCall,
    UsageCall,
)
from physiclaw.contract.turn import TurnInput, TurnMetadata
from physiclaw.macros.model import Macro


class SessionChatProvider(Protocol):
    """The stateless chat service a plugin may borrow —
    enough to fall back to the session model for scoped playbook calls
    and to wire-log its round-trips verbatim. Each producer owns its
    context and compaction; the host owns the transport lifetime.
    The full interface lives in `physiclaw.provider`; this protocol
    exists so the contract need not import it (provider imports
    contract, never the reverse)."""

    async def chat(
        self,
        history: list[Message],
        tools: list[dict],
        *,
        metadata: TurnMetadata | None = None,
        purpose: UsageCall = USAGE_CALL_TURN,
        thinking: Thinking | None = None,
    ) -> AssistantMessage: ...

    def serialize_history(
        self, history: list[Message], *, metadata: TurnMetadata | None = None
    ) -> list[dict]:
        """Encode a fresh request with metadata and provider cache markers.

        Chat calls and host wire logs use the same encoder so their requests
        match. Caller-owned history is never mutated.
        """
        ...


class EventSink(Protocol):
    """`Trace.write`-shaped: one structured event into events.jsonl."""

    def write(self, event: dict) -> None: ...


class WireSink(Protocol):
    """`RawLog.write_micro`-shaped: one scoped LLM round-trip, whole."""

    def write_micro(self, rec: MicroRecord) -> None: ...


@dataclass(frozen=True, slots=True)
class PluginServices:
    """Wake-time services shared by all plugins, without a model context.

    The chat provider accepts caller-owned messages and holds no conversation.
    Its lifecycle stays with the host; each plugin owns its own context.
    """

    session_provider: SessionChatProvider
    events: EventSink | None = None
    wire: WireSink | None = None


@dataclass(frozen=True, slots=True)
class SessionSetup:
    """A plugin's wake-time contribution. `gated_macros` are qualified
    `app/name` macros dispatchable ONLY while the registering plugin's turn is
    executing — they join the prompt bundle's macro surface but never
    its model-visible list."""

    gated_macros: dict[str, Macro] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TurnProducer:
    """Host-stamped identity; presentation and execution grants are separate."""

    plugin_id: str


@runtime_checkable
class TurnPlugin(Protocol):
    """One neutral invocation contract for every plugin."""

    async def session_setup(self, ctx: PluginServices) -> SessionSetup | None: ...

    async def advance(
        self, turn: TurnInput, *, metadata: TurnMetadata
    ) -> ProposedTurn | None: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProposedTurn:
    """Tool calls with optional observation/target evidence, checked by the host."""

    message: AssistantMessage
    observation_id: str | None = None
    targets: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.observation_id is not None and (
            not isinstance(self.observation_id, str) or not self.observation_id
        ):
            raise ValueError("a bound proposal must reference its observation")
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))

    @classmethod
    def from_action(cls, action: ActionCandidate, observation_id: str) -> ProposedTurn:
        """Bind one selected action to a fresh local tool-call ID."""
        if action.tool is None:
            raise ValueError("a proposal must contain an executable action")
        call_id = f"call_{uuid4().hex}"
        arguments = decision_json(action.arguments)
        assert isinstance(arguments, dict)
        return cls(
            message=AssistantMessage(
                content="",
                finish_reason=FinishReason.TOOL_CALLS,
                tool_calls=[ToolCall(call_id, action.tool, arguments)],
            ),
            observation_id=observation_id,
            targets={call_id: action.target_ref}
            if action.target_ref is not None
            else {},
        )


@dataclass(frozen=True, slots=True)
class PluginRegistration:
    """A named pipeline entry, separate from its implementation and order.

    IDs are explicit registry keys, such as ``physiclaw.playbook``.
    Validate identity before creating a plugin or granting it any authority.
    ``create`` returns a fresh session instance for this registration.
    """

    id: str
    name: str
    create: Callable[[], TurnPlugin]

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value or value != value.strip()
            for value in (self.name, self.id)
        ):
            raise ValueError("plugin name and id must be nonblank, trimmed strings")
        if not callable(self.create):
            raise TypeError("plugin registration must provide a factory")
