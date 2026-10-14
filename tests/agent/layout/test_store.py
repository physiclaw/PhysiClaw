"""Layout persistence and setup state through public APIs and real files.

Accepted mutation survivors: 69 presentation-only changes to card, reminder,
diagnostic and log text; eight redundant region bounds. Coordinates are already
within [0, 1] with strictly ordered edges, so a center cannot reach 1; widening
seven upper limits from 1 to 2 or making the x-center upper limit strict changes
no valid box. Field mappings, missing-field guidance, save status and region
boundaries are checked without freezing every word of the generated prose.
"""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.agent import layout
from physiclaw.agent.layout import store
from physiclaw.common import paths
from physiclaw.common.text import write_text


def _saved():
    return json.loads(paths.screen_layout_json().read_text())


def _fail_card_write(path, text):
    if path == paths.screen_layout_md():
        raise OSError("disk full")
    write_text(path, text)


def test_regression_rounding_cannot_save_a_degenerate_box(learned_layout):
    before = paths.screen_layout_json().read_bytes()

    response = layout.record(
        "chat-keyboard", "chat_paste", [0.1201, 0.52, 0.1202, 0.566], "wechat"
    )

    assert "Layout not saved" in response and "left<right" in response
    assert paths.screen_layout_json().read_bytes() == before


def test_regression_changing_chat_app_requires_new_chat_boxes(learned_layout):
    response = layout.record(
        "chat-keyboard", "send", [0.85, 0.586, 0.95, 0.634], "whatsapp"
    )
    saved = _saved()

    assert saved["im_app"] == "WhatsApp"
    assert saved["send"] == [0.85, 0.586, 0.95, 0.634]
    assert "chat_input_kb_hidden" not in saved and "chat_paste" not in saved
    assert saved["spotlight_input"] == learned_layout["spotlight_input"]
    assert not saved["layout_learned"] and not layout.is_learned()
    assert "chat_input_kb_hidden" in response and "chat_paste" in response


def test_same_chat_app_with_different_case_keeps_existing_boxes(learned_layout):
    layout.record("chat-keyboard", "send", [0.85, 0.586, 0.95, 0.634], "  wEcHaT  ")

    assert _saved()["chat_input_kb_hidden"] == learned_layout["chat_input_kb_hidden"]
    assert layout.is_learned()


def test_spotlight_correction_does_not_change_chat_app(learned_layout):
    layout.record("spotlight", "spotlight_input", [0.02, 0.58, 0.95, 0.66], "whatsapp")

    assert _saved()["im_app"] == "WeChat"
    assert _saved()["chat_paste"] == learned_layout["chat_paste"]


@pytest.mark.parametrize(
    "app, expected",
    [
        ("WECHAT", "WeChat"),
        ("WHATSAPP", "WhatsApp"),
        ("TELEGRAM", "Telegram"),
        ("SIGNAL", "Signal"),
        ("MESSENGER", "Messenger"),
        ("IMESSAGE", "iMessage"),
        ("line", "LINE"),
        ("WCOM", "WeCom"),
        ("qq", "QQ"),
        ("KAKAOTALK", "KakaoTalk"),
        ("Custom IM", "Custom IM"),
    ],
)
def test_chat_app_labels_are_canonical_and_trimmed(app, expected):
    layout.record("chat-keyboard", "send", [0.85, 0.586, 0.95, 0.634], f"  {app}  ")

    assert _saved()["im_app"] == expected
    assert expected in layout.load_layout_md()


def test_custom_app_case_variation_preserves_captured_chat_boxes(learned_layout):
    learned_layout["im_app"] = "Custom IM"
    paths.screen_layout_json().write_text(json.dumps(learned_layout))

    layout.record("chat-keyboard", "send", [0.85, 0.586, 0.95, 0.634], "CUSTOM IM")

    assert layout.is_learned()
    assert _saved()["chat_paste"] == learned_layout["chat_paste"]


