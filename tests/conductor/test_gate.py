"""Tests for `physiclaw.conductor.walk.gate` — the consent transitions
(quote, hold, consent, spend) and the suspension projection that
carries them across wakes."""

from __future__ import annotations

from physiclaw.conductor.walk.gate import Gate


def test_gate_quote_records_the_total_and_its_labels() -> None:
    gate = Gate()

    gate.quote(45.0, ("合计", "实付"))

    assert (gate.quoted, gate.total_label, gate.consented) == (
        45.0,
        ("合计", "实付"),
        None,
    )


def test_gate_hold_awaits_from_zero_silence() -> None:
    gate = Gate(silence=3)

    gate.hold()

    assert (gate.awaiting, gate.silence) == (True, 0)


def test_gate_consent_binds_the_quote_and_ends_the_hold() -> None:
    gate = Gate(awaiting=True)
    gate.quote(45.0, ("合计",))

    gate.consent()

    assert (gate.consented, gate.awaiting) == (45.0, False)


def test_gate_consent_without_a_quote_binds_nothing() -> None:
    gate = Gate(awaiting=True)

    gate.consent()

    assert (gate.consented, gate.awaiting) == (None, False)


def test_gate_spend_returns_the_amount_and_clears_the_money() -> None:
    gate = Gate()
    gate.quote(45.0, ("合计",))
    gate.consent()

    amount = gate.spend()

    assert (amount, gate.consented, gate.quoted, gate.total_label) == (
        45.0,
        None,
        None,
        (),
    )


def test_gate_total_label_survives_the_suspension() -> None:
    gate = Gate(quoted=45.0, consented=45.0, total_label=("合计", "实付"))

    restored = Gate.from_suspended(gate.to_suspended())

    assert restored.total_label == ("合计", "实付") and restored.consented == 45.0
