"""Tests for `physiclaw debug` — the one-command debug runner: task
seeding + staged replies, the armed wake, the reset, and the
server-start path (no live server → become one, in debug mode). An
autouse stub fakes a live server so no test ever starts a real one.

Mutation review accepts display prose/decoration and a non-recursive mkdir:
seed/stage already create the wake directory before either arm-wake call.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from physiclaw.agent.hooks.debug import wake_path
from physiclaw.cli import app
from physiclaw.common import runtime_state
from physiclaw.common.config import DEBUG_ENV_VAR, MACRO_FAILURE_ENV_VAR
from physiclaw.debug import thread as vthread

runner = CliRunner()


@pytest.fixture(autouse=True)
def _live_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to "a server is already running" so `--task`
    never starts a real one; the server-start tests override this."""
    monkeypatch.setattr(runtime_state, "read_live", lambda: {"pid": 1})


def test_task_with_live_server_prints_the_debug_hint() -> None:
    result = runner.invoke(app, ["debug", "--task", "buy milk", "--no-wake"])

    assert result.exit_code == 0
    assert "physiclaw debug" in result.output  # the debug-mode hint


def test_task_with_no_live_server_starts_one_in_debug_mode(
    mocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_state, "read_live", lambda: None)
    mocker.patch.dict("os.environ")  # snapshot: the command exports env flags
    server_spy = mocker.patch("physiclaw.cli.server.server")

    result = runner.invoke(app, ["debug", "--task", "buy milk"])

    assert result.exit_code == 0
    server_spy.assert_called_once_with(hot_start=True)
    import os

    assert os.environ.get(DEBUG_ENV_VAR) == "1"
    assert os.environ.get(MACRO_FAILURE_ENV_VAR) == "1"  # the debug default


def test_no_macro_failure_leaves_the_halt_unarmed(
    mocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_state, "read_live", lambda: None)
    monkeypatch.delenv(MACRO_FAILURE_ENV_VAR, raising=False)
    mocker.patch.dict("os.environ")
    mocker.patch("physiclaw.cli.server.server")

    result = runner.invoke(app, ["debug", "--task", "buy milk", "--no-macro-failure"])

    assert result.exit_code == 0
    import os

    assert os.environ.get(DEBUG_ENV_VAR) == "1"
    assert os.environ.get(MACRO_FAILURE_ENV_VAR) is None


def test_no_flags_is_an_error() -> None:
    result = runner.invoke(app, ["debug"])

    assert result.exit_code != 0


def test_task_seeds_thread_stages_replies_and_arms_a_wake() -> None:
    result = runner.invoke(app, ["debug", "--task", "buy milk", "--reply", "ok"])

    assert result.exit_code == 0
    thread = vthread.load()
    assert thread.bubbles == [vthread.Bubble(sender=vthread.USER, text="buy milk")]
    assert thread.staged == ["ok"]
    assert json.loads(wake_path().read_text(encoding="utf-8"))["description"]


def test_task_no_wake_leaves_the_trigger_unarmed() -> None:
    result = runner.invoke(app, ["debug", "--task", "buy milk", "--no-wake"])

    assert result.exit_code == 0
    assert not wake_path().exists()


def test_task_resets_a_previous_script() -> None:
    runner.invoke(app, ["debug", "--task", "buy milk", "--reply", "ok"])

    runner.invoke(app, ["debug", "--task", "buy eggs"])

    thread = vthread.load()
    assert thread.bubbles == [vthread.Bubble(sender=vthread.USER, text="buy eggs")]
    assert thread.staged == []


def test_reply_alone_stages_without_arming_a_wake() -> None:
    runner.invoke(app, ["debug", "--task", "buy milk", "--no-wake"])

    result = runner.invoke(app, ["debug", "--reply", "ok"])

    assert result.exit_code == 0
    assert vthread.load().staged == ["ok"]
    assert not wake_path().exists()


def test_reply_wakes_a_suspended_walk() -> None:
    from physiclaw.common.logger import write_json_atomic
    from physiclaw.conductor.walk.suspension import SUSPENDED_SCHEMA, suspended_path

    suspended_path().parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        suspended_path(),
        {"schema": SUSPENDED_SCHEMA, "app": "demo", "playbook": "flow", "idx": 0},
    )

    result = runner.invoke(app, ["debug", "--reply", "ok"])

    assert result.exit_code == 0
    assert wake_path().exists()


def test_status_reports_the_script_state() -> None:
    runner.invoke(app, ["debug", "--task", "buy milk", "--reply", "ok"])

    result = runner.invoke(app, ["debug", "--status"])

    assert "buy milk" in result.output
    assert "ok" in result.output
    assert "wake armed: True" in result.output
    assert "suspended walk: False" in result.output


def test_clear_drops_thread_and_wake() -> None:
    runner.invoke(app, ["debug", "--task", "buy milk"])

    result = runner.invoke(app, ["debug", "--clear"])

    assert result.exit_code == 0
    assert vthread.load() == vthread.Thread() and not wake_path().exists()


