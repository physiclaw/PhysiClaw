"""Offline usage must preserve unknowns and never bill an aggregate twice."""

from copy import deepcopy

import pytest

from physiclaw.agent.trace.accounting import (
    estimate_cost,
    summarize_usage,
    usage_rows,
)
from physiclaw.contract.dto import Usage, usage_event


def chat(**patch):
    return {
        **usage_event(
            Usage(100, 20, 30, 10, 8),
            call="turn",
            model="vendor/model",
            elapsed_ms=1500,
        ),
        **patch,
    }


def decision(**patch):
    return {
        "event": "decision_call",
        "model": "vendor/model",
        "response_model": "actual-model",
        "turn": 0,
        "mode": "shadow",
        "status": "answered",
        "request_id": "request",
        "provider_request_id": "native",
        "elapsed_ms": 400,
        "usage": {"input_tokens": 200, "output_tokens": 0},
        **patch,
    }


def test_only_accounting_events_contribute_once_without_modifying_sources():
    events = [
        chat(),
        {"event": "micro_call", "input": 10000},
        {"event": "decision_projection", "evaluation": {"status": "scheduled"}},
        decision(),
        {"event": "response", "usage": {"input_tokens": 10000}},
        {"event": "decision_skipped", "reason": "busy"},
    ]
    original = deepcopy(events)

    rows = usage_rows(events)

    assert events == original
    assert [row["api_kind"] for row in rows] == ["chat", "decision"]
    assert rows[0]["input"] == 100
    assert rows[0]["input_new"] == 60
    assert rows[0]["reasoning"] == 8
    assert rows[1]["input"] == 200
    assert rows[1]["output"] == 0
    assert rows[1]["request_id"] == "request"
    assert rows[1]["response_model"] == "actual-model"
    assert "event" not in rows[0] and "event" not in rows[1]

    summary = summarize_usage(rows)
    assert summary["calls"] == 2
    assert summary["excluded_rows"] == 0
    first, second = summary["groups"]
    assert first["api_kind"] == "chat" and second["api_kind"] == "decision"
    assert first["model"] == second["model"] == "vendor/model"
    assert first["tokens"]["input"] == {"known": 100, "missing": 0}
    assert second["tokens"]["input"] == {"known": 200, "missing": 0}
    assert second["tokens"]["output"] == {"known": 0, "missing": 0}
    assert second["tokens"]["cache_read"] == {"known": 0, "missing": 1}


def test_failed_cancelled_and_partially_reported_decisions_preserve_missing_counts():
    rows = usage_rows(
        [
            decision(),
            decision(status="error", error="TimeoutError", usage=None),
            decision(status="cancelled", elapsed_ms=None, usage={"input_tokens": 25}),
        ]
    )
    assert rows[1]["input"] is None and rows[1]["output"] is None
    group = summarize_usage(rows)["groups"][0]

    assert group["calls"] == 3 and group["failed"] == 2
    assert group["tokens"]["input"] == {"known": 225, "missing": 1}
    assert group["tokens"]["output"] == {"known": 0, "missing": 2}
    assert group["elapsed_ms"] == {"known": 800, "missing": 1}


def test_all_zero_chat_is_unreported_but_native_decision_zero_is_known():
    missing_chat = usage_event(
        Usage(), call="turn", model="vendor/model", elapsed_ms=5, error="TimeoutError"
    )
    rows = usage_rows(
        [missing_chat, decision(usage={"input_tokens": 0, "output_tokens": 0})]
    )

    assert rows[0]["input"] == 0 and rows[0]["usage_missing"] is True
    assert rows[1]["usage_missing"] is False
    groups = summarize_usage(rows)["groups"]
    assert groups[0]["tokens"]["input"] == {"known": 0, "missing": 1}
    assert groups[1]["tokens"]["input"] == {"known": 0, "missing": 0}


@pytest.mark.parametrize("bad", [-1, True, 1.5, "12", [], float("nan"), float("inf")])
def test_bad_count_excludes_the_whole_row_from_known_subtotals(bad):
    rows = usage_rows([chat(output=bad), chat()])
    summary = summarize_usage(rows)

    assert summary["calls"] == 2 and summary["excluded_rows"] == 1
    assert summary["groups"][0]["calls"] == 1
    assert summary["groups"][0]["tokens"]["input"]["known"] == 100
    cost = estimate_cost(rows, {})
    assert cost["missing_calls"] == 2 and cost["total_usd"] is None


