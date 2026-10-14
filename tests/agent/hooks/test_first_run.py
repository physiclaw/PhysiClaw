"""Tests for `physiclaw.agent.hooks.first_run`.

Accepted mutation survivors: five log/prompt decorations and initializing the
truth-tested fired flag to None instead of False. Fresh-module tests cover
registration and the initial wake without resetting that flag in advance.
"""

from __future__ import annotations

import pytest

from physiclaw.agent.hooks import first_run
from physiclaw.agent.runtime.hook import Trigger, check_hooks


@pytest.fixture(autouse=True)
def _reset_fired(monkeypatch):
    """The hook fires at most once per process — give each test a fresh flag."""
    monkeypatch.setattr(first_run, "_fired", False)


def test_fires_once_when_layout_not_learned(monkeypatch) -> None:
    monkeypatch.setattr(first_run.layout, "is_learned", lambda: False)

    first = first_run.first_run_layout()
    second = first_run.first_run_layout()

    assert isinstance(first, Trigger)
    assert first.source == "first-run"
    assert "screen-layout" in first.description
    assert second is None  # already fired — don't re-wake every tick


def test_silent_when_layout_already_learned(monkeypatch) -> None:
    monkeypatch.setattr(first_run.layout, "is_learned", lambda: True)

    assert first_run.first_run_layout() is None


def test_a_learned_tick_does_not_consume_the_one_setup_wake(monkeypatch):
    monkeypatch.setattr(first_run.layout, "is_learned", lambda: True)
    learned_tick = first_run.first_run_layout()
    monkeypatch.setattr(first_run.layout, "is_learned", lambda: False)

    reset_tick = first_run.first_run_layout()
    following_tick = first_run.first_run_layout()

    assert learned_tick is None and following_tick is None
    assert reset_tick is not None and reset_tick.source == "first-run"


@pytest.mark.asyncio
async def test_first_run_registers_and_starts_with_setup_pending(fresh_hook_module):
    fresh_hook_module(first_run, "first_run_layout")

    first_tick = await check_hooks()
    second_tick = await check_hooks()

    assert len(first_tick) == 1 and first_tick[0].source == "first-run"
    assert second_tick == []
