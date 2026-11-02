"""TurnOutput freezes model intent, without execution metadata."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.contract.decision import decision_json
from physiclaw.contract.turn import TurnOutput


@pytest.mark.parametrize(
    "summary,scratchpad,action",
    [
        (3, None, {"peek": {}}),
        ("x", 4, {"peek": {}}),
        ("x", None, {}),
        ("x", None, {"a": {}, "b": {}}),
        ("x", None, {"a": []}),
    ],
)
def test_turn_output_invalid_envelope_rejected(summary, scratchpad, action):
    with pytest.raises(ValueError):
        TurnOutput(summary, scratchpad, action)


@given(st.text(), st.one_of(st.none(), st.text()))
def test_turn_output_freezes_nested_arguments(summary, scratchpad):
    action = {"tap": {"bbox": [0, 0, 1, 1]}}
    output = TurnOutput(summary, scratchpad, action)

    action["tap"]["bbox"].clear()

    assert decision_json(output.action) == {"tap": {"bbox": [0, 0, 1, 1]}}
    assert output.summary == summary
    assert output.scratchpad == scratchpad
