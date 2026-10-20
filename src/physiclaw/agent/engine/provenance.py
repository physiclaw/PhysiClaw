"""Host-owned producer identities and tool permissions."""

from dataclasses import dataclass
from typing import Literal

from physiclaw.contract.dto import AssistantMessage
from physiclaw.contract.plugin import TurnPlugin, TurnProducer


@dataclass(frozen=True, slots=True)
class PluginBinding:
    """One pipeline slot. Display metadata grants no execution exemptions."""

    id: str
    plugin: TurnPlugin
    name: str
    private_macros: frozenset[str] = frozenset()
    presentation: Literal["model", "plugin", "playbook"] = "plugin"


@dataclass(frozen=True, slots=True)
class ProducedTurn:
    message: AssistantMessage
    binding: PluginBinding
    elapsed_ms: int = 0

    @property
    def producer(self) -> TurnProducer:
        return TurnProducer(plugin_id=self.binding.id)

    @property
    def presentation(self) -> Literal["model", "plugin", "playbook"]:
        return self.binding.presentation
