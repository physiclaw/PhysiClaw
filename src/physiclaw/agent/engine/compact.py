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
import logging

import cv2
import numpy as np

from physiclaw.agent.engine.dto import (
    AssistantMessage,
    ContentBlock,
    ImageBlock,
    Message,
    TextBlock,
    ToolResultMessage,
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
