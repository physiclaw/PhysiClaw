"""Bounded solenoid diagnostics and emergency coil-off attempts use a fake wire."""

from types import SimpleNamespace

import pytest

from scripts import grbl_solenoid_test as solenoid


def test_find_port_bounds_probe_writes_and_closes_probe(diagnostic_device, mocker):
    serial = diagnostic_device(solenoid, responder=lambda _: [b"[VER:Grbl test]\nok\n"])
    mocker.patch.object(
        solenoid.serial.tools.list_ports,
        "comports",
        return_value=[SimpleNamespace(device="FAKE", description="USB CP210")],
    )
    open_port = mocker.patch.object(solenoid.serial, "Serial", return_value=serial)

    port = solenoid.find_port()

    assert port == "FAKE"
    open_port.assert_called_once_with("FAKE", solenoid.BAUD, timeout=2, write_timeout=2)
    assert serial.writes == [b"$I\n"]
    assert serial.closed


@pytest.mark.parametrize("description", ["Bluetooth", "Unknown USB device"])
def test_connect_auto_detection_without_grbl_fails_cleanly(
    diagnostic_device, mocker, description
):
    serial = diagnostic_device(solenoid, responder=lambda _: [b"not the controller\n"])
    mocker.patch.object(
        solenoid.serial.tools.list_ports,
        "comports",
        return_value=[SimpleNamespace(device="FAKE", description=description)],
    )
    mocker.patch.object(solenoid.serial, "Serial", return_value=serial)

    with pytest.raises(SystemExit, match="No GRBL device found"):
        solenoid.connect(None)


def test_find_port_skips_unopenable_candidate(diagnostic_device, mocker):
    diagnostic_device(solenoid)
    mocker.patch.object(
        solenoid.serial.tools.list_ports,
        "comports",
        return_value=[SimpleNamespace(device="FAKE", description="USB CP210")],
    )
    mocker.patch.object(solenoid.serial, "Serial", side_effect=OSError("port busy"))

    port = solenoid.find_port()

    assert port is None


def test_connect_uses_auto_detected_port_and_drains_banner(diagnostic_device, mocker):
    serial = diagnostic_device(solenoid, [b"Grbl ready\n"])
    mocker.patch.object(solenoid, "find_port", return_value="FAKE")
    open_port = mocker.patch.object(solenoid.serial, "Serial", return_value=serial)

    connected = solenoid.connect(None)

    assert connected is serial
    assert not serial.replies
    open_port.assert_called_once_with("FAKE", solenoid.BAUD, timeout=3, write_timeout=3)


def test_unlock_if_alarmed_waits_for_unlock_acknowledgment(diagnostic_device):
    serial = diagnostic_device(solenoid, [b"<Alarm|WPos:0,0>\n", b"ok\n"])

    solenoid.unlock_if_alarmed(serial)

    assert serial.writes == [b"?", b"$X\n"]


@pytest.mark.parametrize("stage", ["read", "reset_input_buffer"])
def test_connect_initialization_failure_closes_open_handle(
    diagnostic_device, mocker, stage
):
    serial = diagnostic_device(solenoid)
    mocker.patch.object(solenoid.serial, "Serial", return_value=serial)
    mocker.patch.object(serial, stage, side_effect=OSError("startup failed"))

    with pytest.raises(OSError, match="startup failed"):
        solenoid.connect("FAKE")

    assert serial.closed


def test_connect_interrupted_settle_closes_open_handle(diagnostic_device, mocker):
    serial = diagnostic_device(solenoid)
    mocker.patch.object(solenoid.serial, "Serial", return_value=serial)
    mocker.patch.object(solenoid.time, "sleep", side_effect=KeyboardInterrupt)

    with pytest.raises(KeyboardInterrupt):
        solenoid.connect("FAKE")

    assert serial.closed


@pytest.fixture
def diagnostic_run(diagnostic_device, monkeypatch, mocker):
    def make(
        *args,
        fail_command=None,
        failure_reply=b"error:23\n",
        dwell_seconds=0,
        cleanup_reply=b"ok\n",
    ):
        serial = diagnostic_device(solenoid)
        activated = False

        def respond(command):
            nonlocal activated
            if command.startswith(b"M3"):
                activated = True
            if command == b"M5\n" and activated:
                return [cleanup_reply]
            if command == fail_command:
                return [failure_reply]
            if command == b"?":
                return [b"<Idle|WPos:0,0>\n"]
            return [b"ok\n"]

        serial.responder = respond
        read = serial.read
        pending_dwell = 0.0
        write = serial.write

        def timed_write(command):
            nonlocal pending_dwell
            if command == b"G4 P20.000\n":
                pending_dwell = dwell_seconds
            return write(command)

        def timed_read(size):
            nonlocal pending_dwell
            if pending_dwell > 0:
                elapsed = min(pending_dwell, serial.timeout)
                pending_dwell -= elapsed
                serial.clock.sleep(elapsed)
                return b""
            return read(size)

        mocker.patch.object(serial, "write", side_effect=timed_write)
        mocker.patch.object(serial, "read", side_effect=timed_read)
        mocker.patch.object(solenoid.serial, "Serial", return_value=serial)
        mocker.patch.object(solenoid, "configure_console")
        monkeypatch.setattr(
            solenoid.sys, "argv", ["diagnostic", "--port", "FAKE", *args]
        )
        return serial

    return make