def test_all_three_keyboard_keys_are_repeatable(learned_layout):
    assert layout.repeatable_key_boxes() == [
        learned_layout["backspace"],
        learned_layout["space"],
        learned_layout["return"],
    ]


@pytest.mark.parametrize(
    "bad_box",
    [None, [], ["x", 0, 1, 1], [0, 0, 0, 1], [False, 0, 1, 1], [0, 0, float("nan"), 1]],
)
def test_regression_invalid_saved_boxes_do_not_count_as_learned(
    learned_layout, bad_box
):
    learned_layout["space"] = bad_box
    paths.screen_layout_json().write_text(json.dumps(learned_layout))

    complete = layout.is_learned()
    missing = layout.missing_pages()
    repeatable = layout.repeatable_key_boxes()

    assert not complete and "spotlight" in missing
    assert repeatable == [learned_layout["backspace"], learned_layout["return"]]


@pytest.mark.parametrize("payload", ["[]", "{broken", '{"space": "bad"}'])
def test_unusable_json_does_not_expose_a_stale_markdown_card(learned_layout, payload):
    paths.screen_layout_json().write_text(payload)
    paths.screen_layout_md().write_text("STALE BOXES")

    card = layout.load_layout_md()

    assert card == ""


def test_regression_layout_card_is_rendered_from_current_json(learned_layout):
    paths.screen_layout_md().write_text("STALE BOXES")

    card = layout.load_layout_md()

    assert "STALE" not in card
    assert "[0.098, 0.900, 0.716, 0.950]" in card
    assert "WeChat" in card


def test_json_layout_can_render_without_a_markdown_file(learned_layout):
    card = layout.load_layout_md()

    assert "[0.098, 0.900, 0.716, 0.950]" in card


@pytest.mark.parametrize(
    "label, coordinates",
    [
        ("Spotlight search", "[0.020, 0.582, 0.958, 0.660]"),
        ("Chat input, keyboard hidden", "[0.098, 0.900, 0.716, 0.950]"),
        ("Chat input, keyboard visible", "[0.098, 0.586, 0.716, 0.634]"),
        ("Send", "[0.752, 0.868, 0.990, 0.918]"),
        ("Spotlight search → Paste at", "[0.090, 0.512, 0.205, 0.560]"),
        ("Chat input (keyboard visible) → Paste at", "[0.120, 0.520, 0.300, 0.566]"),
        ("backspace ⌫", "[0.867, 0.809, 0.995, 0.861]"),
        ("return / search / send", "[0.752, 0.868, 0.990, 0.918]"),
        ("space", "[0.251, 0.868, 0.747, 0.920]"),
    ],
)
def test_layout_card_pairs_each_landmark_with_its_coordinates(
    learned_layout, label, coordinates
):
    card = layout.load_layout_md()

    assert f"- {label} {coordinates}" in card


def test_partial_json_renders_captured_and_missing_landmarks():
    layout.record("chat-keyboard", "send", [0.85, 0.586, 0.95, 0.634], "wechat")

    card = layout.load_layout_md()

    assert "- Send [0.850, 0.586, 0.950, 0.634]" in card
    assert "- Chat input, keyboard hidden (not captured yet)" in card


def test_capture_creates_a_missing_home_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "HOME", tmp_path / "new" / "nested" / "home")

    response = layout.record("spotlight", "spotlight_input", [0, 0.55, 1, 0.65])

    assert response.startswith("Saved")
    assert _saved()["spotlight_input"] == [0, 0.55, 1, 0.65]
    assert paths.screen_layout_md().exists()


def test_failed_convenience_card_write_does_not_undo_a_saved_layout(
    learned_layout, mocker, caplog
):
    mocker.patch.object(store, "write_text", side_effect=_fail_card_write)

    response = layout.record(
        "chat-keyboard", "send", [0.85, 0.586, 0.95, 0.634], "wechat"
    )

    assert response.startswith("Saved")
    assert _saved()["send"] == [0.85, 0.586, 0.95, 0.634]
    assert "- Send [0.850, 0.586, 0.950, 0.634]" in layout.load_layout_md()
    assert "disk full" in caplog.text


