"""The session viewer — one HTML page that shows a session whole.

The two record streams (events.jsonl, wire.jsonl) merged by time and
grouped by turn, every record kept — the exact message array each
provider request carried (frames inline from `images/`) with the turn's
new messages marked, the raw reply, each tool result's full text and
frames, the walk's readings, every micro decision with its own request
and reply, usage, warnings — plus the narrative files (notes, runtime
log, MCP log, summary) as tabs. Nothing is summarized away: a record the
viewer has no special rendering for still shows as its fields, and every
record has its raw line one click away.

The page references frames by their session-relative paths and nothing
else outside itself, so it serves two ways from one template: the
studio serves it live at `/sessions/<sid>/` with a picker of recent
sessions (`physiclaw studio --review`), and `physiclaw logs --save`
writes it into the zip as `session.html`, where it opens from the
session dir with no server.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from physiclaw.agent.trace.format import (
    CALL_WORDS,
    SENTINEL_WORDS,
    describe_call,
    fmt_tokens,
    note_text,
    summarize_event,
)
from physiclaw.agent.trace.store import load_summary, recent_sessions
from physiclaw.agent.trace.trace import WARNING_EVENTS, WARNING_WORDS
from physiclaw.common.logger.session_artifacts import iso_now
from physiclaw.common.text import json_span, read_text, write_text
from physiclaw.contract.wire import image_ref, leaf_blocks, reply_gist

VIEWER_FILE = "session.html"
_TEMPLATE = Path(__file__).with_name("viewer.html")
_TURN_LINE = re.compile(r"\bturn (\d+)\b")
_NARRATIVE = ("notes.md", "runtime.log", "mcp.log", "summary.json")


def load(
    session_dir: Path, sessions: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The viewer's model of one session dir: both streams merged by time
    and grouped by turn, each record stamped with its words (`_stamp`);
    `sessions` is the served page's picker (None for the static copy)."""
    records = _records(session_dir / "events.jsonl", "events") + _records(
        session_dir / "wire.jsonl", "wire"
    )
    # Same clock, millisecond stamps; ties keep events before wire and
    # both in file order, so a turn's request precedes its response.
    records.sort(key=lambda r: r["at"])
    _mark_request_deltas(records)
    groups: dict[int | None, dict[str, Any]] = {}
    current: int | None = None
    for r in records:
        turn = r["rec"].get("turn")
        if isinstance(turn, int):
            current = turn
        group = groups.setdefault(
            current, {"turn": current, "records": [], "t": r["at"]}
        )
        group["records"].append(r)
        group["t_end"] = r["at"]
        del r["at"]
        _stamp(r)
    narrative = {name: _read(session_dir / name) for name in _NARRATIVE}
    by_turn = _runtime_by_turn(narrative["runtime.log"])
    summary = load_summary(session_dir)
    usage = (summary or {}).get("usage") or {}
    return {
        "sid": session_dir.name,
        "generated_at": iso_now(),
        "summary": summary,
        "files": _inventory(session_dir),
        "groups": [
            {**group, "runtime": by_turn.get(turn, [])}
            for turn, group in groups.items()
        ],
        "narrative": narrative,
        "sessions": sessions,
        "words": {
            "calls": CALL_WORDS,
            "sentinels": SENTINEL_WORDS,
            "tokens": {
                k: fmt_tokens(usage[k])
                for k in ("input_tokens", "output_tokens")
                if isinstance(usage.get(k), int)
            },
        },
    }


def recent(sessions_root: Path, limit: int = 50) -> list[dict[str, Any]]:
    """The picker's rows: the newest sessions' ids and outcomes."""
    return [
        {"sid": s["sid"], "outcome": s.get("outcome") or {}}
        for s in recent_sessions(sessions_root, limit)
    ]


def render(model: dict[str, Any]) -> str:
    """The viewer page with `model` embedded — the JSON is escaped so no
    record text can close the script block."""
    data = json.dumps(model, ensure_ascii=False).replace("<", "\\u003c")
    return read_text(_TEMPLATE).replace("__SESSION_JSON__", data)


def write(session_dir: Path) -> Path:
    """Write the static viewer page into the session dir and return its
    path."""
    out = session_dir / VIEWER_FILE
    write_text(out, render(load(session_dir)))
    return out


