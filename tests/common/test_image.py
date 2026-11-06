"""Tests for `physiclaw.common.image` — the image ingress cap."""

from __future__ import annotations

import base64
import struct
import warnings
import zlib
from io import BytesIO

import cv2
import numpy as np
import pytest

from physiclaw.common import config, image


def _png_header(width: int, height: int) -> bytes:
    """A tiny IHDR/IDAT prefix with no image plane to allocate."""
    data = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + data
        + struct.pack(">I", zlib.crc32(data))
        + struct.pack(">I", 0)
        + b"IDAT"
    )


def _webp_header(width: int, height: int) -> bytes:
    return (
        b"RIFF"
        + struct.pack("<I", 22)
        + b"WEBPVP8X"
        + struct.pack("<I", 10)
        + bytes(4)
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )


def _jpeg_header(width: int, height: int) -> bytes:
    raw = bytearray(_encode_jpg(np.full((2, 2, 3), 128, dtype=np.uint8)))
    frame = raw.index(b"\xff\xc0")
    raw[frame + 5 : frame + 9] = struct.pack(">HH", height, width)
    return bytes(raw)


@pytest.mark.parametrize("extension", [".jpg", ".png", ".webp"])
def test_supported_metadata_keeps_small_image_ingress_working(extension) -> None:
    from physiclaw.core.vision.util import decode_image

    ok, encoded = cv2.imencode(extension, np.full((20, 30, 3), 128, dtype=np.uint8))
    assert ok
    raw = encoded.tobytes()

    assert image.checked_image_size(raw) == (30, 20)
    assert image.image_dimensions(raw) == (30, 20)
    assert decode_image(raw).shape == (20, 30, 3)
    assert image.scale_image_bytes(raw)[1] == "image/jpeg"


@pytest.mark.parametrize("size", [(6000, 6000), (16_385, 1), (1, 16_385)])
@pytest.mark.parametrize("header", [_png_header, _jpeg_header])
@pytest.mark.parametrize(
    "operation", ["metadata", "dimensions", "scale", "core", "content"]
)
def test_oversized_header_is_rejected_before_opencv(
    size, header, operation, mocker
) -> None:
    from physiclaw.contract.content import mcp_blocks_to_content_blocks
    from physiclaw.core.vision.util import decode_image

    raw = header(*size)
    decode = mocker.patch.object(
        cv2, "imdecode", side_effect=AssertionError("pixel decode")
    )
    operations = {
        "metadata": lambda: image.checked_image_size(raw),
        "dimensions": lambda: image.image_dimensions(raw),
        "scale": lambda: image.scale_image_bytes(raw),
        "core": lambda: decode_image(raw),
        "content": lambda: mcp_blocks_to_content_blocks(
            [
                {
                    "type": "image",
                    "data": base64.b64encode(raw).decode(),
                    "mime_type": "image/png",
                }
            ]
        ),
    }

    if operation == "content":
        result = operations[operation]()
        assert isinstance(result, str) and "Image omitted:" in result
        assert "decode limit" in result
        assert base64.b64encode(raw).decode() not in result
    else:
        with pytest.raises(image.ImageSizeError, match="decode limit"):
            operations[operation]()
    decode.assert_not_called()


def test_webp_size_is_checked_before_pillow_allocates_a_canvas(mocker) -> None:
    from PIL import Image

    open_image = mocker.patch.object(
        Image, "open", side_effect=AssertionError("canvas allocation")
    )
    with pytest.raises(image.ImageSizeError, match="decode limit"):
        image.checked_image_size(_webp_header(6000, 6000))
    open_image.assert_not_called()


def test_pillow_bomb_rejection_remains_a_hard_size_error(mocker) -> None:
    decode = mocker.patch.object(
        cv2, "imdecode", side_effect=AssertionError("pixel decode")
    )

    with pytest.raises(image.ImageSizeError, match="decode limit"):
        image.image_dimensions(_png_header(50_000, 50_000))
    decode.assert_not_called()


@pytest.mark.parametrize("operation", ["metadata", "scale", "content"])
def test_pillow_bomb_warning_as_error_never_forwards_oversized_image(
    operation, mocker
) -> None:
    from PIL import Image

    from physiclaw.contract.content import mcp_blocks_to_content_blocks

    raw = _png_header(10_000, 10_000)
    decode = mocker.patch.object(cv2, "imdecode")
    operations = {
        "metadata": lambda: image.checked_image_size(raw),
        "scale": lambda: image.scale_image_bytes(raw),
        "content": lambda: mcp_blocks_to_content_blocks(
            [{"type": "image", "data": base64.b64encode(raw).decode()}]
        ),
    }

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        if operation == "content":
            result = operations[operation]()
            assert isinstance(result, str) and "Image omitted:" in result
            assert "decode limit" in result
            assert base64.b64encode(raw).decode() not in result
        else:
            with pytest.raises(image.ImageSizeError, match="decode limit"):
                operations[operation]()
    decode.assert_not_called()


