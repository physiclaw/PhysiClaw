"""Tests for `physiclaw.cli.server`.

Exercise the public server command with isolated process, hardware, and ASGI
boundaries. Lifecycle tests run real threads to cover startup and shutdown.
"""

from __future__ import annotations

import importlib
import logging
import socket
import subprocess
import threading
from unittest.mock import MagicMock

import pytest
import typer
from typer.testing import CliRunner

server_mod = importlib.import_module("physiclaw.cli.server")

runner = CliRunner()

# Mutation audit: 180 generated cases, 133 killed plus one detected timeout.
# The 46 accepted survivors only decorate diagnostics, omit debug traceback
# detail, substitute equivalent falsey values/an unused unset-model label, or
# vary polling intervals. All 14 additional lifecycle omission cases are caught.


# ---------- _spawn_runtime ----------


def test_spawn_runtime_builds_cmd_with_port_and_label(mocker) -> None:
    fake_proc = MagicMock(pid=4242)
    spy = mocker.patch.object(server_mod.subprocess, "Popen", return_value=fake_proc)

    proc = server_mod._spawn_runtime(
        "127.0.0.1",
        8048,
        verbose=False,
        label="engine=physiclaw, provider=anthropic, model=test",
    )

    assert proc is fake_proc
    args = spy.call_args.args[0]
    # First arg is sys.executable; remaining must include module + server flag.
    assert args[:2] == [server_mod.sys.executable, "-m"]
    assert "physiclaw.agent.runtime" in args
    assert "--server" in args
    assert "http://127.0.0.1:8048" in args
    assert "--verbose" not in args


def test_spawn_runtime_dials_configured_host(mocker) -> None:
    # With --host set to a LAN IP nothing listens on loopback — the runtime
    # subprocess must dial the configured bind.
    spy = mocker.patch.object(
        server_mod.subprocess, "Popen", return_value=MagicMock(pid=1)
    )

    server_mod._spawn_runtime("192.168.1.5", 8048, verbose=False, label="engine=x")

    assert "http://192.168.1.5:8048" in spy.call_args.args[0]


@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::"])
def test_spawn_runtime_maps_wildcard_bind_to_loopback(mocker, wildcard: str) -> None:
    # A wildcard bind can't be dialed, but it listens on loopback — dial that.
    spy = mocker.patch.object(
        server_mod.subprocess, "Popen", return_value=MagicMock(pid=1)
    )

    server_mod._spawn_runtime(wildcard, 8048, verbose=False, label="engine=x")

    assert "http://127.0.0.1:8048" in spy.call_args.args[0]


def test_spawn_runtime_passes_verbose_flag(mocker) -> None:
    spy = mocker.patch.object(
        server_mod.subprocess,
        "Popen",
        return_value=MagicMock(pid=1),
    )

    server_mod._spawn_runtime("127.0.0.1", 9000, verbose=True, label="engine=x")

    assert "--verbose" in spy.call_args.args[0]


