"""Targeted checks for inline layout styles in the manual's JSON content.

This is not a full CSS validator. It detects non-zero numbers without units
in length properties, which browsers silently ignore. Function values such
as calc() and var() are left to the browser to avoid flagging valid numbers.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from decimal import Decimal

_LENGTH_PROPERTIES = {
    "top",
    "right",
    "bottom",
    "left",
    "inset",
    "width",
    "height",
    "min-width",
    "min-height",
    "max-width",
    "max-height",
    "margin",
    "margin-top",
    "margin-right",
    "margin-bottom",
    "margin-left",
    "padding",
    "padding-top",
    "padding-right",
    "padding-bottom",
    "padding-left",
    "gap",
    "row-gap",
    "column-gap",
    "font-size",
    "letter-spacing",
    "word-spacing",
    "text-indent",
    "flex-basis",
    "border-width",
    "border-radius",
    "outline-width",
    "outline-offset",
}
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_IMPORTANT = re.compile(r"\s*!\s*important\s*$", re.IGNORECASE)


def _inline_styles(node: object, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(node, list):
        for index, child in enumerate(node):
            yield from _inline_styles(child, f"{path}[{index}]")
    elif isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}"
            if key.lower().endswith("style") and isinstance(value, str):
                yield child_path, value
            elif isinstance(value, (dict, list)):
                yield from _inline_styles(value, child_path)


def inline_style_unit_warnings(pages: list[dict]) -> Iterator[str]:
    """Locate missing units once per declaration, with the page and JSON field."""
    for page in pages:
        context = f"page {page['page']!r}"
        for path, style in _inline_styles(page, context):
            for declaration in _COMMENT.sub("", style).split(";"):
                property_name, separator, value = declaration.partition(":")
                if (
                    not separator
                    or property_name.strip().lower() not in _LENGTH_PROPERTIES
                ):
                    continue
                value = _IMPORTANT.sub("", value).strip()
                if "(" in value or ")" in value:
                    continue
                if any(
                    _NUMBER.fullmatch(token) and Decimal(token) != 0
                    for token in value.split()
                ):
                    yield (
                        f"{path}: {declaration.strip()!r} has a non-zero length without "
                        "a CSS unit. Add a unit such as mm or px; browsers ignore "
                        "this declaration."
                    )
