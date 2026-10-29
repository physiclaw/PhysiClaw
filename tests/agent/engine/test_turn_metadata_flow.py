"""One metadata object per host turn, across slots and transport retries."""

from copy import deepcopy
from types import SimpleNamespace

from engine_fakes import (
    FakeProvider,
    _arbitration_run,
    _asst,
    _llm,
    _mk_run,
    _registry,
    _schemas,
    _settings,
    _tc,
    _turn_metadata,
)

from physiclaw.agent.engine.loop import _advance, drive
from physiclaw.agent.engine.provenance import PluginBinding
from physiclaw.agent.engine.session import Session
from physiclaw.agent.runtime.sentinel import DONE
from physiclaw.common.provider_errors import ProviderTransientError
from physiclaw.contract.dto import AssistantMessage, ToolResultMessage, UserMessage
from physiclaw.contract.plugin import ProposedTurn


async def test_slots_and_llm_retries_share_metadata_without_sharing_contexts(mocker):
    history = SimpleNamespace(advance=mocker.AsyncMock(return_value=None))
    facts = SimpleNamespace(advance=mocker.AsyncMock(return_value=None))
    answer = _asst(tool_calls=[_tc("note"), _tc("peek")])
    chat = mocker.AsyncMock(side_effect=[ProviderTransientError("retry"), answer])
    messages = [UserMessage(content="private semantic context")]
    run = _arbitration_run(
        chat,
        messages=messages,
        plugins=(
            PluginBinding("history", history, "History"),
            PluginBinding("facts", facts, "Facts"),
        ),
        attempts=2,
    )
    llm_input = mocker.spy(run.plugins[-1].plugin, "advance")
    metadata = _turn_metadata()

    result = await _advance(run, Session(), metadata)

    assert result.message is answer
    assert history.advance.call_args.args[0] is facts.advance.call_args.args[0]
    assert llm_input.call_args.args[0] is facts.advance.call_args.args[0]
    assert not hasattr(facts.advance.call_args.args[0], "history")
    calls = [
        history.advance.call_args,
        facts.advance.call_args,
        *chat.call_args_list,
    ]
    assert len(calls) == 4
    assert all(call.kwargs["metadata"] is metadata for call in calls)
    assert all(call.args[0][0] is messages[0] for call in chat.call_args_list)
    assert all(call.args[0] is not messages for call in chat.call_args_list)
    assert metadata.turn == 0


async def test_turns_start_at_zero_and_count_plugin_turns_before_llm(mocker):
    closing = _asst(
        tool_calls=[
            _tc("note", {"summary": "finished"}),
            _tc("end_session", {"status": DONE, "recap": "finished"}),
        ]
    )
    provider = FakeProvider([closing])
    chat = mocker.spy(provider, "chat")
    wire = mocker.spy(provider, "serialize_history")
    history = SimpleNamespace(
        advance=mocker.AsyncMock(
            side_effect=[ProposedTurn(_asst(tool_calls=[_tc("peek")])), None]
        )
    )
    facts = SimpleNamespace(advance=mocker.AsyncMock(return_value=None))
    registry = _registry()
    session = Session()
    run = _mk_run(
        provider,
        local_registry=registry,
        tool_schemas=_schemas(registry),
        settings=_settings(max_turns=3),
        session=session,
        messages=[],
    )
    run.plugins = (
        PluginBinding("history", history, "History"),
        PluginBinding("facts", facts, "Facts"),
        *run.plugins,
    )

    await drive(run, session)

    assert session.sentinel_status == DONE
    seen = [call.kwargs["metadata"] for call in history.advance.call_args_list]
    assert [metadata.turn for metadata in seen] == [0, 1]
    assert [metadata.turns_remaining for metadata in seen] == [3, 2]
    assert all(metadata.session_id == session.session_id for metadata in seen)
    assert facts.advance.call_args.kwargs["metadata"] is seen[1]
    assert chat.call_args.kwargs["metadata"] is seen[1]
    assert wire.call_args.kwargs["metadata"] is seen[1]


