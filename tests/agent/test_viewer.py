"""Tests for `physiclaw.agent.trace.viewer` — the session viewer's model
and page."""

# Mutation review accepts three annotation-only edits, a 50-to-51 picker limit,
# missing-tool/error wording, and broader dispatch for fields outside published
# event/wire shapes (the raw data remains available in those cases).

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from freezegun import freeze_time
from hypothesis import example, given
from hypothesis import strategies as st

from physiclaw.agent.trace import viewer

JPEG = b"\xff\xd8\xff\xd9"

JSON_VALUE = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda values: (
        st.lists(values, max_size=4) | st.dictionaries(st.text(), values, max_size=4)
    ),
    max_leaves=12,
)


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


def test_recent_breaks_an_mtime_tie_by_name(tmp_path: Path) -> None:
    # Two sessions stamped within one tick of a coarse filesystem clock
    # still list in one order everywhere: the later sid first.
    root = tmp_path / "sessions"
    for sid in ("20260908-100000-bbbbbb", "20260908-090000-aaaaaa"):
        d = root / sid
        d.mkdir(parents=True)
        os.utime(d, (1_000_000, 1_000_000))

    rows = viewer.recent(root)

    assert [r["sid"] for r in rows] == [
        "20260908-100000-bbbbbb",
        "20260908-090000-aaaaaa",
    ]


