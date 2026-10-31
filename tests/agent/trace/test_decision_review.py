"""Offline review must preserve evidence identity and unknown outcomes."""

import copy
import hashlib
import json

import pytest

from physiclaw.agent.trace.accounting import read_json
from physiclaw.agent.trace.decisions import label_template, report


def _projection(directory, request_id, turn=0):
    raw = f"exact original image for {request_id}".encode()
    image_path = f"images/{request_id}.jpg"
    path = directory / image_path
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(raw)
    image_hash = hashlib.sha256(raw).hexdigest()
    observation_id = f"observation-{request_id}"
    return {
        "event": "decision_projection",
        "mode": "shadow",
        "turn": turn,
        "evaluation": {"status": "scheduled", "request_id": request_id},
        "image": {"path": image_path, "sha256": image_hash},
        "projection": {
            "blockers": [],
            "image": {"observation_id": observation_id, "sha256": image_hash},
            "state": {
                "facts": {
                    "task": {"text": "Open the requested app"},
                    "current_step": {"text": "Tap Calendar"},
                },
                "observation": {
                    "id": observation_id,
                    "ocr": [
                        {
                            "ref": f"{observation_id}:0",
                            "bbox": [0.1, 0.2, 0.3, 0.4],
                            "text": "Calendar",
                        },
                        {
                            "ref": f"{observation_id}:2",
                            "bbox": [0.5, 0.6, 0.7, 0.8],
                            "text": "Clock",
                        },
                    ],
                    "icons": [],
                },
            },
            "profile": {
                "questions": {
                    "target": {
                        "options": {
                            "NONE": {"description": "No supported target"},
                            "b0": {"target_ref": f"{observation_id}:0"},
                            "b2": {"target_ref": f"{observation_id}:2"},
                        }
                    }
                }
            },
        },
    }


def _call(projection, *, choice="b0", gate="passed", status="answered", elapsed=100):
    image = projection["projection"]["image"]
    action = None
    if choice != "YIELD":
        index = 0 if choice == "b0" else 1
        element = projection["projection"]["state"]["observation"]["ocr"][index]
        action = {
            "id": choice,
            "tool": "tap",
            "arguments": {"bbox": element["bbox"]},
            "target_ref": element["ref"],
        }
    return {
        "event": "decision_call",
        "mode": projection["mode"],
        "turn": projection["turn"],
        "request_id": projection["evaluation"]["request_id"],
        "observation_id": image["observation_id"],
        "status": status,
        "model": "test/decision",
        "response_model": "private-model-answer-sentinel",
        "answers": {"target": {"selected_id": choice}},
        "interpretation": {
            "reason": "model_yield" if choice == "YIELD" else "hypothetical_action",
            "action": action,
        },
        "threshold_gate": {"status": gate},
        "view_issue": None,
        "elapsed_ms": elapsed,
        "usage": {"input_tokens": 120, "output_tokens": 0},
    }


def _write(directory, events):
    (directory / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )


def _labelled(directory, expected):
    document = label_template(directory)
    for case in document["cases"]:
        label = expected.get(case["request_id"])
        case["label"]["expected"] = label
        if label == "tap":
            case["label"]["acceptable_targets"] = ["b0"]
    return document


@pytest.mark.parametrize("elapsed", [10**400, -(10**400), None, True, "100", -1])
def test_report_invalid_latency_is_missing_without_losing_the_decision(
    tmp_path, elapsed
):
    projection = _projection(tmp_path, "r0")
    _write(tmp_path, [projection, _call(projection, elapsed=elapsed)])

    result = report(tmp_path)

    assert result["latency"]["all_attempts"] == {
        "samples": 0,
        "missing": 1,
        "p50_ms": None,
        "p95_ms": None,
    }
    assert result["cases"][0]["status"] == "answered"


def test_blind_template_contains_only_original_evidence(tmp_path):
    projection = _projection(tmp_path, "r0")
    answer = _call(projection)
    future = {
        "event": "response",
        "turn": 1,
        "presentation": "model",
        "tool_calls": [{"id": "later", "name": "future-llm-action-sentinel"}],
    }
    _write(tmp_path, [projection, answer, future])

    template = label_template(tmp_path)
    case = template["cases"][0]
    text = json.dumps(template)

    assert case["evidence"] == projection["projection"]
    assert case["image_path"] == projection["image"]["path"]
    assert case["label"] == {
        "expected": None,
        "acceptable_targets": [],
        "note": "",
    }
    assert "private-model-answer-sentinel" not in text
    assert "future-llm-action-sentinel" not in text
    assert not {"answers", "threshold_gate", "interpretation", "mode", "call"} & set(
        case
    )
    _write(tmp_path, [projection])
    assert label_template(tmp_path) == template