def test_spawn_runtime_logs_label(
    mocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mocker.patch.object(
        server_mod.subprocess,
        "Popen",
        return_value=MagicMock(pid=99),
    )

    with caplog.at_level(logging.DEBUG, logger="physiclaw.cli.server"):
        server_mod._spawn_runtime("127.0.0.1", 8048, False, "engine=qwen")

    assert any("engine=qwen" in r.getMessage() for r in caplog.records)


# ---------- ready-line watcher ----------


def _run_ready_watcher(mocker, caplog, polls) -> list:
    """Drive `_announce_ready_when_up` with `check_ready_once` outcomes
    scripted (an Exception instance = probe fails; a bool = its return)
    and the watcher thread's target run inline. The script must end on
    True — the watcher polls forever by design."""
    probe = mocker.patch("physiclaw.common.ready.check_ready_once", side_effect=polls)
    stopping = server_mod.threading.Event()
    sleep_spy = mocker.patch.object(stopping, "wait")
    captured = {}

    def fake_thread(target, daemon=False):
        captured["target"] = target
        return MagicMock()

    mocker.patch.object(server_mod.threading, "Thread", side_effect=fake_thread)

    with caplog.at_level(logging.INFO, logger="physiclaw.cli.server"):
        server_mod._announce_ready_when_up("127.0.0.1", 8047, stopping=stopping)
        captured["target"]()
    probe.assert_called_with("http://127.0.0.1:8047")
    assert probe.call_count == len(polls)
    assert sleep_spy.call_count == len(polls) - 1  # no sleep after ready
    return list(caplog.records)


def test_ready_line_waits_for_status_ready(mocker, caplog) -> None:
    # Serving starts while hardware bring-up is still running — the line
    # must NOT land on port-accept, only when the shared ready probe
    # flips true (the same flag the built-in runtime polls before waking).
    records = _run_ready_watcher(
        mocker,
        caplog,
        [ConnectionError("refused"), False, True],
    )

    assert len(records) == 1
    msg = records[0].getMessage()
    assert "PhysiClaw ready" in msg and "http://127.0.0.1:8047/mcp" in msg


def test_ready_line_immediate_when_already_ready(mocker, caplog) -> None:
    records = _run_ready_watcher(mocker, caplog, [True])

    assert len(records) == 1


# ---------- server CLI invocation ----------


def _patch_server_runtime_deps(
    mocker,
    *,
    primary: str = "http://device.local:8048",
    fallback: str = "http://127.0.0.1:8048",
    resolve_raises: bool = False,
):
    """Stub out everything heavy `server()` touches once it starts up."""
    mocker.patch.object(server_mod, "_spawn_runtime", return_value=MagicMock())
    # The vision-model preflight would exit() — tests run without the model
    # installed. Its own behaviour is covered by the _require_vision_model tests.
    mocker.patch.object(server_mod, "_require_vision_model")
    mocker.patch("physiclaw.common.logger.setup_logging")
    # The startup auto-update / skills-sync hooks have their own tests and would
    # otherwise shell out / hit the network here — stub them to no-ops.
    mocker.patch("physiclaw.cli.update.notify_staged_update")
    mocker.patch("physiclaw.cli.update.start_stage_update_thread")
    mocker.patch("physiclaw.cli.sync_official_skills.maybe_auto_sync")
    # Never spawn real threads (warm-start / setup-wizard) or open a real
    # browser during tests. Tests that need a thread body re-patch Thread to
    # capture the target and invoke it manually.
    mocker.patch.object(server_mod.threading, "Thread")
    mocker.patch("webbrowser.open")

    # `_serve` is the process's serving boundary (two uvicorn servers) —
    # always mocked; tests assert against it instead of running sockets.
    fake_serve = mocker.patch.object(
        server_mod, "_serve", side_effect=lambda *args, **kw: kw["on_started"]()
    )

    fake_mcp = MagicMock()
    fake_shutdown = MagicMock()
    fake_warm = MagicMock()
    fake_warm.wait_for_port.return_value = True
    fake_warm.try_resume.return_value = True
    fake_state = MagicMock()
    fake_state.read_live.return_value = None  # no live server recorded
    # No real socket probe in tests — the already-running guard's branches
    # have their own tests below.
    mocker.patch.object(server_mod, "_port_in_use", return_value=False)
    listeners = []

    def create_listener(*args, **kwargs):
        listener = MagicMock()
        listener.__enter__.return_value = listener
        listeners.append(listener)
        return listener

    mocker.patch.object(server_mod.socket, "create_server", side_effect=create_listener)
    fake_launcher = MagicMock()
    if resolve_raises:
        from physiclaw.common.model_ref import _NO_MODEL_MSG

        fake_launcher.resolve.side_effect = RuntimeError(_NO_MODEL_MSG)
    else:
        fake_launcher.resolve.return_value = ("openai/gpt-5", "config")
    fake_launcher.engine_label.return_value = "engine=openai"
    bridge_lan = importlib.import_module("physiclaw.core.bridge.lan")
    fake_bridge = MagicMock()
    fake_bridge.bridge_base_urls.return_value = (primary, fallback)

    # Patch directly on parent packages so attribute lookups resolve to fakes.
    import physiclaw.common

    mocker.patch.object(physiclaw.common, "runtime_state", fake_state, create=True)
    fake_control_app, fake_bridge_app = MagicMock(), MagicMock()
    fake_core_server = MagicMock(
        mcp=fake_mcp,
        shutdown=fake_shutdown,
        warm_start=fake_warm,
    )
    fake_core_server.build_apps.return_value = (fake_control_app, fake_bridge_app)
    import physiclaw.core

    mocker.patch.object(physiclaw.core, "server", fake_core_server, create=True)
    # The submodules the server body imports must be faked EXPLICITLY:
    # `from physiclaw.core.server.app import ...` looks up
    # "physiclaw.core.server.app" in sys.modules first, and falls back to
    # importing through the parent — a MagicMock, which is "not a package".
    # (Unlisted, the tests only pass when the real module happens to be
    # imported already by an earlier test file — a full-suite-only pass.)
    fake_app = MagicMock()
    fake_core_server.app = fake_app
    mocker.patch.dict(
        "sys.modules",
        {
            "physiclaw.core.server": fake_core_server,
            "physiclaw.core.server.app": fake_app,
            "physiclaw.core.server.warm_start": fake_warm,
            "physiclaw.agent.runtime.launcher": fake_launcher,
            "physiclaw.core.bridge": fake_bridge,
            "physiclaw.core.bridge.lan": bridge_lan,
        },
    )

    return {
        "mcp": fake_mcp,
        "shutdown": fake_shutdown,
        "state": fake_state,
        "launcher": fake_launcher,
        "bridge": fake_bridge,
        "warm_start": fake_warm,
        "serve": fake_serve,
        "core_server": fake_core_server,
        "apps": (fake_control_app, fake_bridge_app),
        "listeners": listeners,
    }


def test_server_opens_setup_wizard_by_default(mocker) -> None:
    deps = _patch_server_runtime_deps(mocker)
    open_spy = mocker.patch("webbrowser.open")
    captured = {}

    def fake_thread(target, daemon=False):
        captured["target"] = target
        return MagicMock()

    mocker.patch.object(server_mod.threading, "Thread", side_effect=fake_thread)

    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=True,
        warm_start=False,
        cam_index=None,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )

    # The wizard opens only after the port is accepting connections.
    captured["target"]()
    deps["warm_start"].wait_for_port.assert_called_once()
    open_spy.assert_called_once_with("http://127.0.0.1:8048/setup-hardware")
    assert server_mod.threading.Thread.call_args.kwargs["daemon"] is True


def test_server_no_setup_hardware_skips_wizard(
    mocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patch_server_runtime_deps(mocker)
    open_spy = mocker.patch("webbrowser.open")
    thread_spy = mocker.patch.object(server_mod.threading, "Thread")

    with caplog.at_level(logging.INFO, logger="physiclaw.cli.server"):
        server_mod.server(
            port=8048,
            host="127.0.0.1",
            verbose=False,
            no_runtime=True,
            warm_start=False,
            cam_index=None,
            no_setup_hardware=True,
            save_tool_calls=False,
            save_snapshots=False,
            save_screenshots=False,
        )

    thread_spy.assert_not_called()  # no warm-start, no wizard → no threads
    open_spy.assert_not_called()
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "physiclaw setup hardware" in msgs
    assert (
        next(
            r for r in caplog.records if "physiclaw setup hardware" in r.getMessage()
        ).console
        is True
    )


def test_server_logs_version_first(mocker, caplog) -> None:
    # The version rides the same timestamped `[physiclaw]` log stream as
    # every other startup line (was a bare typer.echo, which rendered in a
    # different voice than the rest of the output).
    _patch_server_runtime_deps(mocker)

    with caplog.at_level(logging.INFO, logger="physiclaw.cli.server"):
        server_mod.server(
            port=8048,
            host="127.0.0.1",
            verbose=False,
            no_runtime=True,
            warm_start=False,
            cam_index=None,
            save_tool_calls=False,
            save_snapshots=False,
            save_screenshots=False,
        )

    from physiclaw import __version__

    messages = [r.getMessage() for r in caplog.records]
    assert f"PhysiClaw {__version__}" in messages
    assert (
        next(
            r for r in caplog.records if r.getMessage() == f"PhysiClaw {__version__}"
        ).console
        is True
    )


@pytest.mark.parametrize("no_runtime", [False, True])
def test_connection_notice_keeps_the_default_view_short(mocker, caplog, no_runtime):
    from physiclaw.common.log_style import ConsoleFilter, TaggedFormatter

    mocker.patch(
        "physiclaw.core.bridge.bridge_base_urls",
        return_value=("http://rig.local:8049", "http://192.168.1.25:8049"),
    )
    with caplog.at_level(logging.INFO, logger="physiclaw.cli.server"):
        server_mod._log_endpoints("127.0.0.1", 8048, no_runtime=no_runtime)
    visible = [r for r in caplog.records if ConsoleFilter(verbose=False).filter(r)]
    logs = visible[-1]
    normal = "\n".join(
        TaggedFormatter("physiclaw", False, verbose=False).format(record)
        for record in visible
    )
    diagnostic = "\n".join(
        TaggedFormatter("physiclaw", False, verbose=True).format(record)
        for record in caplog.records
    )
    assert "Phone: http://rig.local:8049/bridge" in normal
    assert "Logs:" in normal
    assert ("MCP: http://127.0.0.1:8048/mcp" in normal) is no_runtime
    assert all(
        text not in normal
        for text in ("cli.server", "QR code", "Fallback", "Connections")
    )
    assert "QR code" in diagnostic and "Fallback" in diagnostic
    assert logs.getMessage().startswith("Logs: ")
    assert normal.splitlines()[-1].endswith("[physiclaw] " + logs.getMessage())
    assert len(visible) == (3 if no_runtime else 2)
    assert all("[physiclaw] " in line for line in normal.splitlines())
    assert normal.count("Phone:") == diagnostic.count("Phone:") == 1
    assert normal.count("Logs:") == diagnostic.count("Logs:") == 1


def test_server_drops_uvicorn_invalid_http_noise(mocker) -> None:
    # Phones opening the bridge URL probe https:// first; the TLS bytes
    # against the plaintext port make uvicorn warn "Invalid HTTP request
    # received." per attempt. server() installs a message-targeted filter
    # on uvicorn.error — the noise is dropped, real warnings pass.
    uvicorn_logger = logging.getLogger("uvicorn.error")
    for f in list(uvicorn_logger.filters):  # isolate from prior test runs
        uvicorn_logger.removeFilter(f)
    _patch_server_runtime_deps(mocker)

    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=True,
        warm_start=False,
        cam_index=None,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )

    def record(msg: str) -> logging.LogRecord:
        return logging.LogRecord(
            "uvicorn.error",
            logging.WARNING,
            __file__,
            0,
            msg,
            None,
            None,
        )

    assert not uvicorn_logger.filter(record("Invalid HTTP request received."))
    assert uvicorn_logger.filter(record("ASGI application error"))


