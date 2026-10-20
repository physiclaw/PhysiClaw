"""P3: real tool ingress, independent inputs, arbitration and no-op parity."""

import asyncio
import base64
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest
from engine_fakes import (
    FakeProvider,
    _arbitration_run,
    _asst,
    _llm,
    _mk_run,
    _registry,
    _schemas,
    _tc,
    _turn_metadata,
)
from mcp.server.mcpserver import MCPServer

from physiclaw.agent.engine import engine, mcp_tool
from physiclaw.agent.engine.dispatch import dispatch
from physiclaw.agent.engine.facts import FactualState
from physiclaw.agent.engine.loop import _advance, drive
from physiclaw.agent.engine.provenance import PluginBinding
from physiclaw.agent.engine.session import Session
from physiclaw.agent.runtime.hook import Trigger
from physiclaw.agent.runtime.sentinel import DONE
from physiclaw.contract.decision import ActionCandidate
from physiclaw.contract.dto import AssistantMessage, ImageBlock, ToolResultMessage
from physiclaw.contract.observation import OBSERVATION_META, read_observation
from physiclaw.contract.plugin import PluginServices, ProposedTurn
from physiclaw.core.orchestration import GestureResult
from physiclaw.core.server import tools as tools_mod
from physiclaw.decision.plugin import DecisionPlugin
from physiclaw.macros.model import MACRO_OK_META


def tap_schema():
    return {
        "name": "tap",
        "input_schema": {
            "type": "object",
            "properties": {
                "bbox": {
                    "type": "array",
                    "minItems": 4,
                    "maxItems": 4,
                    "items": {"type": "number"},
                },
            },
            "required": ["bbox"],
        },
    }


async def test_real_server_client_and_dispatch_preserve_a_single_view(
    observation_blocks, mocker
):
    blocks = observation_blocks()
    jpeg = base64.b64decode(blocks[0]["data"])
    rig = SimpleNamespace(
        peek=mocker.Mock(return_value=(jpeg, blocks[1]["text"])),
        screenshot=mocker.Mock(return_value=(jpeg, blocks[1]["text"])),
        tap=mocker.Mock(
            return_value=GestureResult(
                "Tapped | screen: changed", jpeg, blocks[1]["text"]
            )
        ),
    )
    server = MCPServer("test")
    tools_mod.register(server, rig)
    mocker.patch.object(tools_mod, "save_tool_call")
    client = mcp_tool.McpClient()
    client._session = server
    run = _mk_run(
        mcp=client,
        schema_by_name={
            "peek": {"name": "peek", "input_schema": {"type": "object"}},
            "tap": tap_schema(),
        },
    )
    session = Session()

    first = await dispatch(run, session, _tc("peek", tcid="peek"), 0)
    before = session.facts.observation
    tapped = await dispatch(
        run, session, _tc("tap", {"bbox": [0.1, 0.2, 0.3, 0.4]}, tcid="tap"), 1
    )
    after = session.facts.observation

    assert before.id != after.id
    assert after.frame.data == jpeg
    assert after.icons[0].bbox == (0.1, 0.2, 0.3, 0.4)
    assert after.ocr[0].label == "搜索"
    assert before.source == after.source == "camera"
    assert after.captured_at is None
    assert not first.is_error and not tapped.is_error
    assert any(isinstance(block, ImageBlock) for block in first.content)
    assert [e.tool for e in session.facts.events] == ["peek", "tap"]
    assert session.facts.events[-1].changed is True
    assert (
        session.facts.events[-1].result_text
        == f"Tapped | screen: changed {tools_mod.HINT_VIEW_AFTER_TAP}"
    )
    assert session.facts.events[-1].observation_id == after.id
    rig.tap.assert_called_once_with([0.1, 0.2, 0.3, 0.4])
    screenshot = read_observation(await client.call_tool("screenshot", {}))
    assert screenshot.source == "screenshot"
    assert screenshot.id not in {before.id, after.id}


