"""Session-listing corpus — recorded screens for offline matching work.

`events.jsonl` keeps every screen the runtime saw, whole, on its
`tool_result` (a walk's turns included — those send no request, so
`model.jsonl` never carries them); a session recorded before the trace
kept them falls back to the wire log's requests. This module owns only
"is this text a screen" and the corpus file format. A corpus file is
JSONL, one screen per line::

    {"label": "taobao.results" | "other" | "?", "listing": "<text>"}

`extract` prefills labels with "?" for the human to edit; `partition`
consumes the labeled file — `<app>.<page>` lines are that page's genuine
observations, everything else labeled non-"?" is a hard negative.
"""

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from physiclaw.common import paths
from physiclaw.common.listing import Screen, is_screen
from physiclaw.common.text import iter_jsonl, read_text, write_text
from physiclaw.contract.wire import iter_request_texts

UNLABELED = "?"


@dataclass(frozen=True)
class CorpusItem:
    label: str
    listing: str


def session_listings(sid: str) -> list[str]:
    """Every distinct listing text in one recorded session, in first-seen
    order — the tool results' own record when the session kept it, else
    (a session recorded before the trace kept them) the wire log's
    requests."""
    d = paths.sessions_dir() / sid
    events, wire_log = d / "events.jsonl", d / "model.jsonl"
    if events.exists():
        found = _screens(_result_texts(events))
        if found:
            return found
    if not wire_log.exists():
        raise FileNotFoundError(f"no events.jsonl or model.jsonl for session {sid!r}")
    # The SYSTEM prompt quotes the listing header verbatim in doctrine,
    # so system messages must not extract as screens.
    return _screens(
        text for role, text in iter_request_texts(wire_log) if role != "system"
    )


def _screens(texts: Iterable[str]) -> list[str]:
    """The screens among `texts`, deduped in first-seen order."""
    seen: dict[str, None] = {}
    for text in texts:
        if text not in seen and is_screen(text):
            seen[text] = None
    return list(seen)


def _result_texts(path: Path) -> Iterator[str]:
    """The `text` of every `tool_result` event in an events.jsonl."""
    for rec in iter_jsonl(path, '"tool_result"'):
        if rec.get("event") == "tool_result" and isinstance(rec.get("text"), str):
            yield rec["text"]


def partition(
    items: list[CorpusItem], app: str, page_names: set[str]
) -> tuple[dict[str, list[Screen]], list[Screen]]:
    """Split a labeled corpus for `capture_app`: `<app>.<page>` lines are
    that page's genuine observations; any other non-'?' label is a hard
    negative; '?' lines are ignored."""
    prefix = f"{app}."
    by_page: dict[str, list[Screen]] = {}
    negatives: list[Screen] = []
    for it in items:
        if it.label == UNLABELED:
            continue
        page = it.label[len(prefix) :] if it.label.startswith(prefix) else None
        screen = Screen.read(it.listing)
        if page in page_names:
            by_page.setdefault(page, []).append(screen)
        else:
            negatives.append(screen)
    return by_page, negatives


def write_corpus(path: Path, items: list[CorpusItem]) -> None:
    lines = [
        json.dumps({"label": it.label, "listing": it.listing}, ensure_ascii=False)
        for it in items
    ]
    write_text(path, "\n".join(lines) + "\n")


def read_corpus(path: Path) -> list[CorpusItem]:
    out: list[CorpusItem] = []
    # JSON strings may contain Unicode line separators; only LF ends a record.
    for n, line in enumerate(read_text(path).split("\n"), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            if (
                not isinstance(rec, dict)
                or not isinstance(rec.get("label"), str)
                or not isinstance(rec.get("listing"), str)
            ):
                raise ValueError("expected an object with string label and listing")
            out.append(CorpusItem(label=rec["label"], listing=rec["listing"]))
        except ValueError as e:
            raise ValueError(f"{path}:{n}: bad corpus line ({e})") from e
    return out
