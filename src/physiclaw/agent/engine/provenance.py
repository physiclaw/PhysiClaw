"""Host-owned plugin bindings and arbitration results.

Only the selected binding grants private macros or an exemption from
model-discipline guards. Model output carries no execution authority.
"""

from dataclasses import dataclass
from typing import Literal

from physiclaw.contract.dto import AssistantMessage
from physiclaw.contract.plugin import TurnPlugin, TurnProducer


@dataclass(frozen=True, slots=True)
class PluginBinding:
    """One configured slot, with authority assigned by the host."""

    id: str
    plugin: TurnPlugin
    name: str
    private_macros: frozenset[str] = frozenset()
    skip_model_guards: bool = False
    playbook: bool = False


@dataclass(frozen=True, slots=True)
class ProducedTurn:
    message: AssistantMessage
    binding: PluginBinding | None = None
    # Time spent with this producer, excluding earlier plugin declines.
    elapsed_ms: int = 0

    @property
    def producer(self) -> TurnProducer:
        return (
            TurnProducer("plugin", self.binding.id)
            if self.binding is not None
            else TurnProducer()
        )

    @property
    def presentation(self) -> Literal["model", "plugin", "playbook"]:
        if self.binding is None:
            return "model"
        return "playbook" if self.binding.playbook else "plugin"
