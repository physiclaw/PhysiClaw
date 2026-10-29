"""Tests for `physiclaw.core.calibration.at_verify` — AT gesture checks.

`verify_assistive_touch` is exercised over its precondition errors (AT
position / viewport-shift unset), the screenshot-timeout and
decode-failure records, the full tap + long-press success
path, the phone-mode choreography (grid up front, plain bridge page at
the end), and the clipboard-timeout partial failure.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest

from physiclaw.core.bridge import BridgeState, PageState
from physiclaw.core.bridge.calib import CalibrationState
from physiclaw.core.calibration import at_verify as at_verify_mod
from physiclaw.core.calibration.transforms import ViewportShift


def _make_cal(*, viewport_shift="identity") -> MagicMock:
    """A CalibrationState mock with the grid constants surfaced."""
    cal = MagicMock()
    cal.GRID_COLS_PCT = CalibrationState.GRID_COLS_PCT
    cal.GRID_ROWS_PCT = CalibrationState.GRID_ROWS_PCT
    cal.viewport_shift = (
        ViewportShift(0, 0, 1, 400, 800)
        if viewport_shift == "identity"
        else viewport_shift
    )
    return cal


def _identity_pct_to_grbl() -> np.ndarray:
    return np.array(
        [
            [10.0, 0.0, 0.0],
            [0.0, 20.0, 0.0],
        ]
    )


def test_verify_assistive_touch_raises_when_at_unset(mocker) -> None:
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = None

    with pytest.raises(RuntimeError, match="AT position not set"):
        at_verify_mod.verify_assistive_touch(
            arm, at, MagicMock(), MagicMock(), _identity_pct_to_grbl(), MagicMock()
        )


def test_verify_assistive_touch_generates_nonce_when_none_is_stored(mocker) -> None:
    mocker.patch.object(at_verify_mod.time, "sleep")
    nonce = [1] * 64
    mocker.patch.object(at_verify_mod, "generate_nonce", return_value=nonce)
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = (0.05, 0.1)
    cal = _make_cal()
    cal._screenshot_nonce = None

    phone, bridge = MagicMock(), MagicMock()
    bridge.wait_screenshot.return_value = None

    at_verify_mod.verify_assistive_touch(
        arm, at, bridge, cal, _identity_pct_to_grbl(), phone
    )

    phone.set_mode.assert_called_once_with(
        "calibrate", "assistive_touch", nonce_bits=nonce
    )


def test_verify_assistive_touch_returns_failed_dict_on_screenshot_timeout(
    mocker,
) -> None:
    mocker.patch.object(at_verify_mod.time, "sleep")
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = (0.05, 0.1)
    cal = _make_cal()
    bridge = MagicMock()
    bridge.wait_screenshot.return_value = None  # timeout
    bridge.connectivity_hint.return_value = None  # phone still polling

    out = at_verify_mod.verify_assistive_touch(
        arm,
        at,
        bridge,
        cal,
        _identity_pct_to_grbl(),
        MagicMock(),
    )

    assert out["passed"] is False
    assert out["screenshot"]["passed"] is False
    assert out["clipboard"]["fetched"] is False


def test_verify_assistive_touch_screenshot_timeout_carries_connectivity_hint(
    mocker, caplog: pytest.LogCaptureFixture
) -> None:
    mocker.patch.object(at_verify_mod.time, "sleep")
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = (0.05, 0.1)
    cal = _make_cal()
    bridge = MagicMock()
    bridge.wait_screenshot.return_value = None  # timeout
    bridge.connectivity_hint.return_value = (
        "the phone may be off Wi-Fi or on a different Wi-Fi network than this computer"
    )

    with caplog.at_level(
        logging.WARNING, logger="physiclaw.core.calibration.at_verify"
    ):
        out = at_verify_mod.verify_assistive_touch(
            arm,
            at,
            bridge,
            cal,
            _identity_pct_to_grbl(),
            MagicMock(),
        )

    assert out["passed"] is False
    assert any("may be off Wi-Fi" in r.getMessage() for r in caplog.records)


def test_verify_assistive_touch_returns_failed_dict_on_decode_error(mocker) -> None:
    mocker.patch.object(at_verify_mod.time, "sleep")
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = (0.05, 0.1)
    cal = _make_cal()
    bridge = MagicMock()
    bridge.wait_screenshot.return_value = b"not an image"

    out = at_verify_mod.verify_assistive_touch(
        arm,
        at,
        bridge,
        cal,
        _identity_pct_to_grbl(),
        MagicMock(),
    )

    assert out["passed"] is False


def test_verify_assistive_touch_raises_when_viewport_shift_unset(mocker) -> None:
    mocker.patch.object(at_verify_mod.time, "sleep")
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = (0.05, 0.1)
    cal = _make_cal()
    cal.viewport_shift = None
    bridge = MagicMock()
    img = np.zeros((400, 400, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    bridge.wait_screenshot.return_value = buf.tobytes()

    with pytest.raises(RuntimeError, match="viewport_shift not set"):
        at_verify_mod.verify_assistive_touch(
            arm, at, bridge, cal, _identity_pct_to_grbl(), MagicMock()
        )


def test_verify_assistive_touch_full_success_path(mocker) -> None:
    mocker.patch.object(at_verify_mod.time, "sleep")
    mocker.patch.object(
        at_verify_mod.random,
        "randbytes",
        return_value=b"\xab\xcd\xef",
    )
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = (0.05, 0.1)
    vshift = ViewportShift(
        offset_x=0,
        offset_y=0,
        dpr=3.0,
        screenshot_width=1170,
        screenshot_height=2532,
    )
    cal = _make_cal(viewport_shift=vshift)
    bridge = MagicMock()
    bridge.wait_clipboard.return_value = True
    img = np.zeros((400, 400, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    bridge.wait_screenshot.return_value = buf.tobytes()

    mocker.patch.object(at_verify_mod, "verify_nonce", return_value=(True, 4))

    out = at_verify_mod.verify_assistive_touch(
        arm,
        at,
        bridge,
        cal,
        _identity_pct_to_grbl(),
        MagicMock(),
    )

    assert out["passed"] is True
    assert out["screenshot"]["passed"] is True
    assert out["clipboard"]["fetched"] is True
    assert out["clipboard"]["text"].startswith("PhysiClaw-")


def test_verify_assistive_touch_shows_clipboard_confirmation_on_phone(mocker) -> None:
    # On the clipboard sub-step the phone flips to bridge mode and the clip
    # text is queued, so the page shows it then the "copied" confirmation.
    mocker.patch.object(at_verify_mod.time, "sleep")
    mocker.patch.object(at_verify_mod, "generate_nonce", return_value=[1] * 64)
    mocker.patch.object(at_verify_mod.random, "randbytes", return_value=b"\xab\xcd\xef")
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = (0.05, 0.1)
    vshift = ViewportShift(
        offset_x=0,
        offset_y=0,
        dpr=3.0,
        screenshot_width=1170,
        screenshot_height=2532,
    )
    cal = _make_cal(viewport_shift=vshift)
    bridge = MagicMock()
    bridge.wait_clipboard.return_value = True
    img = np.zeros((400, 400, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    bridge.wait_screenshot.return_value = buf.tobytes()
    mocker.patch.object(at_verify_mod, "verify_nonce", return_value=(True, 4))
    phone = MagicMock()

    at_verify_mod.verify_assistive_touch(
        arm,
        at,
        bridge,
        cal,
        _identity_pct_to_grbl(),
        phone,
    )

    # Establishes its own grid up front (so a re-run doesn't depend on
    # /show), then flips to bridge mode for the confirmation and stays there
    # — success ends on the plain bridge page, not the grid.
    assert phone.set_mode.call_args_list == [
        mocker.call("calibrate", "assistive_touch", nonce_bits=[1] * 64),
        mocker.call("bridge"),
    ]
    bridge.send_text.assert_called_once()
    assert bridge.send_text.call_args[0][0].startswith("PhysiClaw-")
    bridge.clear_text.assert_called_once()


def test_verify_assistive_touch_clipboard_timeout(mocker, caplog) -> None:
    from physiclaw.common.log_style import ConsoleFilter, TaggedFormatter

    caplog.set_level(logging.INFO)
    mocker.patch.object(at_verify_mod.time, "sleep")
    mocker.patch.object(at_verify_mod.random, "randbytes", return_value=b"\x01\x02\x03")
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = (0.05, 0.1)
    vshift = ViewportShift(
        offset_x=0,
        offset_y=0,
        dpr=3.0,
        screenshot_width=1170,
        screenshot_height=2532,
    )
    cal = _make_cal(viewport_shift=vshift)
    bridge = MagicMock()
    bridge.wait_clipboard.return_value = False
    bridge.expire_text.return_value = False
    bridge.connectivity_hint.return_value = None
    img = np.zeros((400, 400, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    bridge.wait_screenshot.return_value = buf.tobytes()
    mocker.patch.object(at_verify_mod, "verify_nonce", return_value=(True, 4))

    out = at_verify_mod.verify_assistive_touch(
        arm,
        at,
        bridge,
        cal,
        _identity_pct_to_grbl(),
        MagicMock(),
    )

    # Screenshot passed but clipboard didn't → overall failure.
    assert out["passed"] is False
    assert out["screenshot"]["passed"] is True
    assert out["clipboard"]["fetched"] is False
    normal = "\n".join(
        TaggedFormatter("physiclaw", False, verbose=False).format(r)
        for r in caplog.records
        if ConsoleFilter(verbose=False).filter(r)
    )
    assert normal.count("Phone clipboard shortcut timed out") == 1
    assert "AssistiveTouch verification failed" not in normal
    assert any(
        "AssistiveTouch verification failed" in r.getMessage() for r in caplog.records
    )


def _render_nonce(phone: PageState) -> bytes:
    nonce = phone.get_state()["nonce"]
    image = np.zeros((800, 400, 3), np.uint8)
    for i, color in enumerate(nonce["colors"]):
        x = nonce["x"] + i % nonce["cols"] * nonce["size"]
        y = nonce["y"] + i // nonce["cols"] * nonce["size"]
        image[y : y + nonce["size"], x : x + nonce["size"]] = color
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def _real_at_setup(mocker):
    mocker.patch.object(at_verify_mod.time, "sleep")
    mocker.patch.object(
        at_verify_mod, "generate_nonce", side_effect=[[1] * 64, [0] * 64]
    )
    cal, bridge = CalibrationState(), BridgeState()
    cal.viewport_shift = ViewportShift(0, 0, 1, 400, 800)
    cal.screen_dimension = {"viewport_width": 400, "viewport_height": 800}
    phone = PageState(bridge, cal)
    at = MagicMock(at_screen=(0.1, 0.3))

    def upload(*_):
        token = bridge.screenshot_upload_token()
        assert token is not None
        assert bridge.receive_screenshot(_render_nonce(phone), upload_token=token)

    at.tap.side_effect = upload
    at.long_press.side_effect = lambda *_: bridge.fetch_text()
    return at, cal, bridge, phone


def test_regression_at_retry_rejects_photo_from_previous_attempt(mocker) -> None:
    at, cal, bridge, phone = _real_at_setup(mocker)
    first = at_verify_mod.verify_assistive_touch(
        MagicMock(), at, bridge, cal, _identity_pct_to_grbl(), phone
    )
    assert first["passed"] is True
    at.tap.assert_called_once()
    at.double_tap.assert_not_called()
    at.long_press.assert_called_once()

    old_photo = bridge.recent_screenshots(1)[0]
    at.tap.side_effect = lambda *_: bridge.receive_screenshot(old_photo)

    second = at_verify_mod.verify_assistive_touch(
        MagicMock(), at, bridge, cal, _identity_pct_to_grbl(), phone
    )

    assert second["passed"] is False
    assert second["screenshot"] == {"passed": False, "matched": 0, "total": 64}


@pytest.mark.parametrize("fetched_at_timeout", [True, False])
def test_clipboard_timeout_resolves_fetch_and_retirement_atomically(
    mocker, fetched_at_timeout
):
    at, cal, bridge, phone = _real_at_setup(mocker)
    at.long_press.side_effect = None

    def wait_clipboard(*, timeout):
        if fetched_at_timeout:
            assert bridge.fetch_text() is not None
        return False

    mocker.patch.object(bridge, "wait_clipboard", side_effect=wait_clipboard)
    result = at_verify_mod.verify_assistive_touch(
        MagicMock(), at, bridge, cal, _identity_pct_to_grbl(), phone
    )

    assert result["clipboard"]["fetched"] is fetched_at_timeout
    assert result["passed"] is fetched_at_timeout
    assert bridge.fetch_text() is None


@pytest.mark.parametrize(
    "failure", [OSError("gesture failed"), KeyboardInterrupt("cancelled")]
)
@pytest.mark.parametrize("stage", ["send", "press", "wait"])
def test_regression_at_failure_retires_clipboard_payload(
    mocker, stage, failure
) -> None:
    at, cal, bridge, phone = _real_at_setup(mocker)
    send = bridge.send_text

    def fail_after_send(text):
        send(text)
        raise failure

    targets = {
        "send": (bridge, "send_text", fail_after_send),
        "press": (at, "long_press", failure),
        "wait": (bridge, "wait_clipboard", failure),
    }
    target, method, effect = targets[stage]
    mocker.patch.object(target, method, side_effect=effect)

    with pytest.raises(type(failure), match=str(failure)):
        at_verify_mod.verify_assistive_touch(
            MagicMock(), at, bridge, cal, _identity_pct_to_grbl(), phone
        )

    assert bridge.current_text() is None


def test_at_cleanup_failure_preserves_original_exception(mocker) -> None:
    at, cal, bridge, phone = _real_at_setup(mocker)
    at.long_press.side_effect = KeyboardInterrupt("original cancellation")
    cleanup = mocker.patch.object(
        bridge, "clear_text", side_effect=OSError("cleanup failed")
    )

    with pytest.raises(KeyboardInterrupt, match="original cancellation"):
        at_verify_mod.verify_assistive_touch(
            MagicMock(), at, bridge, cal, _identity_pct_to_grbl(), phone
        )

    cleanup.assert_called_once()
