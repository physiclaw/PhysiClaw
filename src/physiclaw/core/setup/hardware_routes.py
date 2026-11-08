"""Register hardware connection and preview endpoints backed by hardware_handlers."""

import asyncio
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from physiclaw.common.ready import ready_from_status
from physiclaw.core.bridge import PageState
from physiclaw.core.setup.hardware_handlers import (
    handle_camera_preview,
    handle_connect_arm,
    handle_connect_camera,
    handle_disconnect_camera,
)
from physiclaw.core.setup.pages import handle_setup_page

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from physiclaw.core.orchestration import HardwareRig


def register(mcp: "MCPServer", rig: "HardwareRig", phone: PageState) -> None:
    """Register hardware setup routes."""

    async def begin_recording() -> None:
        recorder = getattr(rig, "setup_recording", None)
        if recorder is not None and not ready_from_status(rig.status()):
            await asyncio.to_thread(recorder.begin)

    @mcp.custom_route("/api/setup/begin", methods=["POST"])
    async def _begin_setup(request: Request) -> JSONResponse:
        # Explicit setup intent, before CLI/auto waits for phone connection.
        await begin_recording()
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/setup-hardware", methods=["GET"])
    async def _setup_page(request: Request) -> HTMLResponse:
        await begin_recording()
        return await handle_setup_page(request)

    @mcp.custom_route("/api/connect-arm", methods=["POST"])
    async def _connect_arm(request: Request) -> JSONResponse:
        return await handle_connect_arm(request, rig)

    @mcp.custom_route("/api/connect-camera", methods=["POST"])
    async def _connect_camera(request: Request) -> JSONResponse:
        return await handle_connect_camera(request, rig, phone)

    @mcp.custom_route("/api/disconnect-camera", methods=["POST"])
    async def _disconnect_camera(request: Request) -> JSONResponse:
        return await handle_disconnect_camera(request, rig)

    @mcp.custom_route("/api/camera-preview/{index}", methods=["GET"])
    async def _camera_preview(request: Request) -> JSONResponse:
        return await handle_camera_preview(request, rig)