def test_failed_json_publish_keeps_the_previous_layout_card(learned_layout, mocker):
    before = layout.load_layout_md()
    mocker.patch.object(
        store, "write_json_atomic", side_effect=OSError("rename denied"), create=True
    )

    with pytest.raises(OSError, match="rename denied"):
        layout.record("chat-keyboard", "send", [0.85, 0.586, 0.95, 0.634], "wechat")

    assert layout.load_layout_md() == before
    assert _saved()["send"] == learned_layout["send"]


@pytest.mark.parametrize("legacy_card", [None, "Previously captured legacy card"])
def test_failed_first_json_save_never_publishes_a_new_markdown_card(
    legacy_card, mocker
):
    _seed_legacy_card(legacy_card)
    before = layout.load_layout_md()
    mocker.patch.object(
        store, "write_json_atomic", side_effect=OSError("rename denied")
    )

    with pytest.raises(OSError, match="rename denied"):
        layout.record("chat-keyboard", "send", [0.85, 0.586, 0.95, 0.634], "wechat")

    assert layout.load_layout_md() == before
    assert not paths.screen_layout_json().exists()


def _seed_legacy_card(contents):
    if contents is not None:
        path = paths.screen_layout_md()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)


def test_unreadable_saved_layout_fails_open(learned_layout, mocker):
    mocker.patch.object(store, "read_text", side_effect=PermissionError("unreadable"))

    complete = layout.is_learned()
    card = layout.load_layout_md()

    assert not complete and card == ""


def test_legacy_markdown_without_json_remains_readable(tmp_path):
    path = paths.screen_layout_md()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Legacy layout card\n")

    card = layout.load_layout_md()

    assert card == "Legacy layout card"


def test_invalid_utf8_legacy_card_fails_open():
    path = paths.screen_layout_md()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff")

    card = layout.load_layout_md()

    assert card == ""


def test_oversized_coordinate_is_a_validation_error():
    response = layout.record("spotlight", "spotlight_input", [0, 0.5, 10**400, 0.6])

    assert "Layout not saved" in response
    assert not paths.screen_layout_json().exists()


@pytest.mark.parametrize(
    "bbox, diagnostic",
    [([-0.1, 0.5, 0.9, 0.6], "normalized"), ([0.4, 0.5, 0.6, 0.6], "too narrow")],
)
def test_implausible_input_box_is_rejected_without_saving(bbox, diagnostic):
    response = layout.record("spotlight", "spotlight_input", bbox)

    assert "Layout not saved" in response and diagnostic in response
    assert not paths.screen_layout_json().exists()


@pytest.mark.parametrize(
    "page, field, box",
    [
        ("spotlight", "spotlight_input", [0, 0.49, 0.7, 0.51]),
        ("spotlight", "spotlight_input", [0, 0.69, 1, 0.71]),
        ("chat-no-keyboard", "chat_input_kb_hidden", [0, 0.79, 0.4, 0.81]),
        ("chat-no-keyboard", "chat_input_kb_hidden", [0, 0.98, 1, 1]),
        ("chat-keyboard", "chat_input_kb_visible", [0, 0.49, 0.2, 0.51]),
        ("chat-keyboard", "chat_input_kb_visible", [0, 0.69, 1, 0.71]),
        ("spotlight", "spotlight_paste", [0.1, 0.39, 0.2, 0.41]),
        ("spotlight", "spotlight_paste", [0.1, 0.71, 0.2, 0.73]),
        ("chat-keyboard", "chat_paste", [0.1, 0.39, 0.2, 0.41]),
        ("chat-keyboard", "chat_paste", [0.1, 0.71, 0.2, 0.73]),
        ("spotlight", "space", [0, 0.79, 0.2, 0.81]),
        ("spotlight", "backspace", [0.75, 0.69, 0.85, 0.71]),
        ("spotlight", "return", [0.75, 0.79, 0.85, 0.81]),
        ("chat-keyboard", "send", [0.75, 0.49, 0.85, 0.51]),
    ],
)
def test_region_boundaries_and_minimum_widths_are_inclusive(page, field, box):
    response = layout.record(page, field, box, "wechat")

    assert response.startswith("Saved")
    assert _saved()[field] == box