async def test_llm_is_an_ordinary_first_winning_slot(mocker):
    """Reordering a real LLM producer requires no change in the engine."""
    closing = _asst(tool_calls=[_tc("note"), _tc("end_session", {"status": DONE})])
    provider = FakeProvider([closing])
    registry = _registry()
    session = Session()
    run = _mk_run(
        provider,
        local_registry=registry,
        tool_schemas=_schemas(registry),
        session=session,
        messages=[],
    )
    later = SimpleNamespace(advance=mocker.AsyncMock())
    run.plugins = (*run.plugins, PluginBinding("later", later, "Later"))

    await drive(run, session)

    assert session.sentinel_status == DONE
    assert len(provider.calls) == 1
    later.advance.assert_not_awaited()


async def test_llm_resumes_after_twenty_plugin_turns_with_independent_contexts(mocker):
    first = _asst(content="private LLM content", tool_calls=[_tc("note"), _tc("peek")])
    closing = _asst(tool_calls=[_tc("note"), _tc("end_session", {"status": DONE})])

    class CaptureProvider(FakeProvider):
        async def chat(self, messages, tools, *, metadata):
            self.requests = getattr(self, "requests", []) + [deepcopy(messages)]
            return await super().chat(messages, tools, metadata=metadata)

    provider = CaptureProvider([first, closing])
    registry = _registry()
    session = Session()
    run = _mk_run(
        provider,
        local_registry=registry,
        tool_schemas=_schemas(registry),
        settings=_settings(max_turns=25),
        session=session,
        messages=[],
    )
    scripted = SimpleNamespace(
        advance=mocker.AsyncMock(
            side_effect=[
                None,
                *[
                    ProposedTurn(
                        _asst(
                            content="private plugin content",
                            tool_calls=[
                                _tc("note", {"summary": "private plugin reasoning"}),
                                _tc("peek"),
                            ],
                        )
                    )
                    for _ in range(20)
                ],
                None,
            ]
        )
    )
    run.plugins = (PluginBinding("routine", scripted, "Routine"), *run.plugins)

    await drive(run, session)

    assert session.sentinel_status == DONE
    assert len(provider.calls) == 2
    resumed = provider.requests[1]
    assert "private plugin" not in repr(resumed)
    assert "private LLM content" not in repr(scripted.advance.call_args.args[0])
    assert "private plugin reasoning" not in repr(session.facts.events)
    pending = set()
    for message in resumed:
        if isinstance(message, AssistantMessage):
            assert not pending
            pending = {call.id for call in message.tool_calls}
        elif isinstance(message, ToolResultMessage):
            assert message.tool_call_id in pending
            pending.remove(message.tool_call_id)
    assert not pending
    assert {call.id for call in first.tool_calls} <= {
        message.tool_call_id
        for message in resumed
        if isinstance(message, ToolResultMessage)
    }


async def test_llm_host_rejection_does_not_leave_orphaned_tool_calls(mocker):
    from physiclaw.agent.engine import assemble
    from physiclaw.agent.runtime.hook import Trigger
    from physiclaw.contract.dto import CollapsePolicy

    rejected = _asst(
        tool_calls=[
            _tc(
                "note",
                {"summary": "checkpoint", "scratchpad": "banked"},
                tcid="duplicate",
            ),
            _tc("peek", tcid="duplicate"),
        ]
    )
    closing = _asst(tool_calls=[_tc("note"), _tc("end_session", {"status": DONE})])
    provider = FakeProvider([rejected, closing])
    provider.COLLAPSE = CollapsePolicy(first_at=3, keep=1, interval=1)
    chat = mocker.spy(provider, "chat")
    registry = _registry()
    session = Session()
    messages = assemble.build_initial_messages(
        [Trigger(description="resume")], "system"
    )
    run = _mk_run(
        provider,
        local_registry=registry,
        tool_schemas=_schemas(registry),
        session=session,
        messages=messages,
    )
    fallback = SimpleNamespace(
        advance=mocker.AsyncMock(
            return_value=ProposedTurn(_asst(tool_calls=[_tc("peek")]))
        )
    )
    run.plugins = (*run.plugins, PluginBinding("fallback", fallback, "Fallback"))

    values = [
        "first important result",
        "second important result",
        "third important result",
    ]
    for value in values:
        session.facts.record(
            _tc("read_clipboard"), turn=0, changed=None, failed=False, result_text=value
        )

    await drive(run, session)
    messages = _llm(run).messages

    assert session.sentinel_status == DONE
    assert len(provider.calls) == 2
    assert all(value in repr(chat.call_args_list[1].args[0]) for value in values)
    fallback.advance.assert_awaited_once()
    assert not any(
        isinstance(message, AssistantMessage)
        and any(call.id == "duplicate" for call in message.tool_calls)
        for message in messages
    )


