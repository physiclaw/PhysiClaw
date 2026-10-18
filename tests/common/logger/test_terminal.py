"""Terminal/file integration: quiet presentation must retain diagnostic evidence."""

import asyncio
import io
import json
import logging
import re
import sys

import pytest

from physiclaw.common.logger import logger as logger_mod
from physiclaw.common.logger.context import (
    bind_log_context,
    record_fields,
    reset_log_context,
)
from physiclaw.common.logger.logger import _TaggedFormatter, logged, setup_logging
from physiclaw.common.logger.structured import JsonLogFormatter


@pytest.fixture
def output_logs(tmp_path, monkeypatch):
    # Avoid closing pytest's own capture handlers when basicConfig(force=True)
    # runs; restore the original root after closing our temporary handlers.
    root = logging.getLogger()
    monkeypatch.delenv("PHYSICLAW_LOG_FORMAT", raising=False)
    monkeypatch.delenv("PHYSICLAW_LOG_COMPONENTS", raising=False)
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", root.level)
    console = io.StringIO()

    def configure(level=logging.INFO):
        monkeypatch.setattr(logger_mod.sys, "stderr", console)
        setup_logging("runtime", level, file_dir=tmp_path, file_level=logging.DEBUG)
        return console, tmp_path

    yield configure
    for handler in root.handlers:
        handler.close()


@pytest.mark.parametrize("verbose", [False, True])
def test_new_components_are_quiet_until_they_opt_into_user_notices(
    output_logs, verbose
):
    console, directory = output_logs(logging.DEBUG if verbose else logging.INFO)
    for name in ("physiclaw.future_component", "third_party.future_component"):
        log = logging.getLogger(name)
        log.info("internal diagnostic from %s", name)
        log.info("Helpful update from %s", name, extra={"console": True})
        log.warning("Needs attention from %s", name, extra={"console": False})
    terminal = console.getvalue()
    saved = (directory / "runtime.log").read_text()
    assert ("internal diagnostic" in terminal) is verbose
    assert terminal.count("Helpful update") == 2
    assert terminal.count("Needs attention") == 2
    assert saved.count("internal diagnostic") == 2


@pytest.mark.parametrize("verbose", [False, True])
def test_legacy_message_indentation_does_not_shift_normal_notices(output_logs, verbose):
    console, directory = output_logs(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("physiclaw.core.calibration.viewport").warning(
        "    Camera needs attention"
    )

    normal = console.getvalue()
    saved = (directory / "runtime.log").read_text(encoding="utf-8")
    if verbose:
        assert "|     Camera needs attention" in normal
    else:
        assert "[runtime] Camera needs attention" in normal
    assert "|     Camera needs attention" in saved


def test_task_recap_stays_with_its_outcome_and_retains_full_saved_text(output_logs):
    from physiclaw.agent.trace import Trace
    from physiclaw.agent.trace.store import Images

    console, directory = output_logs()
    trace = Trace("recap-format", Images("recap-format"))
    trace.write(
        {
            "event": "done",
            "sentinel": "DONE",
            "recap": "Order 12345 placed.\nTrack delivery tomorrow.",
        }
    )
    trace.close()

    (line,) = console.getvalue().splitlines()
    assert "Task completed" in line
    assert " — Order 12345 placed. Track delivery tomorrow." in line
    saved = (directory / "runtime.log").read_text(encoding="utf-8")
    assert "Order 12345 placed." in saved and "Track delivery tomorrow." in saved


@pytest.mark.parametrize("verbose", [False, True])
def test_bookkeeping_stays_on_disk_and_verbose_reveals_it(output_logs, verbose):
    console, directory = output_logs(logging.DEBUG if verbose else logging.INFO)
    log = logging.getLogger("physiclaw.agent.engine.loop")
    log.debug("request diagnostic")
    log.info("token accounting", extra={"console": False})
    log.info("action completed", extra={"console": True})
    log.warning("action failed", extra={"console": False})

    terminal = console.getvalue()
    saved = (directory / "runtime.log").read_text()
    assert ("request diagnostic" in terminal) is verbose
    assert ("token accounting" in terminal) is verbose
    assert "action completed" in terminal and "action failed" in terminal
    assert all(
        message in saved
        for message in (
            "request diagnostic",
            "token accounting",
            "action completed",
            "action failed",
        )
    )
    assert "physiclaw.agent.engine.loop:" in saved
    assert "pid=" in saved and "thread=" in saved
    assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4}", saved)
    assert "\033" not in terminal + saved


