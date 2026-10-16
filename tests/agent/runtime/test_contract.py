"""Tests for the session-outcome contract (`runtime.contract.drive`).

The attempt callable is faked throughout — these tests own the policy
layer only: retry selection, the setup restart, the jobless-WAIT
follow-up. Engine-side reporting (which Session fields feed which
SessionOutcome fields) is covered by the engine suite.

Accepted mutation variants: log wording, type-only aliases, dataclass slot
storage, and False→None flags that remain falsy in the close-policy branches.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import FrozenInstanceError

import pytest
from freezegun import freeze_time

from physiclaw.agent.runtime import contract
from physiclaw.agent.runtime.contract import SessionOutcome
from physiclaw.agent.runtime.hook import Trigger
from physiclaw.agent.runtime.sentinel import DONE, IDLE, STUCK, WAIT

_TRIGGERS = [Trigger(description="t", source="phone")]


def test_outcome_is_immutable_after_session_reports_it():
    outcome = SessionOutcome(status=DONE)

    with pytest.raises(FrozenInstanceError):
        outcome.status = STUCK


@pytest.mark.parametrize(
    "status, earned",
    [
        (DONE, True),
        (WAIT, True),
        (STUCK, False),
        ("FAIL", False),
        (IDLE, False),
        (None, False),
    ],
)
@pytest.mark.parametrize("restart", [False, True])
def test_productive_outcomes_match_close_policy(status, earned, restart):
    outcome = SessionOutcome(status=status, restart_requested=restart)

    result = contract.productive(outcome)

    assert result is (earned or restart)


def _attempts(*outcomes: SessionOutcome):
    """An attempt fake yielding `outcomes` in order, recording its calls."""
    seq = iter(outcomes)
    calls: list[int] = []

    async def attempt(triggers, n):
        calls.append(n)
        return next(seq)

    return attempt, calls


def _no_upsert(mocker):
    """Stub the jobs upsert; returns the mock for call assertions."""
    return mocker.patch("physiclaw.agent.engine.jobs.upsert_auto_wait_check")


# ---------- retry selection ----------


@pytest.mark.asyncio
async def test_final_status_returns_after_one_attempt(mocker) -> None:
    _no_upsert(mocker)
    attempt, calls = _attempts(SessionOutcome(status=DONE))

    out = await contract.drive(
        attempt, _TRIGGERS, max_attempts=5, wait_default_minutes=15
    )

    assert calls == [1]
    assert out.status == DONE


@pytest.mark.asyncio
async def test_retry_on_stuck_until_final(mocker) -> None:
    _no_upsert(mocker)
    attempt, calls = _attempts(
        SessionOutcome(status=STUCK, recap="a"),
        SessionOutcome(status=STUCK, recap="b"),
        SessionOutcome(status=DONE),
    )

    out = await contract.drive(
        attempt, _TRIGGERS, max_attempts=3, wait_default_minutes=15
    )

    assert calls == [1, 2, 3]
    assert out.status == DONE


@pytest.mark.asyncio
async def test_stuck_exhaustion_returns_last_outcome(mocker) -> None:
    _no_upsert(mocker)
    attempt, calls = _attempts(
        SessionOutcome(status=STUCK, recap="x"),
        SessionOutcome(status=STUCK, recap="y"),
    )

    out = await contract.drive(
        attempt, _TRIGGERS, max_attempts=2, wait_default_minutes=15
    )

    assert calls == [1, 2]
    assert out.status == STUCK
    assert out.recap == "y"


@pytest.mark.asyncio
async def test_undone_is_final(mocker) -> None:
    # A None status ends the wake without retrying.
    _no_upsert(mocker)
    attempt, calls = _attempts(SessionOutcome(status=None))

    out = await contract.drive(
        attempt, _TRIGGERS, max_attempts=3, wait_default_minutes=15
    )

    assert calls == [1]
    assert out.status is None


@pytest.mark.asyncio
async def test_non_retryable_stuck_outcome_stops(mocker) -> None:
    # e.g. the engine's wall-clock budget exhaustion: STUCK, but final.
    _no_upsert(mocker)
    attempt, calls = _attempts(SessionOutcome(status=STUCK, retryable=False))

    out = await contract.drive(
        attempt, _TRIGGERS, max_attempts=3, wait_default_minutes=15
    )

    assert calls == [1]
    assert out.status == STUCK


@pytest.mark.asyncio
async def test_attempt_exception_propagates(mocker) -> None:
    # Attempt exceptions propagate; drive must not swallow them.
    _no_upsert(mocker)
    calls: list[int] = []

    async def attempt(triggers, n):
        calls.append(n)
        raise RuntimeError("attempt failed")

    with pytest.raises(RuntimeError, match="attempt failed"):
        await contract.drive(
            attempt,
            _TRIGGERS,
            max_attempts=3,
            wait_default_minutes=15,
        )
    assert calls == [1]


# ---------- setup restart ----------


@pytest.mark.asyncio
async def test_setup_restart_reruns_without_consuming_attempt(mocker) -> None:
    _no_upsert(mocker)
    attempt, calls = _attempts(
        SessionOutcome(status=None, restart_requested=True),
        SessionOutcome(status=DONE),
    )

    out = await contract.drive(
        attempt, _TRIGGERS, max_attempts=1, wait_default_minutes=15
    )

    assert calls == [1, 1]  # restart re-runs the same attempt number
    assert out.status == DONE


@pytest.mark.asyncio
async def test_setup_restart_skipped_on_first_run_only_wake(mocker) -> None:
    # A synthetic first-run wake has no request to resume: the layout
    # loads on the next real wake instead of replaying the stale trigger.
    _no_upsert(mocker)
    attempt, calls = _attempts(SessionOutcome(status=IDLE, restart_requested=True))

    out = await contract.drive(
        attempt,
        [Trigger(description="learn the layout", source="first-run")],
        max_attempts=3,
        wait_default_minutes=15,
    )

    assert calls == [1]
    assert out.status == IDLE


@pytest.mark.asyncio
async def test_setup_restart_fires_at_most_once(mocker) -> None:
    _no_upsert(mocker)
    attempt, calls = _attempts(
        SessionOutcome(status=DONE, restart_requested=True),
        SessionOutcome(status=DONE, restart_requested=True),  # pathological
    )

    await contract.drive(attempt, _TRIGGERS, max_attempts=3, wait_default_minutes=15)

    assert calls == [1, 1]  # second restart request ignored — DONE is final


# ---------- WAIT follow-up ----------


@pytest.mark.asyncio
async def test_wait_without_created_job_schedules_followup(mocker, caplog) -> None:
    upsert = _no_upsert(mocker)
    attempt, _ = _attempts(SessionOutcome(status=WAIT))

    with freeze_time("2026-04-28T14:00:00"):
        await contract.drive(
            attempt,
            _TRIGGERS,
            max_attempts=3,
            wait_default_minutes=15,
        )

    upsert.assert_called_once()
    target = upsert.call_args.args[0]
    assert isinstance(target, dt.datetime)
    # Target time = now + wait_default_minutes.
    assert (target.hour, target.minute) == (14, 15)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_wait_with_created_job_skips_followup(mocker) -> None:
    upsert = _no_upsert(mocker)
    attempt, _ = _attempts(SessionOutcome(status=WAIT, created_job=True))

    await contract.drive(attempt, _TRIGGERS, max_attempts=3, wait_default_minutes=15)

    upsert.assert_not_called()


@pytest.mark.asyncio
async def test_wait_followup_failure_is_swallowed(
    mocker, caplog: pytest.LogCaptureFixture
) -> None:
    mocker.patch(
        "physiclaw.agent.engine.jobs.upsert_auto_wait_check",
        side_effect=OSError("disk full"),
    )
    attempt, _ = _attempts(SessionOutcome(status=WAIT))

    with caplog.at_level(logging.ERROR, logger="physiclaw.agent.runtime.contract"):
        out = await contract.drive(
            attempt,
            _TRIGGERS,
            max_attempts=3,
            wait_default_minutes=15,
        )

    assert out.status == WAIT  # the outcome stands
    assert any("failed to auto-schedule" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_regression_wait_followup_date_overflow_preserves_outcome(mocker, caplog):
    upsert = _no_upsert(mocker)
    outcome = SessionOutcome(status=WAIT, recap="resume later")
    attempt, _ = _attempts(outcome)

    with freeze_time("2026-04-28T14:00:00"), caplog.at_level(logging.ERROR):
        result = await contract.drive(
            attempt,
            _TRIGGERS,
            max_attempts=1,
            wait_default_minutes=10**12,
        )

    assert result is outcome
    upsert.assert_not_called()
    assert any("failed to auto-schedule" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_done_never_schedules_followup(mocker) -> None:
    upsert = _no_upsert(mocker)
    attempt, _ = _attempts(SessionOutcome(status=DONE))

    await contract.drive(attempt, _TRIGGERS, max_attempts=3, wait_default_minutes=15)

    upsert.assert_not_called()


@pytest.mark.asyncio
async def test_zero_attempt_budget_returns_undone_without_calling_engine(mocker):
    attempt = mocker.AsyncMock()

    result = await contract.drive(
        attempt, _TRIGGERS, max_attempts=0, wait_default_minutes=15
    )

    assert result.status is None
    attempt.assert_not_called()
