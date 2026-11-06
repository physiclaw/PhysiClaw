"""/api/phone/watch — HTTP route for phone-screen wake detection.

Thin wiring layer: detection logic lives in `Perception.watch()`,
backed by `physiclaw.core.vision.watchdog.Watchdog`. Setup completion
routes live in `core.setup.completion_routes`.

Designed to be polled by `physiclaw.agent.runtime.Runtime`. See that module for
the client-side loop.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from physiclaw.core.orchestration import PhysiClaw

log = logging.getLogger(__name__)


def register(mcp: "MCPServer", physiclaw: "PhysiClaw") -> None:
    """Wire the /api/phone/watch route onto the MCPServer server."""

    @mcp.custom_route("/api/phone/watch", methods=["GET"])
    async def _watch(request: Request) -> JSONResponse:  # noqa: ARG001
        try:
            result = await asyncio.to_thread(physiclaw.perception.watch)
            return JSONResponse(result)
        except RuntimeError as e:
            log.debug("watch skipped: %s", e)
            return JSONResponse({"wake": False, "reason": ""})
        except Exception as e:
            log.exception("watch poll failed")
            return JSONResponse({"error": str(e)}, status_code=503)
