"""One playbook mid-walk — the walk's core.

A `Program` is one playbook being executed. The conductor asks it for
each turn; it answers with a synthesized ``[note, one-other]`` turn, a
``DecisionRequest`` to broker through the micro-caller (fed back via
``resolve()``), or ``None`` — spent for good: the transcript of its
turns is the model's handoff, and every handover or completion mints
ONE last ``[note, peek]`` brief turn first (`brief.py`).

This file is the walk alone: the cursor and phase, the one action in
flight (`turns.py`), the page verdict every step judges against, the
declared recovery toward a page (`recover.py`), the money state an ask
binds and a payment move spends, the terminal moments, and the record
they write (`record.py`). What each STEP does is its executor's
(`step.py` is the contract), one per route entry kind: `step_do`,
`step_agent`, `step_ask`, `step_tell`, `step_activate`.

What the playbook declares is what runs: the walk opens with one peek,
starts at the route's first unsettled node (never below a resumed
suspension's cursor), runs a page's own `recover:` hand on a failed
check or hands over, and hands over on every blocked or errored call.
Money never recovers.
"""

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial

from physiclaw.common import gesture_vocab
from physiclaw.common.listing import Screen
from physiclaw.common.logger import write_json_atomic
from physiclaw.common.text import clip
from physiclaw.conductor.spec.channel import Channel
from physiclaw.conductor.spec.conventions import LOCKED_ID, owned_by, page_id, page_name
from physiclaw.conductor.spec.match import Reading, Verdict, match_screen
from physiclaw.conductor.spec.model import (
    ON_FAIL_SKIP,
    ON_FAIL_STOP,
    READING_COVERED,
    READING_ELSEWHERE,
    READING_LOCKED,
    ActivateNode,
    AgentNode,
    AskNode,
    Checked,
    DoNode,
    Node,
    Playbook,
    PlaybookError,
    Recovery,
    RunNode,
    TellNode,
)
from physiclaw.conductor.spec.pack import qualified_macro, resolve_inputs
from physiclaw.conductor.spec.pages import Landmark, PagePrint
from physiclaw.conductor.spec.refs import fill_args, fill_refs
from physiclaw.conductor.walk import brief, money, recover, speak, views
from physiclaw.conductor.walk.gate import Gate
from physiclaw.conductor.walk.ledger import Ledger, round_prefix
from physiclaw.conductor.walk.micro import MicroOutcome
from physiclaw.conductor.walk.record import Record
from physiclaw.conductor.walk.step import Activator, Paused, Step, Turn
from physiclaw.conductor.walk.step_activate import ActivateStep
from physiclaw.conductor.walk.step_agent import AgentStep
from physiclaw.conductor.walk.step_ask import AskStep
from physiclaw.conductor.walk.step_close import CloseStep
from physiclaw.conductor.walk.step_do import DoStep
from physiclaw.conductor.walk.step_tell import TellStep
from physiclaw.conductor.walk.suspension import (
    SUSPENDED_SCHEMA,
    clear_suspended,
    suspended_path,
)
from physiclaw.conductor.walk.thread import Thread
from physiclaw.conductor.walk.turns import Turnsmith
from physiclaw.conductor.walk.walklog import Outcome
from physiclaw.contract.dto import (
    AssistantMessage,
    ImageBlock,
    Message,
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


# The executor for each route entry kind (`Node` is a closed union).
_STEP_FOR: dict[type, type[Step]] = {
    DoNode: DoStep,
    AgentNode: AgentStep,
    AskNode: AskStep,
    TellNode: TellStep,
    ActivateNode: ActivateStep,
}


# How a checked node names itself in a handover reason.
_CHECKED_KIND: dict[type, str] = {
    DoNode: "move",
    AgentNode: "agent",
    ActivateNode: "select",
}


@dataclass(frozen=True)
class Round:
    """One round of a `run`: the playbook's nodes walked once, with its
    own inputs and its own record in the ledger (`ledger.rounds`, under
    `ledger.round_prefix`), so a re-plan can tell a finished round from
    one still to do, and a round's own refs read only its own record."""

    run: RunNode
    key: str
    inputs: dict[str, str]  # `inputs.<name>` → value, this round's

    @property
    def prefix(self) -> str:
        return round_prefix(self.run.id, self.key)


@dataclass(frozen=True)
class Slot:
    """One place on the route the cursor walks: the node, the spec index
    it came from (what a suspension stores), and the round it belongs
    to — None for the route's own nodes."""

    node: Node
    origin: int
    round: Round | None = None


def _own_slots(node: "Node | None") -> dict[str, str]:
    """The CURSOR step's own return fields, empty — what it reads of its
    own last answer before it has given one (the parser lets a prompt
    quote earlier steps' fields and its own).

    Its own only. Blanking every step's fields would make `fill_refs`
    unable to fail: a ref to a step that has not answered is the
    fail-closed guard behind a stepping jump and behind a suspension
    that outlived an edit to an agent's `returns:`, and the message it
    raises is what a handover reports."""
    if not isinstance(node, AgentNode):
        return {}
    return {f"{node.id}.{f}": "" for f in node.return_fields}


def _run_keys(run: RunNode, vals: dict[str, str]) -> list[str]:
    """The rounds a run has now: one, or one per distinct line of its
    list (the list's CURRENT value — a re-plan changes it)."""
    if run.each is None:
        return [""]
    lines = vals.get(run.each[1], "").splitlines()
    return list(dict.fromkeys(k for line in lines if (k := line.strip())))


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
    ) -> None:
        self.app = spec.app
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
        # episodes are granted them by name.
        self.landmarks: dict[str, Landmark] = landmarks or {}
        # The boot's activation (menu, parse_task, build) — what its
        # `select` step runs; None on every other walk — and the
        # baton that step hands on: the program the conductor drives
        # once this walk goes quiet.
        self.activation = activation
        self.baton: "Program | None" = None
        # The route the cursor walks: one slot per node — the spec's
        # nodes, with each `run` replaced by its rounds' nodes as the
        # cursor reaches it (`_expand`); a slot knows the spec index it
        # came from (the cursor a suspension stores) and its round.
        self.slots: list[Slot] = [Slot(n, i) for i, n in enumerate(spec.nodes)]
        self.idx = 0
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
        self.ledger = Ledger(ref=ref, nodes=len(spec.nodes), task=values)
        # The screen/verdict the current step works from — every path
        # observes one before acting — and the frame the same result
        # carried (None when the read had no image), what a model call
        # sees beside the listing.
        self.screen: Screen | None = None
        self.frame: ImageBlock | None = None
        self.verdict: Verdict | None = None
        # Whether the fired payment's daily-log line landed (the amount
        # itself is the ledger's).
        self._paid_logged = False
        # The step executor at the cursor (a resume pre-step rides the
        # same slot before the walk proper opens).
        self._step: Step | None = None
        # Recovery (`recover.py`): the hand in flight and the actions
        # spent per target page (their sum is the walk-wide count).
        self._recovery: recover.State | None = None
        self._page_recoveries: Counter[str] = Counter()
        # A restored walk's stored cursor and whether it is a wake's
        # suspension — `_restore` says what each buys.
        # Where a restored walk resumed, as a POSITION (spec index, the
        # round's key when inside one, the offset within it) — never a
        # route index, which expansions, finished rounds and revisions
        # all move. `_floor` resolves it against the route as it stands.
        self._resume_pos: tuple[int, str | None, int] | None = None
        self._from_suspension = False
        self._unlocked = False  # the resume unlock, once
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
        # settles, backward when a recover hand restarted the route —
        # WITHOUT opening the next step, so nothing of it (a payment's
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
                self.idx + 1,
                "awaiting reply" if self.gate.awaiting else "walk",
            )

    @property
    def node(self) -> "Node | None":
        """The node at the cursor — None once the walk is past the last."""
        return self.slots[self.idx].node if self.idx < len(self.slots) else None

    def _round_at(self, idx: int) -> Round | None:
        return self.slots[idx].round if 0 <= idx < len(self.slots) else None

    def _span(self, rd: Round) -> tuple[int, int]:
        """A round's first and last route index, inclusive."""
        at = [j for j, s in enumerate(self.slots) if s.round is rd]
        return at[0], at[-1]

    def _route_index(self, spec_idx: int) -> int:
        """The route index of a spec node — where the cursor lands for a
        spec-level position once runs before it have expanded."""
        return next(
            (j for j, s in enumerate(self.slots) if s.origin >= spec_idx),
            len(self.slots),
        )

    def label(self) -> str:
        """Where the walk stands, for logs and the stepping driver:
        `node (i/n)`, a round's node under its run and key."""
        n = len(self.slots)
        if self.idx >= n:
            return f"(end, {n}/{n})"
        return f"{self._node_id()} ({self.idx + 1}/{n})"

    @property
    def _recoveries(self) -> int:
        return sum(self._page_recoveries.values())

    def position(self) -> tuple[int, str | None, int]:
        """Where the cursor stands, in terms a route change cannot move:
        the SPEC index, the round's key, and the offset inside it. A
        route index is not that — expansions, finished rounds and
        revisions all shift it, so two different nodes can wear one
        number (`_resume_pos` is stored this way for the same reason)."""
        if self.idx >= len(self.slots):
            return (len(self.spec.nodes), None, 0)
        rd = self._round_at(self.idx)
        if rd is None:
            return (self.slots[self.idx].origin, None, 0)
        return (self.slots[self.idx].origin, rd.key, self.idx - self._span(rd)[0])

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
        origin, key, at = self.position()
        return {
            "schema": SUSPENDED_SCHEMA,
            "app": self.app,
            "playbook": self.spec.name,
            "idx": origin,
            "round": {"key": key, "at": at} if key is not None else None,
            "label": self.label(),
            "values": self.values,
            **self.thread.to_suspended(),
            **self.ledger.to_suspended(),
            **self.gate.to_suspended(),
        }

    def _restore(self, data: dict, *, resumed: bool) -> None:
        """Overlay one projection (`state()`'s shape). The stored cursor
        is where the walk opens either way (the next node's own checks
        judge whether the world still fits). `resumed` marks a WAKE's
        suspension, which buys two more things: the cursor is also the
        floor no recovery restarts below (the nodes before it ran on an
        earlier wake — an ask answered — and re-running them per wake
        would loop across wakes with a fresh recovery budget each time),
        and the opening read may unlock a locked phone once. A stepping
        position keeps the fresh walk's rules: a recover hand that does
        not restore the page walks again from the top — exactly what
        the author steps to see — and the tool's own preamble wakes the
        phone."""
        idx = int(data["idx"])
        if not (0 <= idx <= len(self.spec.nodes)):
            # The spec changed under the suspension (edited shorter) — a
            # stale cursor must not fake a completion. Raising drops the
            # suspension (load_suspended is fail-open).
            raise PlaybookError(f"suspended idx {idx} is outside the playbook")
        self._from_suspension = resumed
        self.ledger.restore(data)
        # A restored payment was logged before it was persisted
        # (`suspend`), so the latch closes with it: the purchase line
        # is written once per fire, never once per wake carrying it.
        self._paid_logged = self.ledger.paid is not None
        self.thread.restore(data)
        self.gate = Gate.from_suspended(data)
        # The stored cursor is a SPEC index; inside a run's round it
        # comes with the round's key and offset, and the run is expanded
        # again from the restored record (a finished round stays out,
        # the same rule as live) to find that round's place.
        self.idx = idx
        inside = data.get("round")
        if (
            inside
            and idx < len(self.slots)
            and isinstance(self.slots[idx].node, RunNode)
        ):
            key, at = str(inside.get("key", "")), int(inside.get("at", 0))
            rd = next((r for r in self._expand(idx) if r.key == key), None)
            if rd is None:
                raise PlaybookError(f"suspended round {key!r} is not in the run")
            start, end = self._span(rd)
            if not 0 <= at <= end - start:
                # The sub-playbook was edited shorter under the
                # suspension. A stale offset must not fake a position —
                # it would seat the cursor in the NEXT round, past the
                # run, or on a trailing `tell` that reports work nobody
                # did. Raising drops the suspension (fail-open).
                raise PlaybookError(
                    f"suspended round {key!r} offset {at} is outside it"
                )
            self.idx = start + at
        self._resume_pos = self.position()

    @property
    def _floor(self) -> int:
        """The cursor no recovery restarts below (`_restore`) — a wake's
        suspension only; a stepping position keeps the fresh walk's rule."""
        return self._resume_index() if self._from_suspension else 0

    def _resume_index(self) -> int:
        """The restored cursor on today's route: the node inside its
        round while that round is still on the route, else the first
        slot of the spec index it came from (a finished round is gone
        by then). 0 for a walk that restored nothing."""
        if self._resume_pos is None:
            return 0
        origin, key, at = self._resume_pos
        if key is not None:
            rd = next(
                (
                    s.round
                    for s in self.slots
                    if s.round is not None and s.origin == origin and s.round.key == key
                ),
                None,
            )
            if rd is not None:
                return self._span(rd)[0] + at
        return self._route_index(origin)

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
            write_json_atomic(suspended_path(), self.state())
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
        rd = self._round_at(self.idx)
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
        kind = pending.kind
        if kind == "suspend":
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
        # One reading, one verdict: channel-facing actions (declared at
        # the synth site) match against the thread; everything else
        # against the pack's own pages.
        self.screen = views.screen_of(result)
        self.frame = views.frame_of(result)
        self.verdict = match_screen(
            self.screen,
            self.channel.prints if pending.channel and self.channel else self.prints,
        )
        log.info(
            "conductor: %s/%s read after %s — %s",
            self.app,
            self.spec.name,
            kind,
            self.verdict.describe(),
        )
        self.record.read(
            kind, self.node.id if self.node is not None else None, self.verdict
        )
        if (
            self.phase is Phase.OPENING
            and self._from_suspension
            and not self._unlocked
            and self.verdict.matches(LOCKED_ID)
        ):
            # The resumed walk's first reading is the cover: wake the
            # phone once, then open again exactly as before.
            self._unlocked = True
            return self.synth(
                KIND_UNLOCK,
                "conductor: phone is locked on resume — unlocking",
                gesture_vocab.UNLOCK_PHONE,
                {},
            )
        if kind == KIND_UNLOCK:
            return self._opening()
        if kind == "peek":
            # Past the settled pure-text prefix, never below a restored
            # walk's stored cursor (`_restore`).
            self.phase = Phase.LIVE
            self.idx = max(
                self._route_index(self.spec.first_unsettled(self.outputs)),
                self._resume_index(),
            )
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
            node = self.node
            if not isinstance(node, AskNode):
                return self.handover(
                    "suspended awaiting a reply, but no ask at the cursor"
                )
            self._step = AskStep(self, node)
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
        if self.idx < len(self.slots) and isinstance(
            self.slots[self.idx].node, RunNode
        ):
            # The run's rounds take its place in the route, and the
            # cursor walks the first of them — or whatever follows when
            # every round is already done.
            self._expand(self.idx)
            return self.next()
        nodes = [s.node for s in self.slots]
        if self.idx >= len(nodes):
            # The task is the playbook's and it is done: its `tell` already
            # reported to the user and its record wrote the runs row, so
            # the walk closes the session DONE itself (handing the model a
            # "wrap up" bought four turns of note-taking over a full
            # context). The close is a step (`step_close.py`): one call in
            # the session's thread for the record, then `close_done`.
            self._step = CloseStep(self)
            return self._step.open()
        if self.step_one:
            here = self.position()
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
        node = nodes[self.idx]
        self._step = _STEP_FOR[type(node)](self, node)
        return self._step.open()

    def advance_cursor(self) -> Turn:
        """The step at the cursor is done — walk the next node. The ONE
        way the cursor moves forward. Leaving a round's last node
        records the round: its returns, or the miss."""
        rd = self._round_at(self.idx)
        self.idx += 1
        self._step = None
        if rd is not None and self._round_at(self.idx) is not rd:
            self._finish_round(rd)
            self._drop_round(rd)
        return self.next()

    def _drop_round(self, rd: Round) -> None:
        """A round that is done or missed leaves the route: its record
        is the ledger's, and the route reads as a resumed one would (a
        finished round is never expanded again), so a restart from the
        top can never walk it twice. The cursor lands on what followed."""
        start, end = self._span(rd)
        del self.slots[start : end + 1]
        self.ledger.nodes = len(self.slots)
        self.idx = start

    def _node_id(self) -> str | None:
        """Where the cursor stands, as a line of OUR prose — a brief and
        a day line both print it as a sentence of the conductor's. A
        round's key is a line of an agent's answer, read off a screen
        whose text a seller writes, so it is clipped here; the prefix it
        comes from stays verbatim, being an identity the ledger is
        looked up by."""
        node = self.node
        if node is None:
            return None
        rd = self._round_at(self.idx)
        if rd is None:
            return node.id
        return f"{round_prefix(rd.run.id, clip(rd.key, 60))}/{node.id}"

    def _recovers(self) -> dict[str, Recovery]:
        """The declared hands the cursor's page is under — the run
        playbook's inside a round, else this route's."""
        rd = self._round_at(self.idx)
        return rd.run.sub.recovers if rd is not None else self.spec.recovers

    # ---- what the steps read and call ----

    def ref_values(self) -> dict[str, str]:
        """Ref-resolution values, keyed by the dotted ref spellings: the
        walk's inputs under `inputs.<name>`, every agent output under
        `node.field` (the one read of an unrecorded one a text can make
        is a step's own: its last answer, or empty), every run's returns under `run.field` (its
        finished rounds' values, one per line — empty before any), and
        the gate's `ask.replies` (the replies read so far, one per line —
        empty before any). Inside a run's round: that round's inputs and
        its own record, nothing of the route around it. The roots can
        never collide: the parser reserves `inputs` as a move id."""
        rd = self._round_at(self.idx)
        if rd is not None:
            return self._round_values(rd)
        vals = {
            **_own_slots(self.node),
            **self.ledger.previous,
            **self._input_vals,
            **self.outputs,
        }
        for run in self.spec.runs:
            keys = _run_keys(run, vals)
            for fld in run.sub.returns:
                vals[f"{run.id}.{fld}"] = self._run_return(run, fld, keys)
        vals["ask.replies"] = "\n".join(self.gate.replies)
        return vals

    def _round_values(self, rd: Round) -> dict[str, str]:
        """A round's refs: its inputs, its own record, the replies."""
        return {
            **_own_slots(self.node),
            **rd.inputs,
            **self.ledger.round_values(rd.prefix),
            "ask.replies": "\n".join(self.gate.replies),
        }

    def _run_return(self, run: RunNode, fld: str, keys: list[str]) -> str:
        """A run's return: its done rounds' values, in list order, one
        per line — a missed round has no line (its reason is the
        ledger's), so a report never lists what was not done."""
        lines = (self.ledger.round_return(round_prefix(run.id, k), fld) for k in keys)
        return "\n".join(v for v in lines if v)

    def _expand(self, i: int) -> list[Round]:
        """Replace the `run` at route index `i` by its rounds' nodes —
        the rounds still to do: every key of its list (or the one round
        of a plain run) whose record is not in the ledger. Returns them;
        raises PlaybookError when the run cannot expand (the guarded
        callers hand over on it)."""
        run = self.slots[i].node
        assert isinstance(run, RunNode)
        values = self.ref_values()
        keys = _run_keys(run, values)
        # `rounds:` bounds the WORK, not one reading of the list: a
        # revision re-plans and the run expands again, so counting only
        # today's items would hand each re-plan a fresh budget. Rounds
        # already on record count — a finished one is work this run did.
        todo = [
            k for k in keys if not self.ledger.round_finished(round_prefix(run.id, k))
        ]
        total = self.ledger.round_count(run.id) + len(todo)
        if total > run.max_rounds:
            raise PlaybookError(
                f"run {run.id!r}: {total} rounds, more than its {run.max_rounds}"
            )
        rounds: list[Round] = []
        for key in todo:
            # A ref that is empty BY DESIGN — a run's returns before any
            # round ends, an `each` whose rounds all missed — is not a
            # value: left out, so the sub's declared `default:` covers
            # it, and a required input fed nothing fails closed.
            provided = {
                k: str(v)
                for k, v in fill_args(run.args, values, f"run {run.id!r}").items()
                if str(v) != ""
            }
            if run.each is not None:
                provided[run.each[0]] = key
            try:
                resolved = resolve_inputs(run.sub, provided)
            except PlaybookError as e:
                raise PlaybookError(f"run {run.id!r}: {e}") from e
            rounds.append(
                Round(run, key, {f"inputs.{n}": v for n, v in resolved.items()})
            )
        origin = self.slots[i].origin
        self.slots[i : i + 1] = [
            Slot(n, origin, rd) for rd in rounds for n in run.sub.nodes
        ]
        self.ledger.nodes = len(self.slots)
        return rounds

    def revise(self, replies: str) -> Turn:
        """A reply the ask's words missed re-plans the walk, when the
        enclosing `run` says `revise:` and its revisions are not spent:
        the reply joins `{ask.replies}`, the route from the named agent
        on goes back to its spec shape (a run in progress re-expands
        later; a finished round keeps its record and is not walked
        again), and the cursor re-runs that agent."""
        rd = self._round_at(self.idx)
        if rd is None or rd.run.revise is None:
            return None
        if self.gate.revisions >= rd.run.revise_limit:
            return None
        target = [n.id for n in self.spec.nodes].index(rd.run.revise)
        at = self._route_index(target)
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
        opened = {id(s.round): s.round for s in self.slots[at:] if s.round is not None}
        for rd_open in opened.values():
            if not self.ledger.round_finished(rd_open.prefix):
                self.ledger.rounds.pop(rd_open.prefix, None)
        self.slots[at:] = [
            Slot(n, j) for j, n in enumerate(self.spec.nodes[target:], target)
        ]
        # The target's answer is no longer a decision (a restart would
        # open past a pure-text agent with one on record) but stays its
        # last answer, which its own prompt re-reads.
        self.ledger.unsettle(rd.run.revise)
        self.ledger.nodes = len(self.slots)
        if self._resume_pos is not None and self._resume_pos[0] >= target:
            # The floor never stands above the node the walk re-runs from.
            self._resume_pos = (target, None, 0)
        self._recovery = None
        self._step = None
        self.idx = at
        return self.next()

    def _word_at_cursor(self, explicit: str | None) -> str | None:
        """The exit word a failure at the cursor takes. Inside a run's
        round, the composing route's word first — the run's `miss:`,
        then its `on_fail:` — over the playbook's own, which holds when
        it walks alone. Then the one the caller resolved (a page's own,
        in `recover_or_handover`), then the cursor node's `on_fail`."""
        rd = self._round_at(self.idx)
        if rd is not None and (rd.run.miss or rd.run.on_fail):
            return rd.run.miss or rd.run.on_fail
        if explicit is not None:
            return explicit
        node = self.node
        return node.on_fail if node is not None else None

    def _skip_round(self, rd: Round, reason: str) -> Turn:
        log.warning("conductor: round %s missed — %s", rd.prefix, reason)
        self.journal(f"round {rd.prefix} missed — {reason}")
        self.ledger.round_missed(rd.prefix, reason)
        self._recovery = None
        self._step = None
        self._drop_round(rd)
        return self.next()

    def _finish_round(self, rd: Round) -> None:
        """A round's last node settled: its returns, filled from its own
        record, land under its prefix (a template that cannot fill
        raises, and the guarded caller hands over)."""
        values = self._round_values(rd)
        self.ledger.round_done(
            rd.prefix,
            {
                f: str(fill_refs(t, values, where=f"{rd.run.playbook} `returns.{f}`"))
                for f, t in rd.run.sub.returns.items()
            },
        )

    def mismatch(self, verdict: Verdict, expected_id: str) -> str | None:
        """None when the verdict is a match on the full `expected_id`
        (`app.page`); else a short reason — pack pages and the channel
        thread judged by one spelling."""
        if verdict.matches(expected_id):
            return None
        gap = verdict.gaps.get(expected_id)
        if gap is not None:
            # The one clause that matters: what the EXPECTED page lacked,
            # not every candidate's gap.
            return f"screen reads as unknown — {page_name(expected_id)} {gap}"
        return f"screen reads as {verdict.describe()}"

    def money_page_block(self, what: str) -> str | None:
        """A payment fires only off a VERIFIED own-pack page — the move
        once, a payment episode before each of its taps: the ask left
        the phone on the IM thread, and an unverified screen could
        satisfy the predicates with the conductor's own ask bubble, or
        with whatever a screen the pack never declared happens to print.
        None when the current verdict is such a page; else the handover
        reason. (The ask itself reads its total off the exact waypoint
        before it — `AskNode.enter`.)"""
        v = self.verdict
        if (
            v is not None
            and v.kind is Reading.MATCH
            and owned_by(v.page_id or "", self.app)
        ):
            return None
        return (
            f"{what}: current screen is not a verified {self.app} page — "
            "money never reads or fires blind"
        )

    def spend_consent(self) -> None:
        """A payment move fires: consent is consumed, the amount survives
        into the history line and the purchase log. A later action of
        the same payment episode finds the consent already spent and
        leaves the record alone."""
        amount = self.gate.spend()
        if amount is not None:
            self.ledger.pay(amount)
            self._paid_logged = False

    def log_purchase(self) -> None:
        """The doctrine's purchase line, harness-written ONCE as soon as
        a fired payment's result lands, fails, or the session dies —
        whatever the next check says, money may have moved, and the
        daily log is the cross-wake record. Idempotent: nothing new to
        log is a no-op."""
        if self.ledger.paid is None or self._paid_logged:
            return
        self._paid_logged = True
        self.log_day(
            f"conductor: {self.app}: payment ¥{money.plain(self.ledger.paid)} fired "
            f"(playbook {self.ref}) — verify the order before "
            "paying again"
        )

    def enter_gate(self, node: Checked) -> Turn:
        """The one enter-page guard moves, acting agents, and the boot's
        activate share: None when the node has no enter (a `start` runs
        unconditionally) or the page reads; else the recovery/handover
        step."""
        if not node.enter:
            return None
        assert self.verdict is not None
        expected = page_id(self.app, node.enter)
        wrong = self.mismatch(self.verdict, expected)
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
            rd = self._round_at(self.idx)
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
                node=self._node_id(),
                idx=self.idx,
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
        moved: after a fired payment a stop leaves the order unverified."""
        node = self._node_id() or "(end)"
        spent = (
            f"a payment of ¥{money.plain(self.ledger.paid)} fired, unverified"
            if self.ledger.paid is not None
            else "nothing paid"
        )
        # A stop's money clause is a WARNING, not a tally: it says the
        # order is unverified, so it is worded here, not by `recap`.
        recap = "; ".join(
            [
                f"{self.ref} stopped at {node} — {reason}",
                *self.ledger.account(),
                spent,
            ]
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
        with consent bound, mid-gate, for an irreversible move, or once
        a payment fired: money keeps the hard handover (a restart from
        the top would walk back into the ask and pay again)."""
        recovery = self._recovers().get(page_name(expected_id))
        # The page's own word once its hand is spent (or it has none) —
        # either spelling, since a page saying `handover` under a node
        # saying `stop` means exactly that; unsaid, the cursor node's
        # word decides in `handover`.
        fail = partial(
            self.handover, word=recovery.on_fail if recovery is not None else None
        )
        if (
            self.gate.consented is not None
            or self.gate.awaiting
            or node.irreversible
            or self.ledger.paid is not None
        ):
            return fail(reason)
        if not owned_by(expected_id, self.app):
            # Recovery covers this pack's own pages only — a reserved or
            # channel target has no hand to declare.
            return fail(reason)
        st = recover.State(target=expected_id, mode=mode, reason=reason)
        v = self.verdict
        # The reading the page declared its hands for: the lock screen
        # (taps do not land there — the matcher reads it by shape), the
        # page itself under an overlay, or any other screen.
        reading = READING_ELSEWHERE
        if v is not None and v.matches(LOCKED_ID):
            reading = READING_LOCKED
        elif v is not None and v.occludes(expected_id):
            reading = READING_COVERED
        step = recover.plan(
            self._recoveries,
            recovery,
            self._page_recoveries[expected_id],
            reading=reading,
        )
        if isinstance(step, recover.Exhausted):
            return fail(f"{reason} — {step.reason}")
        # The page's DECLARED hand — the planner decides WHETHER, the
        # walk interprets WHAT: a bare gesture, a landmark tap (at its
        # declared box, exactly), or an argument-less macro.
        hand = step.hand
        note = (
            f"conductor: recovering toward {expected_id} via its declared hand "
            f"({reading})"
        )
        if hand.macro is not None:
            return self._recover_act(
                st,
                note,
                gesture_vocab.RUN_MACRO,
                {"name": qualified_macro(self.app, hand.macro)},
            )
        if hand.tool == "tap":
            landmark = self.landmarks.get(hand.landmark or "")
            if landmark is None:
                return self.handover(
                    f"{reason} (recover landmark {hand.landmark!r} undeclared)"
                )
            return self._recover_act(st, note, "tap", {"bbox": list(landmark.bbox)})
        assert hand.tool is not None
        return self._recover_act(st, note, hand.tool, {})

    def _recover_act(
        self, st: recover.State, note: str, tool: str, args: dict
    ) -> AssistantMessage:
        """The hand's turn: the engagement goes in flight, the page's
        limit and the walk-wide ceiling both count it, and the landing
        dispatch reads `KIND_RECOVER`."""
        self._recovery = st
        self._page_recoveries[st.target] += 1
        return self.synth(KIND_RECOVER, note, tool, args)

    def _recover_landed(self) -> Turn:
        """The hand's result view, judged. Restored → resume exactly
        where the walk stood: an interrupted enter re-checks and runs its
        move; an interrupted verify is satisfied by the restored page
        (the macro already ran — never re-run it). Still off → walk the
        route again from its first unsettled node (the start re-runs,
        which is a force_quit hand's whole point)."""
        st = self._recovery
        assert st is not None and self.verdict is not None
        self._recovery = None
        self._step = None
        if self.verdict.matches(st.target):
            self.journal(f"recovered {st.target} via its declared hand")
            if st.mode is recover.Mode.VERIFY:
                return self.advance_cursor()
            return self.next()
        self.journal(f"recover hand ran — walking again toward {st.target}")
        rd = self._round_at(self.idx)
        if rd is None:
            top = self._route_index(self.spec.first_unsettled(self.outputs))
        else:
            # The same rule inside a round: past the sub-playbook's
            # settled pure-text prefix, judged on the ROUND's record.
            # Re-deriving a recorded answer would silently change it —
            # a second model call, a different search keyword, one item.
            top = self._span(rd)[0] + rd.run.sub.first_unsettled(
                self.ledger.round_values(rd.prefix)
            )
        self.idx = max(top, self._floor)
        return self.next()

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
        node = self._node_id() or "(end)"
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
            idx=self.idx,
            nodes=self.ledger.nodes,
            node=self._node_id(),
            reason=reason,
            micros=self._micros,
            rescues=self._recoveries,
            values=self.values,
            total=self.ledger.paid,
        )
