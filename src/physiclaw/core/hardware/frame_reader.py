"""FrameReader — keep a capture stream warm and self-healing.

A background daemon thread continuously pulls frames via an injected
``read_frame`` callable and publishes the latest one; callers get it
through ``fresh_frame()`` / ``wait_frames()`` without blocking on the
native capture. The supervisor policy lives here too: reconnect (via
the injected ``reopen``) when frames go stale, give up and interrupt
the main thread when the drought outlasts ``FATAL_AFTER_SECONDS`` —
reopen can't revive a stream the OS has cut (display sleep, bus
suspend), and dying cleanly beats spamming the log forever.

Nothing in here knows about cv2 or PhysiClaw semantics — ``Camera``
composes a FrameReader with a lock-wrapped ``cap.read`` and its own
``_reopen``, which is what makes this unit testable with scripted
callables instead of thread-timing-sensitive fakes.
"""

import _thread
import logging
import threading
import time

log = logging.getLogger(__name__)


class FrameReader:
    """Background frame pump + self-healing supervisor over one stream."""

    # If the reader gets no frame for this long, ask the owner to
    # close+reopen the stream. Recovers from real disconnects.
    STALE_RECONNECT_SECONDS = 5.0

    # If the reader gets no frame for this long, give up and raise
    # KeyboardInterrupt in the main thread (via _thread.interrupt_main —
    # cross-platform; os.kill(SIGINT) on Windows would TerminateProcess
    # and skip atexit).
    FATAL_AFTER_SECONDS = 60.0

    # Max time fresh_frame() waits for the loop to produce a frame
    # before returning None.
    FRAME_WAIT_SECONDS = 2.0

    # A frame older than this is treated as "not yet fresh" by
    # fresh_frame() — it'll wait for the loop to publish a newer one.
    FRESH_MAX_AGE_SECONDS = 1.0

    # Backoff after a failed read or reconnect, so a permanently broken
    # stream doesn't spin-loop the reader thread.
    READER_BACKOFF_SECONDS = 0.5

    # stop(): how long to wait for the thread. A reader wedged inside a
    # blocking native read can't be joined; the owner handles the
    # consequences (see Camera.close's lock-timeout leak).
    JOIN_TIMEOUT_SECONDS = 3.0

    def __init__(self, read_frame, reopen, *, label: str = "camera") -> None:
        """``read_frame``: () -> (ok, frame) — the owner serializes it
        against its own setters/reopen/close. ``reopen``: () -> None —
        close and reopen the underlying stream; called on staleness."""
        self._read_frame = read_frame
        self._reopen = reopen
        self._label = label
        self._frame = None
        self._frame_time = 0.0
        # Monotonic count of published frames — the settle primitive for
        # exposure tuning (`wait_frames`): after a property set, "wait N
        # frames" is deterministic where "sleep N/fps" is not.
        self._frame_seq = 0
        # Separate from _frame_time because a reopen resets _frame_time —
        # which would otherwise postpone the FATAL_AFTER_SECONDS check
        # indefinitely. _first_fail_time resets only on a real good frame.
        self._first_fail_time: float | None = None
        self._reconnect_attempts = 0
        self._read_failure: str | None = None
        self._cond = threading.Condition()
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"{label}-reader", daemon=True
        )

    # ─── Lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self.JOIN_TIMEOUT_SECONDS)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def healthy(self) -> bool:
        """Live check: loop running, a frame published recently, and no
        ongoing read drought. The drought check matters because a reopen
        resets the stale clock without producing a frame — a dead stream
        that keeps nominally reopening would otherwise read healthy for
        most of every stale cycle."""
        with self._cond:
            age = time.monotonic() - self._frame_time
            return (
                self._thread.is_alive()
                and not self._stopped.is_set()
                and self._first_fail_time is None
                and self._frame is not None
                and age < self.STALE_RECONNECT_SECONDS
            )

    # ─── Publish / consume ──────────────────────────────────────

    def publish(self, frame) -> None:
        """Publish a frame — the loop's good path, and the owner's warmup
        seed before the thread starts."""
        with self._cond:
            failed_at = self._first_fail_time
            self._frame = frame
            self._first_fail_time = None
            self._frame_time = time.monotonic()
            self._frame_seq += 1
            self._cond.notify_all()
            attempts, self._reconnect_attempts = self._reconnect_attempts, 0
            warned = self._read_failure is not None
            self._read_failure = None
        if failed_at is not None:
            elapsed = time.monotonic() - failed_at
            if warned or attempts or elapsed >= self.STALE_RECONNECT_SECONDS:
                log.info(
                    "%s: frames restored after %.1fs%s",
                    self._label,
                    elapsed,
                    f" (reconnects={attempts})" if attempts else "",
                    extra={
                        "event": "camera.frames_restored",
                        "console": True,
                        "console_message": "Camera connection restored",
                        "device_id": self._label,
                        "duration_ms": round(elapsed * 1000),
                        "attempt": attempts,
                    },
                )

    def note_reopened(self) -> None:
        """Reset the stale clock after the owner rebuilt the stream, so
        one reopen isn't immediately followed by another. Discard the old
        frame: reopening is not evidence of a new capture."""
        with self._cond:
            self._frame = None
            self._frame_time = time.monotonic()

    def wait_frames(self, n: int, timeout: float = 5.0) -> bool:
        """Block until `n` MORE frames are published (or timeout).

        The settle primitive for exposure tuning: drivers apply property
        changes with latency, so "wait N fresh frames" is the
        deterministic version of "sleep a bit". Returns False on timeout
        (reader stalled) — callers treat that as a failed meter."""
        with self._cond:
            target = self._frame_seq + n
            return self._cond.wait_for(
                lambda: self._frame_seq >= target,
                timeout=timeout,
            )

    def fresh_frame(self):
        """The latest published frame, or ``None``.

        Waits up to ``FRAME_WAIT_SECONDS`` for a frame fresher than
        ``FRESH_MAX_AGE_SECONDS``; otherwise returns ``None``. Returns the
        internal reference — the owner copies outside the lock so the copy
        can't stall the next publish."""
        with self._cond:
            fresh = self._cond.wait_for(
                lambda: (
                    self._frame is not None
                    and time.monotonic() - self._frame_time < self.FRESH_MAX_AGE_SECONDS
                ),
                timeout=self.FRAME_WAIT_SECONDS,
            )
            return self._frame if fresh else None

    # ─── The loop ───────────────────────────────────────────────

    def _loop(self) -> None:
        """Pull frames continuously so the native pipeline never goes idle.

        ``read_frame`` blocks for the next native-FPS frame, so the loop
        self-paces — no explicit sleep needed in the steady state.
        """
        while not self._stopped.is_set():
            try:
                ok, frame = self._read_frame()
            except Exception as e:
                signature = f"{type(e).__name__}: {e}"
                if not self._stopped.is_set():
                    level = (
                        logging.WARNING
                        if signature != self._read_failure
                        else logging.DEBUG
                    )
                    self._read_failure = signature
                    log.log(
                        level,
                        "%s: read raised %r — retrying automatically; check camera USB and power",
                        self._label,
                        e,
                        exc_info=True,
                        extra={
                            "console_traceback": False,
                            "event": "camera.read_failed",
                            "console_message": "Camera read failed — retrying; check camera USB and power",
                            "device_id": self._label,
                        },
                    )
                ok, frame = False, None

            # stop() can time out while the native read remains blocked.
            # Its eventual result must not publish, reconnect, or interrupt
            # the process after the owner has already begun closing.
            if self._stopped.is_set():
                return

            now = time.monotonic()

            if ok and frame is not None:
                self.publish(frame)
                continue

            if self._first_fail_time is None:
                self._first_fail_time = now
            fail_duration = now - self._first_fail_time
            if fail_duration >= self.FATAL_AFTER_SECONDS:
                log.error(
                    f"{self._label}: no frames for {fail_duration:.0f}s "
                    "— giving up and exiting process "
                    "(display sleep / bus suspend / hardware gone). "
                    "Wake the computer, check camera USB and power, then restart PhysiClaw.",
                    extra={
                        "event": "camera.unavailable",
                        "console_message": f"Camera unavailable for {fail_duration:.0f}s — stopping. Wake the computer, check USB and power, then restart PhysiClaw.",
                        "device_id": self._label,
                        "duration_ms": round(fail_duration * 1000),
                    },
                )
                _thread.interrupt_main()
                return

            with self._cond:
                stale = now - self._frame_time
            if stale > self.STALE_RECONNECT_SECONDS:
                self._reconnect_attempts += 1
                if self._reconnect_attempts == 1:
                    log.warning(
                        "%s: no fresh frames for %.1fs — reconnecting camera",
                        self._label,
                        stale,
                        extra={
                            "event": "camera.reconnecting",
                            "device_id": self._label,
                        },
                    )
                self._reopen()
            # Unconditional on the fail path: caps iteration rate if
            # read_frame raises or returns empty every tick (e.g. display
            # asleep), otherwise the loop would spin at full CPU.
            self._stopped.wait(self.READER_BACKOFF_SECONDS)
