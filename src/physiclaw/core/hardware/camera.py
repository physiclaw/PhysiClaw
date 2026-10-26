"""
Camera capture, negotiation, and remembered device state.

Usage as library:
    from physiclaw.core.hardware.camera import Camera
    cam = Camera(index=0)
    frame = cam.snapshot()
    cam.close()

Note: On macOS, OpenCV won't trigger the camera permission dialog.
If the camera returns blank frames, run `imagesnap` once first to
grant camera access to your terminal app, then retry.
"""

import contextlib
import logging
import os
import sys
import threading
from collections.abc import Iterator

import cv2
import numpy as np

from physiclaw.common import platform
from physiclaw.common.config import CONFIG
from physiclaw.common.dumps import save_raw_camera, save_snapshot
from physiclaw.core.hardware.device import DeviceNotFound, DeviceTimeout
from physiclaw.core.hardware.frame_reader import FrameReader, PublishedFrame

log = logging.getLogger(__name__)

# Serializes the fd-2 redirect in `silenced_stderr` across threads (camera
# reopen vs a concurrent preview/auto-pick open) — see its docstring.
_STDERR_REDIRECT_LOCK = threading.Lock()


@contextlib.contextmanager
def silenced_stderr() -> Iterator[None]:
    """Redirect OS-level stderr (fd 2) to /dev/null for the block.

    OpenCV/AVFoundation/ffmpeg print things like ``out device of bound``
    and ``camera failed to properly initialize`` via C-level fprintf,
    bypassing Python's logging — only an fd-level redirect catches them.
    Wrap any code that probes or opens a ``cv2.VideoCapture`` whose index
    might not exist.

    Process-global: fd 2 is shared, so any other thread logging to
    stderr inside the ``with`` block drops to /dev/null too. Keep the
    wrapped region short. Serialized by ``_STDERR_REDIRECT_LOCK`` —
    without it, two threads redirecting fd 2 concurrently (a camera
    reopen racing a preview/auto-pick open) can save each other's
    /dev/null as the "real" stderr and leave it permanently silenced.
    """
    with _STDERR_REDIRECT_LOCK:
        sys.stderr.flush()
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            saved = os.dup(2)
            try:
                os.dup2(devnull, 2)
                yield
            finally:
                os.dup2(saved, 2)
                os.close(saved)
        finally:
            os.close(devnull)


def apply_size_cap(size: tuple[int, int]) -> tuple[int, int]:
    """Clamp a capture request to the platform's safe maximum, if any.

    On macOS without a UVC exposure channel, high-res modes risk a
    firmware AE that overexposes with no way to correct it, so requests
    drop to the largest mode whose AE behaves. Called both where
    `Camera._request_size` is seeded (so remembered state and warmup
    logs stay honest) and inside `configure_capture` (the choke point,
    so the fallback ladder and reconnects can't sneak past it)."""
    size_cap = platform.camera_size_cap()
    if size_cap is None or size[0] * size[1] <= size_cap[0] * size_cap[1]:
        return size
    log.debug(
        "capture request %dx%d capped to %dx%d — exposure is not "
        "tunable on this rig and higher modes risk uncorrectable "
        "overexposure",
        *size,
        *size_cap,
    )
    return size_cap


