"""Tests for `physiclaw.core.bridge.lan` — LAN address helpers.

Network calls (`socket.socket`, `subprocess.run`, `socket.gethostbyname`)
are mocked so tests don't hit the real network.
"""
from __future__ import annotations

import socket
import subprocess
from unittest.mock import MagicMock

import pytest

from physiclaw.core.bridge import lan


# ---------- get_lan_ip ----------


def test_get_lan_ip_returns_local_ip_on_success(mocker) -> None:
    fake_sock = MagicMock()
    fake_sock.getsockname.return_value = ("192.168.1.42", 12345)
    mocker.patch.object(lan.socket, "socket", return_value=fake_sock)

    assert lan.get_lan_ip() == "192.168.1.42"


def test_get_lan_ip_falls_back_to_loopback_on_socket_error(mocker) -> None:
    fake_sock = MagicMock()
    fake_sock.connect.side_effect = OSError("network unreachable")
    mocker.patch.object(lan.socket, "socket", return_value=fake_sock)

    assert lan.get_lan_ip() == "127.0.0.1"


def test_get_lan_ip_falls_back_to_loopback_on_timeout(mocker) -> None:
    fake_sock = MagicMock()
    fake_sock.connect.side_effect = socket.timeout()
    mocker.patch.object(lan.socket, "socket", return_value=fake_sock)

    assert lan.get_lan_ip() == "127.0.0.1"


# ---------- get_mdns_host ----------


def test_get_mdns_host_returns_lowercased_local_when_scutil_succeeds(mocker) -> None:
    fake_result = MagicMock(returncode=0, stdout="My-Mac\n")
    mocker.patch.object(subprocess, "run", return_value=fake_result)
    # Resolve OK.
    mocker.patch.object(socket, "gethostbyname", return_value="192.168.1.5")

    assert lan.get_mdns_host() == "my-mac.local"


def test_get_mdns_host_returns_none_when_scutil_returns_empty(mocker) -> None:
    fake_result = MagicMock(returncode=0, stdout="\n")
    mocker.patch.object(subprocess, "run", return_value=fake_result)
    mocker.patch.object(
        socket, "gethostname", return_value=""
    )

    assert lan.get_mdns_host() is None


def test_get_mdns_host_falls_back_to_hostname_when_scutil_missing(
    mocker,
) -> None:
    mocker.patch.object(subprocess, "run", side_effect=FileNotFoundError)
    mocker.patch.object(socket, "gethostname", return_value="fallback-name")
    mocker.patch.object(socket, "gethostbyname", return_value="10.0.0.1")

    assert lan.get_mdns_host() == "fallback-name.local"


def test_get_mdns_host_falls_back_when_scutil_times_out(mocker) -> None:
    mocker.patch.object(
        subprocess, "run",
        side_effect=subprocess.TimeoutExpired(cmd="scutil", timeout=1),
    )
    mocker.patch.object(socket, "gethostname", return_value="other")
    mocker.patch.object(socket, "gethostbyname", return_value="10.0.0.1")

    assert lan.get_mdns_host() == "other.local"


def test_get_mdns_host_returns_none_when_resolution_fails(mocker) -> None:
    fake_result = MagicMock(returncode=0, stdout="my-mac")
    mocker.patch.object(subprocess, "run", return_value=fake_result)
    mocker.patch.object(socket, "gethostbyname", side_effect=socket.gaierror)

    assert lan.get_mdns_host() is None


def test_get_mdns_host_strips_dotted_suffix_from_socket_hostname(mocker) -> None:
    fake_result = MagicMock(returncode=1)
    mocker.patch.object(subprocess, "run", return_value=fake_result)
    mocker.patch.object(socket, "gethostname", return_value="host.example.com")
    mocker.patch.object(socket, "gethostbyname", return_value="10.0.0.1")

    assert lan.get_mdns_host() == "host.local"


def test_get_mdns_host_returns_none_when_hostname_call_raises(mocker) -> None:
    mocker.patch.object(subprocess, "run", side_effect=FileNotFoundError)
    mocker.patch.object(socket, "gethostname", side_effect=OSError)

    assert lan.get_mdns_host() is None


# ---------- bridge_base_urls ----------


def test_bridge_base_urls_uses_mdns_when_available(mocker) -> None:
    mocker.patch.object(lan, "get_lan_ip", return_value="192.168.1.10")
    mocker.patch.object(lan, "get_mdns_host", return_value="mac.local")

    primary, fallback = lan.bridge_base_urls(8048)

    assert primary == "http://mac.local:8048"
    assert fallback == "http://192.168.1.10:8048"


def test_bridge_base_urls_primary_equals_fallback_when_no_mdns(mocker) -> None:
    mocker.patch.object(lan, "get_lan_ip", return_value="10.0.0.1")
    mocker.patch.object(lan, "get_mdns_host", return_value=None)

    primary, fallback = lan.bridge_base_urls(8048)

    assert primary == fallback == "http://10.0.0.1:8048"


def test_bridge_base_urls_default_port_is_8048() -> None:
    import inspect

    sig = inspect.signature(lan.bridge_base_urls)
    assert sig.parameters["port"].default == 8048


def test_bridge_base_urls_uses_custom_port(mocker) -> None:
    mocker.patch.object(lan, "get_lan_ip", return_value="10.0.0.1")
    mocker.patch.object(lan, "get_mdns_host", return_value=None)

    primary, _ = lan.bridge_base_urls(9999)

    assert primary == "http://10.0.0.1:9999"
