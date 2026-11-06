"""Shared hardware-service status, used by setup, runtime and diagnostics."""

import asyncio
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse

from physiclaw.common.ready import STATUS_PATH

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from physiclaw.core.orchestration import HardwareRig


async def handle_status(request: Request, rig: "HardwareRig") -> JSONResponse:
    status = rig.status()
    recorder = getattr(rig, "setup_recording", None)
    # The CLI starts with a status request rather than opening the wizard.
    # Begin server observation before it waits for the phone or rig placement.
    if recorder is not None and not status.get("ready"):
        await asyncio.to_thread(recorder.begin)
    return JSONResponse(status)


def register(mcp: "MCPServer", rig: "HardwareRig") -> None:
    @mcp.custom_route(STATUS_PATH, methods=["GET"])
    async def status(request: Request) -> JSONResponse:
        return await handle_status(request, rig)