def test_unreadable_webp_header_never_reaches_pillow_decoder(mocker) -> None:
    from PIL import Image

    open_image = mocker.patch.object(
        Image, "open", side_effect=AssertionError("canvas allocation")
    )
    assert image.checked_image_size(_webp_header(30, 20)[:24]) is None
    open_image.assert_not_called()


def test_missing_pillow_webp_header_helper_fails_closed(monkeypatch, mocker) -> None:
    from PIL import Image, WebPImagePlugin

    monkeypatch.delattr(WebPImagePlugin, "_get_size")
    open_image = mocker.patch.object(
        Image, "open", side_effect=AssertionError("canvas allocation")
    )
    assert image.checked_image_size(_webp_header(6000, 6000)) is None
    open_image.assert_not_called()


def test_unsupported_image_format_keeps_fallback_without_opencv_decode(mocker) -> None:
    from PIL import Image

    encoded = BytesIO()
    Image.new("RGB", (2, 2)).save(encoded, format="TIFF")
    raw = encoded.getvalue()
    decode = mocker.patch.object(
        cv2, "imdecode", side_effect=AssertionError("pixel decode")
    )

    assert image.scale_image_bytes(raw) == (raw, "application/octet-stream")
    with pytest.raises(ValueError, match="invalid observation image"):
        image.image_dimensions(raw)
    decode.assert_not_called()


def _encode_jpg(arr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", arr)
    assert ok
    return buf.tobytes()


def test_scale_image_bytes_scales_when_over_max_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.CONFIG.compact, "max_image_edge_px", 100)
    img = np.full((300, 600, 3), 128, dtype=np.uint8)
    raw = _encode_jpg(img)

    out_bytes, mime = image.scale_image_bytes(raw)

    assert mime == "image/jpeg"
    decoded = cv2.imdecode(np.frombuffer(out_bytes, np.uint8), cv2.IMREAD_COLOR)
    # Long edge 600 → 100; aspect 2 preserved.
    assert max(decoded.shape[:2]) == 100


def test_scale_image_bytes_returns_input_on_decode_failure() -> None:
    raw = b"definitely not an image"

    out_bytes, mime = image.scale_image_bytes(raw)

    assert out_bytes == raw
    assert mime == "application/octet-stream"


def test_scale_image_bytes_jpeg_within_cap_passes_through_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The server already sized this view to the shared knob — a re-encode
    # would only stack a second generation of JPEG loss onto screen text.
    monkeypatch.setattr(config.CONFIG.compact, "max_image_edge_px", 1000)
    raw = _encode_jpg(np.full((300, 200, 3), 128, dtype=np.uint8))

    out_bytes, mime = image.scale_image_bytes(raw)

    assert out_bytes is raw
    assert mime == "image/jpeg"


def test_scale_image_bytes_png_within_cap_reencoded_to_jpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.CONFIG.compact, "max_image_edge_px", 1000)
    ok, buf = cv2.imencode(".png", np.full((300, 200, 3), 128, dtype=np.uint8))
    assert ok
    raw = buf.tobytes()

    out_bytes, mime = image.scale_image_bytes(raw)

    assert mime == "image/jpeg"
    assert out_bytes.startswith(b"\xff\xd8\xff")


@pytest.mark.parametrize("shape", [(1, 5000, 3), (5000, 1, 3)])
def test_regression_scale_image_keeps_thin_dimension_nonzero(
    shape, monkeypatch
) -> None:
    monkeypatch.setattr(config.CONFIG.compact, "max_image_edge_px", 100)
    raw = _encode_jpg(np.full(shape, 128, dtype=np.uint8))

    out, mime = image.scale_image_bytes(raw)

    decoded = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)
    assert sorted(decoded.shape[:2]) == [1, 100]
    assert mime == "image/jpeg"


def test_regression_scale_image_empty_bytes_return_unchanged() -> None:
    raw = b""

    out, mime = image.scale_image_bytes(raw)

    assert out is raw
    assert mime == "application/octet-stream"


@pytest.mark.parametrize("operation", ["imdecode", "resize", "imencode"])
def test_scale_image_opencv_failure_returns_original(
    operation, monkeypatch, mocker
) -> None:
    monkeypatch.setattr(config.CONFIG.compact, "max_image_edge_px", 10)
    raw = _encode_jpg(np.full((20, 20, 3), 128, dtype=np.uint8))
    mocker.patch.object(cv2, operation, side_effect=cv2.error("codec failed"))

    out, mime = image.scale_image_bytes(raw)

    assert out is raw
    assert mime == "application/octet-stream"


def test_scale_image_unsuccessful_encode_returns_original(monkeypatch, mocker) -> None:
    monkeypatch.setattr(config.CONFIG.compact, "max_image_edge_px", 10)
    raw = _encode_jpg(np.full((20, 20, 3), 128, dtype=np.uint8))
    mocker.patch.object(cv2, "imencode", return_value=(False, None))

    out, mime = image.scale_image_bytes(raw)

    assert out is raw
    assert mime == "application/octet-stream"
