"""Context compaction — two jobs, both about keeping image payload small:

  1. `drop_stale_screens` — "latest screen wins": only the most recent
     peek/screenshot tool_result carries the image + full listing.
     Earlier view tool_results are stubbed down to a marker line
     (`(superseded <tool>)`) plus the listing's TEXT rows (icon-kind
     rows are dropped; without the image, numbered icon boxes are
     opaque anyway). Text rows are self-documenting — the label tells
     the agent what and where — and survive as re-targetable anchors.
     The assistant message and its tool_calls stay intact, so the
     decision history ("I called peek here, tap there") is preserved;
     the next turn's `note.summary` already sits in that assistant
     message, immediately after the stub, so no need to duplicate it
     in the stub header.

  2. `scale_image_bytes` — ingress re-encode: normalize every incoming
     tool-result image to JPEG with long edge ≤ MAX_IMAGE_EDGE. Drops PNG
     transparency (fine for screenshots), typically cuts payload 3–10×.

`drop_stale_screens` operates on `Message` DTOs: it finds
`AssistantMessage` turns whose tool_calls are all screen-obs, locates
the matching adjacent `ToolResultMessage`, replaces its `ImageBlock`-
bearing content with a stub string, AND sets `is_superseded=True` on
the new DTO. Providers find the latest stub via the typed flag — no
string parsing across modules.
"""
import json
import logging

import cv2
import numpy as np

