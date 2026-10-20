"""Behavior shared by CLI, services and standalone diagnostic entry points."""

import ast
import io
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from physiclaw.common import log_style
from physiclaw.common.logger import setup_logging

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def isolated_logs(monkeypatch):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", root.level)
    monkeypatch.delenv("PHYSICLAW_LOG_COMPONENTS", raising=False)
    monkeypatch.delenv("PHYSICLAW_LOG_FORMAT", raising=False)
    yield root
    for handler in root.handlers:
        handler.close()


@pytest.mark.parametrize("tag", ["cli", "runtime", "studio", "diagnostic", "vision"])
def test_every_surface_uses_one_json_envelope(isolated_logs, monkeypatch, capsys, tag):
    monkeypatch.setenv("PHYSICLAW_LOG_FORMAT", "json")
    log_style.configure_console(tag)
    logging.getLogger("physiclaw.example").info("Ready\nfor work")
    captured = capsys.readouterr()
    (line,) = captured.err.splitlines()
    record = json.loads(line)
    assert captured.out == ""
    assert (
        record.items()
        >= {
            "type": "log",
            "schema_version": 1,
            "event": "log.message",
            "service": tag,
            "level": "info",
            "logger": "physiclaw.example",
            "message": "Ready\nfor work",
        }.items()
    )
    assert {"timestamp", "line", "pid", "thread"} <= record.keys()


def test_reconfiguration_replaces_own_handlers_and_preserves_host_capture(
    isolated_logs, tmp_path, capsys
):
    captured = io.StringIO()
    host_handler = logging.StreamHandler(captured)
    isolated_logs.addHandler(host_handler)
    setup_logging("cli", file_dir=tmp_path, force=False)
    prior = list(isolated_logs.handlers)
    setup_logging("studio", file_dir=tmp_path, force=False)
    logging.getLogger("physiclaw.studio").info("Open Studio", extra={"console": True})
    assert host_handler in isolated_logs.handlers
    assert captured.getvalue().count("Open Studio") == 1
    assert capsys.readouterr().err.count("Open Studio") == 1
    assert all(
        not getattr(handler, "_physiclaw_managed", False)
        or handler not in isolated_logs.handlers
        for handler in prior
    )
    assert (tmp_path / "studio.log").is_file()


def test_expected_error_is_saved_once_and_follows_stderr_redirection(
    isolated_logs, monkeypatch, tmp_path
):
    setup_logging("cli", file_dir=tmp_path, force=False)
    redirected = io.StringIO()
    monkeypatch.setattr(sys, "stderr", redirected)
    log_style.report_error("Cannot read configuration", exit_code=2)
    assert redirected.getvalue().count("Cannot read configuration") == 1
    saved = (tmp_path / "cli.log").read_text()
    assert saved.count("Cannot read configuration") == 1
    assert "event=process.error" in saved and "exit_code=2" in saved


@pytest.mark.parametrize("verbose", [False, True])
def test_exception_details_follow_one_console_policy(
    isolated_logs, tmp_path, capsys, verbose
):
    setup_logging(
        "studio", logging.DEBUG if verbose else logging.INFO, file_dir=tmp_path
    )
    try:
        raise OSError("port unavailable")
    except OSError:
        logging.getLogger("physiclaw.studio").exception("Cannot start Studio")
    console = capsys.readouterr().err
    saved = (tmp_path / "studio.log").read_text()
    assert "OSError: port unavailable" in console
    assert ("Traceback (most recent call last)" in console) is verbose
    assert "Traceback (most recent call last)" in saved


