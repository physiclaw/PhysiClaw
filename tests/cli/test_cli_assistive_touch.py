"""Standalone startup and graceful handoff, without loading the main server."""

import errno
import importlib
import sys
from contextlib import closing
from http.client import HTTPConnection

import pytest
import typer
from typer.testing import CliRunner

from physiclaw.cli import _assistive_touch_http as http
from physiclaw.cli import _service_handoff as handoff
from physiclaw.common import terminal
from tests.terminal_fakes import Console
from tests.timing_helpers import WAIT_TIMEOUT

command = importlib.import_module("physiclaw.cli.assistive_touch")
app = typer.Typer()
app.command()(command.test_assistive_touch)
runner = CliRunner()


@pytest.fixture
def service(mocker):
    mocker.patch.object(command.runtime_state, "read_live", return_value=None)
    listener = mocker.patch.object(http, "open_server")
    connection = mocker.patch.object(
        http, "connection_info", return_value={"port": 8124}
    )
    serve = mocker.patch.object(
        command, "_serve", side_effect=lambda servers, started: started()
    )
    browser = mocker.patch.object(command.webbrowser, "open", return_value=True)
    write = mocker.patch.object(command.runtime_state, "write")
    clear = mocker.patch.object(command.runtime_state, "clear")
    return {
        "connection": connection,
        "listener": listener,
        "serve": serve,
        "browser": browser,
        "write": write,
        "clear": clear,
    }


def test_standalone_starts_both_listeners_and_opens_page(service):
    result = runner.invoke(app, ["--port", "8123"])
    assert result.exit_code == 0, result.output
    calls = service["listener"].call_args_list
    assert [c.args[:2] for c in calls] == [("127.0.0.1", 8123), ("0.0.0.0", 8124)]
    assert calls[0].args[2] is calls[1].args[2]
    assert [c.kwargs for c in calls] == [{"desktop": True}, {"desktop": False}]
    service["connection"].assert_called_once_with(8124)
    assert calls[0].args[2].connection == {"port": 8124}
    service["browser"].assert_called_once_with(
        "http://127.0.0.1:8123/test-assistive-touch"
    )
    service["write"].assert_called_once_with(
        "127.0.0.1", 8123, no_runtime=True, service="test-assistive-touch"
    )
    service["clear"].assert_called_once_with()


def test_stops_running_server_before_reusing_its_ports(service, mocker):
    events = []
    mocker.patch.object(
        command.runtime_state, "read_live", return_value={"pid": 42, "port": 9123}
    )
    mocker.patch.object(
        handoff.processes, "remote_shutdown_supported", return_value=True
    )
    mocker.patch.object(
        handoff.processes,
        "request_shutdown",
        side_effect=lambda _: events.append("stop"),
    )
    mocker.patch.object(handoff.processes, "pid_alive", side_effect=[True, True, False])
    mocker.patch.object(
        handoff.time, "sleep", side_effect=lambda _: events.append("wait")
    )
    service["listener"].side_effect = lambda *a, **kw: (
        events.append("bind") or mocker.MagicMock()
    )
    result = runner.invoke(app, ["--no-open"])
    assert result.exit_code == 0, result.output
    assert events == ["stop", "wait", "bind", "bind"]
    assert service["listener"].call_args_list[0].args[:2] == ("127.0.0.1", 9123)
    service["browser"].assert_not_called()


@pytest.mark.parametrize("failure", ["unsupported", "timeout", "signal"])
def test_does_not_bind_or_open_page_if_shutdown_fails(service, mocker, failure):
    mocker.patch.object(
        command.runtime_state, "read_live", return_value={"pid": 42, "port": 9123}
    )
    mocker.patch.object(handoff.processes, "pid_alive", return_value=True)
    mocker.patch.object(
        handoff.processes,
        "remote_shutdown_supported",
        return_value=failure != "unsupported",
    )
    mocker.patch.object(
        handoff.processes,
        "request_shutdown",
        side_effect=PermissionError() if failure == "signal" else None,
    )
    mocker.patch.object(handoff, "STOP_TIMEOUT_SECONDS", 0)
    result = runner.invoke(app, [])
    assert result.exit_code == 1
    service["listener"].assert_not_called()
    service["browser"].assert_not_called()
    service["write"].assert_not_called()


def test_explicit_port_still_stops_running_server(service, mocker):
    live = {"pid": 42, "port": 9123}
    mocker.patch.object(command.runtime_state, "read_live", return_value=live)
    stop = mocker.patch.object(command, "stop_service")
    assert runner.invoke(app, ["--port", "8123"]).exit_code == 0
    stop.assert_called_once_with(live)
    assert service["listener"].call_args_list[0].args[:2] == ("127.0.0.1", 8123)


def test_handoff_notice_supports_ascii_terminals(monkeypatch, mocker):
    stream = Console("ascii", tty=False)
    monkeypatch.setattr(sys, "stdout", stream)
    mocker.patch.object(handoff.processes, "pid_alive", side_effect=[True, False])
    mocker.patch.object(
        handoff.processes, "remote_shutdown_supported", return_value=True
    )
    shutdown = mocker.patch.object(handoff.processes, "request_shutdown")

    handoff.stop_service({"pid": 42})

    shutdown.assert_called_once_with(42)
    assert "Stopping PhysiClaw server (pid 42)" in stream.getvalue()


def test_ctrl_c_cleans_up_service_record(service):
    service["serve"].side_effect = KeyboardInterrupt
    assert runner.invoke(app, ["--no-open"]).exit_code == 0
    service["clear"].assert_called_once_with()


