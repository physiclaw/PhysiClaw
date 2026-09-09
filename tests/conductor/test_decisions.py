"""Tests for `physiclaw.conductor.drive.decisions` (behind `playbooks
micro` and the decisions section of `playbooks stats`) — recorded
decision calls loaded, re-asked, judged, and folded."""

from __future__ import annotations

import json

import pytest
from conductor_fakes import ScriptedProvider, agent_reply
from typer.testing import CliRunner

from physiclaw.cli.playbooks import playbooks_app
from physiclaw.common import paths
from physiclaw.conductor.drive import decisions

SID = "20260907-203014-ecae27"


def _micro_record(**over) -> dict:
    rec = {
        "t": "2026-09-07T20:32:42.622",
        "kind": "micro",
        "call": "agent_fields",
        "node": "parse",
        "thinking": "off",
        "allowed": ["done", "escalate"],
        "answer": "done",
        "confidence": 0.95,
        "request": [
            {
                "role": "system",
                "content": [{"type": "text", "text": "Reply with JSON"}],
            },
            {"role": "user", "content": "Derive the keyword"},
        ],
        "raw": {"choices": [{"message": {"content": "{}"}}]},
    }
    rec.update(over)
    return rec


def _session(records: list[dict], events: list[dict] | None = None):
    d = paths.engine_sessions_dir() / SID
    d.mkdir(parents=True, exist_ok=True)
    (d / "wire.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    if events is not None:
        (d / "events.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
            encoding="utf-8",
        )
    return d


_ok = agent_reply  # the fixtures' records are agent calls


# ---------- load ----------


def test_load_turns_a_micro_record_back_into_messages() -> None:
    d = _session([{"kind": "session_start"}, _micro_record()])

    (rec,) = decisions.load(d)

    assert (rec.call, rec.node, rec.answer, rec.confidence) == (
        "agent_fields",
        "parse",
        "done",
        0.95,
    )
    assert rec.allowed == ("done", "escalate") and rec.thinking == "off"
    assert [type(m).__name__ for m in rec.messages] == ["SystemMessage", "UserMessage"]
    assert rec.messages[0].content == "Reply with JSON"


def test_load_keeps_an_episodes_replayed_history_in_order() -> None:
    d = _session(
        [
            _micro_record(
                call="agent_act",
                node="pick",
                request=[
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "screen 1"},
                    {"role": "assistant", "content": '{"answer": "row"}'},
                    {"role": "user", "content": "screen 2"},
                ],
            )
        ]
    )

    (rec,) = decisions.load(d)

    assert [type(m).__name__ for m in rec.messages] == [
        "SystemMessage",
        "UserMessage",
        "AssistantMessage",
        "UserMessage",
    ]


def test_load_reads_a_scrubbed_frame_back_from_the_session_images() -> None:
    # A micro record's frame is a ref (the sink filed the bytes); the
    # re-ask sends the frame the wake sent. A ref whose file is gone
    # leaves the call to ask over the listing alone.
    import base64

    from physiclaw.contract.dto import ImageBlock, TextBlock

    d = _session(
        [
            _micro_record(
                call="agent_act",
                node="pick",
                request=[
                    {"role": "system", "content": "sys"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "[you scrolled down]"},
                            {
                                "type": "image",
                                "source": {"type": "ref", "ref": "images/t1.jpg"},
                            },
                            {"type": "text", "text": "listing"},
                        ],
                    },
                    {"role": "assistant", "content": '{"answer": "3"}'},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "ref", "ref": "images/gone.jpg"},
                            },
                            {"type": "text", "text": "listing 2"},
                        ],
                    },
                ],
            )
        ]
    )
    (d / "images").mkdir()
    (d / "images" / "t1.jpg").write_bytes(b"jpeg bytes")

    (rec,) = decisions.load(d)

    first = rec.messages[1].content
    assert first == [
        TextBlock(text="[you scrolled down]"),
        ImageBlock(
            media_type="image/jpeg",
            data_b64=base64.b64encode(b"jpeg bytes").decode(),
        ),
        TextBlock(text="listing"),
    ]
    assert rec.messages[3].content == "listing 2"


def test_load_without_a_wire_log_raises() -> None:
    d = paths.engine_sessions_dir() / SID
    d.mkdir(parents=True, exist_ok=True)
    with pytest.raises(FileNotFoundError):
        decisions.load(d)


# ---------- ask ----------


@pytest.mark.asyncio
async def test_ask_reads_each_reply_with_the_walks_parser() -> None:
    d = _session([_micro_record()])
    (rec,) = decisions.load(d)
    provider = ScriptedProvider(
        [_ok("done"), _ok("escalate", 0.7), "not json"], reasoning=12
    )

    row = await decisions.ask(provider, rec, reps=3)

    assert [(r.valid, r.answer) for r in row.replies] == [
        (True, "done"),
        (True, "escalate"),
        (False, None),
    ]
    assert all(r.reasoning == 12 and r.output == 20 for r in row.replies)
    # Asked as the wake asked it, the recorded think level included.
    assert provider.asks[0] == {"purpose": "micro", "thinking": "off"}


@pytest.mark.asyncio
async def test_ask_can_override_the_think_level_and_survives_an_error() -> None:
    d = _session([_micro_record()])
    (rec,) = decisions.load(d)
    provider = ScriptedProvider([RuntimeError("down"), _ok("done")])

    row = await decisions.ask(provider, rec, thinking="high", reps=2)

    assert row.replies[0].error.startswith("RuntimeError") and not row.replies[0].valid
    assert row.replies[1].valid
    assert provider.asks[1]["thinking"] == "high"


