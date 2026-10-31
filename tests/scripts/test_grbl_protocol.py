"""Shared wire contracts exercised through both diagnostic scripts."""

import pytest


def test_send_empty_early_read_does_not_discard_later_reply(
    diagnostic_script, diagnostic_device
):
    serial = diagnostic_device(diagnostic_script, [b"", b"ok\n"])

    diagnostic_script.send(serial, "G90")

    assert serial.clock.now == 3
    assert serial.timeout == 3


def test_send_bounds_a_port_without_a_native_timeout(
    diagnostic_script, diagnostic_device
):
    serial = diagnostic_device(diagnostic_script)
    serial.timeout = None

    with pytest.raises(RuntimeError, match="no reply to"):
        diagnostic_script.send(serial, "G90", timeout=0.25)

    assert serial.read_timeouts == [0.25]
    assert serial.timeout is None


def test_query_status_zero_budget_does_not_read(diagnostic_script, diagnostic_device):
    serial = diagnostic_device(diagnostic_script)

    status = diagnostic_script.query_status(serial, timeout=0)

    assert status == ""
    assert serial.read_sizes == []


def test_send_reassembles_reply_and_uses_one_line_ending(
    diagnostic_script, diagnostic_device
):
    serial = diagnostic_device(diagnostic_script, [b"[MSG:ready]\r\n", b"o", b"k\n"])

    diagnostic_script.send(serial, "G90")

    assert serial.writes == [b"G90\n"]
    assert serial.timeout == 3
    assert serial.timeout_updates == []


@pytest.mark.parametrize("reply", [b"error:3\n", b"error: 162\n"])
def test_send_optional_accepts_only_known_unsupported_settings(
    diagnostic_script, diagnostic_device, reply
):
    serial = diagnostic_device(diagnostic_script, [reply])

    diagnostic_script.send(serial, "$32=0", optional=True)

    assert serial.writes == [b"$32=0\n"]


@pytest.mark.parametrize(
    ("reply", "optional", "message"),
    [
        (b"error:3\n", False, "GRBL error"),
        (b"error:23\n", True, "GRBL error"),
        (b"ALARM:1\n", True, "GRBL alarm"),
    ],
)
def test_send_controller_failure_is_not_swallowed(
    diagnostic_script, diagnostic_device, reply, optional, message
):
    serial = diagnostic_device(diagnostic_script, [reply])

    with pytest.raises(RuntimeError, match=message):
        diagnostic_script.send(serial, "G90", optional=optional)


@pytest.mark.parametrize("reply", [b"[MSG:waiting]\n" * 100, b"x" * 1000])
def test_regression_send_continuous_bytes_cannot_extend_deadline(
    diagnostic_script, diagnostic_device, reply
):
    serial = diagnostic_device(diagnostic_script, [reply], byte_delay=0.25)

    with pytest.raises(RuntimeError, match="no reply to"):
        diagnostic_script.send(serial, "G90")

    assert serial.clock.now == pytest.approx(12)
    assert set(serial.read_sizes) == {1}
    assert serial.timeout == 3


def test_send_unterminated_reply_has_bounded_size(diagnostic_script, diagnostic_device):
    serial = diagnostic_device(diagnostic_script, [b"x" * 4096])

    with pytest.raises(RuntimeError, match="reply exceeds 4096 bytes"):
        diagnostic_script.send(serial, "G90")

    assert len(serial.read_sizes) == 4096
    assert serial.timeout == 3


def test_send_native_read_uses_remaining_budget(diagnostic_script, diagnostic_device):
    serial = diagnostic_device(diagnostic_script)

    with pytest.raises(RuntimeError, match="no reply to"):
        diagnostic_script.send(serial, "G90", timeout=0.25)

    assert serial.read_timeouts == [0.25]
    assert serial.timeout == 3
    assert serial.timeout_updates == [0.25, 3]


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_send_read_failure_restores_port_timeout(
    diagnostic_script, diagnostic_device, mocker, error
):
    serial = diagnostic_device(diagnostic_script)
    mocker.patch.object(serial, "read", side_effect=error("disconnected"))

    with pytest.raises(error, match="disconnected"):
        diagnostic_script.send(serial, "G90", timeout=0.25)

    assert serial.timeout == 3
    assert serial.timeout_updates == [0.25, 3]


def test_send_corrupt_acknowledgment_is_not_accepted(
    diagnostic_script, diagnostic_device
):
    serial = diagnostic_device(diagnostic_script, [b"o\xffk\n", b"ok\n"])

    with pytest.raises(UnicodeDecodeError):
        diagnostic_script.send(serial, "G90", timeout=0.25)

    assert list(serial.replies) == [b"ok\n"]
    assert serial.timeout == 3
    assert serial.timeout_updates == [0.25, 3]


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_send_invalid_timeout_never_writes(
    diagnostic_script, diagnostic_device, timeout
):
    serial = diagnostic_device(diagnostic_script)

    with pytest.raises(ValueError, match="finite and positive"):
        diagnostic_script.send(serial, "G90", timeout=timeout)

    assert serial.writes == []


@pytest.mark.parametrize("reply", [b"<Idle", b"<Idle|WPos:1,2"])
def test_query_status_rejects_incomplete_report(
    diagnostic_script, diagnostic_device, reply
):
    serial = diagnostic_device(diagnostic_script, [reply])

    result = diagnostic_script.query_status(serial)

    assert result == ""
    assert serial.clock.now == pytest.approx(3)