@pytest.mark.parametrize(
    "field",
    [
        "session_id",
        "turn",
        "observation_id",
        "image_sha256",
        "projection_sha256",
        "evidence",
    ],
)
def test_labels_cannot_be_rebound_to_different_evidence(tmp_path, field):
    projection = _projection(tmp_path, "r0")
    _write(tmp_path, [projection, _call(projection)])
    document = _labelled(tmp_path, {"r0": "tap"})
    document["cases"][0][field] = 99 if field == "turn" else "different"

    with pytest.raises(ValueError, match="evidence mismatch"):
        report(tmp_path, labels=document)


@pytest.mark.parametrize("targets", [["b1"], ["NONE"], ["b0", "b0"], []])
def test_tap_labels_require_real_unique_offered_indices(tmp_path, targets):
    projection = _projection(tmp_path, "r0")
    _write(tmp_path, [projection, _call(projection)])
    document = _labelled(tmp_path, {"r0": "tap"})
    document["cases"][0]["label"]["acceptable_targets"] = targets

    with pytest.raises(ValueError, match="offered targets"):
        report(tmp_path, labels=document)


def test_duplicate_orphan_and_missing_calls_remain_distinct(tmp_path):
    duplicated_projection = _projection(tmp_path, "duplicate-projection", 0)
    duplicated_call = _projection(tmp_path, "duplicate-call", 1)
    orphan = _projection(tmp_path, "orphan", 2)
    pending = _projection(tmp_path, "pending", 3)
    _write(
        tmp_path,
        [
            duplicated_projection,
            copy.deepcopy(duplicated_projection),
            _call(duplicated_projection),
            duplicated_call,
            _call(duplicated_call),
            _call(duplicated_call),
            _call(orphan),
            pending,
            {
                "event": "decision_projection",
                "evaluation": {"status": "skipped", "reason": "busy"},
            },
            {
                "event": "decision_projection",
                "evaluation": {"status": "skipped", "reason": "blocked"},
            },
        ],
    )

    result = report(tmp_path)

    assert result["opportunities"]["projections"] == 6
    assert result["opportunities"]["calls"] == 4
    assert result["opportunities"]["skipped"] == {"busy": 1, "blocked": 1}
    assert {item["request_id"] for item in result["opportunities"]["invalid"]} == {
        "duplicate-projection",
        "duplicate-call",
        "orphan",
    }
    assert len(result["cases"]) == 1
    assert result["cases"][0]["request_id"] == "pending"
    assert result["cases"][0]["status"] == "missing_call"


@pytest.mark.parametrize("field,value", [("turn", 2), ("observation_id", "other")])
def test_call_must_match_original_turn_and_observation(tmp_path, field, value):
    projection = _projection(tmp_path, "r0")
    call = _call(projection)
    call[field] = value
    _write(tmp_path, [projection, call])

    result = report(tmp_path)

    assert result["cases"] == []
    assert result["opportunities"]["invalid"] == [
        {"request_id": "r0", "reason": "invalid_binding"}
    ]


def test_terminal_skip_is_not_a_missing_call_or_correct_model_yield(tmp_path):
    projection = _projection(tmp_path, "r0")
    terminal = {
        "event": "decision_skipped",
        "mode": "shadow",
        "turn": 0,
        "request_id": "r0",
        "observation_id": projection["projection"]["image"]["observation_id"],
        "reason": "provider_unavailable",
    }
    _write(tmp_path, [projection, terminal])
    labels = _labelled(tmp_path, {"r0": "yield"})

    result = report(tmp_path, labels=labels)

    assert result["cases"][0]["status"] == "skipped_before_call"
    assert result["cases"][0]["gated_correct"] is None
    assert result["opportunities"]["terminal_skips"] == {"provider_unavailable": 1}
    assert result["opportunities"]["calls"] == 0
    assert result["latency"]["all_attempts"]["samples"] == 0
    assert result["usage"]["calls"] == 0


