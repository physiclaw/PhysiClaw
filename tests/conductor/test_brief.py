"""Tests for `physiclaw.conductor.walk.brief` — the handover/completion/boot
report renderings the drivers' final [note, peek] turns carry."""

from __future__ import annotations

import pytest

from physiclaw.conductor.walk import brief
from physiclaw.conductor.walk.ledger import Ledger


def _walk(outputs=None, paid=None, **overrides) -> str:
    ledger = Ledger(ref="demo/flow", nodes=9, task={}, decided=outputs or {}, paid=paid)
    base = dict(ledger=ledger, node="search", idx=2, consented=None)
    return brief.walk_brief("move did not land", **{**base, **overrides})


def test_walk_brief_carries_reason_and_position() -> None:
    text = _walk()

    assert "conductor handing over: move did not land." in text
    assert "Walk demo/flow stopped at node search (3/9)." in text
    assert "Verify state before acting." in text


def test_walk_brief_past_the_last_node_names_the_end() -> None:
    text = _walk(node=None, idx=9)

    assert "past the last node (9/9)" in text


def test_walk_brief_includes_recorded_outputs() -> None:
    text = _walk(outputs={"parse.keyword": "milk 1L"})

    assert "So far: decided parse.keyword='milk 1L'." in text


def test_walk_brief_consent_line_says_payment_did_not_fire() -> None:
    text = _walk(consented=58.0)

    assert "consented to ¥58" in text
    assert "has NOT been made" in text


@pytest.mark.parametrize("absent", ["So far", "consented"])
def test_walk_brief_omits_empty_sections(absent: str) -> None:
    text = _walk()

    assert absent not in text
