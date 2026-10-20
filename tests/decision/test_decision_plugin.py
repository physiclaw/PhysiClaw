"""Shadow calls are bounded, observable and never produce executable turns."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from physiclaw.common import config
from physiclaw.contract.decision import DecisionAnswer, DecisionResult, DecisionUsage
from physiclaw.contract.observation import read_observation
from physiclaw.contract.plugin import PluginServices
from physiclaw.contract.turn import (
    ProgressSnapshot,
    ProgressStep,
    TurnInput,
    TurnMetadata,
)
from physiclaw.decision.plugin import DecisionPlugin


def answer(request):
    return DecisionResult(
        request.id,
        {
            "phase": DecisionAnswer("ROUTINE"),
            "action_type": DecisionAnswer("tap"),
            "target": DecisionAnswer("b1"),
        },
        provider_id="test",
        model_id="actual-model",
        requested_model_id="configured-model",
        usage=DecisionUsage(input_tokens=123, output_tokens=0),
        elapsed_seconds=0.01,
        provider_request_id="remote-request",
    )


@pytest.fixture
def turn(observation_blocks):
    return TurnInput(
        read_observation(observation_blocks()),
        None,
        progress=ProgressSnapshot(
            "Open the app", (ProgressStep("Open the app", "in_progress"),)
        ),
        tools={"tap": {}, "long_press": {}},
    )


@pytest.fixture
def metadata():
    return TurnMetadata("session", 0, None, 20)


@pytest.fixture
async def shadow(mocker, monkeypatch):
    monkeypatch.setattr(config.CONFIG.decision, "model", "test/configured-model")
    monkeypatch.setattr(config.CONFIG.decision, "mode", "shadow")
    monkeypatch.setattr(config.CONFIG.decision, "timeout_seconds", 2.0)
    provider = SimpleNamespace(
        evaluate=mocker.AsyncMock(side_effect=lambda request, **kw: answer(request)),
        aclose=mocker.AsyncMock(),
    )
    events = []
    sink = SimpleNamespace(write=mocker.Mock(side_effect=events.append))
    factory = mocker.Mock(return_value=provider)
    services = PluginServices(
        session_provider=mocker.Mock(),
        events=sink,
        decision_provider_factory=factory,
    )
    plugin = DecisionPlugin()
    await plugin.session_setup(services)
    yield SimpleNamespace(
        plugin=plugin,
        provider=provider,
        factory=factory,
        sink=sink,
        services=services,
        events=events,
    )
    await plugin.aclose()


def calls(shadow):
    return [event for event in shadow.events if event["event"] == "decision_call"]


async def test_shadow_preserves_request_identity_geometry_and_reported_usage(
    shadow, turn, metadata
):
    assert shadow.factory.call_count == 0
    assert await shadow.plugin.advance(turn, metadata=metadata) is None
    scheduled = shadow.events[-1]
    await shadow.plugin._task
    shadow.factory.assert_called_once_with(
        "test", "configured-model", require_images=True
    )
    request = shadow.provider.evaluate.call_args.args[0]
    assert request.id == scheduled["shadow"]["request_id"]
    assert request.context.frame is turn.observation.frame
    assert request.context.facts["turn"] == 0
    record = calls(shadow)[0]
    assert record["request_id"] == request.id
    assert record["turn"] == 0
    assert record["observation_id"] == turn.observation.id
    assert record["model"] == "test/configured-model"
    assert record["response_model"] == "actual-model"
    assert record["provider_request_id"] == "remote-request"
    assert record["usage"] == {"input_tokens": 123, "output_tokens": 0}
    assert record["provider_elapsed_ms"] == 10
    assert record["elapsed_ms"] >= 0
    assert record["interpretation"]["reason"] == "hypothetical_action"
    action = record["interpretation"]["action"]
    target = next(element for element in turn.observation.elements if element.id == 1)
    assert action["target_ref"] == turn.observation.element_ref(target)
    assert action["arguments"]["bbox"] == list(target.bbox)
    json.dumps(shadow.events, allow_nan=False)
    shadow.services.session_provider.assert_not_called()


@pytest.mark.parametrize("ref", ["", "   ", "missing-slash", "test/"])
async def test_shadow_requires_model_and_reports_bad_config_once(
    shadow, turn, metadata, monkeypatch, ref
):
    monkeypatch.setattr(config.CONFIG.decision, "model", ref)
    plugin = DecisionPlugin()
    await plugin.session_setup(shadow.services)
    assert await plugin.advance(turn, metadata=metadata) is None
    assert await plugin.advance(turn, metadata=replace(metadata, turn=1)) is None
    await plugin.aclose()
    shadow.factory.assert_not_called()
    assert not calls(shadow)
    assert shadow.events == [
        {
            "event": "decision_shadow_disabled",
            "reason": "missing_model" if not ref.strip() else "invalid_model_ref",
        }
    ]


@pytest.mark.parametrize("ref", ["", "test/configured-model", "invalid-model"])
async def test_off_bypasses_all_decision_work_even_with_model_configured(
    shadow, turn, metadata, monkeypatch, mocker, ref
):
    from physiclaw.decision import plugin as plugin_module

    monkeypatch.setattr(config.CONFIG.decision, "mode", "off")
    monkeypatch.setattr(config.CONFIG.decision, "model", ref)
    projector = mocker.patch.object(plugin_module, "project")
    plugin = DecisionPlugin()
    await plugin.session_setup(shadow.services)
    assert await plugin.advance(turn, metadata=metadata) is None
    await plugin.aclose()
    projector.assert_not_called()
    shadow.factory.assert_not_called()
    assert shadow.events == []


async def test_settings_are_captured_once_for_each_session(
    shadow, turn, metadata, monkeypatch
):
    monkeypatch.setattr(config.CONFIG.decision, "mode", "off")
    monkeypatch.setattr(config.CONFIG.decision, "model", "other/new-model")
    monkeypatch.setattr(config.CONFIG.decision, "timeout_seconds", 20.0)
    before = asyncio.get_running_loop().time()
    await shadow.plugin.advance(turn, metadata=metadata)
    await shadow.plugin._task
    shadow.factory.assert_called_once_with(
        "test", "configured-model", require_images=True
    )
    deadline = shadow.provider.evaluate.call_args.kwargs["deadline"]
    assert before < deadline <= asyncio.get_running_loop().time() + 2.0

    fresh = DecisionPlugin()
    await fresh.session_setup(shadow.services)
    event_count = len(shadow.events)
    assert await fresh.advance(turn, metadata=metadata) is None
    await fresh.aclose()
    assert len(shadow.events) == event_count
    shadow.provider.evaluate.assert_awaited_once()


@pytest.mark.parametrize("problem", ["no_progress", "no_tools", "bad_view", "deadline"])
async def test_ineligible_work_never_constructs_provider(
    shadow, turn, metadata, problem
):
    if problem == "no_progress":
        turn = replace(turn, progress=None)
    elif problem == "no_tools":
        turn = replace(turn, tools={})
    elif problem == "bad_view":
        turn = replace(turn, observation_issue="invalid_view")
    else:
        metadata = replace(metadata, deadline=asyncio.get_running_loop().time() - 1)
    assert await shadow.plugin.advance(turn, metadata=metadata) is None
    shadow.factory.assert_not_called()
    assert shadow.events[-1]["shadow"]["status"] == "skipped"
    assert not calls(shadow)


@pytest.mark.parametrize("missing", ["events", "decision_provider_factory"])
async def test_missing_host_service_cannot_break_fallback(
    shadow, turn, metadata, missing
):
    plugin = DecisionPlugin()
    await plugin.session_setup(replace(shadow.services, **{missing: None}))
    assert await plugin.advance(turn, metadata=metadata) is None
    await plugin.aclose()
    shadow.factory.assert_not_called()


async def test_busy_turns_are_not_queued_and_results_keep_original_turn(
    shadow, turn, metadata
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def evaluate(request, *, deadline):
        entered.set()
        await release.wait()
        return answer(request)

    shadow.provider.evaluate.side_effect = evaluate
    assert await shadow.plugin.advance(turn, metadata=metadata) is None
    await entered.wait()
    original = shadow.plugin._task
    assert await shadow.plugin.advance(turn, metadata=replace(metadata, turn=1)) is None
    assert shadow.events[-1]["shadow"] == {"status": "skipped", "reason": "busy"}
    assert shadow.plugin._task is original
    shadow.provider.evaluate.assert_awaited_once()
    release.set()
    await original
    assert calls(shadow)[0]["turn"] == 0
    # No queued request starts after the busy call finishes.
    shadow.provider.evaluate.assert_awaited_once()
    await shadow.plugin.advance(turn, metadata=replace(metadata, turn=2))
    await shadow.plugin._task
    assert [event["turn"] for event in calls(shadow)] == [0, 2]
    shadow.factory.assert_called_once()


async def test_factory_failure_is_not_retried_or_logged_with_secrets(
    shadow, turn, metadata
):
    shadow.factory.side_effect = RuntimeError("secret credentials")
    await shadow.plugin.advance(turn, metadata=metadata)
    await shadow.plugin._task
    await shadow.plugin.advance(turn, metadata=replace(metadata, turn=1))
    shadow.factory.assert_called_once()
    assert not calls(shadow)
    assert shadow.events[-1]["shadow"]["reason"] == "provider_unavailable"
    assert "secret credentials" not in json.dumps(shadow.events)


async def test_provider_failure_does_not_retry_or_invent_zero_usage(
    shadow, turn, metadata
):
    shadow.provider.evaluate.side_effect = RuntimeError("private server response")
    await shadow.plugin.advance(turn, metadata=metadata)
    await shadow.plugin._task
    shadow.provider.evaluate.assert_awaited_once()
    record = calls(shadow)[0]
    assert record["status"] == "error"
    assert record["error"] == "RuntimeError"
    assert record["usage"] == {"input_tokens": None, "output_tokens": None}
    assert "private server response" not in json.dumps(shadow.events)


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"request_id": "other-request"}, "invalid_result"),
        ({"answers": {"phase": DecisionAnswer("ROUTINE")}}, "invalid_result"),
        ({"provider_id": "other-provider"}, "error"),
        ({"requested_model_id": "other-model"}, "error"),
    ],
)
async def test_malformed_or_mismatched_answers_are_trace_only(
    shadow, turn, metadata, change, expected
):
    shadow.provider.evaluate.side_effect = lambda request, **kw: replace(
        answer(request), **change
    )
    assert await shadow.plugin.advance(turn, metadata=metadata) is None
    await shadow.plugin._task
    record = calls(shadow)[0]
    if expected == "error":
        assert record["status"] == "error"
        assert "interpretation" not in record
    else:
        assert record["interpretation"] == {"reason": expected, "action": None}


async def test_outer_deadline_bounds_a_structural_provider(shadow, turn, metadata):
    entered = asyncio.Event()

    async def evaluate(request, *, deadline):
        entered.set()
        await asyncio.Event().wait()

    shadow.provider.evaluate.side_effect = evaluate
    deadline = asyncio.get_running_loop().time() + 0.02
    await shadow.plugin.advance(turn, metadata=replace(metadata, deadline=deadline))
    await asyncio.wait_for(shadow.plugin._task, timeout=1)
    assert entered.is_set()
    assert shadow.provider.evaluate.call_args.kwargs["deadline"] == deadline
    assert calls(shadow)[0]["error"] == "TimeoutError"
    assert calls(shadow)[0]["usage"]["input_tokens"] is None


async def test_late_answer_keeps_known_cost_evidence_but_is_not_interpreted(
    shadow, turn, metadata
):
    async def evaluate(request, *, deadline):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return answer(request)

    shadow.provider.evaluate.side_effect = evaluate
    metadata = replace(metadata, deadline=asyncio.get_running_loop().time() + 0.02)
    await shadow.plugin.advance(turn, metadata=metadata)
    await asyncio.wait_for(shadow.plugin._task, timeout=1)
    record = calls(shadow)[0]
    assert record["status"] == "error"
    assert record["error"] == "TimeoutError"
    assert record["usage"] == {"input_tokens": 123, "output_tokens": 0}
    assert record["provider_request_id"] == "remote-request"
    assert "interpretation" not in record


async def test_close_does_not_interpret_a_provider_that_swallows_cancellation(
    shadow, turn, metadata
):
    entered = asyncio.Event()

    async def evaluate(request, *, deadline):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return answer(request)

    shadow.provider.evaluate.side_effect = evaluate
    await shadow.plugin.advance(turn, metadata=metadata)
    await entered.wait()
    await shadow.plugin.aclose()
    record = calls(shadow)[0]
    assert record["status"] == "cancelled"
    assert record["usage"] == {"input_tokens": 123, "output_tokens": 0}
    assert "interpretation" not in record


async def test_close_cancels_pending_inference_before_closing_client(
    shadow, turn, metadata
):
    entered = asyncio.Event()
    order = []

    async def evaluate(request, *, deadline):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            order.append("cancelled")

    shadow.provider.evaluate.side_effect = evaluate
    shadow.provider.aclose.side_effect = lambda: order.append("closed")
    await shadow.plugin.advance(turn, metadata=metadata)
    await entered.wait()
    await shadow.plugin.aclose()
    await shadow.plugin.aclose()
    assert order == ["cancelled", "closed"]
    assert calls(shadow)[0]["status"] == "cancelled"
    assert await shadow.plugin.advance(turn, metadata=metadata) is None
    shadow.provider.evaluate.assert_awaited_once()


async def test_cancellation_of_close_propagates_after_client_cleanup(
    shadow, turn, metadata
):
    entered, cancelling, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def evaluate(request, *, deadline):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            # Hold teardown so cancellation of the *parent* close can be tested.
            await asyncio.Event().wait()

    shadow.provider.evaluate.side_effect = evaluate
    shadow.provider.aclose.side_effect = cleaned.set
    await shadow.plugin.advance(turn, metadata=metadata)
    await entered.wait()
    closing = asyncio.create_task(shadow.plugin.aclose())
    await cancelling.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert cleaned.is_set()
    shadow.provider.aclose.assert_awaited_once()


async def test_close_before_background_start_has_no_fictitious_api_attempt(
    shadow, turn, metadata
):
    await shadow.plugin.advance(turn, metadata=metadata)
    await shadow.plugin.aclose()
    shadow.factory.assert_not_called()
    shadow.provider.evaluate.assert_not_called()
    assert not calls(shadow)


@pytest.mark.parametrize("when", ["projection", "response"])
async def test_broken_trace_never_raises_or_restarts_inference(
    shadow, turn, metadata, when
):
    def write(event):
        if when == "projection" or event["event"] == "decision_call":
            raise OSError("private path")
        shadow.events.append(event)

    shadow.sink.write.side_effect = write
    assert await shadow.plugin.advance(turn, metadata=metadata) is None
    if shadow.plugin._task is not None:
        await shadow.plugin._task
    assert await shadow.plugin.advance(turn, metadata=replace(metadata, turn=1)) is None
    assert shadow.provider.evaluate.call_count == (0 if when == "projection" else 1)
