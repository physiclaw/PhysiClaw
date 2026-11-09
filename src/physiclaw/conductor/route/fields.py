"""The field rules every move parser shares — a closed word, a bounded
int, a `with:` block, a message with refs, the reply words — so a
rule has one home and one wording.
"""

from collections.abc import Callable
from typing import Any

from physiclaw.conductor.route.scope import Line, Scope
from physiclaw.conductor.spec.limits import (
    MAX_MESSAGE_LINES,
)
from physiclaw.conductor.spec.model import (
    IRREVERSIBLE_CLASSES,
    ON_FAIL_MODES,
    PlaybookError,
    prose,
)
from physiclaw.conductor.spec.refs import (
    check_arg_refs,
    check_refs,
    refs_in,
)
from physiclaw.contract.dto import THINKING_LEVELS, Thinking
from physiclaw.macros.model import (
    MacroInput,
)


def unique_list[T](raw: Any, where: str, check: Callable[[Any], T]) -> list[T]:
    """A list of distinct entries, each validated (and normalized) by
    `check` — the one shape `tools` and the reply words share."""
    if not isinstance(raw, list):
        raise PlaybookError(f"{where} must be a list")
    out: list[T] = []
    for item in raw:
        value = check(item)
        if value in out:
            raise PlaybookError(f"{where}: duplicate entry {value!r}")
        out.append(value)
    return out


def closed_word(entry: dict, key: str, allowed: tuple[str, ...], where: str) -> Any:
    """An optional key whose value is one word of a closed vocabulary —
    absent stays None."""
    word = entry.get(key)
    if word is not None and word not in allowed:
        raise PlaybookError(
            f"{where}: `{key}` must be one of {', '.join(allowed)} (got {word!r})"
        )
    return word


def on_fail_mode(entry: dict, where: str) -> str | None:
    """An entry's optional `on_fail:` — stop or handover once its own
    means are spent; absent = handover."""
    return closed_word(entry, "on_fail", ON_FAIL_MODES, where)


def think_level(entry: dict, where: str) -> Thinking | None:
    """A model step's optional `think:` — how much hidden thinking its
    calls ask for; absent leaves the vendor's default (`playbooks
    check` says so, since a thinking model's default is minutes per
    call)."""
    think: Thinking | None = closed_word(entry, "think", THINKING_LEVELS, where)
    return think


def irreversible_class(entry: dict, where: str) -> str | None:
    """A move's optional `irreversible:` class — the same closed
    vocabulary on a `do` and an `agent`."""
    irreversible: str | None = closed_word(
        entry, "irreversible", IRREVERSIBLE_CLASSES, where
    )
    return irreversible


def limit_int(value: Any, where: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise PlaybookError(f"{where} must be {lo}–{hi} (got {value!r})")
    return value


def entry_message(
    scope: Scope,
    where: str,
    entry: dict,
    payloads: dict[str, tuple[str, ...]],
    key: str = "message",
) -> tuple[str, set[str]]:
    """A REQUIRED authored `message:` (or an ask's `denied:`, the same
    shape under `key`) — the exact text sent to the user; only the
    author knows the user's language, so the conductor composes no prose
    around it. Refs held to the same defined-before-use rules as `with:`
    values; returned with them so the ask lints can inspect."""
    text = prose(entry.get(key), f"{where}: `{key}`", lines=MAX_MESSAGE_LINES)
    refs = refs_in(text, f"{where}: `{key}`")
    check_refs(refs, scope.input_names, payloads, f"{where}: `{key}`")
    return text, refs


def check_with(
    where: str,
    args: dict,
    inputs: "tuple[MacroInput, ...]",
    what: str,
    filled: frozenset[str] = frozenset(),
) -> None:
    """A move's `with:` against the inputs of what it runs (a macro, a
    playbook): every key an input, every required input filled — by
    `with`, or by the one `each` fills (`filled`)."""
    declared = {inp.name: inp for inp in inputs}
    unknown = sorted(set(args.keys()) - set(declared))
    if unknown:
        raise PlaybookError(
            f"{where}: `with` key(s) {', '.join(unknown)} are not inputs of "
            f"{what} (declares: {', '.join(sorted(declared)) or '(none)'})"
        )
    missing = sorted(
        n
        for n, i in declared.items()
        if i.required and n not in args and n not in filled
    )
    if missing:
        raise PlaybookError(
            f"{where}: {what} requires input(s) {', '.join(missing)} — "
            "supply them under `with`" + (" (or one with `each:`)" if filled else "")
        )


def limit_mapping(entry: dict, where: str, keys: set[str]) -> dict:
    """An entry's optional `limit:` mapping, its keys held to `keys` —
    empty when absent; each caller reads its own bounds off it."""
    raw = entry.get("limit")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PlaybookError(f"{where}: `limit` must be a mapping")
    unknown = sorted(set(map(str, raw)) - keys)
    if unknown:
        raise PlaybookError(f"{where}: `limit`: unknown key(s): {', '.join(unknown)}")
    return raw


def with_args(scope: Scope, line: Line) -> dict:
    """A move's `with:` block — the mapping of arguments it hands what it
    runs, every ref held to the defined-before-use rule (`check_arg_refs`).
    {} when the line carries none."""
    args = line.entry.get("with", {})
    if not isinstance(args, dict):
        raise PlaybookError(f"{line.where}: `with` must be a mapping of arguments")
    check_arg_refs(args, scope.input_names, scope.payloads, line.where)
    return args
