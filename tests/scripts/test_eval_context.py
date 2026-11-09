"""The replay must test current production context without changing evidence."""

import asyncio
import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from physiclaw.agent.engine.output import ActionCatalog
from physiclaw.contract.dto import AssistantMessage, FinishReason, ToolCall
from physiclaw.provider.chat.openai_compat import OpenAICompatibleProvider
from physiclaw.provider.chat.structured import decode_completion
from tests.support.provider_responses import response

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "eval_context.py"
spec = importlib.util.spec_from_file_location("eval_context", SCRIPT)
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def catalog_fixture():
    return ActionCatalog.compile(
        [
            {
                "name": "peek",
                "description": "Observe",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
            {
                "name": "tap",
                "description": "Press",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "bbox": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4,
                        }
                    },
                    "required": ["bbox"],
                    "additionalProperties": False,
                },
            },
        ],
        [],
        max_scratchpad=100,
    )


def turn_fixture():
    return {"summary": "Check", "scratchpad": None, "action": {"peek": {}}}


def completion_fixture(value=None, *, finish=FinishReason.TOOL_CALLS, calls=None):
    return decode_completion(
        AssistantMessage(
            "",
            calls
            if calls is not None
            else [
                ToolCall(
                    "s", "submit_turn", value if value is not None else turn_fixture()
                ),
            ],
            finish,
        ),
        catalog_fixture().spec(),
    )


def request_fixture(provider="deepseek"):
    schema = catalog_fixture().schema
    messages = [
        {"role": "system", "content": "Exact system — 中文 🦀"},
        {"role": "user", "content": "Exact plan, history and memory"},
    ]
    if provider == "openai":
        from physiclaw.provider.chat.schema import strict_output_schema

        return {
            "model": "test-model",
            "input": messages,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "turn_output",
                    "strict": True,
                    "schema": strict_output_schema(schema),
                }
            },
            "reasoning": {"effort": "low"},
            "max_output_tokens": 1024,
            "prompt_cache_options": {"mode": "explicit"},
        }
    if provider == "anthropic":
        return {
            "model": "test-model",
            "messages": messages[1:],
            "system": [
                {
                    "type": "text",
                    "text": messages[0]["content"],
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "tools": [
                {
                    "name": "submit_turn",
                    "description": "Exact description",
                    "input_schema": schema,
                }
            ],
            "max_tokens": 1024,
            "tool_choice": {"type": "tool", "name": "submit_turn"},
        }
    return {
        "model": "test-model",
        "messages": messages,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "submit_turn",
                    "description": "Exact description",
                    "parameters": schema,
                },
            }
        ],
        "tool_choice": "required",
        "temperature": 0.2,
        "max_tokens": 1024,
    }


