"""Choose the source of the next assistant turn.

``advance(history)`` drives the current Program, resolves its model
requests through MicroCaller, and returns its next tool-call message.
When the boot selects a task, its ``baton`` is the next Program to run.
When no Program remains, return None so the engine calls its model.

The conductor owns the current Program; the engine owns the transcript,
tool dispatch, and context compaction. A failed Program is recorded and
retired for this session. Its transcript is the general model's handoff;
playbook-local restrictions do not become engine-wide policies.
"""

import logging

from physiclaw.conductor.micro.channel import MicroCaller
from physiclaw.conductor.micro.decision import DecisionRequest
from physiclaw.conductor.walk.program import Program
from physiclaw.conductor.walk.surface import Paused
from physiclaw.contract.dto import AssistantMessage, Message
from physiclaw.contract.turn import TurnMetadata

log = logging.getLogger(__name__)


class Conductor:
    """Drive the active Program, then its selected successor (`baton`).

    Returning None gives control back to the engine's general model.
    """

    def __init__(
        self,
        program: Program | None = None,
        micro: MicroCaller | None = None,
    ):
        self._program = program
        self._micro = micro

    def abandon(self) -> None:
        """Record unfinished programs during session teardown.

        Include a successor selected before the session ended. Program.abandon
        is idempotent; recording failures must not prevent other cleanup.
        """
        walk = self._program
        while walk is not None:
            try:
                walk.abandon()
            except Exception:
                log.exception("conductor: abandon record failed — ignored")
            walk = walk.baton

    async def advance(
        self, history: list[Message], *, metadata: TurnMetadata
    ) -> AssistantMessage | None:
        """Return the next turn, following any successor, or None when finished."""
        while self._program is not None:
            turn = await self._drive(self._program, history, metadata)
            if turn is not None:
                return turn
            # Quiet is permanent — the transcript is the handoff (the
            # walk already retired itself at the moment it went quiet).
            # The baton, if it set one, is the next walk; else nobody.
            self._program = self._program.baton
        return None

    async def _drive(
        self, program: Program, history: list[Message], metadata: TurnMetadata
    ) -> AssistantMessage | None:
        """Advance one program and resolve model requests until it yields.

        Without a MicroCaller, resolve with no outcome and let the step
        apply its failure policy. Unexpected errors retire the program
        and preserve the traceback; the engine can continue the session.
        """
        try:
            step = program.advance(history)
            while isinstance(step, DecisionRequest):
                outcome = None
                if self._micro is not None:
                    outcome = (await self._micro.run(step, metadata=metadata)).outcome
                step = program.resolve(outcome)
            if isinstance(step, Paused):
                # Only a stepping tool sets a walk to pause; under the
                # engine it is a program bug like any other.
                raise RuntimeError("a walk paused under the engine")
        except Exception:
            log.exception("conductor: walk crashed — handing over to the model")
            try:
                program.crash()
            except Exception:
                log.exception("conductor: crash record failed — ignored")
            return None
        return step
