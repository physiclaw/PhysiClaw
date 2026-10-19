"""Provider-neutral JSON projection and strict decision-response helpers."""

import httpx

from physiclaw.contract.decision import (
    ActionCandidate,
    DecisionContext,
    DecisionUsage,
    decision_json,
)
from physiclaw.provider.provider_base import (
    ProviderPermanentError,
    ProviderTransientError,
)


def check_response_status(response: httpx.Response, *, vendor: str) -> None:
    """Classify native HTTP failures without exposing response contents."""
    if response.is_success:
        return
    error = (
        ProviderTransientError
        if response.status_code == 429 or response.status_code >= 500
        else ProviderPermanentError
    )
    raise error(f"{vendor} decision HTTP {response.status_code}")


def context_to_json(context: DecisionContext) -> dict[str, object]:
    return {
        "facts": decision_json(context.facts),
        "observation": {
            "id": context.observation_id,
            "ocr": [
                {"ref": e.ref, "bbox": list(e.bbox), "text": e.text}
                for e in context.ocr
            ],
            "icons": [
                {"ref": e.ref, "bbox": list(e.bbox), "text": e.text}
                for e in context.icons
            ],
        },
    }


def candidate_to_json(candidate: ActionCandidate) -> dict[str, object]:
    option: dict[str, object] = {"description": candidate.description}
    if candidate.tool is not None:
        option.update(tool=candidate.tool, arguments=decision_json(candidate.arguments))
    if candidate.target_ref is not None:
        option["target_ref"] = candidate.target_ref
    return option


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
