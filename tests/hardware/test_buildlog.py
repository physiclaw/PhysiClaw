from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from hardware import buildlog


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_terminal_is_concise_but_file_keeps_diagnostics(tmp_path, capsys):
    with buildlog.session(tmp_path) as log:
        buildlog.event("build_plan", "2 assemblies", total=2)
        buildlog.event(
            "variant_finished", "timing details", level="debug", render_s=1.25
        )
        buildlog.event(
            "step_finished", "Completed", stem="frame_10_base", status="success"
        )
        buildlog.event(
            "error", "bad\ninput\x1b[31m", level="error", traceback="full\ntrace"
        )
    output = capsys.readouterr().out
    assert "timing details" not in output
    assert "[1/2] frame_10_base" in output
    assert "Completed" not in output
    assert "INFO" not in output
    assert "ERROR:" in output
    assert "bad\\x0ainput\\x1b[31m" in output
    lines = output.splitlines()
    assert lines[0] == "Hardware build / 2 assemblies"
    header = next(line for line in lines if "Assembly" in line)
    completed = next(line for line in lines if "frame_10_base" in line)
    assert len(header) == len(completed)
    assert header.rstrip().endswith("Elapsed")
    saved = records(log.path)
    assert saved[0]["level"] == "info"
    assert saved[1]["render_s"] == 1.25
    assert saved[2]["message"] == "Completed"
    assert saved[-1]["traceback"] == "full\ntrace"
    assert all(r["timestamp"].endswith("+00:00") for r in saved)


@pytest.mark.parametrize("log_format,verbose", [("json", False), ("text", True)])
def test_detailed_modes_show_debug_events(tmp_path, capsys, log_format, verbose):
    with buildlog.session(tmp_path, log_format=log_format, verbose=verbose):
        buildlog.event("variant_finished", "timing details", level="debug")
    assert "timing details" in capsys.readouterr().out


def test_concurrent_workers_produce_ordered_complete_json_lines(tmp_path, capsys):
    with buildlog.session(tmp_path, log_format="json") as log:

        def worker(index):
            for attempt in range(20):
                line = buildlog.WORKER_PREFIX + json.dumps(
                    {
                        "event": "step_retry",
                        "message": "Retry",
                        "level": "warning",
                        "stem": f"frame_{index}",
                        "attempt": attempt,
                    }
                )
                buildlog.worker_line(line, stems=[f"frame_{index}"], worker_pid=index)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(worker, range(4)))
    saved = records(log.path)
    assert len(saved) == 80
    assert saved == [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["sequence"] for r in saved] == list(range(1, 81))
    assert {r["run_id"] for r in saved} == {log.run_id}


def test_malformed_worker_events_are_preserved_as_diagnostics(tmp_path):
    with buildlog.session(tmp_path) as log:
        for raw in ("native text", "{", "[]", '{"event":"incomplete"}'):
            buildlog.worker_line(
                buildlog.WORKER_PREFIX + raw, stems=["frame_10"], worker_pid=42
            )
    assert all(r["event"] == "worker_output" for r in records(log.path))


def test_runs_have_distinct_logs_and_completed_steps_are_not_double_counted(tmp_path):
    with buildlog.session(tmp_path) as first:
        buildlog.event("build_plan", "One assembly", total=1)
        for _ in range(2):
            buildlog.event(
                "step_finished", "Completed", stem="frame_10_base", status="success"
            )
    with buildlog.session(tmp_path) as second:
        buildlog.event("build_started", "New run")
    assert first.path != second.path
    assert records(first.path)[-1]["completed"] == 1
    assert records(first.path)[-1]["run_id"] != records(second.path)[-1]["run_id"]


def test_crash_path_is_inherited_and_restored_without_overwriting_previous_runs(
    tmp_path, monkeypatch
):
    previous = str(tmp_path / "outer.crash.log")
    monkeypatch.setenv(buildlog.CRASH_LOG_ENV, previous)
    with buildlog.session(tmp_path) as first:
        assert os.environ[buildlog.CRASH_LOG_ENV] == str(first.crash_path)
        first.crash_path.write_text("First run native stack\n")
        with buildlog.session(None, worker=True) as worker:
            assert worker.crash_path == first.crash_path
        assert os.environ[buildlog.CRASH_LOG_ENV] == str(first.crash_path)
    assert os.environ[buildlog.CRASH_LOG_ENV] == previous
    with pytest.raises(ValueError), buildlog.session(tmp_path) as second:
        assert second.crash_path != first.crash_path
        second.crash_path.write_text("Second run native stack\n")
        raise ValueError("Build failed")
    assert os.environ[buildlog.CRASH_LOG_ENV] == previous
    assert first.crash_path.read_text() == "First run native stack\n"
    assert second.crash_path.read_text() == "Second run native stack\n"


