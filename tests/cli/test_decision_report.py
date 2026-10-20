"""Exercise actual shadow tracing through blind review and priced CLI output."""

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from physiclaw.agent.trace.store import Images
from physiclaw.agent.trace.trace import Trace
from physiclaw.cli import app
from physiclaw.common import config, paths
from physiclaw.contract.decision import DecisionAnswer, DecisionResult, DecisionUsage
from physiclaw.contract.dto import Usage, usage_event
from physiclaw.contract.observation import read_observation
from physiclaw.contract.plugin import PluginServices
from physiclaw.contract.turn import (
    ProgressSnapshot,
    ProgressStep,
    TurnInput,
    TurnMetadata,
)
from physiclaw.decision.plugin import DecisionPlugin


async def test_shadow_trace_to_blind_labels_cost_and_baseline(
    monkeypatch, mocker, observation_blocks, tmp_path
):
    monkeypatch.setattr(config.CONFIG.decision, "mode", "shadow")
    monkeypatch.setattr(config.CONFIG.decision, "model", "test/native")
    sid = "report-abc123"
    trace = Trace(sid, Images(sid))
    provider = SimpleNamespace(aclose=mocker.AsyncMock())

    async def evaluate(request, **kwargs):
        selected = {"phase": "ROUTINE", "action_type": "tap", "target": "b1"}
        return DecisionResult(
            request.id,
            {
                question.id: DecisionAnswer(
                    selected[question.id],
                    probabilities={
                        choice.id: 0.95
                        if choice.id == selected[question.id]
                        else 0.05 / (len(question.candidates) - 1)
                        for choice in question.candidates
                    },
                )
                for question in request.questions
            },
            provider_id="test",
            model_id="native",
            requested_model_id="native",
            usage=DecisionUsage(input_tokens=100, output_tokens=0),
        )

    provider.evaluate = evaluate
    plugin = DecisionPlugin()
    observation = replace(
        read_observation(observation_blocks()), observed_after=time.time()
    )
    turn = TurnInput(
        observation,
        None,
        tools={"tap": {}},
        progress=ProgressSnapshot(
            "Open Search", (ProgressStep("Tap Search", "in_progress"),)
        ),
    )
    try:
        await plugin.session_setup(
            PluginServices(
                session_provider=mocker.Mock(),
                events=trace,
                decision_provider_factory=lambda *args, **kwargs: provider,
            )
        )
        assert (
            await plugin.advance(turn, metadata=TurnMetadata(sid, 0, None, 10)) is None
        )
        await plugin._task
    finally:
        await plugin.aclose()
        trace.close()

    runner = CliRunner()
    exported = runner.invoke(app, ["logs", "abc123", "--decisions", "--label-template"])
    assert exported.exit_code == 0, exported.output
    template = json.loads(exported.output)
    assert len(template["cases"]) == 1
    case = template["cases"][0]
    assert (
        paths.sessions_dir() / sid / case["image_path"]
    ).read_bytes() == observation.frame.data
    assert '"answers":' not in exported.output
    assert "threshold_gate" not in exported.output
    case["label"] = {"expected": "tap", "acceptable_targets": ["b1"], "note": ""}
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps(template), encoding="utf-8")
    rates = tmp_path / "rates.json"
    rates.write_text(
        json.dumps({"decision": {"test/native": {"input": 2, "output": 0}}}),
        encoding="utf-8",
    )

    baseline = paths.sessions_dir() / "baseline-def456"
    baseline.mkdir()
    (baseline / "events.jsonl").write_text("", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "logs",
            "abc123",
            "--decisions",
            "--labels",
            str(labels),
            "--rates",
            str(rates),
            "--baseline",
            "def456",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["accuracy"]["raw_accuracy"] == 1
    assert report["accuracy"]["gated_tap_precision"] == 1
    assert report["execution"] == {"not_proposed": 1}
    assert report["cost"]["total_usd"] == pytest.approx(0.0002)
    assert report["usage"]["groups"][0]["api_kind"] == "decision"
    assert report["comparison"]["baseline"]["session_id"] == "baseline-def456"
    usage = runner.invoke(
        app, ["logs", "abc123", "--usage", "--rates", str(rates), "--json"]
    )
    assert usage.exit_code == 0, usage.output
    assert json.loads(usage.output)["cost"]["total_usd"] == pytest.approx(0.0002)
    provider.aclose.assert_awaited_once()


@pytest.mark.parametrize(
    "args",
    [
        ["--decisions"],
        ["sid", "--decisions", "--usage"],
        ["sid", "--labels", "labels.json"],
        ["sid", "--label-template"],
        ["sid", "--baseline", "other"],
        ["sid", "--rates", "rates.json"],
        ["sid", "--decisions", "--label-template", "--labels", "labels.json"],
    ],
)
def test_decision_review_options_are_explicit(args):
    result = CliRunner().invoke(app, ["logs", *args])
    assert result.exit_code != 0
    assert not isinstance(result.exception, (TypeError, AttributeError))


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "duplicate_call",
        "missing_call",
        "missing_request_id",
        "orphan_call",
        "mismatched_call",
        "malformed_line",
        "duplicate_json_key",
        "invalid_utf8",
        "deep_json",
        "nonfinite",
    ],
)
def test_usage_and_decisions_share_complete_and_damaged_trace_accounting(
    tmp_path, damage
):
    sid = "accounting-abc123"
    directory = paths.sessions_dir() / sid
    directory.mkdir(parents=True)
    projection = {
        "event": "decision_projection",
        "turn": 0,
        "mode": "shadow",
        "evaluation": {"status": "scheduled", "request_id": "native-request"},
        "projection": {
            "image": {"observation_id": "view", "sha256": "image-hash"},
            "state": {"observation": {"id": "view"}},
            "profile": {"questions": {"target": {"options": {}}}},
        },
    }
    native = {
        "event": "decision_call",
        "turn": 0,
        "mode": "shadow",
        "request_id": "native-request",
        "observation_id": "view",
        "model": "vendor/native",
        "status": "answered",
        "usage": {"input_tokens": 100, "output_tokens": 0},
    }
    events = [
        usage_event(Usage(100, 10), call="turn", model="vendor/chat", elapsed_ms=1),
        projection,
        native,
    ]
    if damage == "duplicate_call":
        events.append(dict(native))
    elif damage == "missing_call":
        events.remove(native)
    elif damage == "missing_request_id":
        del native["request_id"]
    elif damage == "orphan_call":
        events.remove(projection)
    elif damage == "mismatched_call":
        native["observation_id"] = "different-view"
    extra = {
        "malformed_line": b'{"event":\n',
        "duplicate_json_key": b'{"event":"usage","event":"decision_call"}\n',
        "invalid_utf8": b'{"event":"\xff"}\n',
        "deep_json": b'{"event":"usage","nested":'
        + b"[" * 1100
        + b"0"
        + b"]" * 1100
        + b"}\n",
        "nonfinite": b'{"event":"usage","elapsed_ms":1e999}\n',
    }.get(damage, b"")
    (directory / "events.jsonl").write_bytes(
        "".join(json.dumps(event) + "\n" for event in events).encode() + extra
    )
    rates = tmp_path / "rates.json"
    rates.write_text(
        json.dumps(
            {
                "chat": {"vendor/chat": {"input_new": 2, "output": 3}},
                "decision": {"vendor/native": {"input": 1, "output": 0}},
            }
        ),
        encoding="utf-8",
    )
    runner = CliRunner()
    outputs = []
    for mode in ("--usage", "--decisions"):
        result = runner.invoke(
            app, ["logs", sid, mode, "--rates", str(rates), "--json"]
        )
        assert result.exit_code == 0, result.output
        outputs.append(json.loads(result.output))
    usage, review = outputs
    assert usage["cost"] == review["cost"]
    assert usage["cost"]["incomplete_trace"] is (damage is not None)
    assert (usage["cost"]["total_usd"] is None) is (damage is not None)
    excluded = damage in {"duplicate_call", "missing_call", "missing_request_id"}
    assert len(usage["rows"]) == (1 if excluded else 2)
    assert review["usage"]["calls"] == len(usage["rows"])
    assert usage["cost"]["known_usd"] == pytest.approx(
        0.00023 + (0 if excluded else 0.0001)
    )
    assert review["malformed_lines"] == bool(extra)


def test_usage_does_not_open_decision_images(tmp_path, monkeypatch):
    directory = paths.sessions_dir() / "usage-only-abc123"
    directory.mkdir(parents=True)
    projection = {
        "event": "decision_projection",
        "turn": 0,
        "mode": "shadow",
        "evaluation": {"status": "scheduled", "request_id": "pending"},
        "image": {"path": "images/never-open.jpg", "sha256": "image-hash"},
        "projection": {
            "image": {"observation_id": "view", "sha256": "image-hash"},
            "state": {"observation": {"id": "view"}},
            "profile": {"questions": {"target": {"options": {}}}},
        },
    }
    (directory / "events.jsonl").write_text(json.dumps(projection), encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("usage must not validate or read decision images")

    monkeypatch.setattr("physiclaw.agent.trace.decisions._image_path", forbidden)
    result = CliRunner().invoke(app, ["logs", directory.name, "--usage", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []
