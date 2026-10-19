"""Register the decision slot before the LLM.

Context projection and candidate selection are not wired yet. The enabled
slot yields every turn and creates no model client or model context.
"""

from physiclaw.contract.dto import AssistantMessage, Message
from physiclaw.contract.plugin import PluginRegistration, SessionSetup, SetupContext


def build() -> PluginRegistration:
    return PluginRegistration(
        id="physiclaw.decision", name="Decision", create=DecisionPlugin
    )


class DecisionPlugin:
    async def session_setup(self, _ctx: SetupContext) -> SessionSetup | None:
        return None

    async def advance(self, _history: list[Message]) -> AssistantMessage | None:
        return None

    async def aclose(self) -> None:
        pass
