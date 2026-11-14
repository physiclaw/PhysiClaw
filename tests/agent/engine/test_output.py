"""Whole-output validation before the internal note/action bridge."""

from copy import deepcopy

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.agent.engine.output import ActionCatalog
from physiclaw.agent.engine.validator import ValidationError
from physiclaw.contract.decision import decision_json
from physiclaw.contract.turn import TurnOutput


@pytest.fixture(scope="module")
def actions(mcp_schemas):
    return ActionCatalog.compile(mcp_schemas, [], max_scratchpad=32)


def output(action=None, scratchpad=None):
    return {
        "summary": "Look",
        "scratchpad": scratchpad,
        "action": {"peek": {}} if action is None else action,
    }


@pytest.mark.parametrize("scratchpad", [None, "", " ", "remember", "x" * 32])
def test_lower_preserves_scratchpad_semantics(actions, scratchpad):
    value = actions.decode(output(scratchpad=scratchpad))

    message = actions.lower(value)

    assert message.tool_names() == ["note", "peek"]
    assert message.tool_calls[0].arguments == (
        {"summary": "Look"}
        if scratchpad is None
        else {"summary": "Look", "scratchpad": scratchpad}
    )
    assert message.tool_calls[0].id != message.tool_calls[1].id


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"summary": "x", "action": {"peek": {}}},
        {**output(), "extra": True},
        {**output(), "summary": 2},
        output(scratchpad=False),
        output(scratchpad="x" * 33),
        output({}),
        output({"peek": {}, "tap": {}}),
        output({"note": {}}),
        output({"Peek": {}}),
        output({"peek": []}),
        output({"tap": {}}),
        output({"tap": {"bbox": [0, 0, 0, 1]}}),
        output({"tap": {"bbox": [False, 0, 1, 1]}}),
        output({"tap": {"bbox": [0, 0, float("nan"), 1]}}),
        output({"swipe": {"bbox": [0, 0, 1, 1], "direction": "up", "size": None}}),
        output({"sequence": {"actions": [{"tool_name": "peek", "arg": {}}] * 2}}),
        output(
            {"sequence": {"actions": [{"tool_name": "tap", "arg": [0, 0, 0, 1]}] * 2}}
        ),
        output(
            {"sequence": {"actions": [{"tool_name": "tap", "arg": [0, 0, 1, 1]}] * 6}}
        ),
        output({"sequence": {"actions": [{"tool_name": "sequence", "arg": {}}] * 2}}),
    ],
)
def test_decode_invalid_output_rejects_whole_turn(actions, value):
    with pytest.raises(ValidationError):
        actions.decode(value)


@pytest.mark.parametrize(
    "action",
    [
        {"swipe": {"bbox": [0, 0, 1, 1], "direction": "up"}},
        {
            "sequence": {
                "actions": [
                    {"tool_name": "tap", "arg": [0, 0, 1, 1]},
                    {"tool_name": "send_to_clipboard", "arg": "hello"},
                ]
            }
        },
        {
            "sequence": {
                "actions": [
                    {
                        "tool_name": "swipe",
                        "arg": {"bbox": [0, 0, 1, 1], "direction": "up"},
                    }
                ]
                * 2
            }
        },
    ],
)
def test_decode_preserves_optional_arguments_and_sequence_shape(actions, action):
    result = actions.lower(actions.decode(output(action)))

    assert {result.tool_calls[1].name: result.tool_calls[1].arguments} == action


@given(st.text(max_size=32), st.text())
def test_decode_lower_roundtrip_does_not_mutate_input(actions, scratchpad, summary):
    value = {**output(scratchpad=scratchpad), "summary": summary}
    original = deepcopy(value)

    result = actions.lower(actions.decode(value))

    assert value == original
    assert result.tool_calls[0].arguments == {
        "summary": summary,
        "scratchpad": scratchpad,
    }


