"""Gate reply reading — the deterministic reading of the ask's reply.

Ruled order: (0) there must be a NEW incoming message at all — no new
bubble, no check; (1) the NEWEST message decides, by exact word match
on the WHOLE message, normalized, against the words the ASK ITSELF
declares (`yes:` / `no:`) — the conductor holds no word list of its
own. Whole-message equality is the discipline: "ok, but make it two
boxes" contains "ok" yet carries a qualifier that changes the order —
anything the declared words do not cover is the model's to read off
the thread (the walk hands over).

The screen is a thread with the keyboard often up (a send leaves it
raised): the key rows and the predictive bar above them are never
messages, nor is the status bar above the thread (its clock ticks) or
a lone letter (an avatar doodle OCRs as one) — recognized by shape
and skipped.

New-message detection is positional first, set-difference second. We
know what we sent: the ask's text is kept, and `ask_band` finds that
bubble on the thread by its words. A bubble BELOW our visible ask is
newer than the ask by construction and counts whatever it says — a
reply that repeats a word already on screen must not vanish. Which
side a row hangs from (`side_of`) is read from its center against the
IM's incoming box (`incoming_box`), and only for a row narrow enough
to show a side. Only when position cannot tell — the ask scrolled off,
or a sweep above it — do the side and the baseline (the label set
snapshotted when our ask landed) decide what is new.

OCR is not exact: a row reads as ours by a run of consecutive
characters found in the ask, the ask's end by its last few characters
with one allowed misread, and a wrapped tail under an ask whose end
was not read is claimed as ours whatever it reads as.
"""

import unicodedata
from collections.abc import Set as AbstractSet
from enum import StrEnum

from physiclaw.common.bbox import Bbox, center_of, inside
from physiclaw.common.listing import Element
from physiclaw.common.text import fold

# Which side of the thread the user's bubbles hang from, by IM app
# (the channel's folder, which its manifest's `app:` names): the box a
# narrow row's center falls in when it is theirs — ours, and the
# thread's centered time stamps, hang from the other side. Every IM we
# know puts theirs on the left, so the table holds only the exceptions
# (a right-to-left IM), and an IM it does not list reads as left.
INCOMING_LEFT: Bbox = (0.0, 0.0, 0.45, 1.0)
_INCOMING_BY_APP: dict[str, Bbox] = {}


def incoming_box(im: str) -> Bbox:
    """The user's side of the thread in IM `im` — left unless listed."""
    return _INCOMING_BY_APP.get(im, INCOMING_LEFT)


# A row narrower than this shows its side: a bubble line hangs from its
# side's edge, so a narrow one has its center well within that side's
# half — a wider line fills its bubble and reaches the same edges on
# either side, and its center says nothing.
_SIDE_LEGIBLE_W = 0.5

# The thread begins below the status bar. The clock there ticks every
# minute, so at a send's landing it is a row the baseline never held —
# read as a reply, it matches no declared word. Rows above this line
# are the phone's, never the thread's (the vision layer drops the same
# band: `core.vision.change.STATUS_BAR_FRAC`).
_STATUS_BAR_MAX_Y = 0.06

# A row is read as a piece of our own ask when this many consecutive
# characters of it occur in the ask: a shorter fragment could be
# anything, and OCR misreading a character spoils only the windows
# that cover it, so a line of any real length still reads as ours.
_OWN_FRAGMENT_MIN = 5
# The ask's last words are recognized on a row by its last characters,
# this many of them, one of which OCR may have misread.
_TAIL_CHARS = 3
# Rows of one bubble sit a line apart; bubbles sit further apart. Rows
# closer than this are one message — and the ask's band, anchored on
# lines that read as its text, extends over rows this close.
_LINE_GAP = 0.04

# A raised keyboard: a ROW of single-letter keys (this many at one
# height), and everything from this far above the topmost key row down
# — the predictive bar one line above the keys, the input bar above
# that — is keyboard, not thread. A lone letter (the bar's "I") is not
# a row, so it never sets the floor.
_KEY_ROW_MIN = 5
_KEY_ROW_SPREAD = 0.015
_KEYBOARD_GAP = 0.12

# Stripped from both ends: punctuation, symbols, and a dangling
# combining mark (what a stray ¨ leaves behind once its space is gone).
EDGE_CATEGORIES = ("P", "S", "M")


