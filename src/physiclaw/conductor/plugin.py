"""Build a private playbook context from the shared neutral runtime input.

``session_setup`` restores a suspended Program or builds the boot,
registers its hidden macros, and wires its MicroCaller. ``advance``
returns the conductor's next turn, or None for a normal engine turn.
``aclose`` records an interrupted walk and closes any owned model client.

The engine's session provider is borrowed. An optional configured
micro provider is created lazily and owned here; invalid configuration
or construction failure falls back to the borrowed provider. Plugin
failures are logged and release the session to the general model.
"""

import logging
from functools import partial

from physiclaw.common.config import CONFIG, parse_model_ref
from physiclaw.conductor.drive import setup as conductor_setup
from physiclaw.conductor.drive.conductor import Conductor
from physiclaw.conductor.micro.channel import MicroCaller
from physiclaw.conductor.walk.program import Program
from physiclaw.contract.decision import decision_json
from physiclaw.contract.dto import Message, ToolResultMessage
from physiclaw.contract.plugin import (
    PluginRegistration,
    PluginServices,
    ProposedTurn,
    SessionSetup,
)
from physiclaw.contract.turn import TurnInput, TurnMetadata
from physiclaw.provider import make_chat_provider, mcp_blocks_to_content_blocks

log = logging.getLogger(__name__)


def build() -> PluginRegistration:
    """The factory `[agent] plugins` names."""
    return PluginRegistration(
        id="physiclaw.playbook",
        name="Playbook",
        create=ConductorPlugin,
    )


class ConductorPlugin:
    """One session's conductor behind the plugin seam.

    The promise to the runtime: NOTHING here raises. The engine keeps
    its own belt at the seam (setup, advance, and close are each
    wrapped there), and this class keeps the conductor's: every door
    catches, logs, and degrades — setup to a plain model session,
    advance to "the LLM speaks" with the conductor dropped for the
    rest of the session, close to a logged failure. Two belts on
    purpose: the guarantee must not depend on which side is edited."""

    def __init__(self) -> None:
        self._conductor: Conductor | None = None
        self._micro: MicroCaller | None = None
        self._pending: set[str] = set()

    async def session_setup(self, ctx: PluginServices) -> SessionSetup | None:
        try:
            program, hidden = conductor_setup.session_setup(events=ctx.events)
            self._micro = _wire_micro(program, ctx)
            self._conductor = Conductor(program=program, micro=self._micro)
            return SessionSetup(gated_macros=hidden)
        except Exception:
            log.exception(
                "conductor setup crashed — plain model session",
                extra={
                    "console_message": "Playbooks could not start — using the task model"
                },
            )
            self._conductor = None
            return SessionSetup(gated_macros={})

    async def advance(
        self, turn: TurnInput, *, metadata: TurnMetadata
    ) -> ProposedTurn | None:
        if self._conductor is None:
            return None
        try:
            # Only this plugin's pending calls enter its private execution
            # context. Other producers' messages and reasoning never enter it.
            history: list[Message] = []
            for event in turn.events:
                if event.call_id not in self._pending:
                    continue
                blocks = decision_json(event.result_blocks)
                assert isinstance(blocks, list)
                history.append(
                    ToolResultMessage(
                        tool_call_id=event.call_id,
                        content=mcp_blocks_to_content_blocks(blocks),
                        is_error=event.is_error,
                    )
                )
            message = await self._conductor.advance(history, metadata=metadata)
            self._pending = (
                {call.id for call in message.tool_calls}
                if message is not None
                else set()
            )
            return ProposedTurn(message) if message is not None else None
        except Exception:
            # The conductor is dropped for the rest of the session: its
            # state is unknown, and the transcript so far is the model's
            # hand-off. Every later advance is a pass.
            log.exception(
                "conductor advance crashed — the model takes the session",
                extra={
                    "console_message": "Playbook failed — the task model will continue"
                },
            )
            self._conductor = None
            return None

    async def aclose(self) -> None:
        # Teardown runs on EVERY session end (the engine's finally): a
        # walk cut short mid-flight (Ctrl-C, wall-clock budget) records
        # its abandoned row + breadcrumb here — the conductor's
        # counterpart of `loop.log_external_stop`, which only covers
        # model sessions (it keys on a drafted plan). Each close is its
        # own belt, so one failing never skips the other.
        if self._conductor is not None:
            try:
                self._conductor.abandon()
            except Exception:
                log.exception("conductor abandon record failed — ignored")
        if self._micro is not None:
            try:
                await self._micro.aclose()
            except Exception:
                log.warning("conductor micro client close failed", exc_info=True)


def _wire_micro(program: "Program | None", ctx: PluginServices) -> MicroCaller | None:
    """The micro-caller for the conductor's model calls — None only
    when NOTHING drives (no program). Any live walk can need one (the
    boot's parse_task, an agent step) — and
    the owned cheap-tier client is built lazily on the FIRST call, so
    wiring the caller costs nothing at wake. The session provider (off
    the setup context) is the fail-open floor."""
    if program is None:
        return None
    factory = None
    ref = CONFIG.conductor.micro_model
    if ref:
        try:
            pid, mid = parse_model_ref(ref)
            factory = partial(make_chat_provider, pid, mid, usage_sink=ctx.events)
        except Exception as e:
            log.warning(
                "conductor micro_model %r unusable (%s) — using the session model",
                ref,
                e,
            )
    return MicroCaller(
        ctx.session_provider,
        confidence_floor=CONFIG.conductor.micro_confidence,
        tr=ctx.events,
        rlog=ctx.wire,
        owned_factory=factory,
    )
