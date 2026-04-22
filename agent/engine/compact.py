"""Context compaction — two jobs, both about keeping image payload small:

  1. `drop_stale_screens` — "latest screen wins": only the most recent
     peek/screenshot tool_result carries pixels + full listing.
     Earlier view tool_results are stubbed in place with what the agent
     chose to remember from that view: the `note.screen` description
     and the `note.key_ui_elements` pins from the NEXT turn (the turn
     composed while that image was the latest, so its note describes
     what to carry forward). Everything else — pixels, full listing —
     is elided. The assistant message and its tool_calls stay intact,
     so the decision history ("I called peek here, tap there") is
     preserved.

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


def _note_args(asst: dict[str, Any]) -> dict:
    """Return the parsed `note` tool_call arguments on `asst`, or {}.
    Arguments arrive as a JSON string (wire format produced by
    `assistant_to_wire`)."""
    for tc in asst.get("tool_calls") or []:
        fn = tc.get("function") or {}
        if fn.get("name") != "note":
            continue
        raw = fn.get("arguments")
        if not isinstance(raw, str):
            return {}
        try:
            return json.loads(raw) or {}
        except json.JSONDecodeError:
            return {}
    return {}


def _format_pinned(pinned: dict) -> str:
    """Render `note.key_ui_elements` as a listing block. One line per entry
    in the same shape as the live element listing: `<semantic> [kind]
    "label" [bbox]`. Skips malformed entries silently."""
    lines: list[str] = []
    for semantic, spec in pinned.items():
        if not isinstance(semantic, str) or not isinstance(spec, dict):
            continue
        kind = spec.get("kind")
        label = spec.get("label")
        bbox = spec.get("bbox")
        if not (isinstance(kind, str) and isinstance(label, str)
                and isinstance(bbox, list) and len(bbox) == 4):
            continue
        coords = ",".join(f"{c:.3f}" for c in bbox)
        lines.append(f'  {semantic} [{kind}] "{label}" [{coords}]')
    return "\n".join(lines)


# ---------- pin validation ----------
#
# The model sometimes regenerates bbox digits rather than copying them
# verbatim ("0.520" → "0.518"). For small targets that 0.002 drift can
# land on the neighboring icon. Before the assistant message gets wired
# into history, we check each pinned bbox against the bboxes in the
# latest view listing; anything that doesn't match (within tolerance)
# is dropped silently. Conservative fallbacks: if we can't locate a
# listing to compare against, pins pass through unchanged.

PIN_MATCH_TOLERANCE = 1e-6  # floats only — `0.520` == `0.52` exactly.


def _parse_listing(text: str) -> list[tuple[float, ...]]:
    """Pull bboxes from a live listing text block. Each row is
    `id [kind] "label" [left,top,right,bottom] conf`; the bbox is the
    second `[...]` pair."""
    out: list[tuple[float, ...]] = []
    for line in text.splitlines():
        kind_close = line.find("]")
        if kind_close < 0:
            continue
        bbox_open = line.find("[", kind_close + 1)
        if bbox_open < 0:
            continue
        bbox_close = line.find("]", bbox_open + 1)
        if bbox_close < 0:
            continue
        try:
            coords = tuple(float(x) for x in line[bbox_open + 1:bbox_close].split(","))
        except ValueError:
            continue
        if len(coords) == 4:
            out.append(coords)
    return out


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


def _latest_listing_bboxes(messages: list[dict[str, Any]]) -> list[tuple[float, ...]] | None:
    """Parse bboxes from the most recent view tool_result. Returns None
    when we can't locate a listing (no view yet, stubbed, or zero rows
    parsed) — caller treats None as "can't validate, leave pins alone"."""
    for i in range(len(messages) - 1, -1, -1):
        asst = messages[i]
        if not _is_screen_obs_turn(asst):
            continue
        name_by_id = {
            tc["id"]: ((tc.get("function") or {}).get("name", ""))
            for tc in asst.get("tool_calls") or []
            if tc.get("id")
        }
        j = i + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            name = name_by_id.get(messages[j].get("tool_call_id", ""), "")
            if name in SCREEN_OBS_TOOLS:
                bboxes = _parse_listing(_extract_text(messages[j].get("content")))
                return bboxes or None
            j += 1
        return None
    return None


def _bbox_matches_any(bbox: list, rows: list[tuple[float, ...]]) -> bool:
    try:
        coords = tuple(float(c) for c in bbox)
    except (TypeError, ValueError):
        return False
    if len(coords) != 4:
        return False
    return any(
        all(abs(a - b) <= PIN_MATCH_TOLERANCE for a, b in zip(coords, row))
        for row in rows
    )


def filter_note_pins(messages: list[dict[str, Any]], note_args: dict) -> int:
    """Drop pins whose bbox doesn't match any row in the latest view
    listing. Mutates `note_args['key_ui_elements']` in place; returns
    the count of dropped entries for caller's logging. Silent to the
    agent — no error, no warning, the dropped entries simply don't
    appear in history.
    """
    pinned = note_args.get("key_ui_elements") or {}
    if not isinstance(pinned, dict) or not pinned:
        return 0
    rows = _latest_listing_bboxes(messages)
    if rows is None:
        return 0
    kept: dict = {}
    dropped = 0
    for semantic, spec in pinned.items():
        bbox = spec.get("bbox") if isinstance(spec, dict) else None
        if isinstance(bbox, list) and _bbox_matches_any(bbox, rows):
            kept[semantic] = spec
        else:
            dropped += 1
    if dropped:
        note_args["key_ui_elements"] = kept
    return dropped


def drop_stale_screens(messages: list[dict[str, Any]]) -> None:
    """Stub earlier view tool_results; keep asst + tool_call history intact.

    For each screen-observation turn X before the latest:
      - Keep the assistant message (tool_calls list untouched).
      - Keep the `note` tool_result (small text, harmless).
      - Replace the view tool's tool_result content with a stub built
        from the NEXT turn's `note` args: its `screen` (textual view
        description) and its `key_ui_elements` (agent-pinned bboxes to
        carry forward). The next turn was composed while image_X was
        the latest visible — its note carries what the agent chose to
        remember from that view.

    The latest screen-observation's pair is untouched — its pixels and
    listing are still the agent's live view.

    Runs in place. Idempotent: the stubbed tool_result is plain text;
    a second pass finds the same note args and writes the same stub.
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

        next_args: dict = {}
        if j < len(messages) and messages[j].get("role") == "assistant":
            next_args = _note_args(messages[j])

        screen = next_args.get("screen") or ""
        screen = screen.strip() if isinstance(screen, str) else ""
        pinned = next_args.get("key_ui_elements") or {}
        pinned_block = _format_pinned(pinned) if isinstance(pinned, dict) else ""

        head = f"(superseded {view_tool_name} — past view"
        head += f": {screen})" if screen else ")"
        if pinned_block:
            messages[view_tr_idx]["content"] = f"{head}\npinned from this view:\n{pinned_block}"
        else:
            messages[view_tr_idx]["content"] = head
