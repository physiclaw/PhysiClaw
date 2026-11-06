"""HTTP request handlers for hardware connection and camera preview.

Used by the /setup skill to query status, connect the GRBL arm, enumerate
cameras, and connect a chosen camera. Each handler runs blocking work in
a thread executor so the Starlette event loop stays responsive. The
camera identification/preview algorithms live in
``core/setup/camera_selection.py`` — this module is HTTP marshalling
over the rig and those functions, registered by ``core/setup/hardware_routes.py``.
"""

import asyncio
import base64
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse

from physiclaw.common.logger.evidence import current_evidence, record_event
from physiclaw.core.bridge import PageState
from physiclaw.core.bridge.handler import json_or_none
from physiclaw.core.hardware.device import DeviceNotFound
from physiclaw.core.setup.camera_selection import camera_preview, resolve_auto_index
from physiclaw.core.setup.run_log import record_rejection, run_setup_step

if TYPE_CHECKING:
    from physiclaw.core.orchestration import HardwareRig


def capture_setup_view(rig: "HardwareRig", checkpoint: str) -> None:
    """Capture a diagnostic view under the caller's rig lock, when available.

    Decision inputs are saved by Camera itself. These additional views show
    setup boundaries, including arm-only steps; failure never changes an action.
    """
    if current_evidence() is None:
        return
    cam = rig.cam
    record_event(
        "step.camera_checkpoint", checkpoint=checkpoint, available=cam is not None
    )
    if cam is not None:
        try:
            cam.raw_frame()
        except Exception as exc:
            record_event(
                "step.camera_checkpoint_failed",
                checkpoint=checkpoint,
                exception_type=type(exc).__name__,
                message=str(exc),
            )


# ─── Stylus arm ─────────────────────────────────────────────


async def handle_connect_arm(request: Request, rig: "HardwareRig") -> JSONResponse:
    """POST /api/connect-arm — auto-detect and connect the GRBL arm."""

    def _do() -> None:
        with rig.locked():
            capture_setup_view(rig, "before_arm_connect")
            rig.connect_arm()
            capture_setup_view(rig, "after_arm_connect")

    try:
        await asyncio.to_thread(run_setup_step, rig, "arm.connect", _do)
        return JSONResponse({"status": "ok", "message": "Arm connected"})
    except DeviceNotFound as e:
        # Client state (plug it in / power it), not a server fault — the
        # same 409 convention the calibration preconditions use.
        return JSONResponse({"status": "error", "message": str(e)}, status_code=409)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ─── Camera ─────────────────────────────────────────────────


async def handle_connect_camera(
    request: Request, rig: "HardwareRig", phone: PageState
) -> JSONResponse:
    """POST /api/connect-camera — open a camera by index.

    Body: ``{"index": int}`` — connect that camera directly.
    Body: ``{"index": "auto"}`` (or body omitted) — auto-pick via the
    RGBM corner markers (see ``camera_selection.resolve_auto_index``).
    """
    body = await json_or_none(request, allow_empty=True)
    if body is None:
        await asyncio.to_thread(
            record_rejection, rig, "camera.connect", "body must be a JSON object"
        )
        return JSONResponse(
            {"status": "error", "message": "body must be a JSON object"},
            status_code=400,
        )
    index = body.get("index")
    if index is None or index == "auto":
        index = None
    elif type(index) is not int or index < 0:
        await asyncio.to_thread(
            record_rejection,
            rig,
            "camera.connect",
            "index must be a non-negative integer or auto",
        )
        return JSONResponse(
            {
                "status": "error",
                "message": "index must be a non-negative integer or auto",
            },
            status_code=400,
        )

    def _do() -> int:
        with rig.locked():
            # Auto-pick opens devices and changes the calibration page too.
            # Keep selection, connection, and the response snapshot in one lock.
            selected = resolve_auto_index(rig, phone) if index is None else index
            rig.connect_camera(selected)
            cam = rig.require_cam()
            rig.calibration.cam_index = cam.index
            capture_setup_view(rig, "after_camera_connect")
            return cam.index

    try:
        connected_index = await asyncio.to_thread(
            run_setup_step,
            rig,
            "camera.connect",
            _do,
            parameters={"index": "auto" if index is None else index},
        )
        return JSONResponse(
            {
                "status": "ok",
                "message": f"Camera {connected_index} connected",
                "index": connected_index,
            }
        )
    except DeviceNotFound as e:
        # Same 409 convention as connect-arm: a missing device is the
        # operator's to fix, not a server fault.
        return JSONResponse({"status": "error", "message": str(e)}, status_code=409)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


async def handle_disconnect_camera(
    request: Request, rig: "HardwareRig"
) -> JSONResponse:
    """POST /api/disconnect-camera — release the camera device handle.

    Used by `setup hardware` step 8 so the OS camera-preview app can
    claim the device while the user adjusts the camera angle. Idempotent
    — returns ``released=False`` if no camera was connected.
    """

    def _do() -> bool:
        with rig.locked():
            capture_setup_view(rig, "before_camera_disconnect")
            return rig.disconnect_camera()

    try:
        released = await asyncio.to_thread(
            run_setup_step, rig, "camera.disconnect", _do
        )
        return JSONResponse({"status": "ok", "released": released})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


async def handle_camera_preview(request: Request, rig: "HardwareRig") -> JSONResponse:
    """GET /api/camera-preview/{index} — capture one frame from a camera index."""
    watermark = request.query_params.get("watermark", "0") == "1"
    try:
        # Inside the try: the route has no :int converter, so a
        # non-numeric index must produce this module's JSON error shape,
        # not a bare 500.
        raw_index = request.path_params["index"]

        def _do() -> tuple[int, bytes]:
            index = int(raw_index)
            with rig.locked():
                return index, camera_preview(index, watermark, connected=rig.cam)

        index, jpeg = await asyncio.to_thread(
            run_setup_step,
            rig,
            "camera.preview",
            _do,
            parameters={"index": raw_index, "watermark": watermark},
        )
        return JSONResponse(
            {"status": "ok", "index": index, "image": base64.b64encode(jpeg).decode()}
        )
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=404)
