"""Persist and render the scripted conversation in `debug/thread.json`.

Writers hold one process lock across read, update, and atomic publication.
`load` returns a snapshot; use `stage`, `record_send`, or `peek_bubbles` to
update it. `save` and `seed` replace the entire script.

A staged reply is released only on a peek after an agent send. The send's
own result stays the gate's baseline, so it cannot consume its own reply.

Listings use the real channel fingerprint: learned anchor positions, then
declared region centers, then default chrome positions. Incoming bubbles
occupy the configured IM side; agent bubbles occupy the other. The listing
therefore passes through the ordinary page matcher and reply reader.

Wire blocks mirror the server: `[image, listing]` for views and
`[action text, image, listing]` for gestures. A view must start with an
image so `verdict.action_text` cannot mistake the listing for action text.
"""

import json
import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

from physiclaw.common import paths
from physiclaw.common.bbox import Bbox
from physiclaw.common.file_lock import exclusive_file_lock
from physiclaw.common.listing import Element, format_elements
from physiclaw.common.logger import write_json_atomic
from physiclaw.common.text import read_text
from physiclaw.conductor.spec.conventions import CHANNEL_APP, THREAD_PAGE
from physiclaw.conductor.spec.pages import PagePrint
from physiclaw.conductor.spec.reply import INCOMING_LEFT, incoming_box

log = logging.getLogger(__name__)

THREAD_SCHEMA = 1
USER = "user"
AGENT = "agent"

# Bubble geometry: user bubbles hang from the near edge of the IM's
# incoming box, the agent's from the mirrored edge — the sides the
# reader keys on — and a line runs from its edge as far as its text
# needs, one character's width per character, up to one bubble's width.
# Bands start below the anchor chrome and stack downward like a real
# thread scrolled to its tail.
_BUBBLE_EDGE = 0.18  # a bubble's near edge, in from the screen's (the avatar)
_BUBBLE_MAX_W = 0.64  # the widest a bubble's line runs
_CHAR_W = 0.04  # one character's width
_BUBBLE_TOP = 0.20  # first bubble's center y, below the anchor chrome
_BUBBLE_STEP = 0.06
# Wrapped rows must stay close enough for the production reply reader to
# recognize one message. Separate bubbles retain the larger gap above.
_LINE_STEP = 0.03
_BUBBLE_BOTTOM = 0.95

# Anchor extents are cosmetic (the matcher verifies centers, not
# widths), so one fixed half-width serves every label.
_ANCHOR_HALF_W = 0.06

# A valid 1×1 JPEG. A view reply's block 0 must be an image
# (`verdict.screen_text` drops block 0 only when it is action text), and
# post-handover the model may re-read the faked turn, so the block has
# to decode. Content is irrelevant — the conductor reads listings only.
TINY_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="
)


@dataclass(frozen=True)
class Bubble:
    sender: str  # USER | AGENT
    text: str


@dataclass
class Thread:
    """The whole scripted conversation: what has been said, and the
    user's replies still waiting for an ask to answer."""

    bubbles: list[Bubble] = field(default_factory=list)
    staged: list[str] = field(default_factory=list)


def thread_path() -> Path:
    return paths.debug_dir() / "thread.json"


def load() -> Thread:
    """The virtual thread, fail-open: missing or unreadable → empty (the
    render then shows a bare thread and the walk behaves accordingly)."""
    p = thread_path()
    if not p.exists():
        return Thread()
    try:
        data = json.loads(read_text(p))
        if data.get("schema") != THREAD_SCHEMA:
            log.warning("debug thread %s: unknown schema — ignoring", p)
            return Thread()
        return Thread(
            bubbles=[
                Bubble(
                    sender=AGENT if b.get("from") == AGENT else USER,
                    text=str(b.get("text") or ""),
                )
                for b in data.get("bubbles") or []
            ],
            staged=[str(s) for s in data.get("staged") or []],
        )
    except Exception:
        log.warning("debug thread %s unreadable — ignoring", p, exc_info=True)
        return Thread()


def save(thread: Thread) -> None:
    """Replace the script, waiting for any in-flight update to finish."""
    with _thread_lock():
        _write(thread)


def clear() -> None:
    """Remove the script under the writer lock; missing files raise as usual."""
    with _thread_lock():
        thread_path().unlink()


def _thread_lock() -> AbstractContextManager[None]:
    # Stable inode, shared by CLI and runtime processes. No phone or model work
    # runs under this lock; five seconds bounds a stalled filesystem update.
    return exclusive_file_lock(thread_path().with_suffix(".lock"), timeout=5)


def _write(thread: Thread) -> None:
    """Publish while the caller holds the lock across the entire update."""
    write_json_atomic(
        thread_path(),
        {
            "schema": THREAD_SCHEMA,
            "bubbles": [{"from": b.sender, "text": b.text} for b in thread.bubbles],
            "staged": list(thread.staged),
        },
    )


def seed(task: str, replies: list[str]) -> None:
    """Reset the conversation to one user message plus the staged
    replies — the `physiclaw debug --task --reply` script."""
    save(Thread(bubbles=[Bubble(sender=USER, text=task)], staged=list(replies)))


def stage(replies: list[str]) -> None:
    """Append staged replies to the running script (`--reply` without
    `--task`) — released by the same ask-then-peek rule."""
    with _thread_lock():
        thread = load()
        thread.staged.extend(replies)
        _write(thread)


