"""The shared status endpoint starts setup observation without moving hardware."""

import json
from types import SimpleNamespace

from physiclaw.core.server import status
from physiclaw.core.setup.run_log import SetupRecorder


async def test_status_preserves_hardware_payload(async_request):
    rig = SimpleNamespace(status=lambda: {"arm": True, "camera": False})

    response = await status.handle_status(async_request(), rig)

    assert json.loads(response.body) == {"arm": True, "camera": False}


async def test_status_starts_journal_before_cli_connects_phone(async_request):
    recorder = SetupRecorder(lambda: {"bridge_connected": False})
    rig = SimpleNamespace(status=lambda: {"ready": False}, setup_recording=recorder)

    try:
        await status.handle_status(async_request(), rig)
        journal = (recorder.directory / "events.jsonl").read_text(encoding="utf-8")
    finally:
        recorder.close()

    assert '"event": "setup.initial_state"' in journal
    assert '"bridge_connected": false' in journal


async def test_status_route_is_registered_outside_setup(fake_mcp, async_request):
    rig = SimpleNamespace(status=lambda: {"ready": True})
    status.register(fake_mcp, rig)

    response = await fake_mcp.get("/api/status")(async_request())

    assert json.loads(response.body) == {"ready": True}
