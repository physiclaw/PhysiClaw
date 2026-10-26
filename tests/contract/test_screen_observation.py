"""Same-frame screen evidence and deeply immutable factual plugin contracts."""

from dataclasses import FrozenInstanceError, fields

import pytest

from physiclaw.contract.decision import ActionCandidate
from physiclaw.contract.observation import (
    COORDINATE_SPACE,
    OBSERVATION_META,
    read_observation,
)
from physiclaw.contract.plugin import ProposedTurn
from physiclaw.contract.turn import ActionEvent, TurnInput


def test_image_and_canonical_elements_share_one_observation(observation_blocks):
    blocks = observation_blocks()
    observed = read_observation(blocks)

    assert observed.frame.width == 80 and observed.frame.height == 120
    assert (
        observed.id == observed.frame.id == blocks[0]["_meta"][OBSERVATION_META]["id"]
    )
    assert observed.coordinate_space == COORDINATE_SPACE
    assert observed.captured_at is None
    assert observed.observed_after is None
    assert len(observed.ocr) == len(observed.icons) == 1
    assert observed.ocr[0].label == "搜索"
    assert observed.icons[0].label == ""
    assert observed.icons[0].bbox == (0.1, 0.2, 0.3, 0.4)
    assert observed.icons[0].conf == 0.87
    assert observed.element_ref(observed.icons[0]) == f"{observed.id}:0"


@pytest.mark.parametrize(
    "field,value",
    [
        ("image_sha256", "wrong"),
        ("listing_sha256", "wrong"),
        ("width", 81),
        ("height", 121),
        ("id", ""),
        ("coordinate_space", "camera_pixels"),
        ("image_to_screen", [0, 1, 0, 1, 0, 0]),
        ("captured_at", float("nan")),
        ("captured_at", -1),
        ("observed_after", float("nan")),
        ("observed_after", float("inf")),
        ("observed_after", -1),
        ("observed_after", True),
        ("observed_after", "100"),
        ("source", "unknown"),
        ("quality_warning", False),
        ("quality_warning", []),
    ],
)
def test_rejects_unbound_or_unsupported_view_metadata(observation_blocks, field, value):
    blocks = observation_blocks()
    blocks[0]["_meta"][OBSERVATION_META][field] = value
    with pytest.raises(ValueError):
        read_observation(blocks)


def test_observed_after_preserves_lower_bound_without_claiming_capture_time(
    observation_blocks,
):
    blocks = observation_blocks()
    blocks[0]["_meta"][OBSERVATION_META]["observed_after"] = 123.5
    observation = read_observation(blocks)
    assert observation.observed_after == 123.5
    assert observation.captured_at is None


def test_older_view_metadata_without_quality_warning_is_supported(observation_blocks):
    blocks = observation_blocks()
    del blocks[0]["_meta"][OBSERVATION_META]["quality_warning"]
    assert read_observation(blocks).quality_warning is None


@pytest.mark.parametrize(
    "rows",
    [
        '0 [icon] "" [-0.100,0.200,0.300,0.400] 0.87',
        '0 [icon] "" [0.300,0.200,0.100,0.400] 0.87',
        '0 [icon] "" [0.100,0.200,1.100,0.400] 0.87',
        '0 [icon] "" [0.100,0.200,0.300] 0.87',
        '0 [icon] "" [0.100,0.200,0.300,0.400] 1.10',
        '-1 [text] "negative id" [0.100,0.200,0.300,0.400] 0.90',
        '0 [unknown] "unknown kind" [0.100,0.200,0.300,0.400] 0.90',
        '0 [icon] "" [0.100,0.200,0.300,0.400] 0.87\n0 [text] "重复" [0.4,0.2,0.8,0.4] 0.9',
    ],
)
def test_rejects_invalid_geometry_and_duplicate_element_ids(observation_blocks, rows):
    from physiclaw.common.listing import LISTING_HEADER

    blocks = observation_blocks(listing=f"{LISTING_HEADER}\n{rows}")
    with pytest.raises(ValueError):
        read_observation(blocks)


def test_rejects_mismatched_listing_even_with_valid_rows(observation_blocks):
    blocks = observation_blocks()
    blocks[1]["text"] = blocks[1]["text"].replace("搜索", "购买")
    with pytest.raises(ValueError, match="capture mismatch"):
        read_observation(blocks)


def test_missing_evidence_is_unknown_and_multiple_images_are_ambiguous(
    observation_blocks,
):
    blocks = observation_blocks()
    bare = [{k: v for k, v in blocks[0].items() if k != "_meta"}, blocks[1]]
    assert read_observation(bare) is None
    assert read_observation([]) is None
    with pytest.raises(ValueError, match="ambiguous"):
        read_observation([*blocks, blocks[0]])
    with pytest.raises(ValueError, match="missing"):
        read_observation(blocks[:1])


def test_snapshot_copies_nested_inputs_and_exposes_no_chat_context(observation_blocks):
    observation = read_observation(observation_blocks())
    schema = {"tap": {"properties": {"bbox": {"type": "array"}}}}
    arguments = {"bbox": [0.1, 0.2, 0.3, 0.4]}
    event = ActionEvent(
        1, 0, "call", "tap", arguments, False, True, observation.id, "Tapped"
    )
    events = [event]
    facts = TurnInput(observation, None, events, schema)
    schema["tap"]["properties"].clear()
    arguments["bbox"][0] = 0.9
    events.clear()

    assert facts.tools["tap"]["properties"]["bbox"]["type"] == "array"
    assert facts.events[0].arguments["bbox"][0] == 0.1
    with pytest.raises(TypeError):
        facts.tools["tap"]["properties"]["other"] = {}
    with pytest.raises(FrozenInstanceError):
        facts.observation.frame.width = 1
    with pytest.raises(FrozenInstanceError):
        facts.observation.elements[0].label = "other"
    assert not {f.name for f in fields(facts)} & {
        "history",
        "messages",
        "prompt",
        "session_provider",
        "session",
        "chat",
        "provider",
    }


def test_control_outcomes_cannot_be_executable_proposals():
    with pytest.raises(ValueError, match="executable"):
        ProposedTurn.from_action(ActionCandidate("yield", "YIELD"), "frame")


@pytest.mark.parametrize("expiry", [True, "soon", float("nan"), float("inf")])
def test_proposal_expiry_must_be_finite_monotonic_time(expiry):
    with pytest.raises(ValueError, match="expiry"):
        ProposedTurn.from_action(
            ActionCandidate("tap", "Tap", "tap"), "frame", expires_at=expiry
        )


def test_action_proposal_keeps_optional_expiry():
    action = ActionCandidate("tap", "Tap", "tap")
    assert ProposedTurn.from_action(action, "frame").expires_at is None
    assert (
        ProposedTurn.from_action(action, "frame", expires_at=123.5).expires_at == 123.5
    )


def test_factual_input_rejects_mutable_or_chat_shaped_nested_values():
    with pytest.raises(ValueError, match="text or unknown"):
        ActionEvent(1, 0, "call", "tap", {}, False, None, None, {"mutable": []})
    with pytest.raises(ValueError, match="ActionEvent"):
        TurnInput(None, "not_observed", events=({"history": []},))
    with pytest.raises(ValueError, match="Observation"):
        TurnInput({"frame": []}, None)
    with pytest.raises(ValueError, match="reference"):
        ProposedTurn.from_action(ActionCandidate("tap", "Tap", "tap"), "")
