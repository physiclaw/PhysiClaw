"""Generic structured generation contracts, independent of agent actions."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from physiclaw.contract.decision import DecisionValue, decision_json, freeze_mapping
from physiclaw.contract.dto import AssistantMessage

type StructuredFailureKind = Literal["format", "refusal", "truncation"]


@dataclass(frozen=True, slots=True)
class OutputSpec[T]:
    """Immutable schema and pure decoder, supplied by the consuming producer."""

    name: str
    schema: Mapping[str, DecisionValue]
    decoder: Callable[[object], T]
    function_name: str = "submit_turn"

    def __post_init__(self) -> None:
        if not self.name or not self.function_name:
            raise ValueError("output schema and function names must be nonempty")
        object.__setattr__(self, "schema", freeze_mapping(self.schema))

    def tool(self) -> dict:
        """Fresh generic tool schema; never exposes mutable specification state."""
        return {
            "name": self.function_name,
            "description": f"Submit one complete {self.name} matching the schema.",
            "input_schema": decision_json(self.schema),
        }


@dataclass(frozen=True, slots=True)
class StructuredFailure:
    kind: StructuredFailureKind
    message: str


@dataclass(frozen=True, slots=True)
class StructuredCompletion[T]:
    """Every completed generation retains its original reply, even on rejection."""

    reply: AssistantMessage
    value: T | None = None
    failure: StructuredFailure | None = None

    def __post_init__(self) -> None:
        if (self.value is None) == (self.failure is None):
            raise ValueError("completion requires exactly one value or failure")
