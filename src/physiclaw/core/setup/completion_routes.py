"""Setup completion: return home, settle the camera, then confirm readiness."""

import asyncio
import logging
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse

from physiclaw.core.setup.run_log import run_setup_step

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from physiclaw.core.orchestration import PhysiClaw

log = logging.getLogger(__name__)


def register(mcp: "MCPServer", physiclaw: "PhysiClaw") -> None:
    """Register completion endpoints; requests acknowledge, workers record outcomes."""

    @mcp.custom_route("/api/phone/home", methods=["POST"])
    async def _home(request: Request) -> JSONResponse:  # noqa: ARG001
        try:
            await asyncio.to_thread(
                run_setup_step, physiclaw.rig, "finish.home", physiclaw.home_screen
            )
            return JSONResponse({"ok": True})
        except Exception as e:
            log.exception("home_screen failed")
            return JSONResponse({"error": str(e)}, status_code=503)

    @mcp.custom_route("/api/ready", methods=["POST"])
    async def _mark_ready(request: Request) -> JSONResponse:  # noqa: ARG001
        # Fire-and-forget: setup just parked the phone on the home screen
        # (the dark scene that exposes AE failure). become_ready settles
        # the camera FIRST (exposure tune) and
        # flips ready AFTER, so the runtime's status poll can't observe
        # ready on an unsettled camera — without stalling the wizard's
        # finish screen. This only acknowledges the request; setup must
        # poll /api/status because a busy/closed rig can skip readiness.
        def finish() -> dict:
            return {"ready": physiclaw.become_ready()}

        asyncio.get_running_loop().run_in_executor(
            None, run_setup_step, physiclaw.rig, "finish.ready", finish
        )
        return JSONResponse({"ok": True})
