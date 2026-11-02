"""Incoming MCP content becomes shared DTOs before reaching a provider."""

import base64
import logging

import pytest

from physiclaw.contract.content import mcp_blocks_to_content_blocks
from physiclaw.contract.dto import ImageBlock, TextBlock


@pytest.mark.parametrize(
    "extension,mime", [(".jpg", "image/jpeg"), (".png", "image/png")]
)
def test_regression_mcp_image_codec_failure_preserves_original_mime(
    extension, mime, mocker, monkeypatch
) -> None:
    import cv2
    import numpy as np

    from physiclaw.common import config

    _, encoded = cv2.imencode(extension, np.full((20, 20, 3), 128, dtype=np.uint8))
    data = base64.b64encode(encoded).decode()
    monkeypatch.setattr(config.CONFIG.compact, "max_image_edge_px", 10)
    mocker.patch.object(cv2, "resize", side_effect=cv2.error("resize failed"))

    out = mcp_blocks_to_content_blocks(
        [{"type": "image", "data": data, "mime_type": mime}]
    )

    assert out == [ImageBlock(media_type=mime, data_b64=data)]


def test_mcp_image_soft_scale_failure_defaults_missing_mime_to_jpeg(mocker) -> None:
    mocker.patch(
        "physiclaw.common.image.scale_image_bytes",
        return_value=(b"hi", "application/octet-stream"),
    )

    out = mcp_blocks_to_content_blocks([{"type": "image", "data": "aGk="}])

    assert out == [ImageBlock(media_type="image/jpeg", data_b64="aGk=")]


# ---------- mcp_blocks_to_content_blocks ----------


def test_mcp_blocks_single_text_block_returns_bare_string() -> None:
    # Fast path: avoids list-of-blocks overhead for plain-text results.
    out = mcp_blocks_to_content_blocks([{"type": "text", "text": "hello"}])

    assert out == "hello"


def test_mcp_blocks_single_text_block_with_no_text_field_returns_empty_string() -> None:
    out = mcp_blocks_to_content_blocks([{"type": "text"}])

    assert out == ""


def test_mcp_blocks_multiple_text_blocks_joined_by_newline() -> None:
    out = mcp_blocks_to_content_blocks(
        [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    )

    assert out == "a\nb"


def test_mcp_blocks_empty_list_returns_empty_marker() -> None:
    out = mcp_blocks_to_content_blocks([])

    assert out == "(empty tool result)"


def test_mcp_blocks_single_image_block_returns_list_with_one_image(
    mocker,
) -> None:
    # Patch scale_image_bytes — the contract under test is "scaled bytes
    # land in the ImageBlock", not the scaler's behavior.
    import base64 as _b64

    scaler = mocker.patch(
        "physiclaw.common.image.scale_image_bytes",
        return_value=(b"scaled", "image/png"),
    )

    out = mcp_blocks_to_content_blocks(
        [{"type": "image", "data": "aGk=", "mime_type": "image/png"}]
    )

    # Scaler must have been called with the actual decoded bytes — kills
    # mutants that pass `None` or skip the decode.
    scaler.assert_called_once_with(b"hi")

    assert isinstance(out, list)
    assert len(out) == 1
    assert isinstance(out[0], ImageBlock)
    assert out[0].media_type == "image/png"
    # Scaled bytes go through base64.b64encode — kills mutants that drop
    # the encode step or null out new_b64.
    assert out[0].data_b64 == _b64.b64encode(b"scaled").decode()


def test_mcp_blocks_text_then_image_returns_list_with_textblock_first(
    mocker,
) -> None:
    mocker.patch(
        "physiclaw.common.image.scale_image_bytes",
        return_value=(b"scaled", "image/jpeg"),
    )

    out = mcp_blocks_to_content_blocks(
        [
            {"type": "text", "text": "caption-1"},
            {"type": "text", "text": "caption-2"},
            {"type": "image", "data": "aGk=", "mime_type": "image/jpeg"},
        ]
    )

    assert isinstance(out, list)
    assert isinstance(out[0], TextBlock)
    assert out[0].text == "caption-1\ncaption-2"
    assert isinstance(out[1], ImageBlock)


def test_mcp_blocks_image_with_no_mime_type_defaults_to_jpeg_on_scale_failure(
    mocker,
) -> None:
    # scaler raises → fallback path uses `b.get("mime_type") or "image/jpeg"`.
    mocker.patch(
        "physiclaw.common.image.scale_image_bytes",
        side_effect=RuntimeError("scaler down"),
    )

    out = mcp_blocks_to_content_blocks([{"type": "image", "data": "aGk="}])

    assert isinstance(out, list)
    assert isinstance(out[0], ImageBlock)
    assert out[0].media_type == "image/jpeg"
    # Original (unscaled) base64 forwarded.
    assert out[0].data_b64 == "aGk="


def test_mcp_blocks_image_scale_failure_logs_exception(
    mocker, caplog: pytest.LogCaptureFixture
) -> None:
    mocker.patch(
        "physiclaw.common.image.scale_image_bytes",
        side_effect=RuntimeError("scaler down"),
    )

    with caplog.at_level(logging.INFO, logger="physiclaw.contract.content"):
        out = mcp_blocks_to_content_blocks([{"type": "image", "data": "aGk="}])

    (record,) = [
        r
        for r in caplog.records
        if r.getMessage().startswith("scale failed; forwarding original image")
    ]
    assert record.levelno == logging.INFO and record.exc_info
    assert out[0].data_b64 == "aGk="


def test_mcp_blocks_image_scale_failure_preserves_explicit_mime_type(
    mocker,
) -> None:
    # In the fallback path, mime falls back to b.get("mime_type") — when
    # the block declares a mime, that wins over the "image/jpeg" default.
    mocker.patch(
        "physiclaw.common.image.scale_image_bytes",
        side_effect=RuntimeError("scaler down"),
    )

    out = mcp_blocks_to_content_blocks(
        [{"type": "image", "data": "aGk=", "mime_type": "image/png"}]
    )

    assert isinstance(out, list)
    assert isinstance(out[0], ImageBlock)
    assert out[0].media_type == "image/png"


def test_mcp_blocks_unknown_block_type_silently_dropped(mocker) -> None:
    mocker.patch(
        "physiclaw.common.image.scale_image_bytes",
        return_value=(b"scaled", "image/png"),
    )

    # Unknown kind contributes nothing — outcome is determined by the
    # rest of the blocks.
    out = mcp_blocks_to_content_blocks(
        [{"type": "weird"}, {"type": "text", "text": "kept"}]
    )

    assert out == "kept"


def test_mcp_blocks_multi_block_text_and_no_image_returns_joined_string() -> None:
    # Skips the fast path (len != 1) but no images, so falls through to
    # the joined-string return.
    out = mcp_blocks_to_content_blocks(
        [{"type": "text", "text": "a"}, {"type": "weird"}]
    )

    assert out == "a"


def test_mcp_blocks_text_with_missing_text_field_appends_empty_string() -> None:
    # The slow path's `b.get("text") or ""` — when the field is missing,
    # the result must be empty string concatenation, not a sentinel.
    out = mcp_blocks_to_content_blocks(
        [{"type": "text"}, {"type": "text", "text": "second"}]
    )

    assert out == "\nsecond"
