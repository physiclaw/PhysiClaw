"""The `ask` move — the human gate: its message, its reply words, its
wait and rounds, the resume hand a payment ask needs.
"""

from physiclaw.conductor.route.fields import (
    entry_message,
    limit_int,
    on_fail_mode,
    think_level,
    unique_list,
)
from physiclaw.conductor.route.resolve import argless_macro
from physiclaw.conductor.route.scope import Line, Scope
from physiclaw.conductor.spec import reply
from physiclaw.conductor.spec.limits import (
    DEFAULT_ASK_ROUNDS,
    DEFAULT_ASK_WAIT_SECONDS,
    MAX_ASK_ROUNDS,
    MAX_ASK_WAIT_SECONDS,
    MIN_ASK_WAIT_SECONDS,
)
from physiclaw.conductor.spec.model import (
    PAYMENT,
    AskNode,
    PlaybookError,
    check_name,
    require_str,
)
from physiclaw.macros.model import (
    checked_readings,
)


def reply_words(entry: dict, key: str, where: str) -> list[str]:
    """An ask's `yes:` / `no:` — the whole-message replies it reads, a
    non-empty list of distinct strings, stored in `reply.normalize`
    space so every reader compares without re-normalizing."""
    out = unique_list(
        entry.get(key),
        f"{where}: `{key}`",
        lambda w: reply.normalize(require_str(w, f"{where}: `{key}` entry")),
    )
    if not out:
        raise PlaybookError(f"{where}: `{key}` must list at least one reply word")
    return out


def parse_ask(scope: Scope, line: Line) -> AskNode:
    """An `ask`: a question over the channel whose reply words gate the
    walk. A payment ask reads its total off the page before it."""
    where, nid, entry, before = line.where, line.name, line.entry, line.before
    approve = require_str(entry.get("approve"), f"{where}: `approve`")
    check_name(approve, f"{where}: `approve`")
    if approve == PAYMENT:
        if before is None or "." in before:
            raise PlaybookError(
                f"{where}: a payment ask reads its total off the page before "
                "it — put the sheet's page waypoint immediately before the ask"
            )
    # A payment ask may quote the consent slot (a move literally named
    # `ask` is shadowed in this message — the money slot wins, both at
    # parse and at fill).
    g_payloads = scope.payloads_with_total() if approve == PAYMENT else scope.payloads
    message, msg_refs = entry_message(scope, where, entry, g_payloads)
    total: tuple[str, ...] = ()
    if approve == PAYMENT:
        if "ask.total" not in msg_refs:
            raise PlaybookError(
                f"{where}: a payment ask's `message` must quote the sheet "
                "total — reference {ask.total} (the ask IS the consent record)"
            )
        if "total_label" not in entry:
            raise PlaybookError(
                f"{where}: a payment ask declares `total_label:` — the label "
                "the sheet total sits beside, read off that row only"
            )
        total = checked_readings(
            entry, where, require_str, PlaybookError, key="total_label"
        )
    elif "total_label" in entry:
        raise PlaybookError(f"{where}: `total_label` goes with `approve: payment`")
    # The answer to a no quotes what the ask could (the total included).
    denied = None
    if entry.get("denied") is not None:
        denied, _ = entry_message(scope, where, entry, g_payloads, key="denied")
    wait_seconds, rounds = _ask_wait(entry, where)
    resume = None
    if entry.get("resume") is not None:
        resume = argless_macro(
            entry["resume"], "resume", where, nid, scope.resolve
        ).name
    return AskNode(
        id=nid,
        approve=approve,
        message=message,
        yes=tuple(reply_words(entry, "yes", where)),
        no=tuple(reply_words(entry, "no", where)),
        denied=denied,
        resume=resume,
        enter=before or "",
        total_label=total,
        wait_seconds=wait_seconds,
        silence_rounds=rounds,
        think=think_level(entry, where),
        on_fail=on_fail_mode(entry, where),
    )


def _ask_wait(entry: dict, where: str) -> tuple[int, int]:
    """An ask's patience — `wait:` seconds between reply polls and
    `rounds:` silent polls before the session suspends; both optional,
    defaults visible in the scaffold; bounded by the engine's single-sleep
    cap and a sane number of rounds."""
    seconds = limit_int(
        entry.get("wait", DEFAULT_ASK_WAIT_SECONDS),
        f"{where}: `wait`",
        MIN_ASK_WAIT_SECONDS,
        MAX_ASK_WAIT_SECONDS,
    )
    rounds = limit_int(
        entry.get("rounds", DEFAULT_ASK_ROUNDS), f"{where}: `rounds`", 1, MAX_ASK_ROUNDS
    )
    return seconds, rounds
