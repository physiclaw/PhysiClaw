"""Opt-in AF/MF and exposure stress against a physical camera.

Run with PHYSICLAW_LIVE_CAMERA_INDEX and PHYSICLAW_LIVE_CAMERA_STRESS=1.
The optional PHYSICLAW_LIVE_CAMERA_ROI is x1,y1,x2,y2 in normalized coordinates;
it confines quality measurements to the phone screen rather than the dark desk.
No calibration is saved. Initial focus/exposure modes are restored before close.
"""

import json
import os
import threading
from dataclasses import asdict

import cv2
import pytest

from physiclaw.common.config import CONFIG
from physiclaw.core.hardware import exposure, focus
from physiclaw.core.hardware.camera import Camera
from physiclaw.core.vision.quality import BLUR_THRESHOLD, assess

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _controls(camera):
    # Native getters share the driver's read/set lock, just like public setters.
    with camera._cap_lock:
        return {
            "autofocus": camera.cap.get(cv2.CAP_PROP_AUTOFOCUS),
            "focus": camera.cap.get(cv2.CAP_PROP_FOCUS),
            "auto_exposure": camera.cap.get(cv2.CAP_PROP_AUTO_EXPOSURE),
            "exposure": camera.cap.get(cv2.CAP_PROP_EXPOSURE),
        }


def _meter(camera, roi, frames=exposure.SETTLE_FRAMES):
    assert camera.wait_frames(frames, timeout=5), "capture stalled after control change"
    sample = camera.snapshot_with_time()
    assert sample is not None, "no fresh frame after control change"
    height, width = sample.frame.shape[:2]
    x1, y1, x2, y2 = roi
    crop = sample.frame[
        int(y1 * height) : int(y2 * height), int(x1 * width) : int(x2 * width)
    ]
    assert crop.size and crop.any(), "empty camera region"
    return assess(crop)


def _observe(camera, stop, stats):
    previous = float("-inf")
    try:
        while not stop.is_set():
            if not camera.wait_frames(1, timeout=3):
                raise AssertionError("concurrent reader stalled")
            sample = camera.snapshot_with_time()
            if sample is None:
                raise AssertionError("concurrent reader lost its fresh frame")
            if sample.published_at > previous:
                stats["fresh_frames"] += 1
                previous = sample.published_at
            stats["shape"] = list(sample.frame.shape)
    except BaseException as error:
        stats["error"] = f"{type(error).__name__}: {error}"


def _exercise_cycle(camera, roi, number):
    camera.set_manual_exposure(-6)
    camera.unlock_focus()
    af = _meter(camera, roi, frames=10)
    result = focus.lock(
        lambda: _meter(camera, roi, frames=focus.SETTLE_FRAMES).sharpness,
        camera.lock_focus,
        camera.unlock_focus,
    )
    assert result.locked, f"focus did not settle and lock: {result.detail}"
    value = camera.read_focus()
    assert value is not None, "camera does not expose a replayable focus position"
    accepted = camera.apply_focus(value)
    assert accepted and camera.focus_pinned, "manual focus apply refused"
    mf = _meter(camera, roi)
    assert mf.sharpness >= BLUR_THRESHOLD, "reapplied manual focus is blurry"
    modes = _controls(camera)
    readings = []
    for setting in (-8, -7, -6, -5, -4):
        camera.set_manual_exposure(setting)
        quality = _meter(camera, roi)
        controls = _controls(camera)
        readings.append({"requested": setting, **controls, **asdict(quality)})
        if os.name == "nt":
            assert controls["exposure"] == setting, "manual exposure readback differs"
    assert readings[-1]["p99_luma"] > readings[0]["p99_luma"] + 10, (
        "pixels did not brighten across four manual exposure stops"
    )
    assert all(
        brighter["p99_luma"] > darker["p99_luma"]
        for darker, brighter in zip(readings, readings[1:])
    ), (
        f"exposure measurements are stale or nonmonotonic: {[r['p99_luma'] for r in readings]}"
    )
    camera.set_auto_exposure()
    auto = _meter(camera, roi, frames=15)
    auto_controls = _controls(camera)
    camera.set_manual_exposure(-8)
    dark_start = _meter(camera, roi)
    camera.set_auto_exposure()
    auto_from_dark = _meter(camera, roi, frames=15)
    # DirectShow exposes no useful AE-mode readback on some cameras (-1).
    # Two starting extremes distinguish actual AE response from a no-op setter.
    assert (
        auto_from_dark.p99_luma > dark_start.p99_luma + 10
        or auto.p99_luma < readings[-1]["p99_luma"] - 10
    ), "auto exposure did not change pixels from either manual starting extreme"
    camera.unlock_focus()
    _meter(camera, roi)
    unlocked = _controls(camera)
    assert not camera.focus_pinned
    assert camera.health()
    # DirectShow reports CameraControlFlags: Auto=1, Manual=2.
    if os.name == "nt":
        assert modes["autofocus"] == 2
        assert unlocked["autofocus"] == 1
    return {
        "cycle": number,
        "focus_lock": asdict(result),
        "af_sharpness": af.sharpness,
        "mf_sharpness": mf.sharpness,
        "focus": value,
        "manual_controls": modes,
        "exposures": readings,
        "auto_quality": asdict(auto),
        "auto_from_dark": asdict(auto_from_dark),
        "dark_start": asdict(dark_start),
        "auto_controls": auto_controls,
        "unlocked": unlocked,
    }