@pytest.mark.parametrize("history_enabled", [False, True])
async def test_four_pipeline_configurations_preserve_baseline_requests_and_actions(
    private_macro_run,
    mocker,
    observation_blocks,
    history_enabled,
):
    """For each playbook baseline, adding the real no-op decision changes nothing."""
    outcomes = []
    stamp = datetime(2026, 10, 19, 12, 0)
    mocker.patch.object(
        engine.assemble,
        "dt",
        SimpleNamespace(datetime=SimpleNamespace(now=lambda: stamp)),
    )
    no_decision_api = mocker.patch("physiclaw.provider.make_decision_provider")
    for facts_enabled in (False, True):
        requests = []

        class CaptureProvider(FakeProvider):
            async def chat(self, messages, tools, *, metadata=None):
                requests.append(deepcopy((messages, tools)))
                return await super().chat(messages, tools, metadata=metadata)

        peek = _asst(
            tool_calls=[
                _tc("note", {"summary": "observe"}, tcid="note-peek"),
                _tc("peek", tcid="model-peek"),
            ]
        )
        closing = _asst(
            tool_calls=[
                _tc("note", {"summary": "finished"}, tcid="note-end"),
                _tc("end_session", {"status": DONE, "recap": "finished"}, tcid="end"),
            ]
        )
        provider = CaptureProvider(([peek] if not history_enabled else []) + [closing])
        physical_calls = []

        async def call_tool(name, arguments):
            physical_calls.append((name, deepcopy(arguments)))
            return observation_blocks()

        mcp = SimpleNamespace(call_tool=call_tool)
        mocker.patch.object(engine, "get_mcp", new=mocker.AsyncMock(return_value=mcp))
        mocker.patch.object(engine, "make_chat_provider", return_value=provider)
        mocker.patch.object(
            engine,
            "list_tools_cached",
            new=mocker.AsyncMock(
                return_value=[{"name": "peek", "input_schema": {"type": "object"}}]
            ),
        )
        bindings = []
        if history_enabled:
            history = SimpleNamespace(
                session_setup=mocker.AsyncMock(return_value=None),
                advance=mocker.AsyncMock(
                    side_effect=[
                        ProposedTurn(
                            _asst(tool_calls=[_tc("peek", tcid="playbook-peek")])
                        ),
                        None,
                    ]
                ),
                aclose=mocker.AsyncMock(),
            )
            bindings.append(
                PluginBinding(
                    "playbook",
                    history,
                    "Playbook",
                    presentation="playbook",
                )
            )
        probe = DecisionPlugin()
        advance = mocker.spy(probe, "advance")
        setup = mocker.spy(probe, "session_setup")
        close = mocker.spy(probe, "aclose")
        if facts_enabled:
            bindings.append(PluginBinding("decision", probe, "Decision"))
        private_macro_run.load_plugins.return_value = tuple(bindings)

        outcome = await engine.run(
            [Trigger(description="private task and semantic context")],
            model_ref="fake/model",
        )

        assert outcome.status == DONE
        if facts_enabled:
            context = setup.call_args.args[0]
            assert isinstance(context, PluginServices)
            assert context.session_provider is provider
            assert not hasattr(context, "messages")
            snapshots = [call.args[0] for call in advance.call_args_list]
            assert len(snapshots) == (1 if history_enabled else 2)
            assert snapshots[-1].observation.ocr[0].label == "搜索"
            assert [e.tool for e in snapshots[-1].events if e.tool != "note"] == [
                "peek"
            ]
            assert not hasattr(snapshots[-1], "history")
            close.assert_awaited_once()
        if history_enabled:
            history.advance.assert_awaited()
            assert not hasattr(history.advance.call_args.args[0], "messages")
            history.aclose.assert_awaited_once()
        outcomes.append((outcome.status, physical_calls, requests))

    assert outcomes[0] == outcomes[1]
    no_decision_api.assert_not_called()


