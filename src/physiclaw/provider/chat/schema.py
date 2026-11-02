"""Provider grammar schemas; the original OutputSpec decoder remains authoritative.

Only declared arguments are advertised. Constraints a grammar cannot express
remain in descriptions and in host validation. Optional fields stay optional:
OpenAI gets presence variants, never invented nulls or injected defaults.
"""

import json
from itertools import combinations

from physiclaw.common.provider_errors import ProviderConfigurationError

_SCALAR_TYPES = {
    type(None): "null",
    bool: "boolean",
    int: "integer",
    float: "number",
    str: "string",
}


def strict_output_schema(schema: dict) -> dict:
    def compile_node(node: dict, *, root: bool = False) -> dict:
        if not isinstance(node, dict):
            raise ProviderConfigurationError("Strict output needs typed JSON schemas")
        if root and node.get("type", "object") != "object":
            raise ProviderConfigurationError("Strict output requires an object root")
        kinds = node.get("type")
        if isinstance(kinds, list) and "object" in kinds:
            branches = []
            for kind in kinds:
                branch = {**node, "type": kind}
                if kind != "object":
                    for key in (
                        "properties",
                        "required",
                        "additionalProperties",
                        "minProperties",
                        "maxProperties",
                    ):
                        branch.pop(key, None)
                branches.append(compile_node(branch))
            return {"anyOf": branches}
        result = {
            k: v
            for k, v in node.items()
            if k not in ("title", "default", "examples", "deprecated")
        }
        # oneOf can be widened for generation; the original decoder still
        # rejects values that match multiple alternatives.
        if "oneOf" in result:
            if "anyOf" in result:
                raise ProviderConfigurationError(
                    "Strict output cannot combine oneOf and anyOf"
                )
            result["anyOf"] = result.pop("oneOf")
        if "allOf" in result:
            raise ProviderConfigurationError(
                "OpenAI strict output does not support allOf"
            )
        if "anyOf" in result:
            result["anyOf"] = [compile_node(b) for b in result["anyOf"]]
        if "const" in result:
            value = result.pop("const")
            result["enum"] = [value]
        if "type" not in result and "enum" in result:
            try:
                names = list(
                    dict.fromkeys(_SCALAR_TYPES[type(v)] for v in result["enum"])
                )
            except KeyError as exc:
                raise ProviderConfigurationError(
                    "Strict output requires scalar enum values"
                ) from exc
            result["type"] = names[0] if len(names) == 1 else names
        omitted = {}
        for key in ("minLength", "maxLength", "minProperties", "maxProperties"):
            if key in result:
                omitted[key] = result.pop(key)
        if omitted:
            result["description"] = (
                result.get("description", "")
                + " Constraints: "
                + json.dumps(omitted, separators=(",", ":"))
            ).strip()
        if "items" in result:
            result["items"] = compile_node(result["items"])
        if (
            root
            or result.get("type") == "object"
            or ("type" not in result and "properties" in result)
        ):
            if result.get("additionalProperties", False) is not False:
                raise ProviderConfigurationError(
                    "Strict output cannot represent arbitrary additional properties"
                )
            properties = {
                name: compile_node(prop)
                for name, prop in result.get("properties", {}).items()
            }
            result.update(
                type="object", properties=properties, additionalProperties=False
            )
            required = result.get("required", [])
            optional = [name for name in properties if name not in required]
            if optional:
                if root or len(optional) > 6:
                    raise ProviderConfigurationError(
                        "Strict output needs a required root and at most 6 optional fields per nested object"
                    )
                variants = []
                for count in range(len(optional) + 1):
                    for included in combinations(optional, count):
                        selected = {
                            k: v
                            for k, v in properties.items()
                            if k in required or k in included
                        }
                        variants.append(
                            {
                                **result,
                                "properties": selected,
                                "required": list(selected),
                            }
                        )
                return {"anyOf": variants}
            result["required"] = list(properties)
        if "type" not in result and "anyOf" not in result:
            raise ProviderConfigurationError(
                "Strict output needs a type or union for each field"
            )
        return result

    return compile_node(schema, root=True)
