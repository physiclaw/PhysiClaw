"""Tests for `physiclaw.cli._format`."""

from __future__ import annotations

from physiclaw.cli._format import info, next_hint, ok, section, warn


def test_ok_includes_check_prefix_and_message() -> None:
    out = ok("hello")

    assert "✓" in out
    assert "hello" in out


def test_warn_includes_bang_prefix_and_message() -> None:
    out = warn("careful")

    assert "!" in out
    assert "careful" in out


def test_next_hint_includes_next_prefix() -> None:
    out = next_hint("run something")

    assert "Next:" in out
    assert "run something" in out


def test_info_indents_with_two_spaces() -> None:
    out = info("a note")

    assert out.startswith("  ")
    assert "a note" in out


def test_section_includes_title_text() -> None:
    out = section("Status")

    assert "Status" in out


def test_ok_and_warn_use_different_prefixes() -> None:
    assert "✓" in ok("x") and "✓" not in warn("x")
    assert "!" in warn("x") and "!" not in ok("x")


def test_cli_decorations_respect_encoding_and_no_color(monkeypatch):
    import io
    import sys

    from physiclaw.cli._format import step_fail, step_ok, step_warn

    class ASCIIConsole(io.StringIO):
        @property
        def encoding(self):
            return "ascii"

        def isatty(self):
            return True

    stream = ASCIIConsole()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setenv("NO_COLOR", "")
    rendered = "\n".join(
        [
            ok("ready → go"),
            warn("check cable"),
            section("状态"),
            step_ok(),
            step_warn("retry"),
            step_fail("stopped"),
            next_hint("physiclaw status"),
        ]
    )
    rendered.encode("ascii", "strict")
    assert "\033" not in rendered and "OK ready -> go" in rendered
    assert "\\u72b6\\u6001" in rendered
    assert "Next: physiclaw status" in rendered