@pytest.mark.parametrize("source", ["hook", "camera"])
async def test_failure_bursts_keep_every_traceback_on_disk_without_console_repeats(
    isolated_logs, monkeypatch, tmp_path, capsys, source
):
    setup_logging("runtime", file_dir=tmp_path, file_level=logging.DEBUG, force=False)
    if source == "hook":
        from physiclaw.agent.runtime import hook

        monkeypatch.setattr(hook, "_hooks", [])
        monkeypatch.setattr(hook, "_failures", {})
        outcomes = iter(
            [
                RuntimeError("offline"),
                RuntimeError("offline"),
                None,
                None,
                RuntimeError("offline"),
            ]
        )

        @hook.register
        def watch():
            error = next(outcomes)
            if error is not None:
                raise error

        for _ in range(5):
            assert await hook.check_hooks() == []
        failure_event, recovery_event = "hook.failed", "hook.recovered"
        failure_notice, recovery_notice = "Hook watch failed", "Hook watch recovered"
    else:
        from physiclaw.core.hardware.frame_reader import FrameReader

        def read_frame():
            raise RuntimeError("offline")

        reader = FrameReader(read_frame, lambda: None)
        reader.publish(object())
        monkeypatch.setattr(reader._stopped, "wait", lambda _: reader._stopped.set())
        for recover in (False, False, True, True, False):
            if recover:
                reader.publish(object())
            else:
                reader._stopped.clear()
                reader._loop()
        failure_event, recovery_event = "camera.read_failed", "camera.frames_restored"
        failure_notice, recovery_notice = (
            "Camera read failed",
            "Camera connection restored",
        )

    console = capsys.readouterr().err
    saved = (tmp_path / "runtime.log").read_text()
    assert console.count(failure_notice) == 2  # One notice for each outage.
    assert console.count(recovery_notice) == 1
    assert "Traceback (most recent call last)" not in console
    assert saved.count(f"event={failure_event}") == 3
    assert saved.count(f"event={recovery_event}") == 1
    assert saved.count("Traceback (most recent call last)") == 3


@pytest.mark.parametrize("source", ["runtime", "phone"])
async def test_poll_failures_keep_changed_causes_and_repeated_tracebacks(
    isolated_logs, monkeypatch, tmp_path, capsys, source
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from physiclaw.agent.hooks import poll
    from physiclaw.agent.runtime import runtime

    setup_logging("runtime", file_dir=tmp_path, file_level=logging.DEBUG, force=False)
    outcomes = [
        RuntimeError("offline"),
        RuntimeError("offline"),
        ValueError("invalid status"),
        False,
        False,
        RuntimeError("offline"),
    ]
    if source == "runtime":
        rt = runtime.Runtime(lambda _: pytest.fail("Reacted during a status outage"))
        results = iter(outcomes)
        checks = 0

        async def check():
            nonlocal checks
            checks += 1
            if checks == len(outcomes):
                rt.stop()
            value = next(results)
            if isinstance(value, Exception):
                raise value
            return value

        monkeypatch.setattr(runtime, "load_hooks", lambda: None)
        monkeypatch.setattr(runtime, "_check_ready", check)
        monkeypatch.setattr(rt, "_sleep", AsyncMock())
        await rt.start()
        failure_event, recovery_event = (
            "server.connection_lost",
            "server.connection_restored",
        )
    else:
        response = SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"wake": False}
        )
        client = SimpleNamespace(
            get=AsyncMock(
                side_effect=[
                    v if isinstance(v, Exception) else response for v in outcomes
                ]
            )
        )
        monkeypatch.setattr(poll, "_client", client)
        monkeypatch.setattr(poll, "_failure", log_style.FailureState())
        for _ in outcomes:
            assert await poll.phone_watch() is None
        failure_event, recovery_event = "phone.watch_failed", "phone.watch_recovered"

    console = capsys.readouterr().err
    saved = (tmp_path / "runtime.log").read_text()
    assert console.count("poll failed:") == 3  # New cause, recovery, new outage.
    assert console.count("connection restored") == 1
    assert "invalid status" in console
    assert "Traceback (most recent call last)" not in console
    assert saved.count(f"event={failure_event}") == 4
    assert saved.count(f"event={recovery_event}") == 1
    assert saved.count("Traceback (most recent call last)") == 4


