"""Screen pixels and physical gestures for a single calibrated rig."""

import math
import time
from collections.abc import Callable

from physiclaw.common.config import CONFIG
from physiclaw.core.orchestration import gestures
from physiclaw.core.orchestration.orchestrator import PhysiClaw
from physiclaw.core.orchestration.rig import TOOL_LOCK_WAIT_SECONDS
from physiclaw.core.vision.rectify import rectify_screen
from physiclaw.core.vision.util import encode_jpeg

# Budget complete repetitions, including positioning, contact and release.
SWIPE_BATCH_SECONDS = 30.0
SWIPE_SETUP_ALLOWANCE_SECONDS = 1.0


class MidsceneDevice:
    """All public operations are serialized by the protocol handler."""

    def __init__(self, pl: PhysiClaw):
        self.pl = pl
        self.rig = pl.rig
        self.after_action = 0.0
        self.last_frame_geometry: tuple | None = None

    def _size(self) -> tuple[int, int]:
        if not self.rig.ready:
            raise RuntimeError("Complete PhysiClaw hardware setup first")
        cal = self.rig.calibration
        shift = cal.viewport_shift
        if shift is None or self.rig.require_cam().rotation != cal.cam_rotation:
            raise RuntimeError("Screen calibration is unavailable or has changed")
        w, h = shift.screenshot_width, shift.screenshot_height
        scale = min(1.0, CONFIG.compact.max_image_edge_px / max(w, h))
        return max(1, round(w * scale)), max(1, round(h * scale))

    def _geometry(self, size):
        transforms = self.rig.require_transforms()
        return (
            size,
            self.rig.calibration.cam_rotation,
            transforms.cam_size,
            transforms.pct_to_cam.tobytes(),
            transforms.pct_to_grbl.tobytes(),
        )

    def size(self) -> tuple[int, int]:
        with self.rig.locked(wait_seconds=TOOL_LOCK_WAIT_SECONDS):
            return self._size()

    def screenshot(self) -> tuple[bytes, int, int]:
        with self.rig.locked(wait_seconds=TOOL_LOCK_WAIT_SECONDS):
            size = self._size()
            cam = self.rig.require_cam()
            deadline = time.monotonic() + 2
            while True:
                sample = cam.snapshot_with_time()
                if sample is not None and sample.published_at > self.after_action:
                    break
                if sample is None or time.monotonic() >= deadline:
                    raise RuntimeError("Fresh camera frame unavailable")
                time.sleep(0.02)
            transforms = self.rig.require_transforms()
            try:
                screen = rectify_screen(
                    sample.frame,
                    transforms.pct_to_cam,
                    size,
                    camera_size=transforms.cam_size,
                )
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
            jpeg = encode_jpeg(screen)
            self.last_frame_geometry = self._geometry(size)
            return jpeg, *size

    def act(self, name: str, body: dict) -> str | None:
        if not self.rig.ready:
            raise RuntimeError("Complete PhysiClaw hardware setup first")
        steps: dict[str, tuple[gestures.Gesture, ...]] = {
            "homeButton": gestures.HOME_SCREEN,
            "backButton": gestures.GO_BACK,
            "recentAppsButton": (gestures.RECENT_APPS,),
        }
        if name in steps or name in {"forceQuit", "unlockPhone", "sendToClipboard"}:
            try:
                if name == "sendToClipboard":
                    return self.pl.send_to_clipboard(body["text"], require_ready=True)
                if name == "unlockPhone":
                    return self.pl.execute_unlock(require_ready=True)
                if name == "forceQuit":
                    self.pl.execute_steps(gestures.FORCE_QUIT, require_ready=True)
                    return "Force-quit current app"
                self.pl.execute_steps(steps[name], require_ready=True)
                return None
            finally:
                self.after_action = time.time()

        with self.rig.locked(wait_seconds=TOOL_LOCK_WAIT_SECONDS):
            self.rig.require_hardware()
            move = self._prepare_touch(name, body)
            try:
                with self.rig.park_after_motion():
                    move()
            finally:
                self.after_action = time.time()
        return None

    def _prepare_touch(self, name: str, body: dict) -> Callable[[], None]:
        """Validate against locked geometry before arming motion cleanup."""
        w, h = self._size()
        if (
            self.last_frame_geometry is not None
            and self.last_frame_geometry != self._geometry((w, h))
        ):
            raise RuntimeError("Calibration changed; request a new screenshot")

        def normalized(point):
            x, y = point["x"], point["y"]
            if not (0 <= x < w and 0 <= y < h):
                raise ValueError("Point outside the screen")
            return x / w, y / h

        duration = body.get("duration")
        seconds = None if duration is None else duration / 1000
        arm = self.rig.require_arm()
        guard = self.rig.assistive_touch
        if "point" in body:
            x, y = normalized(body["point"])
            if guard.overlaps_at(x, y):
                raise ValueError("Point overlaps AssistiveTouch")

            def press():
                self.rig.move_to_point(x, y)
                if name == "doubleClick":
                    arm.double_tap()
                elif name == "longPress":
                    arm.long_press(seconds)
                else:
                    arm.tap(seconds)

            return press

        start, end = normalized(body["from"]), normalized(body["to"])
        if guard.swipe_crosses_at(*start, *end):
            raise ValueError("Path crosses AssistiveTouch")
        convert = self.rig.require_transforms().pct_to_grbl_mm
        target = convert(*end)
        feed = None
        distance = math.dist(convert(*start), target)
        if not distance:
            raise ValueError("Path endpoints must differ")
        if seconds is not None:
            fastest = arm.SWIPE_SPEEDS["fast"]
            feed = (
                fastest
                if seconds == 0 or distance * 60 >= fastest * seconds
                else max(1, min(fastest, round(distance * 60 / seconds)))
            )
        dwell = arm.LONG_PRESS_DURATION if name == "dragAndDrop" else 0.0
        repeat = body.get("repeat", 1)
        per_swipe = 0.0
        if repeat > 1:
            slide_seconds = 60 * distance / (feed or arm.SWIPE_SPEEDS["medium"])
            # Allow for the return to the start, plus contact/release and settling.
            per_swipe = (
                max(slide_seconds, seconds or 0)
                + 60 * distance / arm.RAPID_SPEED
                + SWIPE_SETUP_ALLOWANCE_SECONDS
            )
            if repeat * per_swipe > SWIPE_BATCH_SECONDS:
                raise ValueError(
                    "Repeated swipe exceeds the 30-second budget; reduce repeat or duration"
                )

        def swipe():
            deadline = time.monotonic() + SWIPE_BATCH_SECONDS
            for index in range(repeat):
                if index and time.monotonic() + per_swipe > deadline:
                    raise RuntimeError(
                        f"Swipe budget reached after {index} of {repeat} repetitions"
                    )
                self.rig.move_to_point(*start)
                arm.swipe_to(*target, start_dwell=dwell, feed_mm_min=feed)

        return swipe
