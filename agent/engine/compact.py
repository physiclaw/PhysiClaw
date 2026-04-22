"""Context compaction — two jobs, both about keeping image payload small:

  1. `drop_stale_screens` — "latest screen wins": only the most recent
     peek/screenshot tool_result carries the image + full listing.
     Earlier view tool_results are stubbed down to a header line plus
     the listing's TEXT rows (icon-kind rows are dropped; without the
     image, numbered icon boxes are opaque anyway). Text rows are
     self-documenting — the label tells the agent what and where —
     and survive as re-targetable anchors across compaction.
     The assistant message and its tool_calls stay intact, so the
     decision history ("I called peek here, tap there") is preserved.

  2. `scale_image_bytes` — ingress re-encode: normalize every incoming
     tool-result image to JPEG with long edge ≤ MAX_IMAGE_EDGE. Drops PNG
     transparency (fine for screenshots), typically cuts payload 3–10×.
"""
import json
import logging
from typing import Any

import cv2
import numpy as np

log = logging.getLogger(__name__)

MAX_IMAGE_EDGE = 1566
JPEG_QUALITY = 85

# Tools that return a fresh view of the phone screen. An assistant turn
# whose tool_calls are ENTIRELY within SCREEN_OBS ∪ {"note"} is a
# "screen-observation turn" — collapsible under drop_stale_screens.
SCREEN_OBS_TOOLS = frozenset({"peek", "screenshot"})


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


def _tool_names(asst: dict[str, Any]) -> list[str]:
    """Pull tool names out of an assistant-role wire message. Non-assistant
    or tool_call-free messages return []."""
    if asst.get("role") != "assistant":
        return []
    calls = asst.get("tool_calls") or []
    out: list[str] = []
    for tc in calls:
        fn = tc.get("function") or {}
        name = fn.get("name")
        if isinstance(name, str):
            out.append(name)
    return out


def _is_screen_obs_turn(asst: dict[str, Any]) -> bool:
    """True iff `asst` is a screen-observation turn — its tool_calls are
    entirely within SCREEN_OBS ∪ {"note"}, AND at least one tool_call is
    an actual screen observation. A turn with a non-screen tool (tap,
    read_logs, update_plan, …) is a boundary and stays put."""
    names = _tool_names(asst)
    if not names:
        return False
    allowed = SCREEN_OBS_TOOLS | {"note"}
    if not all(n in allowed for n in names):
        return False
    return any(n in SCREEN_OBS_TOOLS for n in names)


def _note_screen(asst: dict[str, Any]) -> str:
    """Pull `note.screen` off an assistant message. Empty string if
    absent or malformed. Used to label stubbed views."""
    for tc in asst.get("tool_calls") or []:
        fn = tc.get("function") or {}
        if fn.get("name") != "note":
            continue
        raw = fn.get("arguments")
        if not isinstance(raw, str):
            return ""
        try:
            args = json.loads(raw) or {}
        except json.JSONDecodeError:
            return ""
        s = args.get("screen")
        return s.strip() if isinstance(s, str) else ""
    return ""


def _extract_text(content: Any) -> str:
    """Pull the text payload from a tool_result content — either a plain
    string or a multipart `[{type:text,text:...}, {type:image_url,...}]`."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return part.get("text") or ""
    return ""


def _filter_text_rows(listing: str) -> str:
    """Keep only `[text]`-kind rows from an element listing. Icon rows
    are opaque once the image is gone; text rows self-document via their
    label. Returns header + kept rows, or empty string if nothing was
    kept (so the caller can omit the block entirely instead of
    embedding a header with no content).

    Format coupling: the row shape `id [kind] "label" [bbox] conf` is
    produced by `physiclaw.vision.util.format_elements`. If that
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


def drop_stale_screens(messages: list[dict[str, Any]]) -> None:
    """Stub earlier view tool_results; keep asst + tool_call history intact.

    For each screen-observation turn X before the latest:
      - Keep the assistant message (tool_calls list untouched).
      - Keep the `note` tool_result (small text, harmless).
      - Replace the view tool's tool_result content with a header line
        (`"(superseded <tool> — past view: <desc>)"`, where `<desc>`
        comes from the NEXT turn's `note.screen`) followed by the
        text-kind rows from the original listing. Icon rows and the
        image are dropped: without the image, numbered icon boxes are
        opaque; text rows self-document via their label.

    The latest screen-observation's pair is untouched — its pixels and
    full listing are still the agent's live view.

    Runs in place. Idempotency is carried by the `isinstance(content,
    str)` guard below: fresh view results arrive as multipart lists
    (text + image_url); once stubbed, content is a plain string and
    further passes skip it. That guard — not the filter — is what
    makes the pass safe to repeat.
    """
    obs_indices = [i for i, m in enumerate(messages) if _is_screen_obs_turn(m)]
    if len(obs_indices) <= 1:
        return

    for i in obs_indices[:-1]:
        asst = messages[i]
        name_by_id = {
            tc["id"]: ((tc.get("function") or {}).get("name", ""))
            for tc in asst.get("tool_calls") or []
            if tc.get("id")
        }

        # Scan contiguous tool_results for the single view one. The
        # [note, one-other] rule guarantees at most one view per turn,
        # so first match wins — break on find.
        view_tr_idx = -1
        view_tool_name = ""
        j = i + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            name = name_by_id.get(messages[j].get("tool_call_id", ""), "")
            if name in SCREEN_OBS_TOOLS:
                view_tr_idx = j
                view_tool_name = name
                j += 1
                break
            j += 1
        while j < len(messages) and messages[j].get("role") == "tool":
            j += 1

        if view_tr_idx < 0:
            continue

        # Skip anything whose content is already a plain string: either
        # already-stubbed from a prior pass, or a failed-tool synthetic
        # result. Fresh view results arrive as multipart lists (text +
        # image_url); only those are eligible for compaction.
        content = messages[view_tr_idx].get("content")
        if isinstance(content, str):
            continue

        listing = _extract_text(content)
        text_rows = _filter_text_rows(listing)

        screen = _note_screen(messages[j]) if (
            j < len(messages) and messages[j].get("role") == "assistant"
        ) else ""

        head = f"(superseded {view_tool_name} — past view"
        head += f": {screen})" if screen else ")"
        messages[view_tr_idx]["content"] = (
            f"{head}\n{text_rows}" if text_rows else head
        )