def test_console_summary_does_not_replace_full_saved_message(output_logs, capsys):
    console, directory = output_logs()
    log = logging.getLogger("physiclaw.agent.engine.dispatch")
    log.info(
        "complete diagnostic payload",
        extra={"console_message": "concise result", "console": True},
    )
    # Distinct events may have identical text; blanket deduplication loses actions.
    log.info("tap completed", extra={"console": True})
    log.info("tap completed", extra={"console": True})

    terminal = console.getvalue()
    saved = (directory / "runtime.log").read_text()
    assert "concise result" in terminal
    assert "complete diagnostic payload" not in terminal
    assert "complete diagnostic payload" in saved
    assert "concise result" not in saved
    assert terminal.count("tap completed") == 2
    assert capsys.readouterr().out == ""  # stdout stays available for protocols/pipes


async def test_server_success_is_quiet_but_failure_keeps_type_and_saved_traceback(
    output_logs,
):
    console, directory = output_logs()

    @logged
    async def probe(*, fail=False):
        if fail:
            raise ValueError("camera disconnected")
        return "ok"

    assert await probe() == "ok"
    with pytest.raises(ValueError, match="camera disconnected"):
        await probe(fail=True)

    terminal = console.getvalue()
    saved = (directory / "runtime.log").read_text()
    assert "tool probe(" not in terminal
    assert terminal.count("Tool probe failed") == 1
    assert "WARN" in terminal and "ValueError: camera disconnected" in terminal
    assert "Traceback" not in terminal
    assert saved.count("event=tool.started") == 2
    assert saved.count("tool probe(") == 4
    assert "Traceback" in saved and "raise ValueError" in saved


@pytest.mark.parametrize("verbose", [False, True])
def test_exception_details_are_short_only_in_normal_output(output_logs, verbose):
    console, directory = output_logs(logging.DEBUG if verbose else logging.INFO)
    error = "request rejected\n" + "SDK payload " * 100 + "END_OF_PAYLOAD"
    try:
        raise ValueError(error)
    except ValueError:
        logging.getLogger("physiclaw.provider").exception("Model request failed")

    terminal = console.getvalue()
    saved = (directory / "runtime.log").read_text()
    assert "ValueError: request rejected" in terminal
    assert ("END_OF_PAYLOAD" in terminal) is verbose
    if not verbose:
        assert len(terminal) < 250
        assert len(terminal.splitlines()) == 1
    assert "END_OF_PAYLOAD" in saved and "Traceback" in saved


def test_cause_already_in_message_is_not_repeated(output_logs):
    console, _ = output_logs()
    try:
        raise OSError("camera unavailable")
    except OSError:
        logging.getLogger("physiclaw.camera").warning(
            "Capture failed: %s", "camera unavailable", exc_info=True
        )
    assert console.getvalue().count("camera unavailable") == 1


def test_failure_state_reports_changed_causes_and_new_outages():
    from physiclaw.common.log_style import FailureState

    state = FailureState()
    assert not state.recovered()
    assert state.level(OSError("offline")) == logging.WARNING
    assert state.level(OSError("offline")) == logging.DEBUG
    assert state.level(ValueError("offline"), logging.ERROR) == logging.ERROR
    assert state.level(ValueError("changed cause")) == logging.WARNING
    assert state.recovered()
    assert not state.recovered()
    assert state.level(ValueError("changed cause")) == logging.WARNING


@pytest.mark.parametrize(
    "level", [logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL]
)
def test_terminal_columns_align_for_all_severities(level):
    record = logging.LogRecord(
        "physiclaw.core.hardware.camera",
        level,
        "camera.py",
        41,
        "first\nsecond",
        (),
        None,
    )
    plain = _TaggedFormatter("physiclaw", False).format(record)
    colored = _TaggedFormatter("physiclaw", True).format(record)
    assert logger_mod._ANSI_RE.sub("", colored) == plain
    first, second = plain.splitlines()
    assert second == " " * first.index("first") + "second"
    assert first.index("[physiclaw]") == len("00:00:00 INFO  ")
    assert "hardware.camera | first" in first


def test_saved_exception_survives_console_first_or_file_first():
    try:
        raise RuntimeError("provider broke")
    except RuntimeError:
        record = logging.LogRecord(
            "physiclaw.provider.test",
            logging.ERROR,
            "test.py",
            19,
            "request failed",
            (),
            sys.exc_info(),
        )
    record.console_traceback = False
    console = _TaggedFormatter("runtime", False)
    saved = _TaggedFormatter("runtime", False, diagnostic=True)

    for formatter in (console, saved, console, saved):
        text = formatter.format(record)
        assert ("Traceback" in text) is formatter.diagnostic
        assert "request failed" in text


