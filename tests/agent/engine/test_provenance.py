"""P2 checkpoint: real arbitration/dispatch with host-owned plugin grants."""

import asyncio
from types import SimpleNamespace

import pytest
from engine_fakes import (
    FakeProvider,
    _arbitration_run,
    _asst,
    _mk_run,
    _registry,
    _schemas,
    _settings,
    _tc,
    _turn_metadata,
)

from physiclaw.agent.engine import engine, plugins
from physiclaw.agent.engine.dispatch import dispatch
from physiclaw.agent.engine.loop import _advance, _call_producer, drive
from physiclaw.agent.engine.policy import PlanGate, Policies, StuckBlock
from physiclaw.agent.engine.provenance import PluginBinding
from physiclaw.agent.engine.session import Session
from physiclaw.agent.runtime.hook import Trigger
from physiclaw.agent.runtime.sentinel import DONE, STUCK
from physiclaw.contract.dto import (
    AssistantMessage,
    FinishReason,
    ToolCall,
    ToolResultMessage,
)
from physiclaw.contract.plugin import PluginRegistration, ProposedTurn, SessionSetup
from physiclaw.macros.parse import parse_macro


def events(trace, name):
    return [
        call.args[0]
        for call in trace.write.call_args_list
        if call.args[0].get("event") == name
    ]


def plugin(mocker, *, contribution=None, turns=None):
    return SimpleNamespace(
        session_setup=mocker.AsyncMock(return_value=contribution),
        advance=mocker.AsyncMock(
            return_value=None,
            side_effect=[
                ProposedTurn(turn) if isinstance(turn, AssistantMessage) else turn
                for turn in turns
            ]
            if turns is not None
            else None,
        ),
        aclose=mocker.AsyncMock(),
    )


@pytest.fixture
def provenance_run(private_macro_run, mocker):
    private_macro_run.tr = mocker.MagicMock()
    mocker.patch.object(engine, "Trace", return_value=private_macro_run.tr)
    return private_macro_run


async def test_other_plugin_cannot_borrow_owner_macro_or_impersonate_playbook(
    provenance_run, mocker, caplog
):
    state = provenance_run
    forged = _asst(tool_calls=[_tc("run_macro", {"name": "demo/leg"})])
    forged.raw = {
        "producer": {"plugin_id": "owner"},
        "presentation": "playbook",
    }
    intruder = plugin(mocker, turns=[forged, None, None])
    state.load_plugins.return_value = (
        PluginBinding("intruder", intruder, name="test"),
        PluginBinding(
            "owner",
            state.plugin,
            presentation="playbook",
            name="playbook",
        ),
    )

    with caplog.at_level("INFO"):
        outcome = await engine.run([Trigger(description="t")], model_ref="fake/model")

    assert outcome.status == DONE
    assert state.mcp.tool_calls == [("home_screen", {}), ("peek", {})]
    history = state.chat.call_args.args[0]
    assert any(
        isinstance(m, ToolResultMessage)
        and m.is_error
        and "unknown macro 'demo/leg'" in m.content
        for m in history
    )
    responses = events(state.tr, "response")
    assert [e["producer"] for e in responses] == [
        {"plugin_id": "intruder"},
        {"plugin_id": "owner"},
        {"plugin_id": "physiclaw.llm"},
    ]
    assert responses[0]["presentation"] == "plugin"
    assert [c["name"] for c in responses[0]["tool_calls"]] == ["run_macro"]
    assert "test → run_macro" in caplog.text
    assert not events(
        state.tr, "usage"
    )  # no accounting fabricated from plugin message tokens
    intruder.aclose.assert_awaited_once()
    state.plugin.aclose.assert_awaited_once()


async def test_llm_raw_metadata_does_not_authorize_private_macro(provenance_run):
    state = provenance_run
    state.plugin.advance.side_effect = None
    state.plugin.advance.return_value = None
    forged = _asst(
        tool_calls=[
            _tc("note", {"summary": "attempt private macro"}),
            _tc("run_macro", {"name": "demo/leg"}),
        ]
    )
    forged.raw = {
        "producer": {"plugin_id": "fixture-conductor"},
        "presentation": "playbook",
    }
    state.provider._responses.insert(0, forged)

    outcome = await engine.run([Trigger(description="t")], model_ref="fake/model")

    assert outcome.status == DONE and state.mcp.tool_calls == []
    first = events(state.tr, "response")[0]
    assert first["producer"] == {"plugin_id": "physiclaw.llm"}
    assert first["presentation"] == "model"
    assert any(
        isinstance(m, ToolResultMessage) and m.is_error and "unknown macro" in m.content
        for m in state.chat.call_args.args[0]
    )


