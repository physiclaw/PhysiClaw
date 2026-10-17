"""Tests for `physiclaw.common.platform.uvc` — IOKit UVC exposure control.

Fake IOKit functions and COM vtables exercise discovery, ctypes dispatch,
cleanup and UVC framing without touching hardware. Smaller doubles cover
device quirks. Compatibility with Apple's actual ABI still needs hardware
integration checks.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from physiclaw.common.platform import uvc


def test_regression_partial_framework_load_does_not_cache_success(mocker) -> None:
    mocker.patch.object(uvc, "_iokit", None)
    mocker.patch.object(uvc, "_cf", None)
    iokit = MagicMock()
    load = mocker.patch.object(
        uvc.C,
        "CDLL",
        side_effect=[iokit, OSError("missing CF"), iokit, OSError("missing CF")],
    )

    first = uvc.camera_terminal()
    second = uvc.camera_terminal()

    assert first is second is None
    assert load.call_count == 4
    iokit.IOServiceGetMatchingServices.assert_not_called()


def test_regression_failed_interface_number_never_probes_or_opens(usb_bus) -> None:
    usb_bus.number_error = 1

    channel = uvc.camera_terminal()

    assert channel is None
    assert usb_bus.calls.count("interface.Release") == 1
    assert "open" not in usb_bus.calls
    assert usb_bus.requests == []


def test_terminal_constructor_reports_interface_number_failure(usb_bus) -> None:
    usb_bus.number_error = 1

    with pytest.raises(OSError, match="UVC GetInterfaceNumber failed: 1"):
        uvc.CameraTerminal(usb_bus.interface)


def test_terminal_native_dispatch_releases_discovery_and_keeps_channel(usb_bus) -> None:
    channel = uvc.camera_terminal()

    assert channel is not None
    assert usb_bus.requests == [
        (0xA1, 0x81, 0x0200, 0x0103, 1),
        (0xA1, 0x81, 0x0400, 0x0103, 4),
    ]
    assert sorted(usb_bus.released_services) == [10, 20, 100, 200]
    assert usb_bus.calls.count("device.Release") == 1
    assert "interface.Release" not in usb_bus.calls
    assert len(usb_bus.created_plugins) == 2
    assert sorted(usb_bus.destroyed_plugins) == sorted(usb_bus.created_plugins)


def test_terminal_native_dispatch_releases_unresponsive_interface(usb_bus) -> None:
    usb_bus.exposure_answers = False

    channel = uvc.camera_terminal()

    assert channel is None
    assert usb_bus.calls.count("interface.Release") == 1
    assert usb_bus.calls.count("close") == 1


@pytest.mark.parametrize("open_error", [0, 1])
def test_terminal_close_releases_once_and_rejects_late_requests(usb_bus, open_error):
    usb_bus.open_error = open_error
    channel = uvc.camera_terminal()
    assert channel is not None
    requests = list(usb_bus.requests)

    channel.close()
    channel.close()

    assert usb_bus.calls.count("interface.Release") == 1
    assert usb_bus.calls.count("close") == (1 if open_error == 0 else 0)
    assert not channel.set_auto()
    assert usb_bus.requests == requests


def test_terminal_close_waits_for_inflight_control_request(usb_bus):
    channel = uvc.camera_terminal()
    entered, resume = threading.Event(), threading.Event()
    closing, closed = threading.Event(), threading.Event()
    result = []

    def control(interface, pipe, request):
        entered.set()
        if not resume.wait(2):
            return 1
        request.contents.wLenDone = request.contents.wLength
        return 0

    callback = uvc._CTRL(control)
    usb_bus.interface.contents.contents.ControlRequest = callback
    reader = threading.Thread(target=lambda: result.append(channel.set_auto()))

    def close():
        closing.set()
        channel.close()
        closed.set()

    closer = threading.Thread(target=close)
    reader.start()
    try:
        assert entered.wait(1)
        closer.start()
        assert closing.wait(1)
        assert not closed.wait(0.05)
    finally:
        resume.set()
        reader.join(2)
        if closer.ident is not None:
            closer.join(2)

    assert result == [True]
    assert closed.is_set()
    assert usb_bus.calls.count("interface.Release") == 1


@pytest.mark.parametrize(
    "error", ["iterator_error", "plugin_error", "query_error", "registry_error"]
)
def test_terminal_native_discovery_failure_does_not_probe(usb_bus, error) -> None:
    setattr(usb_bus, error, 1)

    channel = uvc.camera_terminal()

    assert channel is None
    assert usb_bus.requests == []


def test_terminal_native_failed_query_destroys_plugins(usb_bus) -> None:
    usb_bus.query_error = 1

    channel = uvc.camera_terminal()

    assert channel is None
    # Both registry classes were attempted; each temporary plug-in is released.
    assert len(usb_bus.created_plugins) == 2
    assert sorted(usb_bus.destroyed_plugins) == sorted(usb_bus.created_plugins)
    assert sorted(usb_bus.released_services) == [10, 10, 100, 100]


@pytest.mark.parametrize("present", ["device_present", "interface_present"])
def test_terminal_native_missing_device_does_not_probe(usb_bus, present) -> None:
    setattr(usb_bus, present, False)

    channel = uvc.camera_terminal()

    assert channel is None
    assert usb_bus.requests == []


def test_terminal_native_open_refusal_still_probes_pipe_zero(usb_bus) -> None:
    usb_bus.open_error = 1

    channel = uvc.camera_terminal()

    assert channel is not None
    assert len(usb_bus.requests) == 2


@pytest.mark.parametrize("position", [-1, 65536])
def test_regression_invalid_focus_position_preserves_autofocus(
    position, mocker
) -> None:
    call = mocker.patch.object(uvc, "_call", return_value=0)
    channel = uvc.CameraTerminal("intf")
    call.reset_mock()

    ok = channel.apply_focus(position)

    assert ok is False
    call.assert_not_called()


# ---------- camera_terminal fail-soft policy ----------


def test_terminal_none_when_iokit_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(uvc, "_load", lambda: False)

    assert uvc.camera_terminal() is None


def test_terminal_none_when_no_uvc_device(monkeypatch) -> None:
    monkeypatch.setattr(uvc, "_load", lambda: True)
    monkeypatch.setattr(uvc, "_video_control_interfaces", lambda: [])

    assert uvc.camera_terminal() is None


def test_terminal_none_and_released_with_multiple_devices(monkeypatch) -> None:
    # OpenCV indexes cameras via AVFoundation, this module via IOKit;
    # with two devices the orders may disagree, so refuse to tune.
    released = []
    monkeypatch.setattr(uvc, "_load", lambda: True)
    monkeypatch.setattr(uvc, "_video_control_interfaces", lambda: ["intf-a", "intf-b"])
    monkeypatch.setattr(
        uvc, "_call", lambda ptr, name, *a: released.append((ptr, name))
    )

    assert uvc.camera_terminal() is None
    assert released == [("intf-a", "Release"), ("intf-b", "Release")]


def test_terminal_none_when_probe_fails(monkeypatch) -> None:
    # Device enumerates but won't answer for its exposure controls (the
    # Monterey+ empty-controls failure mode) -> degrade, don't tune blind.
    monkeypatch.setattr(uvc, "_load", lambda: True)
    monkeypatch.setattr(uvc, "_video_control_interfaces", lambda: ["intf"])

    channel = MagicMock()
    channel.probe.return_value = False
    monkeypatch.setattr(uvc, "CameraTerminal", lambda intf: channel)

    assert uvc.camera_terminal() is None
    channel.close.assert_called_once_with()


def test_terminal_returned_for_single_answering_device(monkeypatch) -> None:
    monkeypatch.setattr(uvc, "_load", lambda: True)
    monkeypatch.setattr(uvc, "_video_control_interfaces", lambda: ["intf"])
    monkeypatch.setattr(uvc, "_call", lambda *a: 0)

    channel = MagicMock()
    channel.probe.return_value = True
    monkeypatch.setattr(uvc, "CameraTerminal", lambda intf: channel)

    assert uvc.camera_terminal() is channel


# ---------- CameraTerminal request framing ----------


def _bare_terminal(monkeypatch, **attrs) -> uvc.CameraTerminal:
    """CameraTerminal without the IOKit-touching __init__, with `attrs`
    set on the instance. The shared base for every framing test."""
    monkeypatch.setattr(uvc.CameraTerminal, "__init__", lambda self, intf: None)
    channel = uvc.CameraTerminal(None)
    channel._lock = threading.Lock()
    for name, value in attrs.items():
        setattr(channel, name, value)
    return channel


def _terminal_with_recorder(monkeypatch, results: dict[tuple, int]):
    """Build an CameraTerminal whose _request is replaced by a recorder.

    `results` maps (direction, request, selector) to an int the fake
    device "returns" (loaded into the data buffer little-endian); a
    missing key means the request fails.
    """
    channel = _bare_terminal(monkeypatch, _range=None)
    sent = []

    def request(direction, request_code, selector, data):
        sent.append((direction, request_code, selector, bytes(data)))
        key = (direction, request_code, selector)
        if key not in results:
            return False
        data[:] = results[key].to_bytes(len(data), "little")
        return True

    channel._request = request
    return channel, sent


def test_set_manual_clamps_to_device_range(monkeypatch) -> None:
    channel, sent = _terminal_with_recorder(
        monkeypatch,
        {
            (uvc._OUT, uvc._SET_CUR, uvc._CT_AE_MODE): 0,
            (uvc._OUT, uvc._SET_CUR, uvc._CT_EXPOSURE_ABS): 0,
            (uvc._IN, uvc._GET_MIN, uvc._CT_EXPOSURE_ABS): 10,
            (uvc._IN, uvc._GET_MAX, uvc._CT_EXPOSURE_ABS): 2000,
        },
    )

    assert channel.set_manual(5000) is True

    # mode=1 first, then the clamped 4-byte exposure write.
    assert sent[0][:3] == (uvc._OUT, uvc._SET_CUR, uvc._CT_AE_MODE)
    assert sent[0][3] == (1).to_bytes(1, "little")
    assert sent[-1][:3] == (uvc._OUT, uvc._SET_CUR, uvc._CT_EXPOSURE_ABS)
    assert sent[-1][3] == (2000).to_bytes(4, "little")


def test_set_manual_passes_ticks_through_without_published_range(
    monkeypatch,
) -> None:
    channel, sent = _terminal_with_recorder(
        monkeypatch,
        {
            (uvc._OUT, uvc._SET_CUR, uvc._CT_AE_MODE): 0,
            (uvc._OUT, uvc._SET_CUR, uvc._CT_EXPOSURE_ABS): 0,
        },
    )

    assert channel.set_manual(156) is True
    assert sent[-1][3] == (156).to_bytes(4, "little")


def test_set_manual_fails_when_mode_write_refused(monkeypatch) -> None:
    channel, sent = _terminal_with_recorder(monkeypatch, {})

    assert channel.set_manual(156) is False
    # Never writes an exposure value on top of an unknown AE mode.
    assert all(s[2] != uvc._CT_EXPOSURE_ABS for s in sent)


def test_set_auto_falls_back_to_full_auto_mode(monkeypatch) -> None:
    # Mode 8 (aperture priority) refused -> retry with mode 2.
    channel, sent = _terminal_with_recorder(monkeypatch, {})

    assert channel.set_auto() is False
    assert [s[3] for s in sent] == [
        (8).to_bytes(1, "little"),
        (2).to_bytes(1, "little"),
    ]


# ---------- camera-terminal discovery + transfer-length guard ----------


def _channel_with_device(monkeypatch, terminals: dict[int, dict[int, int]]):
    """CameraTerminal over a fake bus: `terminals` maps entity id ->
    {selector: control byte size}. GET requests to a listed control
    "transfer" min(its real size, requested length) bytes — mimicking a
    same-numbered selector of the wrong size — and anything else STALLs.
    """
    channel = _bare_terminal(monkeypatch, _terminal=1)

    def request(direction, request_code, selector, data):
        controls = terminals.get(channel._terminal, {})
        if direction != uvc._IN or selector not in controls:
            return False
        done = min(controls[selector], len(data))
        # Mirrors the real _request: partial transfer = failure.
        return done == len(data)

    channel._request = request
    return channel


def test_probe_settles_on_conventional_terminal_1(monkeypatch) -> None:
    channel = _channel_with_device(
        monkeypatch, {1: {uvc._CT_AE_MODE: 1, uvc._CT_EXPOSURE_ABS: 4}}
    )

    assert channel.probe() is True
    assert channel._terminal == 1


def test_probe_discovers_nonstandard_terminal_id(monkeypatch) -> None:
    channel = _channel_with_device(
        monkeypatch, {3: {uvc._CT_AE_MODE: 1, uvc._CT_EXPOSURE_ABS: 4}}
    )

    assert channel.probe() is True
    assert channel._terminal == 3


def test_probe_rejects_processing_unit_by_control_size(monkeypatch) -> None:
    # A PU's selectors 0x02/0x04 are brightness/contrast — 2 bytes each.
    # The size mismatch must reject it, not silently mis-drive it.
    channel = _channel_with_device(
        monkeypatch, {2: {uvc._CT_AE_MODE: 2, uvc._CT_EXPOSURE_ABS: 2}}
    )

    assert channel.probe() is False


def test_probe_false_when_no_terminal_answers(monkeypatch) -> None:
    channel = _channel_with_device(monkeypatch, {})

    assert channel.probe() is False


@pytest.mark.parametrize(
    "done, expected",
    [(2, None), (4, 0)],
    ids=["short-transfer-rejected", "full-transfer-accepted"],
)
def test_request_requires_full_transfer(monkeypatch, done, expected) -> None:
    # kr == 0 alone is not success: wLenDone must equal wLength.
    channel = _bare_terminal(monkeypatch, _intf="intf", _terminal=1, _ifnum=0)

    def fake_call(ptr, name, pipe, req_ref):
        req_ref._obj.wLenDone = done
        return 0

    monkeypatch.setattr(uvc, "_call", fake_call)

    assert channel._get(uvc._GET_CUR, uvc._CT_EXPOSURE_ABS, 4) == expected


# ---------- focus controls ----------


def test_answers_focus_requires_both_controls_and_caches(monkeypatch) -> None:
    channel, sent = _terminal_with_recorder(
        monkeypatch,
        {
            (uvc._IN, uvc._GET_CUR, uvc._CT_FOCUS_AUTO): 1,
            (uvc._IN, uvc._GET_CUR, uvc._CT_FOCUS_ABS): 120,
        },
    )
    channel._focus_support = None

    assert channel.answers_focus() is True
    assert channel.answers_focus() is True  # cached — no new requests
    assert len(sent) == 2


def test_answers_focus_false_on_fixed_focus_camera(monkeypatch) -> None:
    channel, _ = _terminal_with_recorder(monkeypatch, {})
    channel._focus_support = None

    assert channel.answers_focus() is False


def test_lock_focus_reads_position_disables_af_and_pins_it(monkeypatch) -> None:
    channel, sent = _terminal_with_recorder(
        monkeypatch,
        {
            (uvc._IN, uvc._GET_CUR, uvc._CT_FOCUS_ABS): 120,
            (uvc._OUT, uvc._SET_CUR, uvc._CT_FOCUS_AUTO): 0,
            (uvc._OUT, uvc._SET_CUR, uvc._CT_FOCUS_ABS): 0,
        },
    )

    assert channel.lock_focus() is True
    # AF off before the absolute write — manual focus is undefined while
    # continuous AF is on — and the position pinned back explicitly.
    writes = [s for s in sent if s[0] == uvc._OUT]
    assert writes[0][:3] == (uvc._OUT, uvc._SET_CUR, uvc._CT_FOCUS_AUTO)
    assert writes[0][3] == (0).to_bytes(1, "little")
    assert writes[1][:3] == (uvc._OUT, uvc._SET_CUR, uvc._CT_FOCUS_ABS)
    assert writes[1][3] == (120).to_bytes(2, "little")


def test_lock_focus_skips_pin_when_position_unreadable(monkeypatch) -> None:
    channel, sent = _terminal_with_recorder(
        monkeypatch,
        {(uvc._OUT, uvc._SET_CUR, uvc._CT_FOCUS_AUTO): 0},
    )

    assert channel.lock_focus() is True
    assert all(s[2] != uvc._CT_FOCUS_ABS or s[0] == uvc._IN for s in sent)


def test_lock_focus_fails_when_af_toggle_refused(monkeypatch) -> None:
    channel, sent = _terminal_with_recorder(
        monkeypatch,
        {(uvc._IN, uvc._GET_CUR, uvc._CT_FOCUS_ABS): 120},
    )

    assert channel.lock_focus() is False
    # Never writes a focus position on top of a live AF.
    assert all(
        s[:2] != (uvc._OUT, uvc._SET_CUR) or s[2] == uvc._CT_FOCUS_AUTO for s in sent
    )


def test_unlock_focus_reenables_af(monkeypatch) -> None:
    channel, sent = _terminal_with_recorder(
        monkeypatch,
        {(uvc._OUT, uvc._SET_CUR, uvc._CT_FOCUS_AUTO): 0},
    )

    assert channel.unlock_focus() is True
    assert sent[-1][:3] == (uvc._OUT, uvc._SET_CUR, uvc._CT_FOCUS_AUTO)
    assert sent[-1][3] == (1).to_bytes(1, "little")


def test_read_focus_returns_current_position(monkeypatch) -> None:
    channel, _ = _terminal_with_recorder(
        monkeypatch,
        {(uvc._IN, uvc._GET_CUR, uvc._CT_FOCUS_ABS): 120},
    )

    assert channel.read_focus() == 120


def test_read_focus_none_when_read_fails(monkeypatch) -> None:
    channel, _ = _terminal_with_recorder(monkeypatch, {})

    assert channel.read_focus() is None


def test_apply_focus_disables_af_then_writes_position(monkeypatch) -> None:
    channel, sent = _terminal_with_recorder(
        monkeypatch,
        {
            (uvc._OUT, uvc._SET_CUR, uvc._CT_FOCUS_AUTO): 0,
            (uvc._OUT, uvc._SET_CUR, uvc._CT_FOCUS_ABS): 0,
        },
    )

    assert channel.apply_focus(137) is True
    writes = [s for s in sent if s[0] == uvc._OUT]
    assert writes[0][:3] == (uvc._OUT, uvc._SET_CUR, uvc._CT_FOCUS_AUTO)
    assert writes[0][3] == (0).to_bytes(1, "little")
    assert writes[1][:3] == (uvc._OUT, uvc._SET_CUR, uvc._CT_FOCUS_ABS)
    assert writes[1][3] == (137).to_bytes(2, "little")


def test_apply_focus_fails_when_af_toggle_refused(monkeypatch) -> None:
    channel, sent = _terminal_with_recorder(monkeypatch, {})

    assert channel.apply_focus(137) is False
    # Never writes a focus position on top of a live AF.
    assert all(s[2] != uvc._CT_FOCUS_ABS for s in sent if s[0] == uvc._OUT)