def test_each_call_shows_compact_usage_and_saves_full_accounting(output_logs):
    from physiclaw.agent.trace import Trace
    from physiclaw.agent.trace.store import Images

    console, directory = output_logs()
    trace = Trace("terminal-session", Images("terminal-session"))
    trace.write({"event": "wake", "model_ref": "test/model"})
    trace.write({"event": "request", "turn": 0, "message_count": 2})
    trace.write(
        {
            "event": "usage",
            "call": "turn",
            "model": "test/model",
            "input": 1000,
            "output": 50,
        }
    )
    trace.write({"event": "tool_result", "turn": 0, "name": "tap", "text": "ok"})
    trace.write(
        {
            "event": "usage",
            "call": "micro",
            "model": "test/decision",
            "input": 12400,
            "output": 320,
            "cache_read": 9920,
        }
    )
    trace.write(
        {
            "event": "micro_call",
            "call": "select",
            "out": "tap",
            "detail": "internal decision details",
        }
    )
    trace.write({"event": "done", "sentinel": "DONE", "recap": "Task completed"})
    trace.close()
    trace.close()

    terminal = console.getvalue()
    saved = (directory / "runtime.log").read_text()
    assert terminal.count("Task completed") == 1
    assert "terminal-session" not in terminal
    assert terminal.count("Tokens:") == 2
    assert "Tokens: 1.0k in / 50 out" in terminal
    assert "Tokens: 12.4k in / 320 out · 80% cached" in terminal
    assert "test/decision" in terminal
    assert "test/model" not in terminal  # The primary model was already announced.
    assert "internal decision details" not in terminal
    assert "turns=1" in saved and "tools=1" in saved
    assert "1,000 in / 50 out" not in terminal
    assert "usage[" not in terminal and "usage[turn test/model]" in saved
    assert (
        "usage[micro test/decision]" in saved and "internal decision details" in saved
    )


def test_json_stream_preserves_full_messages_context_and_exceptions(
    output_logs, monkeypatch
):
    monkeypatch.setenv("PHYSICLAW_LOG_FORMAT", "json")
    console, directory = output_logs()
    log = logging.getLogger("physiclaw.agent.engine.dispatch")
    context = bind_log_context(session_id="session-A", turn=0)
    message = "line one\nline two\033[31mred\033[0m\ud800"
    try:
        log.info(
            message,
            extra={
                "event": "tool.completed",
                "tool": "tap",
                "tool_call_id": "call-7",
                "duration_ms": 0,
                "console_message": "short result",
                "api_key": "never serialize arbitrary extras",
            },
        )
        try:
            raise ValueError("camera offline")
        except ValueError:
            log.error(
                "failed",
                exc_info=True,
                extra={"console_traceback": False, "event": "tool.failed"},
            )
    finally:
        reset_log_context(context)

    records = [json.loads(line) for line in console.getvalue().splitlines()]
    assert len(records) == 2
    completed, failed = records
    assert completed["type"] == "log"
    assert completed["session_id"] == "session-A" and completed["turn"] == 0
    assert completed["event"] == "tool.completed" and completed["tool"] == "tap"
    assert completed["tool_call_id"] == "call-7" and completed["duration_ms"] == 0
    assert completed["message"] == "line one\nline twored\ud800"
    assert "api_key" not in completed and "short result" not in console.getvalue()
    assert completed["logger"] == log.name
    assert completed["pid"] > 0 and completed["line"] > 0
    assert re.search(r"\.\d{3}[+-]\d{2}:\d{2}$", completed["timestamp"])
    assert failed["level"] == "error"
    assert "ValueError: camera offline" in failed["exception"]
    saved = (directory / "runtime.log").read_text()
    assert "session_id=session-A" in saved and "tool_call_id=call-7" in saved
    assert "Traceback" in saved  # text diagnostics remain intact