def test_server_default_invocation_serves_both_planes(mocker) -> None:
    deps = _patch_server_runtime_deps(mocker)

    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=True,
        warm_start=False,
        cam_index=None,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )

    # Both ASGI apps built for the control bind and handed to _serve.
    deps["core_server"].build_apps.assert_called_once_with("127.0.0.1")
    deps["serve"].assert_called_once_with(
        "127.0.0.1",
        8048,
        *deps["apps"],
        sockets=tuple(deps["listeners"]),
        announce_ready=True,
        runtime=None,
        on_started=mocker.ANY,
    )
    # State recorded with resolved model.
    deps["state"].write.assert_called_once()


def test_server_save_flags_set_env_vars(
    mocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_server_runtime_deps(mocker)
    monkeypatch.delenv("PHYSICLAW_SAVE_TOOL_CALLS", raising=False)
    monkeypatch.delenv("PHYSICLAW_SAVE_SNAPSHOTS", raising=False)
    monkeypatch.delenv("PHYSICLAW_SAVE_SCREENSHOTS", raising=False)

    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=True,
        warm_start=False,
        cam_index=None,
        save_tool_calls=True,
        save_snapshots=True,
        save_screenshots=True,
    )

    import os

    assert os.environ["PHYSICLAW_SAVE_TOOL_CALLS"] == "1"
    assert os.environ["PHYSICLAW_SAVE_SNAPSHOTS"] == "1"
    assert os.environ["PHYSICLAW_SAVE_SCREENSHOTS"] == "1"


@pytest.mark.parametrize("argv", [["mcp"], ["server", "--no-runtime"]])
def test_server_external_mode_skips_model_resolution(mocker, argv) -> None:
    from physiclaw.cli import app

    deps = _patch_server_runtime_deps(mocker, resolve_raises=True)

    result = runner.invoke(app, [*argv, "--host", "127.0.0.1", "--port", "8048"])

    assert result.exit_code == 0, result.output
    deps["launcher"].resolve.assert_not_called()
    deps["state"].write.assert_called_once_with("127.0.0.1", 8048, no_runtime=True)
    deps["serve"].assert_called_once()


@pytest.mark.parametrize("argv", [[], ["server"]])
def test_server_exits_before_startup_when_no_model_configured(mocker, argv) -> None:
    """Missing model settings stop startup with actionable setup instructions."""
    from physiclaw.cli import app

    deps = _patch_server_runtime_deps(mocker, resolve_raises=True)
    maintenance = mocker.patch.object(server_mod, "_run_startup_maintenance")

    result = runner.invoke(app, argv)

    assert result.exit_code == 1
    assert "Cannot start PhysiClaw: no model configured." in result.stderr
    assert "physiclaw models key <provider>" in result.stderr
    assert "physiclaw models use <provider>/<model>" in result.stderr
    assert "Traceback" not in result.output
    assert "starting hardware" not in result.output
    maintenance.assert_not_called()
    server_mod._require_vision_model.assert_not_called()
    server_mod.socket.create_server.assert_not_called()
    server_mod._spawn_runtime.assert_not_called()
    server_mod.threading.Thread.assert_not_called()
    deps["core_server"].build_apps.assert_not_called()
    deps["state"].write.assert_not_called()
    deps["state"].clear.assert_not_called()
    deps["serve"].assert_not_called()


def test_server_keyboard_interrupt_is_swallowed(mocker) -> None:
    deps = _patch_server_runtime_deps(mocker)
    deps["serve"].side_effect = KeyboardInterrupt

    # Should NOT raise.
    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=True,
        warm_start=False,
        cam_index=None,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )


def test_server_spawns_runtime_subprocess_by_default(mocker) -> None:
    _patch_server_runtime_deps(mocker)
    spawn_spy = mocker.patch.object(
        server_mod,
        "_spawn_runtime",
        return_value=MagicMock(),
    )

    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=True,
        no_runtime=False,
        warm_start=False,
        cam_index=None,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )

    spawn_spy.assert_called_once_with("127.0.0.1", 8048, True, "engine=openai")


def test_server_no_runtime_skips_subprocess(mocker) -> None:
    _patch_server_runtime_deps(mocker)
    spawn_spy = mocker.patch.object(server_mod, "_spawn_runtime")

    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=True,
        warm_start=False,
        cam_index=None,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )

    spawn_spy.assert_not_called()