@pytest.mark.asyncio
async def test_a_record_without_allowed_answers_is_asked_not_judged() -> None:
    d = _session([_micro_record(allowed=[])])
    (rec,) = decisions.load(d)

    row = await decisions.ask(ScriptedProvider([_ok("done")]), rec)

    assert row.replies[0].answer is None and not row.replies[0].valid


def test_summarize_folds_every_ask() -> None:
    rec = decisions.Recorded("agent_fields", "parse", ("done",), (), "done", 0.9, None)
    rows = [
        decisions.Row(
            rec,
            (
                decisions.Reply("done", 0.9, True, 1000, 40, 10),
                decisions.Reply(None, None, False, 3000, 80, 70),
            ),
        )
    ]

    s = decisions.summarize(rows)

    assert (s.calls, s.asks, s.valid, s.agreed) == (1, 2, 1, 1)
    assert s.agreement == 0.5 and s.valid_rate == 0.5
    assert s.mean_ms == 2000 and s.ms_max == 3000 and s.mean_reasoning == 40


# ---------- the decisions of recorded walks ----------


EVENTS = [
    {"event": "env"},
    {
        "event": "usage",
        "turn": 2,
        "call": "micro",
        "model": "moonshot/kimi-k2.6",
        "output": 3394,
        "reasoning": 3311,
    },
    {
        "event": "micro_call",
        "call": "parse_task",
        "node": "parse",
        "rows": 0,
        "out": "taobao/buy",
        "attempts": 1,
        "elapsed_ms": 78609,
    },
    {
        "event": "usage",
        "turn": 5,
        "call": "micro",
        "model": "moonshot/kimi-k2.6",
        "output": 7309,
        "reasoning": 7218,
    },
    {
        "event": "usage",
        "turn": 5,
        "call": "micro",
        "model": "moonshot/kimi-k2.6",
        "output": 200,
        "reasoning": 150,
    },
    {
        "event": "micro_call",
        "call": "agent_act",
        "node": "pick",
        "rows": 40,
        "out": "act",
        "attempts": 2,
        "elapsed_ms": 180479,
    },
    {
        "event": "usage",
        "turn": 6,
        "call": "turn",
        "model": "moonshot/kimi-k2.6",
        "output": 300,
        "reasoning": 0,
    },
    {
        "event": "micro_call",
        "call": "agent_act",
        "node": "pick",
        "rows": 49,
        "out": None,
        "attempts": 2,
        "elapsed_ms": 9000,
    },
]


def test_session_decisions_join_each_call_to_the_usage_its_attempts_wrote() -> None:
    d = _session([], events=EVENTS)

    decs = decisions.session_decisions(d)

    assert [(x["call"], x["output"], x["reasoning"], x["attempts"]) for x in decs] == [
        ("parse_task", 3394, 3311, 1),
        ("agent_act", 7509, 7368, 2),
        ("agent_act", 0, 0, 2),  # the turn's usage is not a decision's
    ]


def test_decision_stats_fold_per_call_kind() -> None:
    d = _session([], events=EVENTS)

    stats = decisions.decision_stats([d, paths.engine_sessions_dir() / "missing"])

    pick = stats["agent_act"]
    assert (pick.calls, pick.escalated, pick.repaired) == (2, 1, 1)
    assert pick.ms_max == 180479 and pick.rows_max == 49 and pick.nodes == {"pick": 2}
    assert stats["parse_task"].mean_reasoning == 3311


# ---------- the CLI ----------


def test_playbooks_micro_reasks_and_reports(monkeypatch) -> None:
    _session([_micro_record()])
    provider = ScriptedProvider([_ok("done"), _ok("escalate")])
    monkeypatch.setattr(
        "physiclaw.provider.make_provider", lambda pid, mid, **kw: provider
    )

    result = CliRunner().invoke(
        playbooks_app, ["micro", SID[-6:], "--model", "moonshot/kimi-k3", "--reps", "2"]
    )

    assert result.exit_code == 0, result.output
    assert "1 decision(s) × 2 on moonshot/kimi-k3" in result.output
    assert "same" in result.output and "differs" in result.output
    assert "valid=100% agree=50%" in result.output
    assert provider.closed


def test_playbooks_micro_rejects_a_bad_think_level() -> None:
    _session([_micro_record()])
    result = CliRunner().invoke(playbooks_app, ["micro", SID[-6:], "--think", "max"])
    assert result.exit_code == 2 and "--think must be one of" in result.output


def test_playbooks_stats_reports_the_walks_decisions(monkeypatch) -> None:
    from physiclaw.conductor.walk import walklog

    _session([], events=EVENTS)
    monkeypatch.setattr(
        walklog,
        "load",
        lambda: [
            {
                "ts": "x",
                "session": SID,
                "app": "taobao",
                "playbook": "buy",
                "outcome": "handover",
                "node": "pick",
                "idx": 4,
                "nodes": 7,
                "reason": "r",
                "micros": 3,
                "rescues": 0,
            }
        ],
    )

    result = CliRunner().invoke(playbooks_app, ["stats"])

    assert result.exit_code == 0, result.output
    assert "decisions (1 session(s) on disk):" in result.output
    assert "agent_act: calls=2 escalated=1 repaired=1" in result.output
    assert "parse_task: calls=1" in result.output