@pytest.mark.parametrize("command", [b"M3 S1000\n", b"G4 P0.080\n", b"M3 S150\n"])
def test_main_command_failure_attempts_m5_and_closes_port(diagnostic_run, command):
    serial = diagnostic_run("--taps", "1", fail_command=command)

    with pytest.raises(RuntimeError, match="GRBL error"):
        solenoid.main()

    assert serial.writes[-1] == b"M5\n"
    assert serial.closed


def test_main_unacknowledged_activation_still_attempts_m5(diagnostic_run):
    serial = diagnostic_run(
        "--taps", "1", fail_command=b"M3 S1000\n", failure_reply=b""
    )

    with pytest.raises(RuntimeError, match="no reply to: M3 S1000"):
        solenoid.main()

    assert serial.writes[-1] == b"M5\n"
    assert serial.closed


def test_main_corrupt_activation_reply_still_attempts_m5(diagnostic_run):
    serial = diagnostic_run(
        "--taps", "1", fail_command=b"M3 S1000\n", failure_reply=b"o\xffk\n"
    )

    with pytest.raises(UnicodeDecodeError):
        solenoid.main()

    assert serial.writes[-1] == b"M5\n"
    assert serial.closed


def test_main_partial_activation_write_still_attempts_m5(diagnostic_run, mocker):
    serial = diagnostic_run("--taps", "1")
    write = serial.write

    def fail_after_activation(command):
        written = write(command)
        if command == b"M3 S1000\n":
            raise OSError("disconnected after activation write")
        return written

    mocker.patch.object(serial, "write", side_effect=fail_after_activation)

    with pytest.raises(OSError, match="disconnected after activation write"):
        solenoid.main()

    assert serial.writes[-2:] == [b"M3 S1000\n", b"M5\n"]
    assert serial.closed


def test_main_keep_on_interruption_attempts_m5_and_closes_port(diagnostic_run, mocker):
    serial = diagnostic_run("--keep-on", "5")
    mocker.patch.object(solenoid.time, "sleep", side_effect=[None, KeyboardInterrupt])

    with pytest.raises(KeyboardInterrupt):
        solenoid.main()

    assert serial.writes[-2:] == [b"M3 S1000\n", b"M5\n"]
    assert serial.closed


def test_main_long_dwell_keeps_response_margin_and_finishes_off(diagnostic_run):
    serial = diagnostic_run("--taps", "0", "--hold", "20", dwell_seconds=20)

    result = solenoid.main()

    assert result == 0
    assert serial.writes[-4:] == [b"M3 S150\n", b"G4 P20.000\n", b"M5\n", b"M5\n"]
    assert serial.clock.now >= 20
    assert serial.closed


def test_main_cleanup_failure_preserves_activation_error_and_closes_port(
    diagnostic_run,
):
    serial = diagnostic_run(
        "--taps", "1", fail_command=b"M3 S1000\n", cleanup_reply=b"error:9\n"
    )

    with pytest.raises(RuntimeError, match="M3 S1000.*error:23"):
        solenoid.main()

    assert serial.writes[-1] == b"M5\n"
    assert serial.closed


def test_main_keep_on_success_turns_off_before_return(diagnostic_run):
    serial = diagnostic_run("--keep-on", "5", "--laser")

    result = solenoid.main()

    assert result == 0
    assert serial.writes[-3:] == [b"M3 S1000\n", b"M5\n", b"M5\n"]
    assert b"$32=1\n" in serial.writes
    assert serial.closed


def test_main_zero_hold_skips_long_activation(diagnostic_run):
    serial = diagnostic_run("--taps", "0", "--hold", "0")

    result = solenoid.main()

    assert result == 0
    assert not any(command.startswith(b"M3") for command in serial.writes)
    assert serial.writes[-1] == b"M5\n"
    assert serial.closed


def test_dump_settings_continuous_unterminated_input_is_time_bounded(diagnostic_device):
    serial = diagnostic_device(
        solenoid, byte_delay=0.05, responder=lambda _: [b"x" * 1000]
    )

    solenoid.dump_settings(serial, duration=2)

    assert serial.clock.now == pytest.approx(2)
    assert serial.timeout == 3
    assert serial.writes == [b"$$\n"]