def normalize(text: str) -> str:
    """The comparison space for whole-message matching: NFKC (folds
    full-width forms), casefold, ALL whitespace removed, punctuation and
    symbols stripped from both ends (。！!?～ and friends — a trailing
    exclamation mark must not defeat "ok"). Idempotent: a word stored
    normalized at parse reads back equal to itself."""
    t = fold(text)
    start, end = 0, len(t)
    while start < end and unicodedata.category(t[start])[0] in EDGE_CATEGORIES:
        start += 1
    while end > start and unicodedata.category(t[end - 1])[0] in EDGE_CATEGORIES:
        end -= 1
    return t[start:end]


class Answer(StrEnum):
    """What a reply reads as against the ask's own words."""

    CONFIRM = "confirm"
    DENY = "deny"


def classify(text: str, yes: AbstractSet[str], no: AbstractSet[str]) -> Answer | None:
    """The verdict for ONE message: confirm, deny, or None (the
    declared words do not cover it). Whole-message equality only —
    `yes`/`no` are the ask's words already in `normalize` space (the
    parser normalizes them once)."""
    norm = normalize(text)
    if not norm:
        return None
    if norm in no:
        return Answer.DENY
    if norm in yes:
        return Answer.CONFIRM
    return None


def classify_all(
    messages: list[str], yes: AbstractSet[str], no: AbstractSet[str]
) -> Answer | None:
    """The verdict of one check round: the NEWEST message (the last in
    screen order) is the user's answer, whatever came before it — a
    changed mind is the newer bubble. An undeclared newest message
    defers to the model."""
    if not messages:
        return None
    return classify(messages[-1], yes, no)


def any_deny(messages: list[str], yes: AbstractSet[str], no: AbstractSet[str]) -> bool:
    """Whether a deny is anywhere in `messages` — the sweep's question
    at a later send: a "no" said while the walk was in the app must stop
    it whatever followed."""
    return any(classify(m, yes, no) is Answer.DENY for m in messages)


def keyboard_top(rows: tuple[Element, ...]) -> float | None:
    """Where the raised keyboard begins — the y above which the thread
    ends — or None when no keyboard shows. Recognized by its shape: a
    row of single-letter keys at one height."""
    keys = sorted(
        c[1]
        for row in rows
        if _is_letter(row.label) and (c := center_of(row.bbox)) is not None
    )
    for y in keys:
        if sum(1 for k in keys if abs(k - y) <= _KEY_ROW_SPREAD) >= _KEY_ROW_MIN:
            return y - _KEYBOARD_GAP
    return None


def _is_letter(label: str) -> bool:
    """A lone ASCII letter: a keyboard key, or an avatar doodle OCR'd —
    never a message."""
    label = label.strip()
    return len(label) == 1 and label.isascii() and label.isalpha()


class Side(StrEnum):
    """Which side of the thread a row hangs from."""

    THEIRS = "theirs"
    OURS = "ours"


def side_of(row: Element, incoming: Bbox) -> Side | None:
    """The side a row hangs from — its center in the IM's incoming box
    or not — or None when the row is too wide to show one
    (`_SIDE_LEGIBLE_W`)."""
    left, _, right, _ = row.bbox
    if right - left >= _SIDE_LEGIBLE_W:
        return None
    c = center_of(row.bbox)
    if c is None:
        return None
    return Side.THEIRS if inside(c, incoming, margin=0.0) else Side.OURS


def ask_band(
    rows: tuple[Element, ...], own_text: str, *, incoming: Bbox
) -> tuple[float, float] | None:
    """Where OUR just-sent message sits on the thread — the y of its
    first and last line — or None when we cannot find it.

    Anchored on rows that read as pieces of the ask and are not known
    to be the user's; anchors a line apart are one bubble, and of
    several such runs the ask is the LOWEST — the same words sent
    earlier (a re-ask, an earlier run of the same errand) sit above the
    latest send. (A user's wide bubble quoting a whole line of the ask
    passes for one: a limit accepted.) Grown UPWARD over rows of our
    own text, however short: a bubble's short leading lines sit inside
    it and can OCR left of center. Grown DOWNWARD by one row only when
    the ask's last words have not been read yet — a wrapped tail OCRs
    left of center and may OCR wrong, and what ends the ask is known
    from its text, not from the row; once the ask is complete nothing
    below joins, since a reply typed right under it may repeat one of
    the ask's own no words.

    None is a real answer, not a failure: the thread may have scrolled
    past it. What a caller may conclude from that is the caller's rule —
    `read_incoming` falls back to the baseline, and the ask step refuses
    to read consent it cannot place."""
    own = normalize(own_text)
    if not own:
        return None
    lines = sorted(
        (c[1], normalize(row.label), side_of(row, incoming))
        for row in rows
        if row.label.strip() and (c := center_of(row.bbox)) is not None
    )
    anchors = [
        y for y, label, side in lines if side is not Side.THEIRS and _is_own(label, own)
    ]
    if not anchors:
        return None
    bottom = top = anchors[-1]
    for y, label, _ in reversed(lines):
        if y >= top:
            continue
        if top - y > _LINE_GAP:
            break
        if label and (label in own or _is_own(label, own)):
            top = y  # a piece of the ask, however short, or an anchor's worth
    complete = any(_ends_ask(label, own) for y, label, _ in lines if top <= y <= bottom)
    if not complete:
        tail = next(((y, label) for y, label, _ in lines if y > bottom), None)
        if tail is not None and tail[0] - bottom <= _LINE_GAP:
            bottom = tail[0]
    return (top, bottom)


