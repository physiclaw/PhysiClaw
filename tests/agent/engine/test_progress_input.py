"""P4 shares existing outputs without changing how the LLM plans or acts."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from engine_fakes import (
    FakeProvider,
    _arbitration_run,
    _asst,
    _mk_run,
    _settings,
    _tc,
    _turn_metadata,
)

from physiclaw.agent.engine.builtin_tool import build_registry, schemas
from physiclaw.agent.engine.dispatch import dispatch
from physiclaw.agent.engine.loop import _advance, drive
from physiclaw.agent.engine.plan import Plan
from physiclaw.agent.engine.provenance import PluginBinding
from physiclaw.agent.engine.session import Session
from physiclaw.agent.runtime.sentinel import DONE
from physiclaw.contract.dto import UserMessage
from physiclaw.contract.plugin import PluginServices
from physiclaw.decision.plugin import DecisionPlugin


def test_snapshot_is_detached_and_excludes_private_interpretation():
    plan = Plan()
    assert plan.runtime_snapshot() is None
    plan.update(understanding="private interpretation only")
    assert plan.runtime_snapshot() is None
    plan.update(steps=[{"content": "打开微信", "status": "in_progress"}])
    snapshot = plan.runtime_snapshot()
    assert snapshot.user_said is None
    assert snapshot.steps[0].content == "打开微信"
    assert "private" not in repr(snapshot)
    plan.steps[0].content = "changed"
    plan.update(user_said="actual request")
    assert snapshot.steps[0].content == "打开微信"
    assert snapshot.user_said is None


def test_task_only_snapshot_does_not_publish_seed_instructions():
    plan = Plan()
    plan.update(user_said="actual request")
    snapshot = plan.runtime_snapshot()
    assert snapshot.user_said == "actual request"
    assert snapshot.steps == ()


async def test_snapshot_is_identical_for_all_slots_and_llm_keeps_own_prompt(mocker):
    session = Session()
    session.plan.update(
        user_said="buy the requested item",
        understanding="private interpretation",
        steps=[{"content": "打开淘宝", "status": "in_progress"}],
    )
    earlier = SimpleNamespace(advance=mocker.AsyncMock(return_value=None))
    decision = DecisionPlugin()
    decision_advance = mocker.spy(decision, "advance")
    chat = mocker.AsyncMock(return_value=_asst(tool_calls=[_tc("note"), _tc("peek")]))
    run = _arbitration_run(
        chat, session=session, messages=[UserMessage(content="private original prompt")]
    )
    llm_advance = mocker.spy(run.plugins[-1].plugin, "advance")
    run.plugins = (
        PluginBinding("earlier", earlier, "Earlier"),
        PluginBinding("decision", decision, "Decision"),
        *run.plugins,
    )

    await _advance(run, session, _turn_metadata())

    shared = earlier.advance.call_args.args[0]
    assert shared is decision_advance.call_args.args[0]
    assert shared is llm_advance.call_args.args[0]
    assert shared.progress.steps[0].content == "打开淘宝"
    assert "private interpretation" not in repr(shared)
    assert "private original prompt" not in repr(shared)
    assert chat.call_args.args[0][0].content == "private original prompt"
    assert "private interpretation" in repr(chat.call_args.args[0])
    assert all(
        call.kwargs["metadata"].turn == 0
        for call in [
            earlier.advance.call_args,
            decision_advance.call_args,
            llm_advance.call_args,
        ]
    )


async def test_rejected_progress_update_leaves_shared_snapshot_unchanged():
    session = Session()
    session.plan.update(
        user_said="old request",
        steps=[{"content": "old step", "status": "in_progress"}],
    )
    before = session.plan.runtime_snapshot()
    registry = build_registry({})
    run = _mk_run(local_registry=registry, tool_schemas=schemas(registry))

    result = await dispatch(
        run,
        session,
        _tc(
            "update_progress",
            {
                "user_said": "new request",
                "steps": [
                    {"content": "one", "status": "in_progress"},
                    {"content": "two", "status": "in_progress"},
                ],
            },
        ),
        0,
    )

    assert result.is_error
    assert session.plan.runtime_snapshot() == before
    assert session.facts.events[-1].arguments == {}


@pytest.mark.parametrize("content", ["", " "])
async def test_unusable_progress_cannot_break_existing_llm_workflow(content, mocker):
    session = Session()
    session.plan.update(
        user_said="real request", steps=[{"content": content, "status": "in_progress"}]
    )
    assert session.plan.runtime_snapshot() is None
    chat = mocker.AsyncMock(return_value=_asst(tool_calls=[_tc("note"), _tc("peek")]))
    run = _arbitration_run(chat, session=session)
    await _advance(run, session, _turn_metadata())
    chat.assert_awaited_once()


async def test_real_workflow_and_requests_match_with_and_without_decision_slot(
    observation_blocks, mocker
):
    responses = [
        _asst(
            tool_calls=[
                _tc(
                    "note",
                    {
                        "summary": "private checkpoint",
                        "scratchpad": "private scratchpad",
                    },
                    tcid="note-0",
                ),
                _tc(
                    "update_progress",
                    {
                        "user_said": "按小王的微信要求买东西",
                        "understanding": "private interpretation",
                        "steps": [
                            {"content": "打开淘宝", "status": "in_progress"},
                            {"content": "比较商品", "status": "pending"},
                        ],
                    },
                    tcid="progress-0",
                ),
            ]
        ),
        _asst(
            tool_calls=[
                _tc("note", {"summary": "look at current screen"}, tcid="note-1"),
                _tc("peek", tcid="peek-1"),
            ]
        ),
        _asst(
            tool_calls=[
                _tc("note", {"summary": "finished"}, tcid="note-2"),
                _tc("end_session", {"status": DONE, "recap": "finished"}, tcid="end-2"),
            ]
        ),
    ]
    blocks = observation_blocks()
    results = []
    for enabled in (False, True):
        session = Session()
        provider = FakeProvider(deepcopy(responses))
        requests = []
        original_chat = provider.chat

        async def capture(messages, tools, *, metadata):
            requests.append((deepcopy(messages), deepcopy(tools)))
            return await original_chat(messages, tools, metadata=metadata)

        provider.chat = capture
        registry = build_registry({})
        mcp = SimpleNamespace(call_tool=mocker.AsyncMock(return_value=deepcopy(blocks)))
        run = _mk_run(
            provider,
            session=session,
            messages=[UserMessage(content="private initial prompt")],
            mcp=mcp,
            local_registry=registry,
            tool_schemas=[
                *schemas(registry),
                *(
                    {"name": name, "input_schema": {"type": "object"}}
                    for name in ("peek", "tap", "long_press")
                ),
            ],
            settings=_settings(max_turns=5),
        )
        plugin = DecisionPlugin()
        events = SimpleNamespace(write=mocker.Mock())
        if enabled:
            await plugin.session_setup(PluginServices(provider, events=events))
            run.plugins = (
                PluginBinding("physiclaw.decision", plugin, "Decision"),
                *run.plugins,
            )
        await drive(run, session)
        assert session.sentinel_status == DONE
        assert session.plan.current_step() == "打开淘宝"
        assert mcp.call_tool.call_args_list == [mocker.call("peek", {})]
        results.append(requests)
        if enabled:
            assert len(events.write.call_args_list) == 3
            data = events.write.call_args.args[0]["projection"]
            preview = json.dumps(data, ensure_ascii=False)
            assert data["state"]["facts"]["current_step"]["text"] == "打开淘宝"
            assert "private initial prompt" not in preview
            assert "private scratchpad" not in preview
            assert "private interpretation" not in preview
            assert data["mode"] == "projection_only"
            assert data["blockers"] == []
            questions = data["profile"]["questions"]
            assert set(questions) == {"phase", "action_type", "target"}
            assert list(questions["action_type"]["options"]) == [
                "YIELD",
                "tap",
                "long_press",
            ]
            assert list(questions["target"]["options"]) == ["NONE", "b0", "b1"]
    assert results[0] == results[1]
