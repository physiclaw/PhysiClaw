"""Tests for `physiclaw.agent.runtime.launcher`.

Accepted mutation variants only change help, error, or log wording.
"""

from __future__ import annotations

import asyncio

import pytest

from physiclaw.agent.runtime import launcher
from physiclaw.agent.runtime.launcher import (
    engine_label,
    resolve,
)

# ---------- engine_label ----------


def test_engine_label_for_in_process_provider() -> None:
    assert engine_label("qwen/qwen3-plus") == (
        "engine=physiclaw, provider=qwen, model=qwen3-plus"
    )


# ---------- resolve ----------


def test_resolve_returns_ref_and_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHYSICLAW_MODEL", "qwen/qwen3-plus")

    ref, source = resolve()

    assert ref == "qwen/qwen3-plus"
    assert source == "PHYSICLAW_MODEL env"


def test_resolve_raises_for_unknown_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHYSICLAW_MODEL", "mystery/x")

    with pytest.raises(RuntimeError, match=r"^unknown provider 'mystery'") as error:
        resolve()

    assert "qwen" in str(error.value)


def test_resolve_raises_runtime_error_for_slashless_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # parse_model_ref's ValueError must normalize to RuntimeError — every
    # resolve() caller guards with `except RuntimeError` (server startup's
    # non-fatal fallback, doctor's diagnosis line).
    monkeypatch.setenv("PHYSICLAW_MODEL", "gpt-4o")

    with pytest.raises(RuntimeError, match="must be 'provider/model'"):
        resolve()


# ---------- launch() ----------


@pytest.mark.parametrize("interval", ["-1", "nan", "inf", "-inf"])
def test_launch_rejects_invalid_interval_before_startup(monkeypatch, capsys, interval):
    monkeypatch.setattr("sys.argv", ["runtime", f"--interval={interval}"])
    monkeypatch.delenv("PHYSICLAW_MODEL", raising=False)

    with pytest.raises(SystemExit) as error:
        launcher.launch()

    assert error.value.code == 2
    assert "interval must be finite and non-negative" in capsys.readouterr().err


@pytest.mark.parametrize("interval", [0.0, 0.125, 1.0])
def test_launch_runs_engine_path_for_in_process_provider(
    mocker,
    monkeypatch: pytest.MonkeyPatch,
    caplog,
    interval,
) -> None:
    caplog.set_level("INFO", logger="physiclaw.agent.runtime.launcher")
    monkeypatch.setenv("PHYSICLAW_MODEL", "qwen/qwen3-plus")
    monkeypatch.setattr(
        "sys.argv",
        ["runtime", "--server", "http://x:9000", "--interval", str(interval)],
    )
    mocker.patch.object(launcher, "setup_logging")

    fake_runtime = mocker.MagicMock()

    async def _start():
        return None

    fake_runtime.start.side_effect = _start

    runtime_cls = mocker.patch.object(
        launcher,
        "Runtime",
        return_value=fake_runtime,
    )

    async def _close():
        return None

    mocker.patch.object(launcher, "close_mcp", side_effect=_close)

    fake_engine_run = mocker.MagicMock()
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.agent.engine.engine": mocker.MagicMock(run=fake_engine_run)},
    )

    launcher.launch()

    runtime_cls.assert_called_once()
    kwargs = runtime_cls.call_args.kwargs
    assert kwargs["interval"] == interval
    assert kwargs["label"].startswith("engine=physiclaw")
    assert kwargs["react"].func is fake_engine_run
    assert kwargs["react"].keywords == {"model_ref": "qwen/qwen3-plus"}
    (selected,) = [
        r for r in caplog.records if getattr(r, "event", None) == "model.selected"
    ]
    assert selected.console is True
    assert selected.console_message == "Model: qwen/qwen3-plus"
    assert selected.model_ref == "qwen/qwen3-plus"