@pytest.mark.parametrize(
    "damage", ["missing", "tampered", "wrong_attachment_hash", "invalid_attachment"]
)
def test_missing_or_tampered_image_never_counts_as_verified_label(tmp_path, damage):
    projection = _projection(tmp_path, "r0")
    _write(tmp_path, [projection, _call(projection)])
    labels = _labelled(tmp_path, {"r0": "tap"})
    path = tmp_path / projection["image"]["path"]
    if damage == "missing":
        path.unlink()
    elif damage == "tampered":
        path.write_bytes(b"different image")
    elif damage == "wrong_attachment_hash":
        projection["image"]["sha256"] = "wrong"
    else:
        projection["image"] = "invalid attachment"
    _write(tmp_path, [projection, _call(projection)])

    result = report(tmp_path, labels=labels)

    assert result["cases"][0]["image_path"] is None
    assert result["cases"][0]["raw_correct"] is None
    assert result["accuracy"]["counts"]["unreviewable"] == 1
    assert result["accuracy"]["raw_accuracy"] is None


def test_image_reference_cannot_escape_the_session_even_with_matching_hash(tmp_path):
    directory = tmp_path / "session"
    directory.mkdir()
    projection = _projection(directory, "r0")
    original = directory / projection["image"]["path"]
    external = tmp_path / "external.jpg"
    external.write_bytes(original.read_bytes())
    projection["image"]["path"] = "../external.jpg"
    _write(directory, [projection, _call(projection)])

    template = label_template(directory)

    assert template["cases"][0]["image_path"] is None


def test_accuracy_gates_and_coverage_keep_errors_and_unknowns_visible(tmp_path):
    projections = [_projection(tmp_path, f"r{n}", n) for n in range(8)]
    calls = [
        _call(projections[0], elapsed=100),
        _call(projections[1], choice="b2", gate="failed", elapsed=200),
        _call(projections[2], choice="YIELD", gate="not_applicable", elapsed=300),
        _call(projections[3], status="error", elapsed=2000),
        _call(projections[4], elapsed=400),
        _call(projections[5], elapsed=500),
        _call(projections[6], elapsed=600),
    ]
    calls[6]["view_issue"] = "stale_view"
    _write(tmp_path, [*projections, *calls])
    labels = _labelled(
        tmp_path,
        {
            "r0": "tap",
            "r1": "yield",
            "r2": "tap",
            "r3": "yield",
            "r4": "unknown",
            "r6": "tap",
            "r7": "tap",
        },
    )

    result = report(tmp_path, labels=labels)

    counts = result["accuracy"]["counts"]
    assert counts["labelled"] == 6
    assert counts["tap_opportunities"] == 4
    assert counts["unknown_labels"] == counts["unlabelled"] == 1
    assert counts["raw_unscored"] == counts["gated_unscored"] == 2
    assert counts["raw_scored"] == counts["gated_scored"] == 4
    assert counts["correct_yields"] == 1
    assert result["accuracy"]["raw_accuracy"] == 0.5
    assert result["accuracy"]["gated_accuracy"] == 0.5
    assert result["accuracy"]["gated_tap_precision"] == 1.0
    assert result["accuracy"]["useful_tap_coverage"] == 0.25
    by_id = {case["request_id"]: case for case in result["cases"]}
    assert by_id["r1"]["raw_correct"] is False
    assert by_id["r1"]["gated_correct"] is True
    assert by_id["r3"]["gated_action"] is None
    assert by_id["r3"]["gated_correct"] is None
    assert result["latency"]["all_attempts"]["samples"] == 7
    assert result["latency"]["all_attempts"]["p50_ms"] == 400
    assert result["latency"]["all_attempts"]["p95_ms"] == pytest.approx(1580)
    assert result["latency"]["answered"]["samples"] == 6
    assert result["latency"]["answered"]["p50_ms"] == 350
    assert result["latency"]["answered"]["p95_ms"] == pytest.approx(575)


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("not_proposed", "not_proposed"),
        ("not_selected", "not_selected"),
        ("no_receipt", "outcome_unknown"),
        ("completed", "completed"),
        ("missing_outcome_flag", "outcome_unknown"),
        ("partial_failure", "failed"),
        ("exception", "failed"),
        ("refused", "refused"),
        ("expired", "refused"),
        ("duplicate_receipt", "invalid_join"),
        ("duplicate_selection", "invalid_join"),
        ("receipt_without_selection", "invalid_join"),
        ("different_turn", "invalid_join"),
        ("different_arguments", "invalid_join"),
    ],
)
def test_execution_requires_exact_selection_and_receipt_join(
    tmp_path, outcome, expected
):
    projection = _projection(tmp_path, "r0")
    projection["mode"] = "active"
    call = _call(projection)
    call["proposal_call_id"] = "proposed-tap"
    action = call["interpretation"]["action"]
    selected = {
        "event": "response",
        "turn": 0,
        "presentation": "plugin",
        "tool_calls": [
            {
                "id": "proposed-tap",
                "name": action["tool"],
                "arguments": action["arguments"],
            }
        ],
    }
    receipt = {
        "event": "tool_result",
        "turn": 0,
        "id": "proposed-tap",
        "name": "tap",
        "arguments": copy.deepcopy(action["arguments"]),
        "is_error": False,
        "changed": True,
        "elapsed_ms": 900,
    }
    events = [projection, call]
    if outcome == "not_proposed":
        del call["proposal_call_id"]
    elif outcome != "not_selected":
        if outcome != "receipt_without_selection":
            events.append(selected)
            if outcome == "duplicate_selection":
                events.append(copy.deepcopy(selected))
        if outcome != "no_receipt":
            if outcome == "partial_failure":
                receipt["is_error"] = True
            elif outcome == "missing_outcome_flag":
                del receipt["is_error"]
            elif outcome == "exception":
                receipt["event"] = "tool_error"
            elif outcome == "refused":
                receipt["event"] = "tool_blocked_layout"
            elif outcome == "expired":
                receipt["event"] = "tool_blocked_expired"
            elif outcome == "different_turn":
                receipt["turn"] = 1
            elif outcome == "different_arguments":
                receipt["arguments"]["bbox"] = [0.5, 0.6, 0.7, 0.8]
            events.append(receipt)
            if outcome == "duplicate_receipt":
                events.append(copy.deepcopy(receipt))
    _write(tmp_path, events)

    result = report(tmp_path)

    assert result["cases"][0]["execution"]["status"] == expected


