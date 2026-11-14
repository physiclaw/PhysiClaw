"""Structured LLM turns through real adapters, gates and guarded dispatch."""

import json
import time
from dataclasses import replace

import httpx
import pytest
import respx
from freezegun import freeze_time

from physiclaw.agent.engine import assemble, builtin_tool, loop, policy
from physiclaw.agent.engine.provenance import PluginBinding
from physiclaw.agent.engine.runspec import EngineRun, Settings
from physiclaw.agent.engine.session import Session
from physiclaw.agent.llm.context_policy import CollapsePolicy, context_policy_for
from physiclaw.agent.llm.producer import LLMProducer, retry_delay
from physiclaw.agent.runtime.hook import Trigger
from physiclaw.common.provider_errors import ProviderTransientError
from physiclaw.contract.dto import AssistantMessage, OutputResultMessage, UserMessage
from physiclaw.contract.turn import ActionEvent, TurnInput, TurnMetadata
from physiclaw.provider.services.registry import provider_class
from tests.support.provider_responses import response

VENDORS = [
    ("deepseek", "deepseek-flash"),
    ("openai", "gpt-5"),
    ("openai", "gpt-6-luna"),
    ("openai", "gpt-6.1-sol"),
    ("openai", "gpt-6.1-terra"),
    ("anthropic", "claude-sonnet-4-6"),
    ("google", "gemini-3-flash-preview"),
    ("qwen", "qwen3.6-plus"),
    ("moonshot", "kimi-k2.6"),
    ("openrouter", "anthropic/claude-sonnet-4.6"),
]


class Sink:
    def __init__(self):
        self.events = []

    def write(self, event):
        self.events.append(event)
        return True

    def write_native_request(self, turn, body, **metadata):
        self.events.append({"body": body, **metadata})

    def write_response(self, turn, raw, **metadata):
        self.events.append({"raw": raw, **metadata})


class Mcp:
    def __init__(self, blocks, fail=False):
        self.blocks, self.fail, self.calls = blocks, fail, []

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        if self.fail:
            raise RuntimeError("fixture action failure")
        return self.blocks()


def turn(
    action="peek", arguments=None, scratchpad=None, *, summary="Inspect then continue"
):
    return {
        "summary": summary,
        "scratchpad": scratchpad,
        "action": {action: arguments or {}},
    }


def closing():
    return turn("end_session", {"status": "DONE", "recap": "complete"})


@pytest.fixture
def harness(monkeypatch, mocker, observation_blocks, mcp_schemas):
    from physiclaw.agent import layout

    mocker.patch.object(layout, "is_learned", return_value=True)

    def make(vendor="deepseek", model="deepseek-flash", *, fail=False, collapse=None):
        monkeypatch.setenv(f"{vendor.upper()}_API_KEY", "fixture")
        adapter = provider_class(vendor)(model=model, base_url="https://fixture.test")
        bundle = assemble.build_prompt_bundle(vendor, mcp_schemas=mcp_schemas)
        sink = Sink()
        adapter.usage_sink = sink
        session = Session()
        run = EngineRun(
            plugins=(),
            mcp=Mcp(observation_blocks, fail),
            schema_by_name={
                t["name"]: t
                for t in [*mcp_schemas, *builtin_tool.schemas(bundle.local_registry)]
            },
            local_registry=bundle.local_registry,
            tr=sink,
            rlog=sink,
            settings=Settings(
                max_turns=12,
                max_session_attempts=1,
                provider_retry_attempts=3,
                retry_backoff_seconds=0,
                wait_default_minutes=15,
                max_session_seconds=0,
            ),
            policies=policy.default_policies(layout_incomplete=False),
        )
        producer = LLMProducer(
            adapter,
            run,
            session,
            assemble.build_initial_messages(
                [Trigger(description="fixture")], bundle.system_prompt
            ),
            catalog=bundle.catalog,
            context_policy=replace(context_policy_for(vendor), collapse=collapse)
            if collapse
            else context_policy_for(vendor),
        )
        run.plugins = (PluginBinding("physiclaw.llm", producer, "LLM"),)
        return adapter, run, session, producer, sink

    return make


