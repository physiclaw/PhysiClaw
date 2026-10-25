"""The replay must test current production context without changing evidence."""

import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from physiclaw.contract.dto import AssistantMessage, FinishReason, ToolCall

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "eval_context.py"
spec = importlib.util.spec_from_file_location("eval_context", SCRIPT)
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def request_fixture():
    return {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "Exact system"},
            {"role": "user", "content": "Exact plan, history and memory"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {"type": "object"},
                },
            }
            for name, description in [
                ("note", "Exact description"),
                ("peek", "Observe"),
            ]
        ],
        "tool_choice": "auto",
    }


def write_case(tmp_path, before, after):
    case = {"provider": "deepseek", "session": str(tmp_path), "turn": 1}
    for variant, payload in [("before", before), ("after", after)]:
        path = tmp_path / f"{variant}.json"
        path.write_text(json.dumps(payload))
        case[variant] = str(path)
    return case


def test_explicit_requests_preserve_every_message_and_tool_byte(tmp_path):
    from physiclaw.provider.chat.wire import tool_to_wire

    before, after = request_fixture(), request_fixture()
    after["messages"][0]["content"] = "Current system, supplied explicitly"
    after["tools"][0]["function"]["description"] = "Current tool"
    case = write_case(tmp_path, before, after)
    model, session, variants = replay.load_case(case)
    assert model == "test-model"
    assert session == tmp_path
    for (_, messages, schemas), expected in zip(variants, [before, after], strict=True):
        assert messages == expected["messages"]
        assert [tool_to_wire(schema) for schema in schemas] == expected["tools"]


@pytest.mark.parametrize("change", ["model", "parameter", "choice", "tool", "messages"])
def test_unreplayable_requests_fail_before_any_api_call(tmp_path, change):
    before, after = request_fixture(), request_fixture()
    if change == "model":
        after["model"] = "another-model"
    elif change == "parameter":
        after["temperature"] = 0.2
    elif change == "choice":
        after["tool_choice"] = "required"
    elif change == "tool":
        after["tools"][0]["function"]["strict"] = True
    else:
        after["messages"] = []
    with pytest.raises(ValueError):
        replay.load_case(write_case(tmp_path, before, after))


@pytest.mark.asyncio
async def test_evaluate_sends_explicit_payloads_without_prompt_rewrites(
    tmp_path, monkeypatch, respx_mock
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-only")
    before, after = request_fixture(), request_fixture()
    after["messages"][0]["content"] = "Revised system"
    case = write_case(tmp_path, before, after)
    case["actions"] = [{"tool": "peek"}]
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([case]))
    route = respx_mock.post("https://api.deepseek.com/v1/chat/completions").respond(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "n",
                                "type": "function",
                                "function": {"name": "note", "arguments": "{}"},
                            },
                            {
                                "id": "a",
                                "type": "function",
                                "function": {"name": "peek", "arguments": "{}"},
                            },
                        ],
                    },
                }
            ]
        },
    )
    args = SimpleNamespace(
        cases=cases, output=tmp_path / "results.json", repeats=1, concurrency=1
    )
    assert await replay.evaluate(args) == 0
    assert [json.loads(call.request.content) for call in route.calls] == [before, after]


def test_restore_keeps_image_bytes_exact_and_confines_reads(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    data = b"synthetic image bytes"
    (images / "screen.jpg").write_bytes(data)
    block = {"type": "image_url", "image_url": {"url": "images/screen.jpg"}}
    result = replay.restore_images(block, tmp_path)
    assert base64.b64decode(result["image_url"]["url"].split(",", 1)[1]) == data
    assert block["image_url"]["url"] == "images/screen.jpg"
    with pytest.raises(ValueError, match="outside"):
        replay.restore_images(
            {"type": "image_url", "image_url": {"url": "../secret.jpg"}}, tmp_path
        )


def test_grading_accepts_only_grounded_alternatives():
    case = {
        "actions": [
            {"tool": "tap", "args": {"bbox": [0.1, 0.2, 0.3, 0.4]}},
            {"tool": "peek"},
        ],
    }
    schemas = [
        {"name": name, "input_schema": {"type": "object"}}
        for name in ("note", "tap", "peek")
    ]

    def errors(name, arguments):
        reply = AssistantMessage(
            "",
            [
                ToolCall("n", "note", {"summary": "Check"}),
                ToolCall("a", name, arguments),
            ],
            FinishReason.TOOL_CALLS,
        )
        return replay.grade(reply, case, schemas)

    assert not errors("tap", {"bbox": [0.1, 0.2, 0.3, 0.4]})
    assert not errors("peek", {})
    assert errors("tap", {"bbox": [0.11, 0.2, 0.3, 0.4]})
    assert errors("tap", {})


@pytest.mark.parametrize(
    "action,args",
    [
        ("tap", {"bbox": [0.0, 0.0, 0.5, 0.5]}),
        ("sequence", {"actions": [{"tool_name": "tap", "arg": [0.0, 0.0, 0.5, 0.5]}]}),
    ],
)
def test_grading_rejects_forbidden_box_regardless_of_numeric_spelling(action, args):
    reply = AssistantMessage(
        "",
        [ToolCall("n", "note", {}), ToolCall("a", action, args)],
        FinishReason.TOOL_CALLS,
    )
    schemas = [
        {"name": name, "input_schema": {"type": "object"}} for name in ["note", action]
    ]
    case = {"actions": [{"tool": action}], "forbidden_bboxes": [[0, 0, 0.5, 0.5]]}
    assert "unsafe target" in replay.grade(reply, case, schemas)
    assert not replay.contains_box(
        {"text": "quoted [0, 0, 0.5, 0.5]"}, [0, 0, 0.5, 0.5]
    )


def test_grading_keeps_invalid_scratchpad_as_a_model_failure():
    reply = AssistantMessage(
        "",
        [ToolCall("n", "note", {"scratchpad": 42}), ToolCall("a", "peek", {})],
        FinishReason.TOOL_CALLS,
    )
    schemas = [
        {"name": name, "input_schema": {"type": "object"}} for name in ["note", "peek"]
    ]
    assert "missing checkpoint scratchpad" in replay.grade(
        reply, {"actions": [{"tool": "peek"}], "scratchpad_required": True}, schemas
    )


@pytest.mark.parametrize(
    "finish,ident",
    [
        (FinishReason.CONTENT_FILTER, "a"),
        (FinishReason.ERROR, "a"),
        (FinishReason.TOOL_CALLS, "n"),
    ],
)
def test_grading_rejects_unusable_protocol_results(finish, ident):
    reply = AssistantMessage(
        "", [ToolCall("n", "note", {}), ToolCall(ident, "peek", {})], finish
    )
    assert replay.grade(reply, {"actions": [{"tool": "peek"}]}, [])
