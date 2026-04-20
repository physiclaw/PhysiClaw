"""Context compaction.

Two jobs, both about keeping per-turn image payload small:

  1. `prior_image` — history invariant: only the most recent image-bearing
     message retains pixels; older images get stripped. The model's prior
     `describe_view` response (in the assistant message) covers the stale
     ones.

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


def prior_image(messages: list[dict[str, Any]]) -> None:
    """Strip images from the second-latest image-bearing message.

    Role-agnostic: walks back from the end, finds the latest image-bearing
    message, then the one before it, and strips that one's images. Under
    the invariant (held by calling this after every message append), no
    earlier message can still carry images.

    Image blocks are identified as `{"type": "image_url", ...}` within
    `content` lists — the shape both user messages and tool-role messages
    use when they carry pixels.
    """
    found_latest = False
    for i in range(len(messages) - 1, -1, -1):
        content = messages[i].get("content")
        if not isinstance(content, list):
            continue
        if not any(b.get("type") == "image_url" for b in content):
            continue
        if not found_latest:
            found_latest = True
            continue
        filtered = [b for b in content if b.get("type") != "image_url"]
        messages[i]["content"] = (
            filtered if filtered else [{"type": "text", "text": "(image elided)"}]
        )
        return