@pytest.mark.parametrize("no_runtime, sync_calls", [(True, 0), (False, 1)])
def test_server_skills_sync_gated_on_runtime(
    mocker, no_runtime: bool, sync_calls: int
) -> None:
    # The official pack feeds the in-tree engine's prompt — startup
    # maintenance must fetch it only when a runtime will consume it.
    _patch_server_runtime_deps(mocker)
    sync_spy = mocker.patch("physiclaw.cli.sync_official_skills.maybe_auto_sync")

    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=no_runtime,
        warm_start=False,
        cam_index=None,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )

    assert sync_spy.call_count == sync_calls


def test_server_no_runtime_logs_external_client_hint(
    mocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # `physiclaw mcp` users land here — the /mcp endpoint line must say how
    # to point an external MCP client at it, not just print a bare URL.
    _patch_server_runtime_deps(mocker)

    with caplog.at_level(logging.INFO, logger="physiclaw.cli.server"):
        server_mod.server(
            port=8048,
            host="127.0.0.1",
            verbose=False,
            no_runtime=True,
            warm_start=False,
            cam_index=None,
            save_tool_calls=False,
            save_snapshots=False,
            save_screenshots=False,
        )

    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "claude mcp add --transport http physiclaw http://127.0.0.1:8048/mcp" in msgs


def test_server_logs_single_phone_url_when_no_mdns(
    mocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patch_server_runtime_deps(
        mocker,
        primary="http://127.0.0.1:8048",
        fallback="http://127.0.0.1:8048",
    )

    with caplog.at_level(logging.INFO, logger="physiclaw.cli.server"):
        server_mod.server(
            port=8048,
            host="127.0.0.1",
            verbose=False,
            no_runtime=True,
            warm_start=False,
            cam_index=None,
            save_tool_calls=False,
            save_snapshots=False,
            save_screenshots=False,
        )

    assert any("stable LocalHostName" in r.getMessage() for r in caplog.records)


def test_server_runtime_stop_terminates_subprocess(mocker) -> None:
    _patch_server_runtime_deps(mocker)
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # still running
    mocker.patch.object(server_mod, "_spawn_runtime", return_value=fake_proc)

    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=False,
        warm_start=False,
        cam_index=None,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )

    # Leaving the serving scope reaps the child immediately.
    fake_proc.terminate.assert_called_once()


def test_server_runtime_stop_kills_after_timeout(mocker) -> None:
    _patch_server_runtime_deps(mocker)
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_proc.wait.side_effect = [server_mod.subprocess.TimeoutExpired("cmd", 5), -9]
    mocker.patch.object(server_mod, "_spawn_runtime", return_value=fake_proc)

    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=False,
        warm_start=False,
        cam_index=None,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )

    fake_proc.terminate.assert_called_once()
    fake_proc.kill.assert_called_once()


def test_warm_start_thread_exits_on_port_timeout(mocker) -> None:
    deps = _patch_server_runtime_deps(mocker)
    deps["warm_start"].wait_for_port.return_value = False
    captured = {}

    def fake_thread(target, daemon=False):
        captured["target"] = target
        return MagicMock()

    mocker.patch.object(server_mod.threading, "Thread", side_effect=fake_thread)
    interrupt_spy = mocker.patch.object(server_mod._thread, "interrupt_main")

    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=True,
        warm_start=True,
        cam_index=None,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )

    # Run the thread body manually.
    captured["target"]()

    interrupt_spy.assert_called_once_with()


def test_warm_start_thread_exits_on_resume_failure(mocker) -> None:
    deps = _patch_server_runtime_deps(mocker)
    deps["warm_start"].try_resume.return_value = False
    captured = {}

    def fake_thread(target, daemon=False):
        captured["target"] = target
        return MagicMock()

    mocker.patch.object(server_mod.threading, "Thread", side_effect=fake_thread)
    interrupt_spy = mocker.patch.object(server_mod._thread, "interrupt_main")

    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=True,
        warm_start=True,
        cam_index=None,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )

    captured["target"]()

    interrupt_spy.assert_called_once_with()


@pytest.mark.parametrize(
    ("warm_start", "hot_start", "expected_verify"),
    [
        pytest.param(True, False, True, id="warm-start-verifies"),
        pytest.param(False, True, False, id="hot-start-skips-verify"),
        pytest.param(True, True, True, id="both-flags-verifying-mode-wins"),
    ],
)
def test_resume_thread_verify_mode(
    mocker, warm_start: bool, hot_start: bool, expected_verify: bool
) -> None:
    """--warm-start resumes with verify, --hot-start without; when both are
    passed the verifying mode wins."""
    deps = _patch_server_runtime_deps(mocker)
    captured = {}

    def fake_thread(target, daemon=False):
        captured["target"] = target
        return MagicMock()

    mocker.patch.object(server_mod.threading, "Thread", side_effect=fake_thread)
    interrupt_spy = mocker.patch.object(server_mod._thread, "interrupt_main")

    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=True,
        warm_start=warm_start,
        hot_start=hot_start,
        cam_index=None,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )

    captured["target"]()

    call = deps["warm_start"].try_resume.call_args
    assert call.args == (None,)
    assert call.kwargs["verify"] is expected_verify
    # The default assembly's state objects ride along as parameters.
    assert {"physiclaw", "calib", "phone"} <= set(call.kwargs)
    interrupt_spy.assert_not_called()


def test_warm_start_thread_exits_silently_when_resume_succeeds(mocker) -> None:
    deps = _patch_server_runtime_deps(mocker)
    deps["warm_start"].try_resume.return_value = True
    captured = {}

    def fake_thread(target, daemon=False):
        captured["target"] = target
        return MagicMock()

    mocker.patch.object(server_mod.threading, "Thread", side_effect=fake_thread)
    interrupt_spy = mocker.patch.object(server_mod._thread, "interrupt_main")

    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=True,
        warm_start=True,
        cam_index=None,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )

    captured["target"]()

    interrupt_spy.assert_not_called()


def test_server_warm_start_starts_thread(mocker) -> None:
    _patch_server_runtime_deps(mocker)
    thread_spy = mocker.patch.object(server_mod.threading, "Thread")

    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=True,
        warm_start=True,
        cam_index=None,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )

    thread_spy.assert_called_once()
    assert thread_spy.call_args.kwargs.get("daemon") is True
    thread_spy.return_value.start.assert_called_once()


# ---------- already-running guard ----------


def _server_kwargs() -> dict:
    return dict(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=True,
        warm_start=False,
        cam_index=None,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )


