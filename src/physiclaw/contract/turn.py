"""Immutable runtime input and metadata shared by all turn producers."""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from physiclaw.contract.decision import DecisionValue, freeze_mapping
from physiclaw.contract.observation import Observation


@dataclass(frozen=True, slots=True)
class TurnMetadata:
    """Host-owned metadata shared by every producer asked in one iteration.

    ``turn`` starts at zero. Declines and transport retries keep the same
    metadata. ``deadline`` uses the host's monotonic clock; remaining turns
    include the current iteration. No producer identity or task intent.
    """

    session_id: str
    turn: int
    deadline: float | None
    turns_remaining: int

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("turn metadata requires a session ID")
        if type(self.turn) is not int or self.turn < 0:
            raise ValueError("turn index must be a nonnegative integer")
        if type(self.turns_remaining) is not int or self.turns_remaining < 1:
            raise ValueError("remaining turns must be a positive integer")
        if self.deadline is not None and (
            type(self.deadline) not in (int, float) or not math.isfinite(self.deadline)
        ):
            raise ValueError("turn deadline must be finite or unknown")


@dataclass(frozen=True, slots=True)
class ActionEvent:
    """An execution receipt without another producer's private context.

    ``executed`` distinguishes a refusal from a tool that ran. Context-writing
    calls retain only a redacted acknowledgement. ``result_blocks`` preserves
    tool output; only the latest image is retained by the runtime journal.
    ``notices`` are host diagnostics, which each producer may project itself.
    ``is_error`` describes the tool result, not goal completion. The actual
    action text preserves partial-sequence failures even on a normal reply.
    """

    sequence: int
    turn: int
    call_id: str
    tool: str
    arguments: Mapping[str, DecisionValue]
    is_error: bool
    changed: bool | None
    observation_id: str | None
    result_text: str | None
    result_blocks: tuple[Mapping[str, DecisionValue], ...] = ()
    executed: bool = True
    notices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", freeze_mapping(self.arguments))
        object.__setattr__(
            self,
            "result_blocks",
            tuple(freeze_mapping(block) for block in self.result_blocks),
        )
        object.__setattr__(self, "notices", tuple(self.notices))
        if self.result_text is not None and not isinstance(self.result_text, str):
            raise ValueError("action result must be text or unknown")


@dataclass(frozen=True, slots=True)
class TurnInput:
    """Factual context; invocation metadata is supplied separately."""

    observation: Observation | None
    observation_issue: str | None
    events: tuple[ActionEvent, ...] = ()
    tools: Mapping[str, DecisionValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))
        if any(not isinstance(event, ActionEvent) for event in self.events):
            raise ValueError("turn events must be immutable ActionEvent instances")
        if self.observation is not None and not isinstance(
            self.observation, Observation
        ):
            raise ValueError("turn observation must be an immutable Observation")
        object.__setattr__(self, "tools", freeze_mapping(self.tools))
