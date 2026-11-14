"""Public update hooks with real marker files and fake process/HTTP boundaries.

Mutation review accepts log-only edits and three equivalent variants: an
unrecognized default CI value remains false, the version-cache write already
creates HOME before marker creation, and missing-marker unlink errors are
already swallowed. All other variants must fail through public behavior.
"""

from __future__ import annotations

import importlib
import io
import json
import logging
import subprocess
from types import SimpleNamespace

import pytest
from freezegun import freeze_time
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from physiclaw.common import config, paths

up = importlib.import_module("physiclaw.cli.update")


@pytest.fixture(autouse=True)
def update_env(monkeypatch):
    monkeypatch.delenv("PHYSICLAW_DISABLE_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(config, "CONFIG", config.Config())
    monkeypatch.setattr(up, "_pkg_version", "1.0.0")


@pytest.fixture
def boundary(mocker):
    state = SimpleNamespace(
        stdout="physiclaw v1.0.0\n- physiclaw\n",
        query_code=0,
        query_error=None,
        warm_code=0,
        warm_error=None,
        response={"info": {"version": "1.1.0"}},
        network_error=None,
        calls=[],
    )

    def run(command, **kwargs):
        state.calls.append((command, kwargs))
        if command == ["/fake/uv", "tool", "list"]:
            if state.query_error is not None:
                raise state.query_error
            return subprocess.CompletedProcess(
                command, state.query_code, state.stdout, ""
            )
        if command[:3] == ["/fake/uv", "tool", "run"]:
            if state.warm_error is not None:
                raise state.warm_error
            return subprocess.CompletedProcess(command, state.warm_code, "", "")
        raise AssertionError(f"unexpected subprocess: {command}")

    def fetch(*args, **kwargs):
        if state.network_error is not None:
            raise state.network_error
        return io.BytesIO(json.dumps(state.response).encode())

    state.which = mocker.patch(
        "physiclaw.cli.update.shutil.which", return_value="/fake/uv"
    )
    state.process = mocker.patch("physiclaw.cli.update.subprocess.run", side_effect=run)
    state.http = mocker.patch(
        "physiclaw.cli._update_check.urllib.request.urlopen", side_effect=fetch
    )
    return state


def _marker(version="1.1.0"):
    path = paths.cache_dir() / "update.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": version}), encoding="utf-8")
    return path


def _version_cache():
    return json.loads(
        (paths.HOME / "run" / "version-check.json").read_text(encoding="utf-8")
    )


def _deny_marker_stat(marker):
    original_stat = type(marker).stat

    def stat(path, *args, **kwargs):
        if path == marker:
            raise PermissionError("marker inaccessible")
        return original_stat(path, *args, **kwargs)

    return stat


@pytest.fixture
def threads(mocker):
    instances = []

    class Thread:
        def __init__(self, *, target, name, daemon):
            self.target, self.name, self.daemon = target, name, daemon
            self.started = False
            instances.append(self)

        def start(self):
            self.started = True
            self.target()

    mocker.patch("physiclaw.cli.update.threading.Thread", Thread)
    return instances


def test_update_command_only_prints_manual_upgrade_instructions(boundary, capsys):
    up.update()

    assert (
        capsys.readouterr().out
        == "To update, stop the server and run:\n\n    uv tool upgrade physiclaw\n"
    )
    boundary.process.assert_not_called()
    boundary.http.assert_not_called()


@pytest.mark.parametrize(
    "hook",
    [up.notify_staged_update, up.maybe_stage_update, up.start_stage_update_thread],
)
@pytest.mark.parametrize(
    "name,value",
    [
        ("CI", "1"),
        ("CI", " TRUE "),
        ("CI", "yes"),
        ("PHYSICLAW_DISABLE_UPDATE_CHECK", "1"),
        ("PHYSICLAW_DISABLE_UPDATE_CHECK", " TRUE "),
        ("PHYSICLAW_DISABLE_UPDATE_CHECK", "yes"),
    ],
)
def test_disabled_hooks_do_not_start_work(
    boundary, threads, monkeypatch, hook, name, value
):
    marker = _marker()
    monkeypatch.setenv(name, value)

    hook()

    assert threads == []
    boundary.which.assert_not_called()
    boundary.http.assert_not_called()
    assert marker.exists()


@pytest.mark.parametrize(
    "hook",
    [up.notify_staged_update, up.maybe_stage_update, up.start_stage_update_thread],
)
def test_regression_hooks_observe_config_updates_in_same_process(
    boundary, threads, hook
):
    marker = _marker()
    config.set_dotted("update.check", "false")

    hook()

    boundary.which.assert_not_called()
    boundary.http.assert_not_called()
    assert threads == []
    assert marker.exists()


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_false_environment_switch_does_not_disable_notification(
    boundary, monkeypatch, caplog, value
):
    _marker()
    monkeypatch.setenv("CI", value)
    monkeypatch.setenv("PHYSICLAW_DISABLE_UPDATE_CHECK", value)

    with caplog.at_level(logging.INFO, logger=up.__name__):
        up.notify_staged_update()

    assert "1.1.0 available" in caplog.text
    boundary.process.assert_called_once()


