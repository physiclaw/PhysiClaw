"""AssistiveTouch verification — single-tap and long-press end-to-end.

The runtime depends on two iOS Shortcuts wired to AssistiveTouch
gestures; this step proves each one actually fires from an arm-driven
press. Single-tap captures and uploads the current screen — the
uploaded image must contain the grey nonce grid the server generated,
so a stale photo from the camera roll can't pass. Long-press makes the
phone fetch the server's queued clipboard text; the fetch event (plus
the operator paste-verifying the random text) proves the clipboard
pipeline. The step re-establishes its own nonce grid on entry and
leaves the phone on the plain bridge page, so it is self-sufficient
and idempotent across re-runs.
"""

import logging
import random
import time

import cv2
import numpy as np

from physiclaw.common.image import checked_image_size
from physiclaw.common.logger.evidence import record_event, record_upload
from physiclaw.core.bridge import BridgeState, CalibrationState, PageState
from physiclaw.core.bridge.nonce import NONCE_COUNT, generate_nonce, verify_nonce
from physiclaw.core.hardware.arm import StylusArm
from physiclaw.core.hardware.iphone import SCREENSHOT_TIMEOUT_SECONDS, AssistiveTouch

log = logging.getLogger(__name__)


def _failed(reason: str) -> dict:
    """The screenshot-sub-step failure record: nothing matched, no clipboard."""
    return {
        "passed": False,
        "reason": reason,
        "screenshot": {"passed": False, "matched": 0, "total": NONCE_COUNT},
        "clipboard": {"fetched": False, "text": None},
    }