def test_recent_lists_the_newest_first_with_what_the_summary_says(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    older = root / "20260908-090000-aaaaaa"
    older.mkdir(parents=True)
    newer = root / "20260908-100000-bbbbbb"
    newer.mkdir()
    (newer / "summary.json").write_text(
        json.dumps(
            {"outcome": {"sentinel": "WAIT", "recap": "asked"}, "model_ref": "p/m"}
        )
    )
    (root / "stray.txt").write_text("")
    # The order is by mtime, so say which is newer instead of trusting
    # two mkdirs to land in different ticks of the filesystem clock.
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

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


@pytest.mark.parametrize(
    "record",
    [
        {"event": []},
        {"event": "tool_result", "name": "tap", "arguments": {"bbox": 1}},
        {"event": "tool_result", "images": 1},
    ],
)
def test_regression_malformed_record_keeps_raw_data_and_following_record(
    tmp_path, record
):
    following = {"event": "done", "sentinel": "DONE", "recap": "still visible"}
    (tmp_path / "events.jsonl").write_text(_lines(record, following), encoding="utf-8")

    model = viewer.load(tmp_path)

    rows = _records(model, None)
    assert [row["rec"] for row in rows] == [record, following]
    assert rows[0]["unformatted"] is True
    assert rows[0]["warn"] is True
    assert rows[1]["summary"] == "OUTCOME: DONE — still visible"


@given(
    st.dictionaries(
        st.sampled_from(
            [
                "event",
                "kind",
                "turn",
                "t",
                "name",
                "arguments",
                "images",
                "raw",
                "messages",
                "request",
            ]
        ),
        JSON_VALUE,
        max_size=6,
    )
)
@example({"event": "tool_result", "name": "tap", "arguments": {"bbox": 1}})
@example({"kind": "request", "messages": [None, 7, {"content": [None]}]})
def test_load_preserves_arbitrary_record_fields(record):
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        (directory / "events.jsonl").write_text(_lines(record), encoding="utf-8")
        (directory / "wire.jsonl").write_text(_lines(record), encoding="utf-8")

        model = viewer.load(directory)

        rows = [row for group in model["groups"] for row in group["records"]]
        assert [row["rec"] for row in rows] == [record, record]


@pytest.mark.parametrize("filename", ["events.jsonl", "wire.jsonl"])
def test_regression_torn_utf8_keeps_its_line_and_later_records(tmp_path, filename):
    (tmp_path / filename).write_bytes(b'{"turn":0}\n\xff\n{"turn":1}\n')

    model = viewer.load(tmp_path)

    rows = [row for group in model["groups"] for row in group["records"]]
    assert [row["line"] for row in rows] == [1, 2, 3]
    assert rows[1]["rec"] == {"unparsed": r"\xff"}
    assert rows[2]["rec"] == {"turn": 1}


@pytest.mark.parametrize(
    "filename", ["notes.md", "runtime.log", "mcp.log", "summary.json"]
)
def test_regression_invalid_narrative_utf8_is_still_readable(tmp_path, filename):
    (tmp_path / filename).write_bytes(b"prefix \xff suffix")

    model = viewer.load(tmp_path)

    assert model["narrative"][filename] == r"prefix \xff suffix"


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_regression_nonfinite_record_stays_visible_as_unparsed_text(tmp_path, number):
    line = '{"event":"usage","input":' + number + "}"
    (tmp_path / "events.jsonl").write_text(line + "\n", encoding="utf-8")

    model = viewer.load(tmp_path)

    assert _records(model, None)[0]["rec"] == {"unparsed": line}
    assert json.loads(_embedded(viewer.render(model))) == model


def _embedded(page):
    return page.split('<script id="session-data" type="application/json">', 1)[1].split(
        "</script>", 1
    )[0]


def test_regression_export_surrogate_text_keeps_valid_embedded_json(tmp_path):
    record = {"event": "unknown", "text": "\ud800</script>中文"}
    (tmp_path / "events.jsonl").write_text(_lines(record), encoding="utf-8")

    page = viewer.write(tmp_path).read_text(encoding="utf-8")

    model = json.loads(_embedded(page))
    assert _records(model, None)[0]["rec"] == record
    assert page.count("</script>") == 2


@pytest.mark.parametrize("usage", [[1], "bad", 1, True])
def test_regression_malformed_usage_does_not_hide_summary_or_records(tmp_path, usage):
    summary = {"usage": usage, "outcome": {"sentinel": "DONE"}}
    (tmp_path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

    model = viewer.load(tmp_path)

    assert model["summary"] == summary
    assert model["words"]["tokens"] == {}


def test_regression_nonfinite_summary_remains_in_narrative(tmp_path):
    raw = '{"usage":{"input_tokens":NaN}}'
    (tmp_path / "summary.json").write_text(raw, encoding="utf-8")

    model = viewer.load(tmp_path)

    assert model["summary"] is None
    assert model["narrative"]["summary.json"] == raw


def test_regression_unreadable_stream_does_not_hide_other_stream(tmp_path, mocker):
    record = {"kind": "request", "messages": []}
    (tmp_path / "wire.jsonl").write_text(_lines(record), encoding="utf-8")
    open_path = Path.open

    def open_unless_events(path, *args, **kwargs):
        if path == tmp_path / "events.jsonl":
            raise PermissionError("unreadable events")
        return open_path(path, *args, **kwargs)

    mocker.patch.object(Path, "open", open_unless_events)

    model = viewer.load(tmp_path)

    assert _records(model, None)[0]["rec"] == record


def test_regression_unreadable_inventory_entry_does_not_hide_records(tmp_path, mocker):
    record = {"event": "done", "sentinel": "DONE"}
    (tmp_path / "events.jsonl").write_text(_lines(record), encoding="utf-8")
    broken = tmp_path / "a-unreadable.jpg"
    broken.write_bytes(b"frame")
    stat_path = Path.stat

    def stat_unless_broken(path, *args, **kwargs):
        if path == broken:
            raise PermissionError("unreadable metadata")
        return stat_path(path, *args, **kwargs)

    mocker.patch.object(Path, "stat", stat_unless_broken)

    model = viewer.load(tmp_path)

    assert _records(model, None)[0]["rec"] == record
    assert [item["name"] for item in model["files"]] == ["events.jsonl"]


def test_load_preserves_unicode_line_separators_and_physical_line_numbers(tmp_path):
    record = {"event": "other", "text": "one\u2028two\u2029three"}
    (tmp_path / "events.jsonl").write_text(
        "\n \n" + json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    model = viewer.load(tmp_path)

    assert _records(model, None)[0]["rec"] == record
    assert _records(model, None)[0]["line"] == 3


def test_load_boolean_turn_does_not_create_numbered_group(tmp_path):
    (tmp_path / "events.jsonl").write_text(_lines({"turn": True}), encoding="utf-8")

    model = viewer.load(tmp_path)

    assert [group["turn"] for group in model["groups"]] == [None]


def test_load_micro_record_recovers_legacy_reason_and_frames(tmp_path):
    record = {
        "kind": "micro",
        "request": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "images/frame.jpg"}}
                ],
            }
        ],
        "raw": {"choices": [{"message": {"content": '{"reason":"legacy reason"}'}}]},
    }
    (tmp_path / "wire.jsonl").write_text(_lines(record), encoding="utf-8")

    model = viewer.load(tmp_path)

    row = _records(model, None)[0]
    assert row["reason"] == "legacy reason"
    assert row["frames"] == ["images/frame.jpg"]


