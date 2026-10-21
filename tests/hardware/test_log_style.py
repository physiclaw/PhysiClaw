"""Standalone handler contract; runnable with --noconftest and pytest alone."""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hardware import buildlog, log_style


class Terminal(io.StringIO):
    codec = "utf-8"

    @property
    def encoding(self):
        return self.codec

    def isatty(self):
        return True

    def write(self, text):
        text.encode(self.encoding)
        return super().write(text)


@pytest.fixture
def terminal_output(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.delenv("CI", raising=False)

    def activate():
        stream = Terminal()
        monkeypatch.setattr(sys, "stdout", stream)
        return stream

    return activate


def visible_lines(text):
    """Interpret CR overwrites so assertions check what remains on screen."""
    lines = []
    for line in text.split("\n"):
        screen = io.StringIO()
        for update in line.split("\r"):
            screen.seek(0)
            screen.write(update)
        lines.append(screen.getvalue().rstrip())
    return lines


def saved_events(log):
    return [
        json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()
    ]


def test_interactive_build_reuses_one_progress_row_and_saves_every_completion(
    tmp_path, terminal_output
):
    terminal_output = terminal_output()
    with buildlog.session(tmp_path) as log:
        buildlog.event("build_plan", "Plan", total=71)
        for i in range(1, 72):
            buildlog.event(
                "step_finished", "Complete", stem=f"part_{i:02d}", status="success"
            )
        buildlog.event("build_finished", "Done", status="success", exit_code=0)
    displayed = visible_lines(terminal_output.getvalue())
    progress = [line for line in displayed if line.startswith("[")]
    assert len(progress) == 1
    assert progress[0].startswith("[71/71] part_71")
    assert "SUCCESS  71/71 complete" in "\n".join(displayed)
    assert terminal_output.getvalue().endswith("\n")
    saved = saved_events(log)
    assert [e["completed"] for e in saved if e["event"] == "step_finished"] == list(
        range(1, 72)
    )


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_live_progress_finishes_before_errors_and_final_status(
    tmp_path, terminal_output, status
):
    terminal_output = terminal_output()
    with buildlog.session(tmp_path) as log:
        buildlog.event("build_plan", "Plan", total=2)
        buildlog.event("step_finished", "Complete", stem="frame_10", status="success")
        buildlog.event("error", "Keep this diagnosis", level="error")
        buildlog.event(
            "build_finished",
            "Stopped",
            status=status,
            exit_code=130 if status == "cancelled" else 1,
            log_path=str(log.path),
        )
    displayed = "\n".join(visible_lines(terminal_output.getvalue()))
    assert "[1/2] frame_10" in displayed
    assert "ERROR: Keep this diagnosis" in displayed
    assert status.upper() in displayed and "Details:" in displayed
    assert terminal_output.getvalue().endswith("\n")


@pytest.mark.parametrize("codec", ["utf-8", "ascii"])
def test_live_rows_fit_after_resize_and_keep_full_names_on_disk(
    tmp_path, terminal_output, monkeypatch, codec
):
    terminal_output = terminal_output()
    terminal_output.codec = codec
    width = 80
    monkeypatch.setattr(
        log_style.shutil,
        "get_terminal_size",
        lambda **_: os.terminal_size((width, 24)),
    )
    name = "camera_摄像头_" + "long_assembly_name_" * 5
    with buildlog.session(tmp_path) as log:
        buildlog.event("build_plan", "Plan", total=3)
        for width in (80, 40, 16):
            start = len(terminal_output.getvalue())
            buildlog.event("step_finished", "Complete", stem=name, status="success")
            # Repeated verification is intentionally hidden; use a unique stem next.
            name += "x"
            update = terminal_output.getvalue()[start:]
            assert update.startswith("\r") and "\n" not in update
            assert log_style._cell_width(update[1:]) < width
    assert "..." in terminal_output.getvalue()
    assert all(
        len(e["stem"]) > 80 for e in saved_events(log) if e["event"] == "step_finished"
    )


def test_interactive_tasks_print_separate_rows(tmp_path, terminal_output):
    terminal_output = terminal_output()
    with buildlog.session(tmp_path):
        with buildlog.step("Build the print package"):
            pass
        with buildlog.step("Render the manual"):
            pass
    output = terminal_output.getvalue()
    first, second = output.splitlines()
    assert "[1] Build the print package" in first
    assert "[2] Render the manual" in second
    assert "\r" not in output
    assert output.endswith("\n")


@pytest.mark.parametrize("mode", ["pipe", "verbose", "json", "worker", "dumb", "ci"])
def test_append_only_modes_never_receive_live_control_characters(
    tmp_path, terminal_output, monkeypatch, mode
):
    terminal_output = terminal_output()
    if mode == "pipe":
        monkeypatch.setattr(terminal_output, "isatty", lambda: False)
    elif mode == "dumb":
        monkeypatch.setenv("TERM", "dumb")
    elif mode == "ci":
        monkeypatch.setenv("CI", "true")
    with buildlog.session(
        tmp_path,
        log_format="json" if mode == "json" else "text",
        verbose=mode == "verbose",
        worker=mode == "worker",
    ):
        buildlog.event("build_plan", "Plan", total=2)
        for stem in ("frame_10", "frame_20"):
            buildlog.event("step_finished", "Complete", stem=stem, status="success")
    output = terminal_output.getvalue()
    assert "\r" not in output
    assert "frame_10" in output and "frame_20" in output


def test_redirection_finishes_the_live_row_on_its_original_stream(
    tmp_path, terminal_output, monkeypatch
):
    terminal_output = terminal_output()
    redirected = io.StringIO()
    with buildlog.session(tmp_path):
        buildlog.event("step_finished", "Complete", stem="frame_10", status="success")
        monkeypatch.setattr(sys, "stdout", redirected)
        buildlog.event("step_finished", "Complete", stem="frame_20", status="success")
    assert terminal_output.getvalue().endswith("\n")
    assert "frame_10" not in redirected.getvalue()
    assert "frame_20" in redirected.getvalue() and "\r" not in redirected.getvalue()


def test_collector_uses_isolated_standard_logging_and_preserves_domain_fields(
    tmp_path, capsys
):
    root = logging.getLogger()
    handlers, level, disabled = list(root.handlers), root.level, root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with buildlog.session(tmp_path, log_format="json") as log:
            assert isinstance(log._logger, logging.Logger)
            assert not log._logger.propagate
            assert isinstance(log._logger.handlers[0], logging.FileHandler)
            assert isinstance(log._logger.handlers[0].formatter, logging.Formatter)
            buildlog.event(
                "source_detail",
                "Complete diagnostic",
                level="debug",
                filename="part.py",
                process="producer",
                thread="worker",
                levelname="domain value",
            )
            assert root.handlers == handlers and root.level == level
        assert all(handler._closed for handler in log._logger.handlers)
    finally:
        logging.disable(disabled)
    (event,) = saved_events(log)
    assert event["level"] == "debug"
    assert event["filename"] == "part.py" and event["levelname"] == "domain value"
    assert "hardware_state" not in event and "hardware_json" not in event
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        event
    ]


