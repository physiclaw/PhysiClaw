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

log = logging.getLogger(__name__)


class Conductor:
    """Turn arbiter: the live program produces turns while it can, then
    its baton does; None means the LLM produces this one."""

    def __init__(
        self,
        program: Program | None = None,
        micro: MicroCaller | None = None,
    ):
        self._program = program
        self._micro = micro

    def abandon(self) -> None:
        """Session teardown (the plugin's aclose): a walk still in
        flight records its abandoned row and breadcrumb —
        `Program.abandon` is latched, so a walk that closed properly is
        a no-op. An un-taken baton counts too (built at the boot's
        resolve, session died before the next advance). Fail-open:
        teardown must never raise."""
        walk = self._program
        while walk is not None:
            try:
                walk.abandon()
            except Exception:
                log.exception("conductor: abandon record failed — ignored")
            walk = walk.baton

    async def advance(self, history: list[Message]) -> AssistantMessage | None:
        """Produce the next assistant turn, or None ("the LLM speaks"):
        the live program's turn, brokering any decision requests it
        raises; when it goes quiet, its baton's — the boot hands the
        activated program on this way; no walk, or one that handed over
        → None."""
        while self._program is not None:
            turn = await self._drive(self._program, history)
            if turn is not None:
                return turn
            # Quiet is permanent — the transcript is the handoff (the
            # walk already retired itself at the moment it went quiet).
            # The baton, if it set one, is the next walk; else nobody.
            self._program = self._program.baton
        return None

    async def _drive(
        self, program: Program, history: list[Message]
    ) -> AssistantMessage | None:
        """Run one walk for one turn, brokering every decision request
        it raises. None = it produced no turn (it went quiet) and the
        caller decides what that means.

        The brokering rule lives here once — in particular the rule
        that an unwired micro-caller resolves as NO outcome: the walk
        then hands over like any failed decision (the boot finishes
        empty like `not_a_task`). A program bug is caught here, once:
        the walk is crashed (recorded, quiet from here) and the model
        takes the session, because a session must survive the conductor
        while every tool and test sees the traceback."""
        try:
            step = program.advance(history)
            while isinstance(step, DecisionRequest):
                outcome = None
                if self._micro is not None:
                    outcome = (await self._micro.run(step)).outcome
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
