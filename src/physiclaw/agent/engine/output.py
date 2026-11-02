"""Session action catalog, complete TurnOutput validation and internal bridge.

The catalog is compiled once from live MCP and eligible local tools. Dispatch's
broader registry is deliberately not an input. Nothing in this module writes
state or executes an action.
"""

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

from physiclaw.agent.engine.validator import (
    ValidationError,
    validate_arguments,
    validate_value,
)
from physiclaw.common import gesture_vocab as gv
from physiclaw.contract.decision import DecisionValue, decision_json, freeze_mapping
from physiclaw.contract.dto import AssistantMessage, FinishReason, ToolCall
from physiclaw.contract.structured import OutputSpec
from physiclaw.contract.turn import TurnOutput

_KEYWORDS = frozenset(
    "type enum const minimum maximum minLength maxLength items minItems maxItems "
    "properties required additionalProperties minProperties maxProperties "
    "anyOf oneOf allOf title description default examples deprecated".split()
)
_TYPES = frozenset("object array string number integer boolean null".split())


def _check_schema(schema: dict | bool) -> None:
    """Fail closed on constraints our validator cannot enforce."""
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict) or set(schema) - _KEYWORDS:
        raise ValueError(f"unsupported action schema: {schema!r}")
    types = schema.get("type", [])
    if isinstance(types, str):
        types = [types]
    if not isinstance(types, list) or any(t not in _TYPES for t in types):
        raise ValueError("unsupported schema type")
    for child in schema.get("properties", {}).values():
        _check_schema(child)
    for key in ("items", "additionalProperties"):
        if key in schema:
            _check_schema(schema[key])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            if not isinstance(schema[key], list) or not schema[key]:
                raise ValueError(f"{key} must contain schema branches")
            for child in schema[key]:
                _check_schema(child)


@dataclass(frozen=True, slots=True)
class ActionCatalog:
    """Frozen model-visible actions, shared by the schema and prompt index."""

    _tools: tuple[Mapping[str, DecisionValue], ...]
    max_scratchpad: int

    @classmethod
    def compile(
        cls, mcp_tools: list[dict], local_tools: list[dict], *, max_scratchpad: int
    ) -> "ActionCatalog":
        if type(max_scratchpad) is not int or max_scratchpad < 0:
            raise ValueError("scratchpad limit must be a nonnegative integer")
        tools: dict[str, dict] = {}
        for tool in [*mcp_tools, *local_tools]:
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("action name must be nonempty text")
            if name == "note":
                continue
            schema = tool.get("input_schema")
            if not isinstance(schema, dict) or schema.get("type", "object") != "object":
                raise ValueError("action input schema must describe an object")
            tools[name] = deepcopy(tool)
        if not tools:
            raise ValueError("action catalog must not be empty")
        # Sequence uses scalar press/clipboard arguments, not ordinary tool args.
        if gv.SEQUENCE in tools:
            branches = []
            for name in sorted(gv.PRESS_TOOLS | {gv.SWIPE, gv.SEND_TO_CLIPBOARD}):
                if name not in tools:
                    continue
                schema = tools[name]["input_schema"]
                if name in gv.PRESS_TOOLS:
                    argument_schema = schema["properties"]["bbox"]
                elif name == gv.SEND_TO_CLIPBOARD:
                    argument_schema = schema["properties"]["text"]
                else:
                    argument_schema = schema
                branches.append(
                    {
                        "type": "object",
                        "properties": {
                            "tool_name": {"const": name},
                            "arg": argument_schema,
                        },
                        "required": ["tool_name", "arg"],
                        "additionalProperties": False,
                    }
                )
            if not branches:
                raise ValueError("sequence requires eligible gesture schemas")
            tools[gv.SEQUENCE]["input_schema"]["properties"]["actions"]["items"] = {
                "oneOf": branches
            }
        for tool in tools.values():
            _check_schema(tool["input_schema"])
        return cls(tuple(freeze_mapping(t) for t in tools.values()), max_scratchpad)

    @property
    def tools(self) -> list[dict]:
        return [cast(dict, decision_json(tool)) for tool in self._tools]

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Last result and next action; aim for 20 words or fewer.",
                },
                "scratchpad": {
                    "type": ["string", "null"],
                    "maxLength": self.max_scratchpad,
                },
                "action": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                t["name"]: {
                                    **t["input_schema"],
                                    "description": t.get("description", ""),
                                }
                            },
                            "required": [t["name"]],
                            "additionalProperties": False,
                        }
                        for t in self.tools
                    ]
                },
            },
            "required": ["summary", "scratchpad", "action"],
            "additionalProperties": False,
        }

    @property
    def identity(self) -> str:
        return hashlib.sha256(
            json.dumps(self.schema, sort_keys=True).encode()
        ).hexdigest()

    def spec(self) -> OutputSpec[TurnOutput]:
        return OutputSpec("turn_output", self.schema, self.decode)

    def decode(self, value: object) -> TurnOutput:
        validate_value(value, self.schema)
        assert isinstance(value, dict)
        name, args = next(iter(value["action"].items()))
        # The envelope already validates the action schema; retain bbox geometry.
        validate_arguments(args, {})
        if name == gv.SEQUENCE:
            for step in args["actions"]:
                tool, arg = step["tool_name"], step["arg"]
                arguments = {"bbox": arg} if tool in gv.PRESS_TOOLS else arg
                if tool in gv.PRESS_TOOLS or tool == gv.SWIPE:
                    validate_arguments(arguments, {})
        try:
            return TurnOutput(value["summary"], value["scratchpad"], value["action"])
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def lower(self, value: TurnOutput) -> AssistantMessage:
        # Revalidate even manually constructed DTOs at the conversion boundary.
        checked = self.decode(
            {
                "summary": value.summary,
                "scratchpad": value.scratchpad,
                "action": decision_json(value.action),
            }
        )
        action = decision_json(checked.action)
        assert isinstance(action, dict)
        name, arguments = next(iter(action.items()))
        note = {"summary": checked.summary}
        if checked.scratchpad is not None:
            note["scratchpad"] = checked.scratchpad
        return AssistantMessage(
            content="",
            finish_reason=FinishReason.TOOL_CALLS,
            tool_calls=[
                ToolCall(f"note_{uuid4().hex}", "note", note),
                ToolCall(f"action_{uuid4().hex}", name, arguments),
            ],
        )