def write_case(tmp_path, before, after, *, provider="deepseek", catalog=None):
    case = {"provider": provider, "session": str(tmp_path), "turn": 1}
    (tmp_path / "model.jsonl").write_text(
        json.dumps(
            {
                "kind": "session_start",
                "output_schema": (catalog or catalog_fixture()).schema,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    for variant, payload in [("before", before), ("after", after)]:
        path = tmp_path / f"{variant}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        case[variant] = str(path)
    return case


def evaluation_args(tmp_path):
    case = write_case(tmp_path, request_fixture(), request_fixture())
    case["actions"] = [{"tool": "peek"}]
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([case]), encoding="utf-8")
    return SimpleNamespace(
        cases=cases, output=tmp_path / "results.json", repeats=1, concurrency=1
    )


class BoundaryProvider(OpenAICompatibleProvider):
    """Exercise evaluation lifecycle without constructing a network client."""

    def __init__(self, *, chat_error=None, close_error=None):
        self.chat_error = chat_error
        self.close_error = close_error
        self.close_calls = 0

    async def generate_structured(self, history, output_spec):
        if self.chat_error is not None:
            raise self.chat_error
        return completion_fixture()

    async def aclose(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


async def test_evaluate_records_factory_failure_and_continues_other_variants(
    tmp_path, mocker
):
    args = evaluation_args(tmp_path)
    healthy = BoundaryProvider()
    factory = mocker.patch(
        "physiclaw.provider.make_chat_provider",
        side_effect=[RuntimeError("cannot construct"), healthy],
    )

    result = await replay.evaluate(args)

    rows = json.loads(args.output.read_text(encoding="utf-8"))["results"]
    assert result == 1
    assert [row["variant"] for row in rows] == ["before", "after"]
    assert rows[0]["error"] == "RuntimeError"
    assert rows[0]["seconds"] >= 0
    assert rows[1]["failures"] == [] and "error" not in rows[1]
    assert factory.call_count == 2 and healthy.close_calls == 1


@pytest.mark.parametrize(
    "chat_error, expected_error, keeps_reply",
    [(None, "OSError", True), (TimeoutError("chat failed"), "TimeoutError", False)],
    ids=["cleanup-only", "primary-and-cleanup"],
)
async def test_evaluate_records_cleanup_failure_without_losing_other_results(
    tmp_path, mocker, chat_error, expected_error, keeps_reply
):
    args = evaluation_args(tmp_path)
    failed = BoundaryProvider(
        chat_error=chat_error, close_error=OSError("close failed")
    )
    healthy = BoundaryProvider()
    mocker.patch("physiclaw.provider.make_chat_provider", side_effect=[failed, healthy])

    result = await replay.evaluate(args)

    rows = json.loads(args.output.read_text(encoding="utf-8"))["results"]
    assert result == 1
    assert [row["variant"] for row in rows] == ["before", "after"]
    assert rows[0]["error"] == expected_error
    assert rows[0]["cleanup_error"] == "OSError"
    assert ("calls" in rows[0]) is keeps_reply
    assert rows[1]["failures"] == [] and "error" not in rows[1]
    assert failed.close_calls == healthy.close_calls == 1


@pytest.mark.parametrize(
    "chat_error, close_error",
    [
        (asyncio.CancelledError(), OSError("cleanup after cancellation")),
        (None, asyncio.CancelledError()),
    ],
    ids=["chat-cancelled-cleanup-failed", "cleanup-cancelled"],
)
async def test_evaluate_propagates_cancellation_from_chat_or_cleanup(
    tmp_path, mocker, chat_error, close_error
):
    args = evaluation_args(tmp_path)
    args.concurrency = 2
    first = BoundaryProvider(chat_error=chat_error, close_error=close_error)
    second = BoundaryProvider(chat_error=chat_error, close_error=close_error)
    mocker.patch("physiclaw.provider.make_chat_provider", side_effect=[first, second])

    with pytest.raises(asyncio.CancelledError):
        await replay.evaluate(args)

    assert first.close_calls == second.close_calls == 1
    assert not args.output.exists()


async def test_evaluate_propagates_factory_cancellation(tmp_path, mocker):
    args = evaluation_args(tmp_path)
    mocker.patch(
        "physiclaw.provider.make_chat_provider", side_effect=asyncio.CancelledError
    )

    with pytest.raises(asyncio.CancelledError):
        await replay.evaluate(args)

    assert not args.output.exists()


@pytest.mark.parametrize("provider", ["deepseek", "openai", "anthropic"])
def test_explicit_requests_preserve_every_field(tmp_path, provider):
    before, after = request_fixture(provider), request_fixture(provider)
    field = "input" if provider == "openai" else "messages"
    after[field][-1]["content"] = "Current prompt, supplied explicitly"
    case = write_case(tmp_path, before, after, provider=provider)
    model, session, catalog, variants = replay.load_case(case)
    assert model == "test-model" and session == tmp_path
    assert catalog.schema == catalog_fixture().schema
    assert variants == [("before", before), ("after", after)]


@pytest.mark.parametrize("change", ["model", "old_tools", "messages", "wrong_endpoint"])
async def test_unreplayable_requests_fail_before_any_api_call(tmp_path, mocker, change):
    args = evaluation_args(tmp_path)
    after = request_fixture()
    if change == "model":
        after["model"] = "another-model"
    elif change == "old_tools":
        after["tools"][0]["function"]["name"] = "note"
    elif change == "wrong_endpoint":
        after["input"] = after.pop("messages")
    else:
        after["messages"] = []
    (tmp_path / "after.json").write_text(json.dumps(after), encoding="utf-8")
    factory = mocker.patch("physiclaw.provider.make_chat_provider")
    with pytest.raises(ValueError, match="same model|TurnOutput|nonempty"):
        await replay.evaluate(args)
    factory.assert_not_called()


# OpenCV's config loader uses a default encoding when the real schemas import it.
@pytest.mark.filterwarnings(
    r"ignore:'encoding' argument not specified:EncodingWarning:^cv2\.load_config_py3$"
)
def test_catalog_reconstruction_preserves_full_runtime_schema(tmp_path, mcp_schemas):
    catalog = ActionCatalog.compile(mcp_schemas, [], max_scratchpad=8000)
    write_case(tmp_path, request_fixture(), request_fixture(), catalog=catalog)
    assert replay.load_catalog(tmp_path).schema == catalog.schema


@pytest.mark.parametrize("schema", [None, {}, {"type": "object"}])
def test_replay_requires_recorded_turn_output_schema(tmp_path, schema):
    (tmp_path / "model.jsonl").write_text(
        json.dumps(
            {
                "kind": "session_start",
                "output_schema": schema,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="TurnOutput"):
        replay.load_catalog(tmp_path)


@pytest.mark.parametrize(
    "provider,url",
    [
        ("deepseek", "https://api.deepseek.com/v1/chat/completions"),
        ("openai", "https://api.openai.com/v1/responses"),
        ("anthropic", "https://api.anthropic.com/v1/messages"),
        (
            "google",
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        ),
        ("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
        ("moonshot", "https://api.moonshot.cn/v1/chat/completions"),
        ("openrouter", "https://openrouter.ai/api/v1/chat/completions"),
    ],
)
async def test_evaluate_sends_exact_native_payload_and_records_cache_usage(
    tmp_path, monkeypatch, respx_mock, provider, url
):
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "fixture-only")
    before, after = request_fixture(provider), request_fixture(provider)
    field = "input" if provider == "openai" else "messages"
    after[field][-1]["content"] = "Revised context"
    case = write_case(tmp_path, before, after, provider=provider)
    case.update(actions=[{"tool": "peek"}], name="现场回放 — 🦀")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([case], ensure_ascii=False), encoding="utf-8")
    value = turn_fixture()
    raw = response(provider, value, 0)
    if provider == "openai":
        raw["usage"] = {
            "input_tokens": 100,
            "output_tokens": 10,
            "input_tokens_details": {"cached_tokens": 75},
        }
    elif provider == "anthropic":
        raw["usage"] = {
            "input_tokens": 25,
            "output_tokens": 10,
            "cache_read_input_tokens": 75,
        }
    else:
        raw["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 75},
        }
    route = respx_mock.post(url).respond(200, json=raw)
    args = SimpleNamespace(
        cases=cases, output=tmp_path / "results.json", repeats=2, concurrency=1
    )
    assert await replay.evaluate(args) == 0
    assert [json.loads(call.request.content) for call in route.calls] == [
        before,
        after,
    ] * 2
    report = json.loads(args.output.read_text(encoding="utf-8"))
    assert [row["repeat"] for row in report["results"]] == [0, 0, 1, 1]
    for row in report["results"]:
        assert row["case"] == case["name"] and row["output"] == value
        assert row["usage"]["prompt_tokens"] == 100
        assert row["usage"]["cached_tokens"] == 75


@pytest.mark.parametrize("kind", ["image_url", "input_image", "image"])
def test_restore_keeps_image_bytes_exact_and_confines_reads(tmp_path, kind):
    from physiclaw.contract.wire import scrub_block

    images = tmp_path / "images"
    images.mkdir()
    data = b"synthetic image bytes"
    (images / "screen.jpg").write_bytes(data)
    encoded = base64.b64encode(data).decode()
    block = {
        "image_url": {
            "type": kind,
            "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": "high"},
        },
        "input_image": {
            "type": kind,
            "image_url": f"data:image/jpeg;base64,{encoded}",
            "detail": "high",
        },
        "image": {
            "type": kind,
            "source": {"type": "base64", "media_type": "image/jpeg", "data": encoded},
        },
    }[kind]
    scrubbed = scrub_block(block, lambda mime, data: "images/screen.jpg")
    assert replay.restore_images(scrubbed, tmp_path) == block
    assert replay.restore_images(block, tmp_path) == block
    assert replay.restore_images(
        {"type": "tool_result", "content": [scrubbed]}, tmp_path
    )["content"] == [block]
    with pytest.raises(ValueError, match="outside"):
        replay.restore_images(
            scrub_block(block, lambda mime, data: "../secret.jpg"), tmp_path
        )


def test_grading_accepts_only_grounded_alternatives():
    case = {
        "actions": [
            {"tool": "tap", "args": {"bbox": [0.1, 0.2, 0.3, 0.4]}},
            {"tool": "peek"},
        ]
    }

    def errors(name, arguments):
        return replay.grade(
            completion_fixture({**turn_fixture(), "action": {name: arguments}}), case
        )

    assert not errors("tap", {"bbox": [0.1, 0.2, 0.3, 0.4]})
    assert not errors("peek", {})
    assert errors("tap", {"bbox": [0.11, 0.2, 0.3, 0.4]})
    assert errors("tap", {})
    assert errors("tap", {"bbox": [0.3, 0.2, 0.1, 0.4]})


def test_grading_rejects_forbidden_box_regardless_of_numeric_spelling():
    args = {"bbox": [0.0, 0.0, 0.5, 0.5]}
    reply = completion_fixture({**turn_fixture(), "action": {"tap": args}})
    case = {"actions": [{"tool": "tap"}], "forbidden_bboxes": [[0, 0, 0.5, 0.5]]}
    assert "unsafe target" in replay.grade(reply, case)
    assert replay.contains_box(
        {"actions": [{"tool_name": "tap", "arg": args["bbox"]}]}, [0, 0, 0.5, 0.5]
    )
    assert not replay.contains_box(
        {"text": "quoted [0, 0, 0.5, 0.5]"}, [0, 0, 0.5, 0.5]
    )


@pytest.mark.parametrize("scratchpad", [42, None, "", "  "])
def test_grading_keeps_invalid_or_missing_scratchpad_as_a_model_failure(scratchpad):
    reply = completion_fixture({**turn_fixture(), "scratchpad": scratchpad})
    assert replay.grade(
        reply, {"actions": [{"tool": "peek"}], "scratchpad_required": True}
    )


@pytest.mark.parametrize(
    "finish", [FinishReason.CONTENT_FILTER, FinishReason.LENGTH, FinishReason.ERROR]
)
def test_grading_rejects_unusable_protocol_results(finish):
    assert replay.grade(
        completion_fixture(finish=finish), {"actions": [{"tool": "peek"}]}
    )


def test_replay_rejects_old_two_call_response():
    reply = completion_fixture(
        calls=[ToolCall("n", "note", {}), ToolCall("a", "peek", {})]
    )
    assert replay.grade(reply, {"actions": [{"tool": "peek"}]})