def test_server_refuses_when_live_server_recorded(mocker, capsys) -> None:
    """A second `physiclaw server` must exit BEFORE writing runtime state —
    its write+atexit-clear would otherwise erase the LIVE server's record."""
    deps = _patch_server_runtime_deps(mocker)
    deps["state"].read_live.return_value = {
        "pid": 4242,
        "host": "127.0.0.1",
        "port": 8048,
    }

    with pytest.raises(typer.Exit):
        server_mod.server(**_server_kwargs())

    deps["state"].write.assert_not_called()
    assert (
        "already running (pid 4242, http://127.0.0.1:8048)" in capsys.readouterr().err
    )


def test_server_refuses_when_port_probe_hits(mocker, capsys) -> None:
    # State file gone (SIGKILL'd server) but the port is still held.
    deps = _patch_server_runtime_deps(mocker)
    mocker.patch.object(server_mod, "_port_in_use", return_value=True)

    with pytest.raises(typer.Exit):
        server_mod.server(**_server_kwargs())

    deps["state"].write.assert_not_called()
    assert "already accepting connections" in capsys.readouterr().err


def test_port_in_use_probes_real_socket() -> None:
    import socket as socket_mod

    with socket_mod.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        # Backlog > 1: the probes never get accept()ed, so each occupies a
        # queue slot until the listener closes.
        srv.listen(5)
        port = srv.getsockname()[1]
        assert server_mod._port_in_use("127.0.0.1", port) is True
        # Wildcard bind address maps to a dialable loopback probe.
        assert server_mod._port_in_use("0.0.0.0", port) is True
    assert server_mod._port_in_use("127.0.0.1", port) is False


# ---------- auto_calibrate branch (physiclaw auto) ----------


def test_server_auto_calibrate_starts_thread_and_skips_wizard(mocker, caplog) -> None:
    from physiclaw.common.log_style import ConsoleFilter

    caplog.set_level(logging.INFO)
    _patch_server_runtime_deps(mocker)
    open_spy = mocker.patch("webbrowser.open")
    # The branch spawns a thread that lazily imports the setup module and
    # calls its worker — patch that worker where it lives.
    worker_spy = mocker.patch("physiclaw.cli.setup.hardware.await_bridge_and_calibrate")
    captured = {}

    def fake_thread(target, daemon=False):
        captured["target"] = target
        return MagicMock()

    mocker.patch.object(server_mod.threading, "Thread", side_effect=fake_thread)

    server_mod.server(
        port=8048,
        host="127.0.0.1",
        verbose=False,
        no_runtime=True,
        warm_start=False,
        cam_index=None,
        auto_calibrate=True,
        save_tool_calls=False,
        save_snapshots=False,
        save_screenshots=False,
    )

    open_spy.assert_not_called()  # no desktop wizard in auto mode
    captured["target"]()  # run the daemon-thread body
    worker_spy.assert_called_once_with("127.0.0.1", 8048)
    assert server_mod.threading.Thread.call_args.kwargs["daemon"] is True
    mode = next(r for r in caplog.records if r.getMessage().startswith("Auto mode:"))
    assert not ConsoleFilter(verbose=False).filter(mode)


# ---------- _require_vision_model ----------


def test_require_vision_model_exits_when_model_missing() -> None:
    # The autouse physiclaw_home fixture gives a fresh tmp HOME — no model file.
    with pytest.raises(typer.Exit):
        server_mod._require_vision_model()


def test_require_vision_model_passes_when_model_installed() -> None:
    from physiclaw.common import paths

    model = paths.omniparser_onnx()
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"onnx")

    server_mod._require_vision_model()  # no exit


def test_server_preflights_vision_model_regardless_of_runtime(mocker) -> None:
    # The preflight is unconditional — it runs even under --no-runtime.
    _patch_server_runtime_deps(mocker)  # mocks _require_vision_model

    for no_runtime in (True, False):
        server_mod._require_vision_model.reset_mock()
        server_mod.server(
            port=8048,
            host="127.0.0.1",
            verbose=False,
            no_runtime=no_runtime,
            warm_start=False,
            cam_index=None,
            save_tool_calls=False,
            save_snapshots=False,
            save_screenshots=False,
        )
        server_mod._require_vision_model.assert_called_once()


def test_regression_terminate_child_reaps_after_forced_kill(mocker) -> None:
    proc = mocker.Mock()
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired("child", 0.1), -9]

    server_mod.terminate_child(proc, timeout=0.1)

    assert proc.method_calls == [
        mocker.call.poll(),
        mocker.call.terminate(),
        mocker.call.wait(timeout=0.1),
        mocker.call.kill(),
        mocker.call.wait(timeout=0.1),
    ]


@pytest.mark.parametrize("port", [-1, 0, 65535, 65536])
def test_regression_server_rejects_invalid_port_before_startup(port, mocker, capsys):
    deps = _patch_server_runtime_deps(mocker)

    with pytest.raises(typer.Exit) as error:
        server_mod.server(port=port, no_runtime=True)

    assert error.value.exit_code == 2
    assert "65534" in capsys.readouterr().err
    deps["state"].write.assert_not_called()
    deps["core_server"].build_apps.assert_not_called()


def test_regression_second_listener_bind_failure_releases_first(mocker, capsys):
    deps = _patch_server_runtime_deps(mocker)
    control_socket = mocker.MagicMock()
    mocker.patch.object(
        server_mod.socket,
        "create_server",
        side_effect=[control_socket, OSError("address in use")],
    )

    with pytest.raises(typer.Exit) as error:
        server_mod.server(**_server_kwargs())

    assert error.value.exit_code == 1
    assert "address in use" in capsys.readouterr().err
    deps["state"].write.assert_not_called()
    control_socket.__exit__.assert_called_once()


def test_server_refuses_existing_loopback_bridge_listener_before_binding(
    mocker, capsys
):
    deps = _patch_server_runtime_deps(mocker)
    mocker.patch.object(server_mod, "_port_in_use", side_effect=[False, True])

    with pytest.raises(typer.Exit) as error:
        server_mod.server(**_server_kwargs())

    assert error.value.exit_code == 1
    assert "8049 is already accepting connections" in capsys.readouterr().err
    assert deps["listeners"] == []
    deps["state"].write.assert_not_called()


def test_regression_server_return_cleans_child_hardware_and_state(mocker):
    deps = _patch_server_runtime_deps(mocker)
    proc = mocker.Mock()
    proc.poll.return_value = None
    mocker.patch.object(server_mod, "_spawn_runtime", return_value=proc)

    server_mod.server(port=8048, host="127.0.0.1", no_runtime=False)

    proc.terminate.assert_called_once()
    proc.wait.assert_called_once_with(timeout=5)
    deps["shutdown"].assert_called_once()
    deps["state"].clear.assert_called_once()


