"""Tests for `physiclaw.agent.runtime.launcher`."""
from __future__ import annotations

import pytest

from physiclaw.agent.runtime import launcher
from physiclaw.agent.runtime.launcher import (
    _claude_available,
    engine_label,
    resolve,
)


# ---------- _claude_available ----------


def test_claude_available_true_when_package_present(mocker) -> None:
    fake = mocker.MagicMock()
    mocker.patch.object(launcher, "_claude_available", wraps=_claude_available)
    mocker.patch("importlib.util.find_spec", return_value=fake)

    assert _claude_available() is True


def test_claude_available_false_when_package_missing(mocker) -> None:
    mocker.patch("importlib.util.find_spec", return_value=None)

    assert _claude_available() is False


# ---------- engine_label ----------


def test_engine_label_for_claude_code() -> None:
    assert engine_label("claude-code/claude-sonnet-4-6") == (
        "engine=claude-code, model=claude-sonnet-4-6"
    )


def test_engine_label_for_in_process_provider() -> None:
    assert engine_label("qwen/qwen3-plus") == (
        "engine=physiclaw, provider=qwen, model=qwen3-plus"
    )


# ---------- resolve ----------


def test_resolve_returns_ref_and_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHYSICLAW_MODEL", "qwen/qwen3-plus")

    ref, source = resolve()

    assert ref == "qwen/qwen3-plus"
    assert source == "PHYSICLAW_MODEL env"


def test_resolve_raises_for_unknown_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHYSICLAW_MODEL", "mystery/x")

    with pytest.raises(RuntimeError, match=r"^unknown provider 'mystery'"):
        resolve()


def test_resolve_raises_for_claude_code_when_unavailable(
    monkeypatch: pytest.MonkeyPatch, mocker
) -> None:
    monkeypatch.setenv("PHYSICLAW_MODEL", "claude-code/claude-test")
    mocker.patch.object(launcher, "_claude_available", return_value=False)

    with pytest.raises(
        RuntimeError, match=r"selects claude-code but agent/claude/ is not installed"
    ):
        resolve()


def test_resolve_succeeds_for_claude_code_when_available(
    monkeypatch: pytest.MonkeyPatch, mocker
) -> None:
    monkeypatch.setenv("PHYSICLAW_MODEL", "claude-code/claude-test")
    mocker.patch.object(launcher, "_claude_available", return_value=True)

    ref, source = resolve()

    assert ref == "claude-code/claude-test"
    assert source == "PHYSICLAW_MODEL env"