def verify_assistive_touch(
    arm: StylusArm,
    at: AssistiveTouch,
    bridge: BridgeState,
    cal: CalibrationState,
    pct_to_grbl: np.ndarray,
    phone: PageState,
) -> dict:
    """Verify both AT shortcuts end-to-end.

    1. Single-tap → "PhysiClaw Upload Screenshot" captures and uploads the screen.
       Verify the uploaded image contains the grey nonce grid.
    2. Long-press → "PhysiClaw Clipboard" Shortcut GETs /api/bridge/clipboard.
       Verify the server's clipboard-copied event fires within the timeout.
       Return the queued text so the user can paste-verify downstream.

    Requires:
    - assistive-touch/show has positioned the button — this call generates
      a fresh nonce grid, so the phone may be in any mode on entry
    - User has positioned AT at the orange circle
    - cal.viewport_shift is set (from pre-cal)

    Leaves the phone on the plain bridge page after the clipboard confirmation.

    Returns:
        {
          "passed": bool,  # True iff both sub-checks passed
          "screenshot": {"passed": bool, "matched": int, "total": int},
          "clipboard":  {"fetched": bool, "text": str | None},
        }
    """
    if at.at_screen is None:
        raise RuntimeError("AT position not set — call compute_at_screen_pos first")
    t = cal.viewport_shift
    if t is None:
        raise RuntimeError("viewport_shift not set — run measure-viewport-shift first")

    log.info(
        "═══ AssistiveTouch screenshot verification ═══",
        extra={
            "event": "calibration.checking",
            "console": True,
            "console_message": "Calibration: checking phone shortcuts",
        },
    )
    log.info(
        f"  AT position: screen 0-1 ({at.at_screen[0]:.3f}, {at.at_screen[1]:.3f})"
    )

    nonce = generate_nonce()
    record_event(
        "assistive_touch.challenge", expected_nonce=nonce, at_screen=at.at_screen
    )
    bridge.clear_screenshot()

    # Show the nonce grid for the screenshot sub-step, whatever mode the
    # page was left in — a prior verify ends in bridge mode after its
    # clipboard confirmation. Re-establishing it here makes this call
    # self-sufficient instead of relying on assistive-touch/show still
    # being on-screen.
    phone.set_mode("calibrate", "assistive_touch", nonce_bits=nonce)
    time.sleep(0.5)  # let the phone poll and render the grid before the shot

    cal.flush_touches()
    log.info("  Single-tap AT (capture and upload)...")
    record_event(
        "assistive_touch.tap_requested",
        at_screen=at.at_screen,
        strike_seconds=at.TAP_DURATION,
    )
    at.tap(arm, pct_to_grbl)

    log.info("  Waiting for screenshot upload...")
    started = time.monotonic()
    data = bridge.wait_screenshot(timeout=SCREENSHOT_TIMEOUT_SECONDS)
    record_event(
        "assistive_touch.screenshot_wait",
        received=data is not None,
        timeout_seconds=SCREENSHOT_TIMEOUT_SECONDS,
        elapsed_ms=round((time.monotonic() - started) * 1000),
        bytes=len(data) if data is not None else 0,
        bridge_connected=bridge.connected,
        page_touches=cal.flush_touches(),
    )
    if data is None:
        hint = bridge.connectivity_hint()
        log.warning(
            f"  Screenshot upload timed out after {SCREENSHOT_TIMEOUT_SECONDS:g}s"
            + (f" — {hint}" if hint else ""),
            extra={
                "console_message": f"Phone screenshot timed out after {SCREENSHOT_TIMEOUT_SECONDS:g}s — "
                + str(hint or "check the phone's screenshot shortcut")
            },
        )
        return _failed("screenshot_timeout")

    record_upload(data, purpose="assistive_touch")
    img = (
        cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if checked_image_size(data) is not None
        else None
    )
    if img is None:
        log.warning(
            "  Failed to decode screenshot",
            extra={
                "console_message": "Phone screenshot could not be decoded — retry shortcut verification"
            },
        )
        return _failed("screenshot_decode_failed")

    log.info(f"  Screenshot received: {img.shape[1]}×{img.shape[0]}px")

    shot_passed, matched = verify_nonce(img, t, nonce, css_x=cal.nonce_x())
    record_event(
        "assistive_touch.nonce_checked",
        passed=shot_passed,
        matched=matched,
        total=NONCE_COUNT,
        viewport_shift=t,
    )

    if shot_passed:
        log.info(
            f"  ✓ Screenshot pipeline verified ({matched}/{NONCE_COUNT} bits matched)"
        )
    else:
        log.warning(
            f"  ✗ Screenshot verification failed: {matched}/{NONCE_COUNT} bits matched",
            extra={
                "console_message": "Phone screenshot verification failed — retry shortcut verification"
            },
        )

    # ─── Long-press: clipboard fetch verification ──────────────
    # Give iOS a moment to finish the previous Shortcut run before we
    # queue new text and trigger another one.
    time.sleep(5.0)
    clip_text = f"PhysiClaw-{random.randbytes(3).hex().upper()}"
    log.info(f"  Queuing clipboard text: {clip_text!r}")
    # Flip the phone to bridge mode so it shows the text, then the
    # "✓ written to clipboard — paste to verify" confirmation once the
    # Shortcut fetches it. The AT circle / nonce grid were only needed
    # for the screenshot sub-steps; the long-press lands on the iOS AT
    # button regardless of what the web page is showing underneath.
    phone.set_mode("bridge")
    try:
        bridge.send_text(clip_text)
        log.info("  Long-press AT (iOS Shortcut → fetch bridge text)...")
        record_event(
            "assistive_touch.long_press_requested",
            at_screen=at.at_screen,
            strike_seconds=arm.LONG_PRESS_DURATION,
        )
        at.long_press(arm, pct_to_grbl)

        log.info("  Waiting for iOS Shortcut to fetch clipboard text...")
        timeout_seconds = 10.0
        started = time.monotonic()
        clip_fetched = (
            bridge.wait_clipboard(timeout=timeout_seconds) or bridge.expire_text()
        )
        record_event(
            "assistive_touch.clipboard_wait",
            fetched=clip_fetched,
            timeout_seconds=timeout_seconds,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            bridge_connected=bridge.connected,
        )
        if clip_fetched:
            log.info(f"  ✓ Clipboard fetched from server — text: {clip_text!r}")
            log.info("    Phone now shows the copied text — paste anywhere to verify.")
            time.sleep(3.0)  # Hold the confirmation for the operator.
        else:
            hint = bridge.connectivity_hint()
            log.warning(
                "  ✗ Clipboard fetch timed out — server was not hit"
                + (f"; {hint}" if hint else ""),
                extra={
                    "console_message": "Phone clipboard shortcut timed out — "
                    + str(hint or "check the phone's clipboard shortcut")
                },
            )
    except BaseException:
        # Retire even a partially published payload when a gesture fails or
        # is interrupted. Leave the page in bridge mode for the next attempt.
        try:
            bridge.clear_text()
        except Exception:
            log.exception("AT verification failed; retiring clipboard text also failed")
        raise
    bridge.clear_text()

    passed = shot_passed and clip_fetched
    record_event(
        "assistive_touch.verified",
        screenshot_passed=shot_passed,
        clipboard_fetched=clip_fetched,
        passed=passed,
    )
    if passed:
        log.info(
            "  ✓ AssistiveTouch done: single-tap + long-press verified",
            extra={
                "event": "calibration.checked",
                "console": True,
                "console_message": "Phone shortcuts verified",
            },
        )
    else:
        # The failing screenshot/clipboard step already reported its cause.
        log.info("  ✗ AssistiveTouch verification failed")

    return {
        "passed": passed,
        "screenshot": {
            "passed": shot_passed,
            "matched": matched,
            "total": NONCE_COUNT,
        },
        "clipboard": {
            "fetched": clip_fetched,
            "text": clip_text,
        },
    }