def test_file_failure_propagates_before_a_success_can_reach_the_console(
    tmp_path, monkeypatch, capsys
):
    class BrokenFile:
        def write(self, text):
            raise OSError("disk full")

    with buildlog.session(tmp_path) as log:
        file_handler = log._logger.handlers[0]
        with monkeypatch.context() as fault:
            fault.setattr(file_handler, "stream", BrokenFile())
            with pytest.raises(OSError, match="disk full"):
                buildlog.event(
                    "build_finished", "Complete", status="success", exit_code=0
                )
    assert capsys.readouterr().out == ""
    assert saved_events(log) == []


@pytest.mark.parametrize("log_format,worker", [("json", False), ("text", True)])
def test_broken_protocol_output_is_a_hard_failure(
    tmp_path, monkeypatch, log_format, worker
):
    class BrokenConsole(io.StringIO):
        def write(self, text):
            raise BrokenPipeError("collector disconnected")

    with buildlog.session(tmp_path, log_format=log_format, worker=worker) as log:
        with monkeypatch.context() as fault:
            fault.setattr(sys, "stdout", BrokenConsole())
            with pytest.raises(BrokenPipeError, match="collector disconnected"):
                buildlog.event("step_started", "Started", stem="frame_10")
    if not worker:
        assert saved_events(log)[0]["event"] == "step_started"


def test_closed_pretty_console_does_not_interrupt_complete_file_capture(
    tmp_path, monkeypatch
):
    closed = io.StringIO()
    closed.close()
    with buildlog.session(tmp_path) as log:
        with monkeypatch.context() as fault:
            fault.setattr(sys, "stdout", closed)
            buildlog.event(
                "error", "Keep this diagnosis", level="error", traceback="Full stack"
            )
    assert saved_events(log)[0]["traceback"] == "Full stack"


@pytest.mark.parametrize(
    "log_format,worker", [("text", False), ("json", False), ("text", True)]
)
def test_limited_encodings_preserve_original_json_values(
    tmp_path, monkeypatch, log_format, worker
):
    data = io.BytesIO()
    stream = io.TextIOWrapper(data, encoding="ascii")
    message = "Camera 摄像头 · bad surrogate \ud800"
    try:
        with buildlog.session(tmp_path, log_format=log_format, worker=worker) as log:
            with monkeypatch.context() as output:
                output.setattr(sys, "stdout", stream)
                buildlog.event("diagnostic", message)
        rendered = data.getvalue().decode("ascii")
        if worker:
            assert (
                json.loads(rendered.removeprefix(buildlog.WORKER_PREFIX))["message"]
                == message
            )
        else:
            assert saved_events(log)[0]["message"] == message
            if log_format == "json":
                assert json.loads(rendered)["message"] == message
            else:
                assert "\\u6444" in rendered
    finally:
        stream.close()


def test_backend_runs_from_hardware_alone_without_site_packages(tmp_path):
    package = tmp_path / "hardware"
    package.mkdir()
    source = Path(buildlog.__file__).parent
    for name in ("buildlog.py", "log_style.py", "scheme.py"):
        shutil.copyfile(source / name, package / name)
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import sys\n"
            "from pathlib import Path\n"
            "sys.modules['physiclaw'] = None\n"
            "sys.modules['build123d'] = None\n"
            "from hardware import buildlog\n"
            "with buildlog.session(Path('output'), log_format='json'):\n"
            "    buildlog.event('independent', 'Hardware only', level='debug')\n",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout)["event"] == "independent"
