"""Strict HTTP/response helpers for decision providers."""

import httpx

from physiclaw.common.provider_errors import (
    ProviderPermanentError,
    ProviderRequestError,
    ProviderTransientError,
)
from physiclaw.contract.decision import DecisionUsage


def check_response_status(response: httpx.Response, *, vendor: str) -> None:
    """Classify native HTTP failures without exposing response contents."""
    if response.is_success:
        return
    status = response.status_code
    message = f"{vendor} decision HTTP {status}"
    if status in {408, 429} or status >= 500:
        raise ProviderTransientError(message)
    if status in {400, 413, 422}:
        # This payload was rejected; a later screen/request may be accepted.
        raise ProviderRequestError(message)
    raise ProviderPermanentError(message)


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response key")
        result[key] = value
    return result


def parse_usage(value: object) -> DecisionUsage:
    if not isinstance(value, dict):
        return DecisionUsage()

    def count(item: object) -> int | None:
        return item if type(item) is int and item >= 0 else None

    return DecisionUsage(
        input_tokens=count(value.get("input_tokens")),
        output_tokens=count(value.get("output_tokens")),
    )
