"""Offline accounting for chat and native decision calls in one session."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from physiclaw.contract.dto import USAGE_BUCKETS

_BILLABLE = {
    "chat": ("input_new", "cache_read", "cache_write", "output"),
    "decision": ("input", "output"),
}


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON value: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        _reject_constant(value)
    return number


def _parse_json(data: str | bytes) -> Any:
    try:
        value = json.loads(
            data,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except RecursionError:
        raise ValueError("JSON exceeds maximum nesting depth") from None
    # Bound later hashing/rendering too, independent of a library raising the
    # interpreter recursion limit. Walking iteratively cannot repeat that crash.
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise ValueError("JSON exceeds maximum nesting depth")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


def read_json(path: Path) -> dict:
    value = _parse_json(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def read_events(directory: Path) -> tuple[list[dict], int, str]:
    data = (directory / "events.jsonl").read_bytes()
    events, malformed = [], 0
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            event = _parse_json(line)
            if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                raise ValueError("expected an event")
            events.append(event)
        except ValueError:
            malformed += 1
    return events, malformed, hashlib.sha256(data).hexdigest()


def prepare_accounting(events: list[dict], *, malformed_lines: int = 0) -> dict:
    """Join decision lifecycles and prepare one conservative accounting view.

    This validates recorded request identity without opening image files. Both
    usage and decision review use the same duplicate and completeness rules.
    """
    projections: dict[str, list[dict]] = defaultdict(list)
    calls: dict[str, list[dict]] = defaultdict(list)
    terminals: dict[str, list[dict]] = defaultdict(list)
    skips: Counter[str] = Counter()
    invalid = []
    for event in events:
        if event["event"] == "decision_projection":
            evaluation = event.get("evaluation")
            if not isinstance(evaluation, dict):
                evaluation = {}
            if evaluation.get("status") == "skipped":
                skips[str(evaluation.get("reason") or "unknown")] += 1
            elif evaluation.get("status") == "scheduled" and isinstance(
                evaluation.get("request_id"), str
            ):
                projections[evaluation["request_id"]].append(event)
            else:
                invalid.append({"reason": "invalid_projection"})
        elif event["event"] == "decision_call":
            if isinstance(event.get("request_id"), str):
                calls[event["request_id"]].append(event)
            else:
                invalid.append({"reason": "missing_request_id"})
        elif event["event"] == "decision_skipped":
            if isinstance(event.get("request_id"), str):
                terminals[event["request_id"]].append(event)
            else:
                invalid.append({"reason": "missing_request_id"})
    records = []
    for request_id in sorted(projections.keys() | calls.keys() | terminals.keys()):
        sources, replies, skipped = (
            projections[request_id],
            calls[request_id],
            terminals[request_id],
        )
        if len(sources) != 1 or len(replies) + len(skipped) > 1:
            invalid.append({"request_id": request_id, "reason": "ambiguous_or_orphan"})
            continue
        event = sources[0]
        projection = event.get("projection")
        try:
            if not isinstance(projection, dict):
                raise ValueError("invalid projection")
            image = projection["image"]
            observation_id = image["observation_id"]
            if (
                type(event["turn"]) is not int
                or event["turn"] < 0
                or event["mode"] not in {"active", "shadow"}
                or observation_id != projection["state"]["observation"]["id"]
                or not isinstance(image["sha256"], str)
                or not isinstance(
                    projection["profile"]["questions"]["target"]["options"], dict
                )
            ):
                raise ValueError("invalid projection")
            terminal = (replies or skipped)[0] if replies or skipped else None
            if terminal is not None and any(
                terminal.get(key) != expected
                for key, expected in {
                    "turn": event["turn"],
                    "mode": event["mode"],
                    "observation_id": observation_id,
                }.items()
            ):
                raise ValueError("mismatched call")
            records.append(
                {
                    "request_id": request_id,
                    "turn": event["turn"],
                    "observation_id": observation_id,
                    "image_sha256": image["sha256"],
                    "projection_event": event,
                    "evidence": projection,
                    "mode": event["mode"],
                    "call": replies[0] if replies else None,
                    "terminal_skip": skipped[0].get("reason") if skipped else None,
                }
            )
        except (KeyError, TypeError, ValueError):
            invalid.append({"request_id": request_id, "reason": "invalid_binding"})
    records.sort(key=lambda record: (record["turn"], record["request_id"]))
    opportunities = {
        "projections": sum(e["event"] == "decision_projection" for e in events),
        "scheduled": sum(map(len, projections.values())),
        "calls": sum(e["event"] == "decision_call" for e in events),
        "skipped": dict(skips),
        "terminal_skips": dict(
            Counter(
                str(event.get("reason") or "unknown")
                for entries in terminals.values()
                for event in entries
            )
        ),
        "invalid": invalid,
    }

    unique_calls = {
        request_id for request_id, replies in calls.items() if len(replies) == 1
    }
    rows = usage_rows(
        event
        for event in events
        if event["event"] != "decision_call"
        or (
            isinstance(event.get("request_id"), str)
            and event["request_id"] in unique_calls
        )
    )
    return {
        "decisions": records,
        "opportunities": opportunities,
        "rows": rows,
        "incomplete_trace": bool(
            malformed_lines
            or invalid
            or any(
                record["call"] is None and record["terminal_skip"] is None
                for record in records
            )
        ),
    }


def session_cost(accounting: dict, rates: Mapping[str, Any]) -> dict[str, Any]:
    """Estimate recorded usage while retaining uncertainty from its journal."""
    cost = estimate_cost(accounting["rows"], rates)
    cost["incomplete_trace"] = accounting["incomplete_trace"]
    if cost["incomplete_trace"]:
        cost["total_usd"] = None
    return cost


def usage_rows(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Read the one accounting event for each call, preserving reported values.

    Chat ``micro_call`` and decision projections describe calls already accounted
    for by ``usage`` and ``decision_call``; counting them would double the bill.
    The chat contract uses all-zero counts to mean unreported usage. Native
    decision usage distinguishes an actual zero from an unreported ``None``.
    """
    rows = []
    for event in events:
        if event.get("event") == "usage":
            row = {k: v for k, v in event.items() if k not in ("event", "t")}
            row["api_kind"] = "chat"
            row["usage_missing"] = all(
                event.get(bucket, 0) == 0 for bucket in USAGE_BUCKETS
            )
        elif event.get("event") == "decision_call":
            row = {
                key: event.get(key)
                for key in (
                    "turn",
                    "model",
                    "response_model",
                    "request_id",
                    "provider_request_id",
                    "mode",
                    "status",
                    "elapsed_ms",
                    "provider_elapsed_ms",
                    "error",
                )
            }
            usage = event.get("usage")
            usage = usage if isinstance(usage, Mapping) else {}
            row.update(
                api_kind="decision",
                call="decision",
                input=usage.get("input_tokens"),
                output=usage.get("output_tokens"),
                usage_missing=False,
            )
        else:
            continue
        rows.append(row)
    return rows