def test_regression_setup_wizard_dials_configured_bind(mocker):
    _patch_server_runtime_deps(mocker)
    opened = mocker.patch("webbrowser.open")
    targets = []
    mocker.patch.object(
        server_mod.threading,
        "Thread",
        side_effect=lambda **kw: targets.append(kw["target"]) or mocker.Mock(),
    )

    server_mod.server(port=8048, host="192.0.2.10", no_runtime=True)
    targets[0]()

    opened.assert_called_once_with("http://192.0.2.10:8048/setup-hardware")


def _serving_pair(mocker, mode="normal"):
    """Real serving orchestration and threads, fake ASGI runners/listeners."""
    real_serve, real_thread = server_mod._serve, threading.Thread
    deps = _patch_server_runtime_deps(mocker)
    mocker.patch.object(server_mod, "_serve", real_serve)
    mocker.patch.object(server_mod.threading, "Thread", real_thread)
    created = []
    bridge_entered = threading.Event()
    deps["bringup_completed"] = threading.Event()
    deps["ready_probed"] = threading.Event()

    class Server:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.stopped = threading.Event()
            self.sockets = []
            created.append(self)

        @property
        def should_exit(self):
            return self.stopped.is_set()

        @should_exit.setter
        def should_exit(self, value):
            if value:
                self.stopped.set()

        def run(self, *, sockets):
            self.sockets = sockets
            if self.config.host == "0.0.0.0":
                bridge_entered.set()
                if mode == "bridge-error":
                    raise RuntimeError("bridge startup broke")
                if mode == "bridge-return":
                    return
                if mode == "delayed-bridge":
                    deps["bringup_completed"].wait(0.05)
                self.started = True
                assert self.stopped.wait(4), "bridge was never stopped"
                return
            assert bridge_entered.wait(2), "bridge was never started"
            if mode == "control-startup":
                return
            if mode == "interrupt-startup":
                raise KeyboardInterrupt
            if mode == "delayed-control":
                deps["bringup_completed"].wait(0.05)
            self.started = True
            if mode == "stopping":
                self.should_exit = True
                deps["bringup_completed"].wait(0.05)
            if mode == "wait-ready":
                assert deps["ready_probed"].wait(2), "ready watcher never started"
            if mode in {"wait-bringup", "delayed-control", "delayed-bridge"}:
                assert deps["bringup_completed"].wait(2), "bringup was never started"
            if mode.startswith("bridge-") or mode == "startup-error":
                assert self.stopped.wait(2), "control never learned the bridge failed"
            if mode == "control-error":
                raise OSError("control serving broke")
            if mode == "interrupt":
                raise KeyboardInterrupt

    mocker.patch("uvicorn.Server", Server)
    return deps, created


def test_server_serves_reserved_sockets_and_stops_bridge_on_return(mocker):
    deps, servers = _serving_pair(mocker)

    server_mod.server(host="127.0.0.1", port=8048, no_setup_hardware=True)

    assert servers[0].sockets == [deps["listeners"][1]]
    assert servers[1].sockets == [deps["listeners"][0]]
    assert all(s.config.log_config is None for s in servers)
    assert all(s.config.access_log is False for s in servers)
    assert servers[0].should_exit
    deps["listeners"][0].__exit__.assert_called_once()
    deps["listeners"][1].__exit__.assert_called_once()
    deps["state"].clear.assert_called_once()


@pytest.mark.parametrize("mode", ["bridge-error", "bridge-return", "control-startup"])
def test_server_reports_listener_failure_and_cleans_both_planes(mode, mocker, capsys):
    deps, servers = _serving_pair(mocker, mode)

    with pytest.raises(typer.Exit) as error:
        server_mod.server(host="127.0.0.1", port=8048, no_setup_hardware=True)

    assert error.value.exit_code == 1
    assert "failed" in capsys.readouterr().err
    assert servers[0].should_exit
    deps["listeners"][0].__exit__.assert_called_once()
    deps["listeners"][1].__exit__.assert_called_once()
    deps["shutdown"].assert_called_once()
    deps["state"].clear.assert_called_once()


def test_server_reports_hardware_dispatch_failure_and_stops_both_planes(mocker, capsys):
    deps, servers = _serving_pair(mocker, "startup-error")
    deps["warm_start"].resume_flag.side_effect = RuntimeError("bundle import failed")

    with pytest.raises(typer.Exit) as error:
        server_mod.server(host="127.0.0.1", port=8048, warm_start=True)

    assert error.value.exit_code == 1
    assert "hardware startup failed: bundle import failed" in capsys.readouterr().err
    assert servers[0].should_exit
    deps["shutdown"].assert_called_once()


def test_server_closes_bridge_when_control_serving_raises(mocker):
    deps, servers = _serving_pair(mocker, "control-error")

    with pytest.raises(OSError, match="control serving broke"):
        server_mod.server(host="127.0.0.1", port=8048, no_setup_hardware=True)

    assert servers[0].should_exit
    deps["listeners"][0].__exit__.assert_called_once()
    deps["listeners"][1].__exit__.assert_called_once()
    deps["shutdown"].assert_called_once()


@pytest.mark.parametrize("mode", ["interrupt", "interrupt-startup"])
def test_server_interrupt_cleans_both_planes_and_restores_signal(mode, mocker):
    import signal

    previous = signal.getsignal(signal.SIGTERM)
    deps, servers = _serving_pair(mocker, mode)
    handler = mocker.spy(signal, "signal")

    server_mod.server(host="127.0.0.1", port=8048, no_setup_hardware=True)

    assert signal.getsignal(signal.SIGTERM) == previous
    assert handler.call_args_list == [
        mocker.call(signal.SIGTERM, signal.default_int_handler),
        mocker.call(signal.SIGTERM, previous),
    ]
    assert servers[0].should_exit
    deps["shutdown"].assert_called_once()
    deps["state"].clear.assert_called_once()


def test_server_refuses_concurrent_start_before_opening_listeners(mocker, capsys):
    from physiclaw.common import paths
    from physiclaw.port.locks import exclusive_file_lock

    deps = _patch_server_runtime_deps(mocker)
    with exclusive_file_lock(paths.runtime_state_file().with_suffix(".lock")):
        with pytest.raises(typer.Exit) as error:
            server_mod.server(**_server_kwargs())

    assert error.value.exit_code == 1
    assert "running or starting" in capsys.readouterr().err
    assert deps["listeners"] == []
    deps["state"].write.assert_not_called()


