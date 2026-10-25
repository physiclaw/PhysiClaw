"""Tests for `physiclaw.agent.engine.pitfalls` — the opt-in, append-only
learned-pitfalls list.

The autouse `physiclaw_home` fixture points `paths.HOME` at a per-test tmp dir,
so `paths.pitfalls_dir()` resolves under it.

Accepted mutation survivors:
- Changing the private dedup-key separator from a space to "XX XX" preserves
  equality of whitespace-normalized word sequences; the key is never exposed.
- Decorating doctrine, diagnostic seeds, or corrective prose with "XX" keeps
  the instructions and diagnostic values intact. Tests pin structured headings,
  bullets, counts, and appended trajectory data rather than every prose byte.
"""

from __future__ import annotations

import json

import pytest
from freezegun import freeze_time
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from physiclaw.agent.engine import pitfalls
from physiclaw.agent.engine.session import Session
from physiclaw.agent.runtime.sentinel import DONE, FAIL, IDLE, STUCK, WAIT
from physiclaw.common import config, paths


def _history() -> list[dict]:
    path = paths.pitfalls_dir() / "history.jsonl"
    return [
        json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln
    ]


# ---------- helpers ----------


def test_add_clamps_to_item_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.CONFIG.pitfalls, "max_item_chars", 20)

    pitfalls.add(["x" * 100])

    assert pitfalls.read() == ["x" * 19 + "…"]


@pytest.mark.parametrize(
    "item, expected",
    [("x" * 19, "x" * 19), ("x" * 20, "x" * 20), ("x" * 18 + " yz", "x" * 18 + "…")],
)
def test_add_preserves_items_at_character_limit_and_trims_before_ellipsis(
    monkeypatch, item, expected
) -> None:
    monkeypatch.setattr(config.CONFIG.pitfalls, "max_item_chars", 20)

    pitfalls.add([item])

    assert pitfalls.read() == [expected]


def test_add_drops_blanks_and_normalized_batch_duplicates() -> None:
    result = pitfalls.add([" A ", "a", "", "  ", "B"])

    assert result == {"added": 2, "total": 2}
    assert pitfalls.read() == ["A", "B"]


# ---------- add: append-only, newest on top ----------


def test_add_prepends_newest_on_top() -> None:
    pitfalls.add(["京东: trap one"])
    res = pitfalls.add(["wechat: trap two"])
    assert res["added"] == 1 and res["total"] == 2
    assert pitfalls.read() == ["wechat: trap two", "京东: trap one"]  # newest first


def test_add_caps_to_three_per_call() -> None:
    res = pitfalls.add(["a", "b", "c", "d", "e"])
    assert res["added"] == 3
    assert pitfalls.read() == ["a", "b", "c"]


def test_add_dedups_against_existing() -> None:
    pitfalls.add(["京东: avoid Ai搜索"])
    res = pitfalls.add(
        ["京东: Avoid  AI搜索".replace("AI", "Ai"), "new one"]
    )  # first is a dupe
    assert res["added"] == 1  # only "new one"
    assert pitfalls.read().count("京东: avoid Ai搜索") == 1


def test_add_hard_caps_from_the_bottom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.CONFIG.pitfalls, "max_items", 3)
    pitfalls.add(["a", "b", "c"])  # a on top, c at the bottom (oldest)
    pitfalls.add(["d"])  # prepends d → [d,a,b,c], cut top 3 drops the bottom (c)
    assert pitfalls.read() == ["d", "a", "b"]


def test_add_empty_is_noop() -> None:
    pitfalls.add(["real"])
    res = pitfalls.add(["   ", ""])
    assert res["added"] == 0 and res["total"] == 1


@pytest.mark.parametrize("cap", [1, 2])
def test_regression_add_reports_only_items_retained_under_small_cap(
    monkeypatch: pytest.MonkeyPatch, cap: int
) -> None:
    monkeypatch.setattr(config.CONFIG.pitfalls, "max_items", cap)
    items = ["first trap", "second trap", "third trap"]

    result = pitfalls.add(items)

    assert result == {"added": cap, "total": cap}
    assert pitfalls.read() == items[:cap]
    assert _history()[-1]["items"] == items[:cap]


