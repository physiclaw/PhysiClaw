"""Register the decision slot before the LLM.

The host supplies a factual turn snapshot. Model-context projection and
candidate selection are not wired yet: the enabled slot yields every turn
and creates no model client or model context.
"""

from physiclaw.contract.plugin import (
    PluginRegistration,
    PluginServices,
    ProposedTurn,
    SessionSetup,
)
from physiclaw.contract.turn import TurnInput, TurnMetadata


def build() -> PluginRegistration:
    return PluginRegistration(
        id="physiclaw.decision",
        name="Decision",
        create=DecisionPlugin,
    )


class DecisionPlugin:
    async def session_setup(self, _ctx: PluginServices) -> SessionSetup | None:
        return None

    async def advance(
        self, _turn: TurnInput, *, metadata: TurnMetadata
    ) -> ProposedTurn | None:
        return None

    async def aclose(self) -> None:
        pass
