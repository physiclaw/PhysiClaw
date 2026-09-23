"""One playbook mid-walk — the walk's core.

A `Program` is one playbook being executed. The conductor asks it for
each turn; it answers with a synthesized ``[note, one-other]`` turn, a
``DecisionRequest`` to broker through the micro-caller (fed back via
``resolve()``), or ``None`` — spent for good: the transcript of its
turns is the model's handoff, and every handover or completion mints
ONE last ``[note, peek]`` brief turn first (`brief.py`).

This file is the walk alone: the phase, the cursor on the route
(`course.py`; the walk says when a run expands, a round ends, a
revision re-plans, and `rounds.py` computes what those moments
produce), the one action in flight (`turns.py`), the page verdict
every step judges against, the sequencing of declared recovery toward
a page (`recover.py` holds its rules and counts), the terminal
moments, and the record they write (`record.py`). The money state is
the gate's (`gate.py`), its guard `money.py`'s. What each STEP does is
its executor's (`walk/surface.py` is the contract), one per route line
kind: `steps.do`, `steps.agent`, `steps.ask`, `steps.tell`,
`steps.select`.

What the playbook declares is what runs: the walk opens with one peek,
starts at the route's first unsettled node (never below a resumed
suspension's cursor), runs a page's own `recover:` hand on a failed
check or hands over, and hands over on every blocked or errored call.
Money never recovers.
"""

import logging
from collections.abc import Callable
from enum import StrEnum
from functools import partial

from physiclaw.common import gesture_vocab
from physiclaw.common.listing import Screen
from physiclaw.conductor.micro.decision import MicroOutcome
from physiclaw.conductor.spec.channel import Channel
from physiclaw.conductor.spec.conventions import (
    LOCKED_ID,
    owned_by,
    page_id,
    page_name,
)
from physiclaw.conductor.spec.match import Verdict, match_screen
from physiclaw.conductor.spec.model import (
    ON_FAIL_SKIP,
    ON_FAIL_STOP,
    AgentNode,
    AskNode,
    Checked,
    DoNode,
    Playbook,
    PlaybookError,
    RunNode,
    SelectNode,
)
from physiclaw.conductor.spec.pages import Landmark, PagePrint
from physiclaw.conductor.walk import brief, recover, rounds, speak, views
from physiclaw.conductor.walk.course import Course, Round
from physiclaw.conductor.walk.gate import Gate
from physiclaw.conductor.walk.ledger import Ledger
from physiclaw.conductor.walk.record import Record
from physiclaw.conductor.walk.surface import Activator, Paused, Step, Steps, Turn
from physiclaw.conductor.walk.suspension import (
    SUSPENDED_SCHEMA,
    clear_suspended,
    write_suspended,
)
from physiclaw.conductor.walk.thread import Thread
from physiclaw.conductor.walk.turns import Turnsmith
from physiclaw.conductor.walk.walklog import Outcome
from physiclaw.contract.dto import (
    AssistantMessage,
    ImageBlock,
    Message,
    ToolResultMessage,
)
from physiclaw.contract.plugin import EventSink
from physiclaw.macros.model import Macro

log = logging.getLogger(__name__)

# runtime.sentinel.WAIT, spelled literally: the conductor may not import
# engine runtimes; a test pins the two equal.
SUSPEND_STATUS = "WAIT"
DONE_STATUS = "DONE"  # runtime.sentinel.DONE, likewise pinned

# The one recovery action's pending kind — the declared hand's landing.
KIND_RECOVER = "recover-hand"
# A resumed walk's one infrastructure action: the unlock before its
# opening reading.
KIND_UNLOCK = "resume-unlock"


class Phase(StrEnum):
    """Where a walk stands in its life. One value replaces the latches
    it grew up with (started / done / paused): every transition is a
    named event, and the terminal rules read off one field."""

    FRESH = "fresh"  # constructed, never advanced
    OPENING = "opening"  # the first reading: unlock, gate resume, the opening peek
    LIVE = "live"  # a step at the cursor: stepping, recovering, gated
    PAUSED = "paused"  # a stepping run ended with the cursor moved on
    DONE = "done"  # the last word is minted: brief, or crash — quiet from here


# How a checked node names itself in a handover reason.
_CHECKED_KIND: dict[type, str] = {
    DoNode: "move",
    AgentNode: "agent",
    SelectNode: "select",
}


