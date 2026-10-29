"""Standalone startup and graceful handoff, without loading the main server."""

import importlib

import pytest
import typer
from typer.testing import CliRunner

from physiclaw.cli import _assistive_touch_http as http
from physiclaw.cli import _service_handoff as handoff

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
    return dict(
        connection=connection,
        listener=listener,
        serve=serve,
        browser=browser,
        write=write,
        clear=clear,
    )


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


def test_browser_failure_prints_manual_url(service):
    service["browser"].return_value = False
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Open the URL above" in result.output


def test_port_must_leave_room_for_phone_bridge(service):
    assert runner.invoke(app, ["--port", "65535"]).exit_code == 2
    service["listener"].assert_not_called()