def test_catalog_local_precedence_and_snapshot_isolation(mcp_schemas):
    local = [
        {
            "name": "peek",
            "description": "Local",
            "input_schema": {
                "type": "object",
                "required": ["x"],
                "properties": {"x": {"type": ["string", "null"], "default": "z"}},
            },
        },
        {"name": "note", "input_schema": {}},
    ]
    actions = ActionCatalog.compile(mcp_schemas, local, max_scratchpad=32)
    before = actions.identity

    local[0]["description"] = "mutated"
    actions.tools[0]["input_schema"].clear()
    value = actions.decode(output({"peek": {"x": None}}))

    assert actions.identity == before
    assert actions.tools[0]["description"] == "Local"
    assert "note" not in [t["name"] for t in actions.tools]
    assert decision_json(value.action) == {"peek": {"x": None}}


@pytest.mark.parametrize("note_index", [0, 1, 2])
def test_catalog_excludes_note_without_dropping_following_actions(note_index):
    tools = [
        {"name": "first", "input_schema": {}},
        {"name": "second", "input_schema": {}},
    ]
    inputs = deepcopy(tools)
    inputs.insert(note_index, {"name": "note"})

    actions = ActionCatalog.compile(inputs, [], max_scratchpad=32)

    assert actions.tools == tools


@pytest.mark.parametrize(
    "schema",
    [{"$ref": "#/$defs/x"}, {"type": "alien"}, {"pattern": ".*"}, {"oneOf": []}],
)
def test_catalog_unsupported_constraints_fail_compilation(schema):
    tools = [{"name": "custom", "input_schema": schema}]

    with pytest.raises(ValueError, match="schema"):
        ActionCatalog.compile(tools, [], max_scratchpad=32)


@pytest.mark.parametrize(
    "tools,limit",
    [
        ([], 32),
        ([{"name": "", "input_schema": {}}], 32),
        ([{"name": "peek", "input_schema": {}}], -1),
        ([{"name": "sequence", "input_schema": {}}], 32),
    ],
)
def test_catalog_invalid_setup_fails(tools, limit):
    with pytest.raises(ValueError, match="action|scratchpad|sequence"):
        ActionCatalog.compile(tools, [], max_scratchpad=limit)


def test_lower_revalidates_manually_constructed_contract(actions):
    value = TurnOutput("x", None, {"note": {}})

    with pytest.raises(ValidationError):
        actions.lower(value)


def test_catalog_union_constraints_and_live_only_actions():
    tools = [
        {
            "name": "live",
            "input_schema": {
                "type": "object",
                "properties": {
                    "value": {
                        "allOf": [
                            {"anyOf": [{"type": "string"}, {"type": "null"}]},
                            {"enum": ["ok", None]},
                        ]
                    }
                },
                "required": ["value"],
                "additionalProperties": False,
            },
        }
    ]
    actions = ActionCatalog.compile(tools, [], max_scratchpad=0)

    result = actions.lower(actions.decode(output({"live": {"value": None}})))

    assert result.tool_names() == ["note", "live"]
    assert actions.spec().tool()["input_schema"] == actions.schema


def test_catalog_schema_matches_reviewed_wire_contract():
    actions = ActionCatalog.compile(
        [
            {"name": "peek", "description": "Inspect.", "input_schema": {}},
            {
                "name": "label",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": ["string", "null"], "default": None}
                    },
                    "required": ["text"],
                },
            },
        ],
        [],
        max_scratchpad=32,
    )

    schema = actions.schema

    assert schema == {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Last result and next action; aim for 20 words or fewer.",
            },
            "scratchpad": {"type": ["string", "null"], "maxLength": 32},
            "action": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {"peek": {"description": "Inspect."}},
                        "required": ["peek"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "object",
                                "properties": {
                                    "text": {
                                        "type": ["string", "null"],
                                        "default": None,
                                    }
                                },
                                "required": ["text"],
                                "description": "",
                            }
                        },
                        "required": ["label"],
                        "additionalProperties": False,
                    },
                ]
            },
        },
        "required": ["summary", "scratchpad", "action"],
        "additionalProperties": False,
    }
    assert isinstance(actions.identity, str)
    assert len(actions.identity) == 64
    assert actions.spec().name == "turn_output"
    assert actions.spec().function_name == "submit_turn"


