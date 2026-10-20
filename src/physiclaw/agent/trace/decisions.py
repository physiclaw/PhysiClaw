"""Offline decision review from recorded evidence; never runs a model or tool."""

import hashlib
import json
import math
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path

from physiclaw.agent.trace.accounting import (
    prepare_accounting,
    read_events,
    session_cost,
    summarize_usage,
)
from physiclaw.agent.trace.store import load_summary


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _array(value: object) -> list:
    return value if isinstance(value, list) else []


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def _image_path(
    directory: Path, event: dict, expected_hash: str, hashes: dict[Path, str]
) -> str | None:
    image = _object(event.get("image"))
    relative = image.get("path")
    if not isinstance(relative, str) or image.get("sha256") != expected_hash:
        return None
    try:
        path = (directory / relative).resolve()
        path.relative_to(directory.resolve())
        if path not in hashes:
            hashes[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        if hashes[path] == expected_hash:
            return relative
    except (OSError, ValueError):
        pass
    return None


def _cases(directory: Path, records: list[dict]) -> list[dict]:
    hashes: dict[Path, str] = {}
    return [
        {
            **{
                key: value for key, value in record.items() if key != "projection_event"
            },
            "session_id": directory.name,
            "projection_sha256": _digest(record["evidence"]),
            "image_path": _image_path(
                directory, record["projection_event"], record["image_sha256"], hashes
            ),
        }
        for record in records
    ]


def label_template(directory: Path) -> dict:
    """Export only pre-decision evidence; exclude answers and future actions."""
    events, malformed, _ = read_events(directory)
    accounting = prepare_accounting(
        [event for event in events if event["event"] == "decision_projection"],
        malformed_lines=malformed,
    )
    cases = _cases(directory, accounting["decisions"])
    return {
        "session_id": directory.name,
        "malformed_lines": malformed,
        "instructions": (
            "Label from this projection and its exact image only. expected: tap, "
            "yield, unknown, or null (unlabelled). For tap, list all acceptable "
            "original bN targets. Missing image means unreviewable. Do not use "
            "model answers or later LLM actions as ground truth."
        ),
        "cases": [
            {
                **{
                    key: value
                    for key, value in case.items()
                    if key not in {"call", "mode", "terminal_skip"}
                },
                "label": {"expected": None, "acceptable_targets": [], "note": ""},
            }
            for case in cases
        ],
    }


def _labels(document: dict | None, cases: list[dict], session_id: str) -> dict:
    if document is None:
        return {}
    if document.get("session_id") != session_id or not isinstance(
        document.get("cases"), list
    ):
        raise ValueError("labels must belong to this session")
    indexed = {case["request_id"]: case for case in cases}
    labels = {}
    for record in document["cases"]:
        if not isinstance(record, dict):
            raise ValueError("invalid label record")
        request_id = record.get("request_id")
        if (
            not isinstance(request_id, str)
            or request_id not in indexed
            or request_id in labels
        ):
            raise ValueError("unknown or duplicate labelled request")
        case = indexed[request_id]
        if (
            any(
                record.get(key) != case[key]
                for key in (
                    "session_id",
                    "turn",
                    "observation_id",
                    "image_sha256",
                    "projection_sha256",
                )
            )
            or _digest(record.get("evidence")) != case["projection_sha256"]
        ):
            raise ValueError(f"label evidence mismatch: {request_id}")
        label = record.get("label")
        if not isinstance(label, dict) or label.get("expected") not in (
            None,
            "tap",
            "yield",
            "unknown",
        ):
            raise ValueError("invalid expected action")
        targets = label.get("acceptable_targets")
        offered = case["evidence"]["profile"]["questions"]["target"]["options"]
        if (
            not isinstance(targets, list)
            or any(
                not isinstance(t, str) or t == "NONE" or t not in offered
                for t in targets
            )
            or len(set(targets)) != len(targets)
            or (label["expected"] == "tap") != bool(targets)
        ):
            raise ValueError(
                "tap labels require offered targets; other labels require none"
            )
        labels[request_id] = label
    return labels


def _execution(call: dict, selections: dict, outcomes: dict) -> dict:
    call_id = call.get("proposal_call_id")
    if not call_id:
        return {"status": "not_proposed"}
    if not isinstance(call_id, str):
        return {"status": "invalid_join", "call_id": call_id}
    selected = selections.get((call["turn"], call_id), [])
    receipts = outcomes.get(call_id, [])
    if len(selected) > 1 or len(receipts) > 1 or (receipts and not selected):
        return {"status": "invalid_join", "call_id": call_id}
    if not selected:
        return {"status": "not_selected", "call_id": call_id}
    action = _object(_object(call.get("interpretation")).get("action"))
    if selected[0].get("name") != action.get("tool") or selected[0].get(
        "arguments"
    ) != action.get("arguments"):
        return {"status": "invalid_join", "call_id": call_id}
    if not receipts:
        return {"status": "outcome_unknown", "call_id": call_id}
    receipt = receipts[0]
    if (
        receipt.get("turn") != call["turn"]
        or receipt.get("name") != selected[0]["name"]
        or (
            "arguments" in receipt
            and receipt["arguments"] != selected[0].get("arguments")
        )
    ):
        return {"status": "invalid_join", "call_id": call_id}
    kind = receipt["event"]
    if kind == "tool_result":
        status = (
            "failed"
            if receipt.get("is_error") is True
            else "completed"
            if receipt.get("is_error") is False
            else "outcome_unknown"
        )
    else:
        status = "failed" if kind == "tool_error" else "refused"
    return {
        "status": status,
        "call_id": call_id,
        "changed": receipt.get("changed"),
        "elapsed_ms": receipt.get("elapsed_ms"),
    }


def _actions(call: dict | None) -> tuple[str | None, str | None]:
    if call is None or call.get("status") != "answered":
        return None, None
    interpretation = _object(call.get("interpretation"))
    action = interpretation.get("action")
    if interpretation.get("reason") == "hypothetical_action" and isinstance(
        action, dict
    ):
        raw = action.get("id") if action.get("tool") == "tap" else None
    elif interpretation.get("reason") in ("non_routine", "model_yield", "no_target"):
        raw = "YIELD"
    else:
        return None, None
    gate = _object(call.get("threshold_gate")).get("status")
    if (
        gate not in ("passed", "failed", "missing_scores", "not_applicable")
        or "view_issue" not in call
    ):
        return raw, None
    return raw, raw if gate == "passed" and call["view_issue"] is None else "YIELD"


def _latency(events: list[dict]) -> dict:
    values = sorted(
        e["elapsed_ms"]
        for e in events
        if type(e.get("elapsed_ms")) in (int, float)
        and math.isfinite(e["elapsed_ms"])
        and e["elapsed_ms"] >= 0
    )

    def percentile(fraction):
        if not values:
            return None
        position = (len(values) - 1) * fraction
        low = int(position)
        return values[low] + (values[math.ceil(position)] - values[low]) * (
            position - low
        )

    return {
        "samples": len(values),
        "missing": len(events) - len(values),
        "p50_ms": percentile(0.5),
        "p95_ms": percentile(0.95),
    }


def report(
    directory: Path, *, labels: dict | None = None, rates: dict | None = None
) -> dict:
    events, malformed, event_hash = read_events(directory)
    accounting = prepare_accounting(events, malformed_lines=malformed)
    cases = _cases(directory, accounting["decisions"])
    labelled = _labels(labels, cases, directory.name)
    selections: dict[tuple[int, str], list[dict]] = defaultdict(list)
    outcomes: dict[str, list[dict]] = defaultdict(list)
    llm_turns = set()
    llm_calls = 0
    for event in events:
        if event["event"] == "response":
            if event.get("presentation") == "model":
                llm_calls += 1
                if type(event.get("turn")) is int:
                    llm_turns.add(event["turn"])
            if type(event.get("turn")) is int:
                for tool in _array(event.get("tool_calls")):
                    if isinstance(tool, dict) and isinstance(tool.get("id"), str):
                        selections[(event["turn"], tool["id"])].append(tool)
        elif isinstance(event.get("id"), str) and (
            event["event"]
            in {"tool_result", "tool_error", "tool_invalid_args", "tool_unknown"}
            or event["event"].startswith("tool_blocked_")
        ):
            outcomes[event["id"]].append(event)
    ordered_llm_turns = sorted(llm_turns)
    metrics: Counter[str] = Counter()
    rows = []
    for case in cases:
        call = case.pop("call")
        label = labelled.get(case["request_id"], {})
        expected = label.get("expected")
        raw, gated = _actions(call)
        next_llm = bisect_right(ordered_llm_turns, case["turn"])
        offered = case["evidence"]["profile"]["questions"]["target"]["options"]
        if raw is not None and (
            not isinstance(raw, str) or (raw != "YIELD" and raw not in offered)
        ):
            raw, gated = None, None
        row = {
            **{key: value for key, value in case.items() if key != "evidence"},
            "status": call.get("status")
            if call
            else (
                "skipped_before_call"
                if case["terminal_skip"] is not None
                else "missing_call"
            ),
            "expected": expected,
            "raw_action": raw,
            "gated_action": gated,
            "raw_correct": None,
            "gated_correct": None,
            "execution": _execution(call, selections, outcomes)
            if call
            else {"status": "not_proposed"},
            "llm_same_turn": case["turn"] in llm_turns,
            "next_llm_turn": ordered_llm_turns[next_llm]
            if next_llm < len(ordered_llm_turns)
            else None,
        }
        if case["image_path"] is None:
            metrics["unreviewable"] += 1
        elif expected not in {"tap", "yield"}:
            metrics["unknown_labels" if expected == "unknown" else "unlabelled"] += 1
        else:
            metrics["labelled"] += 1
            metrics["tap_opportunities"] += expected == "tap"
            acceptable = label["acceptable_targets"] if expected == "tap" else ["YIELD"]
            for prefix, choice in (("raw", raw), ("gated", gated)):
                if choice is None:
                    metrics[f"{prefix}_unscored"] += 1
                    continue
                correct = choice in acceptable
                row[f"{prefix}_correct"] = correct
                metrics[f"{prefix}_scored"] += 1
                metrics[f"{prefix}_correct"] += correct
                if choice != "YIELD":
                    metrics[f"{prefix}_taps"] += 1
                    metrics[f"{prefix}_correct_taps"] += correct
            metrics["correct_yields"] += gated == "YIELD" and expected == "yield"
        rows.append(row)

    def ratio(numerator, denominator):
        return (
            metrics[numerator] / metrics[denominator] if metrics[denominator] else None
        )

    counts = dict(metrics)
    accuracy = {
        "counts": counts,
        "raw_accuracy": ratio("raw_correct", "raw_scored"),
        "gated_accuracy": ratio("gated_correct", "gated_scored"),
        "gated_tap_precision": ratio("gated_correct_taps", "gated_taps"),
        "useful_tap_coverage": ratio("gated_correct_taps", "tap_opportunities"),
    }
    usage = accounting["rows"]
    attempts = [row for row in usage if row["api_kind"] == "decision"]
    cost = session_cost(accounting, rates) if rates is not None else None
    summary = load_summary(directory) or {}
    return {
        "session_id": directory.name,
        "events_sha256": event_hash,
        "labels_sha256": _digest(labels) if labels is not None else None,
        "malformed_lines": malformed,
        "opportunities": accounting["opportunities"],
        "accuracy": accuracy,
        "execution": dict(Counter(row["execution"]["status"] for row in rows)),
        "latency": {
            "all_attempts": _latency(attempts),
            "answered": _latency(
                [e for e in attempts if e.get("status") == "answered"]
            ),
        },
        "session": {
            "duration_s": summary.get("duration_s"),
            "reported_outcome": summary.get("outcome"),
            "llm_turns": llm_calls,
        },
        "usage": summarize_usage(usage),
        "cost": cost,
        "rates_usd_per_million": rates,
        "cases": rows,
        "limitations": [
            "Labels are manual judgments, not LLM agreement.",
            "Completed/changed tool results do not prove intended task success.",
            "Later LLM turns are not automatically attributed to recovery.",
            "Latency covers model evaluation and local assessment, excluding projection, client construction and trace persistence.",
            "Shadow latency overlaps LLM work; shadow calls establish no net savings.",
            "Missing usage or rates leave total cost unknown.",
            "Ambiguous duplicate decision calls are excluded from usage and latency; incomplete traces cannot establish total cost.",
        ],
    }