@pytest.mark.parametrize("source", ["turn", "decision"])
async def test_model_failures_save_the_full_exception_chain(
    isolated_logs, monkeypatch, tmp_path, capsys, source
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from physiclaw.agent.engine.loop import _call_producer
    from physiclaw.agent.engine.policy import default_policies
    from physiclaw.agent.engine.provenance import PluginBinding
    from physiclaw.agent.engine.session import Session
    from physiclaw.agent.llm import LLMProducer
    from physiclaw.common.provider_errors import ProviderTransientError
    from physiclaw.conductor.micro.channel import MicroCaller
    from physiclaw.conductor.micro.decision import AGENT_FIELDS, DecisionRequest
    from physiclaw.contract.dto import CollapsePolicy

    setup_logging("runtime", file_dir=tmp_path, file_level=logging.DEBUG, force=False)

    attempts = 0

    async def failed_chat(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        try:
            raise OSError(f"transport detail {attempts}")
        except OSError as cause:
            raise ProviderTransientError("gateway unavailable") from cause

    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    if source == "turn":
        session = Session()
        run = SimpleNamespace(
            plugins=(),
            tool_schemas=[],
            schema_by_name={},
            deadline=None,
            settings=SimpleNamespace(
                provider_retry_attempts=2, retry_backoff_seconds=0, max_turns=300
            ),
            policies=default_policies(layout_incomplete=False),
            rlog=MagicMock(),
            tr=MagicMock(),
        )
        provider = SimpleNamespace(
            chat=failed_chat,
            serialize_history=lambda _, **kw: [],
            COLLAPSE=CollapsePolicy(first_at=100, keep=10, interval=100),
        )
        run.plugins = (
            PluginBinding(
                "physiclaw.llm", LLMProducer(provider, run, session, []), "LLM"
            ),
        )
        assert await _call_producer(run, session, 0) is None
        assert session.sentinel_status == "STUCK"
    else:
        caller = MicroCaller(SimpleNamespace(chat=failed_chat), confidence_floor=0.6)
        request = DecisionRequest(
            call=AGENT_FIELDS,
            node_id="test",
            outcomes=(),
            material={"prompt": "test", "fields": "- keyword: k"},
        )
        assert (await caller.run(request)).outcome is None

    console = capsys.readouterr().err
    saved = (tmp_path / "runtime.log").read_text()
    assert "gateway unavailable" in console
    assert "Traceback (most recent call last)" not in console
    assert "OSError: transport detail 1" in saved
    assert "OSError: transport detail 2" in saved
    assert "ProviderTransientError: gateway unavailable" in saved


@pytest.mark.parametrize("env", [{"NO_COLOR": ""}, {"NO_COLOR": "1"}, {"TERM": "dumb"}])
def test_color_disabled_consistently(monkeypatch, env):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: True)
    assert not log_style.color_enabled(stream)


def test_real_cli_keeps_payload_stdout_and_records_errors(
    isolated_logs, monkeypatch, physiclaw_home
):
    from physiclaw.cli import app

    monkeypatch.setenv("PHYSICLAW_LOG_FORMAT", "json")
    runner = CliRunner()
    result = runner.invoke(app, ["config", "path"])
    assert result.exit_code == 0
    assert result.stdout.strip() == str(physiclaw_home / "config.toml")
    assert result.stderr == ""
    result = runner.invoke(app, ["config", "get", "missing.value"])
    assert result.exit_code != 0 and result.stdout == ""
    (record,) = [json.loads(line) for line in result.stderr.splitlines()]
    assert record["event"] == "process.error" and record["service"] == "cli"
    saved = (physiclaw_home / "log" / "cli.log").read_text()
    assert saved.count(record["message"]) == 1


@pytest.mark.parametrize("script", ["grbl_jog.py", "grbl_solenoid_test.py"])
def test_serial_diagnostics_use_shared_output_without_touching_hardware(
    tmp_path, script
):
    # An explicit nonexistent path avoids discovery and cannot address a real board.
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / script),
            "--port",
            str(tmp_path / "absent-port"),
            "--log-format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1 and result.stdout == ""
    records = [json.loads(line) for line in result.stderr.splitlines()]
    assert all(
        record["service"] == "diagnostic" and record["schema_version"] == 1
        for record in records
    )
    errors = [record for record in records if record["level"] == "error"]
    assert len(errors) == 1
    assert errors[0]["event"] == "diagnostic.failed"
    assert "absent-port" in errors[0]["exception"]


def test_processes_do_not_install_competing_log_formats():
    """A new entry point must reuse the shared renderer instead of drifting."""
    offenders = []
    for path in (ROOT / "src" / "physiclaw").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logging"
                and node.func.attr == "basicConfig"
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "uvicorn"
                and node.func.attr == "Config"
            ):
                settings = {arg.arg: arg.value for arg in node.keywords}
                assert isinstance(settings.get("log_config"), ast.Constant)
                assert settings["log_config"].value is None
                assert isinstance(settings.get("access_log"), ast.Constant)
                assert settings["access_log"].value is False
    assert offenders == [], f"Reuse common.log_style: {offenders}"