def configure_capture(
    cap: cv2.VideoCapture,
    *,
    exposure_auto: bool,
    exposure: int,
    size: tuple[int, int] | None = None,
    focus_value: float | None = None,
) -> bool:
    """Configure capture size, exposure, and focus; return focus acceptance.

    Set FOURCC before dimensions: format changes can renegotiate the stream,
    and MJPG avoids uncompressed USB bandwidth limits. Apply exposure and
    focus afterward because negotiation can change device controls.

    ``size`` overrides CONFIG for warmup's resolution fallback ladder;
    `apply_size_cap` enforces platform limits. ``focus_value`` restores the
    saved position, or None requests AF explicitly: reopening does not
    reset the camera's previous manual-focus mode.

    True means the driver accepted the focus apply. False means no saved
    position or a refused apply. This is neither optical verification nor
    overall configuration success; callers check the resulting frames.

    Used for camera startup, reconnect, resolution fallback, and the
    doctor's deep camera probe.
    """
    width, height = apply_size_cap(
        size
        if size is not None
        else (
            CONFIG.camera.width,
            CONFIG.camera.height,
        )
    )
    # AVFoundation (macOS) ignores this; V4L (Linux) honors it.
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    # VideoWriter.fourcc is the stub-visible spelling of VideoWriter_fourcc
    # (same function at runtime since OpenCV 3).
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*CONFIG.camera.fourcc))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    if exposure_auto:
        platform.camera_set_auto_exposure(cap)
    else:
        platform.camera_set_manual_exposure(cap, exposure)
    # Apply focus after negotiation. Exposure tuning needs this attempt's
    # result, not merely the existence of a saved position.
    if focus_value is None:
        platform.camera_unlock_focus(cap)
        return False
    if not _apply_focus(cap, focus_value):
        log.warning(
            "could not apply the remembered focus position — autofocus requested",
            extra={
                "console_message": "Saved camera focus could not be restored — autofocus requested"
            },
        )
        return False
    return True


def _apply_focus(cap: cv2.VideoCapture, value: float) -> bool:
    """Apply a position, requesting AF on refusal or interruption.

    Both capture setup and live changes use this rollback: either write
    can fail after AF was disabled. Keep the original error if cleanup
    also fails; a refused rollback cannot establish the physical mode.
    """
    applied = False
    try:
        applied = platform.camera_apply_focus(cap, value)
        return applied
    finally:
        if not applied:
            try:
                platform.camera_unlock_focus(cap)
            except Exception:
                log.warning(
                    "focus rollback to autofocus failed",
                    exc_info=True,
                    extra={
                        "console_message": "Camera could not re-enable autofocus — lens state is unknown"
                    },
                )


# Fallback requests when a high-resolution negotiation misbehaves.
# Linux V4L2 guarantees nearest-match adjustment (VIDIOC_S_FMT rounds
# down and reports back) and macOS AVFoundation snaps to the closest
# supported mode, but Windows MSMF can fail stream selection outright
# or fall back to 640×480 when asked for a mode the camera lacks
# (opencv/opencv#25243, #12822). `Camera._warmup` steps down this
# ladder until a frame arrives at ≥ BASELINE_LONG_EDGE; the last rung
# equals the pre-4K-default fixed request, so no camera negotiates
# worse than it did before the default moved.
RESOLUTION_FALLBACKS = [(2560, 1440), (1920, 1080)]
BASELINE_LONG_EDGE = 1920


# ─── Reusable Camera class ──────────────────────────────────────


