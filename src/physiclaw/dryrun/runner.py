"""Run the production MCP routes and managed runtime against the fixtures."""

import asyncio
import datetime as dt
import json
import logging
import socket
from collections import Counter
from collections.abc import Iterator
from contextlib import AsyncExitStack, contextmanager
from functools import partial
from pathlib import Path

import httpx
import uvicorn
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from physiclaw.agent.engine import job_store, jobs
from physiclaw.cli.runtime_process import ManagedRuntime
from physiclaw.cli.server import _spawn_runtime
from physiclaw.common import daylog, paths
from physiclaw.common.logger import (
    attach_server_mcp_tee,
    setup_logging,
    write_json_atomic,
)
from physiclaw.common.ready import STATUS_PATH
from physiclaw.core.hardware.camera import CameraBackend
from physiclaw.core.orchestration.rig import HardwareRig
from physiclaw.core.server.app import build_server, shutdown
from physiclaw.core.server.planes import build_bridge_app, build_control_app
from physiclaw.core.server.warm_start import try_resume
from physiclaw.dryrun import DAILY_ACTIONS, HOST, DryRunError
from physiclaw.dryrun.devices import CaptureControls, CaptureDevice, SerialDevice
from physiclaw.dryrun.scenario import (
    DAILY_RESULT,
    JOB_ID,
    JOB_SOURCE,
    PARKED,
    REPLY,
    ModelStub,
    Phone,
    calibration,
)

_WATCH_PATH = "/api/phone/watch"
_MODEL_PATH = "/v1/responses"
_CLIPBOARD_PATH = "/api/bridge/clipboard"


class _ControlApp:
    """Expose the returning-user routes and feed the first camera wake."""

    def __init__(self, control: ASGIApp, bridge: ASGIApp, phone: Phone) -> None:
        self.bridge = bridge
        self.control = control
        self.phone = phone
        self.wake_seen = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            if scope["path"] == _CLIPBOARD_PATH:
                await self.bridge(scope, receive, send)
                return
            if scope["path"] == _WATCH_PATH:
                send = partial(self._watch, send)
            elif scope["path"] not in {STATUS_PATH, "/mcp", _MODEL_PATH}:
                response = JSONResponse(
                    {"error": "Unknown dryrun route"}, status_code=404
                )
                await response(scope, receive, send)
                return
        await self.control(scope, receive, send)

    async def _watch(self, send: Send, message: dict) -> None:
        if message["type"] == "http.response.body":
            result = json.loads(message["body"])
            if result.get("reason") == "ema initialized":
                self.phone.notify()
            if result.get("wake") is True:
                self.wake_seen = True
        await send(message)


class _Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # asyncio.run owns Ctrl+C so the runtime can close while MCP is alive.
        yield


def _seed_learned_layout() -> None:
    # A returning user already completed the three keyboard/layout captures.
    layout = paths.screen_layout_json()
    layout.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        layout,
        {
            "spotlight_input": [0.020, 0.582, 0.958, 0.660],
            "spotlight_paste": [0.090, 0.512, 0.205, 0.560],
            "space": [0.251, 0.868, 0.747, 0.920],
            "backspace": [0.867, 0.809, 0.995, 0.861],
            "return": [0.752, 0.868, 0.990, 0.918],
            "chat_input_kb_hidden": [0.098, 0.900, 0.716, 0.950],
            "chat_input_kb_visible": [0.098, 0.586, 0.716, 0.634],
            "send": [0.752, 0.868, 0.990, 0.918],
            "chat_paste": [0.120, 0.520, 0.300, 0.566],
            "im_app": "WhatsApp",
            "layout_learned": True,
        },
    )


