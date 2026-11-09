"""Generic result and immutable specification boundaries."""

import pytest

from physiclaw.contract.dto import AssistantMessage, FinishReason
from physiclaw.contract.structured import (
    OutputSpec,
    StructuredCompletion,
    StructuredFailure,
)


@pytest.mark.parametrize(
    "name,function",
    [("", "submit"), ("schema", "")],
)
def test_spec_invalid_configuration_rejected(name, function):
    with pytest.raises(ValueError, match="schema and function names"):
        OutputSpec(name, {}, str, function)


def test_spec_copies_schema_on_ingress_and_export():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    spec = OutputSpec("test", schema, str)

    schema.clear()
    spec.tool()["input_schema"].clear()

    assert spec.tool()["input_schema"]["properties"]["x"]["type"] == "string"


@pytest.mark.parametrize(
    "value,failure", [(None, None), ("yes", StructuredFailure("format", "no"))]
)
def test_completion_rejects_ambiguous_outcome(value, failure):
    reply = AssistantMessage("", [], FinishReason.STOP)

    with pytest.raises(ValueError, match="exactly one"):
        StructuredCompletion(reply, value, failure)