async def test_unknown_or_missing_view_invalidates_prior_evidence(
    observation_blocks, mocker
):
    state = FactualState()
    blocks = observation_blocks()
    state.observe(blocks, tool="peek")
    current = state.observation
    run = _mk_run(
        mcp=SimpleNamespace(
            call_tool=mocker.AsyncMock(
                return_value=[{"type": "text", "text": "view unavailable"}]
            )
        ),
        schema_by_name={"tap": tap_schema()},
    )
    session = Session(facts=state)

    result = await dispatch(run, session, _tc("tap", {"bbox": [0.1, 0.2, 0.3, 0.4]}), 1)

    assert not result.is_error
    assert current is not None and state.observation is None
    assert state.observation_issue == "awaiting_view"
    state.observe(blocks, tool="peek")
    assert state.observation is None and state.observation_issue == "invalid_view"


async def test_corrupt_observation_does_not_discard_actual_tool_result(
    observation_blocks, mocker
):
    blocks = observation_blocks()
    blocks[0]["_meta"][OBSERVATION_META]["listing_sha256"] = "wrong"
    run = _mk_run(
        mcp=SimpleNamespace(call_tool=mocker.AsyncMock(return_value=blocks)),
        schema_by_name={"peek": {"name": "peek", "input_schema": {"type": "object"}}},
    )
    session = Session()

    result = await dispatch(run, session, _tc("peek"), 0)

    assert not result.is_error
    assert any(isinstance(block, ImageBlock) for block in result.content)
    assert session.facts.observation is None
    assert session.facts.observation_issue == "invalid_view"


def test_execution_journal_retains_receipts_and_redacts_context_writes():
    facts = FactualState()
    facts.record(
        _tc("note", {"summary": "LLM reasoning"}), turn=0, changed=None, failed=False
    )
    for i in range(20):
        arguments = {"bbox": [0.1, 0.2, 0.3, 0.4]}
        facts.record(_tc("tap", arguments), turn=i, changed=None, failed=False)
        arguments["bbox"][0] = 0.9
    assert len(facts.events) == 21
    assert facts.events[0].arguments == {}
    assert "LLM reasoning" not in repr(facts.events)
    assert facts.events[0].sequence == 1
    assert facts.events[-1].arguments["bbox"][0] == 0.1


@pytest.mark.parametrize(
    "bad", ["stale", "target", "bbox", "schema", "tool", "wrong_return"]
)
async def test_invalid_fact_proposal_yields_before_dispatch(
    observation_blocks, mocker, bad
):
    session = Session()
    session.facts.observe(observation_blocks(), tool="peek")
    observation = session.facts.observation
    action = ActionCandidate(
        "tap",
        "Tap search",
        "tap",
        {"bbox": [0.1, 0.2, 0.3, 0.4]},
        observation.element_ref(observation.icons[0]),
    )
    if bad == "target":
        action = replace(action, target_ref="other:0")
    elif bad == "bbox":
        action = replace(action, arguments={"bbox": [0.5, 0.5, 0.6, 0.6]})
    elif bad == "schema":
        action = replace(action, arguments={})
    elif bad == "tool":
        action = replace(action, tool="not_a_tool")
    proposal = ProposedTurn.from_action(
        action, "old" if bad == "stale" else observation.id
    )
    if bad == "wrong_return":
        proposal = _asst(tool_calls=[_tc("tap", dict(action.arguments))])
    probe = SimpleNamespace(advance=mocker.AsyncMock(return_value=proposal))
    answer = _asst(tool_calls=[_tc("note"), _tc("peek")])
    chat = mocker.AsyncMock(return_value=answer)
    run = _arbitration_run(
        chat, plugins=(PluginBinding("decision", probe, "Decision"),)
    )
    run.schema_by_name = {"tap": tap_schema()}

    result = await _advance(run, session, _turn_metadata())

    assert result.binding.id == "physiclaw.llm" and result.message is answer
    chat.assert_awaited_once()
    assert not run.mcp.tool_calls


