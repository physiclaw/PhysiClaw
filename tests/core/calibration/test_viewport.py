"""Tests for `physiclaw.core.calibration.viewport` — pre-cal mapping.

Covers the disk-cache lookup (`_find_viewport_cache`) and the full
`measure_viewport_shift` step driven by synthetic screenshots: cache
hit, pending-upload fast path, fresh-wait fallback, and every
RuntimeError branch (missing dimension, timeout, decode failure, no
orange square).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest

from physiclaw.core.calibration import viewport as viewport_mod
from physiclaw.core.calibration.transforms import ViewportShift
from physiclaw.core.calibration.viewport import (
    _find_viewport_cache,
    measure_viewport_shift,
)

# ---------- _find_viewport_cache ----------


def test_find_viewport_cache_returns_none_when_absent(
    tmp_path: Path,
    mocker,
) -> None:
    mocker.patch.object(viewport_mod, "VIEWPORT_CACHE_STEM", tmp_path / "viewport")

    assert _find_viewport_cache() is None


def test_find_viewport_cache_returns_png_when_present(
    tmp_path: Path,
    mocker,
) -> None:
    stem = tmp_path / "viewport"
    png = stem.with_suffix(".png")
    png.write_bytes(b"\x89PNG")
    mocker.patch.object(viewport_mod, "VIEWPORT_CACHE_STEM", stem)

    out = _find_viewport_cache()

    assert out == png


def test_find_viewport_cache_prefers_png_over_jpg(
    tmp_path: Path,
    mocker,
) -> None:
    stem = tmp_path / "viewport"
    stem.with_suffix(".png").write_bytes(b"png")
    stem.with_suffix(".jpg").write_bytes(b"jpg")
    mocker.patch.object(viewport_mod, "VIEWPORT_CACHE_STEM", stem)

    assert _find_viewport_cache() == stem.with_suffix(".png")


def test_find_viewport_cache_falls_back_to_jpg(
    tmp_path: Path,
    mocker,
) -> None:
    stem = tmp_path / "viewport"
    jpg = stem.with_suffix(".jpg")
    jpg.write_bytes(b"jpg")
    mocker.patch.object(viewport_mod, "VIEWPORT_CACHE_STEM", stem)

    assert _find_viewport_cache() == jpg


# ---------- measure_viewport_shift ----------


def _orange_square_image(
    *,
    css_size: int = 50,
    dpr: float = 3.0,
    expected_cx: int = 125,
    expected_cy: int = 225,
    actual_cx: int | None = None,
    actual_cy: int | None = None,
    sw: int = 1170,
    sh: int = 2532,
    encoding: str = ".jpg",
) -> bytes:
    """Build a JPEG with an orange square at a known position."""
    img = np.zeros((sh, sw, 3), dtype=np.uint8)
    px_size = int(css_size * dpr)
    cx = actual_cx if actual_cx is not None else int(expected_cx * dpr)
    cy = actual_cy if actual_cy is not None else int(expected_cy * dpr)
    half = px_size // 2
    # OpenCV BGR — orange is roughly (0, 165, 255).
    img[cy - half : cy + half, cx - half : cx + half] = (0, 165, 255)
    ok, buf = cv2.imencode(encoding, img)
    assert ok
    return buf.tobytes()


def test_measure_viewport_shift_raises_when_dim_missing(mocker) -> None:
    mocker.patch.object(viewport_mod.time, "sleep")
    cal = MagicMock()
    cal.screen_dimension = None

    with pytest.raises(RuntimeError, match="Screen dimension not received"):
        measure_viewport_shift(cal, MagicMock())


def test_measure_viewport_shift_raises_when_dim_zero_width(mocker) -> None:
    mocker.patch.object(viewport_mod.time, "sleep")
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 0, "viewport_height": 100}

    with pytest.raises(RuntimeError, match="Screen dimension not received"):
        measure_viewport_shift(cal, MagicMock())


def test_measure_viewport_shift_raises_when_screenshot_timeout(
    mocker,
    tmp_path: Path,
    caplog,
) -> None:
    import logging

    mocker.patch.object(viewport_mod.time, "sleep")
    mocker.patch.object(
        viewport_mod,
        "VIEWPORT_CACHE_STEM",
        tmp_path / "viewport",
    )
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 390, "viewport_height": 844}
    bridge = MagicMock()
    bridge.take_pending_screenshot.return_value = None
    bridge.wait_screenshot.return_value = None
    bridge.connectivity_hint.return_value = None  # phone still polling

    def wait_screenshot(*args, **kwargs):
        # The instruction must be visible BEFORE the blocking wait.
        notices = [r for r in caplog.records if getattr(r, "console", False)]
        assert any("tap AssistiveTouch once" in r.console_message for r in notices)
        return None

    bridge.wait_screenshot.side_effect = wait_screenshot
    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError, match="Tap AssistiveTouch once"):
            measure_viewport_shift(cal, bridge, fresh=True)


def test_measure_viewport_shift_timeout_carries_connectivity_hint(
    mocker,
    tmp_path: Path,
) -> None:
    mocker.patch.object(viewport_mod.time, "sleep")
    mocker.patch.object(
        viewport_mod,
        "VIEWPORT_CACHE_STEM",
        tmp_path / "viewport",
    )
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 390, "viewport_height": 844}
    bridge = MagicMock()
    bridge.take_pending_screenshot.return_value = None
    bridge.wait_screenshot.return_value = None
    bridge.connectivity_hint.return_value = (
        "the phone may be off Wi-Fi or on a different Wi-Fi network than this computer"
    )

    with pytest.raises(RuntimeError, match="may be off Wi-Fi"):
        measure_viewport_shift(cal, bridge, fresh=True)


def test_measure_viewport_shift_raises_on_decode_failure(
    mocker,
    tmp_path: Path,
) -> None:
    mocker.patch.object(viewport_mod.time, "sleep")
    mocker.patch.object(
        viewport_mod,
        "VIEWPORT_CACHE_STEM",
        tmp_path / "viewport",
    )
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 390, "viewport_height": 844}
    bridge = MagicMock()
    bridge.take_pending_screenshot.return_value = None
    bridge.wait_screenshot.return_value = b"not an image"

    with pytest.raises(RuntimeError, match="Failed to decode"):
        measure_viewport_shift(cal, bridge, fresh=True)


def test_measure_viewport_shift_raises_when_no_orange_detected(
    mocker,
    tmp_path: Path,
) -> None:
    mocker.patch.object(viewport_mod.time, "sleep")
    mocker.patch.object(
        viewport_mod,
        "VIEWPORT_CACHE_STEM",
        tmp_path / "viewport",
    )
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 390, "viewport_height": 844}
    # All-black image — no orange to find.
    img = np.zeros((400, 400, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    bridge = MagicMock()
    bridge.take_pending_screenshot.return_value = None
    bridge.wait_screenshot.return_value = buf.tobytes()

    with pytest.raises(RuntimeError, match="Could not detect orange square"):
        measure_viewport_shift(cal, bridge, fresh=True)


def test_measure_viewport_shift_succeeds_and_caches(
    mocker,
    tmp_path: Path,
) -> None:
    mocker.patch.object(viewport_mod.time, "sleep")
    cache_stem = tmp_path / "viewport"
    mocker.patch.object(viewport_mod, "VIEWPORT_CACHE_STEM", cache_stem)
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 390, "viewport_height": 844}
    bridge = MagicMock()
    bridge.take_pending_screenshot.return_value = None
    bridge.wait_screenshot.return_value = _orange_square_image()

    transform = measure_viewport_shift(cal, bridge, fresh=True)

    assert isinstance(transform, ViewportShift)
    assert transform.dpr > 0
    assert cal.viewport_shift is transform
    # Cache was written.
    assert cache_stem.with_suffix(".img").exists()
    # No usable pending upload → any stale shot is discarded before the
    # wait, so it only sees post-square uploads.
    bridge.clear_screenshot.assert_called_once()


def test_measure_viewport_shift_uses_pending_upload_without_waiting(
    mocker,
    tmp_path: Path,
) -> None:
    # Best-UX path: the user tapped while the square was showing,
    # before pressing Measure — that shot is used straight away.
    mocker.patch.object(viewport_mod.time, "sleep")
    mocker.patch.object(viewport_mod, "VIEWPORT_CACHE_STEM", tmp_path / "viewport")
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 390, "viewport_height": 844}
    cal.phase_since = 123.0
    bridge = MagicMock()
    bridge.take_pending_screenshot.return_value = _orange_square_image()

    transform = measure_viewport_shift(cal, bridge, fresh=True)

    assert isinstance(transform, ViewportShift)
    bridge.take_pending_screenshot.assert_called_once_with(received_after=123.0)
    bridge.wait_screenshot.assert_not_called()
    bridge.clear_screenshot.assert_not_called()


def test_measure_viewport_shift_falls_back_when_pending_upload_unusable(
    mocker,
    tmp_path: Path,
) -> None:
    # A pending shot without the square (tapped mid-render) must not fail
    # the step — it falls back to waiting for a fresh upload.
    mocker.patch.object(viewport_mod.time, "sleep")
    mocker.patch.object(viewport_mod, "VIEWPORT_CACHE_STEM", tmp_path / "viewport")
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 390, "viewport_height": 844}
    bridge = MagicMock()
    bridge.take_pending_screenshot.return_value = b"not an image"
    bridge.wait_screenshot.return_value = _orange_square_image()

    transform = measure_viewport_shift(cal, bridge, fresh=True)

    assert isinstance(transform, ViewportShift)
    bridge.clear_screenshot.assert_called_once()
    bridge.wait_screenshot.assert_called_once()


def test_measure_viewport_shift_uses_cache_when_not_fresh(
    mocker,
    tmp_path: Path,
) -> None:
    mocker.patch.object(viewport_mod.time, "sleep")
    cache_stem = tmp_path / "viewport"
    cache_stem.with_suffix(".png").write_bytes(_orange_square_image())
    mocker.patch.object(viewport_mod, "VIEWPORT_CACHE_STEM", cache_stem)
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 390, "viewport_height": 844}
    bridge = MagicMock()

    transform = measure_viewport_shift(cal, bridge, fresh=False)

    assert isinstance(transform, ViewportShift)
    # Cache hit path → bridge.wait_screenshot never called.
    bridge.wait_screenshot.assert_not_called()


def test_measure_viewport_shift_caches_png_without_reencoding(
    mocker,
    tmp_path: Path,
) -> None:
    mocker.patch.object(viewport_mod.time, "sleep")
    cache_stem = tmp_path / "viewport"
    mocker.patch.object(viewport_mod, "VIEWPORT_CACHE_STEM", cache_stem)
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 390, "viewport_height": 844}
    bridge = MagicMock()
    # No pending upload → falls through to the fresh wait_screenshot path.
    bridge.take_pending_screenshot.return_value = None
    # Build a real PNG with the orange square.
    img = np.zeros((400, 400, 3), dtype=np.uint8)
    cv2.rectangle(img, (75, 175), (175, 275), (0, 165, 255), -1)
    ok, buf = cv2.imencode(".png", img)
    bridge.wait_screenshot.return_value = buf.tobytes()

    measure_viewport_shift(cal, bridge, fresh=True)

    assert cache_stem.with_suffix(".img").read_bytes() == buf.tobytes()


def _viewport_upload(mocker, tmp_path, data):
    mocker.patch.object(viewport_mod.time, "sleep")
    mocker.patch.object(viewport_mod, "VIEWPORT_CACHE_STEM", tmp_path / "viewport")
    cal = MagicMock(screen_dimension={"viewport_width": 390, "viewport_height": 844})
    bridge = MagicMock()
    bridge.take_pending_screenshot.return_value = data
    return cal, bridge


def test_regression_fresh_jpeg_supersedes_legacy_png_cache(mocker, tmp_path) -> None:
    (tmp_path / "viewport.png").write_bytes(_orange_square_image(encoding=".png"))
    data = _orange_square_image(actual_cy=775)
    cal, bridge = _viewport_upload(mocker, tmp_path, data)
    fresh = measure_viewport_shift(cal, bridge, fresh=True)

    cached = measure_viewport_shift(cal, bridge)

    assert fresh.offset_y > 50
    assert cached == fresh
    assert (tmp_path / "viewport.img").read_bytes() == data
    bridge.wait_screenshot.assert_not_called()


@pytest.mark.parametrize("bad_data", [b"", b"not an image"])
def test_measure_viewport_shift_replaces_corrupt_cache_from_upload(
    mocker, tmp_path, bad_data
) -> None:
    cache = tmp_path / "viewport.img"
    cache.write_bytes(bad_data)
    data = _orange_square_image()
    cal, bridge = _viewport_upload(mocker, tmp_path, data)

    transform = measure_viewport_shift(cal, bridge)

    assert cal.viewport_shift is transform
    assert cache.read_bytes() == data
    bridge.wait_screenshot.assert_not_called()


def test_measure_viewport_shift_uses_upload_when_cache_read_fails(
    mocker, tmp_path
) -> None:
    (tmp_path / "viewport.img").write_bytes(b"unreadable")
    cal, bridge = _viewport_upload(mocker, tmp_path, _orange_square_image())
    mocker.patch.object(Path, "read_bytes", side_effect=OSError("read failed"))

    transform = measure_viewport_shift(cal, bridge)

    assert cal.viewport_shift is transform
    bridge.take_pending_screenshot.assert_called_once()


def test_regression_cache_replace_failure_preserves_measurement_and_previous_bytes(
    mocker, tmp_path
) -> None:
    cache = tmp_path / "viewport.img"
    original = _orange_square_image()
    cache.write_bytes(original)
    cal, bridge = _viewport_upload(
        mocker, tmp_path, _orange_square_image(actual_cy=775)
    )
    mocker.patch.object(Path, "replace", side_effect=OSError("disk full"))

    transform = measure_viewport_shift(cal, bridge, fresh=True)

    assert cal.viewport_shift is transform
    assert transform.offset_y > 50
    assert cache.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_measure_viewport_shift_survives_cache_directory_failure(
    mocker, tmp_path
) -> None:
    cal, bridge = _viewport_upload(mocker, tmp_path, _orange_square_image())
    mocker.patch.object(Path, "mkdir", side_effect=OSError("permission denied"))

    transform = measure_viewport_shift(cal, bridge, fresh=True)

    assert cal.viewport_shift is transform


def test_measure_viewport_shift_cancelled_cache_write_preserves_previous_shift(
    mocker, tmp_path
) -> None:
    cal, bridge = _viewport_upload(mocker, tmp_path, _orange_square_image())
    previous = cal.viewport_shift
    mocker.patch.object(Path, "replace", side_effect=KeyboardInterrupt("cancelled"))

    with pytest.raises(KeyboardInterrupt, match="cancelled"):
        measure_viewport_shift(cal, bridge, fresh=True)

    assert cal.viewport_shift is previous
    assert not (tmp_path / "viewport.img").exists()
    assert list(tmp_path.glob("*.tmp")) == []
