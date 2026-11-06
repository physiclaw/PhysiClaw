"""GRBL diagnostic protocol and motion polling without serial hardware."""

from types import SimpleNamespace

import pytest

from scripts import grbl_jog as jog


def test_find_port_bounds_probe_writes_and_closes_probe(diagnostic_device, mocker):
    serial = diagnostic_device(jog, responder=lambda _: [b"[VER:Grbl test]\nok\n"])
    mocker.patch.object(
        jog.serial.tools.list_ports,
        "comports",
        return_value=[SimpleNamespace(device="FAKE", description="USB CP210")],
    )
    open_port = mocker.patch.object(jog.serial, "Serial", return_value=serial)

    port = jog.find_port()

    assert port == "FAKE"
    open_port.assert_called_once_with("FAKE", jog.BAUD, timeout=2, write_timeout=2)
    assert serial.writes == [b"$I\n"]
    assert serial.closed


@pytest.mark.parametrize("description", ["Bluetooth", "Unknown USB device"])
def test_connect_auto_detection_without_grbl_fails_cleanly(
    diagnostic_device, mocker, description
):
    serial = diagnostic_device(jog, responder=lambda _: [b"not the controller\n"])
    mocker.patch.object(
        jog.serial.tools.list_ports,
        "comports",
        return_value=[SimpleNamespace(device="FAKE", description=description)],
    )
    mocker.patch.object(jog.serial, "Serial", return_value=serial)

    with pytest.raises(SystemExit, match="No GRBL device found"):
        jog.connect(None)


def test_find_port_skips_unopenable_candidate(diagnostic_device, mocker):
    diagnostic_device(jog)
    mocker.patch.object(
        jog.serial.tools.list_ports,
        "comports",
        return_value=[SimpleNamespace(device="FAKE", description="USB CP210")],
    )
    mocker.patch.object(jog.serial, "Serial", side_effect=OSError("port busy"))

    port = jog.find_port()

    assert port is None


def test_connect_uses_auto_detected_port_and_drains_banner(diagnostic_device, mocker):
    serial = diagnostic_device(jog, [b"Grbl ready\n"])
    mocker.patch.object(jog, "find_port", return_value="FAKE")
    open_port = mocker.patch.object(jog.serial, "Serial", return_value=serial)

    connected = jog.connect(None)

    assert connected is serial
    assert not serial.replies
    open_port.assert_called_once_with("FAKE", jog.BAUD, timeout=3, write_timeout=3)


def test_unlock_if_alarmed_waits_for_unlock_acknowledgment(diagnostic_device):
    serial = diagnostic_device(jog, [b"<Alarm|WPos:0,0>\n", b"ok\n"])

    jog.unlock_if_alarmed(serial)

    assert serial.writes == [b"?", b"$X\n"]


def test_main_runs_balanced_motion_phases_and_closes_port(
    diagnostic_device, monkeypatch, mocker
):
    def respond(command):
        return [b"<Idle>\n"] if command == b"?" else [b"ok\n"]

    serial = diagnostic_device(jog, responder=respond)
    mocker.patch.object(jog.serial, "Serial", return_value=serial)
    mocker.patch.object(jog, "configure_console")
    monkeypatch.setattr(
        jog.sys, "argv", ["diagnostic", "--port", "FAKE", "--loops", "1", "--dist", "2"]
    )

    result = jog.main()

    assert result == 0
    assert [command for command in serial.writes if command.startswith(b"G91")] == [
        b"G91 G1 X2.000 F1000\n",
        b"G91 G1 X-2.000 F1000\n",
        b"G91 G1 Y2.000 F1000\n",
        b"G91 G1 Y-2.000 F1000\n",
        b"G91 G1 X2.000 Y2.000 F1000\n",
        b"G91 G1 X-2.000 Y-2.000 F1000\n",
        b"G91 G1 X2.000 Y-2.000 F1000\n",
        b"G91 G1 X-2.000 Y2.000 F1000\n",
    ]
    assert b"G94\n" in serial.writes
    assert serial.closed


@pytest.mark.parametrize(
    "args",
    [
        ["--dist", "nan"],
        ["--dist", "inf"],
        ["--dist", "0"],
        ["--feed", "0"],
        ["--feed", "-1"],
        ["--loops", "0"],
    ],
)
def test_invalid_motion_options_fail_before_opening_a_port(monkeypatch, mocker, args):
    connect = mocker.patch.object(jog, "connect")
    monkeypatch.setattr(jog.sys, "argv", ["diagnostic", *args])

    with pytest.raises(SystemExit) as exc:
        jog.main()

    assert exc.value.code == 2
    connect.assert_not_called()


def test_slow_requested_feed_gets_enough_time_to_finish(
    diagnostic_device, monkeypatch, mocker
):
    serial = diagnostic_device(
        jog, responder=lambda command: [b"<Idle>\n"] if command == b"?" else [b"ok\n"]
    )
    mocker.patch.object(jog, "connect", return_value=serial)
    mocker.patch.object(jog, "configure_console")
    idle = mocker.patch.object(jog, "wait_idle")
    monkeypatch.setattr(
        jog.sys,
        "argv",
        ["diagnostic", "--port", "FAKE", "--loops", "1", "--dist", "2", "--feed", "1"],
    )

    assert jog.main() == 0

    assert idle.call_args_list[0].kwargs["timeout"] == pytest.approx(125)
    assert idle.call_args_list[4].kwargs["timeout"] == pytest.approx(2**0.5 * 120 + 5)
    assert b"G91 G1 X2.000 F1\n" in serial.writes


@pytest.mark.parametrize(
    "status", [b"<NotIdle>\n", b"<Run|MSG:Idle>\n", b"<IdleExtra>\n"]
)
def test_regression_wait_idle_requires_exact_state(diagnostic_device, status):
    serial = diagnostic_device(jog, [status, b"<Idle|WPos:0,0>\n"])

    jog.wait_idle(serial)

    assert serial.writes == [b"?", b"?"]


def test_wait_idle_bounds_last_native_status_read(diagnostic_device):
    serial = diagnostic_device(jog)

    with pytest.raises(RuntimeError, match="not idle after"):
        jog.wait_idle(serial, timeout=0.5)

    assert serial.clock.now == pytest.approx(0.55)
    assert serial.read_timeouts == [0.5]
    assert serial.timeout == 3


@pytest.mark.parametrize("stage", ["read", "reset_input_buffer"])
def test_connect_initialization_failure_closes_open_handle(
    diagnostic_device, mocker, stage
):
    serial = diagnostic_device(jog)
    mocker.patch.object(jog.serial, "Serial", return_value=serial)
    mocker.patch.object(serial, stage, side_effect=OSError("startup failed"))

    with pytest.raises(OSError, match="startup failed"):
        jog.connect("FAKE")

    assert serial.closed


def test_connect_interrupted_settle_closes_open_handle(diagnostic_device, mocker):
    serial = diagnostic_device(jog)
    mocker.patch.object(jog.serial, "Serial", return_value=serial)
    mocker.patch.object(jog.time, "sleep", side_effect=KeyboardInterrupt)

    with pytest.raises(KeyboardInterrupt):
        jog.connect("FAKE")

    assert serial.closed