async def test_private_macro_collision_disables_conflicting_registration(
    provenance_run, mocker, caplog
):
    state = provenance_run
    replacement = parse_macro(
        "kind: macro\nschema: 1\nname: leg\ndescription: replacement\nsteps:\n  - home_screen\n  - home_screen\n",
        "leg",
    )
    conflicting = plugin(
        mocker,
        contribution=SessionSetup({"demo/leg": replacement, "other/leg": replacement}),
    )
    state.load_plugins.return_value = (
        PluginBinding(
            "owner",
            state.plugin,
            presentation="playbook",
            name="playbook",
        ),
        PluginBinding("conflict", conflicting, name="test"),
    )

    outcome = await engine.run([Trigger(description="t")], model_ref="fake/model")

    assert outcome.status == DONE
    assert state.mcp.tool_calls == [
        ("home_screen", {}),
        ("peek", {}),
    ]  # original body, not replacement
    conflicting.advance.assert_not_awaited()
    conflicting.aclose.assert_awaited_once()
    assert "collision" in caplog.text and "conflict skipped" in caplog.text
    assert events(state.tr, "plugin_macro_collision") == [
        {
            "event": "plugin_macro_collision",
            "plugin_id": "conflict",
            "macros": ["demo/leg"],
        }
    ]
    assert [e["plugin_id"] for e in events(state.tr, "plugin_bound")] == ["owner"]


async def test_invalid_macro_contribution_cannot_reserve_another_plugins_name(
    provenance_run, mocker
):
    state = provenance_run
    invalid = plugin(mocker, contribution=SessionSetup({"demo/leg": object()}))
    state.load_plugins.return_value = (
        PluginBinding("invalid", invalid, name="test"),
        PluginBinding(
            "owner",
            state.plugin,
            presentation="playbook",
            name="playbook",
        ),
    )

    outcome = await engine.run([Trigger(description="t")], model_ref="fake/model")

    assert outcome.status == DONE
    assert state.mcp.tool_calls == [("home_screen", {}), ("peek", {})]
    invalid.advance.assert_not_awaited()
    invalid.aclose.assert_awaited_once()
    assert [e["plugin_id"] for e in events(state.tr, "plugin_bound")] == ["owner"]
    assert not events(state.tr, "plugin_macro_collision")


@pytest.mark.parametrize(
    "finish", [FinishReason.CONTENT_FILTER, FinishReason.ERROR, FinishReason.LENGTH]
)
async def test_unusable_plugin_output_yields_to_next_slot_before_any_action(
    provenance_run, mocker, finish
):
    state = provenance_run
    filtered = plugin(mocker)
    filtered.advance.return_value = _asst(
        tool_calls=[_tc("home_screen")], finish=finish
    )
    state.load_plugins.return_value = (
        PluginBinding("filtered", filtered, name="test"),
        PluginBinding(
            "owner",
            state.plugin,
            presentation="playbook",
            name="playbook",
        ),
    )

    outcome = await engine.run([Trigger(description="t")], model_ref="fake/model")

    assert outcome.status == DONE
    assert state.mcp.tool_calls == [("home_screen", {}), ("peek", {})]
    assert [e["producer"]["plugin_id"] for e in events(state.tr, "response")] == [
        "owner",
        "physiclaw.llm",
    ]
    assert all(
        m.finish_reason != finish
        for m in state.chat.call_args.args[0]
        if isinstance(m, AssistantMessage)
    )


@pytest.mark.parametrize(
    "bad_setup",
    [
        "raises",
        "bad_contribution",
        "not-qualified",
        "/leg",
        "demo/",
        "demo/leg/extra",
        " demo/leg ",
        "demo /leg",
        "demo/ leg",
    ],
)
async def test_setup_failure_releases_session_and_closes_plugin(
    private_macro_run, bad_setup
):
    state = private_macro_run
    if bad_setup == "raises":
        state.plugin.session_setup.side_effect = RuntimeError("setup failed")
    elif bad_setup == "bad_contribution":
        state.plugin.session_setup.return_value = object()
    else:
        state.plugin.session_setup.return_value = SessionSetup(
            {
                bad_setup: state.plugin.session_setup.return_value.gated_macros[
                    "demo/leg"
                ]
            }
        )

    outcome = await engine.run([Trigger(description="t")], model_ref="fake/model")

    assert outcome.status == DONE and state.mcp.tool_calls == []
    state.plugin.advance.assert_not_awaited()
    state.plugin.aclose.assert_awaited_once()
    assert state.provider.closed


