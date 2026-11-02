"""Provider grammars preserve omission/null semantics; canonical validation wins."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from physiclaw.agent.engine.assemble import build_prompt_bundle
from physiclaw.agent.engine.validator import ValidationError
from physiclaw.common.provider_errors import ProviderConfigurationError
from physiclaw.contract.decision import decision_json
from physiclaw.provider.chat.schema import strict_output_schema


def test_optional_default_and_null_are_distinct_without_rewriting():
    schema = {
        "type": "object",
        "properties": {
            "args": {
                "type": "object",
                "properties": {
                    "speed": {"type": "number", "default": 1},
                    "memo": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
        },
        "required": ["args"],
        "additionalProperties": False,
    }
    original = deepcopy(schema)
    compiled = strict_output_schema(schema)
    grammar = Draft202012Validator(compiled)
    canonical = Draft202012Validator(schema)
    for args in (
        {},
        {"speed": 1},
        {"memo": None},
        {"memo": ""},
        {"speed": 2, "memo": "x"},
    ):
        value = {"args": args}
        grammar.validate(value)
        canonical.validate(value)
    assert not grammar.is_valid({"args": {"speed": None}})
    assert not grammar.is_valid({"args": {"unknown": 1}})
    assert schema == original


def test_nullable_object_stays_nullable():
    schema = {
        "type": "object",
        "properties": {
            "item": {
                "type": ["object", "null"],
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
                "additionalProperties": False,
            },
        },
        "required": ["item"],
        "additionalProperties": False,
    }
    compiled = strict_output_schema(schema)
    assert compiled["properties"]["item"]["anyOf"][1] == {"type": "null"}
    grammar = Draft202012Validator(compiled)
    grammar.validate({"item": None})
    grammar.validate({"item": {"x": "value"}})
    assert not grammar.is_valid({"item": {}})


def test_real_turn_output_grammar_and_canonical_constraints(mcp_schemas):
    catalog = build_prompt_bundle("openai", mcp_schemas=mcp_schemas).catalog
    schema = strict_output_schema(catalog.schema)
    grammar = Draft202012Validator(schema)
    for action in (
        {"peek": {}},
        {"swipe": {"bbox": [0, 0, 1, 1], "direction": "up"}},
        {"update_progress": {}},
        {"sequence": {"actions": [{"tool_name": "tap", "arg": [0, 0, 1, 1]}] * 2}},
    ):
        value = {"summary": "Inspect", "scratchpad": None, "action": action}
        assert grammar.is_valid(value)
        assert decision_json(catalog.decode(value).action) == action
    # Grammar-only constraints are deliberately incomplete; host bounds survive.
    value = {
        "summary": "Inspect",
        "scratchpad": "x" * (catalog.max_scratchpad + 1),
        "action": {"peek": {}},
    }
    grammar.validate(value)
    with pytest.raises(ValidationError):
        catalog.decode(value)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array"},
        {"type": "object", "additionalProperties": {"type": "string"}},
        {"type": "object", "properties": {"optional": {"type": "string"}}},
    ],
)
def test_unrepresentable_openai_schema_fails_explicitly(schema):
    with pytest.raises(ProviderConfigurationError):
        strict_output_schema(schema)