def test_report_reuses_image_hashes_and_indexes_nearest_llm_turns(
    tmp_path, monkeypatch
):
    first = _projection(tmp_path, "r0", 0)
    second = _projection(tmp_path, "r1", 1)
    second["image"] = dict(first["image"])
    second["projection"]["image"]["sha256"] = first["image"]["sha256"]
    _write(
        tmp_path,
        [
            first,
            second,
            _call(first),
            _call(second),
            {"event": "response", "presentation": "model", "turn": 5},
            {"event": "response", "presentation": "model", "turn": 1},
            {"event": "response", "presentation": "model", "turn": 3},
        ],
    )
    reads = []
    original = type(tmp_path).read_bytes

    def read_bytes(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(type(tmp_path), "read_bytes", read_bytes)

    result = report(tmp_path)

    assert reads.count((tmp_path / first["image"]["path"]).resolve()) == 1
    assert result["cases"][0]["next_llm_turn"] == 1
    assert result["cases"][0]["llm_same_turn"] is False
    assert result["cases"][1]["llm_same_turn"] is True
    assert result["cases"][1]["next_llm_turn"] == 3
    assert result["session"]["llm_turns"] == 3


@pytest.mark.parametrize("damage", ["duplicate_call", "missing_call", "bad_json"])
def test_incomplete_trace_cannot_claim_complete_cost(tmp_path, damage):
    projection = _projection(tmp_path, "r0")
    call = _call(projection)
    events = [projection]
    if damage != "missing_call":
        events.append(call)
    if damage == "duplicate_call":
        events.append(copy.deepcopy(call))
    _write(tmp_path, events)
    if damage == "bad_json":
        with (tmp_path / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write('{"event":\n')

    result = report(
        tmp_path,
        rates={"decision": {"test/decision": {"input": 1.0, "output": 0.0}}},
    )

    assert result["cost"]["total_usd"] is None
    assert result["cost"]["incomplete_trace"] is True
    if damage == "duplicate_call":
        assert result["usage"]["calls"] == 0
        assert result["latency"]["all_attempts"]["samples"] == 0
        assert result["latency"]["answered"]["samples"] == 0
    if damage == "bad_json":
        assert result["malformed_lines"] == 1
        assert result["cost"]["known_usd"] == pytest.approx(120 / 1_000_000)


@pytest.mark.parametrize("text", ['{"cases":[],"cases":[]}', '{"cost":NaN}'])
def test_ambiguous_or_nonfinite_label_files_are_rejected(tmp_path, text):
    path = tmp_path / "labels.json"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError):
        read_json(path)
