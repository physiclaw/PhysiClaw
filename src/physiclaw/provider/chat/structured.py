"""Decode a generic structured reply without knowing any agent tool semantics."""

import json
from typing import Literal

from physiclaw.contract.dto import AssistantMessage, FinishReason
from physiclaw.contract.structured import (
    OutputSpec,
    StructuredCompletion,
    StructuredFailure,
)


def strict_json(text: str) -> object:
    """Reject duplicate keys and non-JSON numeric constants before validation."""

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    def constant(value):
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def decode_completion[T](
    reply: AssistantMessage,
    spec: OutputSpec[T],
    *,
    transport: Literal["tool", "native"] = "tool",
) -> StructuredCompletion[T]:
    if reply.refused or reply.finish_reason == FinishReason.CONTENT_FILTER:
        failure = StructuredFailure("refusal", "Provider refused this generation")
    elif reply.finish_reason == FinishReason.LENGTH:
        failure = StructuredFailure(
            "truncation", "Provider truncated the output; nothing was accepted"
        )
    elif reply.finish_reason == FinishReason.ERROR:
        failure = StructuredFailure("format", "Provider generation failed")
    elif reply.decode_error:
        failure = StructuredFailure("format", reply.decode_error)
    else:
        try:
            if transport == "native":
                if reply.tool_calls:
                    raise ValueError("Native output must not contain tool calls")
                data = strict_json(reply.content)
            else:
                if len(reply.tool_calls) != 1:
                    raise ValueError("Expected exactly one output function call")
                call = reply.tool_calls[0]
                if call.name != spec.function_name or not call.id:
                    raise ValueError(
                        f"Expected {spec.function_name} with a provider call ID"
                    )
                data = call.arguments
            value = spec.decoder(data)
            return StructuredCompletion(reply, value=value)
        except (ValueError, TypeError, RecursionError) as exc:
            failure = StructuredFailure("format", str(exc))
    return StructuredCompletion(reply, failure=failure)
