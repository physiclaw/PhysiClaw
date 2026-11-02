"""The live test harness must validate recorded evidence before advancing."""

import base64
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from physiclaw.contract.observation import read_observation


@pytest.fixture
def replay():
    path = Path(__file__).parents[1] / "support/live_replay.py"
    return SimpleNamespace(**runpy.run_path(str(path)))


@pytest.fixture
def recording(tmp_path, observation_blocks):
    blocks = observation_blocks()
    event = {
        "event": "tool_result",
        "name": "peek",
        "arguments": {},
        "text": "Recorded action result\n" + blocks[1]["text"],
        "images": ["frame.jpg"],
    }
    (tmp_path / "frame.jpg").write_bytes(base64.b64decode(blocks[0]["data"]))
    (tmp_path / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    return tmp_path, event


async def test_recorded_observation_preserves_atomic_image_listing(replay, recording):
    path, event = recording
    device = replay.ReplayDevice(path, [{"name": "peek"}])

    blocks = await device.call_tool("peek", {})
    view = read_observation(blocks)

    assert view.source == "debug" and view.ocr[0].label == "搜索"
    assert blocks[0]["text"] == "Recorded action result\n"
    assert device.index == 1 and device.current == event


async def test_invalid_recorded_image_does_not_complete_transition(replay, recording):
    path, _ = recording
    (path / "frame.jpg").unlink()
    device = replay.ReplayDevice(path, [{"name": "peek"}])

    with pytest.raises(FileNotFoundError):
        await device.call_tool("peek", {})

    assert device.index == 0 and device.current is None


@pytest.mark.parametrize("schemas", [[], [{"name": "tap"}]])
def test_empty_replay_is_rejected(replay, recording, schemas):
    path, _ = recording

    with pytest.raises(ValueError, match="no matching device transitions"):
        replay.ReplayDevice(path, schemas)


@pytest.mark.parametrize("images", [[], ["frame.jpg", "frame.jpg"], ["../outside.jpg"]])
def test_invalid_image_references_are_rejected(replay, recording, images):
    path, event = recording
    device = replay.ReplayDevice(path, [{"name": "peek"}])

    with pytest.raises(ValueError, match="exactly one image|escapes session"):
        device.blocks({**event, "images": images})

    assert device.index == 0


async def test_unrecorded_action_cannot_advance_device(replay, recording):
    path, _ = recording
    device = replay.ReplayDevice(path, [{"name": "peek"}])

    with pytest.raises(ValueError, match="no matching transition"):
        await device.call_tool("tap", {"bbox": [0, 0, 1, 1]})

    assert device.index == 0 and device.current is None


def _exchange(call_id):
    return [
        {
            "role": "assistant",
            "tool_calls": [{"id": call_id, "function": {"name": "submit_turn"}}],
        },
        {"role": "tool", "tool_call_id": call_id, "content": "observed"},
    ]


@pytest.mark.parametrize(
    "history,valid",
    [
        (_exchange("one"), True),
        (_exchange("one") + _exchange("two"), True),
        (_exchange("one") + _exchange("one"), False),
        (_exchange(""), False),
        (_exchange(None), False),
        (_exchange("one")[:1], False),
        (_exchange("one")[1:], False),
    ],
)
def test_request_audit_requires_unique_paired_envelopes(replay, history, valid):
    body = {"tools": [{"function": {"name": "submit_turn"}}], "messages": history}

    audit = replay.audit_requests([body])

    assert audit["valid"] is valid
    assert bool(audit["violations"]) is not valid