def _stress(index, roi, output):
    report = {"index": index, "roi": roi, "cycles": [], "sessions": []}
    initial = None
    try:
        for session in range(2):
            camera = Camera(index=index)
            stop = threading.Event()
            stats = {"fresh_frames": 0}
            observer = threading.Thread(target=_observe, args=(camera, stop, stats))
            try:
                initial = initial or _controls(camera)
                observer.start()
                for cycle in range(10):
                    record = _exercise_cycle(camera, roi, session * 10 + cycle + 1)
                    report["cycles"].append(record)
                    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
                    print(
                        f"Camera controls: cycle {record['cycle']}/20 passed",
                        flush=True,
                    )
                camera.apply_focus(camera.read_focus())
                camera.set_manual_exposure(-8)
                tuned = exposure.converge(
                    lambda: _meter(camera, roi),
                    camera.set_auto_exposure,
                    camera.set_manual_exposure,
                    start=-8,
                    prefer_auto=False,
                    max_exposure=-4,
                )
                stats["convergence"] = asdict(tuned)
                assert tuned.ok, f"lit reference scene did not converge: {tuned.detail}"
            finally:
                stop.set()
                if observer.ident is not None:
                    observer.join(timeout=5)
                try:
                    if initial is not None:
                        if initial["autofocus"] == 1:
                            camera.unlock_focus()
                        else:
                            camera.apply_focus(initial["focus"])
                        if CONFIG.camera.auto_exposure:
                            camera.set_auto_exposure()
                        else:
                            camera.set_manual_exposure(int(initial["exposure"]))
                        _meter(camera, roi)
                        stats["restored"] = _controls(camera)
                finally:
                    camera.close()
                stats["observer_stopped"] = not observer.is_alive()
                stats["capture_closed"] = not camera.cap.isOpened()
                report["sessions"].append(stats)
            assert stats["observer_stopped"] and stats["capture_closed"]
            assert "error" not in stats, stats.get("error")
            assert stats["fresh_frames"] >= 50
        report["initial"] = initial
        report["ok"] = True
        return report
    finally:
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def test_live_af_mf_exposure_stress_with_concurrent_capture(tmp_path):
    if os.environ.get("PHYSICLAW_LIVE_CAMERA_STRESS") != "1":
        pytest.skip("set PHYSICLAW_LIVE_CAMERA_STRESS=1 to opt into control changes")
    value = os.environ.get("PHYSICLAW_LIVE_CAMERA_INDEX")
    if value is None:
        pytest.skip("set PHYSICLAW_LIVE_CAMERA_INDEX to choose a physical camera")
    roi = tuple(
        float(part)
        for part in os.environ.get("PHYSICLAW_LIVE_CAMERA_ROI", "0,0,1,1").split(",")
    )
    assert len(roi) == 4 and 0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1

    report = _stress(int(value), roi, tmp_path / "camera-controls-stress.json")

    assert report["ok"]
    assert len(report["cycles"]) == 20
    assert len(report["sessions"]) == 2
