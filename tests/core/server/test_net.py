"""Startup probes resolve IPv4/IPv6 addresses against one connection deadline.

Socket and clock boundaries are fake here; local IPv6 integration is also
checked separately without requiring a listener in the fast test suite.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from physiclaw.core.server import net
from physiclaw.core.server.net import wait_for_port


def _probe(mocker, *, failures=0, latency=0):
    clock = SimpleNamespace(now=0.0)
    sock = mocker.MagicMock()
    sock.__enter__.return_value = sock
    attempts = []
    sleeps = []

    def connect(address):
        nonlocal failures
        timeout = sock.settimeout.call_args.args[0]
        attempts.append((address, timeout))
        clock.now += min(latency, timeout)
        if failures:
            failures -= 1
            raise OSError("not listening")
        return sock

    def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    mocker.patch.object(net.time, "monotonic", side_effect=lambda: clock.now)
    mocker.patch.object(net.time, "sleep", side_effect=sleep)

    def resolve(host, port, type):
        family = net.socket.AF_INET6 if ":" in host else net.socket.AF_INET
        return [(family, type, 0, "", (host, port))]

    mocker.patch.object(net.socket, "getaddrinfo", side_effect=resolve)
    mocker.patch.object(net.socket, "socket", return_value=sock)
    sock.connect.side_effect = connect
    return SimpleNamespace(clock=clock, sock=sock, attempts=attempts, sleeps=sleeps)


@pytest.mark.parametrize(
    ("host", "expected", "family"),
    [
        ("127.0.0.1", "127.0.0.1", net.socket.AF_INET),
        ("0.0.0.0", "127.0.0.1", net.socket.AF_INET),
        ("", "127.0.0.1", net.socket.AF_INET),
        ("::", "::1", net.socket.AF_INET6),
        ("::1", "::1", net.socket.AF_INET6),
        ("device.local", "device.local", net.socket.AF_INET),
    ],
)
def test_wait_for_port_probes_dialable_address_and_closes_connection(
    mocker, host, expected, family
):
    probe = _probe(mocker)

    result = wait_for_port(host, 8048, timeout=1)

    assert result is True
    assert probe.attempts == [((expected, 8048), min(net.PORT_WAIT_CONNECT_TIMEOUT, 1))]
    net.socket.socket.assert_called_once_with(family, net.socket.SOCK_STREAM, 0)
    probe.sock.__exit__.assert_called_once()


def test_wait_for_port_retries_refused_connection_until_listener_starts(mocker):
    probe = _probe(mocker, failures=1)

    result = wait_for_port("localhost", 8048, timeout=2)

    assert result is True
    assert len(probe.attempts) == 2
    assert probe.sleeps == [net.PORT_WAIT_INTERVAL]


@pytest.mark.parametrize("timeout", [0, -1])
def test_wait_for_port_elapsed_deadline_does_not_connect(mocker, timeout):
    probe = _probe(mocker)

    result = wait_for_port("localhost", 8048, timeout=timeout)

    assert result is False
    assert probe.attempts == []


def test_wait_for_port_limits_final_connect_to_remaining_time(mocker):
    probe = _probe(mocker, failures=10, latency=10)

    result = wait_for_port("::1", 8048, timeout=0.01)

    assert result is False
    assert probe.attempts == [(("::1", 8048), 0.01)]
    assert probe.clock.now == pytest.approx(0.01)


def test_wait_for_port_limits_final_sleep_to_remaining_time(mocker):
    probe = _probe(mocker, failures=10)

    result = wait_for_port("::1", 8048, timeout=0.01)

    assert result is False
    assert probe.sleeps == [0.01]
    assert probe.clock.now == pytest.approx(0.01)


def test_regression_wait_for_port_addresses_share_one_deadline(mocker):
    probe = _probe(mocker, failures=10, latency=10)
    net.socket.getaddrinfo.side_effect = None
    net.socket.getaddrinfo.return_value = [
        (net.socket.AF_INET6, net.socket.SOCK_STREAM, 0, "", ("::1", 8048)),
        (net.socket.AF_INET, net.socket.SOCK_STREAM, 0, "", ("127.0.0.1", 8048)),
    ]

    result = wait_for_port("localhost", 8048, timeout=0.01)

    assert result is False
    assert probe.attempts == [(("::1", 8048), 0.01)]
    assert probe.clock.now == pytest.approx(0.01)


def test_wait_for_port_falls_back_to_second_address_without_sleep(mocker):
    probe = _probe(mocker, failures=1)
    net.socket.getaddrinfo.side_effect = None
    net.socket.getaddrinfo.return_value = [
        (net.socket.AF_INET6, net.socket.SOCK_STREAM, 0, "", ("::1", 8048)),
        (net.socket.AF_INET, net.socket.SOCK_STREAM, 0, "", ("127.0.0.1", 8048)),
    ]

    result = wait_for_port("localhost", 8048, timeout=1)

    assert result is True
    assert probe.attempts[-1][0] == ("127.0.0.1", 8048)
    assert probe.sleeps == []


def test_wait_for_port_dns_failure_retries_until_deadline(mocker):
    probe = _probe(mocker)
    net.socket.getaddrinfo.side_effect = OSError("DNS unavailable")

    result = wait_for_port("device.local", 8048, timeout=0.01)

    assert result is False
    assert probe.attempts == []
    assert probe.clock.now == pytest.approx(0.01)


@settings(max_examples=25, derandomize=True)
@given(timeout=st.floats(min_value=0.001, max_value=2, allow_nan=False))
def test_wait_for_port_failed_attempts_respect_budget(timeout):
    from contextlib import contextmanager
    from unittest.mock import patch

    clock = SimpleNamespace(now=0.0)

    def refuse(address):
        clock.now += clock.connect_timeout
        raise OSError("refused")

    def settimeout(timeout):
        clock.connect_timeout = timeout

    @contextmanager
    def connection(*args):
        yield SimpleNamespace(connect=refuse, settimeout=settimeout)

    def sleep(seconds):
        clock.now += seconds

    with (
        patch.object(net.time, "monotonic", side_effect=lambda: clock.now),
        patch.object(net.time, "sleep", side_effect=sleep),
        patch.object(net.socket, "socket", side_effect=connection),
        patch.object(
            net.socket, "getaddrinfo", return_value=[(10, 1, 0, "", ("::1", 8048))]
        ),
    ):
        result = wait_for_port("::1", 8048, timeout=timeout)

    assert result is False
    assert clock.now == pytest.approx(timeout)
