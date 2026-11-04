"""HTTP failure metadata shared across services; no retries or fallback.

Each service classifies errors in its own context. Callers decide whether
to retry, suppress a request, disable a service, or yield to another producer.
"""

import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

_EXHAUSTED_QUOTA_CODES = frozenset(
    {
        "insufficient_quota",
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    }
)


def quota_exhausted(response: httpx.Response) -> bool:
    """Billing/usage limits require account action, unlike a temporary 429."""
    try:
        payload = response.json()
    except ValueError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    return any(
        isinstance(code, str) and code in _EXHAUSTED_QUOTA_CODES
        for code in (error.get("code"), error.get("type"))
    )


def describe(e: BaseException) -> str:
    """`ClassName: message`, or just `ClassName` when the message is
    empty — httpx timeouts commonly str() to '', which made wrapped
    errors log as the famously unhelpful `transport: `."""
    msg = str(e)
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


def retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (
                parsedate_to_datetime(value) - datetime.now(timezone.utc)
            ).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None