def _stamp(r: dict[str, Any]) -> None:
    """The words and frames the page shows for this record, from the
    modules that own them — the page carries no vocabulary of its own."""
    rec = r["rec"]
    if r["src"] == "events":
        name = rec.get("event")
        if name in WARNING_EVENTS:
            r["warn"] = True
            r["words"] = WARNING_WORDS[name]
        try:
            line = summarize_event({**rec, "turn": None})
        except Exception:  # a malformed record still gets a row, wordless
            line = None
        if line:
            r["summary"] = line
        if name == "tool_result":
            args = (
                rec.get("arguments") if isinstance(rec.get("arguments"), dict) else {}
            )
            r["title"] = describe_call(str(rec.get("name", "?")), args)
            r["frames"] = [p for p in rec.get("images") or [] if isinstance(p, str)]
            if rec.get("name") == "note":
                r["note"] = note_text(args)
        return
    kind = rec.get("kind")
    # A turn's request and a micro record carry their frames the same
    # way (scrubbed refs in typed blocks), under different keys.
    sent = rec.get("messages") if kind == "request" else rec.get("request")
    if kind in ("request", "micro") and isinstance(sent, list):
        r["frames"] = [
            ref
            for m in sent
            if isinstance(m, dict)
            for b in leaf_blocks(m.get("content"))
            if (ref := image_ref(b))
        ]
    if kind in ("response", "micro") and isinstance(rec.get("raw"), dict):
        gist = reply_gist(rec["raw"])
        if gist is not None:
            r["gist"] = gist
        reason = rec.get("reason")
        if reason is None and kind == "micro" and gist is not None:
            # A record from before the reason rode along: read it off the reply.
            obj = json_span(gist["text"], "{", "}")
            reason = obj.get("reason") if isinstance(obj, dict) else None
        if reason:
            r["reason"] = str(reason)


def _records(path: Path, src: str) -> list[dict[str, Any]]:
    """Every line of one stream as `{src, line, rec}`; a line that is not
    JSON stays in as `{"unparsed": ...}` rather than vanishing. `at` is
    the sort key: the record's own stamp, or its predecessor's for a
    line without one, so it keeps its place in the file."""
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out
    at = ""
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                rec = None
            if not isinstance(rec, dict):
                rec = {"unparsed": line}
            at = str(rec.get("t") or at)
            out.append({"src": src, "line": n, "rec": rec, "at": at})
    return out


def _mark_request_deltas(records: list[dict[str, Any]]) -> None:
    """Mark each wire request with `new_from`, the index of its first
    message that differs from the previous request's, and `carried`, how
    many the previous request had. `new_from == carried` is the usual
    turn (the old array plus the news); less means the history itself
    changed from there (compaction, a stub for an old frame). Cache
    breakpoints move every turn and are not a change."""
    prev: list = []
    for r in records:
        rec = r["rec"]
        if r["src"] != "wire" or rec.get("kind") != "request":
            continue
        msgs = rec.get("messages")
        if not isinstance(msgs, list):
            continue
        cur = [_without_cache_marks(m) for m in msgs]
        same = 0
        for a, b in zip(prev, cur):
            if a != b:
                break
            same += 1
        r["new_from"], r["carried"] = same, len(prev)
        prev = cur


def _without_cache_marks(message: Any) -> Any:
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if not isinstance(content, list):
        return message
    return {
        **message,
        "content": [
            {k: v for k, v in b.items() if k != "cache_control"}
            if isinstance(b, dict)
            else b
            for b in content
        ],
    }


def _runtime_by_turn(text: str) -> dict[int | None, list[str]]:
    """runtime.log lines keyed by the turn they name; lines naming none go
    under null."""
    out: dict[int | None, list[str]] = {}
    for line in text.splitlines():
        m = _TURN_LINE.search(line)
        key = int(m.group(1)) if m else None
        out.setdefault(key, []).append(line)
    return out


def _inventory(session_dir: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for p in sorted(session_dir.rglob("*")):
        if p.is_file() and p.name != VIEWER_FILE:
            files.append(
                {
                    "name": p.relative_to(session_dir).as_posix(),
                    "bytes": p.stat().st_size,
                }
            )
    return files


def _read(path: Path) -> str:
    try:
        return read_text(path)
    except OSError:
        return ""


__all__ = ["VIEWER_FILE", "load", "recent", "render", "write"]