def queue(router, vendor, values, *, finish=None):
    requests = []
    replies = iter(enumerate(values))

    def respond(request):
        requests.append(json.loads(request.content))
        index, value = next(replies)
        return httpx.Response(200, json=response(vendor, value, index, finish=finish))

    router.route(method="POST").mock(side_effect=respond)
    return requests


@pytest.mark.parametrize("vendor,model", VENDORS)
@respx.mock
async def test_structured_full_loop_preserves_action_state_and_pairing(
    harness, vendor, model
):
    adapter, run, session, producer, sink = harness(vendor, model)
    requests = queue(respx, vendor, [turn(scratchpad="saved"), turn(), closing()])

    await loop.drive(run, session)
    await adapter.aclose()

    assert session.sentinel_status == "DONE"
    assert session.scratchpad == "saved"
    assert run.mcp.calls == [("peek", {}), ("peek", {})]
    assert [event.tool for event in session.facts.events] == [
        "note",
        "peek",
        "note",
        "peek",
        "note",
        "end_session",
    ]
    assert len([e for e in sink.events if e.get("event") == "usage"]) == 3
    if vendor == "openai":
        assert "envelope0" not in json.dumps(requests)
        assert all("tools" not in r and "text" in r for r in requests)
        assert "Structured output result:" in json.dumps(requests[1])
    else:
        assert "envelope0" in json.dumps(requests[1])
    assert "note_" not in json.dumps(requests)
    assert (
        len([m for m in producer.messages if isinstance(m, OutputResultMessage)]) == 2
    )
    assert len([m for m in producer.messages if isinstance(m, AssistantMessage)]) == 3


@pytest.mark.parametrize(
    "bad",
    [
        turn("tap", {}, "must not save"),
        {**turn(), "extra": True},
        turn("note"),
        turn("peek", {"x": float("nan")}),
    ],
)
@pytest.mark.parametrize(
    "vendor,model", [("deepseek", "deepseek-flash"), ("openai", "gpt-6.1-sol")]
)
@respx.mock
async def test_invalid_whole_turn_cannot_write_or_dispatch(harness, bad, vendor, model):
    adapter, run, session, producer, sink = harness(vendor, model)
    requests = queue(respx, vendor, [bad, closing()])

    await loop.drive(run, session)
    await adapter.aclose()

    assert session.scratchpad == ""
    assert run.mcp.calls == []
    assert [e.tool for e in session.facts.events] == ["note", "end_session"]
    assert "envelope0" not in json.dumps(requests[1])


@respx.mock
async def test_action_failure_retains_accepted_scratchpad_and_returns_both_outcomes(
    harness,
):
    adapter, run, session, producer, sink = harness(fail=True)
    requests = queue(respx, "deepseek", [turn(scratchpad="saved"), closing()])

    await loop.drive(run, session)
    await adapter.aclose()

    assert session.scratchpad == "saved"
    assert run.mcp.calls == [("peek", {})]
    assert "scratchpad/summary: ok" in json.dumps(requests[1])
    assert "peek: error" in json.dumps(requests[1])


@respx.mock
async def test_checkpoint_one_retry_then_fail_open_and_repeated_compaction(harness):
    adapter, run, session, producer, sink = harness(
        collapse=CollapsePolicy(first_at=2, keep=1, interval=1)
    )
    requests = queue(
        respx,
        "deepseek",
        [turn(), turn(), turn(), turn(scratchpad="checkpoint"), closing()],
    )

    await loop.drive(run, session)
    await adapter.aclose()

    assert len(requests) == 5
    assert session.collapsed_once
    assert session.scratchpad == "checkpoint"
    assert "Inspect then continue" in producer.messages[2].content
    assert (
        len([e for e in sink.events if e.get("event") == "checkpoint_corrective"]) == 1
    )


@respx.mock
async def test_missing_receipts_return_unknown_outcome_without_reexecution(harness):
    adapter, run, session, producer, sink = harness()
    requests = queue(respx, "deepseek", [turn(), closing()])
    metadata = TurnMetadata("fixture", 0, None, 12)

    first = await producer.advance(TurnInput(None, None), metadata=metadata)
    second = await producer.advance(
        TurnInput(None, None), metadata=replace(metadata, turn=1)
    )
    await adapter.aclose()

    assert first.message.tool_names() == ["note", "peek"]
    assert second.message.tool_names() == ["note", "end_session"]
    assert "Outcome unknown" in json.dumps(requests[1])
    assert "envelope0" in json.dumps(requests[1])
    assert run.mcp.calls == []


