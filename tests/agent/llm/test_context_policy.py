"""Prompt and compaction policy is agent-owned and available offline."""

import dataclasses

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.agent.llm.context_policy import (
    DEFAULT_COLLAPSE,
    CollapsePolicy,
    context_policy_for,
)

# ---------- CollapsePolicy ----------


def test_collapse_policy_is_frozen() -> None:
    policy = CollapsePolicy(first_at=30, keep=10, interval=20)

    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.keep = 5  # type: ignore[misc]


def test_collapse_policy_keep_may_equal_first_at() -> None:
    # Boundary: keep == first_at leaves zero turns to fold on the first
    # collapse but never indexes past turn_starts — legal.
    assert CollapsePolicy(first_at=10, keep=10, interval=1).keep == 10


def test_collapse_policy_rejects_keep_above_first_at() -> None:
    # keep > first_at would IndexError at `turn_starts[-keep]` the
    # moment the first collapse fires — must die at construction.
    with pytest.raises(ValueError, match="keep=40"):
        CollapsePolicy(first_at=30, keep=40, interval=20)


def test_collapse_policy_rejects_zero_keep() -> None:
    with pytest.raises(ValueError, match="keep=0"):
        CollapsePolicy(first_at=30, keep=0, interval=20)


def test_collapse_policy_rejects_zero_interval() -> None:
    with pytest.raises(ValueError, match="interval=0"):
        CollapsePolicy(first_at=30, keep=10, interval=0)


@pytest.mark.parametrize("field", ["first_at", "keep", "interval"])
@pytest.mark.parametrize(
    "value", [True, False, 1.5, 10.0, float("inf"), float("nan"), "10", None]
)
def test_collapse_policy_noninteger_counts_rejected_before_history_indexing(
    field, value
) -> None:
    counts = {"first_at": 30, "keep": 10, "interval": 20, field: value}

    with pytest.raises(ValueError, match="counts must be integers"):
        CollapsePolicy(**counts)


@given(
    keep=st.integers(min_value=1, max_value=1000),
    extra=st.integers(min_value=0, max_value=1000),
    interval=st.integers(min_value=1, max_value=1000),
)
def test_collapse_policy_valid_counts_preserve_history_window(keep, extra, interval):
    first_at = keep + extra

    policy = CollapsePolicy(first_at=first_at, keep=keep, interval=interval)

    assert list(range(policy.first_at))[-policy.keep] == extra
    assert policy.keep + policy.interval > policy.keep


@pytest.mark.parametrize(
    "provider",
    [
        "",
        "unknown",
        "qwen",
        "moonshot",
        "google",
        "deepseek",
        "openai",
        "openrouter",
        "anthropic",
    ],
)
def test_existing_prompt_and_compaction_defaults(provider):
    policy = context_policy_for(provider)
    assert policy.collapse == DEFAULT_COLLAPSE == CollapsePolicy(30, 10, 20)
    assert policy.inline_tool_index is (provider != "anthropic")
