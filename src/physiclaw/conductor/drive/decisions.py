"""Recorded decision calls, measured — behind `playbooks micro` and the
decisions section of `playbooks stats`.

`load` reads a session's micro records (`MicroRecord`, whole) back into
messages; `ask` re-sends one to a live provider under the recorded
think level or another and reads each reply with the walk's own
parser; `summarize` folds the replies into validity, agreement with
what the wake read, and cost. `session_decisions` and `decision_stats`
read the same cost off a session's events (`micro_call` beside its
`usage`) with no provider.
"""

import base64
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from physiclaw.common.logger.session_artifacts import mime_for
from physiclaw.common.text import iter_jsonl
from physiclaw.conductor.walk.micro import move_key, parse_reply, reply_args
from physiclaw.contract.dto import (
    USAGE_CALL_MICRO,
    ContentBlock,
    ImageBlock,
    Message,
    TextBlock,
    Thinking,
    message_of,
)
from physiclaw.contract.plugin import ChatProvider
from physiclaw.contract.wire import image_ref, leaf_blocks


@dataclass(frozen=True)
class Recorded:
    """One micro call as the wake made it, its request as messages."""

    call: str
    node: str
    allowed: tuple[str, ...]
    messages: tuple[Message, ...]
    answer: str | None  # what the caller read; None = invalid reply
    confidence: float | None
    thinking: Thinking | None
    args: dict[str, Any] | None = None  # a tool call's arguments, as read


@dataclass(frozen=True)
class Reply:
    """One re-ask's reading."""

    answer: str | None
    confidence: float | None
    valid: bool
    ms: int
    output: int
    reasoning: int
    error: str = ""
    args: dict[str, Any] | None = None


@dataclass(frozen=True)
class Row:
    recorded: Recorded
    replies: tuple[Reply, ...]


def load(session_dir: Path, only: str | None = None) -> list[Recorded]:
    """Every micro record in a session's wire log, in order — or only
    those whose call or node is `only`, judged before any frame is read.
    A record from before the log kept the caller's reading (no
    `allowed`) still loads — its replies can be asked, not judged."""
    path = session_dir / "wire.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"no wire.jsonl for session {session_dir.name!r}")
    frames: dict[Path, ImageBlock | None] = {}
    return [
        Recorded(
            call=str(rec.get("call") or ""),
            node=str(rec.get("node") or ""),
            allowed=tuple(rec.get("allowed") or ()),
            messages=tuple(_messages(rec.get("request") or [], session_dir, frames)),
            answer=rec.get("answer"),
            confidence=rec.get("confidence"),
            thinking=rec.get("thinking"),
            args=rec.get("args"),
        )
        for rec in iter_jsonl(path, '"micro"')
        if rec.get("kind") == "micro"
        and (only is None or only in (rec.get("call"), rec.get("node")))
    ]


def _messages(
    request: list[dict], session_dir: Path, frames: dict[Path, ImageBlock | None]
) -> list[Message]:
    """Recorded messages back to the shapes the provider door takes: a
    text content as is; a block list as its text and the frames its
    refs name, read back from the session's `images/` so the re-ask
    sees the screen the wake saw (a ref whose file is gone is skipped;
    the call still asks over the listing). `frames` memoizes the reads:
    an episode's records replay the whole prefix, so one frame recurs
    across them. `leaf_blocks` also reads a record from before the log
    kept its own shape."""
    out: list[Message] = []
    for m in request:
        role = str(m.get("role") or "user")
        content = m.get("content")
        if isinstance(content, str) or role != "user":
            out.append(
                message_of(
                    role,
                    "\n".join(
                        b.get("text", "")
                        for b in leaf_blocks(content)
                        if b.get("type") == "text"
                    ),
                )
            )
            continue
        blocks: list[ContentBlock] = []
        for b in leaf_blocks(content):
            ref = image_ref(b)
            if ref is not None:
                path = session_dir / ref
                if path not in frames:
                    frames[path] = _frame(path)
                frame = frames[path]
                if frame is not None:
                    blocks.append(frame)
            elif b.get("type") == "text":
                blocks.append(TextBlock(text=b.get("text", "")))
        texts = [b for b in blocks if isinstance(b, TextBlock)]
        if len(texts) == len(blocks):
            out.append(message_of(role, "\n".join(b.text for b in texts)))
        else:
            out.append(message_of(role, blocks))
    return out


