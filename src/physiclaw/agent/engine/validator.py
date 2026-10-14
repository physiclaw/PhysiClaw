"""JSONSchema argument validator for principle 4.

Validates each `ToolCall.arguments` against the tool's declared
`input_schema` BEFORE dispatch. Catches:
  - missing required properties, or forbidden additional properties
  - wrong types (string vs array, number vs string-digits)
  - enum violations
  - truncated arguments (technically valid JSON but missing a required key)

On failure, the engine does NOT actuate — it pairs the call with an error
`ToolResult` so the model sees the issue next turn and self-corrects.

Supported keywords: type, enum, minimum/maximum, minLength/maxLength,
items, minItems/maxItems, properties, required and additionalProperties.
Container constraints also apply to nullable types and schemas without
an explicit type. Boolean subschemas are supported. Unknown types and
other keywords remain ignored; this is not a full JSONSchema validator.
No values are coerced, and non-finite numbers are never valid arguments.
"""

import math
from typing import Any

from physiclaw.common.bbox import validate_bbox


class ValidationError(Exception):
    """Raised when arguments don't match the schema. Caller converts this
    into an error ToolResult rather than executing the tool."""


_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def _is_number(v: Any) -> bool:
    """True iff v is int or float — NOT bool (Python bool is subclass of int)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _matches_type(value: Any, name: str) -> bool:
    py_type = _TYPE_MAP.get(name)
    if py_type is None:  # Preserve forward compatibility with unknown types.
        return True
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, py_type)


def _json_equal(left: Any, right: Any) -> bool:
    """JSON enums distinguish booleans from numbers, including inside containers."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def validate_arguments(args: Any, schema: dict[str, Any]) -> None:
    """Check `args` against a JSONSchema `schema`. Raises ValidationError.

    `args` is typed `Any` because it crosses a trust boundary — the data
    is JSON-decoded LLM output, so a wrong top-level type (list, string,
    null) is a possibility we have to defend against, even though every
    in-process caller passes a dict.

    The schema is expected to look like:
        {
          "type": "object",
          "properties": {name: {"type": ..., "enum": [...], "items": {...}}},
          "required": ["name", ...],
        }
    """
    if not isinstance(args, dict):
        raise ValidationError(
            f"arguments must be a JSON object, got {type(args).__name__}"
        )

    # Bbox shape — the shared `common.bbox` contract, wrapped into this
    # layer's ValidationError. Runs before the per-key `_check_value`
    # loop so the bbox-specific error fires before the generic schema
    # type check. Only the tool's own bbox is a gesture; a nested object
    # may contain arbitrary bbox-named metadata. The orchestrator checks
    # the geometry again at gesture time as defense-in-depth.
    bbox = args.get("bbox")
    if bbox is not None:
        try:
            validate_bbox(bbox)
        except ValueError as e:
            raise ValidationError(str(e)) from e

    try:
        _check_object(args, schema)
    except RecursionError as e:
        raise ValidationError("arguments or schema are too deeply nested") from e


def _check_object(value: dict, schema: dict[str, Any], prefix: str = "") -> None:
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    missing = sorted(prefix + name for name in required if name not in value)
    if missing:
        raise ValidationError(f"missing required argument(s): {', '.join(missing)}")
    additional = schema.get("additionalProperties", True)
    for name, item in value.items():
        _check_value(prefix + name, item, properties.get(name, additional))


def _check_value(key: str, value: Any, prop: dict[str, Any] | bool) -> None:
    if prop is False:
        raise ValidationError(f"{key}: value is not allowed by the schema")
    if prop is True:
        prop = {}
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError(f"{key}: number must be finite, got {value!r}")

    enum = prop.get("enum")
    if enum is not None and not any(_json_equal(value, choice) for choice in enum):
        raise ValidationError(f"{key}: value {value!r} is not one of {enum}")

    if _is_number(value):
        minimum = prop.get("minimum")
        if minimum is not None and value < minimum:
            raise ValidationError(f"{key}: {value} < minimum {minimum}")
        maximum = prop.get("maximum")
        if maximum is not None and value > maximum:
            raise ValidationError(f"{key}: {value} > maximum {maximum}")

    if isinstance(value, str):
        min_len = prop.get("minLength")
        if min_len is not None and len(value) < min_len:
            raise ValidationError(
                f"{key}: string length {len(value)} < minLength {min_len}"
            )
        max_len = prop.get("maxLength")
        if max_len is not None and len(value) > max_len:
            raise ValidationError(
                f"{key}: string length {len(value)} > maxLength {max_len}"
            )

    expected_type = prop.get("type")
    if expected_type is not None:
        # `type` may be a list in JSONSchema (e.g. ["string", "null"]).
        types_to_check = (
            expected_type if isinstance(expected_type, list) else [expected_type]
        )
        if not any(_matches_type(value, name) for name in types_to_check):
            got = type(value).__name__
            want = "|".join(types_to_check)
            raise ValidationError(f"{key}: expected {want}, got {got} ({value!r})")

    # Constraints apply to the instance's shape, independently of `type`.
    if isinstance(value, list):
        min_items = prop.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise ValidationError(
                f"{key}: array has {len(value)} items, min {min_items}"
            )
        max_items = prop.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            raise ValidationError(
                f"{key}: array has {len(value)} items, max {max_items}"
            )
        items_schema = prop.get("items", True)
        for i, elem in enumerate(value):
            _check_value(f"{key}[{i}]", elem, items_schema)

    if isinstance(value, dict):
        _check_object(value, prop, prefix=key + ".")
