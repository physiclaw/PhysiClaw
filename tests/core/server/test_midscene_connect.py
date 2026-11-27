"""Connect protocol through the mounted control app, with an in-memory rig."""

import asyncio
import base64
import time
from threading import Event
from unittest.mock import MagicMock

import cv2
import httpx
import numpy as np
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from physiclaw.common import paths
from physiclaw.core.calibration.transforms import ScreenTransforms, ViewportShift
from physiclaw.core.hardware.arm import DEFAULT_RAPID_SPEED
from physiclaw.core.hardware.frame_reader import PublishedFrame
from physiclaw.core.orchestration import gestures
from physiclaw.core.orchestration.midscene_device import MidsceneDevice
from physiclaw.core.orchestration.orchestrator import PhysiClaw
from physiclaw.core.orchestration.rig import HardwareRig
from physiclaw.core.server import app as assembly
from physiclaw.core.server import tools as tools_mod
from physiclaw.core.server import warm_start
from physiclaw.core.setup import completion_routes

PREFIX = "/midscene-device/api"
AUTH = {"Authorization": "Bearer test-token"}


def mock_phone():
    phone = MagicMock(spec=PhysiClaw)
    rig = phone.rig = MagicMock()
    rig.ready = True
    cal = rig.calibration
    cal.viewport_shift.screenshot_width = 80
    cal.viewport_shift.screenshot_height = 160
    camera = rig.require_cam.return_value
    camera.rotation = cal.cam_rotation = -1
    camera.snapshot_with_time.side_effect = lambda: PublishedFrame(
        np.zeros((160, 80, 3), np.uint8), time.time()
    )
    rig.require_transforms.return_value = ScreenTransforms(
        np.diag([50.0, 100.0, 1.0])[:2], np.eye(2, 3), (80, 160)
    )
    rig.assistive_touch.overlaps_at.return_value = False
    rig.assistive_touch.swipe_crosses_at.return_value = False
    arm = rig.require_arm.return_value
    arm.LONG_PRESS_DURATION = 1.2
    arm.RAPID_SPEED = DEFAULT_RAPID_SPEED
    arm.SWIPE_SPEEDS = {"fast": 10000}
    phone.execute_unlock.return_value = "Passcode entered"
    phone.send_to_clipboard.return_value = "copied"
    return phone


@pytest.fixture
def device(monkeypatch):
    phone = mock_phone()
    monkeypatch.setattr(assembly, "physiclaw", phone)
    control, _ = assembly.build_apps(midscene_token="test-token")
    return phone, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://127.0.0.1"
    )


@pytest.mark.asyncio
async def test_discovery_auth_docs_and_route_isolation(device):
    phone, client = device
    async with client:
        phone.rig.ready = False
        for path in ("info", "screenshot", "size"):
            assert (await client.get(f"{PREFIX}/{path}")).status_code == 401
        assert (
            await client.post(
                f"{PREFIX}/actions/forceQuit", headers={"Authorization": "Bearer wrong"}
            )
        ).status_code == 401
        phone.execute_steps.assert_not_called()
        info = (await client.get(f"{PREFIX}/info", headers=AUTH)).json()
        assert info["protocol"] == "midscene-connect/1"
        assert info["description"]
        actions_by_name = {a["name"]: a for a in info["actions"]}
        assert actions_by_name["sendToClipboard"]["params"] == {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1, "maxLength": 10000}
            },
            "required": ["text"],
        }
        spec = (await client.get(f"{PREFIX}/openapi.json")).json()
        assert all("/primitives/" + p in spec["paths"] for p in info["primitives"])
        assert all("/actions/" + a["name"] in spec["paths"] for a in info["actions"])
        docs = await client.get(f"{PREFIX}/docs/")
        assert docs.status_code == 200
        assert "SwaggerUIBundle" in docs.text
        for asset in ("swagger-ui-bundle.js", "swagger-ui.css"):
            assert (
                await client.get(f"{PREFIX}/docs/assets/{asset}")
            ).status_code == 200
        assert (await client.get(f"{PREFIX}/size", headers=AUTH)).status_code == 503
    for app in assembly.build_apps():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            assert (await client.get(f"{PREFIX}/info", headers=AUTH)).status_code == 404