@respx.mock
async def test_other_producer_receipts_are_observations_without_private_context(
    harness,
):
    adapter, run, session, producer, sink = harness()
    requests = queue(respx, "deepseek", [closing()])
    events = (
        ActionEvent(
            1,
            0,
            "external",
            "peek",
            {},
            False,
            None,
            None,
            "observed",
            ({"type": "text", "text": "observed"},),
        ),
        ActionEvent(2, 0, "private", "note", {}, False, None, None, "PRIVATE"),
    )

    await producer.advance(
        TurnInput(None, None, events), metadata=TurnMetadata("fixture", 0, None, 12)
    )
    await adapter.aclose()

    assert "observed" in json.dumps(requests)
    assert "PRIVATE" not in json.dumps(requests)
    assert "external" not in json.dumps(requests)
    assert isinstance(producer.messages[-2], UserMessage)


@pytest.mark.parametrize("vendor,model", VENDORS)
async def test_adapter_selects_transport_without_engine_override(
    harness, vendor, model
):
    adapter, run, session, producer, sink = harness(vendor, model)
    try:
        body = adapter.build_request(
            producer.messages, [], output_spec=producer.catalog.spec()
        )
        if vendor == "openai":
            assert adapter.structured_transport == "native"
            assert "text" in body and "tools" not in body
        else:
            assert adapter.structured_transport == "tool"
            assert "tools" in body and "response_format" not in body
        assert run.mcp.calls == []
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "finish,expected", [("content_filter", "FAIL"), ("length", "FAIL")]
)
@respx.mock
async def test_rejected_provider_finish_cannot_write_or_execute(
    harness, finish, expected
):
    adapter, run, session, producer, sink = harness()
    requests = queue(
        respx, "deepseek", [turn(scratchpad="must not save")] * 5, finish=finish
    )

    await loop.drive(run, session)
    await adapter.aclose()

    assert session.sentinel_status == expected
    assert session.scratchpad == ""
    assert run.mcp.calls == []
    assert session.facts.events == []
    assert len(requests) == 1
    if finish == "length":
        assert "provider_max_output_tokens" in session.sentinel_recap


@respx.mock
async def test_transport_retry_before_execution_does_not_duplicate_action(harness):
    adapter, run, session, producer, sink = harness()
    respx.route(method="POST").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=response("deepseek", turn(), 0)),
            httpx.Response(200, json=response("deepseek", closing(), 1)),
        ]
    )

    await loop.drive(run, session)
    await adapter.aclose()

    assert run.mcp.calls == [("peek", {})]
    assert len([e for e in sink.events if "body" in e]) == 3
    assert len([e for e in sink.events if e.get("event") == "usage"]) == 3


@pytest.mark.parametrize("vendor,model", [v for v in VENDORS if v[0] != "anthropic"])
@pytest.mark.parametrize("failure", ["error_finish", "error_object"])
@respx.mock
async def test_failed_generation_retries_without_writing_or_executing(
    harness, vendor, model, failure
):
    adapter, run, session, producer, sink = harness(vendor, model)
    failed = response(vendor, turn(scratchpad="must not save"), 0)
    if failure == "error_finish":
        if vendor == "openai":
            failed["status"] = "failed"
        else:
            failed["choices"][0]["finish_reason"] = "error"
    else:
        failed["error"] = {"message": "upstream failed", "code": 503}
    route = respx.route(method="POST").mock(
        side_effect=[
            httpx.Response(200, json=failed),
            httpx.Response(200, json=response(vendor, closing(), 1)),
        ]
    )

    try:
        await loop.drive(run, session)
    finally:
        await adapter.aclose()

    assert session.sentinel_status == "DONE"
    assert session.scratchpad == ""
    assert run.mcp.calls == []
    assert route.call_count == 2
    assert session.model_turns == 1
    usage = [e for e in sink.events if e.get("event") == "usage"]
    assert len(usage) == 2
    assert usage[0]["error"] == "ProviderTransientError"
    assert usage[0]["output"] == 20
    assert [e["raw"] for e in sink.events if "raw" in e][0] == failed