async def test_factual_proposal_is_one_host_bound_call_without_a_fabricated_note(
    observation_blocks, mocker
):
    session = Session()
    session.facts.observe(observation_blocks(), tool="peek")
    observed = session.facts.observation
    proposal = ProposedTurn.from_action(
        ActionCandidate(
            "option-a",
            "Tap icon",
            "tap",
            {"bbox": [0.1, 0.2, 0.3, 0.4]},
            observed.element_ref(observed.icons[0]),
        ),
        observed.id,
    )
    probe = SimpleNamespace(advance=mocker.AsyncMock(return_value=proposal))
    run = _arbitration_run(
        mocker.AsyncMock(),
        plugins=(PluginBinding("decision", probe, "Decision"),),
    )
    run.schema_by_name = {"tap": tap_schema()}

    result = await _advance(run, session, _turn_metadata())

    assert result.binding.id == "decision"
    assert result.message.tool_names() == ["tap"]
    assert result.message.content == ""
    assert result.message.tool_calls[0].id.startswith("call_")
    assert result.message.tool_calls[0].id != "option-a"
    run.plugins[-1].plugin.provider.chat.assert_not_awaited()


async def test_fact_plugin_mutation_and_exception_preserve_llm_fallback(
    observation_blocks, mocker
):
    session = Session()
    session.facts.observe(observation_blocks(), tool="peek")
    seen = []

    async def mutate(facts, *, metadata):
        seen.append(facts)
        facts.observation.elements[0].label = "changed"

    plugin = SimpleNamespace(advance=mocker.AsyncMock(side_effect=mutate))
    answer = _asst(tool_calls=[_tc("note"), _tc("peek")])
    chat = mocker.AsyncMock(return_value=answer)
    messages = [_asst(content="private main-model context")]
    run = _arbitration_run(
        chat,
        messages=messages,
        plugins=(PluginBinding("decision", plugin, "Decision"),),
    )

    result = await _advance(run, session, _turn_metadata())

    assert result.message is answer
    assert session.facts.observation.icons[0].label == ""
    assert not hasattr(seen[0], "history")
    chat.assert_awaited_once()
    assert chat.call_args.args[0][0] is messages[0]
    assert chat.call_args.kwargs["metadata"] == _turn_metadata()


async def test_transport_retry_does_not_repeat_fact_arbitration(mocker):
    from physiclaw.provider import ProviderTransientError

    plugin = SimpleNamespace(advance=mocker.AsyncMock(return_value=None))
    answer = _asst(tool_calls=[_tc("note"), _tc("peek")])
    chat = mocker.AsyncMock(side_effect=[ProviderTransientError("retry"), answer])
    run = _arbitration_run(
        chat,
        plugins=(PluginBinding("decision", plugin, "Decision"),),
        attempts=2,
    )

    result = await _advance(run, Session(), _turn_metadata())

    assert result.message is answer
    plugin.advance.assert_awaited_once()
    assert chat.await_count == 2


async def test_fact_arbitration_cancellation_does_not_call_llm(mocker):
    plugin = SimpleNamespace(
        advance=mocker.AsyncMock(side_effect=asyncio.CancelledError)
    )
    chat = mocker.AsyncMock()
    run = _arbitration_run(
        chat, plugins=(PluginBinding("decision", plugin, "Decision"),)
    )
    with pytest.raises(asyncio.CancelledError):
        await _advance(run, Session(), _turn_metadata())
    chat.assert_not_awaited()