def test_load_formats_valid_summary_tokens(tmp_path):
    summary = {"usage": {"input_tokens": 1_200_000, "output_tokens": 9_800}}
    (tmp_path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

    model = viewer.load(tmp_path)

    assert model["words"]["tokens"] == {"input_tokens": "1.2M", "output_tokens": "9.8k"}


def test_load_exposes_generated_at_and_sentinel_words(tmp_path):
    with freeze_time("2026-10-14 12:00:00"):
        model = viewer.load(tmp_path)

    assert model["generated_at"] == "2026-10-14T12:00:00.000"
    assert model["words"]["sentinels"]["DONE"] == {"words": "finished", "tone": "ok"}


def test_render_rejects_nonfinite_direct_model():
    model = {"value": float("nan")}

    with pytest.raises(ValueError, match="Out of range float values"):
        viewer.render(model)


def test_load_note_preserves_the_turn_narrative(tmp_path):
    record = {
        "event": "tool_result",
        "name": "note",
        "arguments": {"summary": "conductor: step finished"},
    }
    (tmp_path / "events.jsonl").write_text(_lines(record), encoding="utf-8")

    model = viewer.load(tmp_path)

    assert _records(model, None)[0]["note"] == "step finished"


@pytest.mark.parametrize("kind", ["response", "micro"])
def test_load_reply_gist_and_explicit_reason_remain_available(tmp_path, kind):
    record = {
        "kind": kind,
        "raw": {"choices": [{"message": {"content": '{"reason":"legacy"}'}}]},
        "reason": "recorded reason",
    }
    (tmp_path / "wire.jsonl").write_text(_lines(record), encoding="utf-8")

    model = viewer.load(tmp_path)

    row = _records(model, None)[0]
    assert row["gist"]["text"] == '{"reason":"legacy"}'
    assert row["reason"] == "recorded reason"


def test_load_deltas_stop_at_first_changed_message(tmp_path):
    first = {
        "kind": "request",
        "messages": [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "unchanged"},
        ],
    }
    second = {
        "kind": "request",
        "messages": [{"role": "user", "content": "changed"}, first["messages"][1]],
    }
    (tmp_path / "wire.jsonl").write_text(_lines(first, second), encoding="utf-8")

    model = viewer.load(tmp_path)

    assert _records(model, None)[1]["new_from"] == 0
    assert _records(model, None)[1]["carried"] == 2


def test_load_deltas_skip_malformed_request_and_continue(tmp_path):
    message = {"role": "user", "content": "first"}
    first = {"kind": "request", "messages": [message]}
    malformed = {"kind": "request", "messages": 1}
    last = {
        "kind": "request",
        "messages": [message, {"role": "assistant", "content": "second"}],
    }
    (tmp_path / "wire.jsonl").write_text(
        _lines(first, malformed, last), encoding="utf-8"
    )

    model = viewer.load(tmp_path)

    assert _records(model, None)[2]["new_from"] == 1
    assert _records(model, None)[2]["carried"] == 1


def test_load_deltas_ignore_message_fields_in_event_records(tmp_path):
    message = {"role": "user", "content": "message"}
    (tmp_path / "events.jsonl").write_text(
        _lines({"event": "request", "messages": [message]}), encoding="utf-8"
    )
    (tmp_path / "wire.jsonl").write_text(
        _lines({"kind": "request", "messages": [message]}), encoding="utf-8"
    )

    model = viewer.load(tmp_path)

    assert "new_from" not in _records(model, None)[0]
    assert _records(model, None)[1]["carried"] == 0


def test_load_missing_timestamps_keep_prelude_and_file_order(tmp_path):
    (tmp_path / "events.jsonl").write_text(
        _lines(
            {"event": "env"},
            {"event": "request", "turn": 0, "t": "2026-10-14T12:00:00"},
            {"event": "other"},
        ),
        encoding="utf-8",
    )

    model = viewer.load(tmp_path)

    assert model["groups"][0]["t"] == ""
    assert [row["line"] for group in model["groups"] for row in group["records"]] == [
        1,
        2,
        3,
    ]
    assert model["groups"][1]["t_end"] == "2026-10-14T12:00:00"


@pytest.mark.parametrize("line", ["unparsed lineX", "null", "[1]", '"a string"'])
def test_load_keeps_non_object_lines_whole(tmp_path, line):
    (tmp_path / "wire.jsonl").write_text(line + "\n", encoding="utf-8")

    model = viewer.load(tmp_path)

    assert _records(model, None)[0]["rec"] == {"unparsed": line}


def test_load_inventory_reports_byte_sizes(tmp_path):
    payload = "记录\n".encode("utf-8")
    (tmp_path / "notes.md").write_bytes(payload)

    model = viewer.load(tmp_path)

    assert model["files"] == [{"name": "notes.md", "bytes": len(payload)}]