def test_server_stops_assistive_touch_before_acquiring_lock_and_binding(mocker):
    from contextlib import ExitStack

    from physiclaw.common import paths
    from physiclaw.port.locks import exclusive_file_lock

    deps = _patch_server_runtime_deps(mocker)
    live = {"pid": 42, "port": 8048, "service": "test-assistive-touch"}
    deps["state"].read_live.side_effect = [live, None]
    # Simulate the standalone test holding the shared lock until stopped.
    with ExitStack() as held:
        held.enter_context(exclusive_file_lock(paths.runtime_state_file().with_suffix(".lock")))

        def stop(record):
            assert record == live
            assert deps["listeners"] == []
            held.close()

        stopped = mocker.patch("physiclaw.cli._service_handoff.stop_service", side_effect=stop)
        server_mod.server(**_server_kwargs())
    stopped.assert_called_once_with(live)
    assert len(deps["listeners"]) == 2


def test_server_startup_failure_releases_listeners_and_lock(mocker):
    from physiclaw.common import paths
    from physiclaw.port.locks import exclusive_file_lock

    deps = _patch_server_runtime_deps(mocker)
    deps["core_server"].build_apps.side_effect = RuntimeError("build failed")

    with pytest.raises(RuntimeError, match="build failed"):
        server_mod.server(**_server_kwargs())

    deps["listeners"][0].__exit__.assert_called_once()
    deps["listeners"][1].__exit__.assert_called_once()
    deps["state"].clear.assert_called_once()
    deps["shutdown"].assert_called_once()
    with exclusive_file_lock(paths.runtime_state_file().with_suffix(".lock")):
        pass


@pytest.mark.parametrize("method", ["terminate", "kill"])
def test_terminate_child_reaps_when_exit_races_signal(method, mocker):
    proc = mocker.Mock()
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired("child", 0.1), 0]
    getattr(proc, method).side_effect = ProcessLookupError("already exited")

    server_mod.terminate_child(proc, timeout=0.1)

    assert proc.wait.call_args_list == [
        mocker.call(timeout=0.1),
        mocker.call(timeout=0.1),
    ]


def test_terminate_child_does_not_signal_exited_process(mocker):
    proc = mocker.Mock()
    proc.poll.return_value = 0

    server_mod.terminate_child(proc)

    assert proc.method_calls == [mocker.call.poll()]


@pytest.mark.parametrize("port", [1, 65534])
def test_server_reserves_control_and_adjacent_bridge_ports(port, mocker):
    _patch_server_runtime_deps(mocker)
    create = server_mod.socket.create_server

    server_mod.server(
        port=port, host="127.0.0.1", no_runtime=True, no_setup_hardware=True
    )

    assert create.call_args_list == [
        mocker.call(("127.0.0.1", port), family=socket.AF_INET, dualstack_ipv6=False),
        mocker.call(("0.0.0.0", port + 1), family=socket.AF_INET, dualstack_ipv6=False),
    ]


@pytest.mark.parametrize("port", ["-1", "0", "65535", "65536"])
def test_server_cli_rejects_invalid_ports_before_vision_preflight(port, mocker):
    from physiclaw.cli import app

    vision = mocker.patch.object(server_mod, "_require_vision_model")

    result = runner.invoke(app, ["server", "--port", port])

    assert result.exit_code == 2
    assert "Invalid value" in result.output
    vision.assert_not_called()


def test_server_ipv6_listener_and_endpoint_logs_use_the_same_host(mocker, caplog):
    _patch_server_runtime_deps(mocker)
    mocker.patch.object(server_mod.socket, "has_dualstack_ipv6", return_value=True)

    with caplog.at_level(logging.INFO, logger="physiclaw.cli.server"):
        server_mod.server(host="::1", port=8048, no_setup_hardware=True)

    server_mod.socket.create_server.assert_any_call(
        ("::1", 8048), family=socket.AF_INET6, dualstack_ipv6=True
    )
    assert "http://[::1]:8048/mcp" in caplog.text
    assert "http://[::1]:8048/api/bridge/qr" in caplog.text


def test_server_reports_lock_permission_error_before_opening_listeners(mocker, capsys):
    deps = _patch_server_runtime_deps(mocker)
    mocker.patch(
        "physiclaw.port.locks.exclusive_file_lock",
        side_effect=PermissionError("read-only home"),
    )

    with pytest.raises(typer.Exit) as error:
        server_mod.server(**_server_kwargs())

    assert error.value.exit_code == 1
    assert "cannot acquire the server lock: read-only home" in capsys.readouterr().err
    assert deps["listeners"] == []


def test_server_keeps_signal_handlers_when_called_off_main_thread(mocker):
    worker = threading.Thread(name="server-test-worker")
    _patch_server_runtime_deps(mocker)
    mocker.patch.object(server_mod.threading, "current_thread", return_value=worker)
    signal = mocker.patch.object(server_mod.signal, "signal")

    server_mod.server(**_server_kwargs())

    signal.assert_not_called()


def test_server_bridge_thread_start_failure_releases_resources(mocker):
    real_serve = server_mod._serve
    deps = _patch_server_runtime_deps(mocker)
    mocker.patch.object(server_mod, "_serve", real_serve)
    thread = server_mod.threading.Thread.return_value
    thread.start.side_effect = RuntimeError("no threads available")
    thread.ident = None
    mocker.patch("uvicorn.Server")

    with pytest.raises(RuntimeError, match="no threads available"):
        server_mod.server(port=8048, no_setup_hardware=True)

    thread.join.assert_not_called()
    deps["listeners"][0].__exit__.assert_called_once()
    deps["listeners"][1].__exit__.assert_called_once()
    deps["shutdown"].assert_called_once()


def test_server_reports_bridge_that_will_not_stop(mocker, capsys):
    real_serve = server_mod._serve
    deps = _patch_server_runtime_deps(mocker)
    mocker.patch.object(server_mod, "_serve", real_serve)
    thread = server_mod.threading.Thread.return_value
    thread.ident = 123
    thread.name = "bridge-http"
    thread.is_alive.return_value = True
    mocker.patch("uvicorn.Server")

    with pytest.raises(typer.Exit) as error:
        server_mod.server(port=8048, no_setup_hardware=True)

    assert error.value.exit_code == 1
    assert "bridge-http did not stop" in capsys.readouterr().err
    assert thread.join.call_args_list == [
        mocker.call(timeout=5),
        mocker.call(timeout=5),
    ]
    deps["state"].clear.assert_called_once()


