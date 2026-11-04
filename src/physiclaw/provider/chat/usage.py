"""Tolerant parsing of provider-reported token counters."""

from typing import Any


def token_count_or_none(value: Any) -> int | None:
    """Parse a reported counter without confusing invalid values with zero."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        count = int(value)
    except ValueError:
        return None
    return count if count >= 0 else None


def token_count(value: Any) -> int:
    """An optional usage counter: accept integers and proxy-stringified integers.

    Malformed accounting must not discard an otherwise usable model reply.
    Booleans, fractions, negatives, and missing counts are unreported (zero).
    """
    return token_count_or_none(value) or 0
