"""Tests for `physiclaw.conductor.walk.recover` — declared recovery's policy:
the page's declared hand, the budget that bounds it, the money that bars
it, the tool call a hand becomes, and the counts a walk keeps."""

from __future__ import annotations

import pytest

from physiclaw.conductor.spec.limits import MAX_RECOVER_ACTIONS
from physiclaw.conductor.spec.model import DoNode, RecoverHand, Recovery
from physiclaw.conductor.spec.pages import Landmark
from physiclaw.conductor.walk import recover

HAND = RecoverHand(tool="go_back")
RECOVERY = Recovery(covered=HAND, elsewhere=HAND, tries=2)


def test_the_declared_hand_is_the_plan() -> None:
    step = recover.plan(0, RECOVERY)

    assert isinstance(step, recover.Hand) and step.hand is HAND


def test_page_without_a_hand_is_exhausted_at_once() -> None:
    # Nothing recovers in the background: no re-peek, no unlock, no tap.
    step = recover.plan(0, None)

    assert isinstance(step, recover.Exhausted) and "declares no recover" in step.reason


def test_global_budget_wins_over_the_hand() -> None:
    step = recover.plan(MAX_RECOVER_ACTIONS, RECOVERY)

    assert isinstance(step, recover.Exhausted) and "budget" in step.reason


def test_page_limit_is_spent_before_the_walk_budget() -> None:
    step = recover.plan(1, RECOVERY, page_actions=RECOVERY.tries)

    assert isinstance(step, recover.Exhausted) and "recover tries (2)" in step.reason


def test_keyed_hands_follow_the_reading() -> None:
    # A page declaring only an `covered` hand hands over on any other
    # screen — and vice versa: what is declared is what runs.
    dismiss = RecoverHand(tool="tap", landmark="dismiss")
    only_occluded = Recovery(covered=dismiss)

    step = recover.plan(0, only_occluded, reading="covered")
    assert isinstance(step, recover.Hand) and step.hand is dismiss
    step = recover.plan(0, only_occluded, reading="elsewhere")
    assert isinstance(step, recover.Exhausted) and "`elsewhere`" in step.reason
    step = recover.plan(0, only_occluded, reading="locked")
    assert isinstance(step, recover.Exhausted) and "`locked`" in step.reason


def test_the_locked_reading_takes_its_own_hand() -> None:
    # A sleeping phone gets no taps: the hand declared for `locked` is
    # the one that runs there, whatever the elsewhere hand is.
    wake = RecoverHand(tool="unlock_phone")
    keyed = Recovery(elsewhere=HAND, locked=wake)

    step = recover.plan(0, keyed, reading="locked")
    assert isinstance(step, recover.Hand) and step.hand is wake
    assert keyed.hands == (HAND, wake)


def test_state_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown recovery mode"):
        recover.State(node=HAND, target="demo.home", mode="sideways")  # type: ignore[arg-type]


# ---------- barred, action, Recoveries ----------


SEARCH = DoNode(id="search", macro="search", args={}, enter="results", verify="results")
PAY = DoNode(
    id="pay",
    macro="pay",
    args={},
    enter="sheet",
    verify="done",
    irreversible="payment",
)


def _barred(
    node: DoNode = SEARCH,
    *,
    consented: float | None = None,
    awaiting: bool = False,
    paid: float | None = None,
) -> bool:
    return recover.barred(node, consented=consented, awaiting=awaiting, paid=paid)


def test_barred_with_no_money_in_play_is_false() -> None:
    assert _barred() is False


@pytest.mark.parametrize(
    "over",
    [
        {"consented": 45.0},
        {"awaiting": True},
        {"paid": 45.0},
        {"node": PAY},
    ],
    ids=["consent", "awaiting", "paid", "irreversible"],
)
def test_barred_by_money_is_true(over: dict) -> None:
    assert _barred(**over) is True


def test_action_of_a_macro_hand_runs_the_qualified_macro() -> None:
    act = recover.action(RecoverHand(macro="open-app"), "demo", {})

    assert act == ("run_macro", {"name": "demo/open-app"})


def test_action_of_a_landmark_tap_presses_its_declared_box() -> None:
    back = Landmark(label=("back",), bbox=(0.02, 0.05, 0.10, 0.10))

    act = recover.action(
        RecoverHand(tool="tap", landmark="back"), "demo", {"back": back}
    )

    assert act == ("tap", {"bbox": [0.02, 0.05, 0.10, 0.10]})


def test_action_of_an_undeclared_landmark_is_the_reason() -> None:
    act = recover.action(RecoverHand(tool="tap", landmark="back"), "demo", {})

    assert act == "recover landmark 'back' undeclared"


def test_action_of_a_bare_gesture_is_the_gesture() -> None:
    assert recover.action(HAND, "demo", {}) == ("go_back", {})


def test_recoveries_engage_counts_per_page_and_in_total() -> None:
    tally = recover.Recoveries()
    st = recover.State(node=SEARCH, target="demo.results", mode=recover.Mode.ENTER)

    tally.engage(st)
    tally.engage(st)

    assert (tally.spent("demo.results"), tally.spent("demo.home"), tally.total) == (
        2,
        0,
        2,
    )


def test_recoveries_land_returns_the_hand_and_clears_it() -> None:
    tally = recover.Recoveries()
    st = recover.State(node=SEARCH, target="demo.results", mode=recover.Mode.ENTER)
    tally.engage(st)

    landed = tally.land()

    assert (landed, tally.inflight, tally.total) == (st, None, 1)


def test_recoveries_drop_keeps_the_counts() -> None:
    tally = recover.Recoveries()
    tally.engage(
        recover.State(node=SEARCH, target="demo.results", mode=recover.Mode.ENTER)
    )

    tally.drop()

    assert (tally.inflight, tally.total) == (None, 1)