def usage_error(row: Mapping[str, Any]) -> str | None:
    """Reject an entire damaged accounting row rather than partially total it."""
    model = row.get("model")
    if model is not None and not isinstance(model, str):
        return "invalid model"
    for bucket in USAGE_BUCKETS:
        value = row.get(bucket)
        if value is not None and (type(value) is not int or value < 0):
            return f"invalid {bucket}"
    elapsed = row.get("elapsed_ms")
    if elapsed is not None and (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or elapsed < 0
        or (isinstance(elapsed, float) and not math.isfinite(elapsed))
    ):
        return "invalid elapsed_ms"
    return None


def token_count(row: Mapping[str, Any], bucket: str) -> int | None:
    """Read a validated token bucket without turning missing usage into zero."""
    return None if row.get("usage_missing") else row.get(bucket)


def summarize_usage(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Known subtotals and missing counts, grouped by API kind and model."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    calls = excluded = 0
    for row in rows:
        calls += 1
        if usage_error(row) is not None:
            excluded += 1
            continue
        key = (str(row.get("api_kind") or "?"), str(row.get("model") or "?"))
        if key not in groups:
            groups[key] = {
                "api_kind": key[0],
                "model": key[1],
                "calls": 0,
                "failed": 0,
                "elapsed_ms": {"known": 0, "missing": 0},
                "tokens": {
                    bucket: {"known": 0, "missing": 0} for bucket in USAGE_BUCKETS
                },
            }
        group = groups[key]
        group["calls"] += 1
        group["failed"] += bool(
            row.get("error") or row.get("status") in ("error", "cancelled")
        )
        for bucket, counter in group["tokens"].items():
            value = token_count(row, bucket)
            counter["missing" if value is None else "known"] += (
                1 if value is None else value
            )
        elapsed = row.get("elapsed_ms")
        group["elapsed_ms"]["missing" if elapsed is None else "known"] += (
            1 if elapsed is None else elapsed
        )
    return {"calls": calls, "excluded_rows": excluded, "groups": list(groups.values())}


def estimate_cost(
    rows: Iterable[Mapping[str, Any]], rates: Mapping[str, Any]
) -> dict[str, Any]:
    """Estimate USD using supplied per-million rates; never infer prices.

    Rates are ``{api_kind: {provider/model: {bucket: rate}}}``. Chat bills the
    input split and output; decision bills native input and output. Input totals
    and reasoning are already included in those buckets and must not be added.
    ``known_usd`` includes known portions of incomplete calls; ``total_usd``
    remains unknown until every call's billable counts and rates are known.
    """
    _validate_rates(rates)
    known_usd = 0.0
    missing_calls = 0
    for row in rows:
        api_kind = row.get("api_kind")
        if usage_error(row) is not None or api_kind not in _BILLABLE:
            missing_calls += 1
            continue
        model_rates = rates.get(api_kind, {}).get(row.get("model"), {})
        incomplete = False
        for bucket in _BILLABLE[api_kind]:
            count = token_count(row, bucket)
            rate = model_rates.get(bucket)
            if count is None or (count != 0 and rate is None):
                incomplete = True
            elif count:
                try:
                    known_usd += count * rate / 1_000_000
                except OverflowError as exc:
                    raise ValueError("cost estimate exceeds numeric range") from exc
        missing_calls += incomplete
    if not math.isfinite(known_usd):
        raise ValueError("cost estimate exceeds numeric range")
    return {
        "currency": "USD",
        "known_usd": known_usd,
        "missing_calls": missing_calls,
        "total_usd": None if missing_calls else known_usd,
    }


def _validate_rates(rates: Mapping[str, Any]) -> None:
    if not isinstance(rates, Mapping):
        raise ValueError("rates must be an object keyed by chat or decision")
    for api_kind, models in rates.items():
        if api_kind not in _BILLABLE or not isinstance(models, Mapping):
            raise ValueError(f"invalid rates for API kind {api_kind!r}")
        for model, buckets in models.items():
            if (
                not isinstance(model, str)
                or not model
                or not isinstance(buckets, Mapping)
            ):
                raise ValueError("rates must contain model names and bucket objects")
            for bucket, rate in buckets.items():
                if bucket not in _BILLABLE[api_kind]:
                    raise ValueError(f"unsupported {api_kind} rate bucket {bucket!r}")
                if (
                    isinstance(rate, bool)
                    or not isinstance(rate, (int, float))
                    or rate < 0
                    or (isinstance(rate, float) and not math.isfinite(rate))
                ):
                    raise ValueError(
                        f"rate for {model}/{bucket} must be finite and >= 0"
                    )