def _frame(path: Path) -> ImageBlock | None:
    """A filed frame back as the block the wake sent; None when the file
    is gone (retention) or unreadable."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return ImageBlock(
        media_type=mime_for(path.name), data_b64=base64.b64encode(data).decode()
    )


async def ask(
    provider: ChatProvider,
    rec: Recorded,
    *,
    thinking: Thinking | None = None,
    reps: int = 1,
) -> Row:
    """Re-send one recorded call `reps` times — under `thinking` when
    given, else as the wake asked — and read each reply the way the walk
    does. Sequential on purpose: each reply's time is the measurement,
    and concurrent asks of one provider would measure contention."""
    level = thinking if thinking is not None else rec.thinking
    replies: list[Reply] = []
    for _ in range(reps):
        t0 = time.perf_counter()
        try:
            asst = await provider.chat(
                list(rec.messages), [], purpose=USAGE_CALL_MICRO, thinking=level
            )
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            replies.append(
                Reply(None, None, False, ms, 0, 0, error=f"{type(e).__name__}: {e}")
            )
            continue
        ms = int((time.perf_counter() - t0) * 1000)
        parsed, _ = parse_reply(asst.content or "", rec.allowed, rec.call)
        replies.append(
            Reply(
                answer=parsed[0] if parsed else None,
                confidence=parsed[2] if parsed else None,
                valid=parsed is not None and bool(rec.allowed),
                ms=ms,
                output=asst.usage.completion_tokens,
                reasoning=asst.usage.reasoning_tokens,
                args=reply_args(parsed[3]) if parsed else None,
            )
        )
    return Row(recorded=rec, replies=tuple(replies))


def agreed(reply: Reply, recorded: Recorded) -> bool:
    """Whether a re-ask made the wake's move: a valid reply, the same
    tool, and the same locating args (`move_key`) — never the model's
    own wording. The one rule the summary and the CLI's per-row word
    share."""
    if not reply.valid or reply.answer is None or recorded.answer is None:
        return False
    return move_key(reply.answer, reply.args or {}) == move_key(
        recorded.answer, recorded.args or {}
    )


def _mean(total: float, n: int) -> float:
    return total / n if n else 0.0


@dataclass
class _Cost:
    """Time and tokens over `n` calls — what both folds report."""

    ms_total: int = 0
    ms_max: int = 0
    output_total: int = 0
    reasoning_total: int = 0

    @property
    def n(self) -> int:
        raise NotImplementedError

    def _add(self, ms: int, output: int, reasoning: int) -> None:
        self.ms_total += ms
        self.ms_max = max(self.ms_max, ms)
        self.output_total += output
        self.reasoning_total += reasoning

    @property
    def mean_ms(self) -> float:
        return _mean(self.ms_total, self.n)

    @property
    def mean_reasoning(self) -> float:
        return _mean(self.reasoning_total, self.n)

    @property
    def mean_output(self) -> float:
        return _mean(self.output_total, self.n)


@dataclass
class Summary(_Cost):
    """The numbers a change is judged by, over every reply asked."""

    calls: int = 0
    asks: int = 0
    valid: int = 0
    agreed: int = 0
    errors: int = 0

    @property
    def n(self) -> int:
        return self.asks

    @property
    def valid_rate(self) -> float:
        return _mean(self.valid, self.asks)

    @property
    def agreement(self) -> float:
        return _mean(self.agreed, self.asks)


def summarize(rows: list[Row]) -> Summary:
    s = Summary(calls=len(rows))
    for row in rows:
        for r in row.replies:
            s.asks += 1
            s.valid += r.valid
            s.agreed += agreed(r, row.recorded)
            s.errors += bool(r.error)
            s._add(r.ms, r.output, r.reasoning)
    return s


# ---------- what recorded walks' decisions cost (no provider) ----------


@dataclass
class DecisionStats(_Cost):
    """One call kind's decisions across sessions — what `playbooks stats`
    renders beside the escalation rate."""

    calls: int = 0
    escalated: int = 0  # no outcome: invalid twice, under the floor, failed
    repaired: int = 0  # answered on the repair retry
    rows_max: int = 0
    nodes: Counter = field(default_factory=Counter)

    @property
    def n(self) -> int:
        return self.calls


def session_decisions(session_dir: Path) -> list[dict[str, Any]]:
    """Every decision a session made, each with its token buckets: the
    `micro_call` event joined to the `usage` events its attempts wrote
    just before it (the provider writes usage as a call returns; the
    caller writes the decision once it has read the reply)."""
    out: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for ev in iter_jsonl(session_dir / "events.jsonl", '"micro'):
        kind = ev.get("event")
        if kind == "usage" and ev.get("call") == USAGE_CALL_MICRO:
            pending.append(ev)
        elif kind == "micro_call":
            out.append(
                {
                    "call": ev.get("call"),
                    "node": ev.get("node"),
                    "out": ev.get("out"),
                    "attempts": int(ev.get("attempts") or 0),
                    "elapsed_ms": int(ev.get("elapsed_ms") or 0),
                    "rows": int(ev.get("rows") or 0),
                    "output": sum(int(u.get("output") or 0) for u in pending),
                    "reasoning": sum(int(u.get("reasoning") or 0) for u in pending),
                    "model": next((u.get("model") for u in pending), None),
                }
            )
            pending = []
    return out


def decision_stats(session_dirs: list[Path]) -> dict[str, DecisionStats]:
    """Fold the decisions of many sessions per call kind."""
    stats: dict[str, DecisionStats] = {}
    for d in session_dirs:
        for dec in session_decisions(d):
            st = stats.setdefault(str(dec["call"]), DecisionStats())
            st.calls += 1
            st.escalated += dec["out"] is None
            st.repaired += dec["attempts"] > 1 and dec["out"] is not None
            st.rows_max = max(st.rows_max, dec["rows"])
            st.nodes[str(dec["node"] or "?")] += 1
            st._add(dec["elapsed_ms"], dec["output"], dec["reasoning"])
    return stats
