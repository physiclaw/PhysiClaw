"""What a rehearsal reports — the model round-trips it kept (`ModelLog`,
the wire sink), each exchange as a record and as lines for the eye,
and a tool result as lines. `rehearsal.walk` drives; this renders.
"""

import json

from physiclaw.common.listing import is_header
from physiclaw.common.verdict import all_text
from physiclaw.conductor.micro.decision import DecisionRequest, MicroResult
from physiclaw.conductor.micro.moves import describe_move, move_of
from physiclaw.conductor.spec.match import Verdict
from physiclaw.contract.dto import MicroRecord
from physiclaw.contract.wire import leaf_blocks, reply_gist


class ModelLog:
    """A `contract.plugin.WireSink` for a rehearsal: every model
    round-trip the micro-caller makes (each attempt's serialized
    request and the provider's raw reply), kept in memory so the
    debugger can show exactly what went to the provider and what came
    back. `walk` drains it after each decision."""

    def __init__(self) -> None:
        self.exchanges: list[dict] = []

    def write_micro(self, rec: MicroRecord) -> None:
        self.exchanges.append(
            {"call": rec.call, "request": rec.request, "reply": rec.raw}
        )

    def drain(self) -> list[dict]:
        out, self.exchanges = self.exchanges, []
        return out


def exchanges(drained: list[dict], req: DecisionRequest, decision: str) -> list[dict]:
    """The drained round-trips of one decision as debugger records:
    which call and node, the attempt (a repair retry is a second
    round-trip), how many replayed episode turns the request carries
    (the record is whole; the rendering folds them, since earlier
    calls showed them), the decision the caller made of the last
    reply, and `lines` — the round-trip rendered once for every eye
    (the CLI prints them, the studio shows them), so no skin parses
    the provider wire itself."""
    out = []
    for i, x in enumerate(drained, start=1):
        record = {
            **x,
            "node": req.node_id,
            "attempt": i,
            "attempts": len(drained),
            "history": len(req.history),
            "outcome": decision if i == len(drained) else "",
        }
        record["lines"] = exchange_lines(record)
        out.append(record)
    return out


def exchange_lines(record: dict) -> list[str]:
    """Show the system message, current request, repair messages, and reply.

    ``history`` counts individual messages, matching DecisionRequest.history.
    Hide only that prefix after the system message; keep the full request
    in the record for inspection.
    """
    head = f"── model {record['call']} ({record['node']})"
    if record["attempts"] > 1:
        head += f" attempt {record['attempt']}/{record['attempts']}"
    history_messages = record["history"]
    if history_messages:
        head += f" · {history_messages} replayed message(s) folded"
    lines = [head]
    request = record["request"]
    shown = request[:1] + request[1 + history_messages :]
    for m in shown:
        lines.append(f"[{m.get('role', '?')}]")
        lines.extend(_message_text(m).splitlines())
    lines.append("── reply")
    lines.extend(reply_text(record["reply"]).splitlines())
    if record["outcome"]:
        lines.append(f"── decision: {record['outcome']}")
    return lines


def _message_text(message: dict) -> str:
    """A serialized message's text, whichever wire shape — the wire
    codec's own flattening."""
    return all_text(list(leaf_blocks(message.get("content"))))


def reply_text(raw: dict) -> str:
    """The model's text out of a raw provider reply — the two wire
    shapes in the tree (`wire.reply_gist`), else the reply verbatim."""
    gist = reply_gist(raw)
    return gist["text"] if gist is not None else json.dumps(raw, ensure_ascii=False)


def describe_verdict(v: Verdict) -> str:
    """What the matcher made of the screen the next turn acts on."""
    return f"screen reads {v.describe()}"


def describe_result(result: MicroResult) -> str:
    o = result.outcome
    if o is None:
        return f"no outcome — {result.detail} ({result.elapsed_ms} ms)"
    picked = f" → {describe_move(*move_of(o))}" if o.picked is not None else ""
    fields = f" {o.payload}" if o.payload else ""
    return (
        f"{o.out}{picked}{fields} [{o.confidence:.2f}] {o.reason} "
        f"({result.elapsed_ms} ms)"
    )


def args_text(arguments: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in (arguments or {}).items())


def result_lines(text: str, verbose: bool) -> list[str]:
    """A result's text for the eye: a macro's header and step log always,
    the listing rows only when `verbose`. Shared with the macro stepping
    driver, which reads a macro run the same way."""
    lines = text.splitlines()
    head: list[str] = []
    for i, line in enumerate(lines):
        if is_header(line):
            return head + (lines[i:] if verbose else [f"({len(lines) - i - 1} rows)"])
        head.append(line)
    return head
