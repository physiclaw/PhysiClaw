"""Image ingress cap for LLM-bound payloads.

Lives in `common` because both sides of the provider/engine boundary
need the cap (`provider.wire` scales MCP images on ingress; the server
sizes its views to the same `compact.max_image_edge_px` knob) — when it
lived in `agent.engine.compact`, the provider package had to import the
engine's behavior module for one pure function.
"""

import logging
from io import BytesIO

from physiclaw.common import config

log = logging.getLogger(__name__)

_JPEG_MAGIC = b"\xff\xd8\xff"  # JPEG SOI marker
# Allow an 8K frame, while bounding a decoded BGR buffer to 96 MiB.
MAX_IMAGE_PIXELS = 32 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384


class ImageSizeError(ValueError):
    """Image dimensions exceed the application's allocation budget."""


def _require_image_size(width: int, height: int) -> None:
    if (
        width > MAX_IMAGE_DIMENSION
        or height > MAX_IMAGE_DIMENSION
        or width * height > MAX_IMAGE_PIXELS
    ):
        raise ImageSizeError(
            f"image dimensions {width}x{height} exceed the decode limit "
            f"({MAX_IMAGE_PIXELS} pixels, {MAX_IMAGE_DIMENSION} per dimension)"
        )


def _webp_header_size(raw: bytes) -> tuple[int, int] | None:
    # Pillow 13's WebP open allocates its animation canvas before returning
    # .size. Use its own header reader first, isolated here because it is a
    # private API within our pinned pillow>=13,<14 dependency. An unavailable
    # helper or unreadable header must never fall through to the decoder.
    from PIL import WebPImagePlugin

    reader = getattr(WebPImagePlugin, "_get_size", None)
    return reader(raw[:30]) if reader is not None else None


def checked_image_size(raw: bytes) -> tuple[int, int] | None:
    """Read supported metadata before pixel allocation, enforcing a hard cap.

    None means unsupported/corrupt metadata; callers keep their existing
    decode-failure policy. Oversized dimensions raise ImageSizeError and
    must never select a fallback that forwards or decodes the original.
    Restrict metadata probing to JPEG/PNG/WebP and leave Pillow's process-wide
    warning and decoder settings untouched.
    """
    from PIL import Image

    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        size = _webp_header_size(raw)
        if size is None:
            return None
        _require_image_size(*size)
    try:
        with Image.open(BytesIO(raw), formats=("JPEG", "PNG", "WEBP")) as encoded:
            size = encoded.size
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageSizeError("image dimensions exceed the decode limit") from exc
    except (OSError, ValueError, SyntaxError):
        return None
    _require_image_size(*size)
    return size


def image_dimensions(raw: bytes) -> tuple[int, int]:
    """Validate an encoded image and return its actual (width, height)."""
    import cv2
    import numpy as np

    if checked_image_size(raw) is None:
        raise ValueError("invalid observation image")
    try:
        decoded = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    except cv2.error as exc:
        raise ValueError("invalid observation image") from exc
    if decoded is None:
        raise ValueError("invalid observation image")
    height, width = decoded.shape[:2]
    return width, height


def scale_image_bytes(raw: bytes) -> tuple[bytes, str]:
    """Decode, scale long-edge to `compact.max_image_edge_px` if larger,
    re-encode JPEG.

    A JPEG already within the cap (the steady state — the server sizes
    views to the same knob) passes through byte-identical rather than
    stacking a second generation of JPEG loss onto on-screen text.

    Returns (bytes, mime_type). On decode/encode failure returns the input
    unchanged with a generic mime so the caller still has something to
    forward — context bloat is preferable to a dropped screenshot.
    """
    # Lazy cv2/numpy: common stays import-light. Config reads go through
    # the module — `set_dotted`/`unset_dotted` rebind `config.CONFIG`.
    import cv2
    import numpy as np

    max_edge = config.CONFIG.compact.max_image_edge_px
    quality = config.CONFIG.compact.jpeg_quality
    if checked_image_size(raw) is None:
        return raw, "application/octet-stream"
    try:
        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            log.info(
                "scale_image_bytes: decode failed on %d bytes",
                len(raw),
            )
            return raw, "application/octet-stream"
        h, w = img.shape[:2]
        long_edge = max(h, w)
        if long_edge <= max_edge and raw.startswith(_JPEG_MAGIC):
            return raw, "image/jpeg"
        if long_edge > max_edge:
            scale = max_edge / long_edge
            new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
            img = cv2.resize(img, new_size, interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            log.info("scale_image_bytes: encode failed")
            return raw, "application/octet-stream"
        return buf.tobytes(), "image/jpeg"
    except cv2.error:
        log.info(
            "scale_image_bytes: OpenCV could not process image",
            exc_info=True,
        )
        return raw, "application/octet-stream"