def test_notify_missing_marker_does_not_query_uv(boundary):
    up.notify_staged_update()

    boundary.which.assert_not_called()
    boundary.process.assert_not_called()
    boundary.http.assert_not_called()


@pytest.mark.parametrize(
    "content",
    [
        b"not JSON",
        b"\xff",
        b"null",
        b"[]",
        b"{}",
        b'{"version": 1}',
        b'{"version": ""}',
        b'{"version": false}',
    ],
)
def test_notify_ignores_malformed_marker(boundary, content):
    marker = _marker()
    marker.write_bytes(content)

    up.notify_staged_update()

    assert marker.read_bytes() == content
    boundary.which.assert_not_called()
    boundary.http.assert_not_called()


def test_regression_notify_unreadable_marker_cannot_abort_startup(boundary, mocker):
    marker = _marker()
    mocker.patch("pathlib.Path.stat", _deny_marker_stat(marker))
    mocker.patch(
        "physiclaw.cli.update.read_text",
        side_effect=PermissionError("marker inaccessible"),
    )

    up.notify_staged_update()

    boundary.which.assert_not_called()
    boundary.http.assert_not_called()


@pytest.mark.parametrize("version", ["0.9.0", "1.0.0", "bad-version"])
def test_notify_removes_stale_marker_before_any_uv_query(boundary, version):
    marker = _marker(version)

    up.notify_staged_update()

    assert not marker.exists()
    boundary.which.assert_not_called()


def test_notify_stale_marker_cleanup_failure_is_ignored(boundary, mocker):
    marker = _marker("0.9.0")
    mocker.patch("pathlib.Path.unlink", side_effect=PermissionError("read only"))

    up.notify_staged_update()

    assert marker.exists()
    boundary.which.assert_not_called()


@pytest.mark.parametrize("hook", [up.notify_staged_update, up.maybe_stage_update])
def test_missing_uv_keeps_marker_and_skips_network(boundary, hook):
    marker = _marker()
    boundary.which.return_value = None

    hook()

    assert marker.exists()
    boundary.process.assert_not_called()
    boundary.http.assert_not_called()


@pytest.mark.parametrize(
    "output",
    [
        "",
        "ruff v1.0\n- ruff\n",
        "physiclaw\n",
        "physiclaw 1.0.0\n",
        "physiclaw-other v1.0.0\n",
        "- physiclaw\n",
    ],
)
@pytest.mark.parametrize("hook", [up.notify_staged_update, up.maybe_stage_update])
def test_absent_tool_install_skips_notice_and_download(boundary, caplog, output, hook):
    marker = _marker()
    boundary.stdout = output

    with caplog.at_level(logging.INFO, logger=up.__name__):
        hook()

    assert caplog.records == []
    assert marker.exists()
    boundary.process.assert_called_once()
    boundary.http.assert_not_called()


@pytest.mark.parametrize("hook", [up.notify_staged_update, up.maybe_stage_update])
@pytest.mark.parametrize(
    "error", [OSError("uv missing"), subprocess.TimeoutExpired("uv", 30)]
)
def test_uv_query_failure_cannot_abort_startup(boundary, hook, error):
    marker = _marker()
    boundary.query_error = error

    hook()

    assert marker.exists()
    boundary.http.assert_not_called()


def test_failed_uv_list_does_not_trust_stdout(boundary):
    boundary.query_code = 1

    up.maybe_stage_update()

    boundary.http.assert_not_called()
    assert len(boundary.calls) == 1


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    spaces=st.sampled_from([" ", "  ", "\t"]),
    version=st.tuples(st.integers(0, 99), st.integers(0, 99), st.integers(0, 99)),
)
def test_notify_accepts_uv_tool_lines_with_whitespace(
    boundary, caplog, spaces, version
):
    marker = _marker()
    boundary.stdout = f"ruff v1.0\n{spaces}physiclaw{spaces}v{'.'.join(map(str, version))}{spaces}\n- physiclaw\n"
    boundary.calls.clear()
    caplog.clear()

    with caplog.at_level(logging.INFO, logger=up.__name__):
        up.notify_staged_update()

    assert "physiclaw 1.1.0 available (you're on 1.0.0)" in caplog.text
    assert "stop the server and run `uv tool upgrade physiclaw`" in caplog.text
    assert marker.exists()
    boundary.which.assert_called_with("uv")
    assert boundary.calls == [
        (
            ["/fake/uv", "tool", "list"],
            {
                "capture_output": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 30,
            },
        )
    ]
    boundary.http.assert_not_called()


