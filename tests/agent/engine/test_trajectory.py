"""Tests for `physiclaw.agent.engine.trajectory` — the turn-tagged history of
plan + scratchpad states that the reflect corrective feeds the agent.

The mutation pass accepts two cosmetic survivors that wrap reader guidance
or the elision notice in `XX`; ordering, retained entries, and size/count
boundaries are checked independently of those strings.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.agent.engine import trajectory
from physiclaw.agent.engine.session import Session
from physiclaw.common.config import CONFIG


@pytest.fixture(autouse=True)
def enable_pitfalls(monkeypatch):
    monkeypatch.setattr(CONFIG.pitfalls, "enabled", True)


def _drafted_plan(session: Session, understanding: str, steps: list[str]) -> None:
    session.plan.update(
        understanding=understanding,
        steps=[{"content": c, "status": "pending"} for c in steps],
    )


# ---------- Plan.snapshot ----------


def test_plan_snapshot_is_single_line_and_ignores_tip_and_progress() -> None:
    s = Session()
    _drafted_plan(s, "buy milk", ["open app", "search"])
    snap = s.plan.snapshot()
    assert "\n" not in snap
    assert "buy milk" in snap and "open app" in snap and "search" in snap
    # Ticking the turn counter (which changes render()'s tip) must NOT change
    # the snapshot — it moves only on a real re-plan.
    before = s.plan.snapshot()
    for _ in range(30):
        s.plan.tick_turn()
    assert s.plan.snapshot() == before


# ---------- record ----------


def test_record_logs_every_update_progress_even_if_unchanged() -> None:
    # plan_updated=True is an explicit re-plan — capture each call, no dedup.
    s = Session()
    _drafted_plan(s, "u1", ["a"])
    trajectory.record(s, turn=0, plan_updated=True)
    trajectory.record(s, turn=1, plan_updated=True)  # same content, still logged
    assert [t for t, _ in s.plan_log] == [0, 1]  # both calls captured

    _drafted_plan(s, "u2", ["a", "b"])
    trajectory.record(s, turn=5, plan_updated=True)
    assert [t for t, _ in s.plan_log] == [0, 1, 5]


def test_record_safety_net_logs_changed_plan_without_update_deduped() -> None:
    # Without plan_updated (e.g. the closing turn) a drafted plan that changed
    # is logged once, deduped — so the final state still reaches the trajectory.
    s = Session()
    _drafted_plan(s, "u1", ["a"])
    trajectory.record(s, turn=2)  # no update_progress this turn
    trajectory.record(s, turn=3)  # unchanged → not re-logged
    assert [t for t, _ in s.plan_log] == [2]


def test_record_skips_undrafted_plan_and_blank_scratchpad() -> None:
    s = Session()  # default plan is undrafted, scratchpad ""
    trajectory.record(s, turn=3)  # no update_progress, plan undrafted
    assert s.plan_log == [] and s.scratchpad_log == []

    s.scratchpad = "   "
    trajectory.record(s, turn=4)
    assert s.scratchpad_log == []  # whitespace-only never logged


def test_record_logs_scratchpad_changes() -> None:
    s = Session()
    s.scratchpad = "v1"
    trajectory.record(s, turn=0)
    trajectory.record(s, turn=1)  # unchanged, no write → safety net skips
    s.scratchpad = "v2"
    trajectory.record(s, turn=2)
    assert s.scratchpad_log == [(0, "v1"), (2, "v2")]


def test_record_logs_every_scratchpad_write_even_if_unchanged() -> None:
    # scratchpad_written=True is an explicit re-write — capture each, no dedup.
    s = Session()
    s.scratchpad = "same"
    trajectory.record(s, turn=0, scratchpad_written=True)
    trajectory.record(s, turn=1, scratchpad_written=True)  # same content, still logged
    assert s.scratchpad_log == [(0, "same"), (1, "same")]

    # A write that clears to blank is not logged (nothing to reflect on).
    s.scratchpad = "   "
    trajectory.record(s, turn=2, scratchpad_written=True)
    assert [t for t, _ in s.scratchpad_log] == [0, 1]


def test_record_caps_retained_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    from physiclaw.common import config

    monkeypatch.setattr(config.CONFIG.engine, "trajectory_max_snapshots", 3)
    s = Session()
    for i in range(6):
        s.scratchpad = f"v{i}"
        trajectory.record(s, turn=i)
    # Only the newest 3 retained (oldest evicted).
    assert [text for _, text in s.scratchpad_log] == ["v3", "v4", "v5"]


# ---------- render ----------


def test_render_empty_when_no_history() -> None:
    assert trajectory.render(Session()) == ""


def test_render_merges_into_one_ascending_timeline() -> None:
    s = Session()
    _drafted_plan(s, "u1", ["a"])
    trajectory.record(s, turn=0)  # plan @ t0
    _drafted_plan(s, "u2", ["a", "b"])
    trajectory.record(s, turn=9)  # plan @ t9
    s.scratchpad = "notes-late"
    trajectory.record(s, turn=9, scratchpad_written=True)  # scratchpad @ t9

    out = trajectory.render(s)
    assert out.startswith("<session-trajectory>") and out.endswith(
        "</session-trajectory>"
    )
    # One merged timeline, ascending by turn regardless of kind; every entry is
    # `[tN] <kind>\n<content>`.
    assert (
        out.index("[t0] plan\n")
        < out.index("[t9] plan\n")
        < out.index("[t9] scratchpad\n")
    )
    assert out.index("u1") < out.index("u2")  # oldest→newest


def test_render_elides_over_budget_and_says_so() -> None:
    s = Session()
    for i in range(8):
        s.scratchpad = f"snapshot-{i}-" + "x" * 200
        trajectory.record(s, turn=i, scratchpad_written=True)

    out = trajectory.render(s, budget=400)
    assert "elided" in out
    # Newest survives even under a tight budget; the oldest is dropped.
    assert "snapshot-7-" in out and "snapshot-0-" not in out


def test_render_applies_count_cap_from_newest_keeping_full_text() -> None:
    # max_count stops accumulation from the newest backward, regardless of type;
    # every kept entry retains its FULL text (no mid-entry trimming).
    s = Session()
    for i in range(6):
        s.scratchpad = f"note-{i}-" + "y" * 50  # each well under budget
        trajectory.record(s, turn=i, scratchpad_written=True)

    out = trajectory.render(s, budget=10**9, max_count=2)
    assert "(4 earlier entries elided to fit)" in out
    # Newest 2 kept in full; older 4 elided.
    assert ("note-5-" + "y" * 50) in out and ("note-4-" + "y" * 50) in out
    assert "note-3-" not in out


def test_render_edge_entry_is_never_trimmed() -> None:
    # The oldest KEPT entry that sits right at the budget edge keeps full text.
    s = Session()
    s.scratchpad = "A" * 300  # oldest
    trajectory.record(s, turn=0, scratchpad_written=True)
    s.scratchpad = "B" * 300  # newest
    trajectory.record(s, turn=1, scratchpad_written=True)

    # Budget fits both — the edge (oldest) entry appears in full, not sliced.
    out = trajectory.render(s, budget=10**9, max_count=300)
    assert "A" * 300 in out and "B" * 300 in out


@pytest.mark.parametrize("budget, expected_turns", [(42, [1]), (43, [0, 1])])
def test_render_counts_entry_headers_at_exact_budget_boundary(
    budget, expected_turns
) -> None:
    # Each [tN] scratchpad header is 16 characters, plus five/six for the body.
    session = Session(scratchpad_log=[(0, "first"), (1, "second")])

    text = trajectory.render(session, budget=budget, max_count=10)

    turns = [int(t) for t in re.findall(r"^\[t(\d+)\] scratchpad$", text, re.MULTILINE)]
    assert turns == expected_turns


def test_render_stops_at_oversize_entry_without_skipping_to_older_history() -> None:
    session = Session(scratchpad_log=[(0, "old"), (1, "x" * 100), (2, "new")])

    text = trajectory.render(session, budget=60, max_count=10)

    assert re.findall(r"\[t(\d+)\]", text) == ["2"]
    assert "(2 earlier entries elided to fit)" in text


def test_render_charges_cumulative_size_across_three_entries() -> None:
    session = Session(scratchpad_log=[(0, "a"), (1, "b"), (2, "c")])

    text = trajectory.render(session, budget=34, max_count=10)

    assert re.findall(r"\[t(\d+)\]", text) == ["1", "2"]


@pytest.mark.parametrize("max_count, expected_turns", [(2, [2, 3]), (4, [0, 1, 2, 3])])
def test_render_orders_interleaved_plan_and_scratchpad_history(
    max_count, expected_turns
) -> None:
    session = Session(
        plan_log=[(1, "first plan"), (3, "later plan")],
        scratchpad_log=[(0, "old notes"), (2, "new notes")],
    )

    text = trajectory.render(session, budget=1000, max_count=max_count)

    assert [int(t) for t in re.findall(r"\[t(\d+)\]", text)] == expected_turns


def test_render_describes_one_elided_entry_in_singular() -> None:
    session = Session(scratchpad_log=[(0, "old"), (1, "new")])

    text = trajectory.render(session, budget=1000, max_count=1)

    assert "(1 earlier entry elided to fit)" in text


@given(
    values=st.lists(
        st.text(alphabet="abcdefgh", min_size=1, max_size=100), min_size=1, max_size=30
    ),
    max_count=st.integers(min_value=1, max_value=30),
    budget=st.integers(min_value=0, max_value=1000),
)
def test_render_keeps_ordered_newest_suffix_and_full_latest_entry(
    values, max_count, budget
) -> None:
    session = Session(scratchpad_log=list(enumerate(values)))

    text = trajectory.render(session, budget=budget, max_count=max_count)

    turns = [int(t) for t in re.findall(r"^\[t(\d+)\] scratchpad$", text, re.MULTILINE)]
    assert 1 <= len(turns) <= min(max_count, len(values))
    assert turns == list(range(len(values) - len(turns), len(values)))
    assert f"[t{len(values) - 1}] scratchpad\n{values[-1]}\n" in text
    assert ("elided" in text) == (len(turns) < len(values))
