"""Context compaction.

Keeps raw-image footprint bounded: at most one image lives in history
at a time. The model's prior-turn description + curated_bbox (stored in
the assistant message) covers anything older.
"""
from typing import Any


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
