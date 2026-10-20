"""Decision projection and shadow calls preserve how the LLM plans and acts."""

import asyncio
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
from physiclaw.common.config import CONFIG
from physiclaw.contract.decision import (
    DecisionAnswer,
    DecisionCapabilities,
    DecisionResult,
)
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


@pytest.mark.parametrize("shadow_finishes", [False, True])
async def test_real_workflow_and_requests_match_with_decision_off_and_shadow(
    observation_blocks, mocker, monkeypatch, shadow_finishes
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
                _tc("note", {"summary": "tap the search field"}, tcid="note-2"),
                _tc("tap", {"bbox": [0.4, 0.2, 0.8, 0.4]}, tcid="tap-2"),
            ]
        ),
        _asst(
            tool_calls=[
                _tc("note", {"summary": "finished"}, tcid="note-3"),
                _tc("end_session", {"status": DONE, "recap": "finished"}, tcid="end-3"),
            ]
        ),
    ]
    blocks = observation_blocks()
    results = []
    for mode in ("disabled", "off", "shadow"):
        monkeypatch.setattr(CONFIG.decision, "model", "fake/shadow")
        monkeypatch.setattr(
            CONFIG.decision, "mode", "shadow" if mode == "shadow" else "off"
        )
        session = Session()
        provider = FakeProvider(deepcopy(responses))
        requests = []
        original_chat = provider.chat
        started, release, finished, cancelled = (asyncio.Event() for _ in range(4))

        async def evaluate(request, *, deadline):
            assert deadline > asyncio.get_running_loop().time()
            started.set()
            try:
                await release.wait()
                return DecisionResult(
                    request.id,
                    {
                        "phase": DecisionAnswer("ROUTINE", confidence=0.99),
                        "action_type": DecisionAnswer("tap", confidence=0.99),
                        "target": DecisionAnswer("b0", confidence=0.99),
                    },
                    "fake",
                    "shadow",
                    "shadow",
                )
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                finished.set()

        decision_provider = SimpleNamespace(
            api_kind="decision",
            capabilities=DecisionCapabilities(images=True),
            evaluate=mocker.AsyncMock(side_effect=evaluate),
            aclose=mocker.AsyncMock(),
        )
        decision_factory = mocker.Mock(return_value=decision_provider)

        async def capture(messages, tools, *, metadata):
            requests.append((deepcopy(messages), deepcopy(tools)))
            if mode == "shadow" and metadata.turn == 2:
                # The LLM reaches its normal call while shadow inference waits.
                await asyncio.wait_for(started.wait(), 1)
                if shadow_finishes:
                    release.set()
                    await asyncio.wait_for(finished.wait(), 1)
            return await original_chat(messages, tools, metadata=metadata)

        provider.chat = capture
        registry = build_registry({})
        mcp = SimpleNamespace(
            call_tool=mocker.AsyncMock(
                side_effect=[
                    deepcopy(blocks),
                    [{"type": "text", "text": "tap completed"}],
                ]
            )
        )
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
        if mode != "disabled":
            await plugin.session_setup(
                PluginServices(
                    provider,
                    events=events,
                    decision_provider_factory=decision_factory,
                )
            )
            run.plugins = (
                PluginBinding("physiclaw.decision", plugin, "Decision"),
                *run.plugins,
            )
        try:
            await drive(run, session)
        finally:
            await plugin.aclose()
        assert session.sentinel_status == DONE
        assert session.plan.current_step() == "打开淘宝"
        assert mcp.call_tool.call_args_list == [
            mocker.call("peek", {}),
            mocker.call("tap", {"bbox": [0.4, 0.2, 0.8, 0.4]}),
        ]
        results.append(requests)
        if mode == "shadow":
            projected = [
                call.args[0]
                for call in events.write.call_args_list
                if call.args[0]["event"] == "decision_projection"
            ]
            assert len(projected) == 4
            data = projected[2]["projection"]
            preview = json.dumps(data, ensure_ascii=False)
            assert data["state"]["facts"]["current_step"]["text"] == "打开淘宝"
            assert "private initial prompt" not in preview
            assert "private scratchpad" not in preview
            assert "private interpretation" not in preview
            assert data["blockers"] == []
            questions = data["profile"]["questions"]
            assert set(questions) == {"phase", "action_type", "target"}
            assert list(questions["action_type"]["options"]) == ["YIELD", "tap"]
            assert list(questions["target"]["options"]) == ["NONE", "b0", "b1"]
            decision_provider.evaluate.assert_awaited_once()
            decision_provider.aclose.assert_awaited_once()
            assert cancelled.is_set() is not shadow_finishes
            decision_request = decision_provider.evaluate.call_args.args[0]
            facts = decision_request.context.facts
            assert facts["turn"] == 2
            assert facts["current_step"]["text"] == "打开淘宝"
            assert "private initial prompt" not in repr(decision_request)
            assert "private scratchpad" not in repr(decision_request)
            assert "private interpretation" not in repr(decision_request)
        else:
            decision_factory.assert_not_called()
            decision_provider.evaluate.assert_not_awaited()
            decision_provider.aclose.assert_not_awaited()
            events.write.assert_not_called()
    assert results[0] == results[1] == results[2]