@pytest.mark.parametrize("bad", [-1, True, "12", [], float("nan"), float("inf")])
def test_bad_latency_does_not_partially_account_for_a_damaged_row(bad):
    summary = summarize_usage(usage_rows([decision(elapsed_ms=bad)]))

    assert summary == {"calls": 1, "excluded_rows": 1, "groups": []}


@pytest.mark.parametrize("model", [[], {}, True, 123])
def test_invalid_model_is_excluded_instead_of_crashing_rate_lookup(model):
    rows = usage_rows([decision(model=model)])

    assert summarize_usage(rows) == {"calls": 1, "excluded_rows": 1, "groups": []}
    assert estimate_cost(rows, {}) == {
        "currency": "USD",
        "known_usd": 0,
        "missing_calls": 1,
        "total_usd": None,
    }


def test_cost_uses_input_split_and_total_output_once_and_keeps_api_kinds_separate():
    rows = usage_rows([chat(), decision()])
    rates = {
        "chat": {
            "vendor/model": {
                "input_new": 2,
                "cache_read": 0.5,
                "cache_write": 3,
                "output": 5,
            }
        },
        "decision": {"vendor/model": {"input": 0.042}},
    }

    cost = estimate_cost(rows, rates)

    # Chat: 60*2 + 30*.5 + 10*3 + 20*5; decision: 200*.042.
    assert cost["known_usd"] == pytest.approx(273.4 / 1_000_000)
    assert cost["total_usd"] == cost["known_usd"]
    assert cost["missing_calls"] == 0 and cost["currency"] == "USD"


def test_cost_reports_partial_known_subtotal_without_claiming_a_complete_bill():
    rows = usage_rows(
        [
            chat(),
            decision(usage={"input_tokens": None, "output_tokens": 0}),
            decision(),
        ]
    )
    rates = {
        "chat": {"vendor/model": {"input_new": 2}},
        "decision": {"vendor/model": {"input": 0}},
    }

    cost = estimate_cost(rows, rates)

    assert cost["known_usd"] == pytest.approx(120 / 1_000_000)
    assert cost["missing_calls"] == 2
    assert cost["total_usd"] is None  # A zero rate cannot make unknown tokens known.


def test_cost_needs_no_rate_for_a_known_zero_and_does_not_infer_chat_zero():
    missing_chat = chat(**Usage().buckets())
    decision_zero = decision(usage={"input_tokens": 0, "output_tokens": 0})

    assert estimate_cost(usage_rows([decision_zero]), {})["total_usd"] == 0
    assert estimate_cost(usage_rows([missing_chat]), {})["total_usd"] is None


@pytest.mark.parametrize(
    "rates",
    [
        [],
        {"llm": {}},
        {"chat": []},
        {"chat": {"": {}}},
        {"chat": {"vendor/model": []}},
        {"chat": {"vendor/model": {"input": 1}}},
        {"chat": {"vendor/model": {"reasoning": 1}}},
        {"decision": {"vendor/model": {"cache_read": 1}}},
        {"decision": {"vendor/model": {"input": True}}},
        {"decision": {"vendor/model": {"input": -1}}},
        {"decision": {"vendor/model": {"input": "0.1"}}},
        {"decision": {"vendor/model": {"input": float("inf")}}},
        {"decision": {"vendor/model": {"input": float("nan")}}},
    ],
)
def test_invalid_or_double_billing_rates_are_rejected_even_with_no_calls(rates):
    with pytest.raises(ValueError):
        estimate_cost([], rates)


def test_empty_usage_is_empty_not_missing():
    assert usage_rows([]) == []
    assert summarize_usage([]) == {"calls": 0, "excluded_rows": 0, "groups": []}
    assert estimate_cost([], {}) == {
        "currency": "USD",
        "known_usd": 0,
        "missing_calls": 0,
        "total_usd": 0,
    }
