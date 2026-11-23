"""Check the scripted provider contract with real fixture images."""

import base64
import io
import json

import cv2
import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from physiclaw.agent import layout
from physiclaw.agent.engine import skill
from physiclaw.common.listing import Element, format_elements
from physiclaw.core.hardware import arm as arm_module
from physiclaw.core.orchestration.rig import HardwareRig
from physiclaw.core.vision.util import encode_view_jpeg
from physiclaw.dryrun import DAILY_ACTIONS, KEY, MODEL, DryRunError
from physiclaw.dryrun.devices import SerialDevice
from physiclaw.dryrun.runner import _seed_learned_layout
from physiclaw.dryrun.scenario import (
    JOB_DESCRIPTION,
    LOG_ENTRY,
    MESSAGE,
    PARKED,
    REPLY,
    SCREEN_MM,
    SCREEN_ORIGIN,
    SCREEN_SIZE,
    ModelStub,
    Phone,
    _tool_receipt,
    calibration,
)

_BOX = (0.25, 0.55, 0.75, 0.65)


@pytest.fixture
async def provider():
    _seed_learned_layout()
    skills = layout.fill_builtin_boxes(skill.discover_builtin_skills())
    system = {"role": "system", "content": [_text(skill.render_builtin(skills))]}
    model = ModelStub()
    app = Starlette(routes=[Route("/v1/responses", model.respond, methods=["POST"])])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers={"Authorization": f"Bearer {KEY}"},
    ) as client:

        async def reply(*content):
            return await client.post(
                "/v1/responses",
                json={
                    "model": MODEL,
                    "text": {"format": {"name": "turn_output"}},
                    "input": [system, {"role": "user", "content": list(content)}],
                },
            )

        yield reply, model


def _wake(source="phone", description="notification"):
    return _text(f"[Current wake — act on this]\n[2026-11-23] {source}: {description}")


def _text(text):
    return {"type": "input_text", "text": text}


def _receipt(tool, *details):
    return [
        _text(text)
        for text in (
            "Structured output result:",
            f"{tool}: ok",
            "executed: True",
            *details,
        )
    ]


def _view(phone, *labels):
    listing = format_elements(
        Element(i, "text", label, _BOX, 1.0) for i, label in enumerate(labels)
    )
    frame = cv2.rotate(phone.frame(), cv2.ROTATE_90_CLOCKWISE)
    x, y = SCREEN_ORIGIN
    width, height = SCREEN_SIZE
    jpeg = encode_view_jpeg(frame[y : y + height, x : x + width])
    return [
        _text(listing),
        {
            "type": "input_image",
            "image_url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode(),
        },
    ]


async def test_provider_follows_the_lamp_workflow(provider):
    reply, model = provider
    phone = Phone()
    phone.notify()
    steps = [
        ("peek", "notification", [_wake()], ()),
        ("tap", "notification", _receipt("peek"), ("Open message",)),
        ("update_progress", "message", _receipt("tap"), MESSAGE),
        ("tap", "message", _receipt("update_progress"), ("Open controls",)),
        ("create_job", "controls", _receipt("tap"), ()),
        (
            "sequence",
            "controls",
            _receipt("create_job"),
            ("Power off", "Warm", "Brightness"),
        ),
        (
            "append_log",
            "done",
            _receipt("sequence"),
            ("Power on", "Warm selected", "Low brightness"),
        ),
        ("read_logs", "done", _receipt("append_log"), ()),
        ("tap", "done", _receipt("read_logs", LOG_ENTRY), ("Reply to Sam",)),
        ("sequence", "chat", _receipt("tap"), ("Sam", "Message")),
        ("go_back", "sent", _receipt("sequence"), ("Sam", REPLY)),
        ("home_screen", "chats", _receipt("go_back"), ("Chats",)),
        ("update_progress", "home", _receipt("home_screen"), ("Desk lamp",)),
        ("end_session", "done", _receipt("update_progress"), ()),
    ]

    for expected, screen, receipt, labels in steps:
        phone.screen = screen
        response = await reply(*receipt, *(_view(phone, *labels) if labels else []))
        assert response.status_code == 200, response.text
        action = json.loads(response.json()["output"][0]["content"][0]["text"])[
            "action"
        ]
        assert list(action) == [expected]

    assert action["end_session"]["status"] == "DONE"
    assert model.turns == len(steps)
    extra = await reply()
    assert extra.status_code == 422
    assert "extra request" in extra.json()["error"]