@pytest.mark.parametrize("error", [OSError("offline"), TimeoutError("timeout")])
def test_stage_network_failure_preserves_existing_marker(boundary, error):
    marker = _marker()
    boundary.network_error = error

    up.maybe_stage_update()

    assert json.loads(marker.read_text(encoding="utf-8"))["version"] == "1.1.0"
    assert len(boundary.calls) == 1


@pytest.mark.parametrize("response", [{}, {"info": {}}, {"info": {"version": None}}])
def test_stage_without_latest_version_does_not_warm(boundary, response):
    boundary.response = response

    up.maybe_stage_update()

    assert len(boundary.calls) == 1
    assert not (paths.cache_dir() / "update.json").exists()


def test_stage_current_release_clears_stale_marker_and_caches_version(boundary):
    marker = _marker("0.9.0")
    boundary.response = {"info": {"version": "1.0.0"}}

    up.maybe_stage_update()

    assert not marker.exists()
    assert _version_cache()["latest_version"] == "1.0.0"
    assert len(boundary.calls) == 1


def test_stage_same_release_skips_redundant_warm(boundary):
    marker = _marker()

    up.maybe_stage_update()

    assert json.loads(marker.read_text(encoding="utf-8"))["version"] == "1.1.0"
    assert _version_cache()["latest_version"] == "1.1.0"
    assert len(boundary.calls) == 1


@freeze_time("2026-10-15 01:00:00")
def test_stage_new_release_warms_separate_environment_then_records_it(boundary, caplog):
    marker = _marker("1.0.1")

    with caplog.at_level(logging.INFO, logger=up.__name__):
        up.maybe_stage_update()

    boundary.which.assert_called_once_with("uv")
    assert boundary.calls == [
        (
            ["/fake/uv", "tool", "list"],
            {
                "capture_output": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 30,
            },
        ),
        (
            [
                "/fake/uv",
                "tool",
                "run",
                "--refresh",
                "--from",
                "physiclaw==1.1.0",
                "python",
                "-c",
                "pass",
            ],
            {
                "capture_output": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 600,
            },
        ),
    ]
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "version": "1.1.0",
        "staged_at": "2026-10-15T01:00:00+00:00",
    }
    assert _version_cache()["latest_version"] == "1.1.0"
    assert "physiclaw 1.1.0 staged for the next startup's notice" in caplog.text


@pytest.mark.parametrize(
    "error", [OSError("gone"), subprocess.TimeoutExpired("uv", 600)]
)
def test_stage_warm_exception_preserves_previous_marker(boundary, caplog, error):
    marker = _marker("1.0.1")
    boundary.warm_error = error

    with caplog.at_level(logging.INFO, logger=up.__name__):
        up.maybe_stage_update()

    assert json.loads(marker.read_text(encoding="utf-8"))["version"] == "1.0.1"
    assert "will retry" in caplog.text
    assert "staged for" not in caplog.text


def test_stage_failed_warm_leaves_no_marker(boundary, caplog):
    boundary.warm_code = 1

    with caplog.at_level(logging.INFO, logger=up.__name__):
        up.maybe_stage_update()

    assert not (paths.cache_dir() / "update.json").exists()
    assert "will retry" in caplog.text
    assert "staged for" not in caplog.text


def test_regression_stage_does_not_claim_success_when_marker_cannot_be_written(
    boundary, caplog
):
    marker = paths.cache_dir() / "update.json"
    marker.mkdir(parents=True)

    with caplog.at_level(logging.INFO, logger=up.__name__):
        up.maybe_stage_update()

    assert marker.is_dir()
    assert len(boundary.calls) == 2
    assert "staged for" not in caplog.text


def test_background_stage_runs_as_named_daemon(boundary, threads):
    up.start_stage_update_thread()

    assert len(threads) == 1
    assert threads[0].started is True
    assert threads[0].daemon is True
    assert threads[0].name == "physiclaw-stage-update"
    assert (
        json.loads((paths.cache_dir() / "update.json").read_text(encoding="utf-8"))[
            "version"
        ]
        == "1.1.0"
    )
    assert len(boundary.calls) == 2


def test_regression_thread_start_failure_does_not_abort_server(
    boundary, mocker, caplog
):
    mocker.patch(
        "physiclaw.cli.update.threading.Thread.start",
        side_effect=RuntimeError("can't start new thread"),
    )

    import logging

    with caplog.at_level(logging.DEBUG, logger=up.__name__):
        up.start_stage_update_thread()

    (record,) = [
        r
        for r in caplog.records
        if "could not start background update check" in r.getMessage()
    ]
    assert record.levelno == logging.DEBUG and record.exc_info
    boundary.which.assert_not_called()
    boundary.http.assert_not_called()
