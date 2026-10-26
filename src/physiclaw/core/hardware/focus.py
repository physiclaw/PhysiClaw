"""Calibrate a fixed focus position for the fixed-distance camera/phone rig.

`camera_map._pin_focus` runs this lock on a checkerboard, then reads,
re-applies, and verifies the position before saving it. The checkerboard
provides enough texture for the sharpness gate; flatter calibration pages
can score below it even in focus (the corners page measured 14–34 against
the default gate of 80).

`Camera` restores the position at startup and reconnect. A failed apply
selects AF for that camera session, including later reconnects. Persistent
blur calls for checking the rig and recalibrating; there is no runtime
focus search.

Driver acceptance and sharp pixels are both required, but cannot prove
that firmware honored AF-off. Unreadable or soft frames fail the lock;
after any freeze attempt, failure requests AF again. Injected meter,
freeze, and unfreeze callables keep this logic testable without hardware.
"""

import logging
import math
from dataclasses import dataclass
from typing import Callable

# The pin gate deliberately rides the session gate: calibration proves
# the lens clears the same number every session judges views by, and an
# optics re-tune of `[vision] blur_threshold` moves both scenes
# together. The checkerboard's wide margin (hundreds vs 80) keeps that
# coupling safe.
from physiclaw.core.vision.quality import BLUR_THRESHOLD

log = logging.getLogger(__name__)

# Frames the caller's meter must let pass before each read — the AF
# loop iterates per frame, so consecutive meters separated by a few
# frames sample distinct lens positions instead of one.
SETTLE_FRAMES = 5

# Bounded settle loop: the scene must meter sharp on two CONSECUTIVE
# reads before the freeze — one sharp read can be the instant an AF
# hunt sweeps through focus. Sharpness clears BLUR_THRESHOLD with a
# wide margin on a focused phone-screen crop (300+ vs the 80 floor),
# so two agreeing reads reduce the risk of freezing during a hunt.
CONFIRM_METERS = 4


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