def test_worker_batch_size_does_not_replace_build_total(tmp_path, capsys):
    with buildlog.session(tmp_path) as log:
        buildlog.event("build_plan", "71 assemblies", total=71)
        buildlog.worker_line(
            buildlog.WORKER_PREFIX
            + json.dumps(
                {
                    "event": "build_started",
                    "message": "Worker",
                    "level": "debug",
                    "total": 5,
                }
            ),
            stems=["frame_10"],
            worker_pid=123,
        )
        buildlog.worker_line(
            buildlog.WORKER_PREFIX
            + json.dumps(
                {
                    "event": "step_finished",
                    "message": "Completed",
                    "level": "info",
                    "stem": "frame_10",
                    "status": "success",
                    "completed": 1,
                    "total": 5,
                }
            ),
            stems=["frame_10"],
            worker_pid=123,
        )
    assert "[1/71]" in capsys.readouterr().out
    assert records(log.path)[-1]["total"] == 71


@pytest.mark.parametrize("columns", [40, 79, 80, 100, 120])
def test_terminal_wraps_long_messages_without_losing_identifiers(monkeypatch, columns):
    monkeypatch.setattr(
        buildlog.shutil,
        "get_terminal_size",
        lambda **kwargs: os.terminal_size((columns, 24)),
    )
    message = (
        "CAD worker still running: phone_10_extrusion, phone_20_bed_tnut, "
        "phone_30_bed_extrusion, phone_40_bed_frame"
    )
    rendered = buildlog._text_lines(
        dict(level="info", elapsed_s=164.5, worker_pid=12345, message=message)
    )
    lines = rendered.splitlines()
    assert len(lines) > 1
    assert all(len(line) <= columns for line in lines)
    content = "".join(line[16:] for line in lines[1:])
    assert "".join(message.split()) == "".join(content.split())
    assert all(not line.lstrip().startswith("INFO") for line in lines[1:])


def test_terminal_aligns_messages_across_contexts_and_severities(monkeypatch):
    monkeypatch.setattr(
        buildlog.shutil,
        "get_terminal_size",
        lambda **kwargs: os.terminal_size((100, 24)),
    )
    common = dict(elapsed_s=164.5, message="Message starts here", level="info")
    rows = [
        {**common, "stem": "phone_40_bed_frame", "completed": 59, "total": 71},
        {
            **common,
            "stem": "fastener_10_BHCS",
            "completed": 1,
            "total": 71,
            "level": "warning",
        },
        {**common, "worker_pid": 12345},
        common,
    ]
    lines = [buildlog._text_lines(row) for row in rows]
    details = [line.splitlines()[1] for line in lines[:3]]
    assert all(line == " " * 16 + "Message starts here" for line in details)
    assert lines[3].startswith("INFO    Message starts here")
    assert lines[0].index("INFO") == lines[1].index("WARN")
    assert len({len(line.splitlines()[0]) for line in lines}) == 1


def test_terminal_wraps_long_context_and_file_paths_without_truncation(monkeypatch):
    monkeypatch.setattr(
        buildlog.shutil,
        "get_terminal_size",
        lambda **kwargs: os.terminal_size((80, 24)),
    )
    stem = "phone_40_" + "long_assembly_name" * 3
    path = "/some/long/directory/" + "file-name" * 10 + ".json"
    rendered = buildlog._text_lines(
        dict(level="error", elapsed_s=164.5, stem=stem, message=path)
    )
    lines = rendered.splitlines()
    assert all(len(line) <= 80 for line in lines)
    name_lines, path_lines = lines[:3], lines[3:]
    assert "".join(line[16:42].strip() for line in name_lines) == stem
    assert "".join(line[16:].strip() for line in path_lines) == path
    assert name_lines[0].endswith("164.5s")


def test_json_keeps_long_message_on_one_physical_line(tmp_path, capsys):
    message = "phone_10_extrusion, " * 50
    with buildlog.session(tmp_path, log_format="json") as log:
        buildlog.event("worker_running", message, worker_pid=12345)
    output = capsys.readouterr().out.splitlines()
    assert len(output) == 1
    assert json.loads(output[0])["message"] == message
    assert records(log.path)[0]["message"] == message


