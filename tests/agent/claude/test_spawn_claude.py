"""Tests for `spawn_claude` — async subprocess + retry loop.

Helper coverage lives in `test_spawn.py`; this file owns the
spawn_claude flow with a fake `asyncio.create_subprocess_exec`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from physiclaw.agent.claude import spawn as spawn_mod
from physiclaw.agent.runtime.hook import Trigger

# Mutation audit: accept prompt/log wording, diagnostic labels and traceback
# detail, a 30s -> 31s exit grace, equivalent first-line splitting and overwritten
# initial status. Both "killed" labels remain nonzero exits. CLI permissions,
# metadata, cleanup, finite waits and session outcomes are checked independently.

# ---------- Fakes ----------


class _FakeStdout:
    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeProc:
    def __init__(self, *, lines: list[bytes], returncode: int = 0):
        self.stdout = _FakeStdout(lines)
        self.returncode = None
        self._exitcode = returncode
        self.killed = False

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self._exitcode
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _result_line(
    status_line: str = ">> DONE - all good", *, num_turns: int = 5
) -> list[bytes]:
    """Build a fake stream-json sequence ending in a result event with
    a final assistant text containing the sentinel."""
    asst = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": status_line}]},
    }
    return [
        json.dumps(asst).encode() + b"\n",
        json.dumps(
            {
                "type": "result",
                "num_turns": num_turns,
                "result": "all good",
            }
        ).encode()
        + b"\n",
        b"",
    ]


@pytest.fixture
def patch_environment(mocker, tmp_path: Path):
    """Stub everything spawn_claude touches except create_subprocess_exec."""
    mocker.patch.object(spawn_mod, "_warn_stray_context")
    mocker.patch.object(spawn_mod, "_mcp_tools", return_value=[])
    mocker.patch.object(spawn_mod.skill, "discover", return_value={})
    mocker.patch.object(
        spawn_mod,
        "_render_system_prompt",
        return_value="SYSTEM",
    )
    mocker.patch.object(spawn_mod, "_child_env", return_value={})

    # CLAUDE.md must "exist" for _build_cmd not to raise.
    fake_md = tmp_path / "CLAUDE.md"
    fake_md.write_text("doctrine")
    mocker.patch.object(spawn_mod, "CLAUDE_MD", fake_md)
    mocker.patch.object(spawn_mod, "_mcp_config", return_value="{}")
    mocker.patch.object(spawn_mod, "PROJECT_ROOT", tmp_path)

    log_dir = tmp_path / "logs"
    mocker.patch.object(spawn_mod, "LOG_DIR", log_dir)

    # prepare_plugin_dir returns a dir we can rmtree without trouble.
    plugin = tmp_path / "plugin"

    def _prep(sid, skills=None):
        plugin.mkdir(parents=True, exist_ok=True)
        return plugin

    mocker.patch.object(spawn_mod, "prepare_plugin_dir", side_effect=_prep)
    mocker.patch.object(spawn_mod.asyncio, "sleep")  # collapse RETRY_BACKOFF
    # Keep the WAIT-close job detection off the developer's real jobs.md.
    mocker.patch.object(spawn_mod.engine_jobs, "job_ids", return_value=set())
    return {"plugin": plugin, "log_dir": log_dir}


# ---------- spawn_claude ----------


@pytest.mark.asyncio
async def test_spawn_claude_done_on_first_attempt(
    mocker,
    patch_environment,
) -> None:
    proc = _FakeProc(lines=_result_line(">> DONE - ok"))

    async def _exec(*args, **kwargs):
        return proc

    mocker.patch.object(
        spawn_mod.asyncio,
        "create_subprocess_exec",
        side_effect=_exec,
    )
    spawn_spy = mocker.spy(spawn_mod, "prepare_plugin_dir")

    await spawn_mod.spawn_claude(
        [Trigger(description="t")],
        model_id="opus",
    )

    spawn_spy.assert_called_once()
    # Plugin dir cleaned up.
    assert not patch_environment["plugin"].exists()


@pytest.mark.asyncio
async def test_spawn_claude_retries_on_undone(
    mocker,
    patch_environment,
) -> None:
    """Two UNDONE attempts (no sentinel), third returns DONE."""
    mocker.patch.object(spawn_mod, "MAX_ATTEMPTS", 3)

    procs = [
        _FakeProc(lines=[b"\n", b""]),  # no result, no sentinel → UNDONE
        _FakeProc(lines=[b"\n", b""]),  # UNDONE
        _FakeProc(lines=_result_line(">> DONE - finally")),
    ]
    procs_iter = iter(procs)

    async def _exec(*args, **kwargs):
        return next(procs_iter)

    mocker.patch.object(
        spawn_mod.asyncio,
        "create_subprocess_exec",
        side_effect=_exec,
    )

    await spawn_mod.spawn_claude(
        [Trigger(description="t")],
        model_id="opus",
    )

    # All three attempts ran.
    sleep_spy = spawn_mod.asyncio.sleep
    # Backoff was awaited twice (between attempts 1→2 and 2→3).
    assert sleep_spy.call_count == 2


@pytest.mark.asyncio
async def test_spawn_claude_gives_up_after_max_undone(
    mocker,
    patch_environment,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    mocker.patch.object(spawn_mod, "MAX_ATTEMPTS", 2)

    proc = _FakeProc(lines=[b"\n", b""])

    async def _exec(*args, **kwargs):
        return proc

    mocker.patch.object(
        spawn_mod.asyncio,
        "create_subprocess_exec",
        side_effect=_exec,
    )

    with caplog.at_level(logging.ERROR, logger="physiclaw.agent.claude.spawn"):
        await spawn_mod.spawn_claude(
            [Trigger(description="t")],
            model_id="opus",
        )

    assert any("giving up after 2 UNDONE" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_spawn_claude_logs_nonzero_exit(
    mocker,
    patch_environment,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    proc = _FakeProc(lines=_result_line(">> DONE - x"), returncode=1)

    async def _exec(*args, **kwargs):
        return proc

    mocker.patch.object(
        spawn_mod.asyncio,
        "create_subprocess_exec",
        side_effect=_exec,
    )

    with caplog.at_level(logging.ERROR, logger="physiclaw.agent.claude.spawn"):
        await spawn_mod.spawn_claude(
            [Trigger(description="t")],
            model_id="opus",
        )

    assert any("claude exited 1" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_spawn_claude_kills_on_timeout(
    mocker,
    patch_environment,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    proc = _FakeProc(lines=[b"\n", b""])

    async def _exec(*args, **kwargs):
        return proc

    mocker.patch.object(
        spawn_mod.asyncio,
        "create_subprocess_exec",
        side_effect=_exec,
    )
    mocker.patch.object(
        spawn_mod,
        "_stream",
        side_effect=asyncio.TimeoutError,
    )
    mocker.patch.object(spawn_mod, "MAX_ATTEMPTS", 1)

    with caplog.at_level(logging.ERROR, logger="physiclaw.agent.claude.spawn"):
        await spawn_mod.spawn_claude(
            [Trigger(description="t")],
            model_id="opus",
        )

    assert proc.killed is True
    assert any("killed after" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_spawn_claude_cleans_plugin_dir_on_failure(
    mocker,
    patch_environment,
) -> None:
    """Even when subprocess creation fails, plugin dir gets rmtree'd."""

    async def _bad_exec(*args, **kwargs):
        raise RuntimeError("can't fork")

    mocker.patch.object(
        spawn_mod.asyncio,
        "create_subprocess_exec",
        side_effect=_bad_exec,
    )
    mocker.patch.object(spawn_mod, "MAX_ATTEMPTS", 1)

    with pytest.raises(RuntimeError, match="can't fork"):
        await spawn_mod.spawn_claude(
            [Trigger(description="t")],
            model_id="opus",
        )

    assert not patch_environment["plugin"].exists()


