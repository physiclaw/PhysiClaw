"""Tests for corpus extraction, validation, and the labeled-corpus round trip.

Mutation review accepts diagnostic-decoration changes, the unused dedupe-dict
values, and removing dataclass freezing (this module never mutates items).
"""

from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from physiclaw.common import paths
from physiclaw.common.listing import LISTING_HEADER
from physiclaw.conductor.bench import corpus

LISTING_A = f'{LISTING_HEADER}\n0 [text] "综合" [0.100,0.100,0.200,0.120] 0.90'
LISTING_B = f'{LISTING_HEADER}\n0 [text] "结算" [0.500,0.900,0.600,0.940] 0.90'


def _write_wire(sid: str, lines: list[dict]) -> None:
    d = paths.sessions_dir() / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.jsonl").write_text(
        "\n".join(json.dumps(rec, ensure_ascii=False) for rec in lines) + "\n",
        encoding="utf-8",
    )


def test_session_listings_extracts_deduped_in_order() -> None:
    _write_wire(
        "20260101-000000-aaaaaa",
        [
            {"kind": "session_start", "tools": []},
            {
                "kind": "request",
                "turn": 0,
                "messages": [
                    # System quotes the header in doctrine — must not extract.
                    {"role": "system", "content": LISTING_B},
                    # Header without any parseable row (prose/stub) — skipped.
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": LISTING_HEADER}],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": LISTING_A},
                            {"type": "image", "ref": "images/x.jpg"},
                        ],
                    },
                ],
            },
            {
                "kind": "request",
                "turn": 1,
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": LISTING_A}]},
                    # Anthropic wire shape: the listing rides inside a
                    # tool_result block's own content list.
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t1",
                                "content": [
                                    {"type": "text", "text": LISTING_B},
                                    {"type": "ref", "ref": "images/y.jpg"},
                                ],
                            }
                        ],
                    },
                ],
            },
            {"kind": "response", "turn": 1, "raw": {}},
        ],
    )

    out = corpus.session_listings("20260101-000000-aaaaaa")

    assert out == [LISTING_A, LISTING_B]


def test_session_listings_missing_session_raises() -> None:
    with pytest.raises(FileNotFoundError):
        corpus.session_listings("nope")


def test_corpus_round_trip(tmp_path) -> None:
    items = [
        corpus.CorpusItem(label="taobao.results", listing=LISTING_A),
        corpus.CorpusItem(label="other", listing=LISTING_B),
    ]
    p = tmp_path / "corpus.jsonl"

    corpus.write_corpus(p, items)

    assert corpus.read_corpus(p) == items
    assert "综合" in p.read_text(encoding="utf-8")


def test_read_corpus_names_bad_line(tmp_path) -> None:
    p = tmp_path / "corpus.jsonl"
    p.write_text('{"label": "a"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="bad corpus line"):
        corpus.read_corpus(p)


@pytest.mark.parametrize(
    "record",
    [
        None,
        [],
        42,
        "label",
        {},
        {"label": "a"},
        {"label": None, "listing": "text"},
        {"label": "a", "listing": []},
        {"label": 1, "listing": False},
    ],
)
def test_regression_invalid_corpus_record_names_file_and_line(tmp_path, record):
    path = tmp_path / "bad.jsonl"
    path.write_text("\n" + json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=rf"{path}:2: bad corpus line"):
        corpus.read_corpus(path)


@given(st.lists(st.tuples(st.text(), st.text()), max_size=12))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_corpus_strings_round_trip_without_coercion(tmp_path, records):
    items = [corpus.CorpusItem(label, listing) for label, listing in records]
    path = tmp_path / "roundtrip.jsonl"

    corpus.write_corpus(path, items)

    assert corpus.read_corpus(path) == items


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_regression_unicode_line_separators_round_trip(tmp_path, separator):
    items = [corpus.CorpusItem("app.home", f"before{separator}after")]
    path = tmp_path / "unicode.jsonl"

    corpus.write_corpus(path, items)

    assert corpus.read_corpus(path) == items


def test_partition_splits_genuine_and_negatives() -> None:
    items = [
        corpus.CorpusItem(label="?", listing=LISTING_B),
        corpus.CorpusItem(label="taobao.results", listing=LISTING_A),
        corpus.CorpusItem(label="taobao.unknown-page", listing=LISTING_B),
        corpus.CorpusItem(label="other", listing=LISTING_B),
    ]

    by_page, negatives = corpus.partition(items, "taobao", {"results"})

    assert set(by_page) == {"results"} and len(by_page["results"]) == 1
    # a label that names no declared page counts as a hard negative;
    # '?' lines are ignored entirely.
    assert len(negatives) == 2
    assert by_page["results"][0].text == LISTING_A
    assert [screen.text for screen in negatives] == [LISTING_B, LISTING_B]


def test_session_listings_invalid_event_screens_fall_back_to_wire():
    sid = "20260101-000000-ffffff"
    _write_wire(
        sid, [{"kind": "request", "messages": [{"role": "user", "content": LISTING_A}]}]
    )
    events = [
        {"event": "tool_result", "text": None},
        {"event": "note", "name": "tool_result", "text": LISTING_B},
    ]
    (paths.sessions_dir() / sid / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8"
    )

    result = corpus.session_listings(sid)

    assert result == [LISTING_A]


def test_session_listings_prefers_the_tool_results_own_record() -> None:
    # The trace keeps every screen whole on its tool_result — a walk's
    # turns included, which the wire log never carries.
    sid = "20260101-000000-eeeeee"
    d = paths.sessions_dir() / sid
    d.mkdir(parents=True)
    events = [
        {"event": "env"},
        {"event": "tool_result", "turn": 1, "name": "peek", "text": LISTING_B},
        {"event": "tool_result", "turn": 2, "name": "note", "text": "noted"},
        {"event": "tool_result", "turn": 3, "name": "tap", "text": LISTING_B},
        {"event": "tool_result", "turn": 4, "name": "tap", "text": LISTING_A},
    ]
    (d / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )
    _write_wire_lines(
        d,
        [
            {
                "kind": "request",
                "turn": 5,
                "messages": [{"role": "user", "content": "x"}],
            }
        ],
    )

    assert corpus.session_listings(sid) == [LISTING_B, LISTING_A]


def _write_wire_lines(d, lines: list[dict]) -> None:
    (d / "model.jsonl").write_text(
        "\n".join(json.dumps(rec, ensure_ascii=False) for rec in lines) + "\n",
        encoding="utf-8",
    )
