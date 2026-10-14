"""Public layout-lint behavior for ordered batches and invalid geometry.

Accepted mutation survivors: 17 warning-prose decorations and one unevaluated
local annotation. Blocking, relevant boxes and the reason for blocking are
asserted without snapshotting every word of the corrective guidance.
"""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.agent.layout import lint_gesture, lint_sequence
from physiclaw.common import paths

FOREIGN_BOX = [0.05, 0.055, 0.9, 0.1]


def _step(tool, arg):
    return {"tool_name": tool, "arg": arg}


def test_regression_paste_before_unrelated_long_press_is_allowed(learned_layout):
    actions = [
        _step("tap", learned_layout["chat_paste"]),
        _step("long_press", FOREIGN_BOX),
    ]

    blocked = lint_sequence(actions)

    assert blocked is None


def test_reused_paste_after_foreign_long_press_is_blocked(learned_layout):
    actions = [
        _step("long_press", FOREIGN_BOX),
        _step("tap", learned_layout["chat_paste"]),
    ]

    blocked = lint_sequence(actions)

    assert blocked is not None and "chat_paste" in blocked


def test_regression_latest_long_press_controls_which_paste_menu_is_open(learned_layout):
    actions = [
        _step("long_press", FOREIGN_BOX),
        _step("long_press", learned_layout["chat_input_kb_visible"]),
        _step("tap", learned_layout["chat_paste"]),
    ]

    blocked = lint_sequence(actions)

    assert blocked is None


def test_regression_later_paste_does_not_prove_an_earlier_hidden_box_press_wrong(
    learned_layout,
):
    actions = [
        _step("long_press", learned_layout["chat_input_kb_hidden"]),
        _step("long_press", learned_layout["chat_input_kb_visible"]),
        _step("tap", learned_layout["chat_paste"]),
    ]

    blocked = lint_sequence(actions)

    assert blocked is None


def test_later_valid_paste_does_not_override_known_keyboard_up(learned_layout):
    actions = [
        _step("long_press", learned_layout["chat_input_kb_hidden"]),
        _step("long_press", learned_layout["chat_input_kb_visible"]),
        _step("tap", learned_layout["chat_paste"]),
    ]

    blocked = lint_sequence(actions, keyboard_up=True)

    assert blocked is not None and "already up" in blocked


def test_valid_paste_before_an_invalid_paste_does_not_hide_the_later_error(
    learned_layout,
):
    actions = [
        _step("long_press", learned_layout["chat_input_kb_visible"]),
        _step("tap", learned_layout["chat_paste"]),
        _step("long_press", FOREIGN_BOX),
        _step("tap", learned_layout["chat_paste"]),
    ]

    blocked = lint_sequence(actions)

    assert blocked is not None and "chat_paste" in blocked


@pytest.mark.parametrize(
    "bad_box", [[False, 0.9, True, 0.95], [".05", ".055", ".9", ".1"]]
)
def test_invalid_long_press_cannot_supply_paste_menu_evidence(learned_layout, bad_box):
    actions = [_step("long_press", bad_box), _step("tap", learned_layout["chat_paste"])]

    blocked = lint_sequence(actions)

    assert blocked is None


def test_malformed_later_long_press_breaks_the_previous_menu_association(
    learned_layout,
):
    actions = [
        _step("long_press", FOREIGN_BOX),
        _step("long_press", []),
        _step("tap", learned_layout["chat_paste"]),
    ]

    blocked = lint_sequence(actions)

    assert blocked is None


@pytest.mark.parametrize(
    "bad_box", [[0, 0, 10**400, 1], ["nan", 0, 1, 1], [0, 0, "Infinity", 1], [], None]
)
@pytest.mark.parametrize("tool", ["tap", "long_press"])
def test_regression_malformed_sequence_boxes_fail_open(learned_layout, bad_box, tool):
    actions = [_step(tool, bad_box), _step("send_to_clipboard", "text")]

    blocked = lint_sequence(actions)

    assert blocked is None


@pytest.mark.parametrize("actions", [None, {}, "not-a-list", [None, {"tool_name": []}]])
def test_malformed_action_containers_fail_open(learned_layout, actions):
    blocked = lint_sequence(actions)

    assert blocked is None


