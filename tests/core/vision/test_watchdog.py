"""Tests for `physiclaw.core.vision.watchdog`."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import numpy as np
from freezegun import freeze_time

from physiclaw.core.vision import watchdog
from physiclaw.core.vision.watchdog import (
    BADGE_MIN_AREA,
    EMA_FAST,
    EMA_SLOW,
    EMA_STALE,
    IDLE_INTERVAL,
    MEAN_INCREASE,
    STD_INCREASE,
    Watchdog,
    WORK_HOURS,
    ZONES,
    _check_badge,
    _check_content,
    _crop_zones,
    _ema_update,
    _gray,
)


# ---------- _gray ----------


def test_gray_converts_bgr_to_grayscale() -> None:
    bgr = np.full((10, 10, 3), [255, 0, 0], dtype=np.uint8)  # blue

    out = _gray(bgr)

    assert out.ndim == 2
    assert out.shape == (10, 10)


# ---------- _check_content ----------


def test_check_content_no_change() -> None:
    a = np.full((20, 20, 3), 100, dtype=np.uint8)

    out = _check_content(a, a)

    assert out["wake"] is False
    assert out["std_delta"] == 0.0
    assert out["mean_delta"] == 0.0


def test_check_content_wake_on_mean_increase() -> None:
    slow = np.full((20, 20, 3), 100, dtype=np.uint8)
    fast = np.full((20, 20, 3), 100 + int(MEAN_INCREASE) + 5, dtype=np.uint8)

    out = _check_content(slow, fast)

    assert out["wake"] is True


def test_check_content_wake_on_std_increase() -> None:
    slow = np.full((20, 20, 3), 100, dtype=np.uint8)
    # Mix of values for high std.
    fast = slow.copy()
    fast[::2] = 0
    fast[1::2] = 200

    out = _check_content(slow, fast)

    assert out["wake"] is True
    assert out["std_delta"] > STD_INCREASE


# ---------- _check_badge ----------


def test_check_badge_wake_on_red_pixel_increase() -> None:
    slow = np.zeros((20, 20, 3), dtype=np.uint8)
    fast = slow.copy()
    # OpenCV uses BGR — pure red is (0, 0, 255).
    fast[:8, :8] = (0, 0, 255)

    out = _check_badge(slow, fast)

    assert out["wake"] is True
    assert out["red_delta"] > BADGE_MIN_AREA


def test_check_badge_no_change() -> None:
    a = np.zeros((10, 10, 3), dtype=np.uint8)

    out = _check_badge(a, a)

    assert out["wake"] is False
    assert out["red_delta"] == 0


# ---------- _ema_update ----------


def test_ema_update_blends_frames() -> None:
    ema = np.zeros((4, 4), dtype=np.float32)
    frame = np.full((4, 4), 100, dtype=np.uint8)

    out = _ema_update(ema, frame, alpha=0.5)

    assert out.dtype == np.float32
    np.testing.assert_allclose(out, np.full((4, 4), 50.0))


# ---------- _crop_zones ----------


def _fake_transforms(*, w: int = 100, h: int = 200):
    """Transforms whose pct_to_cam_pixel maps (px, py) → (px*w, py*h)."""
    t = MagicMock()
    t.pct_to_cam_pixel.side_effect = lambda px, py: (int(px * w), int(py * h))
    return t


def test_crop_zones_returns_three_crops() -> None:
    frame = np.zeros((200, 100, 3), dtype=np.uint8)

    crops = _crop_zones(frame, _fake_transforms(w=100, h=200))

    assert crops is not None
    assert len(crops) == len(ZONES)
    # Each crop has positive area.
    for c in crops:
        assert c.size > 0


def test_crop_zones_returns_none_on_empty_crop() -> None:
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    t = MagicMock()
    # Both corners map outside frame → empty crop.
    t.pct_to_cam_pixel.side_effect = lambda px, py: (1000, 1000)

    assert _crop_zones(frame, t) is None


# ---------- Watchdog ----------


def _frame() -> np.ndarray:
    return np.zeros((200, 100, 3), dtype=np.uint8)


def test_watchdog_init_state() -> None:
    w = Watchdog()

    assert w._ema is None
    assert w._poll_time == 0.0


def test_watchdog_first_poll_initializes_ema() -> None:
    w = Watchdog()

    out = w.poll(_frame(), _fake_transforms())

    assert out["wake"] is False
    assert out["reason"] == "ema initialized"
    assert w._ema is not None


def test_watchdog_returns_no_wake_when_zones_unavailable() -> None:
    w = Watchdog()
    t = MagicMock()
    t.pct_to_cam_pixel.side_effect = lambda px, py: (1000, 1000)

    out = w.poll(np.zeros((10, 10, 3), dtype=np.uint8), t)

    assert out == {"wake": False, "reason": ""}


def test_watchdog_reinitializes_after_stale_gap(mocker) -> None:
    w = Watchdog()
    w.poll(_frame(), _fake_transforms())
    # Jump time forward past EMA_STALE.
    mocker.patch.object(
        watchdog.time, "monotonic", return_value=w._poll_time + EMA_STALE + 1,
    )

    out = w.poll(_frame(), _fake_transforms())

    assert out["reason"] == "ema initialized"


def test_watchdog_steady_frames_no_wake() -> None:
    w = Watchdog()
    t = _fake_transforms()
    f = _frame()
    w.poll(f, t)

    out = w.poll(f, t)

    assert out["wake"] is False


def test_watchdog_banner_change_wakes() -> None:
    w = Watchdog()
    t = _fake_transforms()
    base = np.full((200, 100, 3), 100, dtype=np.uint8)
    w.poll(base, t)
    # Modify only the banner zone (y 0-0.1 → rows 0-19 of 200).
    bright = base.copy()
    bright[:20] = 200

    out = w.poll(bright, t)

    assert out["wake"] is True
    assert "banner" in out["reason"]


def test_watchdog_bottom_change_wakes() -> None:
    w = Watchdog()
    t = _fake_transforms()
    base = np.full((200, 100, 3), 100, dtype=np.uint8)
    w.poll(base, t)
    bright = base.copy()
    # Bottom zone is y 0.5-1.0 → rows 100-199.
    bright[100:] = 200

    out = w.poll(bright, t)

    assert out["wake"] is True
    assert "lower half" in out["reason"]


def test_watchdog_dock_red_badge_wakes() -> None:
    w = Watchdog()
    t = _fake_transforms()
    base = np.full((200, 100, 3), 100, dtype=np.uint8)
    w.poll(base, t)
    badged = base.copy()
    # Dock zone is y 0.85-1.0 → rows 170-199. Paint a large red area
    # and poll several times so fast EMA converges enough for the
    # HSV saturation threshold to register the red pixels.
    badged[170:200, :] = (0, 0, 255)

    out = None
    for _ in range(15):
        out = w.poll(badged, t)
        if out["wake"]:
            break

    assert out["wake"] is True
    assert "red badge" in out["reason"]


def test_watchdog_idle_fallback_during_work_hours() -> None:
    w = Watchdog()

    with freeze_time("2026-04-28 10:00:00"):
        # First poll initializes EMA.
        w.poll(_frame(), _fake_transforms())
        # Force last_wake to long ago.
        w._last_wake = time.monotonic() - IDLE_INTERVAL - 100

        out = w.poll(_frame(), _fake_transforms())

    assert out["wake"] is True
    assert "idle check-in" in out["reason"]


def test_watchdog_idle_fallback_outside_work_hours() -> None:
    w = Watchdog()
    w.poll(_frame(), _fake_transforms())
    w._last_wake = time.monotonic() - IDLE_INTERVAL - 100

    with freeze_time("2026-04-28 22:00:00"):
        out = w.poll(_frame(), _fake_transforms())

    # Outside work hours — no idle wake.
    assert out["wake"] is False


def test_watchdog_resets_last_wake_on_real_wake() -> None:
    w = Watchdog()
    t = _fake_transforms()
    base = np.full((200, 100, 3), 100, dtype=np.uint8)
    w.poll(base, t)
    bright = base.copy()
    bright[:20] = 200

    w.poll(bright, t)

    # last_wake updated to most recent poll time.
    assert w._last_wake == w._poll_time


def test_watchdog_constants_unchanged() -> None:
    # Defensive guard: changing thresholds is a behavior change.
    assert STD_INCREASE == 5.0
    assert MEAN_INCREASE == 5.0
    assert BADGE_MIN_AREA == 50
    assert IDLE_INTERVAL == 1800.0
    assert WORK_HOURS == [(9, 12), (14, 17)]
    assert ZONES == [(0.0, 0.1), (0.5, 1.0), (0.85, 1.0)]
    assert 0 < EMA_FAST < 1
    assert 0 < EMA_SLOW < EMA_FAST