class Camera:
    """Persistent camera handle for fast repeated frame grabs.

    A composed :class:`FrameReader` continuously calls ``cap.read()`` on
    a background daemon thread so the macOS AVFoundation pipeline never
    goes idle (cv2 stalls indefinitely on the next read after tens of
    seconds of inactivity — reproduced on the bench rig; no matching
    issue exists on OpenCV's tracker). Callers get the latest frame via
    ``peek()`` / ``snapshot()`` / ``_fresh_frame()`` without blocking on
    cv2. The reader's stale-reconnect / fatal-drought supervisor policy
    lives in ``frame_reader.py``; this class owns the cv2 handle, the
    negotiation ladder, and the remembered device state.

    Holds the software rotation code applied to raw frames. Default is
    ``-1`` (no rotation) — calibration step 3 (`pick_frame_rotation`)
    writes the detected ``cv2.ROTATE_*`` code via ``cam.rotation = code``.
    Callers that need a rotated frame should always use
    ``peek()``/``snapshot()`` rather than calling ``cv2.rotate`` themselves.
    """

    # close(): how long to wait for the cap lock after stopping the
    # reader. A reader wedged inside a blocking native cap.read() (USB
    # bus suspend) can hold the lock indefinitely; close() must not hang
    # the caller on it.
    CLOSE_LOCK_TIMEOUT_SECONDS = 2.0

    # Reconnect must produce a real frame, but must not run startup's
    # long warmup/permission loop while the reader's supervisor is paused.
    RECONNECT_FRAME_ATTEMPTS = 3

    def __init__(self, index: int = 0, focus_value: float | None = None) -> None:
        self.index = index
        self.rotation: int = -1  # no rotation until calibration step 3 sets it
        # cv2.VideoCapture is not thread-safe for concurrent read()+set():
        # this lock serializes the reader's cap.read() against exposure
        # setters, _reopen, and close. Non-reentrant, never nested; _open
        # does NOT take it (callers hold it, or run before the thread).
        self._cap_lock = threading.Lock()
        # A close request forbids reconnect even if a native read outlives
        # the join budget. A later close may still release a wedged handle.
        self._closing = False
        self._closed = False
        # Exposure state remembered on the instance so _reopen() re-applies
        # a converged manual value instead of silently reverting to auto.
        self._exposure_auto: bool = CONFIG.camera.auto_exposure
        self._exposure_value: int = CONFIG.camera.exposure
        # Requested absolute lens position, seeded from calibration.
        # None requests AF; a failed apply clears the value so reconnect
        # keeps that fallback instead of retrying the pin.
        self._focus_value: float | None = focus_value
        # Raw calibration freezes and partial writes also need cleanup,
        # even when no replayable position has been remembered yet.
        self._focus_needs_restore = focus_value is not None
        # Whether the current capture accepted the pin. A failed apply
        # clears the remembered value too, selecting AF for this session.
        self._focus_applied: bool = False
        # The resolution request, remembered so _reopen() re-applies the
        # rung _warmup settled on instead of re-failing at the top of the
        # ladder on every reconnect.
        self._request_size: tuple[int, int] = apply_size_cap(
            (CONFIG.camera.width, CONFIG.camera.height)
        )
        self._reader = FrameReader(
            read_frame=self._locked_read,
            reopen=self._reopen,
            label=f"Camera-{index}",
        )

        self._open()
        try:
            self._warmup()
            self._reader.start()
        except BaseException:
            # Failed warmup/startup, including cancellation, must release
            # safely even if Thread.start began the pump before raising.
            # _open owns cleanup if acquisition itself fails.
            with contextlib.suppress(Exception):
                self.close()
            raise

    def _locked_read(self):
        """One serialized ``cap.read()`` — the reader's injected pump."""
        with self._cap_lock:
            self._require_open()
            return self.cap.read()

    def _require_open(self) -> None:
        """Reject queued operations after close was requested; cap lock held."""
        if self._closing:
            raise DeviceNotFound(f"Camera {self.index} is closing or closed")

    def health(self) -> bool:
        """Live check: the reader loop is running and frames are recent
        (see the Device protocol in ``device.py``)."""
        return self._reader.healthy()

    # ─── cv2 lifecycle ──────────────────────────────────────────

    def _open(self) -> None:
        """Open the underlying ``cv2.VideoCapture``. Retries with macOS perm
        prompt. Caller must hold ``_cap_lock`` or run before the reader
        thread starts (``__init__``/``_warmup``)."""
        backend = platform.camera_backend()
        self._focus_applied = False
        cap = None
        try:
            with silenced_stderr():
                cap = cv2.VideoCapture(self.index, backend)
                if not cap.isOpened():
                    cap.release()
                    cap = None
                    platform.ensure_camera_permission()
                    cap = cv2.VideoCapture(self.index, backend)
            if not cap.isOpened():
                raise DeviceNotFound(f"Cannot open camera index {self.index}")
            # Publish only a configured handle. Failed opens leave no live
            # candidate behind and never retain a previous pin verdict.
            self._configure(cap)
        except BaseException:
            if cap is not None:
                with contextlib.suppress(Exception):
                    cap.release()
            raise
        self.cap = cap

    def _configure(self, cap: cv2.VideoCapture) -> None:
        """Apply remembered settings; a failed focus restore selects AF."""
        self._focus_applied = False
        if self._focus_value is not None:
            self._focus_needs_restore = True
        try:
            self._focus_applied = configure_capture(
                cap,
                exposure_auto=self._exposure_auto,
                exposure=self._exposure_value,
                size=self._request_size,
                focus_value=self._focus_value,
            )
        finally:
            if not self._focus_applied:
                self._focus_value = None

    def _request_ladder(self) -> list[tuple[int, int]]:
        """The configured request first, then each strictly-smaller fallback."""
        first = self._request_size
        return [first] + [
            s for s in RESOLUTION_FALLBACKS if s[0] * s[1] < first[0] * first[1]
        ]

    def _acquire_first_frame(self) -> np.ndarray | None:
        """Discard initial auto-exposure frames and read one; on failure
        release + perm-prompt + reopen once. Returns the frame or None."""
        for _ in range(2):
            for _ in range(15):
                self.cap.read()
            ret, frame = self.cap.read()
            if ret and frame is not None:
                return frame
            # Read returned no frame — likely macOS perm denied silently.
            self.cap.release()
            platform.ensure_camera_permission()
            self._open()
        return None

    def _warmup(self) -> None:
        """Verify reads work, stepping the resolution request down when
        the negotiation misbehaves.

        A frame smaller than BASELINE_LONG_EDGE while a larger mode was
        requested — or no frame at all — means the driver mishandled the
        over-ask (Windows MSMF; see RESOLUTION_FALLBACKS). Renegotiate
        down the ladder; the last rung accepts whatever arrives, which
        matches the behavior of the old fixed 1920×1080 request.
        """
        ladder = self._request_ladder()
        for rung, size in enumerate(ladder):
            if size != self._request_size:
                log.debug(
                    f"Camera {self.index}: negotiation misbehaved — "
                    f"retrying at {size[0]}x{size[1]}"
                )
                self._request_size = size
                self._configure(self.cap)
            frame = self._acquire_first_frame()
            if frame is None:
                continue
            h, w = frame.shape[:2]
            if max(w, h) < BASELINE_LONG_EDGE and rung < len(ladder) - 1:
                log.debug(
                    f"Camera {self.index}: {size[0]}x{size[1]} request "
                    f"negotiated only {w}x{h}"
                )
                continue
            log.info(
                "Camera %s ready | capture=%dx%d | requested=%dx%d",
                self.index,
                w,
                h,
                *ladder[0],
            )
            self._reader.publish(frame)
            return
        raise DeviceTimeout(f"Camera {self.index}: read failed")

    def _reopen(self) -> None:
        """Rebuild capture and controls, then verify frame delivery resumes.

        Keep the negotiated resolution and remembered exposure/focus.
        An open handle alone does not mean its stream is delivering frames.
        """
        log.debug("Camera %s: reopening capture", self.index)
        with self._cap_lock:
            if self._closing:
                return
            with contextlib.suppress(Exception):
                self.cap.release()
            self._reader.note_reopened()
            try:
                platform.camera_reset_controls()
                self._open()
                for _ in range(self.RECONNECT_FRAME_ATTEMPTS):
                    ok, frame = self.cap.read()
                    if self._closing:
                        return
                    if ok and frame is not None:
                        self._reader.publish(frame)
                        log.debug("Camera %s: capture recovered", self.index)
                        return
                raise DeviceTimeout("reopened camera produced no frames")
            except Exception as e:
                with contextlib.suppress(Exception):
                    self.cap.release()
                self._reader.note_reopened()
                log.debug("Camera %s: reopen failed: %r", self.index, e, exc_info=True)

    # ─── Exposure control ───────────────────────────────────────

    @property
    def exposure_tunable(self) -> bool:
        """Whether exposure can be controlled on this rig. Always true on
        Linux/Windows (V4L2/DirectShow); on macOS true only when the vendored
        UVC helper compiled and unambiguously controls the camera —
        AVFoundation itself ignores exposure properties."""
        return platform.camera_exposure_tunable()

    def set_auto_exposure(self) -> None:
        """Hand exposure back to the camera firmware. Remembered, so a
        later `_reopen` keeps the choice."""
        with self._cap_lock:
            self._require_open()
            self._exposure_auto = True
            platform.camera_set_auto_exposure(self.cap)

    def set_manual_exposure(self, value: int) -> None:
        """Hold a fixed exposure (log2 seconds). Remembered, so a
        later `_reopen` re-applies it. Verification is the caller's job —
        measured frame brightness, never the driver's word."""
        with self._cap_lock:
            self._require_open()
            self._exposure_auto = False
            self._exposure_value = value
            platform.camera_set_manual_exposure(self.cap, value)

    # ─── Focus control ──────────────────────────────────────────

    @property
    def focus_lockable(self) -> bool:
        """Whether focus locking can be attempted.

        Linux/Windows expose the AF toggle; macOS needs a live UVC channel
        answering focus controls. Acceptance and sharpness are checked later.
        """
        return platform.camera_focus_lockable()

    @property
    def focus_pinned(self) -> bool:
        """Whether the current capture accepted an absolute focus apply.

        Exposure tuning permits -4 only with an accepted pin; its ~16fps
        slows AF convergence, so unpinned rigs use the -5 ceiling.
        """
        return self._focus_applied

    def lock_focus(self) -> bool:
        """Request an unremembered freeze for `focus.lock`.

        Returns driver acceptance; the caller verifies sharpness and rolls
        back failures. `apply_focus` establishes a replayable position.
        """
        with self._cap_lock:
            self._require_open()
            self._focus_needs_restore = True
            return platform.camera_lock_focus(self.cap)

    def unlock_focus(self) -> None:
        """Clear the remembered pin and request continuous autofocus."""
        with self._cap_lock:
            self._require_open()
            self._focus_value = None
            self._focus_applied = False
            platform.camera_unlock_focus(self.cap)
            self._focus_needs_restore = False

    def read_focus(self) -> float | None:
        """Current absolute lens position, or None when the driver
        doesn't report a usable one."""
        with self._cap_lock:
            self._require_open()
            return platform.camera_read_focus(self.cap)

    def apply_focus(self, value: float) -> bool:
        """Apply and remember a position only when the driver accepts it.

        Failure clears any previous pin and requests AF. The caller must
        also verify sharpness before saving a new calibration value.
        """
        with self._cap_lock:
            self._require_open()
            self._focus_needs_restore = True
            self._focus_applied = False
            # A partial write invalidates any previous pin. Reconnect
            # must keep the AF fallback, not replay a stale position.
            self._focus_value = None
            self._focus_applied = _apply_focus(self.cap, value)
            if self._focus_applied:
                self._focus_value = value
            return self._focus_applied

    def wait_frames(self, n: int, timeout: float = 5.0) -> bool:
        """Block until the reader publishes `n` MORE frames (or timeout).
        See ``FrameReader.wait_frames`` — the settle primitive for
        exposure tuning."""
        return self._reader.wait_frames(n, timeout=timeout)

    # ─── Frame accessors ────────────────────────────────────────

    def _fresh_frame(self) -> PublishedFrame | None:
        """Return the latest raw BGR frame and publication time, or ``None``.

        Waits up to ``FrameReader.FRAME_WAIT_SECONDS`` for a frame
        fresher than ``FRESH_MAX_AGE_SECONDS``; otherwise returns ``None``.
        """
        frame = self._reader.fresh_frame()
        # Copy outside the reader's lock — a frame copy (~1 ms at 1080p,
        # ~8 ms at 4K) would otherwise stall the next publish for that long.
        if frame is None:
            return None
        out = frame.frame.copy()
        save_raw_camera(out)  # no-op unless --save-raw-camera
        return PublishedFrame(out, frame.published_at)

    def raw_frame(self) -> np.ndarray | None:
        """Return a fresh BGR frame without applying calibration rotation.

        Used during camera identification (warm-start auto-pick) where
        rotation isn't known yet. For normal use see ``peek`` and
        ``snapshot`` which apply ``self.rotation`` before returning.
        """
        sample = self._fresh_frame()
        return sample.frame if sample is not None else None

    def _rotate(self, frame: np.ndarray) -> np.ndarray:
        """Apply ``self.rotation`` to a raw frame. No-op when rotation is -1."""
        if self.rotation == -1:
            return frame
        return cv2.rotate(frame, self.rotation)

    def peek(self) -> np.ndarray | None:
        """Return a fresh BGR frame with the calibrated rotation applied.

        Used for high-frequency polling (e.g. the phone-watch runtime) where
        writing a JPEG to disk every tick would be wasteful.
        """
        frame = self._fresh_frame()
        if frame is None:
            return None
        return self._rotate(frame.frame)

    def snapshot(
        self, bbox: tuple[tuple[int, int], tuple[int, int]] | None = None
    ) -> np.ndarray | None:
        """Return a fresh BGR frame with the calibrated rotation applied.

        If ``bbox`` is provided as ``((x1,y1), (x2,y2))``, a green rectangle
        is drawn on the returned frame. When ``PHYSICLAW_SAVE_SNAPSHOTS``
        is set, every frame is also written to ``data/snapshots/``.
        """
        sample = self.snapshot_with_time(bbox)
        return sample.frame if sample is not None else None

    def snapshot_with_time(
        self, bbox: tuple[tuple[int, int], tuple[int, int]] | None = None
    ) -> PublishedFrame | None:
        """A rotated snapshot bound to the reader's host publication time."""
        sample = self._fresh_frame()
        if sample is None:
            return None
        frame = self._rotate(sample.frame)
        if bbox is not None:
            cv2.rectangle(frame, bbox[0], bbox[1], (0, 255, 0), 2)
        save_snapshot(frame)
        return PublishedFrame(frame, sample.published_at)

    def close(self) -> None:
        self._closing = True
        try:
            self._reader.stop()
        finally:
            self._release_capture()

    def _release_capture(self) -> None:
        """Release safely even when stopping the reader was interrupted."""
        # Never release the cap without holding the lock — a native
        # release() concurrent with the reader's cap.read() can segfault
        # the whole process. If the reader is wedged holding the lock,
        # leak the handle instead of deadlocking the caller.
        if self._cap_lock.acquire(timeout=self.CLOSE_LOCK_TIMEOUT_SECONDS):
            try:
                if self._closed:
                    return
                try:
                    # Controls live in volatile RAM: hand a tune-imposed
                    # exposure hold and pinned lens back to firmware auto
                    # so the next run starts fresh. Keep user-pinned exposure.
                    if CONFIG.camera.auto_exposure and not self._exposure_auto:
                        try:
                            platform.camera_set_auto_exposure(self.cap)
                        except Exception:
                            log.warning(
                                "exposure hand-back on close failed", exc_info=True
                            )
                    if self._focus_needs_restore:
                        try:
                            platform.camera_unlock_focus(self.cap)
                            self._focus_needs_restore = False
                        except Exception:
                            log.warning(
                                "focus hand-back on close failed", exc_info=True
                            )
                finally:
                    # A Ctrl-C during a control reset must still release the
                    # native handle, just as an interrupted reader stop does.
                    self.cap.release()
                    self._focus_applied = False
                    self._closed = True
                    platform.camera_reset_controls()
            finally:
                self._cap_lock.release()
        else:
            log.warning(
                f"Camera {self.index}: close() couldn't take the cap lock "
                "(reader wedged in cap.read()?) — leaking the capture handle",
                extra={
                    "console_message": f"Camera {self.index} did not close — unplug and reconnect it before restarting"
                },
            )
