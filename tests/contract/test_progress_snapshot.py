"""Shared progress is an immutable declaration, separate from private context."""

from dataclasses import FrozenInstanceError

import pytest

from physiclaw.contract.turn import ProgressSnapshot, ProgressStep, TurnInput


def test_progress_copies_steps_without_rewriting_text():
    steps = [ProgressStep(" 打开淘宝 ", "in_progress")]
    progress = ProgressSnapshot("  user quote  ", steps)
    turn = TurnInput(None, "not_observed", progress=progress)
    steps.clear()

    assert turn.progress is progress
    assert progress.user_said == "  user quote  "
    assert progress.steps[0].content == " 打开淘宝 "
    with pytest.raises(FrozenInstanceError):
        progress.steps[0].content = "changed"
    with pytest.raises(FrozenInstanceError):
        progress.user_said = "changed"
    assert not hasattr(progress, "understanding")
    assert not hasattr(progress, "scratchpad")


@pytest.mark.parametrize(
    "content,status",
    [
        ("", "pending"),
        (" ", "pending"),
        (None, "pending"),
        ("step", "done"),
        ("step", []),
    ],
)
def test_malformed_progress_step_is_rejected(content, status):
    with pytest.raises(ValueError):
        ProgressStep(content, status)


@pytest.mark.parametrize("quote", ["", " ", 1])
def test_task_quote_is_explicit_text_or_unknown(quote):
    with pytest.raises(ValueError):
        ProgressSnapshot(quote)


def test_unknown_task_and_no_active_step_are_supported():
    progress = ProgressSnapshot(None, (ProgressStep("read message", "pending"),))
    assert progress.user_said is None
    assert ProgressSnapshot("known task").steps == ()


def test_multiple_active_steps_and_mutable_nested_values_are_rejected():
    with pytest.raises(ValueError, match="more than one"):
        ProgressSnapshot(
            None, [ProgressStep("a", "in_progress"), ProgressStep("b", "in_progress")]
        )
    with pytest.raises(ValueError, match="ProgressStep"):
        ProgressSnapshot(None, [{"content": "mutable", "status": "pending"}])
    with pytest.raises(ValueError, match="ProgressSnapshot"):
        TurnInput(None, None, progress={"user_said": "mutable"})
