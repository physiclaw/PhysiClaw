"""Template-pack placeholders — the `<<TOKEN>>` grammar and its local
values file, in one module below both parsers (`macros`, `conductor`).

The sigil is deliberately NOT the macro grammar's `{input}` braces: a
placeholder is a per-INSTALLATION constant, while inputs are per-run
values. Pack files keep their tokens verbatim on disk (shareable,
diffable against the template); values live ONLY in the local
`playbooks/placeholders.yml` and are filled at load. A token with no
value fails loudly instead of, say, searching WeChat for the literal
string "<<CONTACT>>".
"""

import io
import re
from pathlib import Path
from typing import Any

from physiclaw.common import paths
from physiclaw.common.text import read_text, write_text
from physiclaw.common.yaml import safe_yaml

PLACEHOLDER_RE = re.compile(r"<<([A-Z][A-Z0-9_]*)>>")

# ~/.physiclaw/playbooks/placeholders.yml — the flat {TOKEN: value}
# map `resolve_placeholders` fills from. Written by `physiclaw
# playbooks install`, hand-editable.
PLACEHOLDER_VALUES_FILENAME = "placeholders.yml"

# YAML 1.2 safe handler for the values file — this module's one
# instance (the `specfile.yaml_loader` pattern, one layer down).
_yaml = safe_yaml()
# Every key/value is literal text. Quoting also avoids the emitter's invalid
# flow scalars for values such as `?Alice`.
_yaml.default_style = '"'  # type: ignore[assignment]  # ruamel infers this as None


def find_placeholders(text: str) -> list[str]:
    """Unpopulated template tokens in `text`, first-seen order, deduped."""
    return list(dict.fromkeys(PLACEHOLDER_RE.findall(text)))


def fill_placeholders(text: str, values: dict[str, str]) -> str:
    """Substitute `<<TOKEN>>` occurrences from `values`, leaving unknown
    tokens in place (`resolve_placeholders` rejects survivors) — the
    sigil's one writer, beside its one reader above."""
    return PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def placeholder_values() -> dict[str, str]:
    """The local `playbooks/placeholders.yml` as a flat str→str map;
    {} when absent. Raises ValueError on a malformed file — silence
    here would resurface as a baffling "unpopulated placeholder" at
    every pack parse."""
    path = paths.playbooks_dir() / PLACEHOLDER_VALUES_FILENAME
    if not path.exists():
        return {}
    try:
        data = _yaml.load(read_text(path))
    except Exception as e:
        raise ValueError(
            f"{PLACEHOLDER_VALUES_FILENAME}: invalid YAML: {e or type(e).__name__}"
        ) from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"{PLACEHOLDER_VALUES_FILENAME} must be a flat TOKEN: value mapping"
        )
    for key, value in data.items():
        if not isinstance(key, str):
            raise ValueError(
                f"{PLACEHOLDER_VALUES_FILENAME}: token names must be strings"
            )
        if not isinstance(value, str):
            raise ValueError(
                f"{PLACEHOLDER_VALUES_FILENAME}: {key} must be a string — "
                "quote literal values such as numbers or true/false"
            )
    return data


def write_placeholder_values(
    values: dict[str, str], *, path: Path | None = None
) -> None:
    """Write the full `playbooks/placeholders.yml` map — the reader
    above's one counterpart, so the file shape never drifts. Installers
    may supply a private staging `path` before committing the file."""
    buf = io.StringIO()
    _yaml.dump(dict(sorted(values.items())), buf)
    path = paths.playbooks_dir() / PLACEHOLDER_VALUES_FILENAME if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(
        path,
        "# Per-installation constants — filled into pack files' <<TOKEN>>s\n"
        "# at load. Written by `physiclaw playbooks install`; hand-editable.\n"
        + buf.getvalue(),
    )


def resolve_placeholders(
    text: str, error_cls: type[Exception], values: dict[str, str] | None = None
) -> str:
    """`text` with its `<<TOKEN>>`s filled from the local values file —
    plain-text wrapper for prompt files and string terminal for parsed
    YAML. A surviving token (or a broken values file) raises
    `error_cls`, so the message cannot drift between grammars."""
    values = _load_values(values, error_cls)
    if values:
        text = fill_placeholders(text, values)
    tokens = find_placeholders(text)
    if tokens:
        raise error_cls(
            f"unpopulated template placeholder(s) {', '.join(tokens)} — "
            f"add values to playbooks/{PLACEHOLDER_VALUES_FILENAME} "
            "(`physiclaw playbooks install` prompts for them)"
        )
    return text


def resolve_data_placeholders(
    data: Any, error_cls: type[Exception], values: dict[str, str] | None = None
) -> Any:
    """Fill string keys and values AFTER parsing a YAML document.

    A contact's quotes, backslashes, colons, or numeric spelling are
    literal text, never YAML syntax. Callers reject aliases before this
    tree walk. Plain prompt files use `resolve_placeholders` instead.
    """
    values = _load_values(values, error_cls)

    def fill(value: Any) -> Any:
        if isinstance(value, str):
            return resolve_placeholders(value, error_cls, values)
        if isinstance(value, list):
            return [fill(item) for item in value]
        if isinstance(value, dict):
            filled = {}
            for key, item in value.items():
                resolved_key = fill(key)
                if resolved_key in filled:
                    raise error_cls(
                        f"duplicate key after placeholder substitution: {resolved_key!r}"
                    )
                filled[resolved_key] = fill(item)
            return filled
        return value

    return fill(data)


def _load_values(
    values: dict[str, str] | None, error_cls: type[Exception]
) -> dict[str, str]:
    if values is not None:
        return values
    try:
        return placeholder_values()
    except ValueError as e:
        raise error_cls(str(e)) from e
