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
from collections.abc import Callable
from dataclasses import dataclass

from physiclaw.common.logger.evidence import record_event
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
# Full-range moves need longer settling than search increments (UGREEN 4K).
# Verification still requires two sharp readings within SEARCH_SECONDS.
SEARCH_CONFIRM_METERS = 8
SEARCH_MIN_PEAK_RATIO = 0.8
SEARCH_REQUIRED_CONSECUTIVE = 2


def search(
    bounds: tuple[int, int, int],
    meter: Callable[[], float | None],
    apply: Callable[[float], bool],
    unfreeze: Callable[[], None],
) -> float | None:
    """Find and verify a replayable MF position without trusting AF readback.

    Sample at most 17 coarse positions, then refine the best neighborhood in
    control-step units, remeasuring it at each pass so exposure drift cannot
    preserve an obsolete peak. All writes and meters must succeed. Two fresh
    readings of the final re-applied position must clear the gate and retain
    at least 80% of the measured peak. A multi-position search rechecks the worst
    position to distinguish lens response from settling exposure. Failure
    restores AF and never saves a candidate.
    """
    low, high, step = bounds
    thresholds = {
        "minimum_sharpness": BLUR_THRESHOLD,
        "minimum_lens_response_gain": SEARCH_MIN_GAIN,
        "minimum_peak_ratio": SEARCH_MIN_PEAK_RATIO,
        "required_consecutive": SEARCH_REQUIRED_CONSECUTIVE,
        "maximum_confirmation_reads": SEARCH_CONFIRM_METERS,
    }
    record_event(
        "camera.focus_search_started",
        bounds=bounds,
        position_units="device_control_units",
        thresholds=thresholds,
        timeout_seconds=SEARCH_SECONDS,
        maximum_search_samples=SEARCH_SAMPLES,
    )
    if low < 0 or high < low or step <= 0:
        record_event(
            "camera.focus_search_result",
            summarize=True,
            status="failed",
            reason="invalid_focus_bounds",
            bounds=bounds,
        )
        return None  # caller has already selected AF
    last = (high - low) // step
    started = time.monotonic()
    deadline = started + SEARCH_SECONDS
    scores: dict[int, float] = {}
    observations: dict[int, dict] = {}
    peak_sample: dict | None = None
    worst_sample: dict | None = None
    last_sample: dict | None = None
    verification_samples: list[dict] = []
    response_samples: list[dict] = []
    samples = 0
    reads = 0
    search_pass = 0
    phase = "coarse"
    position: float | None = None
    reason = "exception"
    error: dict = {}
    verified = False

    def read() -> float | None:
        nonlocal reads, last_sample, reason
        score = meter()
        reads += 1
        sample = {
            "sample": reads,
            "phase": phase,
            "search_pass": search_pass,
            "position": position,
            "sharpness": score,
        }
        reference = record_event(
            "camera.focus_sample",
            **sample,
            **({"frame_id": None, "input_artifact_id": None} if score is None else {}),
        )
        last_sample = {**sample, **(reference or {})}
        if score is None:
            reason = "measurement_unavailable"
        elif not math.isfinite(score):
            reason = "invalid_sharpness"
        else:
            return score
        return None

    try:
        left, right = 0, last
        stride = max(1, math.ceil(last / 16))
        while True:
            search_pass += 1
            phase = "coarse" if search_pass == 1 else "refinement"
            points = sorted({*range(left, right + 1, stride), right})
            for point in points:
                if samples >= SEARCH_SAMPLES:
                    reason = "sample_budget_exhausted"
                    return None
                if time.monotonic() >= deadline:
                    reason = "deadline_exceeded"
                    return None
                position = float(low + point * step)
                if not apply(position):
                    reason = "driver_rejected_focus"
                    return None
                if time.monotonic() >= deadline:
                    reason = "deadline_exceeded"
                    return None
                score = read()
                if score is None:
                    return None
                scores[point] = score
                observations[point] = last_sample or {}
                samples += 1
            # Compare this pass only: earlier exposure may have been brighter.
            best = max(points, key=lambda point: scores[point])
            peak_sample = observations[best]
            record_event(
                "camera.focus_pass",
                phase=phase,
                search_pass=search_pass,
                stride=step * stride,
                compared_samples=[observations[point] for point in points],
                peak_sample=peak_sample,
                comparison="current_pass_only",
            )
            if stride == 1:
                break
            left, right = max(0, best - stride), min(last, best + stride)
            stride = max(1, stride // 4)
        peak = scores[best]
        if peak < BLUR_THRESHOLD:
            reason = "peak_below_minimum_sharpness"
            return None
        if time.monotonic() >= deadline:
            reason = "deadline_exceeded"
            return None
        thresholds["verification_minimum_sharpness"] = max(
            BLUR_THRESHOLD, peak * SEARCH_MIN_PEAK_RATIO
        )
        thresholds["response_maximum_sharpness"] = peak / SEARCH_MIN_GAIN

        def confirm(accept: Callable[[float], bool], consecutive: int) -> bool:
            nonlocal reason
            checks = (
                response_samples if phase == "lens_response" else verification_samples
            )
            agreed = 0
            for _ in range(SEARCH_CONFIRM_METERS):
                if time.monotonic() >= deadline:
                    reason = "deadline_exceeded"
                    return False
                score = read()
                if score is None:
                    return False
                if time.monotonic() >= deadline:
                    reason = "deadline_exceeded"
                    return False
                accepted = accept(score)
                agreed = agreed + 1 if accepted else 0
                check = {
                    **(last_sample or {}),
                    "accepted": accepted,
                    "consecutive": agreed,
                }
                checks.append(check)
                record_event(
                    "camera.focus_check",
                    sample=check,
                    required_consecutive=consecutive,
                )
                if agreed == consecutive:
                    return True
            reason = (
                "lens_response_not_reproduced"
                if phase == "lens_response"
                else "verification_not_stable"
            )
            return False

        if len(scores) > 1:
            phase = "lens_response"
            worst = min(scores, key=lambda point: scores[point])
            worst_sample = observations[worst]
            if peak < scores[worst] * SEARCH_MIN_GAIN:
                reason = "insufficient_lens_response"
                return None
            position = float(low + worst * step)
            if not apply(position):
                reason = "driver_rejected_focus"
                return None
            if not confirm(lambda score: peak >= score * SEARCH_MIN_GAIN, 1):
                return None
        phase = "verification"
        value = float(low + best * step)
        position = value
        if not apply(value):
            reason = "driver_rejected_focus"
            return None
        if not confirm(
            lambda score: score >= thresholds["verification_minimum_sharpness"],
            SEARCH_REQUIRED_CONSECUTIVE,
        ):
            return None
        verified = True
        reason = "verified_peak"
        log.info("Manual focus calibrated @ %g (peak sharpness %.0f)", value, peak)
        return value
    except BaseException as exc:
        error = {"exception_type": type(exc).__name__, "message": str(exc)}
        reason = "exception" if isinstance(exc, Exception) else "interrupted"
        raise
    finally:
        rollback: dict = {"requested": not verified}
        if not verified:
            try:
                unfreeze()
                rollback["call_succeeded"] = True
            except Exception as exc:
                rollback.update(call_succeeded=False, message=str(exc))
                log.warning("focus search rollback to autofocus failed", exc_info=True)
        record_event(
            "camera.focus_search_result",
            summarize=True,
            status="succeeded" if verified else "failed",
            reason=reason,
            bounds=bounds,
            position_units="device_control_units",
            timeout_seconds=SEARCH_SECONDS,
            maximum_search_samples=SEARCH_SAMPLES,
            phase=phase,
            attempted_position=position,
            selected_position=position if verified else None,
            peak_sample=peak_sample,
            worst_sample=worst_sample,
            last_sample=last_sample,
            response_samples=response_samples,
            verification_samples=verification_samples,
            thresholds=thresholds,
            search_samples=samples,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            autofocus_restore=rollback,
            **error,
        )


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
