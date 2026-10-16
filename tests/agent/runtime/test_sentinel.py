"""Tests for `physiclaw.agent.runtime.sentinel`."""

from __future__ import annotations

import pytest

from physiclaw.agent.runtime.sentinel import (
    STATUSES,
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