@pytest.mark.parametrize("bad_box", [[], [0, 0, 10**400, 1], ["nan", 0, 1, 1]])
def test_invalid_standalone_long_press_fails_open(learned_layout, bad_box):
    blocked = lint_gesture("long_press", {"bbox": bad_box}, keyboard_up=True)

    assert blocked is None


def test_unlearned_layout_allows_a_paste_batch():
    actions = [_step("long_press", FOREIGN_BOX), _step("tap", [0.12, 0.52, 0.3, 0.566])]

    blocked = lint_sequence(actions)

    assert blocked is None


def test_keyboard_raising_tap_still_blocks_hidden_box_long_press(learned_layout):
    hidden = learned_layout["chat_input_kb_hidden"]
    actions = [_step("tap", hidden), _step("long_press", hidden)]

    blocked = lint_sequence(actions)

    assert blocked is not None and "step 1 taps" in blocked


def test_repeated_raising_taps_keep_the_first_evidence_step(learned_layout):
    hidden = learned_layout["chat_input_kb_hidden"]
    actions = [_step("tap", hidden), _step("tap", hidden), _step("long_press", hidden)]

    blocked = lint_sequence(actions)

    assert blocked is not None and "step 1 taps" in blocked


def test_bare_long_press_does_not_hide_later_keyboard_evidence(learned_layout):
    hidden = learned_layout["chat_input_kb_hidden"]
    actions = [
        _step("long_press", hidden),
        _step("tap", hidden),
        _step("long_press", hidden),
        _step("send_to_clipboard", "text"),
    ]

    blocked = lint_sequence(actions)

    assert blocked is not None and "step 2 taps" in blocked


def test_hidden_input_paste_batch_explains_the_keyboard_evidence(learned_layout):
    actions = [
        _step("long_press", learned_layout["chat_input_kb_hidden"]),
        _step("tap", learned_layout["chat_paste"]),
    ]

    blocked = lint_sequence(actions)

    assert blocked is not None and "batch then taps the Paste box" in blocked


@pytest.mark.parametrize("missing", ["chat_input_kb_hidden", "chat_input_kb_visible"])
def test_partial_capture_cannot_diagnose_a_wrong_input_box(learned_layout, missing):
    hidden = learned_layout["chat_input_kb_hidden"]
    learned_layout.pop(missing)
    paths.screen_layout_json().write_text(json.dumps(learned_layout))
    actions = [_step("long_press", hidden), _step("send_to_clipboard", "text")]

    standalone = lint_gesture("long_press", {"bbox": hidden}, keyboard_up=True)
    batch = lint_sequence(actions, keyboard_up=True)

    assert standalone is None and batch is None


def test_missing_paste_box_does_not_invent_a_paste_flow(learned_layout):
    import json

    from physiclaw.common import paths

    learned_layout.pop("chat_paste")
    paths.screen_layout_json().write_text(json.dumps(learned_layout))
    actions = [
        _step("long_press", learned_layout["chat_input_kb_hidden"]),
        _step("send_to_clipboard", "text"),
    ]

    blocked = lint_sequence(actions)

    assert blocked is None


@given(padding=st.lists(st.text(max_size=20), max_size=3))
def test_clipboard_steps_do_not_change_paste_order_rule(padding):
    # Keep the generated-data property independent of fixture lifetime.
    from unittest.mock import patch

    from physiclaw.agent.layout import store

    data = {
        "chat_input_kb_hidden": [0.098, 0.9, 0.716, 0.95],
        "chat_input_kb_visible": [0.098, 0.586, 0.716, 0.634],
        "chat_paste": [0.12, 0.52, 0.3, 0.566],
    }
    paste = _step("tap", data["chat_paste"])
    foreign = _step("long_press", FOREIGN_BOX)
    middle = [_step("send_to_clipboard", text) for text in padding]

    with patch.object(store, "_load", return_value=data):
        valid = lint_sequence([paste, *middle, foreign])
        invalid = lint_sequence([foreign, *middle, paste])

    assert valid is None
    assert invalid is not None and "chat_paste" in invalid