@pytest.mark.asyncio
async def test_copied_device_url_authenticates_without_a_header(device):
    phone, client = device
    async with client:
        base = "/midscene-device/test-token/api"
        assert (await client.get(f"{base}/info")).json()[
            "protocol"
        ] == "midscene-connect/1"
        assert (await client.get(f"{base}/screenshot")).is_success
        assert (await client.get(f"{base}/size")).is_success
        point = {"point": {"x": 1, "y": 2}}
        assert (
            await client.post("/midscene-device/wrong/api/primitives/tap", json=point)
        ).status_code == 401
        phone.rig.move_to_point.assert_not_called()
        assert (await client.post(f"{base}/primitives/tap", json=point)).is_success


@pytest.mark.asyncio
async def test_expired_queued_tap_never_moves(device, monkeypatch):
    phone, client = device
    monkeypatch.setattr(
        "physiclaw.core.server.midscene_connect.TOOL_LOCK_WAIT_SECONDS", 0.05
    )
    started = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()

    def hold_home(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(3)

    phone.execute_steps.side_effect = hold_home
    async with client:
        home = asyncio.create_task(
            client.post(f"{PREFIX}/primitives/homeButton", headers=AUTH)
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            tap = await client.post(
                f"{PREFIX}/primitives/tap",
                headers=AUTH,
                json={"point": {"x": 1, "y": 1}},
            )
            assert tap.status_code == 503
            assert "busy" in tap.json()["error"]
        finally:
            release.set()
            assert (await home).is_success
        assert (await client.get(f"{PREFIX}/size", headers=AUTH)).is_success
    phone.rig.move_to_point.assert_not_called()


@pytest.mark.asyncio
async def test_missing_keypad_fails_unlock_request(device, mocker):
    phone, client = device
    pl = PhysiClaw(rig=phone.rig)
    mocker.patch.object(pl._unlock, "unlock_phone", return_value=None)
    phone.execute_unlock.side_effect = pl.execute_unlock
    async with client:
        response = await client.post(f"{PREFIX}/actions/unlockPhone", headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"] == "Failed to find passcode keypad"


@pytest.mark.asyncio
async def test_screen_pixels_actions_and_input_validation(device):
    phone, client = device
    rig = phone.rig
    async with client:

        async def post(kind, name, args):
            return await client.post(f"{PREFIX}/{kind}/{name}", json=args, headers=AUTH)

        point = {"x": 79, "y": 1}
        for name in ("tap", "doubleClick", "longPress"):
            params = {"point": point}
            if name != "doubleClick":
                params["duration"] = 100
            assert (await post("primitives", name, params)).json() == {"ok": True}
            rig.move_to_point.assert_called_with(79 / 80, 1 / 160)
        arm = rig.require_arm.return_value
        arm.tap.assert_called_once_with(0.1)
        arm.long_press.assert_called_once_with(0.1)
        arm.double_tap.assert_called_once_with()
        path = {"from": {"x": 0, "y": 0}, "to": {"x": 40, "y": 80}}
        assert (
            await post("primitives", "swipe", {**path, "repeat": 2, "duration": 1000})
        ).is_success
        assert arm.swipe_to.call_count == 2
        arm.swipe_to.assert_called_with(25, 50, start_dwell=0.0, feed_mm_min=3354)
        assert (await post("primitives", "dragAndDrop", path)).is_success
        arm.swipe_to.assert_called_with(25, 50, start_dwell=1.2, feed_mm_min=None)
        size = (await client.get(f"{PREFIX}/size", headers=AUTH)).json()
        frame = (await client.get(f"{PREFIX}/screenshot", headers=AUTH)).json()
        image = cv2.imdecode(
            np.frombuffer(base64.b64decode(frame["image"].split(",")[1]), np.uint8), 1
        )
        assert (
            size
            == {"width": frame["width"], "height": frame["height"]}
            == {"width": 80, "height": 160}
        )
        assert image.shape[:2] == (160, 80)
        for name in ("homeButton", "backButton", "recentAppsButton"):
            assert (await post("primitives", name, {})).is_success
        phone.execute_steps.assert_any_call(gestures.HOME_SCREEN, require_ready=True)
        phone.execute_steps.assert_any_call(gestures.GO_BACK, require_ready=True)
        phone.execute_steps.assert_any_call((gestures.RECENT_APPS,), require_ready=True)
        assert (await post("actions", "forceQuit", {})).json() == {
            "ok": True,
            "result": "Force-quit current app",
        }
        phone.execute_steps.assert_any_call(gestures.FORCE_QUIT, require_ready=True)
        assert (await post("actions", "unlockPhone", {})).json() == {
            "ok": True,
            "result": "Passcode entered",
        }
        phone.execute_unlock.assert_called_once_with(require_ready=True)
        assert (await post("actions", "sendToClipboard", {"text": "hello"})).is_success
        phone.send_to_clipboard.assert_called_once_with("hello", require_ready=True)
        rig.move_to_point.reset_mock()
        for invalid in ({"x": 80, "y": 0}, {"x": True, "y": 1}, {"x": 0, "y": -1}):
            assert (
                await post("primitives", "tap", {"point": invalid})
            ).status_code == 400
        rig.assistive_touch.swipe_crosses_at.return_value = True
        assert (await post("primitives", "swipe", path)).status_code == 400
        rig.move_to_point.assert_not_called()


@pytest.mark.asyncio
async def test_post_action_capture_and_changed_geometry(device):
    phone, client = device
    camera = phone.rig.require_cam.return_value
    frame = np.zeros((160, 80, 3), np.uint8)
    point = {"point": {"x": 5, "y": 7}}
    async with client:
        assert (
            await client.post(f"{PREFIX}/primitives/tap", headers=AUTH, json=point)
        ).is_success
        stale = PublishedFrame(frame, 1.0)
        fresh = PublishedFrame(frame, time.time())
        camera.snapshot_with_time.side_effect = [stale, fresh]
        assert (await client.get(f"{PREFIX}/screenshot", headers=AUTH)).is_success
        assert camera.snapshot_with_time.call_count == 2
        phone.rig.require_transforms.return_value.pct_to_grbl[0, 2] = 1
        assert (
            await client.post(f"{PREFIX}/primitives/tap", headers=AUTH, json=point)
        ).status_code == 503
        phone.rig.require_arm.return_value.tap.assert_called_once()
        camera.snapshot_with_time.side_effect = lambda: PublishedFrame(
            frame, time.time()
        )
        assert (await client.get(f"{PREFIX}/screenshot", headers=AUTH)).is_success
        assert (
            await client.post(f"{PREFIX}/primitives/tap", headers=AUTH, json=point)
        ).is_success


@pytest.fixture
def physical_device():
    # Real rig state, lock, coordinate conversion and park. Only I/O is mocked.
    phone = mock_phone()
    hardware = phone.rig
    rig = phone.rig = HardwareRig()
    rig._arm = arm = hardware.require_arm.return_value
    rig._cam = hardware.require_cam.return_value
    rig._origin_pinned = True
    rig.calibration.cam_rotation = -1
    rig.calibration.cam_size = (80, 160)
    rig.calibration.pct_to_grbl = np.diag([50.0, 100.0, 1.0])[:2]
    rig.calibration.pct_to_cam = np.eye(2, 3)
    rig.calibration.viewport_shift = hardware.calibration.viewport_shift
    arm.SWIPE_SPEEDS = {"medium": 6000, "fast": 10000}
    arm.RAPID_SPEED = DEFAULT_RAPID_SPEED
    rig.mark_ready()
    return MidsceneDevice(phone), arm


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        ("primitives/homeButton", {}),
        ("actions/forceQuit", {}),
        ("actions/unlockPhone", {}),
        ("actions/sendToClipboard", {"text": "hello"}),
    ],
)
async def test_queued_action_rechecks_readiness(
    physical_device, monkeypatch, endpoint, payload
):
    device, arm = physical_device
    rig = device.rig
    monkeypatch.setattr(assembly, "physiclaw", PhysiClaw(rig=rig))
    control, _ = assembly.build_apps(midscene_token="test-token")
    loop = asyncio.get_running_loop()
    waiting = asyncio.Event()
    acquire = rig.acquire

    def signal_then_acquire(wait_seconds=0):
        loop.call_soon_threadsafe(waiting.set)
        acquire(wait_seconds)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://127.0.0.1"
    ) as client:
        with rig.locked():
            monkeypatch.setattr(rig, "acquire", signal_then_acquire)
            request = asyncio.create_task(
                client.post(f"{PREFIX}/{endpoint}", json=payload, headers=AUTH)
            )
            await asyncio.wait_for(waiting.wait(), timeout=2)
            rig.unmark_ready()
        response = await request
    assert response.status_code == 503
    assert response.json()["error"] == "Complete PhysiClaw hardware setup first"
    assert arm.method_calls == []