def test_second_port_failure_releases_first_and_does_not_publish(service, mocker):
    first = mocker.MagicMock()
    service["listener"].side_effect = [first, OSError("port busy")]
    result = runner.invoke(app, [])
    assert result.exit_code == 1
    assert "port busy" in result.output
    first.__exit__.assert_called_once()
    service["write"].assert_not_called()
    service["browser"].assert_not_called()


def test_concurrent_start_is_refused_before_binding(service):
    with command.exclusive_file_lock(
        command.paths.runtime_state_file().with_suffix(".lock")
    ):
        assert runner.invoke(app, []).exit_code == 1
    service["listener"].assert_not_called()


@pytest.mark.parametrize(
    "failure", [None, OSError("no browser"), command.webbrowser.Error("no browser")]
)
def test_browser_failure_prints_manual_url(service, failure):
    service["browser"].return_value = False
    service["browser"].side_effect = failure
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Open in your browser: http://127.0.0.1:" in result.output
    assert "/test-assistive-touch" in result.output


def test_port_must_leave_room_for_phone_bridge(service):
    assert runner.invoke(app, ["--port", "65535"]).exit_code == 2
    service["listener"].assert_not_called()


@pytest.fixture
def assistive_touch_listeners(monkeypatch):
    # An address already in use must fail even on macOS with a wildcard bind.
    monkeypatch.setattr(http.TestHTTPServer, "allow_reuse_address", False)
    session = http.TestSession()
    for _ in range(100):
        with http.TestHTTPServer(("127.0.0.1", 0), session, desktop=True) as desktop:
            port = desktop.server_port
            if port == 65535:
                continue
            try:
                phone = http.TestHTTPServer(
                    ("0.0.0.0", port + 1), session, desktop=False
                )
            except OSError as error:
                # Windows can also reject the neighboring port as excluded.
                if error.errno != errno.EADDRINUSE and getattr(
                    error, "winerror", None
                ) not in {10013, 10048}:
                    raise
                continue
            with phone:
                yield desktop, phone
                return
    pytest.fail("Could not bind adjacent AssistiveTouch listeners")


def test_real_command_serves_both_ports_and_releases_them_on_interrupt(
    mocker, assistive_touch_listeners
):
    # Pass the real listeners to the command without a close/rebind window.
    desktop, phone = assistive_touch_listeners
    port = desktop.server_port
    mocker.patch.object(http, "TestSession", return_value=desktop.session)
    mocker.patch.object(http, "open_server", side_effect=[desktop, phone])
    mocker.patch.object(
        http,
        "connection_info",
        return_value={
            "hostname": "desktop.local",
            "ip": "192.168.1.42",
            "port": port + 1,
        },
    )

    def request(number, method, path, body=None):
        with closing(
            HTTPConnection("127.0.0.1", number, timeout=WAIT_TIMEOUT)
        ) as client:
            client.request(method, path, body)
            response = client.getresponse()
            assert response.status == 200
            return response.read()

    requests_completed = False

    def ready(url, connection, open_browser):
        nonlocal requests_completed
        live = command.runtime_state.read_live()
        assert live is not None
        assert live["service"] == "test-assistive-touch"
        assert live["port"] == port
        assert b"AssistiveTouch Test" in request(port, "GET", http.PAGE_PATH)
        code = request(port + 1, "GET", "/api/bridge/clipboard")
        image = b"\x89PNG\r\n\x1a\ncurrent screenshot"
        reply = request(port + 1, "POST", "/api/bridge/screenshot", image)
        assert reply == b"Screenshot uploaded.\nCode: " + code
        assert request(port, "GET", http.API_PATH + "/screenshot/1") == image
        requests_completed = True
        raise KeyboardInterrupt

    mocker.patch.object(command, "_open_page", side_effect=ready)
    result = runner.invoke(app, ["--port", str(port), "--no-open"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert requests_completed, "command exited before both listeners served requests"
    assert command.runtime_state.read_live() is None
    assert not command.paths.runtime_state_file().exists()
    # Closed accepted connections can still reserve the address in TIME_WAIT.
    assert desktop.socket.fileno() == phone.socket.fileno() == -1


@pytest.mark.parametrize("encoding", ["utf-8", "cp1252", "ascii"])
@pytest.mark.parametrize("mode", ["color", "no_color", "pipe", "no_vt"])
def test_connection_output_supports_native_console_modes(monkeypatch, encoding, mode):
    if sys.platform == "win32":
        from colorama import ansitowin32

        monkeypatch.setattr(ansitowin32, "winapi_test", lambda: False)
    stream = Console(encoding, tty=mode != "pipe")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    if mode == "no_color":
        monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.setattr(
        terminal.platform, "enable_terminal_color", lambda stream: mode != "no_vt"
    )

    command._open_page(
        "http://127.0.0.1:8123/test-assistive-touch",
        {"hostname": "desktop.local", "ip": "192.168.1.42", "port": 8124},
        False,
    )
    output = stream.getvalue()
    assert ("\x1b[" in output) is (mode == "color")
    plain = terminal.ANSI_RE.sub("", output)
    assert "http://desktop.local:8124/api/bridge/screenshot" in plain
    assert "http://desktop.local:8124/api/bridge/clipboard" in plain
    assert next(line for line in plain.splitlines() if "Host name" in line).endswith(
        "desktop"
    )
    assert "192.168.1.42" in plain
    assert "127.0.0.1" not in plain