def test_launch_normalizes_proxy_env_first(
    mocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct `python -m physiclaw.agent.runtime` skips the CLI root
    callback, so launch() must normalize `socks://` proxy env itself —
    before anything builds an httpx client (regression: on Linux with a
    VPN on, the poll hook's client raised `Unknown scheme for proxy URL`
    every tick and the agent never woke). Guarded via the earliest exit
    path (no model): env must already be rewritten by then."""
    import os

    monkeypatch.setenv("all_proxy", "socks://127.0.0.1:7897")
    monkeypatch.delenv("PHYSICLAW_MODEL", raising=False)
    from physiclaw.common import config as _cfg

    monkeypatch.setattr(_cfg.CONFIG.agent, "model", "")
    monkeypatch.setattr("sys.argv", ["runtime"])

    with pytest.raises(SystemExit):
        launcher.launch()

    assert os.environ["all_proxy"] == "socks5://127.0.0.1:7897"


def _stub_asyncio_run(mocker, *, raise_kbd_interrupt: bool = False):
    """Stub `asyncio.run` so it accepts the runtime coroutine
    without awaiting it. Without `.close()`, Python flags the unawaited
    coro with a RuntimeWarning (often attributed to whichever test
    happens to be running when GC sweeps it)."""

    def _run(coro):
        coro.close()  # release the coroutine so it isn't garbage-collected unawaited
        if raise_kbd_interrupt:
            raise KeyboardInterrupt

    return mocker.patch.object(launcher.asyncio, "run", side_effect=_run)


def test_launch_exits_cleanly_when_no_model_configured(
    mocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """First-run UX: runtime subprocess shouldn't dump a Python stack
    trace into the parent shell when no model is set — print a friendly
    message to stderr and exit 1."""
    monkeypatch.delenv("PHYSICLAW_MODEL", raising=False)
    # Ensure the in-config default is also empty.
    from physiclaw.common import config as _cfg

    monkeypatch.setattr(_cfg.CONFIG.agent, "model", "")
    monkeypatch.setattr("sys.argv", ["runtime"])

    with pytest.raises(SystemExit) as exc_info:
        launcher.launch()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "no model configured" in captured.err
    assert "physiclaw models" in captured.err


def test_missing_credentials_stop_runtime_without_traceback_or_retries(
    mocker, monkeypatch, capsys, caplog
) -> None:
    from physiclaw.agent.engine import engine
    from physiclaw.agent.runtime import runtime
    from physiclaw.agent.runtime.hook import Trigger
    from physiclaw.common.config import CONFIG

    monkeypatch.setenv("PHYSICLAW_MODEL", "deepseek/deepseek-flash")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(CONFIG.provider, "deepseek_api_key", "")
    monkeypatch.setattr(CONFIG.engine, "max_attempts", 3)
    monkeypatch.setattr("sys.argv", ["runtime"])
    mocker.patch.object(launcher, "setup_logging")
    close = mocker.patch.object(launcher, "close_mcp")
    mcp = mocker.patch.object(engine, "get_mcp")
    make = mocker.spy(engine, "make_chat_provider")
    mocker.patch.object(runtime, "load_hooks")
    mocker.patch.object(runtime, "_check_ready", return_value=True)
    hooks = mocker.patch.object(
        runtime,
        "check_hooks",
        side_effect=[
            [Trigger(source="phone", description="screen changed")],
            asyncio.CancelledError(),
        ],
    )
    mocker.patch.object(runtime.asyncio, "sleep")

    with pytest.raises(SystemExit) as error:
        launcher.launch()

    assert error.value.code == 1
    message = capsys.readouterr().err
    assert len(message.splitlines()) == 1
    assert "ERROR [" in message
    assert "deepseek credential not found" in message
    assert "DEEPSEEK_API_KEY" in message
    assert "deepseek_api_key" in message
    make.assert_called_once()
    hooks.assert_awaited_once()
    mcp.assert_not_awaited()
    close.assert_awaited_once()
    assert not any(record.exc_info for record in caplog.records)
    assert "retrying" not in caplog.text
    assert "backing off" not in caplog.text


def test_launch_swallows_keyboard_interrupt(
    mocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHYSICLAW_MODEL", "qwen/qwen3-plus")
    monkeypatch.setattr("sys.argv", ["runtime"])
    mocker.patch.object(launcher, "setup_logging")
    _stub_asyncio_run(mocker, raise_kbd_interrupt=True)
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.agent.engine.engine": mocker.MagicMock(run=mocker.MagicMock())},
    )

    # Must not raise.
    launcher.launch()


def test_launch_seeds_physiclaw_server_env(
    mocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHYSICLAW_MODEL", "qwen/qwen3-plus")
    monkeypatch.delenv("PHYSICLAW_SERVER", raising=False)
    monkeypatch.setattr("sys.argv", ["runtime", "--server", "http://h:42"])
    mocker.patch.object(launcher, "setup_logging")
    _stub_asyncio_run(mocker)
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.agent.engine.engine": mocker.MagicMock(run=mocker.MagicMock())},
    )

    launcher.launch()

    import os

    assert os.environ.get("PHYSICLAW_SERVER") == "http://h:42"


def test_launch_verbose_flag_sets_debug_level(
    mocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging as _logging

    monkeypatch.setenv("PHYSICLAW_MODEL", "qwen/qwen3-plus")
    monkeypatch.setattr("sys.argv", ["runtime", "--verbose"])
    setup_spy = mocker.patch.object(launcher, "setup_logging")
    _stub_asyncio_run(mocker)
    mocker.patch.dict(
        "sys.modules",
        {"physiclaw.agent.engine.engine": mocker.MagicMock(run=mocker.MagicMock())},
    )

    launcher.launch()

    from physiclaw.common import paths

    setup_spy.assert_called_once_with(
        "runtime",
        _logging.DEBUG,
        file_dir=paths.LOG_DIR,
        file_level=_logging.DEBUG,
    )


@pytest.mark.asyncio
async def test_cleanup_keeps_original_failure_and_task(mocker):
    owner = asyncio.current_task()
    failure = RuntimeError("original runtime failure")
    runtime = mocker.Mock()
    runtime.start = mocker.AsyncMock(side_effect=failure)
    closed = []

    async def close_mcp():
        assert asyncio.current_task() is owner
        closed.append("mcp")
        raise OSError("cleanup failure")

    async def close_status():
        assert asyncio.current_task() is owner
        closed.append("status")

    mocker.patch.object(launcher, "close_mcp", side_effect=close_mcp)
    mocker.patch.object(launcher, "close_status_client", side_effect=close_status)
    with pytest.raises(RuntimeError) as error:
        await launcher._run_runtime(runtime)
    assert error.value is failure
    assert closed == ["mcp", "status"]
