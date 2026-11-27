"""Wire contracts shared by discovery, validation and the OpenAPI document."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

BASE_PATH = "/midscene-device"
DEVICE_DESCRIPTION = (
    "Physical phone operated by a robotic stylus arm over an overhead camera view. "
    "There is no direct keyboard primitive: to enter long or non-ASCII text, call "
    "sendToClipboard and then paste into the focused input field."
)


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Point(Empty):
    x: float
    y: float


class AtPoint(Empty):
    point: Point


# A GRBL dwell drains the planner and acknowledges only when finished.
# Leave headroom under the transport's 12-second acknowledgement deadline.
DurationMs = Annotated[
    float, Field(ge=0, le=10000, description="Duration in milliseconds")
]


class Press(AtPoint):
    duration: DurationMs | None = None


class Path(Empty):
    start: Point = Field(alias="from")
    end: Point = Field(alias="to")


class Swipe(Path):
    duration: DurationMs | None = None
    repeat: int = Field(
        default=1,
        ge=1,
        le=100,
        description="Repeated swipes share a 30-second motion budget, including repositioning.",
    )


class Clipboard(Empty):
    text: str = Field(min_length=1, max_length=10000)


PRIMITIVES: dict[str, type[Empty]] = {
    "tap": Press,
    "doubleClick": AtPoint,
    "longPress": Press,
    "swipe": Swipe,
    "dragAndDrop": Path,
    "homeButton": Empty,
    "backButton": Empty,
    "recentAppsButton": Empty,
}
ACTIONS: dict[str, tuple[type[Empty], str]] = {
    "sendToClipboard": (Clipboard, "Copy text to the phone clipboard for pasting."),
    "forceQuit": (Empty, "Close the current iOS app using the app switcher."),
    "unlockPhone": (
        Empty,
        "Unlock a configured test phone using its supported test passcode.",
    ),
}


_CONNECT_SCHEMA_KEYS: dict[str, tuple[str, ...]] = {
    "string": ("type", "description", "enum", "minLength", "maxLength"),
    "number": ("type", "description", "minimum", "maximum"),
    "integer": ("type", "description", "minimum", "maximum"),
    "boolean": ("type", "description"),
    "array": ("type", "description", "items", "minItems", "maxItems"),
    "object": ("type", "description", "properties", "required"),
}


def _filter_connect_schema(node: dict) -> dict:
    schema_type = node.get("type")
    allowed = (
        _CONNECT_SCHEMA_KEYS.get(schema_type, ("type", "description"))
        if isinstance(schema_type, str)
        else ("type", "description")
    )
    out = {k: node[k] for k in allowed if k in node}
    if schema_type == "object":
        out["properties"] = {
            k: _filter_connect_schema(v) for k, v in node.get("properties", {}).items()
        }
        if not out.get("required"):
            out.pop("required", None)
    elif schema_type == "array" and "items" in node:
        out["items"] = _filter_connect_schema(node["items"])
    return out


def _connect_param_schema(model: type[Empty]) -> dict | None:
    raw = model.model_json_schema()
    if not raw.get("properties"):
        return None
    return _filter_connect_schema(raw)


def device_info() -> dict:
    actions = []
    for name, (model, description) in ACTIONS.items():
        action: dict = {"name": name, "description": description}
        params = _connect_param_schema(model)
        if params is not None:
            action["params"] = params
        actions.append(action)
    return {
        "protocol": "midscene-connect/1",
        "name": "PhysiClaw",
        "description": DEVICE_DESCRIPTION,
        "pointerType": "touch",
        "primitives": list(PRIMITIVES),
        "actions": actions,
    }


def openapi() -> dict:
    schemas = {}
    paths = {}
    size = {"width": {"type": "integer"}, "height": {"type": "integer"}}
    outputs = {
        "info": {
            k: {
                "type": "array",
                "items": {"type": "string" if k == "primitives" else "object"},
            }
            if isinstance(v, list)
            else {"type": "string"}
            for k, v in device_info().items()
        },
        "size": size,
        "screenshot": {
            **size,
            "image": {"type": "string", "description": "JPEG data URL"},
        },
    }

    def response(props):
        return {
            "200": {
                "description": "Success",
                "content": {
                    "application/json": {
                        "schema": {"type": "object", "properties": props}
                    }
                },
            },
            "401": {"description": "Bearer token required"},
        }

    for name, props in outputs.items():
        paths[f"/{name}"] = {
            "get": {
                "tags": ["Device"],
                "operationId": name,
                "responses": response(props),
            }
        }
    for kind, models in (
        ("primitives", PRIMITIVES),
        ("actions", {k: v[0] for k, v in ACTIONS.items()}),
    ):
        for name, model in models.items():
            schema = model.model_json_schema(
                ref_template="#/components/schemas/{model}"
            )
            schemas.update(schema.pop("$defs", {}))
            output = {"ok": {"type": "boolean"}}
            if kind == "actions":
                output["result"] = {"type": "string"}
            paths[f"/{kind}/{name}"] = {
                "post": {
                    "operationId": name,
                    "tags": [kind.capitalize()],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": schema}},
                    },
                    "responses": response(output),
                }
            }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "PhysiClaw Midscene Device API",
            "version": "1",
            "description": "Inspect the connected phone and execute actions on the calibrated rig. Coordinates are pixels in the device screenshot.",
        },
        "servers": [{"url": BASE_PATH + "/api"}],
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        },
        "security": [{"bearer": []}],
    }