def rate_limit(vendor, delay):
    if vendor == "google":
        return httpx.Response(
            429,
            json=[
                {
                    "error": {
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                "retryDelay": f"{delay}s",
                            }
                        ]
                    }
                }
            ],
        )
    return httpx.Response(429, headers={"Retry-After": str(delay)})


@pytest.mark.parametrize(
    "vendor,model", [VENDORS[0], ("google", "gemini-3-flash-preview")]
)
@respx.mock
@freeze_time("2026-08-30 12:00", real_asyncio=True)
async def test_retry_after_beyond_deadline_does_not_start_another_attempt(
    harness, vendor, model
):
    adapter, run, session, producer, sink = harness(vendor, model)
    run.deadline = time.monotonic() + 30
    route = respx.route(method="POST").mock(return_value=rate_limit(vendor, 120))
    try:
        await loop.drive(run, session)
    finally:
        await adapter.aclose()
    assert route.call_count == 1
    assert session.budget_exhausted
    assert run.mcp.calls == []


@pytest.mark.parametrize(
    "vendor,model", [VENDORS[0], ("google", "gemini-3-flash-preview")]
)
@respx.mock
async def test_retry_after_controls_host_wait(harness, monkeypatch, vendor, model):
    adapter, run, session, producer, sink = harness(vendor, model)
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("physiclaw.agent.llm.producer.asyncio.sleep", sleep)
    respx.route(method="POST").mock(
        side_effect=[
            rate_limit(vendor, 7),
            httpx.Response(200, json=response(vendor, closing(), 1)),
        ]
    )
    try:
        await loop.drive(run, session)
    finally:
        await adapter.aclose()
    assert delays == [7]
    assert session.sentinel_status == "DONE"
    assert len([e for e in sink.events if e.get("event") == "usage"]) == 2


@pytest.mark.parametrize("vendor,model", VENDORS)
@respx.mock
async def test_failed_request_log_does_not_prevent_or_retry_generation(
    harness, monkeypatch, vendor, model
):
    adapter, run, session, producer, sink = harness(vendor, model)

    def failed_log(*args, **kwargs):
        raise OSError("request log unavailable")

    monkeypatch.setattr(sink, "write_native_request", failed_log)
    route = respx.route(method="POST").respond(json=response(vendor, closing(), 1))
    try:
        await loop.drive(run, session)
    finally:
        await adapter.aclose()
    assert session.sentinel_status == "DONE"
    assert route.call_count == 1
    usage = [e for e in sink.events if e.get("event") == "usage"]
    assert len(usage) == 1 and usage[0]["error"] is None


@respx.mock
@freeze_time("2026-08-30 12:00", real_asyncio=True)
async def test_retry_sleep_cannot_start_a_request_after_deadline(harness, monkeypatch):
    adapter, run, session, producer, sink = harness()
    run.deadline = time.monotonic() + 30

    async def oversleep(delay):
        run.deadline = time.monotonic() - 1

    monkeypatch.setattr("physiclaw.agent.llm.producer.asyncio.sleep", oversleep)
    route = respx.route(method="POST").mock(
        side_effect=[
            rate_limit("deepseek", 1),
            httpx.Response(200, json=response("deepseek", closing(), 1)),
        ]
    )
    try:
        await loop.drive(run, session)
    finally:
        await adapter.aclose()
    assert route.call_count == 1
    assert session.budget_exhausted
    assert run.mcp.calls == []


class ContinuationMcp(Mcp):
    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        if name == "tap" and self.fail:
            self.fail = False
            raise RuntimeError("synthetic action failure; fixture hardware only")
        return self.blocks()


