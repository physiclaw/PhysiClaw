"""Calibrate a fixed focus position for the fixed-distance camera/phone rig.

`camera_map._pin_focus` searches the device's manual range on the checkerboard,
then reapplies the peak and verifies it against the measured best score.
Without a usable manual range, calibration leaves autofocus enabled.

`Camera` restores the position at startup and reconnect. A failed apply
selects AF for that camera session, including later reconnects. Persistent
blur calls for checking the rig and recalibrating; there is no runtime
focus search.

Driver acceptance and sharp pixels are both required, but cannot prove
that firmware honored AF-off. Missing frames or persistently soft verification
fail calibration and request AF again. Injected callbacks keep both search
and the diagnostic freeze helper testable without hardware.
"""

import logging
import math
import time
from dataclasses import dataclass
from typing import Callable

from physiclaw.core.vision.quality import BLUR_THRESHOLD

log = logging.getLogger(__name__)

# Fresh frames to wait between a lens write and sharpness measurement.
SETTLE_FRAMES = 5

# Bound settling after a focus change; require two consecutive sharp reads.
CONFIRM_METERS = 4

# Only calibration invokes this search, and only when the platform advertises
# a manual range. There is no runtime focus hunt on Windows, macOS, or Linux.
# Leave headroom for page changes and grid detection in the setup request.
SEARCH_SECONDS = 20.0
SEARCH_SAMPLES = 64
SEARCH_MIN_GAIN = 1.5


def search(
    bounds: tuple[int, int, int],
    meter: Callable[[], float | None],
    apply: Callable[[float], bool],
    unfreeze: Callable[[], None],
) -> float | None:
    """Find and verify a replayable MF position without trusting AF readback.

    Sample at most 17 coarse positions, then refine the best neighborhood in
    control-step units. All writes and meters must succeed. Two fresh readings
    of the final re-applied position must clear the gate and retain at least
    80% of the measured peak. A multi-position search rechecks the worst
    position to distinguish lens response from settling exposure. Failure
    restores AF and never saves a candidate.
    """
    low, high, step = bounds
    if low < 0 or high < low or step <= 0:
        return None  # caller has already selected AF
    last = (high - low) // step
    deadline = time.monotonic() + SEARCH_SECONDS
    scores: dict[int, float] = {}
    verified = False
    try:
        left, right = 0, last
        stride = max(1, math.ceil(last / 16))
        while True:
            points = sorted({*range(left, right + 1, stride), right})
            for point in points:
                if point in scores:
                    continue
                if len(scores) >= SEARCH_SAMPLES or time.monotonic() >= deadline:
                    return None
                if not apply(float(low + point * step)):
                    return None
                if time.monotonic() >= deadline:
                    return None
                score = meter()
                if score is None or not math.isfinite(score):
                    return None
                scores[point] = score
            best = max(scores, key=lambda point: scores[point])
            if stride == 1:
                break
            left, right = max(0, best - stride), min(last, best + stride)
            stride = max(1, stride // 4)
        peak = scores[best]
        if peak < BLUR_THRESHOLD or time.monotonic() >= deadline:
            return None

        def confirm(accept: Callable[[float], bool], consecutive: int) -> bool:
            agreed = 0
            for _ in range(CONFIRM_METERS):
                if time.monotonic() >= deadline:
                    return False
                score = meter()
                if score is None or not math.isfinite(score):
                    return False
                if time.monotonic() >= deadline:
                    return False
                agreed = agreed + 1 if accept(score) else 0
                if agreed == consecutive:
                    return True
            return False

        if len(scores) > 1:
            worst = min(scores, key=lambda point: scores[point])
            if peak < scores[worst] * SEARCH_MIN_GAIN:
                return None
            if not apply(float(low + worst * step)):
                return None
            if not confirm(lambda score: peak >= score * SEARCH_MIN_GAIN, 1):
                return None
        value = float(low + best * step)
        if not apply(value):
            return None
        if not confirm(lambda score: score >= max(BLUR_THRESHOLD, peak * 0.8), 2):
            return None
        verified = True
        log.info("Manual focus calibrated @ %g (peak sharpness %.0f)", value, peak)
        return value
    finally:
        if not verified:
            try:
                unfreeze()
            except Exception:
                log.warning("focus search rollback to autofocus failed", exc_info=True)


@dataclass(frozen=True)
class LockResult:
    """Outcome of one lock attempt — for the log, and for tests."""

    locked: bool  # True = driver accepted the freeze and the frame metered sharp
    detail: str  # human line for the lock log


def lock(
    meter: Callable[[], float | None],
    freeze: Callable[[], bool],
    unfreeze: Callable[[], None],
) -> LockResult:
    """Wait for two consecutive sharp readings, freeze, and verify once more.

    The caller must first request AF and clear any remembered pin. Settling
    is bounded by CONFIRM_METERS; a missing frame fails immediately. After
    attempting a freeze, any failure or interruption requests AF again.
    Recovery is best-effort when the driver is unavailable.
    """
    prev: float | None = None
    for _ in range(CONFIRM_METERS):
        cur = meter()
        if cur is None:
            return LockResult(False, "no frame — no focus lock attempted")
        if not math.isfinite(cur):
            return LockResult(False, "invalid sharpness — no focus lock attempted")
        if prev is not None and prev >= BLUR_THRESHOLD and cur >= BLUR_THRESHOLD:
            break
        prev = cur
    else:
        return LockResult(
            False,
            f"scene did not stay sharp (last {cur:.0f}, threshold "
            f"{BLUR_THRESHOLD:.0f}) — no converged focus to freeze",
        )
    locked = False
    try:
        if not freeze():
            return LockResult(
                False, "driver refused the focus lock — autofocus requested"
            )
        after = meter()
        if after is None or not math.isfinite(after) or after < BLUR_THRESHOLD:
            got = "no frame" if after is None else f"sharpness {after:.0f}"
            return LockResult(False, f"frozen frame soft ({got}) — autofocus requested")
        locked = True
        return LockResult(True, f"focus locked (sharpness {after:.0f})")
    finally:
        # A freeze can disable AF before its absolute-position write fails.
        # Restore it on refusal, failed verification, or interruption alike.
        if not locked:
            try:
                unfreeze()
            except Exception:
                log.warning("focus rollback to autofocus failed", exc_info=True)