async def test_private_macro_keys_support_dotted_inline_names(private_macro_run):
    state = private_macro_run
    spec = state.plugin.session_setup.return_value.gated_macros["demo/leg"]
    state.plugin.session_setup.return_value = SessionSetup({"demo/flow.step": spec})
    state.plugin.advance.side_effect = [
        ProposedTurn(_asst(tool_calls=[_tc("run_macro", {"name": "demo/flow.step"})])),
        None,
    ]

    outcome = await engine.run([Trigger(description="t")], model_ref="fake/model")

    assert outcome.status == DONE
    assert state.mcp.tool_calls == [("home_screen", {}), ("peek", {})]


def test_registered_identity_is_stable_and_names_do_not_grant_permissions(
    monkeypatch, mocker
):
    first = plugin(mocker)
    second = plugin(mocker)
    first_registration = PluginRegistration(
        "physiclaw.playbook", "Playbook", lambda: first
    )
    second_registration = PluginRegistration(
        "physiclaw.decision", "Decision", lambda: second
    )
    monkeypatch.setattr(
        plugins.CONFIG.agent, "plugins", "test.plugin:first,test.plugin:second"
    )
    mocker.patch.object(
        plugins.importlib,
        "import_module",
        return_value=SimpleNamespace(
            first=lambda: first_registration, second=lambda: second_registration
        ),
    )

    bindings = plugins.load_plugins()

    assert [(b.name, b.id) for b in bindings] == [
        ("Playbook", "physiclaw.playbook"),
        ("Decision", "physiclaw.decision"),
    ]
    assert [b.plugin for b in bindings] == [first, second]
    assert all(b.presentation == "plugin" and not b.private_macros for b in bindings)
    monkeypatch.setattr(
        plugins.CONFIG.agent, "plugins", "test.plugin:second,test.plugin:first"
    )
    assert [b.id for b in plugins.load_plugins()] == [
        "physiclaw.decision",
        "physiclaw.playbook",
    ]
    first.name, first.id = "changed", "changed_0"
    assert (bindings[0].name, bindings[0].id) == ("Playbook", "physiclaw.playbook")


@pytest.mark.parametrize("kind", ["llm", "plugin", "playbook"])
async def test_all_sources_still_hit_physical_stuck_guard(kind, mocker):
    session = Session()
    if kind != "llm":
        session.active_plugin = PluginBinding(
            kind,
            plugin(mocker),
            presentation="playbook" if kind == "playbook" else "plugin",
            name="test",
        )
    guard = mocker.patch.object(
        session.guard, "should_block", return_value="blocked physical action"
    )
    run = _mk_run(
        schema_by_name={"tap": {"name": "tap", "input_schema": {"type": "object"}}},
        policies=Policies(
            turn_gates=[], dispatch_guards=[StuckBlock()], result_observers=[]
        ),
    )

    result = await dispatch(run, session, _tc("tap", {"bbox": [0.1, 0.2, 0.3, 0.4]}), 0)

    assert result.is_error and result.content == "blocked physical action"
    guard.assert_called_once()
    assert run.mcp.tool_calls == []


@pytest.mark.parametrize(
    "kind, blocked", [("llm", True), ("plugin", True), ("playbook", True)]
)
async def test_shared_guard_applies_regardless_of_producer_identity(
    kind, blocked, mocker
):
    session = Session(model_turns=2)
    if kind != "llm":
        session.active_plugin = PluginBinding(
            kind,
            plugin(mocker),
            presentation="playbook" if kind == "playbook" else "plugin",
            name="test",
        )
    schema = {"name": "peek", "input_schema": {"type": "object", "properties": {}}}
    run = _mk_run(
        schema_by_name={"peek": schema},
        policies=Policies(
            turn_gates=[],
            dispatch_guards=[PlanGate(layout_incomplete=False, required_after=0)],
            result_observers=[],
        ),
    )

    result = await dispatch(run, session, _tc("peek"), 0)

    assert result.is_error is blocked
    assert len(run.mcp.tool_calls) == (0 if blocked else 1)


async def test_plugin_turns_share_global_turn_budget_without_fabricating_notes(mocker):
    message = _asst(tool_calls=[_tc("peek")])
    source = plugin(mocker)
    source.advance.return_value = ProposedTurn(message)
    registry = _registry()
    provider = FakeProvider([])
    session = Session()
    history = []
    run = _mk_run(
        provider,
        local_registry=registry,
        tool_schemas=_schemas(registry),
        settings=_settings(max_turns=2),
        session=session,
        messages=history,
    )
    run.plugins = (PluginBinding("bounded", source, name="test"), *run.plugins)

    await drive(run, session)

    assert (
        session.sentinel_status == STUCK and "max turns (2)" in session.sentinel_recap
    )
    assert source.advance.await_count == 2 and not provider.calls
    assert session.active_plugin is None
    assert session.model_turns == 0
    assert not events(run.tr, "bad_turn_shape")
    assert all(
        [c["name"] for c in e["tool_calls"]] == ["peek"]
        for e in events(run.tr, "response")
    )