async def run(listener: socket.socket, *, verbose: bool) -> None:
    paths.ensure_dirs()
    setup_logging(
        "dryrun",
        logging.DEBUG if verbose else logging.INFO,
        file_dir=paths.LOG_DIR,
        file_prefix="server",
        file_level=logging.DEBUG,
    )
    attach_server_mcp_tee()
    _seed_learned_layout()
    calibration().save(paths.calibration_bundle())
    port = listener.getsockname()[1]

    def fetch_clipboard() -> str:
        # The virtual iOS Shortcut makes the same HTTP request as the real one.
        response = httpx.get(
            f"http://{HOST}:{port}{_CLIPBOARD_PATH}", trust_env=False, timeout=5
        )
        response.raise_for_status()
        return response.text

    phone = Phone(fetch_clipboard)
    model = ModelStub()
    async with AsyncExitStack() as cleanup:
        transcript = cleanup.enter_context(
            (paths.HOME / "serial.log").open("w", encoding="utf-8")
        )
        serial = SerialDevice(PARKED, phone.touch, transcript)
        rig = HardwareRig(
            serial_backend=serial,
            camera_backend=CameraBackend(
                open_capture=lambda _index, _backend: CaptureDevice(phone.frame),
                controls=CaptureControls(),
            ),
        )
        bundle = build_server(rig=rig)
        cleanup.push_async_callback(asyncio.to_thread, shutdown, bundle)
        bundle.mcp.custom_route(_MODEL_PATH, methods=["POST"])(model.respond)
        app = _ControlApp(
            build_control_app(bundle.mcp, HOST),
            build_bridge_app(bundle.phone_app),
            phone,
        )
        server = _Server(
            uvicorn.Config(
                app,
                host=HOST,
                port=port,
                log_config=None,
                access_log=False,
                timeout_graceful_shutdown=5,
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        cleanup.push_async_callback(_stop_server, server, serving)
        await _wait_for_server(server, serving)
        runtime = ManagedRuntime(
            _spawn_runtime(HOST, port, verbose, "dryrun"), timeout=8
        )
        cleanup.push_async_callback(runtime.stop)
        started = dt.datetime.now()
        ready = await asyncio.to_thread(
            try_resume,
            None,
            verify=False,
            physiclaw=bundle.physiclaw,
            calib=bundle.calib,
            phone=bundle.phone,
        )
        if not ready:
            raise DryRunError(serial.failure or "The virtual rig did not become ready")
        first = await _check_session(runtime, app, serial, model, "phone")
        if not app.wake_seen or phone.sent_reply != REPLY:
            raise DryRunError(
                "The phone workflow must wake on camera change and deliver Sam’s reply"
            )
        model.actions, model.turns = DAILY_ACTIONS, 0
        _make_daily_job_due(started)
        await _check_session(runtime, app, serial, model, JOB_SOURCE, previous=first)
        _check_daily_result()
    if runtime.proc.returncode != 0:
        raise DryRunError("The managed runtime did not shut down cleanly")
    if failure := model.failure or serial.failure or phone.failure:
        raise DryRunError(failure)
    if serial.port is None or serial.port.is_open:
        raise DryRunError("The virtual serial port did not close")


async def _wait_for_server(server: _Server, serving: asyncio.Task[None]) -> None:
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                    raise DryRunError("The MCP listener did not start")
                await asyncio.sleep(0.05)
    except TimeoutError as exc:
        raise DryRunError("Timed out starting the MCP listener") from exc


async def _stop_server(server: _Server, serving: asyncio.Task[None]) -> None:
    server.should_exit = True
    if not server.started and not serving.done():
        # Uvicorn checks should_exit after its ASGI startup hook returns.
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        return
    try:
        await asyncio.wait_for(serving, timeout=10)
    except TimeoutError as exc:
        raise DryRunError("Timed out shutting down the MCP listener") from exc


async def _check_session(
    runtime: ManagedRuntime,
    app: _ControlApp,
    serial: SerialDevice,
    model: ModelStub,
    source: str,
    *,
    previous: Path | None = None,
) -> Path:
    sessions = paths.sessions_dir()
    try:
        async with asyncio.timeout(130):
            while True:
                failure = app.phone.failure or serial.failure or model.failure
                if failure:
                    raise DryRunError(failure)
                if runtime.proc.poll() is not None:
                    raise DryRunError(
                        f"The managed runtime exited with status {runtime.proc.returncode}"
                    )
                if summary_path := next(
                    (p for p in sessions.glob("*/summary.json") if p != previous), None
                ):
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    _verify_session(summary, source, model, app.phone)
                    return summary_path
                await asyncio.sleep(0.05)
    except TimeoutError as exc:
        stage = (
            f"after {model.turns} model turns"
            if app.wake_seen
            else "waiting for a camera wake"
        )
        raise DryRunError(f"Timed out during {source} session {stage}") from exc


def _verify_session(summary: dict, source: str, model: ModelStub, phone: Phone) -> None:
    outcome = summary["outcome"]
    if outcome["sentinel"] != "DONE":
        raise DryRunError(f"Agent ended with {outcome['sentinel']}: {outcome['recap']}")
    if [event["source"] for event in summary["triggers"]] != [source]:
        raise DryRunError(f"The session did not start from the expected {source} wake")
    turns = len(model.actions)
    if not summary["turns"] == summary["provider_calls"] == model.turns == turns:
        raise DryRunError("The session did not complete the expected model requests")
    if any(summary["errors"].values()) or outcome["crashed"]:
        raise DryRunError(f"Errors during {source} session: {summary['errors']}")
    expected_tools = Counter(model.actions)
    # Each structured turn writes its summary through the engine's note tool.
    expected_tools["note"] = turns
    if summary["tool_calls"] != expected_tools:
        raise DryRunError(f"Unexpected {source} tool calls: {summary['tool_calls']}")
    if phone.lamp_screen != "done" or phone.screen != "home":
        raise DryRunError(
            "Expected the requested lamp settings and the phone back home"
        )


def _next_noon(at: dt.datetime) -> dt.datetime:
    noon = at.replace(hour=12, minute=0, second=0, microsecond=0)
    return noon if noon > at else noon + dt.timedelta(days=1)


def _make_daily_job_due(started: dt.datetime) -> None:
    job = jobs.get_job(JOB_ID)
    if (job.kind, job.schedule, job.status) != (
        job_store.KIND_PERIODIC,
        "0 12 * * *",
        job_store.STATUS_PEND,
    ) or job.last_fire_time:
        raise DryRunError("The phone session did not create a pending daily noon job")
    now = dt.datetime.now()
    scheduled = dt.datetime.fromisoformat(job.next_fire_time)
    if not _next_noon(started) <= scheduled <= _next_noon(now):
        raise DryRunError("The first occurrence was not scheduled for the next noon")
    # Simulate a missed noon in the isolated store. The normal runtime loop owns
    # due detection, firing, next-fire calculation and the following session.
    previous_noon = (now - dt.timedelta(days=1)).replace(hour=12, minute=0)
    job_store.update_fields(
        paths.jobs_file(),
        {JOB_ID: {"Next fire time": job_store.format_minute(previous_noon)}},
    )


def _check_daily_result() -> None:
    job = jobs.get_job(JOB_ID)
    if (
        not job.last_fire_time
        or not job.execution_time
        or job.execution_result != DAILY_RESULT
        or job.status != job_store.STATUS_PEND
    ):
        raise DryRunError("The daily lamp check did not finish and re-arm its job")
    fired = dt.datetime.fromisoformat(job.last_fire_time)
    if job.next_fire_time != job_store.format_minute(_next_noon(fired)):
        raise DryRunError("The daily job did not schedule the next noon occurrence")
    if DAILY_RESULT not in daylog.load_recent_entries(1):
        raise DryRunError("The daily check did not persist its observation in the log")
