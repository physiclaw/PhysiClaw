"""Exercise the shared output contract through real CLI entry points."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from argparse import Namespace

import pytest

from hardware import buildlog, scheme


def invoke(tmp_path, *args):
    if args[0] == "manual":
        # Supply isolated figure fixtures; this test exercises the real CLI
        # without requiring an entire generated CAD assembly tree.
        figures = tmp_path / "svg"
        figures.mkdir(exist_ok=True)
        for content in (scheme.REPO_ROOT / "hardware/documents/manual/content").glob(
            "*.json"
        ):
            for name in re.findall(
                r'"(?:src|render)"\s*:\s*"([^"/]+\.svg)"', content.read_text()
            ):
                (figures / name).write_text(
                    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>'
                )
    return subprocess.run(
        [sys.executable, "-m", "hardware", *args],
        cwd=scheme.REPO_ROOT,
        env={
            **os.environ,
            "PHYSICLAW_HW_OUTPUT": str(tmp_path),
            "NO_COLOR": "",
            "COLUMNS": "60",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("command", ["check", "manual", "sourcing", "drawing"])
def test_document_and_check_clis_emit_only_saved_json_events(tmp_path, command):
    result = invoke(tmp_path, command, "--log-format", "json")
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stderr == ""
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[0]["event"] == "command_started"
    assert events[-1]["event"] == "command_finished"
    assert events[-1]["status"] == "success"
    assert events[-1]["exit_code"] == 0
    assert {event["schema_version"] for event in events} == {1}
    assert len({event["command"] for event in events}) == 1
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    saved = [
        json.loads(line)
        for line in next((tmp_path / "logs").glob("*.jsonl")).read_text().splitlines()
    ]
    assert saved == events
    assert any(event["event"] == "task_finished" for event in events)


@pytest.mark.parametrize("command", ["check", "manual", "sourcing", "drawing"])
def test_human_output_has_one_result_and_aligned_elapsed_column(tmp_path, command):
    result = invoke(tmp_path, command)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stderr == ""
    assert result.stdout.startswith("Hardware ")
    assert result.stdout.count("SUCCESS") == 1
    assert "INFO" not in result.stdout
    assert "warning:" not in result.stdout.lower()
    assert "\x1b" not in result.stdout
    lines = result.stdout.splitlines()
    heading = next(line for line in lines if "Elapsed" in line)
    timed = [line for line in lines if re.search(r"\d+\.\ds$", line)]
    assert len(timed) >= 2
    assert {len(line) for line in timed} == {len(heading)}
    assert all(len(line) <= 60 for line in lines)


@pytest.mark.parametrize(
    "args",
    [
        ["release", "verify", "--version", "invalid"],
        ["mark", "missing.svg"],
        ["refit", "missing.json"],
        ["replay", "unsupported.txt"],
    ],
)
def test_failed_clis_save_traceback_and_end_with_json_failure(tmp_path, args):
    result = invoke(tmp_path, *args, "--log-format", "json")
    assert result.returncode != 0
    assert result.stderr == ""
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[-1]["event"] == "command_finished"
    assert events[-1]["status"] == "failed"
    assert events[-1]["exit_code"] != 0
    assert any(event.get("traceback") for event in events)


@pytest.mark.parametrize("interrupted", [False, True])
def test_shared_lifecycle_saves_failure_and_cancellation_without_terminal_traceback(
    tmp_path, monkeypatch, capsys, interrupted
):
    monkeypatch.setattr(scheme, "OUTPUT_DIR", tmp_path)
    with pytest.raises(SystemExit) as stopped:
        with buildlog.command("Hardware test"):
            with buildlog.step("Render HTML"):
                buildlog.event("warning", "Hidden warning", level="warning")
                if interrupted:
                    raise KeyboardInterrupt
                raise ValueError("Missing source")
    assert stopped.value.code == (130 if interrupted else 1)
    output = capsys.readouterr().out
    assert ("CANCELLED" if interrupted else "FAILED") in output
    assert "Traceback" not in output
    assert "Hidden warning" not in output
    assert "Details:" in output
    events = [
        json.loads(line)
        for line in next((tmp_path / "logs").glob("*.jsonl")).read_text().splitlines()
    ]
    assert events[-1]["status"] == ("cancelled" if interrupted else "failed")
    assert any(event.get("traceback") for event in events)


def test_subprocess_stdout_and_stderr_are_saved_without_corrupting_json(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(scheme, "OUTPUT_DIR", tmp_path)
    args = Namespace(log_format="json", verbose=False)
    child = (
        "import sys\n"
        'print(\'{"event":"task_finished","schema_version":1,"message":"Child complete"}\')\n'
        "print('native warning', file=sys.stderr)\n"
    )
    with buildlog.command("Hardware subprocess", args):
        output = buildlog.run_process([sys.executable, "-c", child], merge_stderr=False)
    assert json.loads(output)["event"] == "task_finished"
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(
        event.get("child_event", {}).get("event") == "task_finished" for event in events
    )
    assert any(
        event["message"] == "native warning" and event["stream"] == "stderr"
        for event in events
    )
    assert events[-1]["status"] == "success"


def test_subprocess_timeout_retains_partial_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(scheme, "OUTPUT_DIR", tmp_path)
    with pytest.raises(SystemExit, match="1"):
        with buildlog.command("Hardware subprocess"):
            buildlog.run_process(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    "import time; print('partial diagnostics'); time.sleep(30)",
                ],
                timeout=0.5,
            )
    events = [
        json.loads(line)
        for line in next((tmp_path / "logs").glob("*.jsonl")).read_text().splitlines()
    ]
    assert any(event["message"] == "partial diagnostics" for event in events)
    exited = next(event for event in events if event["event"] == "subprocess_exited")
    assert exited["returncode"] != 0
    assert events[-1]["status"] == "failed"


def test_subprocess_collector_failure_stops_child_and_fails_run(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(scheme, "OUTPUT_DIR", tmp_path)
    original_emit = buildlog.BuildLog.emit

    def fail_output(log, event, message, **fields):
        if event == "subprocess_output":
            raise OSError("Cannot save child output")
        original_emit(log, event, message, **fields)

    monkeypatch.setattr(buildlog.BuildLog, "emit", fail_output)
    with pytest.raises(SystemExit, match="1"):
        with buildlog.command("Hardware collector"):
            buildlog.run_process(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    "import time; print('diagnostic'); time.sleep(30)",
                ],
                timeout=5,
            )
    output = capsys.readouterr()
    assert output.err == ""
    assert "FAILED" in output.out and "SUCCESS" not in output.out
    events = [
        json.loads(line)
        for line in next((tmp_path / "logs").glob("*.jsonl")).read_text().splitlines()
    ]
    assert any(
        "Cannot save child output" in event.get("traceback", "") for event in events
    )
    assert (
        next(event for event in events if event["event"] == "subprocess_exited")[
            "returncode"
        ]
        != 0
    )


@pytest.mark.parametrize(
    "child",
    ["raise SystemExit(130)", "import os, signal; os.kill(os.getpid(), signal.SIGINT)"],
)
def test_interrupted_child_produces_cancelled_parent_result(
    tmp_path, monkeypatch, capsys, child
):
    monkeypatch.setattr(scheme, "OUTPUT_DIR", tmp_path)
    with pytest.raises(SystemExit) as stopped:
        with buildlog.command("Hardware child"):
            buildlog.run_process([sys.executable, "-c", child])
    assert stopped.value.code == 130
    output = capsys.readouterr().out
    assert "CANCELLED" in output and "FAILED" not in output
    events = [
        json.loads(line)
        for line in next((tmp_path / "logs").glob("*.jsonl")).read_text().splitlines()
    ]
    assert events[-1]["status"] == "cancelled"


@pytest.mark.parametrize("missing", ["patch", "source"])
def test_replay_missing_inputs_cannot_report_success(
    tmp_path, monkeypatch, capsys, missing
):
    from hardware.assembly.mark import replay

    monkeypatch.setattr(scheme, "OUTPUT_DIR", tmp_path)
    src, patch = tmp_path / "source.svg", tmp_path / "patch.json"
    (src if missing == "patch" else patch).write_text("fixture")
    monkeypatch.setattr(replay, "_resolve_targets", lambda argv: [(src, patch)])
    assert replay.main(["replay"]) == 1
    output = capsys.readouterr().out
    assert "ERROR:" in output and "FAILED" in output and "SUCCESS" not in output


@pytest.mark.parametrize("command", ["parts", "print"])
def test_cad_exports_share_json_contract_with_real_geometry(tmp_path, command):
    if command == "parts":
        script = (
            "from hardware.parts import export_custom\n"
            "export_custom.ALL_PARTS = export_custom.ALL_PARTS[:1]\n"
            "from hardware.__main__ import main\n"
            "raise SystemExit(main(['parts', '--custom', '--log-format', 'json']))\n"
        )
    else:
        script = (
            "from hardware.parts import build_custom_parts as printing\n"
            "printing.CUSTOM_PARTS = printing.CUSTOM_PARTS[:1]\n"
            "raise SystemExit(printing.main(['--log-format', 'json']))\n"
        )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=scheme.REPO_ROOT,
        env={**os.environ, "PHYSICLAW_HW_OUTPUT": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stderr == ""
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert {event["command"] for event in events} == {command}
    assert any(event["event"] == "task_finished" for event in events)
    assert events[-1]["status"] == "success"
    assert events[-1]["exit_code"] == 0
    if command == "parts":
        assert list((tmp_path / "step").glob("*.step"))
    else:
        assert list((tmp_path / "print_3d").glob("*.zip"))


def test_make_check_forwards_format_flags_without_recipe_noise(tmp_path):
    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "hw-check",
            f"HW_DOC={sys.executable} -m hardware",
            "ARGS=--log-format json",
        ],
        cwd=scheme.REPO_ROOT,
        env={**os.environ, "PHYSICLAW_HW_OUTPUT": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[0]["event"] == "command_started"
    assert events[-1]["status"] == "success"