@pytest.mark.parametrize("writer", [pitfalls.add, pitfalls.replace])
@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_regression_history_append_preserves_record_boundaries(writer, ending) -> None:
    pitfalls.add(["existing trap"])
    history_path = paths.pitfalls_dir() / "history.jsonl"
    before = history_path.read_bytes().rstrip(b"\n") + ending
    history_path.write_bytes(before)

    with freeze_time("2026-10-14T09:30:05"):
        writer(["新的陷阱"])

    records = _history()
    assert len(records) == 2
    assert records[0]["items"] == ["existing trap"]
    assert records[1]["items"] == pitfalls.read()
    assert records[1]["ts"] == "2026-10-14T09:30:05"
    assert history_path.read_bytes().startswith(before)


@pytest.mark.parametrize("writer", [pitfalls.add, pitfalls.replace])
def test_invalid_unicode_does_not_change_pitfalls_or_history(writer) -> None:
    pitfalls.add(["keep this trap"])
    directory = paths.pitfalls_dir()
    before_list = (directory / "pitfalls.md").read_bytes()
    before_history = (directory / "history.jsonl").read_bytes()

    with pytest.raises(UnicodeEncodeError):
        writer(["invalid \ud800"])

    assert (directory / "pitfalls.md").read_bytes() == before_list
    assert (directory / "history.jsonl").read_bytes() == before_history


@pytest.mark.parametrize(
    "writer, zero_field", [(pitfalls.add, "added"), (pitfalls.replace, "dropped")]
)
def test_blank_writes_leave_existing_files_unchanged(writer, zero_field) -> None:
    pitfalls.add(["keep this trap"])
    directory = paths.pitfalls_dir()
    before_list = (directory / "pitfalls.md").read_bytes()
    before_history = (directory / "history.jsonl").read_bytes()

    result = writer(["", " \n\t", "\u2028"])

    assert result == {"total": 1, zero_field: 0}
    assert (directory / "pitfalls.md").read_bytes() == before_list
    assert (directory / "history.jsonl").read_bytes() == before_history


@pytest.mark.parametrize(
    "writer, zero_field", [(pitfalls.add, "added"), (pitfalls.replace, "dropped")]
)
def test_empty_write_without_existing_state_does_not_create_files(
    writer, zero_field
) -> None:
    result = writer([])

    assert result == {"total": 0, zero_field: 0}
    assert not (paths.pitfalls_dir() / "pitfalls.md").exists()
    assert not (paths.pitfalls_dir() / "history.jsonl").exists()


# Each generated example resets both files and both configuration limits.
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    items=st.lists(
        st.text(st.characters(blacklist_categories=("Cs",)), max_size=80), max_size=7
    ),
    cap=st.integers(1, 6),
    char_cap=st.integers(1, 40),
)
def test_add_preserves_normalized_bounded_state_and_truthful_counts(
    monkeypatch, items, cap, char_cap
) -> None:
    monkeypatch.setattr(config.CONFIG.pitfalls, "max_items", cap)
    monkeypatch.setattr(config.CONFIG.pitfalls, "max_item_chars", char_cap)
    directory = paths.pitfalls_dir()
    (directory / "pitfalls.md").unlink(missing_ok=True)
    (directory / "history.jsonl").unlink(missing_ok=True)
    pitfalls.add(["previous trap"])
    before = {item.casefold() for item in pitfalls.read()}

    result = pitfalls.add(items)
    stored = pitfalls.read()

    assert result["added"] == len(
        [item for item in stored if item.casefold() not in before]
    )
    assert 0 <= result["added"] <= 3
    assert result["total"] == len(stored) <= cap
    assert len({item.casefold() for item in stored}) == len(stored)
    assert all(
        item and len(item) <= char_cap and item == " ".join(item.split())
        for item in stored
    )
    assert _history()[-1]["items"] == stored


# ---------- replace: curator path ----------


def test_replace_swaps_list_and_cuts_top(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.CONFIG.pitfalls, "max_items", 2)
    pitfalls.add(["old"])
    res = pitfalls.replace(["x", "y", "z"])  # 3 → cut to top 2
    assert res == {"total": 2, "dropped": 1}
    assert pitfalls.read() == ["x", "y"]


def test_replace_empty_does_not_wipe() -> None:
    pitfalls.add(["keep me"])
    pitfalls.replace([])
    assert pitfalls.read() == ["keep me"]


@pytest.mark.parametrize("items", [["one trap"], ["one trap", "another trap"]])
def test_replace_at_or_below_cap_reports_zero_dropped(monkeypatch, items) -> None:
    monkeypatch.setattr(config.CONFIG.pitfalls, "max_items", 2)

    result = pitfalls.replace(items)

    assert result == {"total": len(items), "dropped": 0}
    assert pitfalls.read() == items