async def test_fresh_plugin_receipts_reach_llm_before_compaction(mocker):
    from physiclaw.agent.engine import assemble
    from physiclaw.agent.runtime.hook import Trigger
    from physiclaw.contract.dto import CollapsePolicy

    initial = assemble.build_initial_messages([Trigger(description="resume")], "system")
    previous = _asst(
        tool_calls=[_tc("note", {"summary": "earlier summary"}), _tc("peek")]
    )
    initial.extend(
        [
            previous,
            *(
                ToolResultMessage(tool_call_id=call.id, content="ok")
                for call in previous.tool_calls
            ),
        ]
    )
    closing = _asst(tool_calls=[_tc("note"), _tc("end_session", {"status": DONE})])
    provider = FakeProvider([closing])
    provider.COLLAPSE = CollapsePolicy(first_at=3, keep=1, interval=1)
    chat = mocker.spy(provider, "chat")
    session = Session()
    run = _mk_run(provider, session=session, messages=initial)
    values = ["first item costs 30", "second item costs 20", "delivery takes 2 days"]
    for value in values:
        session.facts.record(
            _tc("read_clipboard"), turn=0, changed=None, failed=False, result_text=value
        )

    result = await _llm(run).advance(
        session.facts.snapshot(schemas={}), metadata=_turn_metadata(1)
    )

    assert result.message is closing
    request = chat.call_args.args[0]
    assert all(value in repr(request) for value in values)
    assert not session.collapsed_once


async def test_wire_log_keeps_corrective_replies_without_duplicate_acceptance():
    bad = _asst(tool_calls=[_tc("peek")])
    bad.raw = {"id": "malformed"}
    closing = _asst(tool_calls=[_tc("note"), _tc("end_session", {"status": DONE})])
    closing.raw = {"id": "accepted"}
    provider = FakeProvider([bad, closing])
    session = Session()
    registry = _registry()
    run = _mk_run(
        provider,
        session=session,
        local_registry=registry,
        tool_schemas=_schemas(registry),
        settings=_settings(max_turns=1),
    )

    await drive(run, session)

    assert session.sentinel_status == DONE
    assert run.rlog.write_request.call_count == 2
    assert [call.args[1]["id"] for call in run.rlog.write_response.call_args_list] == [
        "malformed",
        "accepted",
    ]
    accepted = [
        call.args[0]
        for call in run.tr.write.call_args_list
        if call.args[0]["event"] == "response"
    ]
    assert len(accepted) == 1 and accepted[0]["producer"] == {
        "plugin_id": "physiclaw.llm"
    }
    assert accepted[0]["turn"] == 0


async def test_plugin_usage_is_stamped_with_current_host_turn():
    import json
    import time

    from physiclaw.agent.trace import Trace
    from physiclaw.agent.trace.store import Images
    from physiclaw.common import paths
    from physiclaw.provider.chat.base import BaseChatProvider

    trace = Trace("plugin-usage", images=Images("plugin-usage"))
    account = SimpleNamespace(usage_sink=trace, ref="fake/micro")

    class Plugin:
        async def advance(self, turn, *, metadata):
            BaseChatProvider._account(account, "micro", time.perf_counter(), _asst())
            return ProposedTurn(_asst(tool_calls=[_tc("peek")]))

    registry = _registry()
    run = _mk_run(
        tr=trace,
        local_registry=registry,
        tool_schemas=_schemas(registry),
        settings=_settings(max_turns=2),
    )
    run.plugins = (PluginBinding("micro", Plugin(), "Micro"),)
    try:
        await drive(run, Session())
    finally:
        trace.close()

    events = [
        json.loads(line)
        for line in (paths.sessions_dir() / "plugin-usage" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["turn"] for event in events if event["event"] == "usage"] == [0, 1]