@pytest.mark.parametrize("style", ["pretty", "json"])
@pytest.mark.parametrize(
    "pattern", ["hardware.camera", "hardware.*", "physiclaw.core.hardware.camera"]
)
def test_component_filter_limits_debug_chatter_but_preserves_all_warnings(
    output_logs, monkeypatch, style, pattern
):
    monkeypatch.setenv("PHYSICLAW_LOG_FORMAT", style)
    monkeypatch.setenv("PHYSICLAW_LOG_COMPONENTS", pattern)
    console, directory = output_logs(logging.DEBUG)
    logging.getLogger("physiclaw.core.hardware.camera").debug("camera diagnostic")
    logging.getLogger("physiclaw.agent.engine.loop").debug("model diagnostic")
    logging.getLogger("physiclaw.agent.engine.loop").warning("provider unavailable")
    logging.getLogger("physiclaw.agent.engine.dispatch").error("action failed")

    text = console.getvalue()
    assert "camera diagnostic" in text and "model diagnostic" not in text
    assert "provider unavailable" in text and "action failed" in text
    assert "model diagnostic" in (directory / "runtime.log").read_text()
    if style == "json":
        assert len([json.loads(line) for line in text.splitlines()]) == 3


def test_unknown_log_format_falls_back_with_an_actionable_warning(
    output_logs, monkeypatch
):
    monkeypatch.setenv("PHYSICLAW_LOG_FORMAT", "typo")
    console, _ = output_logs()

    assert "Unknown PHYSICLAW_LOG_FORMAT='typo'" in console.getvalue()
    assert "choose pretty or json" in console.getvalue()


async def test_log_context_is_isolated_between_tasks_and_restored_after_cancellation():
    formatter = JsonLogFormatter("runtime")
    record = logging.LogRecord(
        "physiclaw.test", logging.INFO, "test.py", 1, "hi", (), None
    )
    outer = bind_log_context(session_id="parent", turn=9)
    gate = asyncio.Event()
    restored = []

    async def worker(sid, *, cancel=False):
        token = bind_log_context(session_id=sid, turn=0)
        try:
            await asyncio.sleep(0)
            assert json.loads(formatter.format(record))["session_id"] == sid
            if cancel:
                gate.set()
                await asyncio.Event().wait()
        finally:
            reset_log_context(token)
            restored.append(record_fields(record))

    try:
        cancelled = asyncio.create_task(worker("cancelled", cancel=True))
        await asyncio.gather(worker("A"), worker("B"), gate.wait())
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert restored == [{"session_id": "parent", "turn": 9}] * 3
        assert record_fields(record) == {"session_id": "parent", "turn": 9}
    finally:
        reset_log_context(outer)
    assert "session_id" not in record_fields(record)


def test_json_boot_errors_are_parseable_before_logging_starts(monkeypatch, capsys):
    import typer

    from physiclaw.cli._format import exit_error

    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    monkeypatch.setenv("PHYSICLAW_LOG_FORMAT", "json")
    with pytest.raises(typer.Exit) as error:
        exit_error("vision model missing", code=2)

    record = json.loads(capsys.readouterr().err)
    assert error.value.exit_code == 2
    assert record["event"] == "process.error" and record["level"] == "error"
    assert record["message"] == "vision model missing"


def test_json_session_footer_exposes_numeric_totals(output_logs, monkeypatch):
    from physiclaw.agent.trace import Trace
    from physiclaw.agent.trace.store import Images

    monkeypatch.setenv("PHYSICLAW_LOG_FORMAT", "json")
    console, _ = output_logs()
    trace = Trace("json-footer", Images("json-footer"))
    trace.write({"event": "usage", "call": "turn", "input": 1234, "output": 50})
    trace.write({"event": "done", "sentinel": "DONE", "recap": "finished"})
    trace.close()

    records = [json.loads(line) for line in console.getvalue().splitlines()]
    assert any(record["event"] == "usage" for record in records)
    (footer,) = [record for record in records if record["event"] == "session.completed"]
    assert footer["event"] == "session.completed" and footer["status"] == "DONE"
    assert footer["session_id"] == "json-footer"
    assert footer["input_tokens"] == 1234 and footer["output_tokens"] == 50
    assert footer["tool_count"] == 0 and footer["turn_count"] == 0
    assert isinstance(footer["duration_ms"], int)


async def test_json_mcp_transport_records_have_tool_events_and_timings(
    output_logs, monkeypatch
):
    monkeypatch.setenv("PHYSICLAW_LOG_FORMAT", "json")
    console, _ = output_logs(logging.DEBUG)

    @logged
    async def probe(*, fail=False):
        if fail:
            raise ValueError("camera offline")
        return "ok"

    await probe()
    with pytest.raises(ValueError):
        await probe(fail=True)

    records = [json.loads(line) for line in console.getvalue().splitlines()]
    assert [r["event"] for r in records] == [
        "tool.started",
        "tool.completed",
        "tool.started",
        "tool.failed",
    ]
    assert all(r["tool"] == "probe" for r in records)
    assert all(r["duration_ms"] >= 0 for r in records if r["event"] != "tool.started")
    assert "ValueError: camera offline" in records[-1]["exception"]


