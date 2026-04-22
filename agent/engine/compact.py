"""Context compaction — two jobs, both about keeping image payload small:

  1. `drop_stale_screens` — "latest screen wins": only the most recent
     scan/peek/screenshot tool_result carries pixels + full listing.
     Earlier view tool_results are stubbed in place with the textual
     description of what that image showed (pulled from the NEXT turn's
     `note.screen`, which was composed while that image was the latest
     view). The assistant message and its tool_calls stay intact —
     the agent's decision history ("I called scan here, tap there") is
     preserved; only the bulky result payload is elided.

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
SCREEN_OBS_TOOLS = frozenset({"scan", "peek", "screenshot"})


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


def _extract_note_screen(asst: dict[str, Any]) -> str:
    """Return the `screen` argument of a `note` tool_call on `asst`, or ''
    if absent. Arguments arrive as a JSON string (wire format produced by
    `assistant_to_wire`)."""
    for tc in asst.get("tool_calls") or []:
        fn = tc.get("function") or {}
        if fn.get("name") != "note":
            continue
        raw = fn.get("arguments")
        if not isinstance(raw, str):
            return ""
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            return ""
        s = args.get("screen")
        if isinstance(s, str) and s.strip():
            return s.strip()
    return ""


def drop_stale_screens(messages: list[dict[str, Any]]) -> None:
    """Stub earlier view tool_results; keep asst + tool_call history intact.

    For each screen-observation turn X before the latest:
      - Keep the assistant message (tool_calls list untouched).
      - Keep the `note` tool_result (small text, harmless).
      - Replace the view tool's tool_result content with a short string
        like `"(superseded <tool> — past view: <desc>)"` (or `"(superseded
        <tool> — past view)"` if no description is available). `<desc>`
        comes from the NEXT turn's `note.screen` — that turn was composed
        while image_X was the latest visible, so its note.screen
        describes what we're about to elide.

    The latest screen-observation's pair is untouched — its pixels and
    listing are still the agent's live view.

    Runs in place. Idempotent: the stubbed tool_result is plain text;
    a second pass finds the same descriptions and writes the same stub.
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
        # Advance j past the remainder of the tool-result run so the
        # next-asst lookup below points at the following assistant.
        while j < len(messages) and messages[j].get("role") == "tool":
            j += 1

        if view_tr_idx < 0:
            continue

        description = ""
        if j < len(messages) and messages[j].get("role") == "assistant":
            description = _extract_note_screen(messages[j])

        tail = f": {description}" if description else ""
        messages[view_tr_idx]["content"] = (
            f"(superseded {view_tool_name} — past view{tail})"
        )
