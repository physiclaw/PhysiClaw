"""Warm-start: resume from the saved Calibration bundle.

``try_resume`` (called only when ``physiclaw server --warm-start`` is
passed) loads the bundle from disk into ``physiclaw.calibration``,
reconnects hardware, runs an end-to-end sanity tap, and flips the ready
flag only if every test passes. A plain ``physiclaw server`` boot
ignores the bundle entirely — see ``core/server/app.py``.

The clean-shutdown invariant is what makes warm-start work at all.
``PhysiClaw.shutdown()`` fast-moves the stylus to ``(0, 0)`` (= screen
center per the bundle's affine) before closing the serial port. On the
next ``connect_arm`` the ``G92 X0 Y0`` in ``arm.setup()`` re-pins the
origin at the same physical spot, keeping ``pct_to_grbl`` valid. The
sanity tap is the only mechanism that catches violations of this
invariant (crash, power cut, arm bumped).
"""

import logging
import socket
import sys
import time

from physiclaw.config import CONFIG

log = logging.getLogger(__name__)

# How long to wait for the phone to (re)load /bridge, in seconds.
BRIDGE_WAIT_TIMEOUT = CONFIG.warm_start.bridge_wait_timeout_seconds
# After a /bridge load is detected, let the page finish rendering before
# we start tapping dots at it.
BRIDGE_SETTLE_SECONDS = CONFIG.warm_start.bridge_settle_seconds

# How long to wait for uvicorn's listening socket to be accepting
# connections, in seconds. IPv4 only — if --host is IPv6 this will time
# out; today all callers use v4.
PORT_WAIT_TIMEOUT = CONFIG.warm_start.port_wait_timeout_seconds
PORT_WAIT_CONNECT_TIMEOUT = CONFIG.warm_start.port_wait_connect_timeout_seconds
PORT_WAIT_INTERVAL = CONFIG.warm_start.port_wait_interval_seconds


def wait_for_port(
    host: str, port: int, timeout: float = PORT_WAIT_TIMEOUT
) -> bool:
    """Block until something is accepting TCP connections on (host, port).

    Returns True on first successful connect, False if `timeout` elapses.
    Lives here because the warm-start thread uses it to synchronize with
    uvicorn startup — signalling SIGINT mid-startup leaks CancelledError
    tracebacks through the lifespan machinery.
    """
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(PORT_WAIT_CONNECT_TIMEOUT)
            try:
                s.connect((probe_host, port))
                return True
            except OSError:
                pass
        time.sleep(PORT_WAIT_INTERVAL)
    return False




def _sanity(physiclaw, calib, phone) -> bool:
    """Run a compact end-to-end tap verification. Returns True iff every
    tap landed within tolerance. No-touches counts as failure — warm-start
    only succeeds when we've proven the calibration still holds.

    On failure, logs the specific diagnosis (no touches = /bridge not open;
    touches but off = arm/phone/camera moved) so the caller can stay terse.
    """
    from physiclaw.core.calibration.calibrate import validate_calibration

    cal = physiclaw.calibration
    phone.set_mode("calibrate")
    try:
        results = validate_calibration(
            physiclaw.arm,
            physiclaw.cam,
            calib,
            cal.z_tap,
            cal.effective_rotation(),
            cal.pct_to_grbl,
            cal.pct_to_cam,
            cam_size=cal.cam_size,
            num_tests=2,
        )
    finally:
        phone.set_mode("bridge")

    total = len(results)
    received = sum(1 for r in results if r.get("error", 999) < 999)
    passed = sum(1 for r in results if r["passed"])
    if passed == total:
        log.info(f"--warm-start: sanity passed ({passed}/{total} taps)")
        return True
    if received == 0:
        log.error(
            "--warm-start: sanity — no taps registered. "
            "Is the phone's /bridge page open and foregrounded?"
        )
    else:
        log.error(
            f"--warm-start: sanity — {passed}/{total} taps within tolerance "
            f"({received}/{total} touches received). Calibration looks stale "
            f"(arm, phone, or camera likely moved since last setup)."
        )
    return False


def try_resume(cam_index_override: int | None) -> bool:
    """Connect hardware, run sanity, flip ready if everything holds.

    Camera index comes from ``--cam-index`` if provided, else from
    ``bundle.cam_index``, else 0.

    Returns True on success; False (with a logged reason) if the bundle
    is incomplete, hardware reconnect fails, or sanity doesn't pass.
    The caller exits non-zero so the user can fall back to plain
    ``uv run physiclaw`` + ``setup.py``.
    """
    from physiclaw.core.calibration.state import Calibration
    from physiclaw.core.server.app import physiclaw, _calib, _phone

    loaded = Calibration.load()
    if loaded is None:
        log.error("--warm-start: no calibration bundle on disk")
        return False
    physiclaw.calibration = loaded
    if loaded.viewport_shift is not None:
        # Mirror into the bridge-side state so calibration handlers that
        # read `calib.viewport_shift` (e.g. show_assistive_touch) see it.
        _calib.viewport_shift = loaded.viewport_shift
        physiclaw.assistive_touch.compute_at_screen_pos(loaded.viewport_shift)
    if loaded.screen_dimension is not None:
        # Restore the CSS-pt dimensions so warm-start's validate can run
        # without waiting for the phone's /bridge page to POST them again.
        _calib.screen_dimension = loaded.screen_dimension
    log.info(
        f"--warm-start: loaded bundle (z_tap={loaded.z_tap}mm, "
        f"rotation={loaded.cam_rotation}, complete={loaded.complete})"
    )

    cal = physiclaw.calibration
    if not cal.complete:
        log.error("--warm-start: bundle on disk is incomplete")
        return False
    cam_index = cam_index_override if cam_index_override is not None else (
        cal.cam_index if cal.cam_index is not None else 0
    )
    try:
        physiclaw.connect_arm()
        physiclaw.connect_camera(cam_index)
    except Exception as e:
        log.error(f"--warm-start: hardware reconnect failed: {e}")
        return False

    # Clean shutdown parks the stylus at (0, 0) = screen center, so the
    # fresh setup() on reconnect re-origins there. Warm-start assumes
    # that invariant held; the sanity tap catches cases where it didn't
    # (killed without shutdown, power yank, arm bumped).
    if sys.stdin.isatty():
        print()
        print("━" * 60)
        print("Warm-start")
        print("  Open or refresh /bridge on the phone (foreground, not locked).")
        print(f"  Server waits up to {BRIDGE_WAIT_TIMEOUT}s for steady polling.")
        print("━" * 60)
        if not physiclaw._bridge.wait_for_connection(
            BRIDGE_WAIT_TIMEOUT, BRIDGE_SETTLE_SECONDS
        ):
            log.error(
                f"--warm-start: /bridge page not polling within "
                f"{BRIDGE_WAIT_TIMEOUT}s — open or refresh /bridge on the phone."
            )
            return False
    else:
        log.info("--warm-start: non-interactive; running sanity immediately")

    if not _sanity(physiclaw, _calib, _phone):
        # _sanity logged the specific diagnosis.
        return False

    # Match setup.py's final step: send the phone home (swipe from bottom),
    # then flip ready. home_screen's locked() context auto-parks the arm
    # off-screen on exit, so nothing is hovering over the glass afterward.
    physiclaw.home_screen()
    physiclaw.mark_ready()
    log.info(
        f"--warm-start: resumed from bundle "
        f"(z_tap={cal.z_tap}mm, cam={cam_index}) — MCP tools ready"
    )
    return True