@pytest.mark.parametrize("verbose", [False, True])
def test_now_default_is_task_level_but_every_detail_is_saved(output_logs, verbose):
    console, directory = output_logs(logging.DEBUG if verbose else logging.INFO)
    notices = [
        (
            "physiclaw.cli.server",
            "PhysiClaw 0.7.1",
            "PhysiClaw 0.7.1 — starting hardware",
        ),
        (
            "physiclaw.agent.runtime.runtime",
            "physiclaw ready=True",
            "Ready — watching for tasks",
        ),
        (
            "physiclaw.agent.engine.engine",
            "Session private-session started | model=test/model | triggers=['cron:job-id']",
            "Scheduled task started",
        ),
        (
            "physiclaw.agent.engine.builtin_tool",
            "Plan step started: Find the requested item",
            "Working on: Find the requested item",
        ),
        (
            "physiclaw.agent.trace.trace",
            "END session=private-session outcome=DONE turns=5 tools=10",
            "Task completed (12.0s)",
        ),
    ]
    diagnostics = [
        ("physiclaw.core.server.warm_start", "loaded bundle rotation=2 complete=True"),
        ("physiclaw.core.hardware.iphone", "AT screen position CSS (39,260)"),
        ("physiclaw.common.platform.darwin", "UVC IOKit control live"),
        ("physiclaw.core.hardware.arm", "Arm connected /dev/cu.usbserial-1110"),
        ("physiclaw.core.hardware.arm", "Arm setup complete"),
        ("physiclaw.core.hardware.camera", "capture=3840x2160 requested=3840x2160"),
        ("physiclaw.core.orchestration.rig", "Re-pinned GRBL origin from park spot"),
        (
            "physiclaw.core.server.warm_start",
            "trusting the parked arm; skipping bridge wait",
        ),
        (
            "physiclaw.core.orchestration.perception",
            "exposure tune converged median=15 clip=0%",
        ),
        ("physiclaw.agent.engine.mcp_tool", "MCP client connected"),
        ("physiclaw.agent.engine.loop", "turn 0 PLAYBOOK channel/boot note peek"),
        ("physiclaw.agent.engine.dispatch", "note: conductor observing the screen"),
        (
            "physiclaw.agent.engine.dispatch",
            "peek: id kind label bbox conf <image 197124b>",
        ),
        (
            "physiclaw.core.vision.ui_elements",
            "detected 41 UI elements icon=6.67s ocr=3.46s",
        ),
        (
            "physiclaw.conductor.walk.program",
            "read after peek: thread missing QiaoQian",
        ),
        (
            "physiclaw.core.orchestration.observation",
            "gesture view frame dark; tuning exposure",
        ),
    ]
    for name, full, summary in notices:
        logging.getLogger(name).info(
            full, extra={"console": True, "console_message": summary}
        )
    for name, message in diagnostics:
        logging.getLogger(name).info(message)
    logging.getLogger("physiclaw.agent.runtime.launcher").debug(
        "model selection and config provenance"
    )
    logging.getLogger("physiclaw.cli.sync_official_skills").debug(
        "official skills already up to date"
    )
    logging.getLogger("physiclaw.agent.engine.dispatch").warning(
        "Could not tap; check phone connection", extra={"console": False}
    )

    terminal = console.getvalue()
    saved = (directory / "runtime.log").read_text()
    assert all(full in saved for _, full, _ in notices)
    assert all(message in saved for _, message in diagnostics)
    assert "model selection and config provenance" in saved
    assert "official skills already up to date" in saved
    assert "Could not tap; check phone connection" in terminal
    if verbose:
        assert all(full in terminal for _, full, _ in notices)
        assert all(message in terminal for _, message in diagnostics)
        assert "engine.dispatch" in terminal
    else:
        assert len(terminal.splitlines()) == 6
        assert all(summary in terminal for _, _, summary in notices)
        assert all(message not in terminal for _, message in diagnostics)
        assert "private-session" not in terminal and "model selection" not in terminal
        assert "official skills" not in terminal and "engine." not in terminal
        assert "hardware." not in terminal and "cli.server" not in terminal