# ---------- history + render ----------


def test_every_write_snapshots_history() -> None:
    pitfalls.add(["a"])
    pitfalls.replace(["a", "b"])
    hist = _history()
    assert [h["op"] for h in hist] == ["add", "curate"]
    assert hist[-1]["items"] == ["a", "b"]


def test_render_section_is_empty_when_no_pitfalls() -> None:
    assert pitfalls.render_section() == ""


def test_render_section_has_header_doctrine_and_items() -> None:
    pitfalls.add(["京东: avoid Ai搜索"])
    out = pitfalls.render_section()
    assert out.startswith("## Learned pitfalls")
    assert "Re-ground any mentioned coordinates" in out
    assert "- 京东: avoid Ai搜索" in out


def test_render_section_keeps_heading_paragraph_and_bullet_structure() -> None:
    result = pitfalls.render_section(["first trap", "第二个陷阱"])

    lines = result.splitlines()
    assert lines[0] == "## Learned pitfalls"
    assert lines[1] == lines[3] == ""
    assert "relevant to this app" in lines[2]
    assert lines[4:] == ["- first trap", "- 第二个陷阱"]


def test_read_skips_documentation_and_empty_bullets() -> None:
    directory = paths.pitfalls_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pitfalls.md").write_text(
        "# Learned pitfalls\n\n- first trap\nlegacy prose\n- \n  - second trap  \n",
        encoding="utf-8",
    )

    result = pitfalls.read()

    assert result == ["first trap", "second trap"]


@pytest.mark.parametrize("snapshot", [[], ["explicit snapshot"]])
def test_render_section_uses_supplied_snapshot_without_rereading(snapshot) -> None:
    directory = paths.pitfalls_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pitfalls.md").write_bytes(b"\xff")

    result = pitfalls.render_section(snapshot)

    assert result.count("- explicit snapshot") == len(snapshot)
    assert bool(result) == bool(snapshot)


@pytest.mark.parametrize(
    "trajectory", ["", "[t3] Plan: retry\n[t7] Scratchpad: resolved"]
)
def test_corrective_names_capture_then_close_and_preserves_trajectory(
    trajectory,
) -> None:
    result = pitfalls.corrective("57 turns, closing DONE", trajectory)

    assert "57 turns, closing DONE" in result
    assert result.index("add_pitfall") < result.index("end_session")
    assert "empty list" in result
    assert result.count("[t3] Plan: retry\n[t7] Scratchpad: resolved") == bool(
        trajectory
    )

    assert result.endswith(trajectory)


def test_corrective_without_trajectory_matches_explicit_empty_trajectory() -> None:
    result = pitfalls.corrective("57 turns, closing DONE")

    assert result == pitfalls.corrective("57 turns, closing DONE", "")


# ---------- should_capture (the pre-close gate's predicate) ----------


@pytest.mark.parametrize(
    "status, turn", [(IDLE, 999), (WAIT, 999), (STUCK, 999), (FAIL, 999), (DONE, 49)]
)
def test_should_capture_skips_non_done_or_short_sessions(
    monkeypatch, status, turn
) -> None:
    monkeypatch.setattr(config.CONFIG.pitfalls, "enabled", True)
    monkeypatch.setattr(config.CONFIG.pitfalls, "capture_turn_floor", 50)

    result = pitfalls.should_capture(status, turn, Session())

    assert result == (False, "")


@pytest.mark.parametrize("turn", [50, 51])
@pytest.mark.parametrize("stuck_events", [0, 2])
def test_should_capture_includes_threshold_and_preserves_all_diagnostic_counts(
    monkeypatch, turn, stuck_events
) -> None:
    monkeypatch.setattr(config.CONFIG.pitfalls, "enabled", True)
    monkeypatch.setattr(config.CONFIG.pitfalls, "capture_turn_floor", 50)
    session = Session()
    session.stuck_events = stuck_events

    capture, seed = pitfalls.should_capture(DONE, turn, session)

    assert capture is True
    assert f"{turn} turns" in seed
    assert (f"loop-guard fired {stuck_events}×" in seed) == bool(stuck_events)
    assert "DONE" in seed


def test_should_capture_disabled(monkeypatch) -> None:
    monkeypatch.setattr(config.CONFIG.pitfalls, "enabled", False)

    result = pitfalls.should_capture(DONE, 999, Session())

    assert result == (False, "")