@pytest.mark.asyncio
async def test_spawn_claude_logs_done_summary_on_clean_exit(
    mocker,
    patch_environment,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    proc = _FakeProc(
        lines=_result_line(">> DONE - turns ok", num_turns=12),
    )

    async def _exec(*args, **kwargs):
        return proc

    mocker.patch.object(
        spawn_mod.asyncio,
        "create_subprocess_exec",
        side_effect=_exec,
    )

    with caplog.at_level(logging.INFO, logger="physiclaw.agent.claude.spawn"):
        await spawn_mod.spawn_claude(
            [Trigger(description="t")],
            model_id="opus",
        )

    assert any("turns=12" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_spawn_claude_first_attempt_no_backoff(
    mocker,
    patch_environment,
) -> None:
    """Backoff sleep only fires on retry, never before the first attempt."""
    proc = _FakeProc(lines=_result_line(">> DONE - x"))

    async def _exec(*args, **kwargs):
        return proc

    mocker.patch.object(
        spawn_mod.asyncio,
        "create_subprocess_exec",
        side_effect=_exec,
    )

    sleep_spy = spawn_mod.asyncio.sleep  # already patched in fixture

    await spawn_mod.spawn_claude(
        [Trigger(description="t")],
        model_id="opus",
    )

    sleep_spy.assert_not_called()


@pytest.mark.asyncio
async def test_spawn_claude_kills_proc_when_stream_raises_unexpectedly(
    mocker,
    patch_environment,
) -> None:
    """An unexpected error mid-stream must never orphan a live claude
    subprocess driving the phone — kill before propagating."""

    class _LiveProc(_FakeProc):
        def __init__(self):
            super().__init__(lines=[b""])
            self.returncode = None  # still running

        async def wait(self) -> int:
            self.returncode = -9
            return self.returncode

    proc = _LiveProc()

    async def _exec(*args, **kwargs):
        return proc

    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", side_effect=_exec)
    mocker.patch.object(spawn_mod, "_stream", side_effect=RuntimeError("boom"))
    mocker.patch.object(spawn_mod, "MAX_ATTEMPTS", 1)

    with pytest.raises(RuntimeError, match="boom"):
        await spawn_mod.spawn_claude([Trigger(description="t")], model_id="opus")

    assert proc.killed is True


@pytest.mark.asyncio
async def test_spawn_claude_kills_on_oversized_output_line(
    mocker,
    patch_environment,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """readline raises ValueError past the buffer limit — treated like a
    timeout: kill + 'killed' status, not a crash."""
    import logging

    proc = _FakeProc(lines=[b""])

    async def _exec(*args, **kwargs):
        return proc

    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", side_effect=_exec)
    mocker.patch.object(
        spawn_mod, "_stream", side_effect=ValueError("Separator is not found")
    )
    mocker.patch.object(spawn_mod, "MAX_ATTEMPTS", 1)

    with caplog.at_level(logging.ERROR, logger="physiclaw.agent.claude.spawn"):
        await spawn_mod.spawn_claude([Trigger(description="t")], model_id="opus")

    assert proc.killed is True
    assert any("stream buffer" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_spawn_claude_wait_without_job_schedules_followup(
    mocker,
    patch_environment,
) -> None:
    """A WAIT close with no session-created job gets the contract's
    auto-scheduled follow-up — the doctrine's promise, previously
    implemented only by the native engine."""
    upsert = mocker.patch("physiclaw.agent.engine.jobs.upsert_auto_wait_check")
    proc = _FakeProc(lines=_result_line(">> WAIT - awaiting user reply"))

    async def _exec(*args, **kwargs):
        return proc

    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", side_effect=_exec)

    await spawn_mod.spawn_claude([Trigger(description="t")], model_id="opus")

    upsert.assert_called_once()


@pytest.mark.asyncio
async def test_spawn_claude_wait_with_new_job_skips_followup(
    mocker,
    patch_environment,
) -> None:
    """jobs.md gained an id during the wake → the session scheduled its
    own resume job; the generic follow-up must stay out of its way."""
    upsert = mocker.patch("physiclaw.agent.engine.jobs.upsert_auto_wait_check")
    # Before-snapshot empty; after-snapshot carries the new job.
    mocker.patch.object(
        spawn_mod.engine_jobs,
        "job_ids",
        side_effect=[set(), {"user-latte-2026-07-16"}],
    )
    proc = _FakeProc(lines=_result_line(">> WAIT - resume job created"))

    async def _exec(*args, **kwargs):
        return proc

    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", side_effect=_exec)

    await spawn_mod.spawn_claude([Trigger(description="t")], model_id="opus")

    upsert.assert_not_called()


@pytest.mark.asyncio
async def test_spawn_claude_wait_followup_ignores_earlier_attempt_jobs(
    mocker,
    patch_environment,
) -> None:
    """The before-snapshot is per attempt: a job written during a killed
    UNDONE attempt must not suppress the follow-up when the retry closes
    WAIT without its own resume job."""
    upsert = mocker.patch("physiclaw.agent.engine.jobs.upsert_auto_wait_check")
    mocker.patch.object(spawn_mod, "MAX_ATTEMPTS", 2)
    # attempt 1 before: empty; a job lands during attempt 1, which dies
    # UNDONE (no after-read). attempt 2 before AND after both see it →
    # no diff → the WAIT close still gets the generic follow-up.
    mocker.patch.object(
        spawn_mod.engine_jobs,
        "job_ids",
        side_effect=[set(), {"leftover-a1"}, {"leftover-a1"}],
    )
    procs = iter(
        [
            _FakeProc(lines=[b"\n", b""]),  # UNDONE
            _FakeProc(lines=_result_line(">> WAIT - awaiting reply")),
        ]
    )

    async def _exec(*args, **kwargs):
        return next(procs)

    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", side_effect=_exec)

    await spawn_mod.spawn_claude([Trigger(description="t")], model_id="opus")

    upsert.assert_called_once()


@pytest.mark.asyncio
async def test_spawn_claude_done_never_schedules_followup(
    mocker,
    patch_environment,
) -> None:
    upsert = mocker.patch("physiclaw.agent.engine.jobs.upsert_auto_wait_check")
    proc = _FakeProc(lines=_result_line(">> DONE - ok"))

    async def _exec(*args, **kwargs):
        return proc

    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", side_effect=_exec)

    await spawn_mod.spawn_claude([Trigger(description="t")], model_id="opus")

    upsert.assert_not_called()


@pytest.mark.asyncio
async def test_spawn_claude_stuck_is_final_no_retry(
    mocker,
    patch_environment,
) -> None:
    """A model-declared STUCK is a considered close on this path — it must
    not be re-spawned (only UNDONE retries)."""
    mocker.patch.object(spawn_mod, "MAX_ATTEMPTS", 3)
    procs = iter([_FakeProc(lines=_result_line(">> STUCK - captcha wall"))])

    async def _exec(*args, **kwargs):
        return next(procs)  # a second attempt would exhaust the iterator

    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", side_effect=_exec)
    spawn_spy = mocker.spy(spawn_mod, "prepare_plugin_dir")

    await spawn_mod.spawn_claude([Trigger(description="t")], model_id="opus")

    spawn_spy.assert_called_once()


@pytest.mark.asyncio
async def test_spawn_claude_bounds_post_eof_wait(
    mocker,
    patch_environment,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A child that closes stdout but never exits must be killed after
    the EXIT_WAIT_SECONDS grace, not awaited forever."""
    import logging

    class _HangingProc(_FakeProc):
        def __init__(self):
            super().__init__(lines=[b""])
            self.returncode = None
            self._waits = 0

        async def wait(self) -> int:
            self._waits += 1
            if self.killed:
                self.returncode = -9
                return self.returncode
            await asyncio.sleep(3600)  # never exits on its own

    proc = _HangingProc()

    async def _exec(*args, **kwargs):
        return proc

    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", side_effect=_exec)
    mocker.patch.object(spawn_mod, "EXIT_WAIT_SECONDS", 0.01)
    mocker.patch.object(spawn_mod, "MAX_ATTEMPTS", 1)

    with caplog.at_level(logging.ERROR, logger="physiclaw.agent.claude.spawn"):
        await spawn_mod.spawn_claude([Trigger(description="t")], model_id="opus")

    assert proc.killed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [
        ["unexpected"],
        "unexpected",
        12,
        {"input_tokens": "invalid"},
        {"output_tokens": float("nan")},
        {"cache_read_input_tokens": float("inf")},
    ],
)
async def test_regression_bad_usage_metadata_cannot_crash_a_done_session(
    mocker, patch_environment, usage
):
    lines = _result_line(">> DONE - task complete")
    lines[1] = json.dumps({"type": "result", "usage": usage}).encode() + b"\n"
    proc = _FakeProc(lines=lines)
    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", return_value=proc)

    outcome = await spawn_mod.spawn_claude([Trigger("check")], model_id="opus")

    assert outcome.status == "DONE"
    assert not patch_environment["plugin"].exists()


class _ExitedDuringKill(_FakeProc):
    def kill(self):
        self.returncode = 0
        raise ProcessLookupError("already exited")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.TimeoutError, ValueError("line too long")])
async def test_regression_exit_race_during_kill_preserves_undone_outcome(
    mocker, patch_environment, error
):
    proc = _ExitedDuringKill(lines=[])
    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", return_value=proc)
    mocker.patch.object(proc.stdout, "readline", side_effect=error)
    mocker.patch.object(spawn_mod, "MAX_ATTEMPTS", 1)

    outcome = await spawn_mod.spawn_claude([Trigger("check")], model_id="opus")

    assert outcome.status is None
    assert not patch_environment["plugin"].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [RuntimeError("stream failed"), asyncio.CancelledError("cancelled")]
)
async def test_regression_cleanup_exit_race_keeps_original_exception_and_closes_log(
    mocker, patch_environment, error
):
    proc = _ExitedDuringKill(lines=[])
    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", return_value=proc)
    mocker.patch.object(proc.stdout, "readline", side_effect=error)
    close = mocker.spy(spawn_mod._SessionLog, "close")

    with pytest.raises(type(error), match=str(error)):
        await spawn_mod.spawn_claude([Trigger("check")], model_id="opus")

    close.assert_called_once()
    assert not patch_environment["plugin"].exists()


@pytest.mark.asyncio
async def test_regression_cancelled_cleanup_still_closes_log(mocker, patch_environment):
    proc = _FakeProc(lines=[])
    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", return_value=proc)
    mocker.patch.object(
        proc.stdout, "readline", side_effect=RuntimeError("stream failed")
    )
    mocker.patch.object(proc, "wait", side_effect=asyncio.CancelledError)
    close = mocker.spy(spawn_mod._SessionLog, "close")

    with pytest.raises(asyncio.CancelledError):
        await spawn_mod.spawn_claude([Trigger("check")], model_id="opus")

    close.assert_called_once()
    assert proc.killed
    assert not patch_environment["plugin"].exists()


@pytest.mark.asyncio
async def test_regression_command_setup_failure_removes_plugin(
    mocker, patch_environment
):
    spawn_mod.CLAUDE_MD.unlink()
    start = mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec")

    with pytest.raises(FileNotFoundError, match="CLAUDE.md not found"):
        await spawn_mod.spawn_claude([Trigger("check")], model_id="opus")

    start.assert_not_called()
    assert not patch_environment["plugin"].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.TimeoutError, ValueError("invalid startup")])
async def test_start_failure_closes_log_and_plugin(mocker, patch_environment, error):
    from physiclaw.common import paths

    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", side_effect=error)
    mocker.patch.object(spawn_mod, "MAX_ATTEMPTS", 1)
    close = mocker.spy(spawn_mod._SessionLog, "close")

    outcome = await spawn_mod.spawn_claude([Trigger("check")], model_id="opus")

    assert outcome.status is None
    close.assert_called_once()
    assert not patch_environment["plugin"].exists()
    summary = json.loads(
        next(paths.claude_sessions_dir().glob("*/summary.json")).read_text()
    )
    assert summary["outcome"]["crashed"] is True


@pytest.mark.asyncio
async def test_already_removed_plugin_does_not_fail_a_done_session(
    mocker, patch_environment
):
    import shutil

    async def start(*args, **kwargs):
        shutil.rmtree(patch_environment["plugin"])
        return _FakeProc(lines=_result_line())

    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", side_effect=start)

    outcome = await spawn_mod.spawn_claude([Trigger("check")], model_id="opus")

    assert outcome.status == "DONE"


@pytest.mark.asyncio
async def test_stream_error_after_child_exit_reaps_without_killing(
    mocker, patch_environment
):
    proc = _FakeProc(lines=[])
    proc.returncode = 0
    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", return_value=proc)
    mocker.patch.object(
        proc.stdout, "readline", side_effect=ValueError("line too long")
    )
    mocker.patch.object(spawn_mod, "MAX_ATTEMPTS", 1)
    wait = mocker.spy(proc, "wait")

    outcome = await spawn_mod.spawn_claude([Trigger("check")], model_id="opus")

    assert outcome.status is None
    assert not proc.killed
    wait.assert_called_once()


@pytest.mark.asyncio
async def test_invalid_utf8_preserves_later_done(mocker, patch_environment):
    proc = _FakeProc(
        lines=[b"invalid utf8: \xff\n", *_result_line(">> DONE - recovered")]
    )
    mocker.patch.object(spawn_mod.asyncio, "create_subprocess_exec", return_value=proc)
    raw = mocker.spy(spawn_mod._SessionLog, "raw")

    outcome = await spawn_mod.spawn_claude([Trigger("check")], model_id="opus")

    assert outcome.status == "DONE"
    raw.assert_called_once()


@pytest.mark.asyncio
async def test_spawn_passes_cli_permissions_environment_and_buffer(
    mocker, patch_environment
):
    import hashlib
    import math

    from physiclaw.common import paths

    proc = _FakeProc(lines=_result_line())
    start = mocker.patch.object(
        spawn_mod.asyncio, "create_subprocess_exec", return_value=proc
    )
    prepare = mocker.spy(spawn_mod, "prepare_plugin_dir")
    wait_for = mocker.spy(spawn_mod.asyncio, "wait_for")

    await spawn_mod.spawn_claude([Trigger("check")], model_id="opus")

    argv = start.call_args.args
    flags = {
        flag: argv[argv.index(flag) + 1]
        for flag in [
            "--model",
            "--append-system-prompt",
            "--plugin-dir",
            "--setting-sources",
            "--permission-mode",
            "--output-format",
            "--mcp-config",
        ]
    }
    assert argv[:2] == ("claude", "-p")
    assert flags == {
        "--model": "opus",
        "--append-system-prompt": "SYSTEM",
        "--plugin-dir": str(patch_environment["plugin"]),
        "--setting-sources": "user",
        "--permission-mode": "acceptEdits",
        "--output-format": "stream-json",
        "--mcp-config": "{}",
    }
    assert {"--verbose", "--no-session-persistence", "--strict-mcp-config"} <= set(argv)
    assert set(argv[argv.index("--allowedTools") + 1].split(",")) == {
        "Read(memory/**)",
        "Glob(memory/**)",
        "Grep(memory/**)",
        "Write(memory/**)",
        "Edit(memory/**)",
        "Bash(uv run:*)",
        "Skill",
    }
    assert set(argv[argv.index("--disallowedTools") + 1].split(",")) == {
        "Write(jobs/**)",
        "Edit(jobs/**)",
    }
    assert start.call_args.kwargs == {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
        "cwd": str(spawn_mod.PROJECT_ROOT),
        "env": {},
        "limit": spawn_mod.CONFIG.claude.stream_buffer_mb * 1024**2,
    }
    assert prepare.call_args.kwargs == {"skills": {}}
    exit_timeout = wait_for.call_args.kwargs["timeout"]
    assert math.isfinite(exit_timeout) and exit_timeout > 0
    summary_path = next(paths.claude_sessions_dir().glob("*/summary.json"))
    summary = json.loads(summary_path.read_text())
    assert summary["model_ref"] == "claude-code/opus"
    assert summary["prompt_hash"] == hashlib.sha256(b"SYSTEM").hexdigest()