@pytest.mark.parametrize("verbose", [False, True])
def test_text_hides_warnings_but_preserves_errors_and_saved_warnings(
    tmp_path, capsys, verbose
):
    with buildlog.session(tmp_path, verbose=verbose) as log:
        buildlog.event("worker_failed", "Recoverable warning", level="warning")
        buildlog.event("error", "Actual failure", level="error")
    output = capsys.readouterr().out
    assert "Recoverable warning" not in output
    assert "WARN" not in output
    assert "Actual failure" in output
    assert records(log.path)[0]["level"] == "warning"


def test_default_view_omits_metadata_and_verification_duplicates(tmp_path, capsys):
    with buildlog.session(tmp_path) as log:
        buildlog.event("build_started", "Start banner", stems=None)
        buildlog.event("log_opened", "Internal log path")
        buildlog.event("build_plan", "Internal worker configuration", total=2)
        buildlog.event("cache_checked", "Internal cache path", cached=1, to_build=1)
        buildlog.event("step_cached", "Restored", level="debug", stem="frame_10_base")
        buildlog.event("step_finished", "3.0s", stem="frame_20_next", status="success")
        buildlog.event(
            "step_finished",
            "Verification duplicate",
            stem="frame_20_next",
            status="success",
        )
        buildlog.event(
            "build_summary",
            "Verbose artifact tally",
            complete=2,
            total=2,
            cached=1,
            rebuilt=1,
            reannotated=0,
        )
        buildlog.event(
            "build_finished",
            "Original result",
            status="success",
            exit_code=0,
            output_dir=str(tmp_path),
        )
    output = " ".join(capsys.readouterr().out.split())
    assert "Hardware build / 2 assemblies" in output
    assert "1 cached; 1 to build" in output
    assert "3.0s" not in output
    assert "SUCCESS 2/2 complete" in output
    assert "1 cached, 1 built" in output
    assert all(
        text not in output
        for text in (
            "Start banner",
            "Internal",
            "Verification duplicate",
            "Verbose artifact tally",
        )
    )
    assert len(records(log.path)) == 9
    assert records(log.path)[5]["message"] == "3.0s"


@pytest.mark.parametrize("columns", [60, 80, 120])
def test_terminal_ends_with_elapsed_time_and_omits_step_duration(monkeypatch, columns):
    monkeypatch.setattr(
        buildlog.shutil,
        "get_terminal_size",
        lambda **kwargs: os.terminal_size((columns, 24)),
    )
    rendered = buildlog._text_lines(
        dict(
            event="step_finished",
            status="success",
            elapsed_s=257.6,
            level="info",
            completed=29,
            total=71,
            stem="idler_21_ru",
            message="3.0s",
        ),
        show_level=False,
    )
    assert rendered.startswith("[29/71]")
    assert rendered.rstrip().endswith("257.6s")
    assert "3.0s" not in rendered
    assert all(len(line) <= columns for line in rendered.splitlines())
    if columns >= 80:
        assert (
            len(rendered) <= 48
        )  # A wide terminal must not add an empty message column.


@pytest.mark.parametrize("columns", [24, 40, 60, 79, 80, 120])
def test_terminal_time_column_matches_header_for_all_rows(monkeypatch, capsys, columns):
    monkeypatch.setattr(
        buildlog.shutil,
        "get_terminal_size",
        lambda **kwargs: os.terminal_size((columns, 24)),
    )
    monkeypatch.setattr(buildlog.time, "monotonic", lambda: 51.4)
    monkeypatch.setenv("NO_COLOR", "")
    log = buildlog.BuildLog()
    log.started = 0
    log.emit("build_plan", "71 assemblies", total=71)
    stems = ("linear_33_x", "linear_40_idler_lj1", "assembly_" + "long_name_" * 4)
    for stem in stems:
        log.emit("step_finished", "Completed", stem=stem, status="success")
    log.emit(
        "error",
        "Assembly export failed; inspect the saved log for details",
        level="error",
        stem="linear_33_x",
    )
    log.emit(
        "error",
        "A build-wide error with enough detail to require several lines",
        level="error",
    )
    log.emit("build_finished", "Cancelled", status="cancelled", exit_code=130)
    lines = capsys.readouterr().out.splitlines()
    header = next(line for line in lines if "Elapsed" in line)
    timed = [line for line in lines if line.endswith("51.4s")]
    assert len(timed) == len(stems) + 3
    assert all(len(line) == len(header) for line in timed)
    assert all(len(line) <= columns for line in lines)
    assert "CANCELLED" in " ".join(lines)
    # Wrapping preserves every part of long assembly names and error details.
    first = lines.index(timed[2])
    last = lines.index(timed[3])
    name_lines = [lines[first][8:-10], *(line[8:] for line in lines[first + 1 : last])]
    assert "".join(part.strip() for part in name_lines) == stems[2]
    detail_lines = lines[last + 1 : lines.index(timed[4])]
    assert "inspectthesavedlog" in "".join("".join(detail_lines).split())


