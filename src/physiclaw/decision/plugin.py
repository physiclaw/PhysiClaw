"""Register the decision slot before the LLM.

The host supplies the same neutral input as every producer receives. The
plugin projects progress and screen evidence, then prepares guarded action
choices for inspection. It still yields without model calls or execution.
"""

from physiclaw.contract.plugin import (
    EventSink,
    PluginRegistration,
    PluginServices,
    ProposedTurn,
    SessionSetup,
)
from physiclaw.contract.turn import TurnInput, TurnMetadata
from physiclaw.decision.context import project


def build() -> PluginRegistration:
    return PluginRegistration(
        id="physiclaw.decision",
        name="Decision",
        create=DecisionPlugin,
    )


class DecisionPlugin:
    def __init__(self) -> None:
        self._events: EventSink | None = None

    async def session_setup(self, ctx: PluginServices) -> SessionSetup | None:
        self._events = ctx.events
        return None

    async def advance(
        self, turn: TurnInput, *, metadata: TurnMetadata
    ) -> ProposedTurn | None:
        if self._events is not None:
            self._events.write(
                {
                    "event": "decision_projection",
                    "turn": metadata.turn,
                    "projection": project(turn, metadata=metadata).to_dict(),
                }
            )
        return None

    async def aclose(self) -> None:
        pass
