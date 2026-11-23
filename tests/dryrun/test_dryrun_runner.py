"""Check the dryrun worker's isolation and acceptance criteria."""

import asyncio
import datetime as dt
import json
import os
import sys
import tomllib
from collections import Counter
from types import SimpleNamespace

import httpx
import pytest
from freezegun import freeze_time
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from physiclaw.common import paths
from physiclaw.dryrun import HOST, PHONE_ACTIONS, DryRunError, runner
from physiclaw.dryrun import __main__ as worker
from physiclaw.dryrun.scenario import ModelStub, Phone


async def test_notification_follows_the_watch_baseline():
    phone = Phone()
    watch_state = {"reason": "ema initialized"}

    async def watch(_request):
        return JSONResponse(watch_state)

    app = Starlette(routes=[Route("/api/phone/watch", watch)])
    control = runner._ControlApp(app, Starlette(), phone)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://fixture"
    ) as client:
        assert (await client.get("/api/setup")).status_code == 404
        assert phone.screen == "idle"
        assert (await client.get("/api/phone/watch")).json() == watch_state
        assert phone.screen == "notification"
        assert not control.wake_seen
        watch_state = {"wake": True}
        assert (await client.get("/api/phone/watch")).json() == watch_state
        assert control.wake_seen


@pytest.mark.parametrize(
    ("summary_override", "screen", "expected"),
    [
        ({}, "home", None),
        (
            {"outcome": {"sentinel": "FAILED", "recap": "could not tap"}},
            "home",
            "Agent ended with FAILED",
        ),
        ({"triggers": [{"source": "manual"}]}, "home", "expected phone wake"),
        ({"provider_calls": 0}, "home", "complete the expected model requests"),
        ({"tool_calls": {"peek": 1}}, "home", "Unexpected phone tool calls"),
        ({}, "controls", "requested lamp settings"),
    ],
)
def test_session_must_prove_the_workflow(summary_override, screen, expected):
    summary = {
        "errors": {},
        "outcome": {"sentinel": "DONE", "crashed": False},
        "triggers": [{"source": "phone"}],
        "turns": len(PHONE_ACTIONS),
        "provider_calls": len(PHONE_ACTIONS),
        "tool_calls": {**Counter(PHONE_ACTIONS), "note": len(PHONE_ACTIONS)},
        **summary_override,
    }
    phone = Phone()
    phone.screen = screen
    phone.lamp_screen = "done"
    model = ModelStub()
    model.turns = len(PHONE_ACTIONS)
    if expected is None:
        runner._verify_session(summary, "phone", model, phone)
    else:
        with pytest.raises(DryRunError, match=expected):
            runner._verify_session(summary, "phone", model, phone)


async def test_startup_failure_stops_runtime_before_control_server(
    physiclaw_home, monkeypatch
):
    events = []
    runtime_stopped = asyncio.Event()
    listener = SimpleNamespace(getsockname=lambda: (HOST, 8471))

    async def serve(server, *, sockets):
        assert sockets == [listener]
        server.started = True
        await runtime_stopped.wait()
        events.append("control server stopped")

    async def stop():
        events.append("agent runtime stopped")
        runtime_stopped.set()

    def fail_resume(*args, **kwargs):
        events.append("rig initialization failed")
        return False

    monkeypatch.setattr(runner._Server, "serve", serve)
    monkeypatch.setattr(runner, "try_resume", fail_resume)
    monkeypatch.setattr(runner, "_spawn_runtime", lambda *a: None)
    monkeypatch.setattr(
        runner, "ManagedRuntime", lambda *a, **kw: SimpleNamespace(stop=stop)
    )
    monkeypatch.setattr(runner, "setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "attach_server_mcp_tee", lambda: None)

    with pytest.raises(DryRunError, match="virtual rig did not become ready"):
        await runner.run(listener, verbose=False)

    assert events == [
        "rig initialization failed",
        "agent runtime stopped",
        "control server stopped",
    ]


@pytest.mark.parametrize("failure", [None, "virtual touch missed"])
def test_worker_sets_local_config_before_running(
    tmp_path, monkeypatch, mocker, failure
):
    monkeypatch.setenv("PHYSICLAW_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["dryrun", str(tmp_path)])
    socket = mocker.MagicMock()
    monkeypatch.setattr(worker, "socket", SimpleNamespace(socket=socket))
    listener = socket.return_value.__enter__.return_value
    listener.getsockname.return_value = (HOST, 8471)

    async def run(bound, *, verbose):
        assert bound is listener
        assert not verbose
        listener.bind.assert_called_once_with((HOST, 0))
        with (tmp_path / "config.toml").open("rb") as config:
            settings = tomllib.load(config)
        assert settings["providers"]["openai"]["base_url"] == f"http://{HOST}:8471/v1"
        assert settings["engine"]["max_turns"] == len(PHONE_ACTIONS)
        assert os.environ["HTTPS_PROXY"] == f"http://{HOST}:8471"
        assert os.environ["NO_PROXY"] == HOST
        if failure:
            raise DryRunError(failure)

    monkeypatch.setattr(runner, "run", run)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.setenv(name, "http://example.invalid")

    assert worker.main() == (1 if failure else 0)
    failure_path = tmp_path / "failure.json"
    if failure:
        assert json.loads(failure_path.read_text(encoding="utf-8")) == failure
    else:
        assert not failure_path.exists()


@pytest.mark.parametrize("time", ["2026-05-08 11:59", "2026-05-08 12:00"])
async def test_daily_check_requires_a_real_firing_and_next_noon(time, monkeypatch):
    from physiclaw.agent.engine import job_store, jobs
    from physiclaw.agent.hooks import cron
    from physiclaw.common import daylog
    from physiclaw.dryrun.scenario import (
        DAILY_RESULT,
        JOB_DESCRIPTION,
        JOB_ID,
        JOB_SOURCE,
    )

    for module in (jobs, job_store, cron):
        monkeypatch.setattr(module, "JOBS_PATH", paths.jobs_file())
    with freeze_time(time):
        started = dt.datetime.now()
        jobs.create_job(
            id=JOB_ID,
            kind="periodic",
            schedule="0 12 * * *",
            description=JOB_DESCRIPTION,
            context="Check the lamp for Sam.",
        )
        initial = jobs.get_job(JOB_ID).next_fire_time
        job_store.update_fields(
            paths.jobs_file(), {JOB_ID: {"Next fire time": "2027-05-09T12:00"}}
        )
        with pytest.raises(DryRunError, match="first occurrence"):
            runner._make_daily_job_due(started)
        job_store.update_fields(
            paths.jobs_file(), {JOB_ID: {"Next fire time": initial}}
        )
        runner._make_daily_job_due(started)
        trigger = await cron.cron()
        assert trigger is not None and trigger.source == JOB_SOURCE
        with pytest.raises(DryRunError, match="finish and re-arm"):
            runner._check_daily_result()
        jobs.finish_job(id=JOB_ID, status="done", recap=DAILY_RESULT)
        with pytest.raises(DryRunError, match="persist its observation"):
            runner._check_daily_result()
        daylog.append_log(daylog.stamped(DAILY_RESULT))
        runner._check_daily_result()
        assert await cron.cron() is None
        job_store.update_fields(
            paths.jobs_file(), {JOB_ID: {"Next fire time": "2026-05-10T12:00"}}
        )
        with pytest.raises(DryRunError, match="next noon occurrence"):
            runner._check_daily_result()