class Program:
    """One playbook mid-walk. Constructed per session (the conductor
    plugin's wake setup builds it), so the cursor state lives for
    exactly one attempt; persistence across wakes is the suspension file
    (`suspend`), and only that."""

    def __init__(
        self,
        *,
        spec: Playbook,
        values: dict[str, str],
        pack_macros: dict[str, Macro],
        prints: list[PagePrint],
        channel: Channel | None = None,
        suspended: dict | None = None,
        position: dict | None = None,
        landmarks: "dict[str, Landmark] | None" = None,
        dry: bool = False,
        activation: "Activator | None" = None,
        events: "EventSink | None" = None,
        thread: "Thread | None" = None,
        steps: Steps,
    ) -> None:
        self.app = spec.app
        # The executors (`walk/surface.py` says the shape, `steps/table.py`
        # fills it): handed in so the walk never imports one.
        self._steps = steps
        # A dry walk (`replay.py`) leaves no trace: no runs.jsonl line,
        # no daily-log entry, no suspension file. Everything else runs
        # exactly as live.
        self.spec = spec
        self.values = values
        # The `inputs.<name>` half of the ref-resolution dict, built once:
        # `values` never mutates after construction.
        self._input_vals = {f"inputs.{k}": v for k, v in values.items()}
        # The program's own qualified dispatch contribution — merged into
        # the wake registry by session_setup.
        self.pack_macros = pack_macros
        self.prints = prints
        # The user-channel infrastructure (constructor-injected via
        # build.build_program). None degrades to hand-over at the first
        # ask.
        self.channel = channel
        # The pack's declared fixed spots — recover hands tap them, agent
        # episodes read them as a `given:`.
        self.landmarks: dict[str, Landmark] = landmarks or {}
        # The boot's activation (menu, parse_task, build) — what its
        # `select` step runs; None on every other walk — and the
        # baton that step hands on: the program the conductor drives
        # once this walk goes quiet.
        self.activation = activation
        self.baton: "Program | None" = None
        self.course = Course(spec)  # the route and the cursor (course.py)
        # Turn minting + the one action in flight (`turns.py`). The scope
        # is the playbook ref: two walks in one session (the boot, then
        # the program it activates) mint under different names, so a
        # call id can never find the other walk's stale result.
        ref = f"{spec.app}/{spec.name}"  # one spelling, both readers
        self.turns = Turnsmith(ref)
        self.gate = Gate()
        # The session's thread (`thread.py`): opened by the boot, handed
        # to the walk it activates; a walk built alone opens its own.
        self.thread = thread if thread is not None else Thread()
        # The walk's one account (`ledger.py`): the task, the agents'
        # decisions (`{node.field}` refs read them), what was said and
        # paid — every step writes it, every exit reads it.
        self.ledger = Ledger(ref=ref, task=values)
        # The screen/verdict the current step works from — every path
        # observes one before acting — and the frame the same result
        # carried (None when the read had no image), what a model call
        # sees beside the listing.
        self.screen: Screen | None = None
        self.frame: ImageBlock | None = None
        self.verdict: Verdict | None = None
        # The step executor at the cursor (a resume pre-step rides the
        # same slot before the walk proper opens).
        self._step: Step | None = None
        # Recovery (`recover.py`): the hand in flight and the actions
        # spent per target page.
        self.recoveries = recover.Recoveries()
        # Whether the restored cursor is a wake's suspension: its opening
        # read may unlock a locked phone, once.
        self._from_suspension = False
        self._unlocked = False
        # Telemetry (`walklog`): decision outcomes brokered to this walk.
        self._micros = 0
        # The record (`record.py`): the runs.jsonl line, the session's
        # `walk` event, the daily-log entries a terminal moment writes,
        # and the outcome latch.
        self.record = Record(self.app, spec.name, dry, events)
        self.phase = Phase.FRESH
        # The journal line the next synthesized note carries
        # (record-don't-replay: the transcript carries what happened).
        self._journal: str | None = None
        # Stepping: a rehearsal that wants ONE node sets `step_one`; the
        # walk runs the first node it opens and answers `Paused` the
        # moment the cursor stands anywhere else — forward when the node
        # settles, backward when a revision re-plans — WITHOUT opening
        # the next step, so nothing of it (a payment's
        # consent, an ask's numbers) is spent. (The opening peek may
        # move the cursor past a settled prefix first; the node opened
        # after it is the one.)
        self.step_one = False
        self._stepped: tuple[int, str | None, int] | None = None
        if suspended is not None and position is not None:
            raise ValueError("a walk resumes a suspension OR a position, not both")
        restored = suspended if suspended is not None else position
        if restored is not None:
            # A restored walk is ALSO whole at construction: the
            # projection overlays the fresh state right here, so no
            # caller ever patches a program up afterwards.
            self._restore(restored, resumed=suspended is not None)
        if suspended is not None:
            log.info(
                "conductor: resuming suspended %s/%s at node %d (%s)",
                self.app,
                spec.name,
                self.course.idx + 1,
                "awaiting reply" if self.gate.awaiting else "walk",
            )

    # ---- suspension ----

    def state(self) -> dict:
        """The walk's position as the suspension projection — what a
        later wake, or a stepping rehearsal's next invocation, rebuilds
        the walk from (`suspended=` or `position=` at
        construction): walk state here,
        gate state via `Gate.to_suspended` — each field list lives
        beside its fields. `idx` is the SPEC index (a run counts as one
        node); a cursor inside a run's round adds the round's key and
        the offset within it."""
        origin, key, at = self.course.position()
        return {
            "schema": SUSPENDED_SCHEMA,
            "app": self.app,
            "playbook": self.spec.name,
            "idx": origin,
            "round": {"key": key, "at": at} if key is not None else None,
            "label": self.course.label(),
            "values": self.values,
            **self.thread.to_suspended(),
            **self.ledger.to_suspended(),
            **self.gate.to_suspended(),
        }

    def _restore(self, data: dict, *, resumed: bool) -> None:
        """Overlay one projection (`state()`'s shape). The stored cursor
        is where the walk opens either way (the next node's own checks
        judge whether the world still fits). `resumed` marks a WAKE's
        suspension: its opening read may unlock a locked phone once. A
        stepping position keeps the fresh walk's rules, and the tool's
        own preamble wakes the phone."""
        idx = int(data["idx"])
        if not (0 <= idx <= len(self.spec.nodes)):
            # The spec changed under the suspension (edited shorter) — a
            # stale cursor must not fake a completion. Raising drops the
            # suspension (load_suspended is fail-open).
            raise PlaybookError(f"suspended idx {idx} is outside the playbook")
        self._from_suspension = resumed
        self.ledger.restore(data)
        self.thread.restore(data)
        self.gate = Gate.from_suspended(data)
        # The stored cursor is a SPEC index; inside a run's round it
        # comes with the round's key and offset, and the run is expanded
        # again from the restored record (a finished round stays out,
        # the same rule as live) to find that round's place.
        self.course.skip_to(idx)
        inside = data.get("round")
        run = self.course.node
        if inside and isinstance(run, RunNode):
            key, at = str(inside.get("key", "")), int(inside.get("at", 0))
            rd = next((r for r in self._expand(run) if r.key == key), None)
            if rd is None:
                raise PlaybookError(f"suspended round {key!r} is not in the run")
            self.course.seat(rd, at)  # raises on a stale offset (fail-open)

    def drop_suspension(self) -> None:
        """Forget the walk's suspension file — a rehearsal's, once the
        rehearsal is over. Dry-aware like `suspend` itself: a dry walk
        never wrote one, and a stepping rehearsal (always dry) that
        suspends at an ask must not delete a REAL wake's pending file —
        its cursor, consent and round records — from under it."""
        if not self.dry:
            clear_suspended()

    def suspend(self) -> AssistantMessage:
        """Write the suspended state (`state()`, the cursor where it
        stands) and close the session WAIT. No job is synthesized: a
        WAIT without a session-created job auto-schedules the follow-up
        alarm (`contract.drive`) — the file, not the job, is what
        resumes the walk on ANY next wake. The one caller is an ask out
        of patience, which already holds the gate open."""
        assert self.gate.awaiting, "only an ask awaiting its reply suspends"
        # A suspension never carries an unlogged payment: the amount
        # rides to the next wake, the line saying it fired stays here.
        self.log_purchase()
        if not self.dry:
            write_suspended(self.state())
        recap = self.ledger.recap(
            f"waiting for the user's reply on {self.ref}", consented=self.gate.consented
        )
        self._record_run(Outcome.SUSPENDED, recap)
        return self._close_session(
            "suspend",
            recap,
            f"{self.ref} suspended — {recap}; any wake resumes it",
        )

    def close_done(self, recap: str, memory: str | None) -> AssistantMessage:
        """A completed walk's end: recorded, the daily log's line (the
        model's memory line when the closing call wrote one, else the
        recap), and the session closed DONE."""
        log.info("conductor: playbook %s complete — %s", self.ref, recap)
        self._end(Outcome.COMPLETED)
        return self._close_session(
            "complete", recap, memory or recap, status=DONE_STATUS
        )

    def _close_session(
        self, kind: str, recap: str, day_line: str, status: str = SUSPEND_STATUS
    ) -> AssistantMessage:
        """The session closed by the walk's own hand — the one
        synthesized `end_session` (a completion's DONE; a suspension's
        or a stop's WAIT). The close-routine's daily-log line is
        harness-written here: the model never runs this wake, so without
        it the close would be invisible to the next wake's memory
        window."""
        self.log_day(f"conductor: {day_line}")
        return self.synth(
            kind,
            f"conductor: {kind} — {recap}",
            "end_session",
            {"status": status, "recap": recap},
        )

    @property
    def ref(self) -> str:
        """The playbook ref this walk runs, as every line names it."""
        return self.ledger.ref

    @property
    def outputs(self) -> dict[str, str]:
        """The agents' return fields, keyed `node.field` — the ledger's
        decisions (`{node.field}` refs and the resume cursor read them);
        a round's under its prefix (`Round`)."""
        return self.ledger.decided

    def decide(self, node_id: str, field: str, value: str) -> None:
        rd = self.course.round
        if rd is not None:
            self.ledger.decide_in_round(rd.prefix, f"{node_id}.{field}", value)
        else:
            self.ledger.decide(f"{node_id}.{field}", value)

    # ---- the conductor's two calls ----

    def advance(self, history: list[Message]) -> Turn:
        """The next synthesized turn; a DecisionRequest for the conductor
        to broker (feed the outcome back via ``resolve``); `Paused` when
        a stepping run is over; or None — spent, hand over to the model.
        A spec-level failure (a ref with no value) hands over with its
        reason. A program bug RAISES: the one caller that must keep a
        session alive (`Conductor._drive`) catches it and calls `crash`;
        every tool and test sees the traceback."""
        if self.phase is Phase.FRESH:
            self.phase = Phase.OPENING
        return self._guarded(lambda: self._advance(history))

    def resolve(self, outcome: MicroOutcome | None) -> Turn:
        """Continue the walk with a micro-call's outcome (None = the call
        failed or was under-confident — hand over). Same return contract
        and same guarantees as ``advance``."""
        self._micros += 1
        return self._guarded(lambda: self._resolve(outcome))

    def _guarded(self, run: "Callable[[], Turn]") -> Turn:
        try:
            return run()
        except PlaybookError as e:
            return self.handover(str(e))

    def crash(self) -> None:
        """The walk died of a program bug (the conductor caught it):
        quiet from here whatever else fails, and recorded as crashed
        when the record can be written. The transcript so far is the
        model's hand-off."""
        self.phase = Phase.DONE
        try:
            # First, and by itself: money may have moved, and `_record_run`
            # latches the outcome, so the teardown's `abandon()` — the
            # other writer of this line — returns early after this.
            self.log_purchase()
        except Exception:
            log.exception("conductor: crash purchase line failed — ignored")
        try:
            self._record_run(Outcome.CRASHED, "program crashed")
        except Exception:
            log.exception("conductor: crash record failed — ignored")

    def _advance(self, history: list[Message]) -> Turn:
        if self.phase is Phase.DONE:
            # The last turn (a brief's peek, or the walk's own end_session)
            # was the walk's last word; its result is ordinary history.
            # Quiet from here — the conductor drops us.
            return None
        if self.phase is Phase.PAUSED:
            return Paused()
        if self.turns.pending is None:
            return self._opening()
        pending, result, failed = self.turns.settle(history)
        if pending.kind == "suspend":
            # The suspension file is already written; whether the
            # end_session was blocked, its result never arrived, or the
            # session simply ran on, a dead walk must not resurrect on
            # the next wake.
            if not self.dry:
                clear_suspended()
            return self.handover(
                f"{failed or 'suspend end_session returned'} — suspension dropped"
            )
        if failed is not None:
            # Nothing retries in the background: what the playbook did
            # not declare, the model decides. A fired payment is logged
            # first — money may have moved even though the call failed.
            self.log_purchase()
            return self.handover(failed)
        assert result is not None  # settle: a result or a failure
        self._observe(pending.kind, result, channel=pending.channel)
        unlock = self._resume_unlock()
        if unlock is not None:
            return unlock
        return self._landed(pending.kind)

    def _observe(self, kind: str, result: ToolResultMessage, *, channel: bool) -> None:
        """One reading, one verdict: channel-facing actions (declared at
        the synth site) match against the thread; everything else
        against the pack's own pages."""
        self.screen = views.screen_of(result)
        self.frame = views.frame_of(result)
        self.verdict = match_screen(
            self.screen,
            self.channel.prints if channel and self.channel else self.prints,
        )
        log.info(
            "conductor: %s/%s read after %s — %s",
            self.app,
            self.spec.name,
            kind,
            self.verdict.describe(),
        )
        node = self.course.node
        self.record.read(kind, node.id if node is not None else None, self.verdict)

    def _resume_unlock(self) -> AssistantMessage | None:
        """A resumed walk's first reading is the cover: wake the phone
        once, then open again exactly as before. None otherwise."""
        assert self.verdict is not None
        if (
            self.phase is Phase.OPENING
            and self._from_suspension
            and not self._unlocked
            and self.verdict.matches(LOCKED_ID)
        ):
            self._unlocked = True
            return self.synth(
                KIND_UNLOCK,
                "conductor: phone is locked on resume — unlocking",
                gesture_vocab.UNLOCK_PHONE,
                {},
            )
        return None

    def _landed(self, kind: str) -> Turn:
        """The settled action's kind decides who reads the landing: the
        walk's own infrastructure kinds, the recovery hand, or the step
        in flight."""
        if kind == KIND_UNLOCK:
            return self._opening()
        if kind == "peek":
            # Past the settled pure-text prefix (a stepped walk may seed
            # an agent's answer), never below a restored cursor.
            self.phase = Phase.LIVE
            self.course.skip_to(self.spec.first_unsettled(self.outputs))
            return self.next()
        if kind == KIND_RECOVER:
            return self._recover_landed()
        if self._step is not None and kind in self._step.kinds:
            return self._step.landed(kind)
        # A typo'd kind at a synth site must fail loudly, never silently
        # walk the next node.
        return self.handover(f"unknown pending action kind {kind!r}")

    def _resolve(self, outcome: MicroOutcome | None) -> Turn:
        if self._step is None:
            return self.handover("a decision arrived with no step in flight")
        return self._step.resolve(outcome)

    def _opening(self) -> Turn:
        """The walk's first turn (fresh, or after a resume): the ask's
        reply check when suspended at a gate, else the plain opening
        peek."""
        if self.gate.awaiting:
            # Suspended-at-gate resume: the ask was sent before the
            # suspension — the ask step picks up at its reply check.
            node = self.course.node
            if not isinstance(node, AskNode):
                return self.handover(
                    "suspended awaiting a reply, but no ask at the cursor"
                )
            self._step = self._steps.for_node[AskNode](self, node)
            return self._step.open()
        # Observe before acting: the first page check needs a screen.
        return self.peek()

    # ---- the cursor ----

    def next(self) -> Turn:
        """Walk the node at the cursor: open its step executor, or finish.
        Works from the stored screen/verdict — every path here observed
        one first."""
        if self.verdict is None:
            return self.handover("no screen observed yet")
        course = self.course
        node = course.node
        if isinstance(node, RunNode):
            # The run's rounds take its place in the route, and the
            # cursor walks the first of them — or whatever follows when
            # every round is already done.
            self._expand(node)
            return self.next()
        if node is None:
            # The task is the playbook's and it is done: its `tell` already
            # reported to the user and its record wrote the runs row, so
            # the walk closes the session DONE itself (handing the model a
            # "wrap up" bought four turns of note-taking over a full
            # context). The close is a step (`steps/close.py`): one call in
            # the session's thread for the record, then `close_done`.
            self._step = self._steps.close(self)
            return self._step.open()
        if self.step_one:
            here = course.position()
            if self._stepped is None:
                self._stepped = here
            elif here != self._stepped:
                # The cursor left the one node this run was for. Pause
                # here, before the next step opens and spends anything.
                # Judged by POSITION: a finished or missed round leaves
                # the route, so the next round's first node inherits the
                # index this one had and an index check would walk on.
                log.info(
                    "conductor: stepping pause — cursor moved from %s to %s",
                    self._stepped,
                    here,
                )
                self.phase = Phase.PAUSED
                return Paused()
        self._step = self._steps.for_node[type(node)](self, node)
        return self._step.open()

    def advance_cursor(self) -> Turn:
        """The step at the cursor is done — walk the next node. The ONE
        way the cursor moves forward. Leaving a round's last node
        records the round: its returns, or the miss."""
        self._step = None
        left = self.course.step()
        if left is not None:
            self._finish_round(left)
            self.course.drop_round(left)
        return self.next()

    # ---- what the steps read and call ----

    def ref_values(self) -> dict[str, str]:
        """Ref-resolution values at the cursor (`rounds.values`)."""
        return rounds.values(
            self.course, self.ledger, self._input_vals, self.gate.replies
        )

    def _expand(self, run: RunNode) -> list[Round]:
        """Replace the `run` at the cursor by its rounds still to do
        (`rounds.plan`). Returns them; raises PlaybookError when the run
        cannot expand (the guarded callers hand over on it)."""
        planned = rounds.plan(run, self.ref_values(), self.ledger)
        self.course.expand(planned)
        return planned

    def revise(self, replies: str) -> Turn:
        """A reply the ask's words missed re-plans the walk, when the
        enclosing `run` says `revise:` and its revisions are not spent:
        the reply joins `{ask.replies}`, the route from the named agent
        on goes back to its spec shape (a run in progress re-expands
        later; a finished round keeps its record and is not walked
        again), and the cursor re-runs that agent."""
        rd = self.course.round
        if rd is None or rd.run.revise is None:
            return None
        if self.gate.revisions >= rd.run.revise_limit:
            return None
        target = [n.id for n in self.spec.nodes].index(rd.run.revise)
        self.gate.revisions += 1
        self.gate.replies.append(replies)
        # The ask is left, the conversation is not: the thread as it
        # reads NOW becomes the baseline, so a reply the user types
        # while the walk re-plans is still new at the next landing.
        self.gate.rewind_ask(speak.snapshot(self))
        self.journal(
            f"revising from {rd.run.revise!r} ({self.gate.revisions}/"
            f"{rd.run.revise_limit}) — the user said {replies!r}"
        )
        log.info("conductor: %s revises from %s — %r", self.ref, rd.run.revise, replies)
        # A round in progress past the target starts over on the next
        # pass: its partial record (an agent's output from this pass)
        # is dropped, or refs would read it until overwritten.
        for rd_open in self.course.reset(target):
            if not self.ledger.round_finished(rd_open.prefix):
                self.ledger.rounds.pop(rd_open.prefix, None)
        # The target's answer is no longer a decision (an opening walks
        # past a pure-text agent with one on record) but stays its last
        # answer, which its own prompt re-reads.
        self.ledger.unsettle(rd.run.revise)
        self.recoveries.drop()
        self._step = None
        return self.next()

    def _word_at_cursor(self, explicit: str | None) -> str | None:
        """The exit word a failure at the cursor takes. Inside a run's
        round, the composing route's word first — the run's `miss:`,
        then its `on_fail:` — over the playbook's own, which holds when
        it walks alone. Then the one the caller resolved (a page's own,
        in `recover_or_handover`), then the cursor node's `on_fail`."""
        rd = self.course.round
        if rd is not None and (rd.run.miss or rd.run.on_fail):
            return rd.run.miss or rd.run.on_fail
        if explicit is not None:
            return explicit
        node = self.course.node
        return node.on_fail if node is not None else None

    def _skip_round(self, rd: Round, reason: str) -> Turn:
        log.warning("conductor: round %s missed — %s", rd.prefix, reason)
        self.journal(f"round {rd.prefix} missed — {reason}")
        self.ledger.round_missed(rd.prefix, reason)
        self.recoveries.drop()
        self._step = None
        self.course.drop_round(rd)
        return self.next()

    def _finish_round(self, rd: Round) -> None:
        """A round's last node settled: its returns, filled from its own
        record, land under its prefix (`rounds.returns`)."""
        vals = rounds.round_values(self.course.node, rd, self.ledger, self.gate.replies)
        self.ledger.round_done(rd.prefix, rounds.returns(rd, vals))

    def spend_consent(self) -> None:
        """A payment move fires: consent is consumed, the amount survives
        into the history line and the purchase log. A later action of
        the same payment episode finds the consent already spent and
        leaves the record alone."""
        amount = self.gate.spend()
        if amount is not None:
            self.ledger.pay(amount)
            # The line names the walk's running total (`ledger.paid`),
            # not this one fire's amount.
            self.record.fired(self.ledger.paid)

    def log_purchase(self) -> None:
        """The fired payment's purchase line, once (`Record.purchase`)."""
        self.record.purchase(self.ref)

    def enter_gate(self, node: Checked) -> Turn:
        """The one enter-page guard moves, acting agents, and the boot's
        activate share: None when the node has no enter (a `start` runs
        unconditionally) or the page reads; else the recovery/handover
        step."""
        if not node.enter:
            return None
        assert self.verdict is not None
        expected = page_id(self.app, node.enter)
        wrong = self.verdict.mismatch(expected)
        if wrong is None:
            return None
        kind = _CHECKED_KIND[type(node)]
        return self.recover_or_handover(
            node,
            expected,
            recover.Mode.ENTER,
            f"{kind} {node.id!r} expects page {node.enter!r} ({wrong})",
        )

    def journal(self, text: str) -> None:
        """What the next synthesized note carries beside its own summary
        — and one more line of the walk's account."""
        self._journal = text
        self.ledger.note(text)

    def peek(self) -> AssistantMessage:
        return self.synth(
            "peek",
            f"conductor: observing the screen before walking {self.ref}",
            gesture_vocab.PEEK,
            {},
        )

    def synth(
        self, kind: str, summary: str, tool: str, args: dict, *, channel: bool = False
    ) -> AssistantMessage:
        """The walk's synthesized turn: fold in anything journaled, then
        mint it (`turns.py` owns the shape and the call-id convention)."""
        if self._journal is not None:
            summary = f"{summary} | {self._journal}"
            self._journal = None
        return self.turns.synth(kind, summary, tool, args, channel=channel)

    def conclude(self, reason: str) -> None:
        """The walk's quiet completion — recorded, DONE, and no brief
        turn: the boot's ending is the next walk's beginning (its
        `select` step set the baton) or the model's own thread (it
        set none), and either way there is nothing to report to a
        model that is not about to speak from a brief."""
        log.info("conductor: %s/%s concluded — %s", self.app, self.spec.name, reason)
        self._end(Outcome.COMPLETED, reason)

    def handover(
        self, reason: str, *, advice: str = "", word: str | None = None
    ) -> Turn:
        """The walk's exit by an `on_fail` word — `stop`, or (anything
        else, unsaid included) the ONE final synthesized [note, peek]
        brief turn (`brief.walk_brief`), the distilled report the model
        resumes from; `_done` then makes the NEXT advance the permanent
        None the conductor drops the program on — or `skip`, inside a
        run that declared its rounds skippable. The word is resolved
        by `_word_at_cursor`: the caller's (a page that could not be
        reached answers for itself, in `recover_or_handover`), else the
        enclosing run's `miss`, else the cursor node's. `reason`
        is the fact, and lands in the record either way; `advice` is
        what the model taking over owes (a deny's back-out), so only the
        brief carries it — a stop has no model to instruct, and its
        recap is the daily log's line."""
        word = self._word_at_cursor(word)
        if word == ON_FAIL_STOP:
            return self.stop(reason)
        if word == ON_FAIL_SKIP:
            rd = self.course.round
            assert rd is not None  # the word comes from the round's run
            return self._skip_round(rd, reason)
        log.warning(
            "conductor: handing %s/%s over to the model — %s",
            self.app,
            self.spec.name,
            reason,
        )
        self._end(Outcome.HANDOVER, reason)
        return self.synth(
            "brief",
            brief.walk_brief(
                reason,
                ledger=self.ledger,
                where=self.course.label(),
                consented=self.gate.consented,
                advice=advice,
            ),
            gesture_vocab.PEEK,
            {},
        )

    def stop(self, reason: str) -> AssistantMessage:
        """The walk's exit that keeps the model out: recorded as a
        handover, then the session closes WAIT by the walk's own hand
        (the same synthesized end_session a suspension uses). No
        suspension is written, so the next wake reads the thread again
        and walks the route from the top. The recap says whether money
        moved: after a fired payment a stop leaves what it paid for unverified."""
        recap = brief.stop_recap(
            reason, ref=self.ref, where=self.course.label(), ledger=self.ledger
        )
        log.warning("conductor: %s", recap)
        self._end(Outcome.HANDOVER, reason)
        return self._close_session("stop", recap, recap)

    # ---- recovery ----

    def recover_or_handover(
        self, node: Checked, expected_id: str, mode: recover.Mode, reason: str
    ) -> Turn:
        """The page's declared hand before the model: a deviation is
        recovered toward the page the frozen cursor already requires —
        the cursor, outputs, and consent are untouched throughout. Never
        where `recover.barred` says money keeps the hard handover."""
        recovery = self.course.recovers().get(page_name(expected_id))
        # The page's own word once its hand is spent (or it has none) —
        # either spelling, since a page saying `handover` under a node
        # saying `stop` means exactly that; unsaid, the cursor node's
        # word decides in `handover`.
        fail = partial(
            self.handover, word=recovery.on_fail if recovery is not None else None
        )
        if recover.barred(
            node,
            consented=self.gate.consented,
            awaiting=self.gate.awaiting,
            paid=self.ledger.paid,
        ):
            return fail(reason)
        if not owned_by(expected_id, self.app):
            # Recovery covers this pack's own pages only — a reserved or
            # channel target has no hand to declare.
            return fail(reason)
        reading = recover.reading_of(self.verdict, expected_id)
        step = recover.plan(
            self.recoveries.total,
            recovery,
            self.recoveries.spent(expected_id),
            reading=reading,
        )
        if isinstance(step, recover.Exhausted):
            return fail(f"{reason} — {step.reason}")
        act = recover.action(step.hand, self.app, self.landmarks)
        if isinstance(act, str):
            return fail(f"{reason} ({act})")
        tool, args = act
        # In flight and counted (the page's `tries` and the walk-wide
        # ceiling); the landing dispatch reads `KIND_RECOVER`.
        self.recoveries.engage(
            recover.State(node=node, target=expected_id, mode=mode, reason=reason)
        )
        return self.synth(
            KIND_RECOVER,
            f"conductor: recovering toward {expected_id} via its declared hand "
            f"({reading})",
            tool,
            args,
        )

    def _recover_landed(self) -> Turn:
        """The hand's result view, judged. Restored → resume exactly
        where the walk stood: an interrupted enter re-checks and runs its
        move; an interrupted verify is satisfied by the restored page
        (the macro already ran — never re-run it). Still off → the same
        page's hand again, within its `tries`, then its `on_fail` word;
        nothing before the page runs again, so a landed move is never
        crossed twice."""
        st = self.recoveries.land()
        assert self.verdict is not None
        self._step = None
        if self.verdict.matches(st.target):
            self.journal(f"recovered {st.target} via its declared hand")
            if st.mode is recover.Mode.VERIFY:
                return self.advance_cursor()
            return self.next()
        self.journal(f"recover hand ran — {st.target} still off")
        return self.recover_or_handover(st.node, st.target, st.mode, st.reason)

    # ---- the record ----

    def abandon(self) -> None:
        """The session ended with this walk mid-flight (the plugin's
        teardown): one runs.jsonl line so the escalation KPI counts it,
        plus a daily-log breadcrumb when the walk actually moved the
        phone. Latched like every terminal moment — a walk that already
        closed is a no-op, and so is one that never started."""
        if self.phase in (Phase.FRESH, Phase.PAUSED) or self.outcome is not None:
            # Never started, already closed, or a stepping pause (the
            # walk continues from its persisted position next run).
            return
        self.log_purchase()  # a fired payment outlives the session
        node = self.course.node_id() or "(end)"
        self._record_run(Outcome.ABANDONED, "session ended mid-walk")
        self.log_day(
            f"conductor: {self.ref} cut short mid-walk "
            f"at node {node} — the next wake starts the route over"
        )

    @property
    def dry(self) -> bool:
        """A dry walk leaves no trace: no runs row, no daily-log entry,
        no suspension file (`Record`)."""
        return self.record.dry

    @property
    def outcome(self) -> Outcome | None:
        """How the walk ended, None while it runs (`Record`)."""
        return self.record.outcome

    def log_day(self, entry: str) -> None:
        self.record.day(entry)

    def _end(self, outcome: Outcome, reason: str = "") -> None:
        """A terminal moment that also closes the walk: recorded, and
        DONE — the next advance is the permanent None."""
        self._record_run(outcome, reason)
        self.phase = Phase.DONE

    def _record_run(self, outcome: Outcome, reason: str = "") -> None:
        """The walk's runs.jsonl line, with the walk's own numbers
        (`Record.run` keeps the first terminal moment)."""
        self.record.run(
            outcome,
            idx=self.course.idx,
            nodes=len(self.course),
            node=self.course.node_id(),
            reason=reason,
            micros=self._micros,
            rescues=self.recoveries.total,
            values=self.values,
            total=self.ledger.paid,
        )
