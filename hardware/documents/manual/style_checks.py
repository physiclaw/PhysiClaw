"""Targeted checks for inline layout styles in the manual's JSON content.

This detects bare non-zero numbers in common length properties, not full CSS
syntax. Function contents and escaped identifiers are left to the browser.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

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
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_IMPORTANT = re.compile(r"\s*!\s*important\s*$", re.IGNORECASE)


def _top_level_css(style: str) -> str:
    """Mask comments, strings and blocks while preserving declaration offsets."""
    masked = list(style)
    closing = []
    quote = None
    index = 0
    while index < len(style):
        char = style[index]
        if char == "\\":
            end = min(index + 2, len(style))
        elif quote:
            if char == quote:
                quote = None
            end = index + 1
        elif style.startswith("/*", index):
            end = style.find("*/", index + 2)
            end = len(style) if end == -1 else end + 2
        elif char in "\"'":
            quote = char
            end = index + 1
        elif char in "([{":
            closing.append({"(": ")", "[": "]", "{": "}"}[char])
            end = index + 1
        elif closing:
            if char == closing[-1]:
                closing.pop()
            end = index + 1
        else:
            index += 1
            continue
        masked[index:end] = " " * (end - index)
        index = end
    return "".join(masked)


def _nonzero_number(token: str) -> bool:
    if not _NUMBER.fullmatch(token):
        return False
    # Only the significand determines zero; converting extreme exponents can fail.
    significand = token.lower().partition("e")[0]
    return any(digit in "123456789" for digit in significand)


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


def inline_style_unit_errors(pages: list[dict]) -> Iterator[str]:
    """Locate missing units once per declaration, with the page and JSON field."""
    for page in pages:
        context = f"page {page['page']!r}"
        for path, style in _inline_styles(page, context):
            offset = 0
            for masked in _top_level_css(style).split(";"):
                declaration = style[offset : offset + len(masked)]
                offset += len(masked) + 1
                # Escaped identifiers require CSS tokenization, beyond this check.
                if "\\" in declaration:
                    continue
                property_name, separator, value = masked.partition(":")
                if (
                    not separator
                    or property_name.strip().lower() not in _LENGTH_PROPERTIES
                ):
                    continue
                value = _IMPORTANT.sub("", value).strip()
                if any(_nonzero_number(token) for token in re.split(r"[\s/]+", value)):
                    yield (
                        f"{path}: {declaration.strip()!r} has a non-zero length without "
                        "a CSS unit. Add a unit such as mm or px; browsers ignore "
                        "this declaration."
                    )
