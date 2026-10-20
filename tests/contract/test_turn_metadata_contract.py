"""Neutral turn metadata is immutable, zero-based and separate from context."""

from dataclasses import FrozenInstanceError, fields

import pytest

from physiclaw.contract.turn import TurnInput, TurnMetadata


def test_turn_metadata_is_immutable_and_has_no_producer_context():
    metadata = TurnMetadata("session", 0, None, 300)
    assert metadata.turn == 0
    with pytest.raises(FrozenInstanceError):
        metadata.turn = 1
    assert {field.name for field in fields(metadata)} == {
        "session_id",
        "turn",
        "deadline",
        "turns_remaining",
    }
    assert not {field.name for field in fields(metadata)} & {
        field.name for field in fields(TurnInput)
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"session_id": ""},
        {"turn": -1},
        {"turn": True},
        {"turns_remaining": 0},
        {"turns_remaining": True},
        {"deadline": float("nan")},
        {"deadline": True},
    ],
)
def test_invalid_turn_metadata_is_rejected(overrides):
    values = dict(session_id="session", turn=0, deadline=None, turns_remaining=300)
    with pytest.raises(ValueError):
        TurnMetadata(**(values | overrides))
