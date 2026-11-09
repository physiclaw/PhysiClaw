"""Shared status polling is observational, including during saved-calibration resume."""

import json
import logging
from types import SimpleNamespace

import pytest

from physiclaw.core.server import status
from physiclaw.core.setup.run_log import SetupRecorder


async def test_status_preserves_hardware_payload(async_request):
    rig = SimpleNamespace(status=lambda: {"arm": True, "camera": False})

    response = await status.handle_status(async_request(), rig)

    assert json.loads(response.body) == {"arm": True, "camera": False}


@pytest.mark.parametrize("calibrated", [False, True])
async def test_readiness_polling_never_starts_setup(
    async_request, calibrated, physiclaw_home, caplog
):
    recorder = SetupRecorder(dict)
    payload = {"ready": False, "calibrated": calibrated}
    rig = SimpleNamespace(status=lambda: payload, setup_recording=recorder)

    try:
        with caplog.at_level(logging.INFO):
            for ready in (False, False, True):
                payload["ready"] = ready
                response = await status.handle_status(async_request(), rig)
                assert json.loads(response.body) == payload
        assert recorder.directory is None
    finally:
        recorder.close()

    assert not (physiclaw_home / "log" / "setup").exists()
    assert "Setup evidence:" not in caplog.text


async def test_status_route_is_registered_outside_setup(fake_mcp, async_request):
    rig = SimpleNamespace(status=lambda: {"ready": True})
    status.register(fake_mcp, rig)

    response = await fake_mcp.get("/api/status")(async_request())

    assert json.loads(response.body) == {"ready": True}
