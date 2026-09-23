"""Declared recovery — a page's `recover:` hand, and nothing else.

Pure policy over the walk's recovery counts: given a check that needed page P
and did not get it, `plan` names the ONE next action — the hand the
page declared for the reading (`covered`: P under a sheet or popup;
`locked`: the lock screen, read by shape; `elsewhere`: any other
screen; the flat form declares one hand for all three) — or Exhausted,
and the walk hands over. After a hand the walk re-checks on its own
result view and, still off, plans again — at most `tries` times.
Nothing taps, unlocks, or waits in the background.

Two bounds, both visible in the playbook: the page's own `tries:` and
the walk-wide ceiling `MAX_RECOVER_ACTIONS`. `Recoveries` counts both
and holds the one hand in flight; `barred` says when money keeps the
hard handover; `action` turns a declared hand into the tool call the
walk synthesizes. The walk (`program.py`) sequences them.
"""

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from physiclaw.common import gesture_vocab
from physiclaw.conductor.spec.conventions import LOCKED_ID
from physiclaw.conductor.spec.limits import MAX_RECOVER_ACTIONS
from physiclaw.conductor.spec.match import Verdict
from physiclaw.conductor.spec.model import (
    READING_COVERED,
    READING_ELSEWHERE,
    READING_LOCKED,
    Checked,
    RecoverHand,
    Recovery,
)
from physiclaw.conductor.spec.pack import qualified_macro
from physiclaw.conductor.spec.pages import Landmark


class Mode(StrEnum):
    """How the walk continues once the target page is restored."""

    ENTER = "enter"  # re-check the enter, then run the move
    VERIFY = "verify"  # the move already ran — verify is satisfied


@dataclass(frozen=True)
class Hand:
    """The page's declared `recover:` hand — `action` turns it into the
    tool call (a gesture, a landmark tap, a macro)."""

    hand: RecoverHand


@dataclass(frozen=True)
class Exhausted:
    """Nothing declared is left to try — the walk hands over with `reason`."""

    reason: str


Step = Hand | Exhausted


@dataclass(frozen=True)
class State:
    """One recovery in flight: the node whose check needed the page, the
    page the frozen cursor requires, how the walk resumes once it is
    restored, and the ORIGINAL check failure (the exhausted handover
    reports what actually went wrong, not the hand's miss). Owned by the
    walk; cleared on the hand's landing, and what the next try is
    planned from."""

    node: Checked
    target: str  # full `app.page` id the interrupted check needs
    mode: Mode  # the resume rule
    reason: str = ""

    def __post_init__(self) -> None:
        # A typo'd mode must fail loudly, never silently resume as enter.
        if not isinstance(self.mode, Mode):
            raise ValueError(f"unknown recovery mode {self.mode!r}")


def plan(
    actions: int,
    recovery: Recovery | None,
    page_actions: int = 0,
    *,
    reading: str = READING_ELSEWHERE,
) -> Step:
    """The next recovery action: the hand the page declares for this
    `reading` (one of `playbook.RECOVER_READINGS`), within the page's
    own `tries` (`page_actions`, its actions so far) and the walk's
    lifetime ceiling (`actions`)."""
    if actions >= MAX_RECOVER_ACTIONS:
        return Exhausted(f"recovery budget ({MAX_RECOVER_ACTIONS} actions) spent")
    if recovery is None:
        return Exhausted("its page declares no recover")
    if page_actions >= recovery.tries:
        return Exhausted(f"its page's recover tries ({recovery.tries}) spent")
    hand = recovery.hand_for(reading)
    if hand is None:
        return Exhausted(f"its page declares no `{reading}` recover hand")
    return Hand(hand)


def reading_of(verdict: Verdict | None, target: str) -> str:
    """The reading a page declared its hands for: the lock screen (taps
    do not land there — the matcher reads it by shape), the page itself
    under an overlay, or any other screen."""
    if verdict is not None and verdict.matches(LOCKED_ID):
        return READING_LOCKED
    if verdict is not None and verdict.occludes(target):
        return READING_COVERED
    return READING_ELSEWHERE


class Recoveries:
    """The walk's recovery counts and the one hand in flight. Each
    engagement counts against its target page (the page's `tries:`)
    and the walk-wide ceiling (their sum)."""

    def __init__(self) -> None:
        self.inflight: State | None = None
        self._per_page: Counter[str] = Counter()

    @property
    def total(self) -> int:
        """Every hand the walk has run, against the walk-wide ceiling."""
        return sum(self._per_page.values())

    def spent(self, target: str) -> int:
        """The hands run toward `target`, against its page's `tries`."""
        return self._per_page[target]

    def engage(self, st: State) -> None:
        """A hand goes out: in flight, and counted."""
        self.inflight = st
        self._per_page[st.target] += 1

    def land(self) -> State:
        """The hand in flight landed: its state, no longer in flight."""
        st = self.inflight
        assert st is not None
        self.inflight = None
        return st

    def drop(self) -> None:
        """The walk moved elsewhere (a revision, a missed round): no hand
        is in flight any more. The counts stay spent."""
        self.inflight = None


def barred(
    node: Checked, *, consented: float | None, awaiting: bool, paid: float | None
) -> bool:
    """Whether money keeps the hard handover: no hand moves the phone
    beside a bound consent, a held ask, an irreversible move, or a fired
    payment."""
    return (
        consented is not None or awaiting or bool(node.irreversible) or paid is not None
    )


def action(
    hand: RecoverHand, app: str, landmarks: dict[str, Landmark]
) -> tuple[str, dict] | str:
    """The declared hand as the tool call the walk synthesizes — the
    planner decides WHETHER, this says WHAT: an argument-less macro, a
    landmark tap at its declared box exactly, or a bare gesture. A
    string is the reason it cannot run (a landmark the pack does not
    declare)."""
    if hand.macro is not None:
        return gesture_vocab.RUN_MACRO, {"name": qualified_macro(app, hand.macro)}
    if hand.tool == "tap":
        landmark = landmarks.get(hand.landmark or "")
        if landmark is None:
            return f"recover landmark {hand.landmark!r} undeclared"
        return "tap", {"bbox": list(landmark.bbox)}
    assert hand.tool is not None
    return hand.tool, {}
