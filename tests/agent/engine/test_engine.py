"""Tests for `physiclaw.agent.engine.engine` — the session lifecycle:
`_run_session` (wiring, teardown, crash handling) and `run` (the
outcome-contract driver: STUCK retries, WAIT follow-up, setup restart,
budget). The turn driver's tests live in `test_loop.py`; tool-call
execution in `test_dispatch.py`."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from engine_fakes import (
    FakeMcpClient,
    FakeProvider,
    _asst,
    _registry,
    _schemas,
    _tc,
    patch_loop_tails,
)

from physiclaw.agent.engine import (
    builtin_tool as builtin_tool_mod,
)
from physiclaw.agent.engine import (
    compact as compact_mod,
)
from physiclaw.agent.engine import engine as engine_mod
from physiclaw.agent.engine import (
    jobs as jobs_mod,
)
from physiclaw.agent.engine import (
    memory as memory_mod,
)
from physiclaw.agent.engine import pitfalls
from physiclaw.agent.engine import (
    prompt as prompt_mod,
)
from physiclaw.agent.engine import (
    skill as skill_mod,
)
from physiclaw.agent.engine.session import Session
from physiclaw.agent.runtime.hook import Trigger
from physiclaw.agent.runtime.sentinel import DONE, IDLE, STUCK, WAIT
from physiclaw.contract.dto import UserMessage

# ---------- _run_session ----------


def _async_returning(value):
    async def _coro(*a, **kw):
        return value

    return _coro


def _patch_session_deps(mocker):
    """Stub everything _run_session pulls beyond the loop."""
    mocker.patch(
        "physiclaw.common.config.parse_model_ref", return_value=("fake", "fake-model")
    )
    mocker.patch.object(
        engine_mod, "get_mcp", side_effect=_async_returning(FakeMcpClient())
    )
    mocker.patch.object(
        engine_mod, "list_tools_cached", side_effect=_async_returning([])
    )
    mocker.patch.object(skill_mod, "discover_builtin_skills", return_value={})
    mocker.patch.object(skill_mod, "discover_user_skills", return_value={})
    mocker.patch.object(
        builtin_tool_mod,
        "build_registry",
        return_value=_registry(),
    )
    mocker.patch.object(
        builtin_tool_mod,
        "schemas",
        return_value=_schemas(_registry()),
    )
    mocker.patch.object(memory_mod, "load_persistent", return_value="")
    mocker.patch.object(skill_mod, "render_builtin", return_value="")
    mocker.patch.object(skill_mod, "render_section", return_value="")
    mocker.patch.object(prompt_mod, "render_system_prompts", return_value="SYSTEM")
    mocker.patch.object(prompt_mod, "prefix_hash", return_value="hashX")
    mocker.patch.object(
        compact_mod,
        "new_summary_placeholder",
        return_value=UserMessage(content="<sum>"),
    )
    mocker.patch.object(
        compact_mod, "new_memory_placeholder", return_value=UserMessage(content="<mem>")
    )
    mocker.patch.object(
        compact_mod, "new_skills_placeholder", return_value=UserMessage(content="<skl>")
    )
    patch_loop_tails(mocker)
    mocker.patch.object(jobs_mod, "format_fired", return_value="")

    fake_tr = MagicMock()
    fake_rlog = MagicMock()
    mocker.patch.object(engine_mod, "Trace", return_value=fake_tr)
    mocker.patch.object(engine_mod, "RawLog", return_value=fake_rlog)
    return {"tr": fake_tr, "rlog": fake_rlog}


@pytest.fixture
def completed_run(mocker):
    deps = _patch_session_deps(mocker)
    provider = FakeProvider(
        [_asst(tool_calls=[_tc("note"), _tc("end_session", {"status": DONE})])]
    )
    plugin = SimpleNamespace(
        session_setup=mocker.AsyncMock(return_value=None),
        advance=mocker.AsyncMock(return_value=None),
        aclose=mocker.AsyncMock(),
    )
    mocker.patch.object(
        engine_mod.plugin_loader, "load_plugins", return_value=(plugin,)
    )
    factory = mocker.patch.object(engine_mod, "make_provider", return_value=provider)
    return SimpleNamespace(**deps, provider=provider, plugin=plugin, factory=factory)


async def test_regression_failed_plugin_setup_is_skipped_but_closed(completed_run):
    completed_run.plugin.session_setup.side_effect = RuntimeError("setup failed")

    outcome = await engine_mod.run([Trigger(description="t")], model_ref="fake/model")

    assert outcome.status == DONE
    completed_run.plugin.advance.assert_not_awaited()
    completed_run.plugin.aclose.assert_awaited_once()


async def test_run_plugin_without_setup_contribution_still_advances(completed_run):
    plugin = completed_run.plugin

    outcome = await engine_mod.run([Trigger(description="t")], model_ref="fake/model")

    assert outcome.status == DONE
    plugin.advance.assert_awaited_once()
    plugin.aclose.assert_awaited_once()


@pytest.mark.parametrize("resource", ["provider", "plugin"])
async def test_regression_cancelled_close_finishes_remaining_cleanup(
    completed_run, mocker, resource
):
    async def cancel_close():
        asyncio.current_task().cancel("stop")
        await asyncio.sleep(0)

    close = mocker.patch.object(
        getattr(completed_run, resource),
        "aclose",
        side_effect=cancel_close,
    )

    with pytest.raises(asyncio.CancelledError, match="stop"):
        await asyncio.create_task(
            engine_mod.run([Trigger(description="t")], model_ref="fake/model")
        )

    close.assert_awaited_once()
    completed_run.plugin.aclose.assert_awaited_once()
    completed_run.tr.close.assert_called_once()
    completed_run.rlog.close.assert_called_once()


@pytest.mark.parametrize(
    ("resource", "method"),
    [("provider", "aclose"), ("plugin", "aclose"), ("tr", "close"), ("rlog", "close")],
)
async def test_regression_close_error_preserves_done_and_closes_other_resources(
    completed_run, mocker, resource, method
):
    target = getattr(completed_run, resource)
    mocker.patch.object(target, method, side_effect=OSError("close failed"))

    outcome = await engine_mod.run([Trigger(description="t")], model_ref="fake/model")

    assert outcome.status == DONE
    completed_run.factory.assert_called_once()
    completed_run.plugin.aclose.assert_awaited_once()
    completed_run.tr.close.assert_called_once()
    completed_run.rlog.close.assert_called_once()


@pytest.mark.parametrize(("sink", "trace_closes"), [("Trace", 0), ("RawLog", 1)])
async def test_run_log_construction_failure_closes_only_opened_resources(
    completed_run, mocker, sink, trace_closes
):
    mocker.patch.object(engine_mod, sink, side_effect=OSError("cannot open log"))
    mocker.patch.object(engine_mod.CONFIG.engine, "max_attempts", 1)

    outcome = await engine_mod.run([Trigger(description="t")], model_ref="fake/model")

    assert outcome.status == STUCK
    assert "cannot open log" in outcome.recap
    assert completed_run.tr.close.call_count == trace_closes
    completed_run.rlog.close.assert_not_called()
    completed_run.factory.assert_not_called()


async def test_regression_curate_read_error_does_not_retry_completed_task(
    completed_run, mocker
):
    registry = _registry()
    path = pitfalls.paths.pitfalls_dir() / "pitfalls.md"

    async def note(session, args):
        session.added_pitfalls = True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff")
        return "noted"

    registry["note"] = replace(registry["note"], handler=note)
    mocker.patch.object(builtin_tool_mod, "build_registry", return_value=registry)
    mocker.patch.object(engine_mod.CONFIG.pitfalls, "curate_enabled", True)

    outcome = await engine_mod.run([Trigger(description="t")], model_ref="fake/model")

    assert outcome.status == DONE
    completed_run.factory.assert_called_once()
    assert len(completed_run.provider.calls) == 1
    assert path.read_bytes() == b"\xff"


@pytest.mark.asyncio
async def test_run_session_closes_provider_in_finally(mocker) -> None:
    deps = _patch_session_deps(mocker)
    asst = _asst(
        tool_calls=[
            _tc("note", {"summary": "x"}),
            _tc("end_session", {"status": DONE, "recap": "fin"}),
        ]
    )
    fake_provider = FakeProvider([asst])
    mocker.patch.object(engine_mod, "make_provider", return_value=fake_provider)

    session = Session()
    await engine_mod._run_session(
        [Trigger(description="t")],
        model_ref="fake/fake-model",
        session=session,
    )

    assert session.sentinel_status == DONE
    assert fake_provider.closed is True
    deps["tr"].close.assert_called_once()
    deps["rlog"].close.assert_called_once()


@pytest.mark.asyncio
async def test_run_session_crash_marks_stuck(mocker) -> None:
    _patch_session_deps(mocker)
    mocker.patch.object(
        engine_mod,
        "make_provider",
        side_effect=RuntimeError("bad provider"),
    )

    session = Session()
    await engine_mod._run_session(
        [Trigger(description="t")],
        model_ref="fake/fake-model",
        session=session,
    )

    assert session.sentinel_status == STUCK
    assert "session crashed" in session.sentinel_recap


@pytest.mark.asyncio
async def test_run_session_cancellation_propagates(mocker) -> None:
    _patch_session_deps(mocker)
    mocker.patch.object(
        engine_mod,
        "make_provider",
        side_effect=asyncio.CancelledError,
    )

    with pytest.raises(asyncio.CancelledError):
        await engine_mod._run_session(
            [Trigger(description="t")],
            model_ref="fake/fake-model",
            session=Session(),
        )


# ---------- run (outcome-contract wiring: retries, WAIT, budget) ----------


@pytest.mark.asyncio
async def test_run_wait_without_create_job_auto_schedules(mocker) -> None:
    # The follow-up now lives in the outcome contract; run() must feed it
    # the session's created-job flag so a jobless WAIT gets the singleton.
    upsert = mocker.patch("physiclaw.agent.engine.jobs.upsert_auto_wait_check")

    async def fake_session(triggers, *, model_ref, session: Session, settings=None):
        session.sentinel_status = WAIT
        session.sentinel_recap = "waiting"

    mocker.patch.object(engine_mod, "_run_session", side_effect=fake_session)

    await engine_mod.run([Trigger(description="t")], model_ref="x/y")

    upsert.assert_called_once()


@pytest.mark.asyncio
async def test_run_wait_with_create_job_skips_auto_schedule(mocker) -> None:
    upsert = mocker.patch("physiclaw.agent.engine.jobs.upsert_auto_wait_check")

    async def fake_session(triggers, *, model_ref, session: Session, settings=None):
        session.sentinel_status = WAIT
        session.sentinel_recap = "scheduled"
        session.sentinel_turn_created_job = True

    mocker.patch.object(engine_mod, "_run_session", side_effect=fake_session)

    await engine_mod.run([Trigger(description="t")], model_ref="x/y")

    upsert.assert_not_called()


@pytest.mark.asyncio
async def test_run_retries_on_stuck(mocker) -> None:
    mocker.patch.object(engine_mod.CONFIG.engine, "max_attempts", 3)
    statuses = iter([STUCK, STUCK, DONE])

    async def fake_session(triggers, *, model_ref, session: Session, settings=None):
        session.sentinel_status = next(statuses)
        session.sentinel_recap = "x"

    spy = mocker.patch.object(
        engine_mod,
        "_run_session",
        side_effect=fake_session,
    )

    await engine_mod.run([Trigger(description="t")], model_ref="x/y")

    assert spy.call_count == 3


@pytest.mark.asyncio
async def test_run_stops_after_done(mocker) -> None:
    mocker.patch.object(engine_mod.CONFIG.engine, "max_attempts", 5)

    async def fake_session(triggers, *, model_ref, session: Session, settings=None):
        session.sentinel_status = DONE

    spy = mocker.patch.object(
        engine_mod,
        "_run_session",
        side_effect=fake_session,
    )

    await engine_mod.run([Trigger(description="t")], model_ref="x/y")

    assert spy.call_count == 1


@pytest.mark.asyncio
async def test_run_gives_up_after_max_stucks(mocker) -> None:
    mocker.patch.object(engine_mod.CONFIG.engine, "max_attempts", 2)

    async def fake_session(triggers, *, model_ref, session: Session, settings=None):
        session.sentinel_status = STUCK
        session.sentinel_recap = "always stuck"

    spy = mocker.patch.object(
        engine_mod,
        "_run_session",
        side_effect=fake_session,
    )

    await engine_mod.run([Trigger(description="t")], model_ref="x/y")

    assert spy.call_count == 2


@pytest.mark.asyncio
async def test_run_restarts_once_after_setup_completes(mocker) -> None:
    # Session 1 finishes first-run setup → restart; session 2 does the task.
    outcomes = iter([("setup", True), (DONE, False)])

    async def fake_session(triggers, *, model_ref, session: Session, settings=None):
        status, restart = next(outcomes)
        session.sentinel_status = None if restart else status
        session.restart_for_setup = restart

    spy = mocker.patch.object(engine_mod, "_run_session", side_effect=fake_session)

    await engine_mod.run([Trigger(description="t")], model_ref="x/y")

    assert spy.call_count == 2  # setup session + task session


@pytest.mark.asyncio
async def test_run_no_restart_when_only_first_run_trigger(mocker) -> None:
    # A synthetic first-run wake has no request to resume: after setup
    # completes the layout is saved and loads on the next wake, so the loop
    # must NOT restart (which would replay the stale "learn the layout"
    # trigger and re-enter first-run setup).
    async def fake_session(triggers, *, model_ref, session: Session, settings=None):
        session.sentinel_status = IDLE
        session.restart_for_setup = True

    spy = mocker.patch.object(engine_mod, "_run_session", side_effect=fake_session)

    await engine_mod.run(
        [Trigger(description="learn the layout", source="first-run")],
        model_ref="x/y",
    )

    assert spy.call_count == 1  # no restart — nothing real to resume


@pytest.mark.asyncio
async def test_run_restarts_when_real_trigger_accompanies_first_run(mocker) -> None:
    # first-run + a real request in the same wake → restart so the real
    # request is handled with the layout loaded.
    outcomes = iter([(None, True), (DONE, False)])

    async def fake_session(triggers, *, model_ref, session: Session, settings=None):
        status, restart = next(outcomes)
        session.sentinel_status = status
        session.restart_for_setup = restart

    spy = mocker.patch.object(engine_mod, "_run_session", side_effect=fake_session)

    await engine_mod.run(
        [
            Trigger(description="phone IM arrived", source="phone"),
            Trigger(description="learn the layout", source="first-run"),
        ],
        model_ref="x/y",
    )

    assert spy.call_count == 2  # restarted to handle the phone request


@pytest.mark.asyncio
async def test_run_setup_restart_does_not_consume_stuck_attempt(mocker) -> None:
    # Even with max_attempts=1, the setup restart still allows the task session.
    mocker.patch.object(engine_mod.CONFIG.engine, "max_attempts", 1)
    outcomes = iter([("setup", True), (DONE, False)])

    async def fake_session(triggers, *, model_ref, session: Session, settings=None):
        status, restart = next(outcomes)
        session.sentinel_status = None if restart else status
        session.restart_for_setup = restart

    spy = mocker.patch.object(engine_mod, "_run_session", side_effect=fake_session)

    await engine_mod.run([Trigger(description="t")], model_ref="x/y")

    assert spy.call_count == 2


@pytest.mark.asyncio
async def test_run_setup_restart_fires_at_most_once(mocker) -> None:
    # A pathological session that keeps flagging restart must not loop forever.
    mocker.patch.object(engine_mod.CONFIG.engine, "max_attempts", 3)

    async def fake_session(triggers, *, model_ref, session: Session, settings=None):
        session.sentinel_status = DONE
        session.restart_for_setup = True  # always flags

    spy = mocker.patch.object(engine_mod, "_run_session", side_effect=fake_session)

    await engine_mod.run([Trigger(description="t")], model_ref="x/y")

    # 1 setup restart (uncounted) + then the guard blocks further restarts and
    # the DONE session ends the loop → 2 total.
    assert spy.call_count == 2


@pytest.mark.asyncio
async def test_run_budget_exhausted_stuck_not_retried(mocker) -> None:
    # Budget exhaustion is STUCK but retryable=False — a slow environment
    # would burn every retry the same way, so one attempt only.
    mocker.patch.object(engine_mod.CONFIG.engine, "max_attempts", 3)

    async def fake_session(triggers, *, model_ref, session: Session, settings=None):
        session.sentinel_status = STUCK
        session.sentinel_recap = "wall-clock budget (10s) exhausted"
        session.budget_exhausted = True

    spy = mocker.patch.object(engine_mod, "_run_session", side_effect=fake_session)

    await engine_mod.run([Trigger(description="t")], model_ref="x/y")

    assert spy.call_count == 1


@pytest.mark.asyncio
async def test_done_is_recorded_before_the_curation_pass(mocker) -> None:
    # A Ctrl-C during the optional post-session curation call must not
    # cost the summary its outcome: the `done` event is written first.
    deps = _patch_session_deps(mocker)
    asst = _asst(
        tool_calls=[
            _tc("note", {"summary": "x"}),
            _tc("end_session", {"status": DONE, "recap": "fin"}),
        ]
    )
    mocker.patch.object(engine_mod, "make_provider", return_value=FakeProvider([asst]))
    mocker.patch.object(engine_mod.CONFIG.pitfalls, "curate_enabled", True)
    curate = mocker.patch.object(
        engine_mod.curate, "curate", side_effect=asyncio.CancelledError()
    )
    session = Session()
    session.added_pitfalls = True

    with pytest.raises(asyncio.CancelledError):
        await engine_mod._run_session(
            [Trigger(description="t")], model_ref="fake/fake-model", session=session
        )

    curate.assert_awaited_once()
    done = [
        c.args[0]
        for c in deps["tr"].write.call_args_list
        if c.args[0].get("event") == "done"
    ]
    assert done == [{"event": "done", "sentinel": DONE, "recap": "fin"}]
