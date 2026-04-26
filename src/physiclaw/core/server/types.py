"""Pydantic `Annotated` type aliases for MCP tool signatures.

FastMCP propagates the `Field(...)` constraints (ge/le/min_length/etc.)
into each tool's JSONSchema, which the model sees via the `tools=[...]`
API. The engine-side validator (src/physiclaw/agent/engine/validator.py)
enforces the same schema before dispatch AND catches cross-element
constraints JSONSchema can't express (e.g. left < right) — failing
there produces a clean `invalid arguments` tool_result the agent can
self-correct from, vs. a Pydantic ValidationError from the MCP layer.
The orchestrator runs `validate_bbox` again as defense-in-depth before
any GRBL move.
"""
from typing import Annotated

from pydantic import Field

# Each bbox coordinate in [0, 1]. FastMCP emits `minimum`/`maximum` for
# each item and `min_length`/`max_length` for the outer list. The
# left<right / top<bottom invariant lives in the engine validator and
# the orchestrator — see module docstring.
_BboxCoord = Annotated[float, Field(ge=0.0, le=1.0)]

Bbox = Annotated[
    list[_BboxCoord],
    Field(
        description=(
            "Screen bbox [left, top, right, bottom] as 0-1 decimals, "
            "with left < right and top < bottom."
        ),
        min_length=4, max_length=4,
    ),
]


# Plain text to paste into the phone clipboard. Must be a string (not a
# JSON object — a mistake seen in practice when the model wrapped the
# value as `{"text": "..."}`). Non-empty; 10 KB cap is generous for
# real paste content but catches pathological args.
ClipboardText = Annotated[
    str,
    Field(
        description=(
            "Plain text to copy to the phone's clipboard. MUST be a "
            "string — pass the text directly, not a JSON object."
        ),
        min_length=1, max_length=10_000,
    ),
]
