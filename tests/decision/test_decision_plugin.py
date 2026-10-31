"""Shared gates, bounded calls, shadow isolation and active fallback."""

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from physiclaw.common import config
from physiclaw.common.provider_errors import (
    ProviderConfigurationError,
    ProviderPermanentError,
    ProviderRequestError,
    ProviderTransientError,
)
from physiclaw.contract.decision import DecisionAnswer, DecisionResult, DecisionUsage
from physiclaw.contract.observation import read_observation
from physiclaw.contract.plugin import PluginServices
from physiclaw.contract.turn import (
    ActionEvent,
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


def scored_answer(request, *, scores=None, selections=None):
    result = answer(request)
    answers = {}
    for question in request.questions:
        selected = (selections or {}).get(
            question.id, result.answers[question.id].selected_id
        )
        probability = (scores or {}).get(question.id, 0.95)
        answers[question.id] = DecisionAnswer(
            selected,
            probabilities={
                candidate.id: probability
                if candidate.id == selected
                else (1 - probability) / (len(question.candidates) - 1)
                for candidate in question.candidates
            },
        )
    return replace(result, answers=answers)


@pytest.fixture
def turn(observation_blocks):
    return TurnInput(
        replace(read_observation(observation_blocks()), observed_after=time.time()),
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
async def shadow(mocker, monkeypatch, request):
    monkeypatch.setattr(config.CONFIG.decision, "model", "test/configured-model")
    monkeypatch.setattr(
        config.CONFIG.decision, "mode", getattr(request, "param", "shadow")
    )
    monkeypatch.setattr(config.CONFIG.decision, "timeout_seconds", 2.0)
    provider = SimpleNamespace(
        evaluate=mocker.AsyncMock(side_effect=lambda request, **kw: answer(request)),
        aclose=mocker.AsyncMock(),
    )
    events = []

    def write(event):
        events.append(event)
        return True

    sink = SimpleNamespace(write=mocker.Mock(side_effect=write))
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
    assert request.id == scheduled["evaluation"]["request_id"]
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
    probe = record["threshold_gate"]
    assert probe["status"] == "missing_scores"
    assert probe["failed_checks"] == ["phase", "action_type", "target"]
    assert probe["scores"] == {}
    json.dumps(shadow.events, allow_nan=False)
    shadow.services.session_provider.assert_not_called()


@pytest.mark.parametrize(
    "tap_probability,status,failed_checks",
    [(0.95, "passed", []), (0.60, "failed", ["action_type"])],
)
async def test_threshold_gate_is_trace_only_and_preserves_raw_proposal(
    shadow, turn, metadata, tap_probability, status, failed_checks
):
    def evaluate(request, **kwargs):
        return scored_answer(request, scores={"action_type": tap_probability})

    shadow.provider.evaluate.side_effect = evaluate
    assert await shadow.plugin.advance(turn, metadata=metadata) is None
    await shadow.plugin._task
    record = calls(shadow)[0]
    assert record["interpretation"]["reason"] == "hypothetical_action"
    assert record["interpretation"]["action"]["tool"] == "tap"
    assert record["answers"]["action_type"]["probabilities"]["tap"] == tap_probability
    probe = record["threshold_gate"]
    assert probe["calibrated"] is False
    assert probe["status"] == status
    assert probe["failed_checks"] == failed_checks
    assert probe["thresholds"] == {"phase": 0.90, "action_type": 0.90, "target": 0.90}
    assert probe["scores"]["action_type"]["selected_probability"] == tap_probability
    assert record["response_model"] == "actual-model"
    shadow.provider.evaluate.assert_awaited_once()
    shadow.services.session_provider.assert_not_called()
    json.dumps(record, allow_nan=False)


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
            "event": "decision_disabled",
            "mode": "shadow",
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


@pytest.mark.parametrize(
    "problem",
    [
        "no_progress",
        "no_tools",
        "long_press_only",
        "bad_view",
        "screenshot",
        "deadline",
    ],
)
async def test_ineligible_work_never_constructs_provider(
    shadow, turn, metadata, problem
):
    if problem == "no_progress":
        turn = replace(turn, progress=None)
    elif problem == "no_tools":
        turn = replace(turn, tools={})
    elif problem == "long_press_only":
        turn = replace(turn, tools={"long_press": {}})
    elif problem == "bad_view":
        turn = replace(turn, observation_issue="invalid_view")
    elif problem == "screenshot":
        turn = replace(turn, observation=replace(turn.observation, source="screenshot"))
    else:
        metadata = replace(metadata, deadline=asyncio.get_running_loop().time() - 1)
    assert await shadow.plugin.advance(turn, metadata=metadata) is None
    shadow.factory.assert_not_called()
    assert shadow.events[-1]["evaluation"]["status"] == "skipped"
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
    assert shadow.events[-1]["evaluation"] == {"status": "skipped", "reason": "busy"}
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
    assert shadow.events[-1]["evaluation"]["reason"] == "provider_unavailable"
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


@pytest.mark.parametrize("shadow", ["shadow", "active"], indirect=True)
@pytest.mark.parametrize("error", [ProviderPermanentError, ProviderConfigurationError])
async def test_permanent_provider_failure_disables_session(
    shadow, turn, metadata, error
):
    shadow.provider.evaluate.side_effect = error("private server response")
    assert await shadow.plugin.advance(turn, metadata=metadata) is None
    await shadow.plugin._task
    assert calls(shadow)[0]["suppression"] == "session"
    assert await shadow.plugin.advance(turn, metadata=replace(metadata, turn=1)) is None
    assert shadow.events[-1]["evaluation"]["reason"] == "provider_unavailable"
    shadow.provider.evaluate.assert_awaited_once()
    assert "private server response" not in json.dumps(shadow.events)
    await shadow.plugin.aclose()
    shadow.provider.aclose.assert_awaited_once()


@pytest.mark.parametrize("shadow", ["shadow", "active"], indirect=True)
@pytest.mark.parametrize("error", [ProviderTransientError, TimeoutError, RuntimeError])
async def test_provider_failure_cools_down_then_recovers_without_retrying_same_call(
    shadow, turn, metadata, monkeypatch, error
):
    loop = asyncio.get_running_loop()
    started = loop.time()
    clock = [started]
    monkeypatch.setattr(loop, "time", lambda: clock[0])
    shadow.provider.evaluate.side_effect = error("private server response")
    assert await shadow.plugin.advance(turn, metadata=metadata) is None
    await shadow.plugin._task
    record = calls(shadow)[0]
    assert record["suppression"] == "cooldown"
    assert record["cooldown_seconds"] == 5.0
    shadow.provider.evaluate.side_effect = lambda request, **kw: scored_answer(request)
    clock[0] = started + 4.999
    assert await shadow.plugin.advance(turn, metadata=replace(metadata, turn=1)) is None
    assert shadow.events[-1]["evaluation"]["reason"] == "provider_cooldown"
    shadow.provider.evaluate.assert_awaited_once()
    clock[0] = started + 5.0
    proposal = await shadow.plugin.advance(turn, metadata=replace(metadata, turn=2))
    await shadow.plugin._task
    assert (proposal is not None) == (shadow.plugin._mode == "active")
    assert shadow.provider.evaluate.await_count == 2
    assert calls(shadow)[1]["status"] == "answered"
    assert "private server response" not in json.dumps(shadow.events)


@pytest.mark.parametrize("shadow", ["shadow", "active"], indirect=True)
async def test_request_rejection_does_not_disable_the_next_valid_request(
    shadow, turn, metadata
):
    shadow.provider.evaluate.side_effect = ProviderRequestError("too many choices")
    assert await shadow.plugin.advance(turn, metadata=metadata) is None
    await shadow.plugin._task
    assert calls(shadow)[0]["suppression"] == "request"
    shadow.provider.evaluate.side_effect = lambda request, **kw: scored_answer(request)
    proposal = await shadow.plugin.advance(turn, metadata=replace(metadata, turn=1))
    await shadow.plugin._task
    assert (proposal is not None) == (shadow.plugin._mode == "active")
    assert shadow.provider.evaluate.await_count == 2
    assert calls(shadow)[1]["status"] == "answered"


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
        assert "threshold_gate" not in record
    else:
        assert record["interpretation"] == {"reason": expected, "action": None}
        assert record["threshold_gate"]["status"] == "not_applicable"


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
        return True

    shadow.sink.write.side_effect = write
    assert await shadow.plugin.advance(turn, metadata=metadata) is None
    if shadow.plugin._task is not None:
        await shadow.plugin._task
    assert await shadow.plugin.advance(turn, metadata=replace(metadata, turn=1)) is None
    assert shadow.provider.evaluate.call_count == (0 if when == "projection" else 1)


@pytest.fixture
def scored_provider(shadow):
    shadow.provider.evaluate.side_effect = lambda request, **kw: scored_answer(request)
    return shadow


@pytest.mark.parametrize("shadow", ["active"], indirect=True)
async def test_active_returns_only_a_bound_local_tap(scored_provider, turn, metadata):
    active = scored_provider
    proposal = await active.plugin.advance(turn, metadata=metadata)
    assert proposal.observation_id == turn.observation.id
    call = proposal.message.tool_calls[0]
    assert call.name == "tap"
    assert call.arguments == {"bbox": list(turn.observation.elements[1].bbox)}
    assert proposal.targets == {call.id: f"{turn.observation.id}:1"}
    record = calls(active)[0]
    assert record["mode"] == "active"
    assert record["threshold_gate"]["status"] == "passed"
    assert record["view_issue"] is None
    assert record["proposal_call_id"] == call.id
    active.services.session_provider.assert_not_called()
    active.provider.evaluate.assert_awaited_once()


@pytest.mark.parametrize("shadow", ["active"], indirect=True)
@pytest.mark.parametrize("host_deadline", [None, 2.0])
async def test_proposal_expiry_is_captured_before_inference_and_trace_io(
    scored_provider, turn, metadata, monkeypatch, host_deadline
):
    active = scored_provider
    loop = asyncio.get_running_loop()
    started = loop.time()
    clock = [started]
    monkeypatch.setattr(loop, "time", lambda: clock[0])
    monkeypatch.setattr(
        "physiclaw.decision.plugin.time.time",
        lambda: turn.observation.observed_after,
    )

    def write(event):
        clock[0] += 0.25
        active.events.append(event)
        return True

    def evaluate(request, **kwargs):
        clock[0] += 0.25
        return scored_answer(request)

    active.sink.write.side_effect = write
    active.provider.evaluate.side_effect = evaluate
    if host_deadline is not None:
        metadata = replace(metadata, deadline=started + host_deadline)
    proposal = await active.plugin.advance(turn, metadata=metadata)
    assert proposal.expires_at == pytest.approx(started + (host_deadline or 10.0))
    assert clock[0] == pytest.approx(started + 0.75)


@pytest.mark.parametrize("shadow", ["active"], indirect=True)
async def test_wall_clock_cannot_extend_proposal_lifetime(
    scored_provider, turn, metadata, monkeypatch
):
    active = scored_provider
    active.plugin._timeout = 20.0
    loop = asyncio.get_running_loop()
    clock = [loop.time()]
    monkeypatch.setattr(loop, "time", lambda: clock[0])
    monkeypatch.setattr(
        "physiclaw.decision.plugin.time.time",
        lambda: turn.observation.observed_after,
    )

    def evaluate(request, **kwargs):
        clock[0] += 11.0
        return scored_answer(request)

    active.provider.evaluate.side_effect = evaluate
    assert await active.plugin.advance(turn, metadata=metadata) is None
    assert calls(active)[0]["view_issue"] == "stale_view"
    assert "proposal_call_id" not in calls(active)[0]


@pytest.mark.parametrize("shadow", ["shadow", "active"], indirect=True)
@pytest.mark.parametrize("delay_in", ["projection_trace", "factory"])
async def test_expired_view_skips_api_after_synchronous_work(
    scored_provider, turn, metadata, monkeypatch, delay_in
):
    active = scored_provider
    active.plugin._timeout = 20.0
    loop = asyncio.get_running_loop()
    clock = [loop.time()]
    monkeypatch.setattr(loop, "time", lambda: clock[0])
    monkeypatch.setattr(
        "physiclaw.decision.plugin.time.time",
        lambda: turn.observation.observed_after,
    )

    if delay_in == "projection_trace":

        def write(event):
            if event["event"] == "decision_projection":
                clock[0] += 11.0
            active.events.append(event)
            return True

        active.sink.write.side_effect = write
    else:

        def factory(*args, **kwargs):
            clock[0] += 11.0
            return active.provider

        active.factory.side_effect = factory

    assert await active.plugin.advance(turn, metadata=metadata) is None
    await active.plugin._task
    active.provider.evaluate.assert_not_awaited()
    assert active.events[-1]["event"] == "decision_skipped"
    assert active.events[-1]["reason"] == "stale_view"
    assert not calls(active)


@pytest.mark.parametrize("shadow", ["active"], indirect=True)
@pytest.mark.parametrize(
    "problem",
    [
        "phase",
        "action_type",
        "target",
        "missing_scores",
        "semantic",
        "yield",
        "none",
        "request_mismatch",
        "model_mismatch",
        "exception",
    ],
)
async def test_active_rejections_yield_without_a_proposal(
    scored_provider, turn, metadata, problem
):
    active = scored_provider

    def evaluate(request, **kw):
        if problem in {"phase", "action_type", "target"}:
            return scored_answer(request, scores={problem: 0.6})
        if problem == "missing_scores":
            return answer(request)
        if problem in {"semantic", "yield", "none"}:
            selections = {
                "semantic": {"phase": "SEMANTIC"},
                "yield": {"action_type": "YIELD"},
                "none": {"target": "NONE"},
            }
            return scored_answer(request, selections=selections[problem])
        if problem == "request_mismatch":
            return replace(scored_answer(request), request_id="unrelated")
        if problem == "model_mismatch":
            return replace(scored_answer(request), requested_model_id="other")
        raise RuntimeError("private response")

    active.provider.evaluate.side_effect = evaluate
    assert await active.plugin.advance(turn, metadata=metadata) is None
    assert "proposal_call_id" not in calls(active)[0]
    assert active.plugin._pending is None
    active.provider.evaluate.assert_awaited_once()
    assert "private response" not in json.dumps(active.events)


@pytest.mark.parametrize("shadow", ["shadow", "active"], indirect=True)
@pytest.mark.parametrize("age", [None, -1, 11])
async def test_unknown_future_or_old_views_skip_inference(
    scored_provider, turn, metadata, age
):
    observation = replace(
        turn.observation, observed_after=None if age is None else time.time() - age
    )
    assert (
        await scored_provider.plugin.advance(
            replace(turn, observation=observation), metadata=metadata
        )
        is None
    )
    scored_provider.factory.assert_not_called()
    assert scored_provider.events[-1]["evaluation"]["reason"] == (
        "unknown_view_age" if age is None else "stale_view"
    )


@pytest.mark.parametrize("shadow", ["shadow", "active"], indirect=True)
async def test_view_that_ages_during_inference_never_becomes_proposal(
    scored_provider, turn, metadata, monkeypatch
):
    clock = [turn.observation.observed_after]
    monkeypatch.setattr("physiclaw.decision.plugin.time.time", lambda: clock[0])

    def evaluate(request, **kw):
        clock[0] += 11
        return scored_answer(request)

    scored_provider.provider.evaluate.side_effect = evaluate
    assert await scored_provider.plugin.advance(turn, metadata=metadata) is None
    await scored_provider.plugin._task
    record = calls(scored_provider)[0]
    assert record["threshold_gate"]["status"] == "passed"
    assert record["view_issue"] == "stale_view"
    assert "proposal_call_id" not in record


@pytest.mark.parametrize("shadow", ["shadow", "active"], indirect=True)
async def test_view_expiry_cancels_inference_without_cooling_down_fresh_views(
    scored_provider, turn, metadata, monkeypatch
):
    monkeypatch.setattr("physiclaw.decision.plugin._MAX_VIEW_AGE_SECONDS", 0.05)
    turn = replace(
        turn, observation=replace(turn.observation, observed_after=time.time())
    )
    cancelled = asyncio.Event()

    async def evaluate(request, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    scored_provider.provider.evaluate.side_effect = evaluate

    async with asyncio.timeout(0.5):
        assert await scored_provider.plugin.advance(turn, metadata=metadata) is None
        await scored_provider.plugin._task
    assert cancelled.is_set()
    record = calls(scored_provider)[0]
    assert record["status"] == "error"
    assert record["reason"] == "stale_view"
    assert record["suppression"] == "request"
    assert scored_provider.plugin._retry_after == 0.0
    assert scored_provider.plugin._factory is not None
    assert "proposal_call_id" not in record


async def test_early_view_expiry_timer_does_not_impose_provider_cooldown(
    shadow, turn, metadata, monkeypatch
):
    monkeypatch.setattr("physiclaw.decision.plugin._MAX_VIEW_AGE_SECONDS", 1.0)
    turn = replace(
        turn, observation=replace(turn.observation, observed_after=time.time())
    )
    monkeypatch.setattr(
        "physiclaw.decision.plugin.asyncio.timeout_at",
        lambda deadline: asyncio.Timeout(asyncio.get_running_loop().time() - 1),
    )

    async def evaluate(request, **kwargs):
        await asyncio.Event().wait()

    shadow.provider.evaluate.side_effect = evaluate

    await shadow.plugin.advance(turn, metadata=metadata)
    await shadow.plugin._task

    record = calls(shadow)[0]
    assert record["reason"] == "stale_view"
    assert record["suppression"] == "request"
    assert shadow.plugin._retry_after == 0.0


def after_action(turn, proposal, *, outcome="changed", index=1):
    observation = turn.observation
    if outcome != "same_image":
        observation = replace(
            observation,
            frame=replace(
                observation.frame,
                id=f"observation-{index}",
                data=observation.frame.data + str(index).encode(),
            ),
            observed_after=time.time(),
        )
    events = turn.events
    if outcome != "missing":
        call = proposal.message.tool_calls[0]
        events += (
            ActionEvent(
                sequence=index,
                turn=index - 1,
                call_id=call.id,
                tool=call.name,
                arguments=call.arguments,
                is_error=outcome == "failed",
                executed=outcome != "refused",
                changed=None if outcome == "unknown" else outcome != "unchanged",
                observation_id=observation.id,
                result_text="tap result",
            ),
        )
    return replace(turn, observation=observation, events=events)


@pytest.mark.parametrize("shadow", ["active"], indirect=True)
@pytest.mark.parametrize(
    "outcome", ["missing", "failed", "refused", "unknown", "unchanged", "same_image"]
)
async def test_unverified_own_action_yields_for_recovery(
    scored_provider, turn, metadata, outcome
):
    active = scored_provider
    proposal = await active.plugin.advance(turn, metadata=metadata)
    following = after_action(turn, proposal, outcome=outcome)
    assert (
        await active.plugin.advance(following, metadata=replace(metadata, turn=1))
        is None
    )
    active.provider.evaluate.assert_awaited_once()
    assert active.events[-1]["evaluation"]["reason"] == (
        "unchanged_view" if outcome == "same_image" else "unverified_previous_action"
    )


@pytest.mark.parametrize("shadow", ["active"], indirect=True)
async def test_action_streak_yields_then_allows_a_new_bounded_run(
    scored_provider, turn, metadata
):
    active = scored_provider
    for index in range(3):
        proposal = await active.plugin.advance(
            turn, metadata=replace(metadata, turn=index)
        )
        assert proposal is not None
        turn = after_action(turn, proposal, index=index + 1)
    assert await active.plugin.advance(turn, metadata=replace(metadata, turn=3)) is None
    assert active.events[-1]["evaluation"]["reason"] == "action_streak_limit"
    assert active.provider.evaluate.call_count == 3
    assert (
        await active.plugin.advance(turn, metadata=replace(metadata, turn=4))
        is not None
    )


@pytest.mark.parametrize("shadow", ["active"], indirect=True)
@pytest.mark.parametrize("swallow", [False, True])
async def test_active_cancellation_cannot_return_a_proposal(
    scored_provider, turn, metadata, swallow
):
    entered = asyncio.Event()

    async def evaluate(request, **kw):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if swallow:
                return scored_answer(request)
            raise

    scored_provider.provider.evaluate.side_effect = evaluate
    pending = asyncio.create_task(
        scored_provider.plugin.advance(turn, metadata=metadata)
    )
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert calls(scored_provider)[0]["status"] == "cancelled"
    assert scored_provider.plugin._pending is None
    assert scored_provider.plugin._retry_after == 0.0
    assert scored_provider.plugin._factory is not None


@pytest.mark.parametrize("shadow", ["active"], indirect=True)
@pytest.mark.parametrize("swallow", [False, True])
async def test_active_timeout_yields_even_if_provider_returns_late(
    scored_provider, turn, metadata, swallow, monkeypatch
):
    loop = asyncio.get_running_loop()
    clock = [loop.time()]
    monkeypatch.setattr(loop, "time", lambda: clock[0])
    monkeypatch.setattr(
        "physiclaw.decision.plugin.time.time",
        lambda: turn.observation.observed_after,
    )

    async def evaluate(request, **kw):
        # Expire the real asyncio timeout only after all preflight guards pass
        # and the provider is entered, independent of Windows clock resolution.
        clock[0] = kw["deadline"] + 1.0
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if swallow:
                return scored_answer(request)
            raise

    scored_provider.provider.evaluate.side_effect = evaluate
    assert (
        await scored_provider.plugin.advance(
            turn,
            metadata=replace(metadata, deadline=clock[0] + 0.01),
        )
        is None
    )
    scored_provider.provider.evaluate.assert_awaited_once()
    record = calls(scored_provider)[0]
    assert record["error"] == "TimeoutError"
    assert "proposal_call_id" not in record


@pytest.mark.parametrize("shadow", ["active"], indirect=True)
@pytest.mark.parametrize("when", ["projection", "response"])
async def test_active_requires_writable_trace(scored_provider, turn, metadata, when):
    def write(event):
        if when == "projection" or event["event"] == "decision_call":
            raise OSError("private path")
        scored_provider.events.append(event)
        return True

    scored_provider.sink.write.side_effect = write
    assert await scored_provider.plugin.advance(turn, metadata=metadata) is None
    assert scored_provider.plugin._pending is None


@pytest.mark.parametrize("shadow", ["active"], indirect=True)
@pytest.mark.parametrize(
    "when,operation",
    [
        ("projection", "write"),
        ("projection", "flush"),
        ("projection", "image"),
        ("response", "write"),
        ("response", "flush"),
    ],
)
async def test_active_yields_when_real_trace_cannot_persist_evidence(
    scored_provider, turn, metadata, mocker, when, operation
):
    from physiclaw.agent.trace import Trace
    from physiclaw.agent.trace.store import Images

    images = Images("decision-write-failure")
    trace = Trace("decision-write-failure", images)
    file = trace._events
    writes = SimpleNamespace(
        write=mocker.Mock(wraps=file.write), flush=mocker.Mock(wraps=file.flush)
    )
    trace._events = writes

    def fail_persistence():
        if operation == "image":
            mocker.patch.object(images, "put", return_value="")
        else:
            getattr(writes, operation).side_effect = OSError("disk full")

    def evaluate(request, **kwargs):
        if when == "response":
            fail_persistence()
        return scored_answer(request)

    scored_provider.provider.evaluate.side_effect = evaluate
    plugin = DecisionPlugin()
    try:
        await plugin.session_setup(replace(scored_provider.services, events=trace))
        if when == "projection":
            fail_persistence()
        assert await plugin.advance(turn, metadata=metadata) is None
        assert plugin._pending is None
        assert await plugin.advance(turn, metadata=replace(metadata, turn=1)) is None
        assert scored_provider.provider.evaluate.await_count == (
            1 if when == "response" else 0
        )
    finally:
        await plugin.aclose()
        trace._events = file
        trace.close()