@pytest.mark.parametrize("inherited", ["1", "0", "false", ""])
def test_regression_no_macro_failure_clears_inherited_halt(
    mocker, monkeypatch: pytest.MonkeyPatch, inherited: str
) -> None:
    monkeypatch.setattr(runtime_state, "read_live", lambda: None)
    monkeypatch.setenv(MACRO_FAILURE_ENV_VAR, inherited)
    mocker.patch.dict("os.environ")
    observed = []
    mocker.patch(
        "physiclaw.cli.server.server",
        side_effect=lambda **kwargs: observed.append(
            os.environ.get(MACRO_FAILURE_ENV_VAR)
        ),
    )

    result = runner.invoke(app, ["debug", "--task", "test", "--no-macro-failure"])

    assert result.exit_code == 0
    assert observed == [None]
    assert "halt on macro failure" not in result.stdout


def test_regression_clear_tolerates_wake_consumed_concurrently(mocker) -> None:
    vthread.seed("test", [])
    wake_path().write_text("{}", encoding="utf-8")
    unlink = Path.unlink
    target = wake_path()

    def consume_before_unlink(path, *args, **kwargs):
        unlink(path, *args, **kwargs)
        if path == target:
            raise FileNotFoundError("wake already consumed")

    mocker.patch.object(Path, "unlink", consume_before_unlink)

    result = runner.invoke(app, ["debug", "--clear"])

    assert result.exit_code == 0
    assert not vthread.thread_path().exists()
    assert not wake_path().exists()


@pytest.mark.parametrize("marker", ["thread.json", "wake.json"])
def test_regression_clear_removes_dangling_markers(tmp_path, marker) -> None:
    wake_path().parent.mkdir(parents=True)
    path = wake_path().with_name(marker)
    path.symlink_to(tmp_path / "missing")

    result = runner.invoke(app, ["debug", "--clear"])

    assert result.exit_code == 0
    assert marker in result.stdout
    assert not path.is_symlink()


def test_regression_clear_reports_deletion_error(mocker) -> None:
    vthread.seed("keep", [])
    mocker.patch.object(Path, "unlink", side_effect=PermissionError("denied"))

    result = runner.invoke(app, ["debug", "--clear"])

    assert result.exit_code == 1
    assert "denied" in result.stderr
    assert "cleared:" not in result.stdout
    assert vthread.thread_path().exists()


def test_clear_missing_markers_is_successful() -> None:
    result = runner.invoke(app, ["debug", "--clear"])

    assert result.exit_code == 0
    assert "nothing to clear" in result.stdout


def test_live_server_does_not_change_debug_flags_or_start_another(mocker, monkeypatch):
    monkeypatch.setenv(DEBUG_ENV_VAR, "existing")
    monkeypatch.setenv(MACRO_FAILURE_ENV_VAR, "existing")
    server = mocker.patch("physiclaw.cli.server.server")

    result = runner.invoke(app, ["debug", "--task", "test", "--no-macro-failure"])

    assert result.exit_code == 0
    assert os.environ[DEBUG_ENV_VAR] == "existing"
    assert os.environ[MACRO_FAILURE_ENV_VAR] == "existing"
    server.assert_not_called()


def test_reply_no_wake_preserves_suspended_walk_without_trigger() -> None:
    from physiclaw.common.logger import write_json_atomic
    from physiclaw.conductor.walk.suspension import suspended_path

    suspended_path().parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(suspended_path(), {"app": "demo", "playbook": "flow"})

    result = runner.invoke(app, ["debug", "--reply", "yes", "--no-wake"])

    assert result.exit_code == 0
    assert not wake_path().exists()
    assert vthread.load().staged == ["yes"]
    assert suspended_path().exists()


def test_status_reports_suspended_reference() -> None:
    from physiclaw.common.logger import write_json_atomic
    from physiclaw.conductor.walk.suspension import suspended_path

    suspended_path().parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(suspended_path(), {"app": "demo", "playbook": "flow"})

    result = runner.invoke(app, ["debug", "--status"])

    assert result.exit_code == 0
    assert "suspended walk: demo/flow" in result.stdout


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(task=st.text(max_size=60), replies=st.lists(st.text(max_size=40), max_size=4))
def test_seed_roundtrips_task_and_replies(task, replies):
    args = ["debug", "--task", task, "--no-wake"]
    args.extend(arg for reply in replies for arg in ("--reply", reply))

    result = runner.invoke(app, args)

    assert result.exit_code == 0
    assert vthread.load() == vthread.Thread(
        [vthread.Bubble(vthread.USER, task)], replies
    )
    assert not wake_path().exists()


def test_reply_appends_without_resetting_existing_thread():
    vthread.seed("initial task", ["first"])

    result = runner.invoke(app, ["debug", "--reply", "second", "--reply", "third"])

    assert result.exit_code == 0
    assert vthread.load() == vthread.Thread(
        [vthread.Bubble(vthread.USER, "initial task")], ["first", "second", "third"]
    )