def record_send(message: str) -> list[Bubble]:
    """The agent's own send, appended and persisted — the ask a gate
    quotes stays visible (and excludable) across a suspension exactly
    like a real sent message. Deliberately releases nothing: the send's
    result screen is the gate's baseline snapshot, and a reply released
    into it would be baselined away."""
    with _thread_lock():
        thread = load()
        thread.bubbles.append(Bubble(sender=AGENT, text=message))
        _write(thread)
        return thread.bubbles


def peek_bubbles() -> list[Bubble]:
    """The thread as a peek sees it — where reply timing lives. A staged
    reply is released iff the newest bubble is the agent's: an ask is
    the latest word, so the reply lands below it, fresh against the
    baseline, on this read. Covers the in-session poll, the suspended
    resume, and replies staged mid-run alike; with no ask outstanding,
    staged replies wait."""
    with _thread_lock():
        thread = load()
        if thread.staged and thread.bubbles and thread.bubbles[-1].sender == AGENT:
            released = thread.staged.pop(0)
            thread.bubbles.append(Bubble(sender=USER, text=released))
            _write(thread)
            log.info("debug thread: released staged reply %r", released)
        return thread.bubbles


# ---------- rendering ----------


def channel_view() -> tuple[PagePrint | None, Bbox]:
    """The channel pack's thread fingerprint (None: an anchorless
    render) and its IM's incoming box (the left default when the pack
    is unreadable) — one load; per-session callers cache the pair, the
    pack cannot change mid-session."""
    from physiclaw.conductor.load.pack import load_pack

    try:
        pack = load_pack(CHANNEL_APP)
    except Exception:
        log.warning("channel pack unreadable — rendering an anchorless thread")
        return None, INCOMING_LEFT
    pp = next((p for p in pack.prints if p.decl.name == THREAD_PAGE), None)
    return pp, incoming_box(paths.pack_root(CHANNEL_APP).name)


def _element(idx: int, label: str, cx: float, cy: float, half_w: float) -> Element:
    return Element(
        id=idx,
        kind="text",
        label=label,
        bbox=(
            max(cx - half_w, 0.0),
            max(cy - 0.015, 0.0),
            min(cx + half_w, 1.0),
            min(cy + 0.015, 1.0),
        ),
        conf=0.99,
    )


def _anchor_elements(pp: PagePrint | None) -> list[Element]:
    """One element per declared thread anchor: learned position first
    (the matcher verifies against it), else the region band's center,
    else spread across the top chrome."""
    if pp is None:
        return []
    out: list[Element] = []
    for i, a in enumerate(pp.decl.anchors):
        la = pp.learned.anchors.get(a.text) if pp.learned else None
        if la is not None:
            cx, cy = la.cx, la.cy
        elif a.within is not None:
            band = a.within
            cx, cy = (band[0] + band[2]) / 2, (band[1] + band[3]) / 2
        else:
            cx, cy = 0.5, 0.03 + i * 0.045
        out.append(_element(i, a.text, cx, cy, _ANCHOR_HALF_W))
    return out


def render_listing(bubbles: list[Bubble], pp: PagePrint | None, incoming: Bbox) -> str:
    """The virtual thread as one element listing: the channel pack's
    anchors, then the newest bubbles that fit, oldest first — a thread
    scrolled to its tail, the way every real peek reads it."""
    elements = _anchor_elements(pp)
    lines: list[tuple[int, str, str]] = [
        (bubble_id, b.sender, line.strip())
        for bubble_id, b in enumerate(bubbles)
        for line in b.text.splitlines()
        if line.strip()
    ]
    fit = int((_BUBBLE_BOTTOM - _BUBBLE_TOP) / _BUBBLE_STEP)
    y = _BUBBLE_TOP
    user_left = (incoming[0] + incoming[2]) / 2 < 0.5  # the user's side
    previous_bubble = None
    for bubble_id, sender, line in lines[-fit:]:
        if previous_bubble is not None:
            y += _LINE_STEP if bubble_id == previous_bubble else _BUBBLE_STEP
        cx, half_w = _line_geometry(line, from_left=(sender == USER) == user_left)
        elements.append(_element(len(elements), line, cx, y, half_w))
        previous_bubble = bubble_id
    return format_elements(elements)


def _line_geometry(line: str, *, from_left: bool) -> tuple[float, float]:
    """A line's center and half-width: it hangs from its bubble's edge
    and runs as far as its text needs, capped at a bubble's width."""
    half_w = min(_BUBBLE_MAX_W, len(line) * _CHAR_W) / 2
    cx = _BUBBLE_EDGE + half_w if from_left else 1.0 - _BUBBLE_EDGE - half_w
    return cx, half_w


# ---------- wire shapes ----------


def _image_block() -> dict:
    return {"type": "image", "data": TINY_JPEG_B64, "mimeType": "image/jpeg"}


def view_blocks(listing: str) -> list[dict]:
    """A faked view reply — `[image, listing]`, the peek shape."""
    return [_image_block(), {"type": "text", "text": listing}]


def gesture_blocks(action_text: str, listing: str) -> list[dict]:
    """A faked gesture reply — `[action text, image, listing]`, the
    run_macro shape. The caller composes `action_text` (verdict marker
    included, via `common.verdict.attach`)."""
    return [{"type": "text", "text": action_text}, *view_blocks(listing)]
