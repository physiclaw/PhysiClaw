"""Keyboard belief stays conservative across observed screen actions."""

import json

import pytest

from physiclaw.agent.layout import KeyboardTracker
from physiclaw.common import paths


@pytest.mark.parametrize("before", ["up", "down", "unknown"])
@pytest.mark.parametrize("tool", ["swipe", "run_macro", "unlock_phone"])
@pytest.mark.parametrize("changed", [True, False, None])
def test_opaque_screen_actions_discard_keyboard_belief(before, tool, changed):
    tracker = KeyboardTracker(state=before)

    tracker.observe(tool, {}, changed)

    assert tracker.state == "unknown"


@pytest.mark.parametrize("tool", ["tap", "double_tap", "long_press"])
@pytest.mark.parametrize(
    "changed, expected", [(True, "up"), (False, "unknown"), (None, "unknown")]
)
def test_raising_press_requires_camera_evidence(
    learned_layout, tool, changed, expected
):
    tracker = KeyboardTracker()

    tracker.observe(tool, {"bbox": learned_layout["chat_input_kb_hidden"]}, changed)

    assert tracker.state == expected


@pytest.mark.parametrize(
    "field",
    [
        "chat_input_kb_visible",
        "send",
        "chat_paste",
        "backspace",
        "return",
        "space",
        "spotlight_input",
        "spotlight_paste",
    ],
)
def test_typing_and_paste_regions_preserve_up_belief(learned_layout, field):
    # Disjoint boxes ensure another region cannot mask a missing landmark.
    boxes = {
        key: [0.05, 0.05, 0.1, 0.1]
        for key in learned_layout
        if key not in {"im_app", "layout_learned"}
    }
    boxes[field] = [0.7, 0.7, 0.8, 0.8]
    paths.screen_layout_json().write_text(json.dumps(boxes))
    tracker = KeyboardTracker(state="up")

    tracker.observe("tap", {"bbox": boxes[field]}, True)

    assert tracker.state == "up"


@pytest.mark.parametrize(
    "field",
    [
        "chat_input_kb_visible",
        "send",
        "chat_paste",
        "backspace",
        "return",
        "spotlight_input",
        "spotlight_paste",
    ],
)
def test_typing_regions_outside_hidden_input_do_not_infer_up(learned_layout, field):
    # Space overlaps the hidden input's slack region; a changed press there
    # follows the raising-press rule, already covered separately.
    tracker = KeyboardTracker()

    tracker.observe("tap", {"bbox": learned_layout[field]}, True)

    assert tracker.state == "unknown"


@pytest.mark.parametrize("bad_box", [None, [], ["nan", 0, 1, 1], [0, 0, 10**400, 1]])
def test_malformed_press_loses_keyboard_belief(learned_layout, bad_box):
    tracker = KeyboardTracker(state="up")

    tracker.observe("tap", {"bbox": bad_box}, True)

    assert tracker.state == "unknown"


@pytest.mark.parametrize("tool", ["tap", "long_press", "double_tap", "swipe"])
def test_sequence_geometry_loses_keyboard_belief(learned_layout, tool):
    tracker = KeyboardTracker(state="up")
    arguments = {
        "actions": [
            {"tool_name": "send_to_clipboard", "arg": "text"},
            {"tool_name": tool, "arg": learned_layout["chat_paste"]},
        ]
    }

    tracker.observe("sequence", arguments, False)

    assert tracker.state == "unknown"


def test_clipboard_only_sequence_keeps_keyboard_belief():
    tracker = KeyboardTracker(state="up")
    arguments = {
        "actions": [
            {"tool_name": "send_to_clipboard", "arg": "one"},
            {"tool_name": "send_to_clipboard", "arg": "two"},
        ]
    }

    tracker.observe("sequence", arguments, True)

    assert tracker.state == "up"