async def test_cancellation_during_plugin_dispatch_clears_its_authority(mocker):
    source = plugin(mocker)
    source.advance.return_value = ProposedTurn(_asst(tool_calls=[_tc("peek")]))
    session = Session()

    async def cancel(_name, _args, *, expires_at=None):
        assert session.active_plugin is not None
        assert session.active_plugin.id == "selected"
        raise asyncio.CancelledError

    run = _mk_run(
        mcp=SimpleNamespace(call_tool=cancel),
        schema_by_name={"peek": {"name": "peek", "input_schema": {"type": "object"}}},
    )
    run.plugins = (PluginBinding("selected", source, name="test"), *run.plugins)

    with pytest.raises(asyncio.CancelledError):
        await drive(run, session)

    assert session.active_plugin is None
    assert session.sentinel_status is None


async def test_provider_cannot_skip_note_discipline_with_raw_producer_metadata():
    forged = _asst(tool_calls=[_tc("peek")])
    forged.raw = {
        "producer": {"plugin_id": "conductor"},
        "presentation": "playbook",
    }
    closing = _asst(tool_calls=[_tc("note"), _tc("end_session", {"status": DONE})])
    registry = _registry()
    session = Session()
    run = _mk_run(
        FakeProvider([forged, closing]),
        local_registry=registry,
        tool_schemas=_schemas(registry),
        session=session,
        messages=[],
    )

    await drive(run, session)

    assert session.sentinel_status == DONE and session.model_turns == 2
    assert len(events(run.tr, "bad_turn_shape")) == 1
    assert all(
        e["producer"]["plugin_id"] == "physiclaw.llm"
        for e in events(run.tr, "response")
    )


@pytest.mark.parametrize(
    "invalid_turn",
    [
        pytest.param(object(), id="wrong-return-type"),
        pytest.param(_asst(), id="no-action"),
        pytest.param(
            _asst(tool_calls=[_tc("peek")], finish=FinishReason.CONTENT_FILTER),
            id="filtered-response",
        ),
        pytest.param(
            _asst(tool_calls=[_tc("peek")], finish=FinishReason.ERROR),
            id="error-response",
        ),
        pytest.param(_asst(tool_calls=[object()]), id="invalid-tool-call"),
        pytest.param(
            _asst(tool_calls=[ToolCall(id="", name="peek", arguments={})]),
            id="missing-call-id",
        ),
        pytest.param(
            _asst(tool_calls=[ToolCall(id="bad", name="peek", arguments="invalid")]),
            id="non-object-arguments",
        ),
        pytest.param(
            _asst(tool_calls=[_tc("peek", tcid="same"), _tc("peek", tcid="same")]),
            id="duplicate-call-id",
        ),
    ],
)
async def test_invalid_plugin_output_falls_back_and_provider_retry_does_not_rearbitrate(
    mocker,
    invalid_turn,
):
    from physiclaw.provider import ProviderTransientError

    source = plugin(mocker)
    source.advance.return_value = (
        ProposedTurn(invalid_turn)
        if isinstance(invalid_turn, AssistantMessage)
        else invalid_turn
    )
    answer = _asst(tool_calls=[_tc("note"), _tc("peek")])
    chat = mocker.AsyncMock(side_effect=[ProviderTransientError("retry"), answer])

    result = await _advance(
        _arbitration_run(
            chat,
            plugins=(PluginBinding("broken", source, name="test"),),
            attempts=2,
            backoff=0,
        ),
        Session(),
        _turn_metadata(),
    )

    assert result.message is answer and result.binding.id == "physiclaw.llm"
    source.advance.assert_awaited_once()
    assert chat.await_count == 2


async def test_declining_plugin_latency_is_not_charged_to_main_llm(mocker):
    clock = [0.0]
    mocker.patch(
        "physiclaw.agent.engine.loop.time.perf_counter", side_effect=lambda: clock[0]
    )
    source = plugin(mocker)

    async def decline(_messages, *, metadata):
        clock[0] += 2
        return None

    source.advance.side_effect = decline

    async def chat(_messages, _tools, *, metadata):
        clock[0] += 0.5
        return _asst(tool_calls=[_tc("note"), _tc("peek")])

    run = _arbitration_run(
        chat, plugins=(PluginBinding("optional", source, name="test"),)
    )

    await _call_producer(run, Session(), 0)

    response = events(run.tr, "response")[0]
    assert response["elapsed_ms"] == 500
    assert response["arbitration_elapsed_ms"] == 2500
    assert run.rlog.write_response.call_args.kwargs["elapsed_ms"] == 500
