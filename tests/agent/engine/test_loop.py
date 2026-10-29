"""Producer arbitration, LLM context policies, dispatch and session budgets."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
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
    patch_loop_tails,
)
from freezegun import freeze_time

from physiclaw.agent.engine import compact as compact_mod
from physiclaw.agent.engine import memory as memory_mod
from physiclaw.agent.engine import scratchpad
from physiclaw.agent.engine.builtin_tool import LocalTool, build_registry
from physiclaw.agent.engine.loop import (
    _advance,
    _call_producer,
    _dispatch_turn,
    drive,
    log_external_stop,
)
from physiclaw.agent.engine.provenance import PluginBinding
from physiclaw.agent.engine.runspec import EngineRun
from physiclaw.agent.engine.session import Session
from physiclaw.agent.llm import CORRECTIVE_LIMIT, _corrective_for_bad_shape
from physiclaw.agent.runtime.sentinel import DONE, FAIL, IDLE, STUCK
from physiclaw.agent.trace import Trace
from physiclaw.common import config
from physiclaw.common.provider_errors import ProviderError, ProviderTransientError
from physiclaw.contract.dto import (
    AssistantMessage,
    CollapsePolicy,
    FinishReason,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from physiclaw.contract.plugin import ProposedTurn

# ---------- drive ----------


@pytest.fixture
def patched_loop_deps(mocker) -> dict:
    """Stub out compact / scratchpad / plan tail injection so `drive` runs
    in isolation; returns the mocks keyed by target for spying."""
    return patch_loop_tails(mocker)


def _loop_run(provider, registry, **over) -> EngineRun:
    return _mk_run(
        provider=provider,
        tool_schemas=_schemas(registry),
        local_registry=registry,
        **over,
    )


@pytest.mark.asyncio
async def test_loop_closes_cleanly_on_end_session(patched_loop_deps) -> None:
    registry = _registry()

    asst = _asst(
        tool_calls=[
            _tc("note", {"summary": "closing"}),
            _tc("end_session", {"status": DONE, "recap": "all done"}),
        ],
        finish=FinishReason.TOOL_CALLS,
    )
    provider = FakeProvider([asst])

    session = Session()
    messages: list = [
        SystemMessage(content="sys"),
        UserMessage(content="trig"),
    ]

    run = _loop_run(provider, registry, session=session, messages=messages)
    await drive(run, session)
    messages = _llm(run).messages

    assert session.sentinel_status == DONE
    assert session.sentinel_recap == "all done"
    assert messages[-1] is asst
    assert [event.call_id for event in session.facts.events] == [
        call.id for call in asst.tool_calls
    ]
    assert all(not event.is_error for event in session.facts.events)


@pytest.mark.parametrize("restart", [False, True])
async def test_closing_a_batch_refuses_later_actions_and_preserves_receipts(
    budget_clock, restart
):
    registry = _registry()

    async def close(session, _args):
        session.sentinel_status = IDLE if restart else DONE
        session.restart_for_setup = restart
        budget_clock.tick(2)
        return "closed"

    name = "report_screen_layout" if restart else "end_session"
    registry[name] = LocalTool(name, "close", {}, close)
    calls = [_tc(name), _tc("tap"), _tc("end_session", {"status": STUCK})]
    session = Session()
    run = _mk_run(
        local_registry=registry,
        schema_by_name={**{name: {} for name in registry}, "tap": {}},
        deadline=time.monotonic() + 1,
    )

    successful = await _dispatch_turn(run, session, _asst(tool_calls=calls), 0)

    assert successful == calls[:1]
    assert run.mcp.tool_calls == []
    assert session.sentinel_status == (IDLE if restart else DONE)
    assert session.restart_for_setup is restart
    assert session.budget_exhausted is False
    assert [event.call_id for event in session.facts.events] == [c.id for c in calls]
    assert [event.executed for event in session.facts.events] == [True, False, False]
    assert [event.is_error for event in session.facts.events] == [False, True, True]


@pytest.mark.parametrize(
    "call, expected",
    [
        (_tc("note", {"summary": 42}), "expected string"),
        (_tc("update_progress", {"user_said": "   "}), "user_said must not be blank"),
    ],
)
async def test_llm_keeps_its_own_context_errors_private_and_actionable(
    patched_loop_deps, call, expected
):
    registry = build_registry({})
    companion = (
        _tc("read_memory")
        if call.name == "note"
        else _tc("note", {"summary": "drafting"})
    )
    session = Session()
    provider = FakeProvider(
        [
            _asst(tool_calls=[companion, call]),
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "closing"}),
                    _tc("end_session", {"status": IDLE}),
                ]
            ),
        ]
    )
    run = _loop_run(provider, registry, session=session)

    await drive(run, session)

    reply = next(
        message
        for message in _llm(run).messages
        if isinstance(message, ToolResultMessage) and message.tool_call_id == call.id
    )
    assert reply.is_error and expected in reply.content
    shared = next(event for event in session.facts.events if event.call_id == call.id)
    assert shared.result_text == "context update failed"
    assert shared.arguments == {}
    assert expected not in str(session.facts.snapshot(schemas=run.schema_by_name))


@pytest.mark.asyncio
async def test_loop_skips_layout_reminder_when_complete(patched_loop_deps) -> None:
    # Default (layout_incomplete=False) must NOT call layout.inject_tail
    # — that avoids a per-turn disk read once setup is done.
    spy = patched_loop_deps["layout"]

    registry = _registry()
    asst = _asst(
        tool_calls=[
            _tc("note", {"summary": "x"}),
            _tc("end_session", {"status": DONE, "recap": "done"}),
        ]
    )
    session = Session()

    run = _loop_run(
        FakeProvider([asst]),
        registry,
        session=session,
        messages=[SystemMessage(content="s")],
    )
    await drive(
        run,  # layout_incomplete defaults False
        session,
    )
    spy.assert_not_called()

    # With layout_incomplete=True it IS injected each turn.
    session2 = Session()
    run = _loop_run(
        FakeProvider([asst]),
        registry,
        layout_incomplete=True,
        session=session2,
        messages=[SystemMessage(content="s")],
    )
    await drive(
        run,
        session2,
    )
    spy.assert_called()


@pytest.mark.asyncio
async def test_loop_stucks_after_consecutive_correctives(patched_loop_deps) -> None:
    # A model that never holds the [note, one-other] shape must end
    # STUCK after CORRECTIVE_LIMIT consecutive correctives instead of
    # burning max_turns of round-trips.
    registry = _registry()

    bad = [_asst(tool_calls=[_tc("peek")]) for _ in range(CORRECTIVE_LIMIT + 3)]
    provider = FakeProvider(bad)
    session = Session()

    run = _loop_run(
        provider,
        registry,
        session=session,
        messages=[SystemMessage(content="sys"), UserMessage(content="trig")],
    )
    await drive(
        run,
        session,
    )

    assert session.sentinel_status == STUCK
    assert "malformed turns" in session.sentinel_recap
    assert len(provider.calls) == CORRECTIVE_LIMIT


# ---------- successful state writes ----------


def _state_write_turn() -> AssistantMessage:
    return _asst(
        tool_calls=[
            _tc("note", {"summary": "state saved", "scratchpad": "saved notes"}),
            _tc(
                "update_progress",
                {"steps": [{"content": "old step", "status": "pending"}]},
            ),
        ]
    )


@pytest.mark.parametrize(
    "note_args, progress_args",
    [
        ({"summary": "old step", "scratchpad": "x" * (scratchpad.MAX_CHARS + 1)}, {}),
        ({"summary": 123, "scratchpad": "new notes"}, {"steps": "invalid"}),
        ({"summary": "old step"}, {"user_said": " \t"}),
        (
            {"summary": "old step"},
            {
                "steps": [
                    {"content": "one", "status": "in_progress"},
                    {"content": "two", "status": "in_progress"},
                ]
            },
        ),
    ],
)
async def test_regression_failed_writes_do_not_replace_trajectory_history(
    note_args, progress_args, monkeypatch
) -> None:
    monkeypatch.setattr(config.CONFIG.pitfalls, "enabled", True)
    registry = build_registry({})
    provider = FakeProvider(
        [
            _state_write_turn(),
            _asst(
                tool_calls=[
                    _tc("note", note_args),
                    _tc("update_progress", progress_args),
                ]
            ),
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "closing"}),
                    _tc("end_session", {"status": DONE, "recap": "done"}),
                ]
            ),
        ]
    )
    session = Session()
    messages = _scaffold_messages()

    run = _loop_run(provider, registry, session=session, messages=messages)
    await drive(run, session)
    messages = _llm(run).messages

    assert session.sentinel_status == DONE
    assert session.plan_log == [(0, session.plan.snapshot())]
    assert session.scratchpad_log == [(0, "saved notes")]
    assert any(isinstance(m, ToolResultMessage) and m.is_error for m in messages)


async def test_loop_records_successful_rewrites_even_when_content_is_unchanged(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config.CONFIG.pitfalls, "enabled", True)
    registry = build_registry({})
    provider = FakeProvider(
        [
            _state_write_turn(),
            _state_write_turn(),
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "closing"}),
                    _tc("end_session", {"status": DONE, "recap": "done"}),
                ]
            ),
        ]
    )
    session = Session()
    messages = _scaffold_messages()

    run = _loop_run(provider, registry, session=session, messages=messages)
    await drive(run, session)

    assert session.sentinel_status == DONE
    assert [t for t, _ in session.plan_log] == [0, 1]
    assert session.scratchpad_log == [(0, "saved notes"), (1, "saved notes")]


async def test_regression_blank_memory_save_keeps_close_checkpoint_active(
    mem_paths,
) -> None:
    registry = build_registry({})
    provider = FakeProvider(
        [
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "remember: use metric units"}),
                    _tc("save_memory", {"text": " \n"}),
                ]
            ),
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "closing"}),
                    _tc("end_session", {"status": DONE, "recap": "premature"}),
                ]
            ),
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "saving"}),
                    _tc("save_memory", {"text": "use metric units"}),
                ]
            ),
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "closing"}),
                    _tc("end_session", {"status": DONE, "recap": "saved"}),
                ]
            ),
        ]
    )
    session = Session()
    messages = _scaffold_messages()

    run = _loop_run(provider, registry, session=session, messages=messages)
    await drive(run, session)
    messages = _llm(run).messages

    assert len(provider.calls) == 4
    assert session.sentinel_recap == "saved"
    assert memory_mod.MEMORY_FILE.read_text(encoding="utf-8") == "use metric units\n"
    assert any(
        isinstance(m, UserMessage) and "haven't saved it" in str(m.content)
        for m in messages
    )


# ---------- pre-compression checkpoint ----------


class CheckpointProvider(FakeProvider):
    """Collapse knobs small enough to hit the checkpoint turn in a short
    scripted session; records every request for tail assertions."""

    COLLAPSE = CollapsePolicy(first_at=2, keep=1, interval=100)

    def __init__(self, responses):
        super().__init__(responses)
        self.requests: list[list] = []

    async def chat(self, messages, tools, *, metadata=None):
        self.requests.append(list(messages))
        return await super().chat(messages, tools, metadata=metadata)


def _scaffold_messages() -> list:
    """[system, trigger] + the three compaction slots, as bootstrap builds."""
    return [
        SystemMessage(content="sys"),
        UserMessage(content="trig"),
        compact_mod.new_summary_placeholder(),
        UserMessage(content=compact_mod.MEMORY_INITIAL),
        compact_mod.new_skills_placeholder(),
    ]


def _req_texts(request: list) -> str:
    return "\n".join(
        m.content
        for m in request
        if isinstance(m, UserMessage) and isinstance(m.content, str)
    )


async def _run_checkpoint_session(responses, provider_cls=CheckpointProvider):
    registry = _registry()
    provider = provider_cls(responses)
    session = Session()
    messages = _scaffold_messages()
    run = _loop_run(provider, registry, session=session, messages=messages)
    await drive(run, session)
    messages = _llm(run).messages
    return provider, session, messages


@pytest.mark.parametrize(
    "arguments",
    [
        {"summary": "checkpoint", "scratchpad": "x" * (scratchpad.MAX_CHARS + 1)},
        {"scratchpad": "checkpoint=updated"},
        {"summary": 123, "scratchpad": "checkpoint=updated"},
    ],
)
async def test_regression_checkpoint_corrects_unwritable_note_before_folding(
    arguments,
) -> None:
    registry = build_registry({}) | {"peek": _registry()["peek"]}
    provider = CheckpointProvider(
        [
            _asst(tool_calls=[_tc("note", {"summary": "evidence"}), _tc("peek")]),
            _asst(tool_calls=[_tc("note", arguments), _tc("peek")]),
            _asst(
                tool_calls=[
                    _tc(
                        "note",
                        {"summary": "checkpoint", "scratchpad": "checkpoint=updated"},
                    ),
                    _tc("peek"),
                ]
            ),
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "closing"}),
                    _tc("end_session", {"status": DONE, "recap": "done"}),
                ]
            ),
        ]
    )
    session = Session(scratchpad="previous")
    messages = _scaffold_messages()

    run = _loop_run(provider, registry, session=session, messages=messages)
    await drive(run, session)

    assert session.sentinel_status == DONE
    assert len(provider.requests) == 4
    assert "Re-issue the SAME turn" in _req_texts(provider.requests[2])
    assert provider.requests[2][2].content == compact_mod.SUMMARY_INITIAL
    assert (
        provider.requests[3][2].content == f"{compact_mod.SUMMARY_HEADER}\n- evidence"
    )
    assert "<scratchpad>\ncheckpoint=updated\n</scratchpad>" in _req_texts(
        provider.requests[3]
    )
    assert session.scratchpad == "checkpoint=updated"


async def test_regression_checkpoint_repeated_oversize_note_fails_open_once() -> None:
    registry = build_registry({}) | {"peek": _registry()["peek"]}
    oversized = {
        "summary": "checkpoint",
        "scratchpad": "x" * (scratchpad.MAX_CHARS + 1),
    }
    provider = CheckpointProvider(
        [
            _asst(tool_calls=[_tc("note", {"summary": "evidence"}), _tc("peek")]),
            _asst(tool_calls=[_tc("note", oversized), _tc("peek")]),
            _asst(tool_calls=[_tc("note", oversized), _tc("peek")]),
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "closing"}),
                    _tc("end_session", {"status": DONE, "recap": "done"}),
                ]
            ),
        ]
    )
    session = Session(scratchpad="previous")
    messages = _scaffold_messages()

    run = _loop_run(provider, registry, session=session, messages=messages)
    await drive(run, session)

    assert session.sentinel_status == DONE
    assert len(provider.requests) == 4
    assert "Re-issue the SAME turn" in _req_texts(provider.requests[2])
    assert (
        provider.requests[3][2].content == f"{compact_mod.SUMMARY_HEADER}\n- evidence"
    )
    assert any(
        isinstance(m, ToolResultMessage)
        and m.is_error
        and "Summarize before writing" in m.content
        for m in provider.requests[3]
    )
    assert session.scratchpad == "previous"


@pytest.mark.parametrize(
    "arguments",
    [{"_raw": 123}, {"_raw": ["bad"]}, {"summary": 123}, {"summary": {"bad": "value"}}],
)
async def test_regression_loop_compacts_rejected_note_without_crashing(
    arguments,
) -> None:
    registry = build_registry({}) | {"peek": _registry()["peek"]}
    provider = CheckpointProvider(
        [
            _asst(tool_calls=[_tc("note", {"summary": "evidence"}), _tc("peek")]),
            _asst(
                tool_calls=[
                    ToolCall(id="invalid-note", name="note", arguments=arguments),
                    _tc("peek"),
                ]
            ),
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "checkpoint", "scratchpad": "saved"}),
                    _tc("peek"),
                ]
            ),
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "closing"}),
                    _tc("end_session", {"status": DONE, "recap": "done"}),
                ]
            ),
        ]
    )
    provider.COLLAPSE = CollapsePolicy(first_at=3, keep=1, interval=100)
    session = Session()
    messages = _scaffold_messages()
    run = _loop_run(provider, registry, session=session, messages=messages)

    await drive(run, session)
    messages = _llm(run).messages

    assert session.sentinel_status == DONE
    assert len(provider.requests) == 4
    assert any(
        isinstance(m, ToolResultMessage)
        and m.tool_call_id == "invalid-note"
        and m.is_error
        for m in provider.requests[2]
    )
    assert messages[2].content == f"{compact_mod.SUMMARY_HEADER}\n- evidence"
    assert session.scratchpad == "saved"


@pytest.mark.asyncio
async def test_checkpoint_requires_scratchpad_then_collapses() -> None:
    # Turn before the first collapse: the request carries the ⚠ tail; a
    # scratchpad-less note is rejected once; the compliant retry passes
    # and the collapse folds turn 0 to its summary line.
    provider, session, messages = await _run_checkpoint_session(
        [
            _asst(tool_calls=[_tc("note", {"summary": "a"}), _tc("peek")]),
            _asst(tool_calls=[_tc("note", {"summary": "b"}), _tc("peek")]),
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "b", "scratchpad": "cart=3; addr saved"}),
                    _tc("peek"),
                ]
            ),
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "closing"}),
                    _tc("end_session", {"status": DONE, "recap": "done"}),
                ]
            ),
        ]
    )

    assert session.sentinel_status == DONE
    assert len(provider.calls) == 4
    # ⚠ tail rides the imminent turn's requests only (1 = first attempt,
    # 2 = retry) — not turn 0, not the post-collapse turn.
    tails = [
        "⚠ Context compresses after this turn:" in _req_texts(r)
        for r in provider.requests
    ]
    assert tails == [False, True, True, False]
    # The retry request carries the checkpoint corrective.
    assert "Re-issue the SAME turn" in _req_texts(provider.requests[2])
    # The collapse fired: turn 0's summary folded into the slot.
    assert "- a" in messages[2].content


@pytest.mark.asyncio
async def test_checkpoint_fails_open_when_reminder_ignored() -> None:
    # A model that never adds the scratchpad is corrected ONCE, then its
    # summary-only note is accepted — compression proceeds on summaries
    # alone rather than stalling the session.
    provider, session, messages = await _run_checkpoint_session(
        [
            _asst(tool_calls=[_tc("note", {"summary": "a"}), _tc("peek")]),
            _asst(tool_calls=[_tc("note", {"summary": "b"}), _tc("peek")]),
            _asst(
                tool_calls=[_tc("note", {"summary": "b"}), _tc("peek")]
            ),  # still none
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "closing"}),
                    _tc("end_session", {"status": DONE, "recap": "done"}),
                ]
            ),
        ]
    )

    assert session.sentinel_status == DONE
    assert len(provider.calls) == 4
    correctives = sum(
        "Re-issue the SAME turn" in _req_texts(r) for r in provider.requests
    )
    assert correctives == 1  # exactly one nudge, no reject loop
    assert "- a" in messages[2].content  # collapse still fired


@pytest.mark.asyncio
async def test_checkpoint_corrective_renews_per_collapse_event() -> None:
    # With a tight interval EVERY turn is collapse-pending — the single
    # retry must renew per collapse event (completed turn), not once per
    # session, or every compression after the first gets no checkpoint.
    class TightProvider(CheckpointProvider):
        COLLAPSE = CollapsePolicy(first_at=2, keep=1, interval=1)

    def _plain_turn():
        return _asst(tool_calls=[_tc("note", {"summary": "s"}), _tc("peek")])

    provider, session, _ = await _run_checkpoint_session(
        [
            _plain_turn(),  # t0: not pending
            _plain_turn(),  # t1: pending → corrective #1
            _plain_turn(),  # t1 retry: fail open → collapse #1
            _plain_turn(),  # t2: pending again → corrective #2
            _plain_turn(),  # t2 retry: fail open → collapse #2
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "closing"}),
                    _tc("end_session", {"status": DONE, "recap": "done"}),
                ]
            ),  # t3: pending but end_session-exempt
        ],
        provider_cls=TightProvider,
    )

    assert session.sentinel_status == DONE
    assert len(provider.calls) == 6
    correctives = sum(
        "Re-issue the SAME turn" in _req_texts(r) for r in provider.requests
    )
    assert correctives == 2  # one per collapse event, renewed after each


@pytest.mark.asyncio
async def test_checkpoint_skipped_on_end_session_turn() -> None:
    # Closing on the checkpoint turn: nothing to preserve — no corrective.
    provider, session, _ = await _run_checkpoint_session(
        [
            _asst(tool_calls=[_tc("note", {"summary": "a"}), _tc("peek")]),
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "closing"}),
                    _tc("end_session", {"status": DONE, "recap": "done"}),
                ]
            ),
        ]
    )

    assert session.sentinel_status == DONE
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_loop_corrective_counter_resets_on_good_turn(patched_loop_deps) -> None:
    # Interleaved good turns reset the counter — occasional shape slips
    # never accumulate into a STUCK.
    registry = _registry()

    def good(i):
        return _asst(
            tool_calls=[
                _tc("note", {"summary": f"turn {i}"}),
                _tc("peek"),
            ]
        )

    responses = []
    for i in range(CORRECTIVE_LIMIT - 1):
        responses.append(_asst(tool_calls=[_tc("peek")]))  # bad shape
    responses.append(good(0))  # resets the counter
    for i in range(CORRECTIVE_LIMIT - 1):
        responses.append(_asst(tool_calls=[_tc("peek")]))  # bad again
    responses.append(
        _asst(
            tool_calls=[
                _tc("note", {"summary": "closing"}),
                _tc("end_session", {"status": DONE, "recap": "ok"}),
            ]
        )
    )
    provider = FakeProvider(responses)
    session = Session()

    run = _loop_run(
        provider,
        registry,
        session=session,
        messages=[SystemMessage(content="sys"), UserMessage(content="trig")],
    )
    await drive(
        run,
        session,
    )

    assert session.sentinel_status == DONE


@pytest.mark.asyncio
async def test_loop_routes_content_filter_to_fail(patched_loop_deps) -> None:
    registry = _registry()
    asst = _asst(finish=FinishReason.CONTENT_FILTER)
    provider = FakeProvider([asst])
    session = Session()

    run = _loop_run(
        provider, registry, session=session, messages=[SystemMessage(content="s")]
    )
    await drive(
        run,
        session,
    )

    assert session.sentinel_status == FAIL
    assert "content filter" in session.sentinel_recap


@pytest.mark.asyncio
async def test_loop_provider_failure_marks_stuck(patched_loop_deps) -> None:
    registry = _registry()
    provider = FakeProvider([RuntimeError("network")])
    session = Session()

    run = _loop_run(
        provider, registry, session=session, messages=[SystemMessage(content="s")]
    )
    await drive(
        run,
        session,
    )

    assert session.sentinel_status == STUCK
    assert "network" in session.sentinel_recap


@pytest.mark.asyncio
async def test_loop_no_tool_calls_injects_corrective(patched_loop_deps) -> None:
    registry = _registry()
    asst_no_calls = _asst(content="just talking")
    asst_close = _asst(
        tool_calls=[
            _tc("note", {"summary": "x"}),
            _tc("end_session", {"status": IDLE, "recap": "nothing"}),
        ]
    )
    provider = FakeProvider([asst_no_calls, asst_close])
    session = Session()
    messages: list = [SystemMessage(content="s")]

    run = _loop_run(provider, registry, session=session, messages=messages)
    await drive(run, session)
    messages = _llm(run).messages

    correctives = [
        m
        for m in messages
        if isinstance(m, UserMessage) and "no tool_calls" in str(m.content)
    ]
    assert len(correctives) == 1
    assert session.sentinel_status == IDLE


@pytest.mark.asyncio
async def test_loop_bad_turn_shape_injects_corrective(
    patched_loop_deps,
) -> None:
    registry = _registry()
    bad = _asst(tool_calls=[_tc("peek")])
    good = _asst(
        tool_calls=[
            _tc("note", {"summary": "y"}),
            _tc("end_session", {"status": DONE, "recap": "ok"}),
        ]
    )
    provider = FakeProvider([bad, good])
    session = Session()
    messages: list = [SystemMessage(content="s")]

    run = _loop_run(provider, registry, session=session, messages=messages)
    await drive(run, session)
    messages = _llm(run).messages

    correctives = [
        m
        for m in messages
        if isinstance(m, UserMessage) and "without `note`" in str(m.content)
    ]
    assert len(correctives) == 1
    assert session.sentinel_status == DONE


@pytest.mark.asyncio
async def test_loop_max_turns_marks_stuck(patched_loop_deps) -> None:
    registry = _registry()
    provider = FakeProvider(
        [_asst(tool_calls=[_tc("note"), _tc("peek")]) for _ in range(2)]
    )
    session = Session()

    run = _loop_run(
        provider,
        registry,
        settings=_settings(max_turns=2),
        session=session,
        messages=[SystemMessage(content="s")],
    )
    await drive(
        run,
        session,
    )

    assert session.sentinel_status == STUCK
    assert "max turns" in session.sentinel_recap


@pytest.mark.asyncio
async def test_loop_finish_length_logs_warning(patched_loop_deps) -> None:
    registry = _registry()
    asst = _asst(
        tool_calls=[
            _tc("note", {"summary": "x"}),
            _tc("end_session", {"status": DONE, "recap": "ok"}),
        ],
        finish=FinishReason.LENGTH,
    )
    provider = FakeProvider([asst])
    session = Session()
    tr = MagicMock()

    run = _loop_run(
        provider,
        registry,
        tr=tr,
        session=session,
        messages=[SystemMessage(content="s")],
    )
    await drive(
        run,
        session,
    )

    events = [c.args[0].get("event") for c in tr.write.call_args_list]
    assert "finish_length_warning" in events


# ---------- pre-close pitfalls gate ----------


def _registry_with_pitfall() -> dict[str, LocalTool]:
    reg = _registry()
    reg["add_pitfall"] = build_registry({})["add_pitfall"]
    return reg


def _close(status: str) -> AssistantMessage:
    return _asst(
        tool_calls=[
            _tc("note", {"summary": "closing"}),
            _tc("end_session", {"status": status, "recap": "r"}),
        ]
    )


def _pitfall_asst() -> AssistantMessage:
    return _asst(
        tool_calls=[
            _tc("note", {"summary": "banking traps"}),
            _tc("add_pitfall", {"items": ["京东: avoid Ai搜索"]}),
        ]
    )


async def _run_close_loop(provider, session, registry):
    messages: list = [SystemMessage(content="s")]
    run = _loop_run(provider, registry, session=session, messages=messages)
    await drive(run, session)
    messages = _llm(run).messages
    return messages


def _pitfall_correctives(messages) -> list:
    return [
        m
        for m in messages
        if isinstance(m, UserMessage) and "add_pitfall(" in str(m.content)
    ]


def _floor0(monkeypatch) -> None:
    # Drop capture_turn_floor to 0 so a short scripted DONE (turn 0) trips the gate.
    monkeypatch.setattr(config.CONFIG.pitfalls, "enabled", True)
    monkeypatch.setattr(config.CONFIG.pitfalls, "capture_turn_floor", 0)


async def test_disabled_pitfalls_skip_close_gate_and_trajectory(
    patched_loop_deps, monkeypatch
):
    monkeypatch.setattr(config.CONFIG.pitfalls, "capture_turn_floor", 0)
    provider = FakeProvider([_close(DONE)])
    session = Session(scratchpad="facts to retain")

    messages = await _run_close_loop(provider, session, build_registry({}))

    assert not _pitfall_correctives(messages)
    assert session.sentinel_status == DONE
    assert session.plan_log == session.scratchpad_log == []


@pytest.mark.asyncio
async def test_loop_forces_pitfall_on_long_done(patched_loop_deps, monkeypatch) -> None:
    # A long DONE → force add_pitfall, then the re-issued close passes.
    from physiclaw.agent.engine import pitfalls

    _floor0(monkeypatch)
    registry = _registry_with_pitfall()
    provider = FakeProvider([_close(DONE), _pitfall_asst(), _close(DONE)])
    session = Session()

    messages = await _run_close_loop(provider, session, registry)

    assert len(_pitfall_correctives(messages)) == 1
    assert session.added_pitfalls is True
    assert session.sentinel_status == DONE
    assert pitfalls.read() == ["京东: avoid Ai搜索"]


@pytest.mark.asyncio
async def test_loop_pitfall_corrective_carries_trajectory(
    patched_loop_deps, monkeypatch
) -> None:
    # The turn-marked trajectory rides the corrective so the agent mines the
    # real turn-wasters even after compaction folds early turns.
    _floor0(monkeypatch)
    registry = _registry_with_pitfall()
    session = Session()
    session.plan.update(
        understanding="buy milk",
        steps=[{"content": "search via 搜索 not Ai搜索", "status": "completed"}],
    )
    provider = FakeProvider([_close(DONE), _pitfall_asst(), _close(DONE)])

    messages = await _run_close_loop(provider, session, registry)

    corrective = _pitfall_correctives(messages)[0].content
    assert "<session-trajectory>" in corrective
    assert "search via 搜索 not Ai搜索" in corrective


@pytest.mark.asyncio
async def test_loop_pitfall_gate_fails_open_after_one_retry(
    patched_loop_deps, monkeypatch
) -> None:
    # Agent ignores the corrective and re-issues end_session — closes on the
    # second attempt (one-shot), never adding.
    _floor0(monkeypatch)
    registry = _registry_with_pitfall()
    provider = FakeProvider([_close(DONE), _close(DONE)])
    session = Session()

    messages = await _run_close_loop(provider, session, registry)

    assert len(_pitfall_correctives(messages)) == 1
    assert session.added_pitfalls is False
    assert session.sentinel_status == DONE


@pytest.mark.asyncio
async def test_loop_no_pitfall_gate_on_stuck(patched_loop_deps, monkeypatch) -> None:
    # A STUCK close never captures — the agent never escaped the trap, so it
    # can't write a real fix. Even with the floor at 0.
    _floor0(monkeypatch)
    registry = _registry_with_pitfall()
    provider = FakeProvider([_close(STUCK)])
    session = Session()

    messages = await _run_close_loop(provider, session, registry)

    assert not _pitfall_correctives(messages)
    assert session.sentinel_status == STUCK


@pytest.mark.asyncio
async def test_loop_no_pitfall_gate_on_short_done(
    patched_loop_deps, monkeypatch
) -> None:
    # A short DONE (below capture_turn_floor) sailed through → no gate.
    monkeypatch.setattr(config.CONFIG.pitfalls, "enabled", True)
    registry = _registry_with_pitfall()
    provider = FakeProvider([_close(DONE)])  # closes at turn 0, far below the floor
    session = Session()

    messages = await _run_close_loop(provider, session, registry)

    assert not _pitfall_correctives(messages)
    assert session.sentinel_status == DONE


@pytest.mark.asyncio
async def test_loop_no_pitfall_gate_on_idle(patched_loop_deps, monkeypatch) -> None:
    _floor0(monkeypatch)
    registry = _registry_with_pitfall()
    provider = FakeProvider([_close(IDLE)])
    session = Session()

    messages = await _run_close_loop(provider, session, registry)

    assert not _pitfall_correctives(messages)
    assert session.sentinel_status == IDLE


# ---------- pre-close memory-cue gate ----------


def _save_memory_tool() -> LocalTool:
    async def _h(session, _args):
        session.saved_memory = True
        return "saved"

    return LocalTool(
        name="save_memory",
        description="save",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=_h,
    )


def _registry_with_save() -> dict[str, LocalTool]:
    reg = _registry()
    st = _save_memory_tool()
    reg[st.name] = st
    return reg


def _note(
    summary: str, other_name: str, other_args: dict | None = None
) -> AssistantMessage:
    return _asst(
        tool_calls=[
            _tc("note", {"summary": summary}),
            _tc(other_name, other_args or {}),
        ]
    )


def _memory_correctives(messages) -> list:
    return [
        m
        for m in messages
        if isinstance(m, UserMessage)
        and "flagged something to remember" in str(m.content)
    ]


@pytest.mark.asyncio
async def test_loop_scans_cue_each_turn_and_forces_save_before_close(
    patched_loop_deps,
) -> None:
    registry = _registry_with_save()
    # turn 0 banks a cue (note summary) alongside an action; close is IDLE so
    # the pitfalls gate stays out of the way — only the memory gate fires.
    provider = FakeProvider(
        [
            _note("记住 user hates cilantro", "peek"),
            _note("closing", "end_session", {"status": IDLE, "recap": "r"}),
            _note("saving", "save_memory", {"text": "no cilantro"}),
            _note("closing", "end_session", {"status": IDLE, "recap": "r"}),
        ]
    )
    session = Session()
    messages = await _run_close_loop(provider, session, registry)

    assert any("cilantro" in c for c in session.memory_cues)  # scanned turn 0
    assert len(_memory_correctives(messages)) == 1
    assert session.saved_memory is True
    assert session.sentinel_status == IDLE


@pytest.mark.asyncio
async def test_loop_no_memory_gate_when_already_saved(patched_loop_deps) -> None:
    registry = _registry_with_save()
    provider = FakeProvider(
        [
            _note("记住 no cilantro", "save_memory", {"text": "no cilantro"}),
            _note("closing", "end_session", {"status": IDLE, "recap": "r"}),
        ]
    )
    session = Session()
    messages = await _run_close_loop(provider, session, registry)

    assert not _memory_correctives(messages)  # cue present but already saved
    assert session.sentinel_status == IDLE


@pytest.mark.asyncio
async def test_loop_no_memory_gate_without_a_cue(patched_loop_deps) -> None:
    registry = _registry_with_save()
    provider = FakeProvider(
        [_note("tapped the button", "end_session", {"status": IDLE, "recap": "r"})]
    )
    session = Session()
    messages = await _run_close_loop(provider, session, registry)

    assert not session.memory_cues
    assert not _memory_correctives(messages)
    assert session.sentinel_status == IDLE


@pytest.mark.asyncio
async def test_loop_memory_gate_fails_open_after_one_retry(patched_loop_deps) -> None:
    registry = _registry_with_save()
    provider = FakeProvider(
        [
            _note("记住 the gate code 4021", "peek"),
            _note("closing", "end_session", {"status": IDLE, "recap": "r"}),
            _note("closing anyway", "end_session", {"status": IDLE, "recap": "r"}),
        ]
    )
    session = Session()
    messages = await _run_close_loop(provider, session, registry)

    assert len(_memory_correctives(messages)) == 1  # one nudge, then closes
    assert session.saved_memory is False
    assert session.sentinel_status == IDLE


# ---------- wall-clock budget ----------

# The application budget is 5ms in the awaited-call cases. This outer guard
# only detects hangs: under CI load a 200ms watchdog could expire in the same
# event-loop iteration and turn the expected budget stop into external cancel.
BUDGET_TEST_WATCHDOG = 5


@pytest.fixture
def budget_clock():
    # Frozen monotonic deadlines, real event-loop scheduling for cancellation.
    with freeze_time("2026-10-14", real_asyncio=True) as clock:
        yield clock


@pytest.fixture(params=["provider", "plugin", "local", "mcp"])
def stalled_run(request, mocker, patched_loop_deps, budget_clock):
    registry = _registry()
    provider = FakeProvider([_asst(tool_calls=[_tc("note"), _tc("peek")])])
    run = _loop_run(
        provider,
        registry,
        settings=_settings(max_session_seconds=1),
        deadline=time.monotonic() + 0.005,
    )
    cancelled = []

    async def stall(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    call = mocker.AsyncMock(side_effect=stall)
    if request.param == "provider":
        run.plugins[-1].plugin.provider.chat = call
    elif request.param == "plugin":
        run.plugins = (
            PluginBinding("stalled", SimpleNamespace(advance=call), name="test"),
            *run.plugins,
        )
    elif request.param == "local":
        registry["peek"] = replace(registry["peek"], handler=call)
    else:
        registry.pop("peek")
        run.mcp.call_tool = call
    return run, call, cancelled


async def test_regression_budget_cancels_inflight_calls(stalled_run):
    run, call, cancelled = stalled_run
    session = _llm(run).session

    await asyncio.wait_for(drive(run, session), timeout=BUDGET_TEST_WATCHDOG)

    assert session.sentinel_status == STUCK
    assert session.budget_exhausted is True
    assert session.active_plugin is None
    call.assert_awaited_once()
    assert cancelled == [True]
    run.tr.write.assert_any_call(
        {"event": "budget_exhausted", "turn": 0, "budget_seconds": 1}
    )


async def test_regression_budget_interrupts_provider_retry_backoff(
    patched_loop_deps, budget_clock
):
    provider = FakeProvider([ProviderTransientError("busy")])
    session = Session()
    run = _loop_run(
        provider,
        _registry(),
        settings=_settings(retry_backoff_seconds=30),
        deadline=time.monotonic() + 0.005,
        session=session,
        messages=[],
    )

    await asyncio.wait_for(drive(run, session), timeout=BUDGET_TEST_WATCHDOG)

    assert session.budget_exhausted is True
    assert len(provider.calls) == 1


@pytest.fixture(params=["provider", "note"])
def expired_before_action(request, mocker, patched_loop_deps, budget_clock):
    registry = _registry()
    end = mocker.AsyncMock(wraps=registry["end_session"].handler)
    registry["end_session"] = replace(registry["end_session"], handler=end)
    reply = _asst(tool_calls=[_tc("note"), _tc("end_session", {"status": DONE})])
    run = _loop_run(
        FakeProvider([reply]),
        registry,
        deadline=time.monotonic() + 1,
    )

    async def slow_call(*args, **kwargs):
        budget_clock.tick(2)
        return reply if request.param == "provider" else "noted"

    if request.param == "provider":
        run.plugins[-1].plugin.provider.chat = slow_call
    else:
        registry["note"] = replace(registry["note"], handler=slow_call)
    return run, end


async def test_regression_budget_rechecked_before_starting_next_action(
    expired_before_action,
):
    run, end = expired_before_action
    session = _llm(run).session

    await drive(run, session)

    assert session.budget_exhausted is True
    assert session.sentinel_status == STUCK
    end.assert_not_awaited()


async def test_budget_does_not_swallow_external_cancellation(
    mocker, patched_loop_deps, budget_clock
):
    session = Session()
    run = _loop_run(
        FakeProvider([]),
        _registry(),
        deadline=time.monotonic() + 10,
        session=session,
        messages=[],
    )
    run.plugins[-1].plugin.provider.chat = mocker.AsyncMock(
        side_effect=asyncio.CancelledError("external stop")
    )

    with pytest.raises(asyncio.CancelledError, match="external stop"):
        await drive(run, session)

    assert session.budget_exhausted is False


async def test_budget_does_not_mislabel_an_unrelated_timeout(
    mocker, patched_loop_deps, budget_clock
):
    session = Session()
    run = _loop_run(
        FakeProvider([_asst(tool_calls=[_tc("note"), _tc("peek")])]),
        _registry(),
        deadline=time.monotonic() + 10,
        session=session,
        messages=[],
    )

    def fail_on_response(event):
        if event["event"] == "response":
            raise TimeoutError("trace timeout")

    run.tr.write.side_effect = fail_on_response

    with pytest.raises(TimeoutError, match="trace timeout"):
        await drive(run, session)

    assert session.budget_exhausted is False


async def test_budget_on_last_tool_of_last_turn_is_not_retryable(
    patched_loop_deps, budget_clock
):
    registry = _registry()

    async def peek(session, args):
        budget_clock.tick(2)
        return "peeked"

    registry["peek"] = replace(registry["peek"], handler=peek)
    provider = FakeProvider([_asst(tool_calls=[_tc("note"), _tc("peek")])])
    session = Session()
    run = _loop_run(
        provider,
        registry,
        settings=_settings(max_turns=1),
        deadline=time.monotonic() + 1,
        session=session,
        messages=[],
    )

    await drive(run, session)

    assert session.sentinel_status == STUCK
    assert session.budget_exhausted is True


async def test_budget_does_not_overwrite_a_completed_outcome(
    patched_loop_deps, budget_clock
):
    registry = _registry()

    async def finish(session, args):
        budget_clock.tick(2)
        session.sentinel_status = DONE
        return "done"

    registry["end_session"] = replace(registry["end_session"], handler=finish)
    provider = FakeProvider([_asst(tool_calls=[_tc("note"), _tc("end_session")])])
    session = Session()
    run = _loop_run(
        provider, registry, deadline=time.monotonic() + 1, session=session, messages=[]
    )

    await drive(run, session)

    assert session.sentinel_status == DONE
    assert session.budget_exhausted is False


async def test_budget_trace_records_the_interrupted_turn(
    patched_loop_deps, budget_clock
):
    provider = FakeProvider([_asst(tool_calls=[_tc("note"), _tc("peek")])])
    session = Session()
    run = _loop_run(
        provider,
        _registry(),
        deadline=time.monotonic() + 0.005,
        session=session,
        messages=[],
    )

    async def stall(*args):
        await asyncio.Event().wait()

    original_chat = provider.chat

    async def chat(messages, tools, *, metadata):
        if provider.calls:
            return await stall()
        return await original_chat(messages, tools, metadata=metadata)

    run.plugins[-1].plugin.provider.chat = chat

    await asyncio.wait_for(drive(run, session), timeout=BUDGET_TEST_WATCHDOG)

    assert session.budget_exhausted is True
    run.tr.write.assert_any_call(
        {"event": "budget_exhausted", "turn": 1, "budget_seconds": 0}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-1, 0])
async def test_loop_closes_stuck_when_budget_exhausted(budget_clock, offset) -> None:
    # Deadline already reached: the loop must close before any
    # provider round-trip (FakeProvider([]) raises if chatted with).
    # The ordinary drive tests use deadline=None for the disabled-budget path.
    session = Session()
    messages: list = []
    run = _mk_run(
        FakeProvider([]),
        settings=_settings(max_session_seconds=10),
        deadline=time.monotonic() + offset,
        session=session,
        messages=messages,
    )

    await drive(run, session)
    messages = _llm(run).messages

    assert session.sentinel_status == STUCK
    assert session.budget_exhausted is True
    assert "budget" in session.sentinel_recap
    assert messages == []  # no turn ran


# ---------- log_external_stop ----------


def test_log_external_stop_writes_recovery_line(mem_paths) -> None:
    import datetime as dt

    session = Session()
    session.plan.update(
        user_said="buy oil",
        steps=[
            {"content": "open JD", "status": "completed"},
            {"content": "fix cart qty", "status": "in_progress"},
        ],
    )

    log_external_stop(session, None)

    text = (mem_paths / f"{dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
    assert "stopped externally mid-task" in text
    assert "fix cart qty" in text
    assert "(1/2 steps done)" in text


def test_log_external_stop_skips_closed_and_undrafted_sessions(mem_paths) -> None:
    closed = Session()
    closed.plan.update(user_said="buy oil")
    closed.sentinel_status = DONE
    log_external_stop(closed, None)

    undrafted = Session()  # default seed plan — nothing recoverable
    log_external_stop(undrafted, None)

    assert not mem_paths.exists()  # neither wrote a daily log


def test_log_external_stop_never_raises(mem_paths, monkeypatch) -> None:
    monkeypatch.setattr(
        memory_mod, "append_log", MagicMock(side_effect=OSError("disk full"))
    )
    session = Session()
    session.plan.update(
        user_said="buy oil",
        steps=[{"content": "open JD", "status": "in_progress"}],
    )

    log_external_stop(session, None)  # must not raise


# ---------- _advance ----------


@pytest.mark.asyncio
async def test_advance_returns_immediately_on_success(mocker) -> None:
    asst = _asst(content="ok", tool_calls=[_tc("note"), _tc("peek")])
    chat = mocker.AsyncMock(return_value=asst)

    out = await _advance(_arbitration_run(chat), Session(), _turn_metadata())

    assert out.message is asst
    assert chat.await_count == 1


@pytest.mark.asyncio
async def test_advance_retries_then_succeeds(mocker) -> None:
    mocker.patch("asyncio.sleep")
    asst = _asst(content="ok", tool_calls=[_tc("note"), _tc("peek")])
    chat = mocker.AsyncMock(side_effect=[ProviderTransientError("transient"), asst])

    out = await _advance(_arbitration_run(chat), Session(), _turn_metadata())

    assert out.message is asst
    assert chat.await_count == 2


@pytest.mark.asyncio
async def test_advance_raises_provider_error_after_max_attempts(
    mocker,
) -> None:
    mocker.patch("asyncio.sleep")
    chat = mocker.AsyncMock(side_effect=ProviderTransientError("nope"))

    # Typed (ProviderError, not bare RuntimeError) so _call_producer logs
    # expected exhaustion as one line instead of a traceback.
    with pytest.raises(ProviderError, match=r"^gave up after 2 attempts:"):
        await (
            _arbitration_run(chat, attempts=2)
            .plugins[-1]
            .plugin._chat_with_retry([], _turn_metadata())
        )


@pytest.mark.asyncio
async def test_advance_does_not_catch_permanent_errors(mocker) -> None:
    chat = mocker.AsyncMock(side_effect=RuntimeError("permanent"))

    with pytest.raises(RuntimeError, match=r"^permanent$"):
        await (
            _arbitration_run(chat)
            .plugins[-1]
            .plugin._chat_with_retry([], _turn_metadata())
        )


@pytest.mark.asyncio
async def test_first_plugin_turn_wins_and_provider_is_never_called(mocker) -> None:
    # Arbitration, not a chain: the first plugin to speak ends the turn —
    # later plugins are not consulted and no provider round-trip is paid.
    turn = _asst(tool_calls=[_tc("peek")])
    first = mocker.MagicMock()
    first.advance = mocker.AsyncMock(return_value=ProposedTurn(turn))
    second = mocker.MagicMock()
    second.advance = mocker.AsyncMock()
    chat = mocker.AsyncMock()

    out = await _advance(
        _arbitration_run(
            chat,
            plugins=(
                PluginBinding("first", first, name="test"),
                PluginBinding("second", second, name="test"),
            ),
        ),
        Session(),
        _turn_metadata(),
    )

    assert out.message is turn
    assert out.binding.id == "first"
    second.advance.assert_not_awaited()
    chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_plugin_pass_and_crash_both_fall_through_to_the_provider(
    mocker,
) -> None:
    # A plugin that passes (None) and one that crashes both degrade the
    # same way: the LLM speaks. A plugin bug must never kill the session.
    passing = mocker.MagicMock()
    passing.advance = mocker.AsyncMock(return_value=None)
    crashing = mocker.MagicMock()
    crashing.advance = mocker.AsyncMock(side_effect=RuntimeError("plugin bug"))
    asst = _asst(content="ok", tool_calls=[_tc("note"), _tc("peek")])
    chat = mocker.AsyncMock(return_value=asst)

    out = await _advance(
        _arbitration_run(
            chat,
            plugins=(
                PluginBinding("passing", passing, name="test"),
                PluginBinding("crashing", crashing, name="test"),
            ),
        ),
        Session(),
        _turn_metadata(),
    )

    assert out.message is asst
    assert passing.advance.await_count == 1
    assert crashing.advance.await_count == 1


# ---------- producer timing ----------


@pytest.fixture
def trace_stub(mocker):
    t = mocker.MagicMock(spec=Trace)
    return t


@pytest.mark.asyncio
async def test_call_producer_response_event_carries_elapsed_ms(
    mocker,
    trace_stub,
) -> None:
    # Producer selection latency remains available independently of API timing.
    asst = AssistantMessage(
        content="",
        tool_calls=[_tc("note"), _tc("peek")],
        finish_reason=FinishReason.TOOL_CALLS,
        usage=Usage(),
    )
    provider = mocker.MagicMock()
    provider.chat = mocker.AsyncMock(return_value=asst)
    rlog = mocker.MagicMock()
    run = _mk_run(provider=provider, tr=trace_stub, rlog=rlog)

    out = await _call_producer(run, Session(), turn=0)

    assert out.message == asst
    response_event = next(
        c.args[0]
        for c in trace_stub.write.call_args_list
        if c.args[0].get("event") == "response"
    )
    assert isinstance(response_event["elapsed_ms"], int)
    # RawLog got the same number.
    assert isinstance(rlog.write_response.call_args.kwargs["elapsed_ms"], int)


# ---------- _corrective_for_bad_shape ----------


def test_corrective_for_action_without_note() -> None:
    out = _corrective_for_bad_shape(["peek"])

    assert "called `peek` without `note`" in out
    assert "[note(summary=...), peek(...)]" in out


def test_corrective_for_action_without_note_with_extras() -> None:
    out = _corrective_for_bad_shape(["peek", "tap"])

    assert "without `note`" in out
    assert "too many action tools" in out
    assert "['tap']" in out


def test_corrective_for_note_alone() -> None:
    out = _corrective_for_bad_shape(["note"])

    assert "`note` alone with no action tool" in out
    assert "peek()" in out  # default suggestion


def test_corrective_for_note_with_too_many_actions() -> None:
    out = _corrective_for_bad_shape(["note", "peek", "tap"])

    assert "`note` plus 2 action tools" in out


def test_corrective_for_multiple_notes() -> None:
    out = _corrective_for_bad_shape(["note", "note"])

    assert "called `note` 2 times" in out


def test_corrective_for_three_notes() -> None:
    out = _corrective_for_bad_shape(["note", "note", "note"])

    assert "called `note` 3 times" in out


# ---------- synthesized turns (conductor legs) ----------


@pytest.mark.asyncio
async def test_plugin_turn_skips_model_gates_and_binds_dispatch(
    patched_loop_deps, mocker
) -> None:
    """The selected plugin owns dispatch; the next LLM turn is gated normally."""
    from physiclaw.agent.engine import policy as policy_mod

    seen_gate_turns: list[list[str]] = []

    class SpyGate(policy_mod.TurnGate):
        def check(self, session, asst, called, *, turn, compaction_imminent):
            seen_gate_turns.append(list(called))
            return None

    producers_during_dispatch: list[str | None] = []

    async def peek_handler(session, _args):
        binding = session.active_plugin
        producers_during_dispatch.append(binding.id if binding is not None else None)
        return "peek-result"

    registry = _registry()
    registry["peek"] = LocalTool(
        name="peek",
        description="look",
        input_schema={"type": "object", "additionalProperties": True},
        handler=peek_handler,
    )

    synthesized = _asst(
        tool_calls=[_tc("note", {"summary": "conductor"}), _tc("peek")],
    )
    closing = _asst(
        tool_calls=[
            _tc("note", {"summary": "done"}),
            _tc("end_session", {"status": DONE, "recap": "ok"}),
        ],
    )
    provider = FakeProvider([closing])
    policies = policy_mod.Policies(
        turn_gates=[SpyGate()], dispatch_guards=[], result_observers=[]
    )

    session = Session()
    messages: list = [SystemMessage(content="sys"), UserMessage(content="trig")]
    run = _loop_run(
        provider, registry, policies=policies, session=session, messages=messages
    )
    run.plugins = (
        PluginBinding(
            "conductor",
            SimpleNamespace(
                advance=mocker.AsyncMock(side_effect=[ProposedTurn(synthesized), None])
            ),
            presentation="playbook",
            name="playbook",
        ),
        *run.plugins,
    )
    await drive(run, session)

    assert session.sentinel_status == DONE
    assert producers_during_dispatch == ["conductor"]
    assert session.active_plugin is None
    # The gate judged only the model's closing turn.
    assert seen_gate_turns == [["note", "end_session"]]


@pytest.mark.asyncio
async def test_producer_trace_and_model_wire_log_are_separate(
    patched_loop_deps,
    mocker,
) -> None:
    registry = _registry()
    synthesized = _asst(tool_calls=[_tc("note", {"summary": "c"}), _tc("peek")])
    closing = _asst(
        tool_calls=[
            _tc("note", {"summary": "done"}),
            _tc("end_session", {"status": DONE, "recap": "ok"}),
        ],
    )
    provider = FakeProvider([closing])
    tr, rlog = MagicMock(), MagicMock()

    session = Session()
    messages: list = [SystemMessage(content="sys"), UserMessage(content="trig")]
    run = _loop_run(
        provider, registry, tr=tr, rlog=rlog, session=session, messages=messages
    )
    run.plugins = (
        PluginBinding(
            "conductor",
            SimpleNamespace(
                advance=mocker.AsyncMock(side_effect=[ProposedTurn(synthesized), None])
            ),
            presentation="playbook",
            name="playbook",
        ),
        *run.plugins,
    )
    await drive(run, session)

    rlog.write_request.assert_called_once()
    rlog.write_response.assert_called_once()
    assert rlog.write_response.call_args.args[1] == closing.raw
    response_events = [
        c.args[0]
        for c in tr.write.call_args_list
        if c.args and c.args[0].get("event") == "response"
    ]
    assert [e["producer"]["plugin_id"] for e in response_events] == [
        "conductor",
        "physiclaw.llm",
    ]
    # The loop writes no usage event of its own (the provider does, per
    # call) — and a synthesized turn made no call.
    assert not [
        c.args[0]
        for c in tr.write.call_args_list
        if c.args and c.args[0].get("event") == "usage"
    ]