@pytest.mark.parametrize(
    "schema",
    [
        {"properties": {"x": {"pattern": "."}}},
        {"items": {"pattern": "."}},
        {"additionalProperties": {"pattern": "."}},
        {"anyOf": [{"pattern": "."}]},
        {"allOf": [{"pattern": "."}]},
        {"oneOf": [{"pattern": "."}]},
    ],
)
def test_catalog_rejects_unsupported_nested_constraints(schema):
    tools = [{"name": "live", "input_schema": schema}]

    with pytest.raises(ValueError, match="unsupported action schema"):
        ActionCatalog.compile(tools, [], max_scratchpad=32)


def test_catalog_is_frozen_after_setup(actions):
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        actions.max_scratchpad = 0


def test_catalog_identity_ignores_object_key_order():
    first = [
        {
            "name": "custom",
            "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
        }
    ]
    second = [
        {
            "input_schema": {"properties": {"x": {"type": "string"}}, "type": "object"},
            "name": "custom",
        }
    ]

    a = ActionCatalog.compile(first, [], max_scratchpad=32)
    b = ActionCatalog.compile(second, [], max_scratchpad=32)

    assert a.identity == b.identity
    assert a.tools[0].get("description", "") == ""
    assert (
        a.schema["properties"]["action"]["oneOf"][0]["properties"]["custom"][
            "description"
        ]
        == ""
    )


def test_sequence_partial_catalog_keeps_only_eligible_members(mcp_schemas):
    tools = [t for t in mcp_schemas if t["name"] in ("sequence", "swipe")]
    actions = ActionCatalog.compile(tools, [], max_scratchpad=32)
    value = output(
        {
            "sequence": {
                "actions": [
                    {
                        "tool_name": "swipe",
                        "arg": {"bbox": [0, 0, 1, 1], "direction": "up"},
                    }
                ]
                * 2
            }
        }
    )

    result = actions.lower(actions.decode(value))

    assert result.content == ""
    assert result.vendor_extra == {}
    assert result.tool_calls[1].arguments == value["action"]["sequence"]


def test_sequence_rejects_unknown_step_fields(actions):
    value = output(
        {
            "sequence": {
                "actions": [{"tool_name": "tap", "arg": [0, 0, 1, 1], "extra": 3}] * 2
            }
        }
    )

    with pytest.raises(ValidationError):
        actions.decode(value)


@pytest.mark.parametrize(
    "schema", [False, True, None, {"type": "array"}, {"type": ["object", "null"]}]
)
def test_catalog_rejects_non_object_root_schema(schema):
    tools = [{"name": "custom", "input_schema": schema}]

    with pytest.raises(ValueError, match="action input schema must describe an object"):
        ActionCatalog.compile(tools, [], max_scratchpad=32)


@pytest.mark.parametrize("kind", ["future_type", ["string", "future_type"], {}, [[]]])
def test_catalog_rejects_unsupported_nested_types(kind):
    tools = [
        {
            "name": "custom",
            "input_schema": {"type": "object", "properties": {"x": {"type": kind}}},
        }
    ]

    with pytest.raises(ValueError, match="unsupported schema type"):
        ActionCatalog.compile(tools, [], max_scratchpad=32)


@pytest.mark.parametrize("name", ["tap", "send_to_clipboard", "sequence"])
@pytest.mark.parametrize("properties", [None, [], {}, {"actions": False}])
def test_regression_sequence_rejects_malformed_gesture_schemas(
    mcp_schemas, name, properties
):
    tools = deepcopy(mcp_schemas)
    tool = next(tool for tool in tools if tool["name"] == name)
    tool["input_schema"]["properties"] = properties

    with pytest.raises(ValueError, match="schema"):
        ActionCatalog.compile(tools, [], max_scratchpad=32)


def nested_object(depth):
    return {"child": nested_object(depth - 1)} if depth else {}


def test_decode_rejects_excessive_json_nesting_before_conversion(actions):
    value = output({"peek": {"metadata": nested_object(40)}})

    with pytest.raises(ValidationError, match="maximum nesting depth"):
        actions.decode(value)
