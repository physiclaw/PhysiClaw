"""Tests for `physiclaw.common.text` — UTF-8 (+ LF) text I/O helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from physiclaw.common.text import (
    append_line,
    append_text,
    iter_jsonl,
    read_text,
    write_text,
)


def test_write_then_read_roundtrips_utf8(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    write_text(path, "伦敦 — 14°")

    assert read_text(path) == "伦敦 — 14°"
    # Bytes are UTF-8, not a locale codepage.
    assert path.read_bytes().decode("utf-8") == "伦敦 — 14°"


def test_append_text_utf8(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    append_text(path, "a\n")
    append_text(path, "b\n")

    assert read_text(path) == "a\nb\n"


def test_write_text_pins_lf_newlines(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    write_text(path, "line1\nline2\n")

    raw = path.read_bytes()
    assert b"\r\n" not in raw  # never CRLF, even on Windows
    assert raw == b"line1\nline2\n"


@pytest.mark.parametrize("needle", [None, "keep"])
def test_regression_iter_jsonl_recovers_after_invalid_utf8(tmp_path, needle) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(
        b'{"keep": 1}\n{"keep": "\xff"}\n{"keep": 2}\n'
        b'null\n[]\n42\n{"torn":\n{"keep": 3}\n'
    )

    records = list(iter_jsonl(path, needle))

    assert records == [{"keep": 1}, {"keep": 2}, {"keep": 3}]


def test_iter_jsonl_filters_before_parsing(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"kind": "other"}\n{"kind": "保留"}\n', encoding="utf-8")

    records = list(iter_jsonl(path, "保留"))

    assert records == [{"kind": "保留"}]


def test_iter_jsonl_missing_file_yields_nothing(tmp_path) -> None:
    path = tmp_path / "missing.jsonl"

    records = list(iter_jsonl(path))

    assert records == []


@pytest.mark.parametrize(
    "before, expected",
    [
        (b"", "新事实\n".encode()),
        (b"old", "old\n新事实\n".encode()),
        (b"old\n", "old\n新事实\n".encode()),
        (b"old\r\n", "old\r\n新事实\n".encode()),
        (b"old\r", "old\r新事实\n".encode()),
        ("旧事实".encode(), "旧事实\n新事实\n".encode()),
    ],
)
def test_append_line_preserves_bytes_and_starts_a_new_utf8_line(
    tmp_path, before, expected
):
    path = tmp_path / "facts.md"
    path.write_bytes(before)

    append_line(path, "新事实")

    assert path.read_bytes() == expected


def test_append_line_creates_missing_file_without_leading_blank_line(tmp_path):
    path = tmp_path / "facts.md"

    append_line(path, "new fact")

    assert path.read_bytes() == b"new fact\n"


def test_append_line_invalid_unicode_preserves_existing_bytes(tmp_path):
    path = tmp_path / "facts.md"
    path.write_bytes(b"old")

    with pytest.raises(UnicodeEncodeError):
        append_line(path, "\ud800")

    assert path.read_bytes() == b"old"


def test_append_line_invalid_unicode_does_not_create_file(tmp_path):
    path = tmp_path / "facts.md"

    with pytest.raises(UnicodeEncodeError):
        append_line(path, "\ud800")

    assert not path.exists()
