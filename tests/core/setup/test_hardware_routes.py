"""Tests for `physiclaw.core.setup.hardware_routes` — hardware setup route registration.

`fake_mcp` and `async_request` fixtures live in `conftest.py`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from starlette.responses import HTMLResponse

from physiclaw.common.text import read_text
from physiclaw.core.setup import hardware_routes as hw_reg
from physiclaw.core.setup.run_log import SetupRecorder, run_setup_step


def test_hardware_register_wires_all_routes(fake_mcp) -> None:
    hw_reg.register(fake_mcp, rig=MagicMock(), phone=MagicMock())

    by_path = {(p, ms[0]) for p, ms, _ in fake_mcp.routes}
    assert by_path == {
        ("/setup-hardware", "GET"),
        ("/api/setup/begin", "POST"),
        ("/api/connect-arm", "POST"),
        ("/api/connect-camera", "POST"),
        ("/api/disconnect-camera", "POST"),
        ("/api/camera-preview/{index}", "GET"),
    }


@pytest.mark.asyncio
async def test_hardware_routes_forward_to_handlers(
    fake_mcp,
    async_request,
    mocker,
) -> None:
    spies = {
        "handle_setup_page": mocker.patch.object(hw_reg, "handle_setup_page"),
        "handle_connect_arm": mocker.patch.object(hw_reg, "handle_connect_arm"),
        "handle_connect_camera": mocker.patch.object(hw_reg, "handle_connect_camera"),
        "handle_camera_preview": mocker.patch.object(hw_reg, "handle_camera_preview"),
    }

    async def _ok(*a, **kw):
        return "ok"

    for s in spies.values():
        s.side_effect = _ok

    pl, ph = MagicMock(), MagicMock()
    hw_reg.register(fake_mcp, pl, ph)

    req = async_request()
    await fake_mcp.get("/setup-hardware")(req)
    spies["handle_setup_page"].assert_called_once_with(req)

    await fake_mcp.get("/api/connect-arm", "POST")(req)
    spies["handle_connect_arm"].assert_called_once_with(req, pl)

    await fake_mcp.get("/api/connect-camera", "POST")(req)
    spies["handle_connect_camera"].assert_called_once_with(req, pl, ph)

    await fake_mcp.get("/api/camera-preview/{index}")(req)
    spies["handle_camera_preview"].assert_called_once_with(req, pl)


@pytest.fixture
def setup_rig(fake_mcp, mocker):
    rig = SimpleNamespace(ready=False)
    rig.status = lambda: {"ready": rig.ready}
    rig.setup_recording = SetupRecorder(
        lambda: {"hardware": rig.status(), "bridge_connected": False}
    )
    mocker.patch.object(hw_reg, "handle_setup_page", return_value=HTMLResponse(""))
    hw_reg.register(fake_mcp, rig, MagicMock())
    yield rig
    rig.setup_recording.close()


@pytest.mark.parametrize(
    "path,method", [("/setup-hardware", "GET"), ("/api/setup/begin", "POST")]
)
async def test_setup_intent_records_phone_wait_and_preserves_completed_run(
    fake_mcp, async_request, setup_rig, path, method
):
    recorder = setup_rig.setup_recording
    begin = fake_mcp.get(path, method)
    request = async_request()

    response = await begin(request)
    assert response.status_code == 200
    if method == "POST":
        assert json.loads(response.body) == {"status": "ok"}
    directory = recorder.directory
    events_path, summary_path = directory / "events.jsonl", directory / "summary.json"
    initial_events = read_text(events_path)
    assert '"event": "setup.initial_state"' in initial_events
    assert '"bridge_connected": false' in initial_events
    assert json.loads(read_text(summary_path))["steps"] == {}

    await begin(request)
    assert recorder.directory == directory
    assert read_text(events_path) == initial_events

    setup_rig.ready = True
    run_setup_step(setup_rig, "finish.ready", lambda: {"ready": True})
    completed_summary = read_text(summary_path)
    assert json.loads(completed_summary)["status"] == "succeeded"
    await begin(request)
    assert recorder.directory == directory
    recorder.close()
    assert read_text(summary_path) == completed_summary


@pytest.mark.parametrize(
    "path,method", [("/setup-hardware", "GET"), ("/api/setup/begin", "POST")]
)
async def test_setup_on_ready_rig_does_not_create_empty_run(
    fake_mcp, async_request, setup_rig, physiclaw_home, path, method
):
    setup_rig.ready = True

    response = await fake_mcp.get(path, method)(async_request())

    assert response.status_code == 200
    assert setup_rig.setup_recording.directory is None
    assert not (physiclaw_home / "log" / "setup").exists()