def test_python_warnings_are_saved_with_source_and_kept_out_of_terminal(
    tmp_path, capsys
):
    import warnings

    original_handler = warnings.showwarning
    with buildlog.session(tmp_path) as log:
        for _ in range(2):
            warnings.warn("Geometry warning", RuntimeWarning)
    saved = records(log.path)
    assert len(saved) == 2
    assert all(
        r["event"] == "python_warning" and r["level"] == "warning" for r in saved
    )
    assert all(r["category"] == "RuntimeWarning" and r["lineno"] > 0 for r in saved)
    assert warnings.showwarning is original_handler
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("verbose", [False, True])
def test_status_updates_are_saved_without_terminal_output(tmp_path, capsys, verbose):
    with buildlog.session(tmp_path, verbose=verbose) as log:
        buildlog.event(
            "worker_running",
            "All batch stems: phone_10_extrusion, phone_40_bed_frame",
            worker_pid=12345,
        )
        buildlog.event("worker_running", "Still working", worker_pid=12345)
    assert capsys.readouterr().out == ""
    saved = records(log.path)
    assert len(saved) == 2
    assert all(r["event"] == "worker_running" for r in saved)
    assert saved[-1]["message"] == "Still working"


@pytest.mark.parametrize(
    "tty,no_color,term,colored",
    [
        (False, None, "xterm-256color", False),
        (True, None, "xterm-256color", True),
        (True, "", "xterm-256color", False),
        (True, "1", "xterm-256color", False),
        (True, None, "dumb", False),
    ],
)
def test_terminal_style_respects_output_and_environment(
    tmp_path, monkeypatch, capsys, tty, no_color, term, colored
):
    monkeypatch.setattr(buildlog.sys.stdout, "isatty", lambda: tty)
    monkeypatch.setenv("TERM", term)
    monkeypatch.delenv("NO_COLOR", raising=False)
    if no_color is not None:
        monkeypatch.setenv("NO_COLOR", no_color)
    with buildlog.session(tmp_path) as log:
        buildlog.event("build_plan", "Plan", total=1)
        buildlog.event(
            "step_finished", "14.5s", status="success", stem="fastener_10_BHCS"
        )
        buildlog.event("worker_failed", "Warning hidden", level="warning")
        buildlog.event("worker_running", "Still working", worker_pid=123)
        buildlog.event(
            "build_finished",
            "Original result",
            status="success",
            exit_code=0,
            output_dir=str(tmp_path),
        )
    output = capsys.readouterr().out
    assert ("\033[" in output) is colored
    plain = re.sub(r"\033\[[0-9;]*m", "", output)
    assert plain.startswith("Hardware build / 1 assembly\n")
    assert "Elapsed" in plain and "SUCCESS  1/1 complete" in plain
    assert "Output:" in plain
    assert all(
        text not in plain
        for text in ("INFO", "Warning hidden", "Still working", "14.5s")
    )
    saved = records(log.path)
    assert saved[1]["message"] == "14.5s"
    assert all("\033" not in r["message"] for r in saved)


def test_json_mode_stays_plain_even_on_color_terminal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(buildlog.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    with buildlog.session(tmp_path, log_format="json") as log:
        buildlog.event("build_plan", "Plan", total=1)
        buildlog.event("build_finished", "Result", status="success", exit_code=0)
    output = capsys.readouterr().out
    assert "\033" not in output
    assert [json.loads(line) for line in output.splitlines()] == records(log.path)


def test_failure_footer_has_explicit_status_and_saved_log_path(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(buildlog.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    with buildlog.session(tmp_path) as log:
        buildlog.event("build_plan", "Plan", total=1)
        buildlog.event(
            "build_finished",
            "Failure details",
            status="failed",
            exit_code=1,
            level="error",
            log_path=str(log.path),
        )
    output = capsys.readouterr().out
    assert "\033[1;31m" in output
    assert "\033[1;32m" not in output
    assert "FAILED  0/1 complete" in output
    assert "Details:" in output
    assert records(log.path)[-1]["exit_code"] == 1