@pytest.mark.parametrize("duration", [None, 0, 10000])
def test_invalid_touch_requests_leave_arm_stationary(physical_device, duration):
    device, arm = physical_device
    with pytest.raises(ValueError, match="outside"):
        device.act("tap", {"point": {"x": 80, "y": 0}})
    device.rig.assistive_touch.at_screen = (0.75, 0.75)
    device.rig.assistive_touch.at_radius_screen = (0.05, 0.05)
    with pytest.raises(ValueError, match="overlaps"):
        device.act("tap", {"point": {"x": 60, "y": 120}})
    args = {"from": {"x": 0, "y": 0}, "to": {"x": 40, "y": 80}, "repeat": 100}
    if duration is not None:
        args["duration"] = duration
    with pytest.raises(ValueError, match="budget"):
        device.act("swipe", args)
    device.screenshot()
    device.rig.calibration.pct_to_grbl[0, 2] = 1
    with pytest.raises(RuntimeError, match="Calibration changed"):
        device.act("tap", {"point": {"x": 20, "y": 50}})
    assert arm.method_calls == []
    assert device.after_action == 0


@pytest.mark.parametrize(
    ("name", "options"),
    [("swipe", {}), ("swipe", {"duration": 50}), ("dragAndDrop", {})],
)
def test_zero_distance_leaves_arm_stationary(physical_device, name, options):
    device, arm = physical_device
    point = {"x": 20, "y": 50}
    with pytest.raises(ValueError, match="endpoints must differ"):
        device.act(name, {"from": point, "to": point, **options})
    assert arm.method_calls == []


