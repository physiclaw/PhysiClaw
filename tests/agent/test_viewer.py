"""Tests for `physiclaw.agent.trace.viewer` — the session viewer's model
and page."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from physiclaw.agent.trace import viewer

JPEG = b"\xff\xd8\xff\xd9"


def _lines(*records: dict) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


def _text(text: str, *, marked: bool = False) -> dict:
    block = {"type": "text", "text": text}
    if marked:
        block["cache_control"] = {"type": "ephemeral"}
    return block


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    d = tmp_path / "20260908-100000-abc123"
    (d / "images").mkdir(parents=True)
    (d / "images" / "100001_000_t0.jpg").write_bytes(JPEG)
    first = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [_text("go", marked=True)]},
    ]
    second = [
        first[0],
        {"role": "user", "content": [_text("go")]},  # the mark moved on
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "peek", "arguments": "{}"}}
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": [
                _text("Screen", marked=True),
                {"type": "image_url", "image_url": {"url": "images/100001_000_t0.jpg"}},
            ],
        },
    ]
    rewritten = [first[0], {"role": "user", "content": "summary of what went before"}]
    (d / "events.jsonl").write_text(
        _lines(
            {"t": "2026-09-08T10:00:00.000", "event": "env", "physiclaw": "0.5.0"},
            {
                "t": "2026-09-08T10:00:00.100",
                "event": "request",
                "turn": 0,
                "message_count": 2,
            },
            {
                "t": "2026-09-08T10:00:01.000",
                "event": "response",
                "turn": 0,
                "finish_reason": "tool_calls",
                "tool_calls": [{"id": "c1", "name": "peek", "arguments": {}}],
            },
            {
                "t": "2026-09-08T10:00:01.500",
                "event": "tool_result",
                "turn": 0,
                "name": "peek",
                "text": "Screen</script><b>",
                "images": ["images/100001_000_t0.jpg"],
            },
            {
                "t": "2026-09-08T10:00:01.600",
                "event": "walk_read",
                "app": "a",
                "playbook": "b",
                "after": "peek",
                "node": None,
                "verdict": "home",
            },
            {
                "t": "2026-09-08T10:00:02.100",
                "event": "request",
                "turn": 1,
                "message_count": 4,
            },
            {
                "t": "2026-09-08T10:00:02.150",
                "event": "bad_turn_shape",
                "turn": 1,
                "tool_calls": ["peek"],
            },
            {
                "t": "2026-09-08T10:00:03.100",
                "event": "request",
                "turn": 2,
                "message_count": 2,
            },
        )
    )
    (d / "wire.jsonl").write_text(
        _lines(
            {
                "t": "2026-09-08T10:00:00.050",
                "kind": "session_start",
                "provider": "p",
                "model": "m",
                "tools": [],
            },
            {
                "t": "2026-09-08T10:00:00.100",
                "kind": "request",
                "turn": 0,
                "messages": first,
            },
            {
                "t": "2026-09-08T10:00:01.000",
                "kind": "response",
                "turn": 0,
                "raw": {"choices": []},
            },
            {
                "t": "2026-09-08T10:00:02.100",
                "kind": "request",
                "turn": 1,
                "messages": second,
            },
            {
                "t": "2026-09-08T10:00:02.200",
                "kind": "micro",
                "call": "parse_task",
                "node": "parse",
                "request": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "ref", "ref": "images/micro.jpg"},
                            },
                            {"type": "text", "text": "which"},
                        ],
                    }
                ],
                "raw": {},
                "answer": "x",
                "confidence": 0.9,
            },
            {
                "t": "2026-09-08T10:00:03.100",
                "kind": "request",
                "turn": 2,
                "messages": rewritten,
            },
        )
        + "not json\n"
    )
    (d / "runtime.log").write_text(
        "10:00 [runtime] turn plugins: none\n10:00 [runtime] turn 0: 2 messages\n10:00 [runtime] turn 1: 4 messages\n"
    )
    (d / "notes.md").write_text("# notes\n")
    (d / "summary.json").write_text(
        json.dumps(
            {
                "sid": d.name,
                "outcome": {"sentinel": "DONE", "recap": "fine"},
                "model_ref": "p/m",
            }
        )
    )
    return d


def _records(model: dict, turn: int | None) -> list[dict]:
    group = next(g for g in model["groups"] if g["turn"] == turn)
    return group["records"]


def _kinds(records: list[dict]) -> list[str]:
    return [
        r["rec"].get("event") or r["rec"].get("kind") or "unparsed" for r in records
    ]


def test_load_groups_every_line_by_turn_in_time_order(session_dir: Path) -> None:
    model = viewer.load(session_dir)

    assert [g["turn"] for g in model["groups"]] == [None, 0, 1, 2]
    assert _kinds(_records(model, None)) == ["env", "session_start"]
    # A record without a turn (the walk's reading, a micro record) rides
    # the turn in progress; the torn wire line is kept, not dropped.
    assert _kinds(_records(model, 0)) == [
        "request",
        "request",
        "response",
        "response",
        "tool_result",
        "walk_read",
    ]
    assert _kinds(_records(model, 1)) == [
        "request",
        "request",
        "bad_turn_shape",
        "micro",
    ]
    # A warning is stamped from the trace's own roster, not a page list.
    assert [r.get("warn") for r in _records(model, 1)] == [None, None, True, None]
    assert _kinds(_records(model, 2)) == ["request", "request", "unparsed"]
    unparsed = _records(model, 2)[-1]
    assert (
        unparsed["src"] == "wire"
        and unparsed["line"] == 7
        and unparsed["rec"] == {"unparsed": "not json"}
    )


def test_request_delta_marks_the_news_and_ignores_moving_cache_marks(
    session_dir: Path,
) -> None:
    model = viewer.load(session_dir)
    requests = [
        r
        for g in model["groups"]
        for r in g["records"]
        if r["src"] == "wire" and r["rec"].get("kind") == "request"
    ]

    assert [(r["new_from"], r["carried"]) for r in requests] == [
        (0, 0),  # the first request: everything is news
        (2, 2),  # two carried unchanged (the cache mark moved), two new
        (1, 4),  # the history itself was rewritten from message #1
    ]


def test_runtime_log_lines_land_on_the_streams_turn(session_dir: Path) -> None:
    model = viewer.load(session_dir)
    by_turn = {g["turn"]: g["runtime"] for g in model["groups"]}

    assert by_turn[0] == ["10:00 [runtime] turn 0: 2 messages"]
    assert by_turn[1] == ["10:00 [runtime] turn 1: 4 messages"]
    assert by_turn[None] == ["10:00 [runtime] turn plugins: none"]


def test_load_carries_the_narrative_files_and_the_inventory(session_dir: Path) -> None:
    model = viewer.load(session_dir)

    assert model["sid"] == session_dir.name
    assert model["summary"]["outcome"]["sentinel"] == "DONE"
    assert model["narrative"]["notes.md"] == "# notes\n"
    assert model["narrative"]["mcp.log"] == ""
    assert [f["name"] for f in model["files"]] == [
        "events.jsonl",
        "images/100001_000_t0.jpg",
        "notes.md",
        "runtime.log",
        "summary.json",
        "wire.jsonl",
    ]
    assert model["sessions"] is None


def test_load_tolerates_a_bare_session_dir(tmp_path: Path) -> None:
    model = viewer.load(tmp_path)

    assert model["groups"] == [] and model["summary"] is None and model["files"] == []


def test_render_embeds_the_model_without_a_way_out_of_the_script(
    session_dir: Path,
) -> None:
    page = viewer.render(viewer.load(session_dir))

    # The tool result's "</script>" cannot close the data block: every
    # "<" in the data is escaped, so the page has exactly its own tags.
    assert page.count("</script>") == 2
    start = page.index('<script id="session-data" type="application/json">') + len(
        '<script id="session-data" type="application/json">'
    )
    data = json.loads(page[start : page.index("</script>", start)])
    assert data["sid"] == session_dir.name
    tool = _records(data, 0)[4]["rec"]
    assert tool["text"] == "Screen</script><b>"


def test_write_puts_the_page_in_the_session_dir_outside_the_inventory(
    session_dir: Path,
) -> None:
    page = viewer.write(session_dir)

    assert page == session_dir / "session.html" and page.stat().st_size > 1000
    assert "session.html" not in [f["name"] for f in viewer.load(session_dir)["files"]]


def test_recent_lists_the_newest_first_with_what_the_summary_says(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    (root / "20260908-090000-aaaaaa").mkdir(parents=True)
    newer = root / "20260908-100000-bbbbbb"
    newer.mkdir()
    (newer / "summary.json").write_text(
        json.dumps(
            {"outcome": {"sentinel": "WAIT", "recap": "asked"}, "model_ref": "p/m"}
        )
    )
    (root / "stray.txt").write_text("")

    rows = viewer.recent(root)

    assert rows == [
        {
            "sid": "20260908-100000-bbbbbb",
            "outcome": {"sentinel": "WAIT", "recap": "asked"},
        },
        {
            "sid": "20260908-090000-aaaaaa",
            "outcome": {
                "sentinel": "?",
                "recap": "(no summary — killed or still running)",
            },
        },
    ]
    assert viewer.recent(root, limit=1) == rows[:1]
    assert viewer.recent(root / "missing") == []


def test_served_model_carries_the_picker(session_dir: Path) -> None:
    rows = viewer.recent(session_dir.parent)

    assert viewer.load(session_dir, sessions=rows)["sessions"] == rows


def test_records_are_stamped_with_their_words(session_dir: Path) -> None:
    model = viewer.load(session_dir)
    by = {
        r["rec"].get("event") or r["rec"].get("kind"): r
        for g in model["groups"]
        for r in g["records"]
    }

    # The tool call as a verb phrase and the event's log line, from format.py.
    assert by["tool_result"]["title"] == "Looked at the screen"
    assert by["tool_result"]["summary"].startswith("peek(")
    # A warning's words come from the trace's own roster.
    assert by["bad_turn_shape"]["warn"] is True
    assert by["bad_turn_shape"]["words"].startswith("The model's turn was malformed")
    # A reply that is neither wire shape gets no gist; the words tables ride along.
    assert "gist" not in by["micro"]
    # A decision's frame is read off its request like a turn's.
    assert by["micro"]["frames"] == ["images/micro.jpg"]
    assert model["words"]["calls"]["parse_task"] == "Which playbook?"
    # The frames a record carries, whichever stream, so the page never searches for them.
    assert by["tool_result"]["frames"] == ["images/100001_000_t0.jpg"]
    requests = [
        r
        for g in model["groups"]
        for r in g["records"]
        if r["rec"].get("kind") == "request"
    ]
    assert requests[1]["frames"] == ["images/100001_000_t0.jpg"]
    # Every turn knows its span from its own records' stamps.
    assert [(g["t"][11:19], g["t_end"][11:19]) for g in model["groups"]][:2] == [
        ("10:00:00", "10:00:00"),
        ("10:00:00", "10:00:01"),
    ]
