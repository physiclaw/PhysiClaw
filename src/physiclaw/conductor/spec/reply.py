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
messages, and a wrapped last line of our own ask can OCR left of
center — both are recognized by shape and skipped.

New-message detection is positional first, set-difference second:
incoming bubbles sit left of center in every listing-shaped IM layout
(ours sit right); a bubble BELOW our visible ask is newer than the ask
by construction and counts whatever it says (a reply that repeats a
word already on screen must not vanish); only when position cannot
tell — the ask scrolled off, or a sweep above it — does the baseline
(the label set snapshotted when our ask landed) decide what is new.
"""

import unicodedata
from collections.abc import Set as AbstractSet
from enum import StrEnum

from physiclaw.common.bbox import center_of
from physiclaw.common.listing import Element
from physiclaw.common.text import fold

# Incoming bubbles' centers sit left of this; our own sit right, and
# centered system rows (timestamps, at ~0.5) fall OUTSIDE it — they
# would otherwise read as a reply after a suspension.
INCOMING_MAX_CX = 0.45

# A right-side row is read as a wrapped LINE of our own ask only above
# this length: a short fragment could be anything.
_OWN_FRAGMENT_MIN = 5
# The ask's own band extends one line below its last recognized line:
# a wrapped tail can OCR left of center. The deny sweep skips the band
# whole; the reply read skips a row there that ends the ask's text —
# however short, since an ask's last word may be one of its own no
# words ("…或 不用 取消。").
_WRAP_GAP = 0.04

# A raised keyboard: a ROW of single-letter keys (this many at one
# height), and everything from this far above the topmost key row down
# — the predictive bar one line above the keys, the input bar above
# that — is keyboard, not thread. A lone letter (the bar's "I") is not
# a row, so it never sets the floor.
_KEY_ROW_MIN = 5
_KEY_ROW_SPREAD = 0.015
_KEYBOARD_GAP = 0.12

# Rows of one bubble sit a line apart; bubbles sit further apart. Rows
# closer than this are one message.
_LINE_GAP = 0.035


# Stripped from both ends: punctuation, symbols, and a dangling
# combining mark (what a stray ¨ leaves behind once its space is gone).
EDGE_CATEGORIES = ("P", "S", "M")


def normalize(text: str) -> str:
    """The comparison space for whole-message matching: NFKC (folds
    full-width forms), casefold, ALL whitespace removed, punctuation and
    symbols stripped from both ends (。！!?～ and friends — a trailing
    exclamation mark must not defeat 好的). Idempotent: a word stored
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
    at a later send: a 不要 said while the walk was in the app must stop
    it whatever followed."""
    return any(classify(m, yes, no) is Answer.DENY for m in messages)


def keyboard_top(rows: tuple[Element, ...]) -> float | None:
    """Where the raised keyboard begins — the y above which the thread
    ends — or None when no keyboard shows. Recognized by its shape: a
    row of single-letter keys at one height."""
    keys = sorted(
        c[1]
        for row in rows
        if _is_key(row.label) and (c := center_of(row.bbox)) is not None
    )
    for y in keys:
        if sum(1 for k in keys if abs(k - y) <= _KEY_ROW_SPREAD) >= _KEY_ROW_MIN:
            return y - _KEYBOARD_GAP
    return None


def _is_key(label: str) -> bool:
    label = label.strip()
    return len(label) == 1 and label.isascii() and label.isalpha()


def new_incoming(
    rows: tuple[Element, ...],
    baseline: AbstractSet[str],
    own_text: str,
    *,
    after_ask: bool = True,
) -> list[str]:
    """The user's new bubbles since the baseline snapshot, in screen
    order. Incoming = left of center (our own lines sit right, so they
    never enter the candidate set).

    `after_ask` (the default) reads THIS ask's reply: when the ask
    bubble is visible, rows above it are older than the ask (a keyboard
    hides bubbles without disturbing the page's anchors — when it
    dismisses, pre-ask history resurfaces) and rows below it are newer
    by construction, whatever their label — a reply repeating a word
    already on screen is still a reply. Only when the ask has scrolled
    off the top does the baseline decide what is new.

    `after_ask=False` is the deny sweep at a later send's landing: it
    reads bubbles ABOVE the just-sent ask, skipping the ask's own band,
    and the baseline (the previous send's snapshot) decides."""
    floor = keyboard_top(rows)
    if floor is not None:
        rows = tuple(
            r for r in rows if (c := center_of(r.bbox)) is not None and c[1] < floor
        )
    own = normalize(own_text)
    ask_top: float | None = None
    ask_bottom: float | None = None
    if own:
        for row in rows:
            label = normalize(row.label)
            if not label:
                continue
            c = center_of(row.bbox)
            if c is None or c[0] <= INCOMING_MAX_CX:
                continue  # ask lines render as OUR bubbles, right of center
            if own in label or (len(label) >= _OWN_FRAGMENT_MIN and label in own):
                ask_top = c[1] if ask_top is None else min(ask_top, c[1])
                ask_bottom = c[1] if ask_bottom is None else max(ask_bottom, c[1])
    out: list[tuple[float, str]] = []
    for row in rows:
        label = row.label.strip()
        if not label:
            continue
        c = center_of(row.bbox)
        if c is None or c[0] > INCOMING_MAX_CX:
            continue
        if ask_top is not None and ask_bottom is not None:
            if after_ask:
                if c[1] <= ask_bottom:
                    continue  # older than the ask — never its reply
                if c[1] <= ask_bottom + _WRAP_GAP and _is_tail(label, own):
                    continue  # the ask's own wrapped last line
                out.append((c[1], label))  # below the ask: newer by construction
                continue
            if ask_top <= c[1] <= ask_bottom + _WRAP_GAP:
                continue  # the just-sent ask's own band (tail included)
        if label in baseline:
            continue
        out.append((c[1], label))
    # Screen order top to bottom is thread order oldest to newest — the
    # last one is the answer. A bubble's rows are one message: the
    # whole-message rule must see "买两袋，好的" whole, not its last line.
    return _bubbles(sorted(out, key=lambda x: x[0]))


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


def _is_tail(label: str, own: str) -> bool:
    norm = normalize(label)
    return bool(norm) and own.endswith(norm)