@pytest.mark.parametrize(
    "page, field, box",
    [
        ("spotlight", "spotlight_input", [0, 0.48, 1, 0.5]),
        ("spotlight", "spotlight_input", [0, 0.7, 1, 0.72]),
        ("spotlight", "spotlight_input", [0, 0.55, 0.69, 0.65]),
        ("chat-no-keyboard", "chat_input_kb_hidden", [0, 0.78, 1, 0.8]),
        ("chat-no-keyboard", "chat_input_kb_hidden", [0, 0.99, 1, 1]),
        ("chat-no-keyboard", "chat_input_kb_hidden", [0, 0.9, 0.39, 0.95]),
        ("chat-keyboard", "chat_input_kb_visible", [0, 0.48, 1, 0.5]),
        ("chat-keyboard", "chat_input_kb_visible", [0, 0.7, 1, 0.72]),
        ("chat-keyboard", "chat_input_kb_visible", [0, 0.55, 0.19, 0.65]),
        ("spotlight", "spotlight_paste", [0.1, 0.38, 0.2, 0.4]),
        ("spotlight", "spotlight_paste", [0.1, 0.72, 0.2, 0.74]),
        ("chat-keyboard", "chat_paste", [0.1, 0.38, 0.2, 0.4]),
        ("chat-keyboard", "chat_paste", [0.1, 0.72, 0.2, 0.74]),
        ("spotlight", "space", [0, 0.78, 0.3, 0.8]),
        ("spotlight", "space", [0, 0.85, 0.19, 0.9]),
        ("spotlight", "backspace", [0.85, 0.68, 0.95, 0.7]),
        ("spotlight", "backspace", [0.74, 0.8, 0.84, 0.9]),
        ("spotlight", "return", [0.85, 0.78, 0.95, 0.8]),
        ("spotlight", "return", [0.74, 0.8, 0.84, 0.9]),
        ("chat-keyboard", "send", [0.85, 0.48, 0.95, 0.5]),
        ("chat-keyboard", "send", [0.74, 0.6, 0.84, 0.7]),
        ("spotlight", "spotlight_input", [0, 0.55, 1.1, 0.65]),
        ("spotlight", "spotlight_input", [0, 0.6, 1, 0.6]),
    ],
)
def test_boxes_outside_plausible_regions_are_not_persisted(page, field, box):
    response = layout.record(page, field, box, "wechat")

    assert "Layout not saved" in response
    assert not paths.screen_layout_json().exists()


@given(
    left=st.floats(min_value=0.1, max_value=0.2, allow_nan=False),
    width=st.floats(min_value=0.001, max_value=0.05, allow_nan=False),
)
def test_recorded_paste_geometry_remains_valid_after_rounding(left, width):
    # An isolated filesystem per example avoids shared Hypothesis fixture state.
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from physiclaw.common.bbox import validate_bbox

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with (
            patch.object(paths, "screen_layout_dir", return_value=root),
            patch.object(
                paths, "screen_layout_json", return_value=root / "layout.json"
            ),
            patch.object(paths, "screen_layout_md", return_value=root / "layout.md"),
        ):
            response = layout.record(
                "chat-keyboard",
                "chat_paste",
                [left, 0.52, left + width, 0.566],
                "wechat",
            )
            saved = _saved()["chat_paste"]

    assert response.startswith("Saved")
    assert validate_bbox(saved) == saved
