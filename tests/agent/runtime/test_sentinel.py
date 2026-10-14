"""Tests for `physiclaw.agent.runtime.sentinel`."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.agent.runtime.sentinel import (
    STATUSES,
    parse_sentinel,
)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("DONE", "DONE"),
        ("STUCK", "STUCK"),
        ("FAIL", "FAIL"),
        ("IDLE", "IDLE"),
        ("WAIT", "WAIT"),
    ],
)
def test_status_constants(name: str, expected: str) -> None:
    from physiclaw.agent.runtime import sentinel

    assert getattr(sentinel, name) == expected


def test_statuses_frozenset_complete() -> None:
    assert STATUSES == frozenset({"DONE", "STUCK", "FAIL", "IDLE", "WAIT"})


def test_parse_sentinel_none_returns_none_empty() -> None:
    assert parse_sentinel(None) == (None, "")


def test_parse_sentinel_empty_string_returns_none_empty() -> None:
    assert parse_sentinel("") == (None, "")


def test_parse_sentinel_canonical_form_with_recap() -> None:
    assert parse_sentinel(">> DONE - completed task") == ("DONE", "completed task")


def test_parse_sentinel_single_caret() -> None:
    assert parse_sentinel("> WAIT - waiting") == ("WAIT", "waiting")


def test_parse_sentinel_lowercase_status() -> None:
    assert parse_sentinel(">> done - x") == ("DONE", "x")


def test_parse_sentinel_no_hyphen_separator() -> None:
    assert parse_sentinel(">> STUCK no hyphen") == ("STUCK", "no hyphen")


def test_parse_sentinel_bare_status_word() -> None:
    assert parse_sentinel("DONE") == ("DONE", "")


def test_parse_sentinel_lowercase_bare_status() -> None:
    assert parse_sentinel("idle") == ("IDLE", "")


def test_parse_sentinel_no_match_returns_none_and_stripped_text() -> None:
    assert parse_sentinel("nothing matches here") == (None, "nothing matches here")


def test_parse_sentinel_recap_strips_whitespace() -> None:
    assert parse_sentinel(">> FAIL -    too many retries   ") == (
        "FAIL",
        "too many retries",
    )


def test_parse_sentinel_finds_match_in_multiline_text() -> None:
    multi = "thinking out loud\n>> DONE - all good\nextra trailing"
    status, recap = parse_sentinel(multi)
    assert status == "DONE"
    assert "all good" in recap


def test_parse_sentinel_unknown_status_word_falls_through() -> None:
    assert parse_sentinel("MAYBE") == (None, "MAYBE")


def test_parse_sentinel_status_prefix_word_is_not_a_match() -> None:
    """`DONEDEAL` must not parse as DONE — the status needs a word boundary."""
    assert parse_sentinel(">> DONEDEAL - shipped") == (None, ">> DONEDEAL - shipped")


def test_parse_sentinel_waiting_prose_is_not_wait() -> None:
    assert parse_sentinel("> waiting on user") == (None, "> waiting on user")


@pytest.mark.parametrize(
    "text",
    [
        "The earlier reply was >> DONE - finished",
        "I could not finish; do not emit >> DONE - ok",
        "`>> WAIT - waiting`",
        ">>\nDONE - status split across lines",
    ],
)
def test_regression_inline_quotes_and_split_status_are_not_sentinel_lines(text):
    assert parse_sentinel(text) == (None, text)


@pytest.mark.parametrize("indent", ["", "  ", "\t"])
def test_indented_sentinel_line_still_parses(indent):
    text = f"Prior prose\n{indent}>> done - finished\nTrailing prose"

    parsed = parse_sentinel(text)

    assert parsed == ("DONE", "finished")


@given(
    status=st.sampled_from(sorted(STATUSES)),
    recap=st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs",), blacklist_characters="\r\n"
        ),
        max_size=60,
    ),
    markers=st.integers(min_value=1, max_value=4),
    indent=st.sampled_from(["", " ", "\t"]),
)
def test_marked_sentinel_round_trips_status_and_recap(status, recap, markers, indent):
    text = f"{indent}{'>' * markers} {status.lower()} - {recap}"

    parsed = parse_sentinel(text)

    assert parsed == (status, recap.strip())


@given(
    prefix=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=30
    ),
    status=st.sampled_from(sorted(STATUSES)),
)
def test_inline_status_after_prose_never_closes_session(prefix, status):
    text = f"{prefix} >> {status} - a quoted result"

    parsed = parse_sentinel(text)

    assert parsed == (None, text)
