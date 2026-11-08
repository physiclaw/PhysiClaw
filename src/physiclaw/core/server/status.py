"""Shared hardware-service status, used by setup, runtime and diagnostics."""

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse

from physiclaw.common.ready import STATUS_PATH

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from physiclaw.core.orchestration import HardwareRig


async def handle_status(request: Request, rig: "HardwareRig") -> JSONResponse:
    """Readiness probes must not start setup or create evidence directories."""
    return JSONResponse(rig.status())


def register(mcp: "MCPServer", rig: "HardwareRig") -> None:
    @mcp.custom_route(STATUS_PATH, methods=["GET"])
    async def status(request: Request) -> JSONResponse:
        return await handle_status(request, rig)
