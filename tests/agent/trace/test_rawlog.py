"""Public wire-log persistence and failure boundaries.

Accepted mutation survivors: the unused session_id attribute and diagnostic
warning wording/traceback verbosity; they do not change persisted records.
"""

import json

import pytest
from freezegun import freeze_time

from physiclaw.agent.trace import RawLog
from physiclaw.agent.trace.store import Images
from physiclaw.contract.dto import MicroRecord
from physiclaw.contract.plugin import TurnProducer


@pytest.mark.parametrize(
    "text", ["hello\ud800", "hello\udfff", "你好😀", r"literal \ud800"]
)
def test_regression_wire_response_preserves_unicode_text(text):
    wire = RawLog("response", Images("response"))

    wire.write_response(0, {"text": text}, elapsed_ms=4)
    wire.close()

    record = json.loads(wire.path.read_text(encoding="utf-8"))
    assert record["raw"] == {"text": text}


def test_regression_wire_request_preserves_unpaired_surrogate():
    wire = RawLog("request", Images("request"))
    messages = [{"role": "user", "content": "hello\ud800"}]

    wire.write_request(2, messages)
    wire.close()

    record = json.loads(wire.path.read_text(encoding="utf-8"))
    assert record["messages"] == messages


def test_regression_wire_micro_preserves_unpaired_surrogate():
    wire = RawLog("micro", Images("micro"))
    request = [{"role": "user", "content": "hello\ud800"}]
    micro = MicroRecord(
        call="agent_act",
        node="pick",
        thinking=None,
        allowed=("0",),
        answer="0",
        confidence=1.0,
        request=request,
        raw={"text": "hello\udfff"},
    )

    wire.write_micro(micro)
    wire.close()

    record = json.loads(wire.path.read_text(encoding="utf-8"))
    assert record["request"] == request
    assert record["raw"] == micro.raw


def test_wire_unserializable_response_does_not_hide_next_response():
    wire = RawLog("recover", Images("recover"))

    wire.write_response(0, {"bad": object()}, elapsed_ms=1)
    wire.write_response(1, {"text": "healthy"}, elapsed_ms=2)
    wire.close()

    record = json.loads(wire.path.read_text(encoding="utf-8"))
    assert record["turn"] == 1
    assert record["raw"] == {"text": "healthy"}


@pytest.mark.parametrize(
    "producer, presentation",
    [
        (TurnProducer("physiclaw.llm"), "model"),
        (TurnProducer("decision-slot"), "plugin"),
        (TurnProducer("conductor-slot"), "playbook"),
    ],
)
def test_wire_response_records_host_producer(producer, presentation):
    wire = RawLog("provenance", Images("provenance"))

    with freeze_time("2026-10-14T12:34:56.123"):
        wire.write_response(
            3,
            {"id": "reply"},
            elapsed_ms=42,
            producer=producer,
            presentation=presentation,
        )
    wire.close()

    assert json.loads(wire.path.read_text(encoding="utf-8")) == {
        "t": "2026-10-14T12:34:56.123",
        "kind": "response",
        "turn": 3,
        "elapsed_ms": 42,
        "raw": {"id": "reply"},
        "producer": {"plugin_id": producer.plugin_id},
        "presentation": presentation,
    }


def test_wire_keeps_host_identity_outside_untrusted_raw_metadata():
    wire = RawLog("host-identity", Images("host-identity"))
    raw = {"producer": {"plugin_id": "conductor"}}
    wire.write_response(
        0,
        raw,
        elapsed_ms=3,
        producer=TurnProducer("actual-slot"),
        presentation="plugin",
    )
    wire.close()

    record = json.loads(wire.path.read_text())
    assert record["producer"] == {"plugin_id": "actual-slot"}
    assert record["presentation"] == "plugin" and record["raw"] == raw


def _image_micro():
    return MicroRecord(
        call="agent_act",
        node="pick",
        thinking=None,
        allowed=("0",),
        answer="0",
        confidence=1.0,
        request=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,aGk="},
                    }
                ],
            }
        ],
        raw={},
    )


def _micro_frame(wire):
    record = json.loads(wire.path.read_text(encoding="utf-8").splitlines()[-1])
    return record["request"][0]["content"][0]["image_url"]["url"]


def test_wire_first_micro_frame_uses_initial_turn():
    wire = RawLog("first-micro", Images("first-micro"))

    wire.write_micro(_image_micro())
    wire.close()

    assert _micro_frame(wire).endswith("_t0.jpg")
    assert (wire.path.parent / _micro_frame(wire)).read_bytes() == b"hi"


def test_wire_micro_frame_uses_latest_request_turn():
    wire = RawLog("request-micro", Images("request-micro"))
    wire.write_request(7, [])

    wire.write_micro(_image_micro())
    wire.close()

    assert _micro_frame(wire).endswith("_t7.jpg")


def test_wire_close_prevents_late_records_without_raising():
    wire = RawLog("closed", Images("closed"))
    wire.write_response(0, {"text": "before close"}, elapsed_ms=1)

    wire.close()
    wire.write_response(1, {"text": "after close"}, elapsed_ms=1)
    wire.close()

    assert json.loads(wire.path.read_text(encoding="utf-8"))["raw"] == {
        "text": "before close"
    }