@pytest.mark.parametrize("vendor,model", VENDORS)
@respx.mock
async def test_continuation_preserves_actions_and_state(harness, vendor, model):
    adapter, run, session, _, _ = harness(vendor, model)
    run.mcp = ContinuationMcp(run.mcp.blocks, fail=True)
    target = {"bbox": [0.1, 0.2, 0.3, 0.4]}
    queue(
        respx,
        vendor,
        [
            turn(
                scratchpad="fixture initial state",
                summary="No current view; inspect screen.",
            ),
            turn("tap", target, summary="Target visible; attempt tap."),
            {"summary": "", "scratchpad": None, "action": {}},
            turn(
                "tap",
                target,
                "fixture recovery state",
                summary="Previous action failed; retry grounded target.",
            ),
            turn(
                "end_session",
                {"status": "DONE", "recap": "fixture complete"},
                "",
                summary="Action succeeded; close.",
            ),
        ],
    )

    await loop.drive(run, session)
    await adapter.aclose()

    assert run.mcp.calls == [("peek", {}), ("tap", target), ("tap", target)]
    assert {
        "status": session.sentinel_status,
        "scratchpad": session.scratchpad,
        "model_turns": session.model_turns,
    } == {"status": "DONE", "scratchpad": "", "model_turns": 5}
    assert [event.tool for event in session.facts.events] == [
        "note",
        "peek",
        "note",
        "tap",
        "note",
        "tap",
        "note",
        "end_session",
    ]


@pytest.mark.parametrize(
    "vendor,model",
    [
        ("anthropic", "claude-sonnet-4-6"),
        ("google", "gemini-3-flash-preview"),
        ("openai", "gpt-6.1-sol"),
    ],
)
@respx.mock
async def test_compaction_preserves_loaded_artifacts_and_signed_replies(
    harness, mocker, tmp_path, vendor, model
):
    from physiclaw.agent.engine import memory, skill

    procedure = skill.Skill(
        "fixture",
        "Fixture procedure",
        "Procedure body: inspect before acting.",
        tmp_path,
    )
    mocker.patch.object(
        skill, "discover_user_skills", return_value={"fixture": procedure}
    )
    mocker.patch.object(memory, "load_persistent", return_value="Retained memory fact.")
    adapter, run, session, producer, sink = harness(
        vendor, model, collapse=CollapsePolicy(first_at=2, keep=1, interval=1)
    )
    requests = queue(
        respx,
        vendor,
        [
            turn("Skill", {"name": "fixture"}, "keep"),
            turn("read_memory", scratchpad="keep"),
            turn(scratchpad="keep"),
            turn(scratchpad="keep"),
            closing(),
        ],
    )

    await loop.drive(run, session)
    await adapter.aclose()

    assert "Procedure body: inspect before acting." in producer.messages[4].content
    assert "Retained memory fact." in producer.messages[3].content
    if vendor != "openai":
        assert "signed3" in json.dumps(requests[-1])
    assert "signed0" not in json.dumps(requests[-1])
    assert count_images(requests[-1]) == 1


@respx.mock
async def test_partial_receipts_preserve_known_outcomes_and_skip_unknown_state_writes(
    harness,
):
    adapter, run, session, producer, sink = harness()
    requests = queue(respx, "deepseek", [turn(), closing()])
    metadata = TurnMetadata("fixture", 0, None, 12)
    first = await producer.advance(TurnInput(None, None), metadata=metadata)
    action = first.message.tool_calls[1]
    receipt = ActionEvent(
        1,
        0,
        action.id,
        "peek",
        {},
        False,
        False,
        None,
        "verified",
        ({"type": "text", "text": "verified"},),
    )

    await producer.advance(
        TurnInput(None, None, (receipt,)), metadata=replace(metadata, turn=1)
    )
    await adapter.aclose()

    assert "verified" in json.dumps(requests[1])
    assert "Outcome unknown" in json.dumps(requests[1])
    assert producer.messages[5].context_calls == [action]
    assert session.scratchpad == ""


def count_images(value):
    if isinstance(value, dict):
        return int(value.get("type") in ("image_url", "input_image", "image")) + sum(
            count_images(item) for item in value.values()
        )
    if isinstance(value, list):
        return sum(count_images(item) for item in value)
    return 0


def test_backoff_caps_and_respects_server_minimum(monkeypatch):
    monkeypatch.setattr("physiclaw.agent.llm.producer.random.uniform", lambda a, b: b)
    assert retry_delay(ProviderTransientError("busy"), 1, 5) == 5.5
    assert retry_delay(ProviderTransientError("busy"), 2, 5) == 11
    assert retry_delay(ProviderTransientError("busy"), 20, 5) == 60
    assert retry_delay(ProviderTransientError("busy", retry_after=120), 1, 5) == 120
