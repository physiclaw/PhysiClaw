"""Context compaction — two jobs, both about keeping image payload small:

  1. `drop_stale_screens` — "latest screen wins": only the most recent
     screen-observation (asst + its tool_results) pair survives in
     history. Older scan/peek/screenshot pairs are deleted outright. The
     `note.screen` argument on each physical-action turn preserves what
     the agent saw as text, so the decision trail is never lost.

  2. `scale_image_bytes` — ingress re-encode: normalize every incoming
     tool-result image to JPEG with long edge ≤ MAX_IMAGE_EDGE. Drops PNG
     transparency (fine for screenshots), typically cuts payload 3–10×.
"""
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
    read_memory, update_plan, …) is a boundary and stays put."""
    names = _tool_names(asst)
    if not names:
        return False
    allowed = SCREEN_OBS_TOOLS | {"note"}
    if not all(n in allowed for n in names):
        return False
    return any(n in SCREEN_OBS_TOOLS for n in names)


def drop_stale_screens(messages: list[dict[str, Any]]) -> None:
    """Delete every screen-observation pair except the most recent one.

    Walks messages in place. A "pair" is an assistant screen-obs turn
    plus its following `tool` messages. Earlier screen-obs pairs have
    been superseded by the newest view of the phone and carry no signal
    the agent still needs — their `note.summary` was scratch reasoning,
    and their `note.screen` (if any) duplicates what the next action's
    `note.screen` captures. The latest pair survives untouched.

    Idempotent: running twice yields the same result as running once.
    """
    latest = -1
    for i in range(len(messages) - 1, -1, -1):
        if _is_screen_obs_turn(messages[i]):
            latest = i
            break
    if latest < 0:
        return

    to_drop: list[int] = []
    i = 0
    while i < latest:
        if _is_screen_obs_turn(messages[i]):
            to_drop.append(i)
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                to_drop.append(j)
                j += 1
            i = j
        else:
            i += 1

    # Delete in reverse so earlier indices stay valid as we remove.
    for idx in reversed(to_drop):
        del messages[idx]