@pytest.mark.parametrize("mode", ["wait-bringup", "delayed-control", "delayed-bridge"])
def test_server_resumes_hardware_only_after_both_asgi_servers_start(mode, mocker):
    deps, servers = _serving_pair(mocker, mode)
    observed = []
    started = mocker.spy(threading.Thread, "start")

    def resume(*args, **kwargs):
        observed.append([server.started for server in servers])
        deps["bringup_completed"].set()
        return True

    deps["warm_start"].try_resume.side_effect = resume

    server_mod.server(port=8048, host="127.0.0.1", warm_start=True)

    assert observed == [[True, True]]
    # The startup coordinator can finish before the warm-start worker runs.
    # Check the launched threads, not a racing snapshot of currently live ones.
    workers = [
        call.args[0]
        for call in started.call_args_list
        if call.args[0].name in {"bridge-http", "server-startup"}
    ]
    assert len(workers) == 2
    assert all(thread.daemon for thread in workers)


@pytest.mark.parametrize("mode", ["bridge-error", "bridge-return", "control-startup"])
def test_server_does_not_resume_hardware_after_asgi_startup_failure(mode, mocker):
    deps, _ = _serving_pair(mocker, mode)

    with pytest.raises(typer.Exit):
        server_mod.server(port=8048, host="127.0.0.1", warm_start=True)

    deps["warm_start"].try_resume.assert_not_called()


def test_server_does_not_start_hardware_during_shutdown(mocker):
    deps, _ = _serving_pair(mocker, "stopping")

    server_mod.server(port=8048, host="127.0.0.1", warm_start=True)

    deps["warm_start"].try_resume.assert_not_called()


def test_server_return_cancels_pending_ready_watcher(mocker):
    deps, _ = _serving_pair(mocker, "wait-ready")
    real_thread = threading.Thread
    threads = []

    def make_thread(**kwargs):
        thread = real_thread(**kwargs)
        threads.append(thread)
        return thread

    def probe(base):
        deps["ready_probed"].set()
        return False

    mocker.patch.object(server_mod.threading, "Thread", side_effect=make_thread)
    mocker.patch("physiclaw.common.ready.check_ready_once", side_effect=probe)

    server_mod.server(port=8048, no_runtime=True, no_setup_hardware=True)
    threads[-1].join(timeout=1)

    assert len(threads) == 3
    assert threads[-1].daemon
    assert not threads[-1].is_alive()


def test_server_defaults_spawn_runtime_with_info_logging(mocker):
    from physiclaw.common import logger, paths

    _patch_server_runtime_deps(mocker)

    server_mod.server(port=8048, no_setup_hardware=True)

    server_mod._spawn_runtime.assert_called_once()
    assert logger.setup_logging.call_args.args[1] == logging.INFO
    assert logger.setup_logging.call_args.kwargs["file_dir"] == paths.LOG_DIR
    assert logger.setup_logging.call_args.kwargs["file_prefix"] == "server"
    assert logging.getLogger("mcp").level == logging.WARNING


def test_server_enables_raw_camera_saving(mocker, monkeypatch):
    import os

    _patch_server_runtime_deps(mocker)
    monkeypatch.delenv("PHYSICLAW_SAVE_RAW_CAMERA", raising=False)

    server_mod.server(port=8048, no_setup_hardware=True, save_raw_camera=True)

    assert os.environ["PHYSICLAW_SAVE_RAW_CAMERA"] == "1"


@pytest.mark.parametrize(
    ("host", "dial"),
    [("0.0.0.0", "127.0.0.1"), ("::", "127.0.0.1"), ("192.0.2.10", "192.0.2.10")],
)
def test_server_probes_dialable_control_and_bridge_addresses(host, dial, mocker):
    real_probe = server_mod._port_in_use
    _patch_server_runtime_deps(mocker)
    mocker.patch.object(server_mod, "_port_in_use", real_probe)
    connect = mocker.patch.object(
        server_mod.socket, "create_connection", side_effect=OSError
    )

    server_mod.server(port=8048, host=host, no_setup_hardware=True)

    assert connect.call_args_list == [
        mocker.call((dial, 8048), timeout=0.5),
        mocker.call(("127.0.0.1", 8049), timeout=0.5),
    ]


def test_server_reports_original_bridge_exception(mocker, capsys):
    _serving_pair(mocker, "bridge-error")

    with pytest.raises(typer.Exit):
        server_mod.server(host="127.0.0.1", port=8048, no_setup_hardware=True)

    assert "phone bridge failed: bridge startup broke" in capsys.readouterr().err


def test_server_resume_exception_interrupts_and_reports_failure(mocker, caplog):
    deps = _patch_server_runtime_deps(mocker)
    deps["warm_start"].try_resume.side_effect = OSError("camera unplugged")
    interrupt = mocker.patch.object(server_mod._thread, "interrupt_main")

    server_mod.server(port=8048, warm_start=True)
    server_mod.threading.Thread.call_args.kwargs["target"]()

    interrupt.assert_called_once_with()
    assert "resume crashed" in caplog.text
    assert "camera unplugged" in caplog.text


def test_server_wizard_browser_failure_is_nonfatal(mocker, caplog):
    _patch_server_runtime_deps(mocker)
    mocker.patch("webbrowser.open", side_effect=OSError("headless machine"))

    with caplog.at_level(logging.DEBUG):
        server_mod.server(port=8048)
        server_mod.threading.Thread.call_args.kwargs["target"]()

    assert "could not open browser" in caplog.text


def test_server_invalid_model_reports_original_error_and_exits(mocker, capsys):
    deps = _patch_server_runtime_deps(mocker)
    deps["launcher"].resolve.side_effect = RuntimeError(
        "unknown provider 'unknown' in ref 'unknown/model' (from PHYSICLAW_MODEL env)"
    )

    with pytest.raises(typer.Exit) as error:
        server_mod.server(no_runtime=False)

    assert error.value.exit_code == 1
    server_mod._spawn_runtime.assert_not_called()
    server_mod.socket.create_server.assert_not_called()
    deps["serve"].assert_not_called()
    deps["state"].write.assert_not_called()
    deps["state"].clear.assert_not_called()
    output = capsys.readouterr().err
    assert "unknown provider 'unknown'" in output
    assert "PHYSICLAW_MODEL env" in output
    assert "no model configured" not in output