@pytest.mark.parametrize(
    ("screen", "labels", "error"),
    [
        ("message", ("Open message",), "did not show the notification"),
        ("notification", ("Sam",), "Expected 'Open message'"),
    ],
)
async def test_provider_rejects_inconsistent_peek(provider, screen, labels, error):
    reply, model = provider
    phone = Phone()
    phone.screen = screen
    assert (await reply(_wake())).status_code == 200

    response = await reply(*_receipt("peek"), *_view(phone, *labels))

    assert response.status_code == 422
    assert error in response.json()["error"]
    assert model.turns == 1


@pytest.mark.parametrize("wake", ["phone", "cron:other-job"])
async def test_daily_session_rejects_unrelated_wake(provider, wake):
    reply, model = provider
    model.actions = DAILY_ACTIONS
    response = await reply(_wake(wake, JOB_DESCRIPTION))
    assert response.status_code == 422
    assert "wake context" in response.json()["error"]


def test_chat_reply_comes_from_the_fetched_and_pasted_text():
    phone = Phone(lambda: "Wrong reply")
    phone.screen = "chat"

    def press(x, y, duration):
        point = complex(
            x * SCREEN_MM[0] / SCREEN_SIZE[0], y * SCREEN_MM[1] / SCREEN_SIZE[1]
        )
        phone.touch(point, point, duration)

    press(180, 760, 0.1)
    press(39, 260, 1.2)
    press(170, 500, 1.2)
    press(90, 450, 0.1)
    press(360, 730, 0.1)
    assert phone.failure is None and phone.screen == "sent"
    assert phone.sent_reply == "Wrong reply"
    assert phone.sent_reply != REPLY


@pytest.mark.parametrize(
    ("header", "status", "execution"),
    [
        (
            "Structured output result (error):",
            "update_progress: error",
            "executed: True",
        ),
        ("Structured output result:", "update_progress: ok", "executed: False"),
        ("Structured output result:", "append_log: ok", "executed: True"),
    ],
)
def test_model_requires_latest_expected_action_to_succeed(header, status, execution):
    earlier = {
        "role": "user",
        "content": _receipt("update_progress", "progress updated"),
    }
    latest = {
        "role": "user",
        "content": [_text(line) for line in (header, status, execution)],
    }

    with pytest.raises(DryRunError, match="successful update_progress receipt"):
        _tool_receipt({"input": [earlier, latest]}, "update_progress")


@pytest.mark.parametrize("x_scale, expected", [(68, "message"), (120, "notification")])
def test_virtual_touch_depends_on_real_arm_mapping(monkeypatch, x_scale, expected):
    monkeypatch.setattr(arm_module.time, "sleep", lambda _seconds: None)
    phone = Phone()
    phone.notify()
    controller = SerialDevice(PARKED, phone.touch, io.StringIO())
    rig = HardwareRig(serial_backend=controller)
    rig.calibration = calibration()
    assert rig.calibration.pct_to_grbl is not None
    rig.calibration.pct_to_grbl[0, 0] = x_scale

    try:
        with rig.locked():
            rig.connect_arm()
            assert rig.restore_park_origin()
            rig.move_to_bbox_center([0.4, 0.60, 0.6, 0.63])
            rig.require_arm().tap()
    finally:
        rig.shutdown()

    assert phone.screen == expected
    assert (phone.failure is not None) == (expected == "notification")