@pytest.mark.parametrize("blocked", [False, True])
async def test_fact_action_uses_common_guards_and_remains_visible_to_llm(
    observation_blocks, mocker, blocked
):
    session = Session()
    session.facts.observe(observation_blocks(), tool="peek")
    observation = session.facts.observation
    proposal = ProposedTurn.from_action(
        ActionCandidate(
            "option",
            "Tap icon",
            "tap",
            {"bbox": [0.1, 0.2, 0.3, 0.4]},
            observation.element_ref(observation.icons[0]),
        ),
        observation.id,
    )
    probe = SimpleNamespace(advance=mocker.AsyncMock(side_effect=[proposal, None]))
    closing = _asst(
        tool_calls=[
            _tc("note", {"summary": "finished"}),
            _tc("end_session", {"status": DONE, "recap": "finished"}),
        ]
    )
    provider = FakeProvider([closing])
    registry = _registry()
    history = []
    run = _mk_run(
        provider,
        local_registry=registry,
        tool_schemas=_schemas(registry),
        schema_by_name={
            **{s["name"]: s for s in _schemas(registry)},
            "tap": tap_schema(),
        },
        session=session,
        messages=history,
    )
    run.plugins = (PluginBinding("decision", probe, "Decision"), *run.plugins)
    guard = mocker.patch.object(
        session.guard,
        "should_block",
        side_effect=lambda name, args: "blocked" if blocked and name == "tap" else None,
    )

    await drive(run, session)
    history = _llm(run).messages

    assert session.sentinel_status == DONE and session.model_turns == 1
    assert session.active_plugin is None
    guard.assert_any_call("tap", {"bbox": [0.1, 0.2, 0.3, 0.4]})
    assert run.mcp.tool_calls == (
        [] if blocked else [("tap", {"bbox": [0.1, 0.2, 0.3, 0.4]})]
    )
    assert isinstance(history[0], AssistantMessage)
    assert history[0].tool_names() == ["tap"]
    assert history[0].content == "" and history[0].raw == {}
    assert isinstance(history[1], ToolResultMessage)
    assert history[1].tool_call_id == history[0].tool_calls[0].id
    assert history[1].is_error is blocked


async def test_factual_view_precedes_llm_image_resizing(observation_blocks, mocker):
    from physiclaw.common.config import CONFIG
    from physiclaw.common.image import image_dimensions

    blocks = observation_blocks()
    mocker.patch.object(CONFIG.compact, "max_image_edge_px", 60)
    run = _mk_run(
        mcp=SimpleNamespace(call_tool=mocker.AsyncMock(return_value=blocks)),
        schema_by_name={"peek": {"name": "peek", "input_schema": {}}},
    )
    session = Session()

    result = await dispatch(run, session, _tc("peek"), 0)

    assert (
        session.facts.observation.frame.width,
        session.facts.observation.frame.height,
    ) == (80, 120)
    model_image = next(b for b in result.content if isinstance(b, ImageBlock))
    assert image_dimensions(base64.b64decode(model_image.data_b64)) == (40, 60)
    assert session.facts.observation.icons[0].bbox == (0.1, 0.2, 0.3, 0.4)


@pytest.mark.parametrize("ok", [False, None, True])
def test_only_verified_macro_results_can_supply_a_current_view(observation_blocks, ok):
    state = FactualState()
    blocks = [
        {"type": "text", "text": "macro summary", "_meta": {MACRO_OK_META: ok}},
        *observation_blocks(),
    ]

    state.observe(blocks, tool="run_macro")

    assert (state.observation is not None) is (ok is True)
    if ok is not True:
        assert state.observation_issue == "unverified_macro_view"
        state.observe(blocks[1:], tool="peek")
        assert state.observation is None
        assert state.observation_issue == "invalid_view"


def test_unrepresentable_action_evidence_does_not_escape_into_dispatch(
    observation_blocks,
):
    state = FactualState()
    state.observe(observation_blocks(), tool="peek")
    state.record(
        _tc("tap", {"unsupported": object()}), turn=0, changed=None, failed=False
    )
    assert state.observation is None
    assert state.observation_issue == "invalid_action_event"
    assert not state.events


