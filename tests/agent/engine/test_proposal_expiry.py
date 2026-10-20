"""Proposal expiry is generic start permission, never cancellation of an action."""

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
    patch_loop_tails,
)

from physiclaw.agent.engine import dispatch as dispatch_module
from physiclaw.agent.engine import loop, mcp_tool
from physiclaw.agent.engine.builtin_tool import LocalTool
from physiclaw.agent.engine.policy import Policies
from physiclaw.agent.engine.provenance import PluginBinding
from physiclaw.agent.engine.session import Session
from physiclaw.agent.runtime.sentinel import DONE
from physiclaw.contract.dto import ToolResultMessage
from physiclaw.contract.plugin import ProposedTurn


@pytest.fixture
def clock(mocker):
    clock = SimpleNamespace(now=100.0)
    timer = SimpleNamespace(monotonic=lambda: clock.now, perf_counter=lambda: clock.now)
    mocker.patch.object(loop, "time", timer)
    mocker.patch.object(dispatch_module, "time", timer)
    mocker.patch.object(mcp_tool, "monotonic", timer.monotonic)
    return clock


@pytest.mark.parametrize("expires_at", [99.0, 100.0])
async def test_expired_proposal_falls_through_with_same_input_and_metadata(
    mocker, clock, expires_at
):
    plugin = SimpleNamespace(
        advance=mocker.AsyncMock(
            return_value=ProposedTurn(
                _asst(tool_calls=[_tc("tap")]), expires_at=expires_at
            )
        )
    )
    answer = _asst(tool_calls=[_tc("note"), _tc("peek")])
    chat = mocker.AsyncMock(return_value=answer)
    run = _arbitration_run(
        chat, plugins=(PluginBinding("test.producer", plugin, "Test"),)
    )
    llm_advance = mocker.spy(_llm(run), "advance")
    metadata = _turn_metadata()

    result = await loop._advance(run, Session(), metadata)

    assert result.message is answer and result.expires_at is None
    assert llm_advance.call_args.args[0] is plugin.advance.call_args.args[0]
    assert llm_advance.call_args.kwargs["metadata"] is metadata
    assert not run.mcp.tool_calls


async def test_expiry_during_response_logging_refuses_tool_and_reaches_llm(
    mocker, clock
):
    patch_loop_tails(mocker)
    session = Session()
    registry = _registry()
    tap = mocker.AsyncMock(return_value="tapped")
    registry["tap"] = LocalTool("tap", "tap", {"type": "object"}, tap)
    proposal = ProposedTurn(_asst(tool_calls=[_tc("tap")]), expires_at=101.0)
    plugin = SimpleNamespace(advance=mocker.AsyncMock(side_effect=[proposal, None]))
    closing = _asst(
        tool_calls=[_tc("note"), _tc("end_session", {"status": DONE, "recap": "done"})]
    )
    provider = FakeProvider([closing])
    run = _mk_run(
        provider,
        session=session,
        local_registry=registry,
        tool_schemas=_schemas(registry),
        policies=Policies(turn_gates=[], dispatch_guards=[], result_observers=[]),
    )
    run.plugins = (PluginBinding("test.producer", plugin, "Test"), *run.plugins)

    def trace(event):
        if event["event"] == "response" and event["turn"] == 0:
            assert event["expires_at"] == 101.0
            clock.now = 101.0
        return True

    run.tr.write.side_effect = trace
    await loop.drive(run, session)

    tap.assert_not_awaited()
    assert session.sentinel_status == DONE
    receipt = session.facts.events[0]
    assert receipt.call_id == proposal.message.tool_calls[0].id
    assert receipt.is_error and not receipt.executed
    assert len(provider.calls) == 1
    assert any(
        isinstance(message, ToolResultMessage)
        and message.tool_call_id == receipt.call_id
        and message.is_error
        for message in _llm(run).messages
    )
    assert any(
        call.args[0]["event"] == "tool_blocked_expired"
        for call in run.tr.write.call_args_list
    )


async def test_expiry_between_calls_keeps_first_result_and_refuses_remaining_calls(
    mocker, clock
):
    async def handler(session, args):
        clock.now = 101.0
        return "finished normally"

    handler = mocker.AsyncMock(side_effect=handler)
    tool = LocalTool("peek", "peek", {"type": "object"}, handler)
    run = _mk_run(
        schema_by_name={"peek": {}},
        local_registry={"peek": tool},
        policies=Policies(turn_gates=[], dispatch_guards=[], result_observers=[]),
    )
    session = Session()
    calls = [_tc("peek"), _tc("peek"), _tc("peek")]

    successful = await loop._dispatch_turn(
        run, session, _asst(tool_calls=calls), 0, expires_at=101.0
    )

    assert successful == calls[:1]
    handler.assert_awaited_once()
    assert [event.call_id for event in session.facts.events] == [
        call.id for call in calls
    ]
    assert [event.executed for event in session.facts.events] == [True, False, False]
    assert [event.is_error for event in session.facts.events] == [False, True, True]


@pytest.mark.parametrize("expires_at", [None, 101.0])
async def test_local_logging_delay_is_checked_without_changing_unbounded_calls(
    mocker, clock, expires_at
):
    def slow_logging(*args, **kwargs):
        clock.now = 101.0

    mocker.patch.object(dispatch_module.log, "info", side_effect=slow_logging)
    handler = mocker.AsyncMock(return_value="tapped")
    tool = LocalTool("tap", "tap", {"type": "object"}, handler)
    run = _mk_run(
        schema_by_name={"tap": {}},
        local_registry={"tap": tool},
        policies=Policies(turn_gates=[], dispatch_guards=[], result_observers=[]),
    )
    session = Session()

    result = await dispatch_module.dispatch(
        run, session, _tc("tap"), 0, expires_at=expires_at
    )

    assert result.is_error is (expires_at is not None)
    assert handler.await_count == (1 if expires_at is None else 0)
    assert session.facts.events[0].executed is (expires_at is None)


async def test_reconnect_expiry_is_an_unexecuted_refusal_not_a_tool_failure(
    mocker, clock
):
    client = mcp_tool.McpClient("http://127.0.0.1:1")
    sdk = SimpleNamespace(call_tool=mocker.AsyncMock())
    client._session = sdk
    client._reconnect_pending = True

    async def reconnect():
        clock.now = 101.0

    mocker.patch.object(client, "_reconnect", side_effect=reconnect)
    observer = SimpleNamespace(observe=mocker.Mock(return_value=None))
    run = _mk_run(
        mcp=client,
        schema_by_name={"tap": {}},
        policies=Policies(
            turn_gates=[], dispatch_guards=[], result_observers=[observer]
        ),
    )
    session = Session()
    result = await dispatch_module.dispatch(
        run, session, _tc("tap"), 0, expires_at=101.0
    )

    sdk.call_tool.assert_not_awaited()
    observer.observe.assert_not_called()
    assert result.is_error and not session.facts.events[0].executed
    events = [call.args[0]["event"] for call in run.tr.write.call_args_list]
    assert "tool_blocked_expired" in events and "tool_error" not in events
