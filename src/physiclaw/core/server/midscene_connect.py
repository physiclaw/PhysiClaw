"""Single-device Midscene Connect v1 HTTP service, mounted on the control plane."""

import asyncio
import base64
import hmac
import logging
from functools import wraps

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from physiclaw.core.orchestration.midscene_device import MidsceneDevice
from physiclaw.core.orchestration.orchestrator import PhysiClaw
from physiclaw.core.orchestration.rig import TOOL_LOCK_WAIT_SECONDS
from physiclaw.core.server.midscene_schema import (
    ACTIONS,
    PRIMITIVES,
    device_info,
)

log = logging.getLogger(__name__)


def error(status: int, code: str, message: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    return JSONResponse(
        {"error": message, "code": code}, status_code=status, headers=headers
    )


def device_app(pl: PhysiClaw, token: str) -> Starlette:
    if not token:
        raise ValueError("A device access token is required")
    expected = f"Bearer {token}".encode()
    device = MidsceneDevice(pl)
    queue = asyncio.Lock()

    async def run_device(operation, *args):
        try:
            await asyncio.wait_for(queue.acquire(), timeout=TOOL_LOCK_WAIT_SECONDS)
        except TimeoutError as exc:
            raise RuntimeError("PhysiClaw is busy; retry the request") from exc
        try:
            return await run_in_threadpool(operation, *args)
        finally:
            queue.release()

    def authenticated(handler):
        @wraps(handler)
        async def checked(request: Request):
            auth = request.headers.getlist("authorization")
            if len(auth) != 1 or not hmac.compare_digest(auth[0].encode(), expected):
                return error(401, "UNAUTHORIZED", "A valid bearer token is required")
            try:
                return await handler(request)
            except ValidationError:
                return error(
                    400, "INVALID_ARGUMENT", "Invalid device action parameters"
                )
            except ValueError as exc:
                return error(400, "INVALID_ARGUMENT", str(exc))
            except (OSError, RuntimeError) as exc:
                return error(503, "DEVICE_UNAVAILABLE", str(exc))
            except Exception:
                log.exception("Midscene device operation failed")
                return error(500, "INTERNAL_ERROR", "Device operation failed")

        return checked

    @authenticated
    async def info(request: Request):
        return JSONResponse(device_info())

    @authenticated
    async def screenshot(request: Request):
        jpeg, width, height = await run_device(device.screenshot)
        image = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        return JSONResponse(
            {"width": width, "height": height, "image": image},
            headers={"Cache-Control": "no-store"},
        )

    @authenticated
    async def size(request: Request):
        width, height = await run_device(device.size)
        return JSONResponse(
            {"width": width, "height": height}, headers={"Cache-Control": "no-store"}
        )

    @authenticated
    async def action(request: Request):
        kind, name = request.path_params["kind"], request.path_params["name"]
        if kind == "primitives" and name in PRIMITIVES:
            model = PRIMITIVES[name]
        elif kind == "actions" and name in ACTIONS:
            model = ACTIONS[name][0]
        else:
            return error(404, "NOT_FOUND", "Unknown device action")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 64000:
                return error(413, "BODY_TOO_LARGE", "Request body too large")
            body.extend(chunk)
        args = model.model_validate_json(body.strip() or b"{}").model_dump(
            by_alias=True, exclude_none=True
        )
        value = await run_device(device.act, name, args)
        result = {"ok": True}
        if kind == "actions" and value is not None:
            return JSONResponse({**result, "result": value})
        return JSONResponse(result)

    routes = [
        Route("/api/info", info),
        Route("/api/screenshot", screenshot),
        Route("/api/size", size),
        Route("/api/{kind}/{name}", action, methods=["POST"]),
    ]
    return Starlette(routes=routes)