def _is_own(label: str, own: str) -> bool:
    """Whether a row (normalized) reads as a piece of our own text: the
    whole ask in the row, or `_OWN_FRAGMENT_MIN` consecutive characters
    of the row found in the ask."""
    if own in label:
        return True
    return any(
        label[i : i + _OWN_FRAGMENT_MIN] in own
        for i in range(len(label) - _OWN_FRAGMENT_MIN + 1)
    )


def _ends_ask(label: str, own: str) -> bool:
    """Whether a row (normalized) carries the ask's last words: the
    whole ask, or the ask's last `_TAIL_CHARS` characters at its end
    with at most one misread."""
    if not label:
        return False
    if own in label or own.endswith(label):
        return True
    if len(label) < _TAIL_CHARS or len(own) < _TAIL_CHARS:
        return False
    tail, want = label[-_TAIL_CHARS:], own[-_TAIL_CHARS:]
    return sum(a != b for a, b in zip(tail, want, strict=True)) <= 1


def read_incoming(
    rows: tuple[Element, ...],
    baseline: AbstractSet[str],
    own_text: str,
    *,
    after_ask: bool = True,
    incoming: Bbox,
) -> tuple[list[str], bool]:
    """The user's new bubbles since the baseline snapshot, in screen
    order, and whether our own ask was PLACED on the thread — the
    positional rule below stood on it — or the baseline had to decide.

    `after_ask` (the default) reads THIS ask's reply: when the ask
    bubble is visible, rows above it are older than the ask (a keyboard
    hides bubbles without disturbing the page's anchors — when it
    dismisses, pre-ask history resurfaces) and rows below it are newer
    by construction, whatever their label — the walk sends nothing
    between an ask and its read, so what is under the ask is the
    user's unless it shows our side (`side_of`). Only when the ask has
    scrolled off the top does the baseline decide what is new, among
    the rows that show the user's side.

    `after_ask=False` is the deny sweep at a later send's landing: it
    reads bubbles ABOVE the just-sent ask, skipping the ask's own band,
    and the baseline (the previous send's snapshot) decides — among the
    rows that show the user's side, as above."""
    floor = keyboard_top(rows)
    if floor is not None:
        rows = tuple(
            r for r in rows if (c := center_of(r.bbox)) is not None and c[1] < floor
        )
    band = ask_band(rows, own_text, incoming=incoming)
    out: list[tuple[float, str]] = []
    for row in rows:
        label = row.label.strip()
        if not label or _is_letter(label):
            continue
        c = center_of(row.bbox)
        if c is None or c[1] < _STATUS_BAR_MAX_Y:
            continue  # the status bar: the phone's rows, never the thread's
        if band is not None and after_ask:
            if c[1] > band[1] and side_of(row, incoming) is not Side.OURS:
                out.append((c[1], label))  # below our ask: newer by construction
            continue
        if band is not None and band[0] <= c[1] <= band[1]:
            continue  # the sweep: the just-sent ask's own band
        # No position to stand on: only a row that shows the user's side
        # is a candidate — a wide row could be a line of our own bubble
        # that OCR read differently than the baseline holds, and read as
        # the user's it would re-plan the errand from our own words.
        if side_of(row, incoming) is Side.THEIRS and label not in baseline:
            out.append((c[1], label))
    # Screen order top to bottom is thread order oldest to newest — the
    # last one is the answer. A bubble's rows are one message: the
    # whole-message rule must see "two bags then, ok" whole, not its last line.
    return _bubbles(sorted(out, key=lambda x: x[0])), band is not None


def _bubbles(rows: list[tuple[float, str]]) -> list[str]:
    out: list[str] = []
    last_y: float | None = None
    for y, label in rows:
        if last_y is not None and y - last_y < _LINE_GAP:
            out[-1] = f"{out[-1]} {label}"
        else:
            out.append(label)
        last_y = y
    return out