@pytest.mark.parametrize("failure", ["setup", "close"])
async def test_fact_plugin_lifecycle_failure_preserves_llm_and_closes_once(
    private_macro_run, mocker, failure
):
    probe = SimpleNamespace(
        session_setup=mocker.AsyncMock(return_value=None),
        advance=mocker.AsyncMock(return_value=None),
        aclose=mocker.AsyncMock(),
    )
    getattr(
        probe, "session_setup" if failure == "setup" else "aclose"
    ).side_effect = RuntimeError("plugin failure")
    private_macro_run.load_plugins.return_value = (
        PluginBinding("decision", probe, "Decision"),
    )

    outcome = await engine.run([Trigger(description="task")], model_ref="fake/model")

    assert outcome.status == DONE
    assert isinstance(probe.session_setup.call_args.args[0], PluginServices)
    probe.aclose.assert_awaited_once()
    if failure == "setup":
        probe.advance.assert_not_awaited()


async def test_partial_sequence_result_is_not_reduced_to_transport_success(
    observation_blocks, mocker
):
    action = "1 tap ok — tapped\n2 long_press FAIL (stylus offline) | screen: changed"
    blocks = [{"type": "text", "text": action}, *observation_blocks()]
    run = _mk_run(
        mcp=SimpleNamespace(call_tool=mocker.AsyncMock(return_value=blocks)),
        schema_by_name={
            "sequence": {"name": "sequence", "input_schema": {"type": "object"}}
        },
    )
    session = Session()

    result = await dispatch(run, session, _tc("sequence", {"actions": []}), 0)

    assert not result.is_error
    event = session.facts.events[-1]
    assert not event.is_error and event.changed is True
    assert event.result_text == action
    assert event.observation_id == session.facts.observation.id


async def test_result_observer_failure_records_one_actual_attempt(
    observation_blocks, mocker
):
    observer = SimpleNamespace(
        observe=mocker.Mock(side_effect=[RuntimeError("observer failed"), None])
    )
    run = _mk_run(
        mcp=SimpleNamespace(
            call_tool=mocker.AsyncMock(return_value=observation_blocks())
        ),
        schema_by_name={"peek": {"name": "peek", "input_schema": {"type": "object"}}},
    )
    run.policies.result_observers = [observer]
    session = Session()

    result = await dispatch(run, session, _tc("peek"), 0)

    assert result.is_error
    run.mcp.call_tool.assert_awaited_once()
    assert len(session.facts.events) == 1
    assert session.facts.events[0].is_error
    assert "observer failed" in session.facts.events[0].result_text


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("note", {"summary": ["private reasoning\nwith context\\and quotes'"]}),
        (
            "update_progress",
            {"steps": [{"content": "one", "status": "private\nreasoning"}]},
        ),
    ],
)
async def test_context_write_errors_never_expose_escaped_private_values(
    tool, arguments
):
    from physiclaw.agent.engine.builtin_tool import build_registry

    registry = build_registry({})
    run = _mk_run(local_registry=registry, tool_schemas=_schemas(registry))
    session = Session()

    result = await dispatch(run, session, _tc(tool, arguments), 0)

    assert result.is_error
    receipt = session.facts.events[-1]
    assert receipt.arguments == {} and receipt.result_text == "context update failed"
    assert receipt.executed is False
    assert "private" not in repr(receipt)


async def test_journal_keeps_receipts_but_only_latest_image(observation_blocks, mocker):
    run = _mk_run(
        mcp=SimpleNamespace(
            call_tool=mocker.AsyncMock(
                side_effect=[observation_blocks(), observation_blocks()]
            )
        ),
        schema_by_name={"peek": {"input_schema": {"type": "object"}}},
    )
    session = Session()
    await dispatch(run, session, _tc("peek"), 0)
    first = session.facts.snapshot(schemas={})

    await dispatch(run, session, _tc("peek"), 1)

    assert len(session.facts.events) == 2
    assert any(block["type"] == "image" for block in first.events[0].result_blocks)
    assert not any(
        block["type"] == "image" for block in session.facts.events[0].result_blocks
    )
    assert any(
        block["type"] == "image" for block in session.facts.events[1].result_blocks
    )