@pytest.mark.parametrize("fail", [False, True])
def test_point_motion_parks_before_unlock_even_on_io_failure(physical_device, fail):
    device, arm = physical_device

    def verify_lock(*args):
        device.rig.assert_locked()

    arm.rapid_to.side_effect = verify_lock
    point = {"point": {"x": 40, "y": 80}}
    if fail:
        arm.tap.side_effect = OSError("lost connection")
        with pytest.raises(OSError, match="lost connection"):
            device.act("tap", point)
    else:
        device.act("tap", point)
    assert [c.args for c in arm.rapid_to.call_args_list] == [(25, 50), (-5, -5)]
    assert device.after_action > 0


def test_slow_swipe_stops_between_repetitions_and_parks(physical_device, mocker):
    device, arm = physical_device
    clock = mocker.patch("physiclaw.core.orchestration.midscene_device.time")
    clock.monotonic.side_effect = [10, 40]
    with pytest.raises(RuntimeError, match="after 1 of 2"):
        device.act(
            "swipe", {"from": {"x": 0, "y": 0}, "to": {"x": 40, "y": 80}, "repeat": 2}
        )
    arm.swipe_to.assert_called_once()
    arm.rapid_to.assert_called_with(-5, -5)


@pytest.mark.asyncio
async def test_screenshot_maps_calibration_errors_to_503(device):
    phone, client = device
    phone.rig.require_transforms.return_value.pct_to_cam = np.array(
        [[1.0, 0.0, 0.2], [0.0, 1.0, 0.0]]
    )
    async with client:
        res = await client.get(f"{PREFIX}/screenshot", headers=AUTH)
    assert res.status_code == 503
    assert res.json()["code"] == "DEVICE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_midscene_lifecycle_runs_without_omniparser_or_observation(
    monkeypatch, mocker
):
    assert not paths.omniparser_onnx().exists()

    rig = HardwareRig()
    phone = mock_phone()
    hardware = phone.rig
    rig._arm = arm = hardware.require_arm.return_value
    rig._cam = cam = hardware.require_cam.return_value
    rig._bridge = MagicMock()
    rig._origin_pinned = True
    rig.calibration.cam_index = 0
    rig.calibration.cam_rotation = -1
    rig.calibration.cam_size = (80, 160)
    rig.calibration.pct_to_grbl = np.diag([50.0, 100.0, 1.0])[:2]
    rig.calibration.pct_to_cam = np.eye(2, 3)
    rig.calibration.viewport_shift = ViewportShift(
        offset_x=0.0,
        offset_y=0.0,
        dpr=1.0,
        screenshot_width=80,
        screenshot_height=160,
    )
    rig.calibration.screen_dimension = {"viewport_width": 80, "viewport_height": 160}
    assert rig.calibration.complete
    arm.SWIPE_SPEEDS = {"slow": 3000, "medium": 6000, "fast": 10000}
    arm.RAPID_SPEED = DEFAULT_RAPID_SPEED
    cam.peek.return_value = np.zeros((160, 80, 3), np.uint8)

    pl = PhysiClaw(rig=rig)
    icon_spy = mocker.patch("physiclaw.core.orchestration.perception.IconDetector")
    observe_spy = mocker.spy(pl._observer, "with_view")

    # 1. Setup completion /api/phone/home works without OmniParser or observation.
    class _FakeMcp:
        def __init__(self):
            self.routes = {}
            self.tools = {}

        def custom_route(self, path, methods):
            def deco(fn):
                self.routes[(path, methods[0])] = fn
                return fn

            return deco

        def tool(self, **kwargs):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn

            return deco

    fake_mcp = _FakeMcp()
    completion_routes.register(fake_mcp, pl)
    tools_mod.register(fake_mcp, pl)
    resp = await fake_mcp.routes[("/api/phone/home", "POST")](MagicMock())
    assert resp.status_code == 200

    # 2. Verified warm-start resume works without OmniParser or observation.
    mocker.patch(
        "physiclaw.core.calibration.state.Calibration.load",
        return_value=rig.calibration,
    )
    mocker.patch.object(rig, "connect_arm")
    mocker.patch.object(rig, "connect_camera")
    mocker.patch.object(rig, "restore_park_origin")
    mocker.patch.object(warm_start, "_sanity", return_value=True)
    mocker.patch.object(warm_start.sys.stdin, "isatty", return_value=False)
    mocker.patch.object(pl.perception, "tune_now")
    assert warm_start.try_resume(
        None, verify=True, physiclaw=pl, calib=MagicMock(), phone=MagicMock()
    )

    # 3. Connect screenshot & system primitives/actions run without OmniParser/observation.
    monkeypatch.setattr(assembly, "physiclaw", pl)
    control, _ = assembly.build_apps(midscene_token="test-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://127.0.0.1"
    ) as client:
        assert (
            await client.get(f"{PREFIX}/screenshot", headers=AUTH)
        ).status_code == 200
        for kind, name in (
            ("primitives", "homeButton"),
            ("primitives", "backButton"),
            ("primitives", "recentAppsButton"),
            ("actions", "forceQuit"),
        ):
            assert (
                await client.post(f"{PREFIX}/{kind}/{name}", json={}, headers=AUTH)
            ).status_code == 200

    icon_spy.assert_not_called()
    observe_spy.assert_not_called()
    assert arm.rapid_to.call_args_list[-1].args == (-5, -5)

    # 4. MCP peek remains available and surfaces the missing OmniParser setup command.
    mocker.stop(icon_spy)
    mocker.patch.object(
        pl._observer,
        "peek_frame",
        return_value=MagicMock(
            frame=np.zeros((160, 80, 3), np.uint8),
            report=MagicMock(),
            retuned=False,
            observed_after=1.0,
        ),
    )
    mocker.patch.object(pl._observer, "observe_quality", return_value=None)
    with pytest.raises(ToolError, match="physiclaw setup local-vision-model"):
        await fake_mcp.tools["peek"]()
