"""Camera identification and preview — setup-flow algorithms.

Owns the "which USB index is the overhead camera?" logic: one-shot
previews for the wizard's manual pick, and the RGBM-corner auto-pick
that probes indices until it finds the camera that can actually see
the phone. Lives in orchestration (next to the rig that will own the
picked camera) because it composes hardware + vision — the hardware
package itself knows nothing about vision, and the HTTP layer
(`core/server/hardware.py`) stays a thin adapter over these functions.
"""

import logging
import time
from contextlib import closing, contextmanager
from typing import TYPE_CHECKING

import numpy as np

from physiclaw.common.config import CONFIG
from physiclaw.core.hardware.camera import Camera
from physiclaw.core.vision.grid_detect import detect_bridge_corners
from physiclaw.core.vision.render import watermark_index
from physiclaw.core.vision.util import encode_jpeg

if TYPE_CHECKING:
    from physiclaw.core.bridge import PageState
    from physiclaw.core.orchestration.rig import HardwareRig

log = logging.getLogger(__name__)

# Timeouts for the auto-pick wait-for-bridge gate (mirrors the
# warm-start pattern; see core/server/warm_start.py). Values come from
# `CONFIG.auto_pick.bridge_*` so operators can tune them via
# `physiclaw config edit`.
AUTO_PICK_BRIDGE_TIMEOUT = CONFIG.auto_pick.bridge_wait_timeout_seconds
AUTO_PICK_BRIDGE_SETTLE = CONFIG.auto_pick.bridge_settle_seconds


@contextmanager
def _camera_for(index: int, connected: Camera | None):
    """Borrow the rig's matching handle; close only handles opened here."""
    if connected is not None and connected.index == index:
        yield connected
    else:
        with closing(Camera(index)) as camera:
            yield camera


def camera_preview(
    index: int, watermark: bool = False, *, connected: Camera | None = None
) -> bytes:
    """Capture one frame from a camera, optionally watermark the index.

    Returns an unrotated frame as JPEG. Reuses a matching ``connected``
    camera (caller holds the rig lock); otherwise opens and closes its own.
    Used by /api/camera-preview/{index} during /setup so the user can pick
    the right camera index by previewing each one without committing to a
    connection.

    Raises RuntimeError if the camera can't be opened or returns no frame.
    """
    with _camera_for(index, connected) as cam:
        frame = cam.raw_frame()
    if frame is None:
        raise RuntimeError(f"Camera {index} returned no frame")

    if watermark:
        frame = watermark_index(frame, index)
    return encode_jpeg(frame, quality=80)


def _capture_raw(idx: int, connected: Camera | None = None) -> np.ndarray | None:
    """Open camera ``idx``, return one raw unrotated frame or None.

    Logs the reason on failure so a silent None doesn't mask a real issue.
    """
    try:
        # Constructor inside the try: a missing index raises here, and
        # the auto-pick loop must move on to the next index, not crash.
        with _camera_for(idx, connected) as cam:
            return cam.raw_frame()
    except (OSError, RuntimeError) as e:
        log.warning(f"  cam {idx}: capture failed — {e}")
        return None


def _auto_pick_camera_index(connected: Camera | None = None) -> int | None:
    """Identify the overhead camera by the RGBM corner markers on /bridge.

    Caller must first put the phone page into the ``corners`` phase so
    bridge.html draws the four colored squares. We then iterate USB
    indices 0..7 and pick the camera whose frame contains all four
    markers arranged clockwise — only the camera actually pointing at
    the phone can possibly see them, so the match is unambiguous.
    """
    # macOS Continuity Camera + iPhone Webcam can occupy low indices,
    # pushing the actual USB camera to 4+. Probing 0..7 covers the
    # typical configurations without measurably slowing the loop —
    # missing-index opens fail fast on macOS.
    for idx in range(8):
        frame = _capture_raw(idx, connected)
        if frame is None:
            continue
        corners = detect_bridge_corners(frame)
        if corners is None:
            log.info(f"  cam {idx}: corners not detected")
            continue
        log.info(f"Auto-picked camera {idx} — all four RGBM corners detected")
        return idx
    return None


def resolve_auto_index(rig: "HardwareRig", phone: "PageState") -> int:
    """Run the auto-pick flow and return the found index, or raise.

    Waits for the phone /bridge tab to be actively polling before
    flipping to the corners phase — if the screen is asleep or the tab
    is backgrounded, set_mode would update server state but the canvas
    would never paint the RGBM markers, and auto-pick would always
    fail. (warm-start uses the same gate.) The phone is restored to
    bridge mode before returning, even on failure. Caller holds the rig lock
    so an existing camera can be borrowed without a duplicate device open.
    """
    rig.assert_locked()
    bridge = rig.require_bridge()
    if not bridge.wait_for_connection(
        AUTO_PICK_BRIDGE_TIMEOUT, AUTO_PICK_BRIDGE_SETTLE
    ):
        raise RuntimeError(
            f"auto-pick: /bridge page not polling within "
            f"{AUTO_PICK_BRIDGE_TIMEOUT}s — open or refresh /bridge "
            f"on the phone, then retry."
        )
    phone.set_mode("calibrate", phase="corners")
    try:
        time.sleep(0.5)  # give bridge.html time to render the corners
        picked = _auto_pick_camera_index(rig.cam)
    finally:
        phone.set_mode("bridge")
    if picked is None:
        raise RuntimeError(
            "auto-pick found no camera with all four RGBM corners; "
            "is /bridge open on the phone? Pass an explicit index to fall back."
        )
    return picked