from physiclaw.agent.engine import memory
from physiclaw.agent.engine.dto import (
    AssistantMessage,
    ContentBlock,
    ImageBlock,
    Message,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from physiclaw.config import CONFIG

log = logging.getLogger(__name__)

MAX_IMAGE_EDGE = CONFIG.compact.max_image_edge_px
JPEG_QUALITY = CONFIG.compact.jpeg_quality

# Tools that return a fresh view of the phone screen. An assistant turn
# whose tool_calls are ENTIRELY within SCREEN_OBS ∪ {"note"} is a
# "screen-observation turn" — collapsible under drop_stale_screens.
SCREEN_OBS_TOOLS = frozenset({"peek", "screenshot"})

# Human-readable lead-in for the stubbed content. Operators reading raw
# logs see `(superseded peek)` and immediately know what happened. Cache-
# marker code does NOT parse this — providers find stubs via the
# `is_superseded` flag on `ToolResultMessage`.
STUB_PREFIX = "(superseded "


# ---------- summary collapse (turn-age pruning) ----------

# Three pre-allocated slots, allocated at session bootstrap right after
# `[system, trigger]`, so `collapse_old_turns` always knows where to
# write — no "find existing slot" lookup, no shifting positions when
# the slots fill:
#
#   messages[2]  summary slot     `note(summary=...)` bullets
#   messages[3]  memory slot      `read_memory` / `read_logs` results
#   messages[4]  skills slot      `Skill(...)` results (bodies + refs)
#
# Memory and skill loads are durable artifacts the agent loaded
# specifically for persistent reference — summarizing them would
# defeat the load. They get carried through collapse as full
# tool_call args + result text. Multiple loads concat with `\n\n`
# (no dedup; if the agent reloaded the same skill, both copies
# survive — keeps the implementation predictable).
SUMMARY_HEADER = "[earlier turns]"
SUMMARY_INITIAL = f"{SUMMARY_HEADER}\n(none yet)"

MEMORY_HEADER = "[memory loads]"
MEMORY_INITIAL = f"{MEMORY_HEADER}\n(none yet)"

SKILLS_HEADER = "[loaded skills]"
SKILLS_INITIAL = f"{SKILLS_HEADER}\n(none yet)"

# Tool calls whose results are durable artifacts. Their tool_results
# would otherwise be dropped by the slice; the collapse harvests them
# into the dedicated slots so subsequent turns can still see what the
# agent loaded earlier.
MEMORY_TOOL_NAMES = frozenset({"read_memory", "read_logs"})
SKILL_TOOL_NAMES = frozenset({"Skill"})

# First message index after the three pre-allocated slots. The collapse
# range is `[_FIRST_TURN_INDEX:cut]`.
_FIRST_TURN_INDEX = 5

# Three knobs for collapse behavior, all on the `Provider` class:
#
#   F = COLLAPSE_FIRST_AT_TURN     first collapse fires at this turn
#   K = KEEP_RECENT_TURNS          recent turns kept intact per collapse
#   I = COLLAPSE_INTERVAL_TURNS    cadence between subsequent collapses
#
# Defaults (F=30, K=10, I=20) are an EOQ optimum for vendors with
# anchored caches (Anthropic/Qwen). Moonshot's whole-prefix
# invalidation pushes its optimum to I=30 — overridden on
# `MoonshotProvider`.


def new_summary_placeholder() -> UserMessage:
    """The pre-allocated summary slot. Engine bootstrap puts this at
    `messages[2]`; `collapse_old_turns` mutates it in place once enough
    turns accumulate. Empty-state body keeps the position stable from
    turn 0 — no "first collapse inserts a new message and shifts all
    indices" surprise."""
    return UserMessage(content=SUMMARY_INITIAL)


def new_memory_placeholder() -> UserMessage:
    """The pre-allocated memory-load slot at `messages[3]`. Pre-populated
    at bootstrap with the latest log entries as a synthetic `read_logs`
    artifact so recent activity is in context from turn 0; falls back to
    `(none yet)` when no log files exist."""
    log_text = memory.load_recent_entries(memory.BOOTSTRAP_LOG_ENTRIES)
    if not log_text:
        return UserMessage(content=MEMORY_INITIAL)
    entry = _format_artifact_text(
        "read_logs", {"entries": memory.BOOTSTRAP_LOG_ENTRIES}, log_text,
    )
    return UserMessage(content=_render_slot(MEMORY_HEADER, [entry], sep="\n\n"))


def new_skills_placeholder() -> UserMessage:
    """The pre-allocated skills-load slot at `messages[4]`. Holds the
    full body of any `Skill(...)` result that would otherwise be
    dropped by collapse. The skill body is the workflow doctrine the
    agent is following; a one-line summary cannot replace it."""
    return UserMessage(content=SKILLS_INITIAL)


def collapse_old_turns(
    messages: list[Message],
    *,
    first_at: int,
    interval: int,
    keep: int,
) -> None:
    """Fold turns older than `keep` into three slots:
      - `messages[2]` — `note(summary=...)` bullets
      - `messages[3]` — `read_memory` / `read_logs` results in full
      - `messages[4]` — `Skill(...)` bodies + references in full

    Cuts at turn boundaries (an `AssistantMessage` starts a turn) so
    every collapsed `tool_use` block has its matching `tool_result`
    collapsed too — no API-rejecting orphans.

    Trigger:
      - First collapse: when complete-turn count reaches `first_at`.
      - Subsequent: when count reaches `keep + interval`.

    All three knobs come from the active provider's class attributes
    (`COLLAPSE_FIRST_AT_TURN`, `KEEP_RECENT_TURNS`, `COLLAPSE_INTERVAL_TURNS`).
    The engine threads them through; this function stays vendor-
    agnostic.

    "First vs subsequent" is detected by the summary slot's content —
    the placeholder body persists until the first collapse rewrites it.

    Source material:
      - summary: each turn's `note(summary=...)` — string concat,
        flattened to one line per bullet, no model call.
      - memory / skills: paired (tool_use, tool_result) carried
        verbatim. These are durable artifacts the agent loaded
        specifically for persistent reference; a summary cannot
        replace them. Multiple loads concat with `\\n\\n`. No dedup —
        if the agent reloaded the same skill, both copies survive.

    Each collapse mutates the prefix bytes between system and the next
    stub anchor → triggers one cache_creation event. The vendor's
    defaults amortize this tax against the bounded-prompt savings
    (EOQ analysis in this file's module-level comment).
    """
    if (
        len(messages) < _FIRST_TURN_INDEX
        or not isinstance(messages[2], UserMessage)
        or not isinstance(messages[3], UserMessage)
        or not isinstance(messages[4], UserMessage)
    ):
        log.warning("collapse_old_turns: missing summary/memory/skill slots")
        return

    turn_starts = [
        i for i, m in enumerate(messages)
        if isinstance(m, AssistantMessage)
    ]
    is_first_collapse = (messages[2].content == SUMMARY_INITIAL)
    threshold = first_at if is_first_collapse else keep + interval
    if len(turn_starts) < threshold:
        return

    cut = turn_starts[-keep]

    # Carry forward existing slot bodies so running history stays
    # continuous across multiple collapse events.
    summary_lines = _carry_items(messages[2].content, SUMMARY_HEADER, sep="\n")
    memory_entries = _carry_items(messages[3].content, MEMORY_HEADER, sep="\n\n")
    skill_entries = _carry_items(messages[4].content, SKILLS_HEADER, sep="\n\n")

    # Pair tool_call ids with their results within the collapsed range.
    # The cut is at a turn boundary, so every AssistantMessage in
    # [..cut] has its ToolResultMessage in [..cut].
    result_by_id: dict[str, ToolResultMessage] = {
        m.tool_call_id: m
        for m in messages[_FIRST_TURN_INDEX:cut]
        if isinstance(m, ToolResultMessage)
    }

    for i in range(_FIRST_TURN_INDEX, cut):
        m = messages[i]
        if not isinstance(m, AssistantMessage):
            continue
        for tc in m.tool_calls:
            if tc.name == "note":
                s = (tc.arguments.get("summary") or "").replace("\n", " ").strip()
                if s:
                    summary_lines.append(f"- {s}")
            elif tc.name in MEMORY_TOOL_NAMES:
                entry = _format_artifact(tc, result_by_id.get(tc.id))
                if entry:
                    memory_entries.append(entry)
            elif tc.name in SKILL_TOOL_NAMES:
                entry = _format_artifact(tc, result_by_id.get(tc.id))
                if entry:
                    skill_entries.append(entry)

    if not (summary_lines or memory_entries or skill_entries):
        return  # nothing salvageable; leave bytes in place

    messages[2:cut] = [
        UserMessage(content=_render_slot(SUMMARY_HEADER, summary_lines, sep="\n")),
        UserMessage(content=_render_slot(MEMORY_HEADER, memory_entries, sep="\n\n")),
        UserMessage(content=_render_slot(SKILLS_HEADER, skill_entries, sep="\n\n")),
    ]


def _carry_items(existing: str | list, header: str, *, sep: str) -> list[str]:
    """Pull `sep`-separated items out of an existing slot body. Used
    to round-trip a slot's content across consecutive collapses so
    the running history stays continuous."""
    if not isinstance(existing, str) or not existing.startswith(header):
        return []
    body = existing.removeprefix(header).lstrip()
    if not body or body == "(none yet)":
        return []
    return [item for item in body.split(sep) if item]


def _render_slot(header: str, items: list[str], *, sep: str) -> str:
    body = sep.join(items) if items else "(none yet)"
    return f"{header}\n{body}"


def _format_artifact(tc: ToolCall, result: ToolResultMessage | None) -> str:
    """Format one (tool_call, tool_result) pair as a slot entry. Skips
    error results and non-string content (image-bearing tool_results
    aren't memory/skill loads anyway)."""
    if result is None or result.is_error:
        return ""
    if not isinstance(result.content, str):
        return ""
    return _format_artifact_text(tc.name, tc.arguments, result.content)


def _format_artifact_text(name: str, arguments: dict, content: str) -> str:
    """Render the slot-entry text for a (tool_name, args, result) triple.
    Shared by real artifact harvesting and synthetic bootstrap entries
    so both surfaces produce identical formatting."""
    args_str = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    return f"{name}({args_str}) →\n{content}"


def scale_image_bytes(raw: bytes) -> tuple[bytes, str]:
    """Decode, scale long-edge to MAX_IMAGE_EDGE if larger, re-encode JPEG.

    Returns (bytes, mime_type). On decode/encode failure returns the input
    unchanged with a generic mime so the caller still has something to
    forward — context bloat is preferable to a dropped screenshot.
    """
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        log.warning("scale_image_bytes: decode failed on %d bytes", len(raw))
        return raw, "application/octet-stream"
    h, w = img.shape[:2]
    long_edge = max(h, w)
    if long_edge > MAX_IMAGE_EDGE:
        scale = MAX_IMAGE_EDGE / long_edge
        new_size = (int(round(w * scale)), int(round(h * scale)))
        img = cv2.resize(img, new_size, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        log.warning("scale_image_bytes: encode failed")
        return raw, "application/octet-stream"
    return buf.tobytes(), "image/jpeg"


def _is_screen_obs_turn(asst: AssistantMessage) -> bool:
    """True iff every tool_call in `asst` is in SCREEN_OBS ∪ {"note"} AND
    at least one is an actual screen observation. A turn with a
    non-screen tool (tap, read_logs, update_progress, …) is a boundary
    and stays put."""
    names = asst.tool_names()
    if not names:
        return False
    allowed = SCREEN_OBS_TOOLS | {"note"}
    if not all(n in allowed for n in names):
        return False
    return any(n in SCREEN_OBS_TOOLS for n in names)


def _content_to_text(content: str | list[ContentBlock]) -> str:
    """Pull the first text out of a tool_result content — a plain string
    or the first `TextBlock` in a multipart list."""
    if isinstance(content, str):
        return content
    for block in content:
        if isinstance(block, TextBlock):
            return block.text
    return ""


def _has_image(content: str | list[ContentBlock]) -> bool:
    """True iff `content` carries at least one `ImageBlock` — i.e. the
    content is multipart and includes a fresh screen capture. Stubbed
    content (plain string) returns False so re-passes are no-ops."""
    if isinstance(content, str):
        return False
    return any(isinstance(b, ImageBlock) for b in content)


def _filter_text_rows(listing: str) -> str:
    """Keep only `[text]`-kind rows from an element listing. Icon rows
    are opaque once the image is gone; text rows self-document via their
    label. Returns header + kept rows, or empty string if nothing was
    kept (so the caller can omit the block entirely instead of
    embedding a header with no content).

    Format coupling: the row shape `id [kind] "label" [bbox] conf` is
    produced by `physiclaw.core.vision.util.format_elements`. If that
    formatter changes, update the parse below to match.
    """
    lines = listing.splitlines()
    if not lines:
        return ""
    header, rest = lines[0], lines[1:]
    kept: list[str] = []
    for line in rest:
        open_ = line.find("[")
        close = line.find("]", open_ + 1)
        if open_ < 0 or close < 0:
            continue
        if line[open_ + 1:close] == "text":
            kept.append(line)
    if not kept:
        return ""
    return "\n".join([header] + kept)


def drop_stale_screens(messages: list[Message]) -> None:
    """Stub earlier view tool_results; keep asst + tool_call history intact.

    For each screen-observation turn X before the latest:
      - Keep the `AssistantMessage` (tool_calls list untouched).
      - Keep the `note` `ToolResultMessage` (small text, harmless).
      - Replace the view tool's `ToolResultMessage.content` with a
        marker line (`"(superseded <tool>)"`) followed by the text-kind
        rows from the original listing. Icon rows and the image are
        dropped: without the image, numbered icon boxes are opaque;
        text rows self-document via their label. No summary is embedded
        in the header — the next turn's `note.summary` is already in
        that turn's assistant message immediately after this stub.

    The latest screen-observation's pair is untouched — its pixels and
    full listing are still the agent's live view.

    Runs in place. Idempotency is carried by the `_has_image` guard:
    fresh view results carry an `ImageBlock`; once stubbed, content is
    a plain string (and `is_superseded=True`) so further passes skip it.
    """
    obs_indices = [
        i for i, m in enumerate(messages)
        if isinstance(m, AssistantMessage) and _is_screen_obs_turn(m)
    ]
    if len(obs_indices) <= 1:
        return

    for i in obs_indices[:-1]:
        asst = messages[i]
        assert isinstance(asst, AssistantMessage)  # guaranteed by obs_indices filter

        name_by_id = {tc.id: tc.name for tc in asst.tool_calls if tc.id}

        # Scan contiguous tool_results for the single view one. The
        # [note, one-other] rule guarantees at most one view per turn,
        # so first match wins — break on find.
        view_tr_idx = -1
        view_tool_name = ""
        j = i + 1
        while j < len(messages) and isinstance(messages[j], ToolResultMessage):
            tr = messages[j]
            assert isinstance(tr, ToolResultMessage)
            name = name_by_id.get(tr.tool_call_id, "")
            if name in SCREEN_OBS_TOOLS:
                view_tr_idx = j
                view_tool_name = name
                break
            j += 1

        if view_tr_idx < 0:
            continue

        view_tr = messages[view_tr_idx]
        assert isinstance(view_tr, ToolResultMessage)
        if not _has_image(view_tr.content):
            # Already stubbed (or never had an image — synthetic error result).
            continue

        listing = _content_to_text(view_tr.content)
        text_rows = _filter_text_rows(listing)

        head = f"{STUB_PREFIX}{view_tool_name})"
        new_content = f"{head}\n{text_rows}" if text_rows else head
        messages[view_tr_idx] = ToolResultMessage(
            tool_call_id=view_tr.tool_call_id,
            content=new_content,
            is_error=view_tr.is_error,
            is_superseded=True,
        )
