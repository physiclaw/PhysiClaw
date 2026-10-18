"""Session close policy for one wake of the PhysiClaw engine.

``drive()`` owns STUCK retries, first-run setup restarts, and jobless-WAIT
follow-ups. Attempts report ``SessionOutcome`` and contain their own runtime
failures. The runtime uses ``productive()`` to back off unproductive wakes.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from physiclaw.agent.runtime.hook import Trigger
from physiclaw.agent.runtime.sentinel import DONE, STUCK, WAIT

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """What one session attempt reports back to ``drive()``.

    ``status`` is a ``runtime.sentinel`` status, or ``None`` when the
    session produced no clean close. ``created_job`` matters only on WAIT:
    True when the session scheduled its own resume job, which suppresses
    the generic follow-up. ``restart_requested`` is the engine's one-shot
    first-run-setup restart. ``retryable=False`` marks an outcome that
    must not burn further attempts even when ``status`` is STUCK (e.g. a
    wall-clock budget exhaustion — a slow environment would eat every retry
    the same way).
    """

    status: str | None = None
    recap: str = ""
    created_job: bool = False
    restart_requested: bool = False
    retryable: bool = True


def productive(outcome: SessionOutcome) -> bool:
    """Close-policy predicate for the runtime's wake backoff: DONE and
    WAIT are earned closes (the task moved, or a resume is scheduled),
    and a requested restart is progress by definition. Everything else —
    STUCK, FAIL, IDLE, no clean close — is an unproductive session the
    wake cadence should back off from. Lives HERE with the rest of the
    close policy, so the runtime loop cannot silently diverge from it."""
    return outcome.status in (DONE, WAIT) or outcome.restart_requested


# One attempt: (triggers, attempt_number) → outcome. The attempt number
# is 1-based and only for the engine's own log lines.
Attempt = Callable[[list[Trigger], int], Awaitable[SessionOutcome]]


async def drive(
    attempt: Attempt,
    triggers: list[Trigger],
    *,
    max_attempts: int,
    wait_default_minutes: int,
) -> SessionOutcome:
    """Run ``attempt`` until a final outcome; enforce the close policy.

    - A retryable STUCK gets a fresh attempt, up to ``max_attempts`` total.
      Other outcomes end the wake immediately.
    - One extra restart is allowed when an attempt completes first-run
      screen-layout setup: the layout only reaches the SYSTEM prompt on
      a fresh render, so the same triggers re-run once with it loaded.
      It doesn't consume an attempt, fires at most once, and is skipped
      when the wake carries nothing but the synthetic first-run trigger
      (no request to resume — the layout loads on the next real wake).
    - A final WAIT with no session-created resume job auto-schedules
      the singleton follow-up job ``wait_default_minutes`` out, so a
      WAIT close can never strand the loop with nothing to wake it.

    Exceptions from ``attempt`` propagate — containment is the
    attempt's own business (see module docstring).
    """
    setup_restart_used = False
    outcome = SessionOutcome()
    n = 0
    while n < max_attempts:
        n += 1
        outcome = await attempt(triggers, n)
        if outcome.restart_requested and not setup_restart_used:
            setup_restart_used = True
            if any(t.source != "first-run" for t in triggers):
                n -= 1  # a setup restart isn't a retry
                log.info("screen layout learned during setup — restarting to load it")
                continue
            log.info("screen layout learned on first-run wake — no request to resume")
        if outcome.status != STUCK or not outcome.retryable:
            break
        if n < max_attempts:
            log.warning(
                "session %s (attempt %d/%d): %r — retrying",
                outcome.status,
                n,
                max_attempts,
                outcome.recap,
                extra={
                    "console_message": f"Retrying task — attempt {n + 1}/{max_attempts}"
                },
            )
    if outcome.status == WAIT and not outcome.created_job:
        log.info(
            "WAIT with no create_job — auto-scheduling %d-min follow-up",
            wait_default_minutes,
        )
        _schedule_wait_followup(minutes=wait_default_minutes)
    return outcome


def _schedule_wait_followup(*, minutes: int) -> None:
    """Upsert the singleton auto-WAIT-check job to fire in ``minutes``.

    One canonical job id across sessions (``jobs.upsert_auto_wait_check``)
    so jobs.md doesn't grow one entry per WAIT close. Best effort — a
    scheduling failure is logged, never raised: the WAIT outcome stands
    either way. Imported lazily so this module stays importable without
    pulling the engine package's dependency tree.
    """
    try:
        from physiclaw.agent.engine import jobs

        at = dt.datetime.now() + dt.timedelta(minutes=minutes)
        jobs.upsert_auto_wait_check(at)
        log.info(
            "auto-WAIT follow-up scheduled at %s",
            at.isoformat(timespec="minutes"),
            extra={
                "event": "task.followup_scheduled",
                "console": True,
                "console_message": f"Will check again at {at:%H:%M}",
            },
        )
    except Exception:
        log.exception("failed to auto-schedule WAIT follow-up")
