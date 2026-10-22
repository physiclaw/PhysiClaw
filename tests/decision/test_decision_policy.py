"""Gesture and target selection scales without executable cross products."""

from dataclasses import replace

import pytest

from physiclaw.contract.decision import DecisionContext, DecisionElement
from physiclaw.contract.observation import read_observation
from physiclaw.decision.policy import (
    CONSERVATIVE_PROMPT,
    REVIEW_TOOLS,
    question_profile,
)


@pytest.fixture
def context(observation_blocks):
    observation = read_observation(observation_blocks())
    return DecisionContext(
        observation.id,
        frame=observation.frame,
        ocr=tuple(
            DecisionElement(observation.element_ref(e), e.bbox, e.label)
            for e in observation.ocr
        ),
        icons=tuple(
            DecisionElement(observation.element_ref(e), e.bbox)
            for e in observation.icons
        ),
    )


def test_all_boxes_are_offered_once_and_gestures_are_a_separate_question(context):
    profile = question_profile(context, tools={"tap", "long_press"})
    questions = profile.to_dict()["questions"]
    assert list(questions) == ["phase", "action_type", "target"]
    assert set(questions["phase"]["options"]) == {
        "ROUTINE",
        "SEMANTIC",
        "COMPLETE",
        "UNCLEAR",
    }
    assert list(questions["action_type"]["options"]) == ["YIELD", "tap"]
    targets = questions["target"]["options"]
    assert list(targets) == ["NONE", "b0", "b1"]
    for index, element in ((0, context.icons[0]), (1, context.ocr[0])):
        assert targets[f"b{index}"]["target_ref"] == element.ref
        assert f"Observed element {index}:" in targets[f"b{index}"]["description"]
    assert context.icons[0].text is None  # No invented icon description.
    assert "搜索" in targets["b1"]["description"]
    assert "icon; identify it from the image" in targets["b0"]["description"]
    assert all(
        question.instructions.startswith(CONSERVATIVE_PROMPT + "\n\n")
        for question in profile.questions
    )
    assert all(
        candidate.tool is None and not candidate.arguments
        for question in profile.questions
        for candidate in question.candidates
    )  # These partial choices never carry an executable tool call.
    assert question_profile(context, tools={"long_press", "tap"}) == profile


def test_only_tap_is_offered_among_available_tools(context):
    tools = {
        "tap",
        "long_press",
        "send_to_clipboard",
        "swipe",
        "double_tap",
        "sequence",
    }
    questions = question_profile(context, tools=tools).to_dict()["questions"]
    assert list(questions["action_type"]["options"]) == ["YIELD", "tap"]


def test_consequential_work_cannot_be_offered_by_a_direct_policy_caller(context):
    context = replace(context, facts={"current_step": "Tap the arrow to send."})
    assert question_profile(context, tools={"tap"}).questions == ()


@pytest.mark.parametrize(
    "problem", ["no_frame", "no_tools", "no_targets", "other_tools"]
)
def test_no_executable_work_means_no_billable_questions(context, problem):
    tools = {"tap", "long_press"}
    if problem == "no_frame":
        context = replace(context, frame=None)
    elif problem == "no_tools":
        tools = set()
    elif problem == "no_targets":
        context = replace(context, ocr=(), icons=())
    else:
        tools = {"long_press", "send_to_clipboard", "swipe", "home_screen"}
    assert question_profile(context, tools=tools).questions == ()


def test_equal_boxes_keep_both_indices_including_the_icon_label_drawn_on_image(context):
    first = replace(context.ocr[0], ref=f"{context.observation_id}:0")
    context = replace(
        context,
        ocr=(
            first,
            replace(
                first,
                ref=f"{context.observation_id}:2",
                bbox=(0.4, 0.6, 0.8, 0.8),
            ),
        ),
        icons=(
            replace(
                context.icons[0], ref=f"{context.observation_id}:1", bbox=first.bbox
            ),
            DecisionElement(f"{context.observation_id}:3", (0.39, 0.2, 0.8, 0.4)),
        ),
    )
    targets = (
        question_profile(context, tools={"tap", "long_press"}).questions[2].candidates
    )
    assert [c.id for c in targets] == ["NONE", "b0", "b1", "b2", "b3"]
    assert targets[2].target_ref == context.icons[0].ref  # Visible image label 1.
    assert {c.target_ref for c in targets[1:]} == {
        first.ref,
        context.icons[0].ref,
        context.ocr[1].ref,
        context.icons[1].ref,
    }
    assert len(context.ocr) + len(context.icons) == 4
    assert context.ocr[0].text == context.ocr[1].text


def test_large_target_set_is_linear_and_never_silently_truncated(context):
    elements = tuple(
        DecisionElement(
            f"{context.observation_id}:{index}",
            (0.1, index / 1000, 0.2, index / 1000 + 0.001),
        )
        for index in range(260)
    )
    context = replace(context, ocr=(), icons=elements)
    phase, gestures, targets = question_profile(context, tools=REVIEW_TOOLS).questions
    assert len(phase.candidates) == 4
    assert len(gestures.candidates) == 2
    assert len(targets.candidates) == 261  # Provider limit, not hidden pruning.
    assert [c.target_ref for c in targets.candidates[1:]] == [e.ref for e in elements]


def test_duplicate_box_cannot_hide_a_consequential_label_in_either_channel(context):
    first = context.ocr[0]
    context = replace(
        context,
        icons=(replace(context.icons[0], bbox=first.bbox, text="支付"),),
    )
    profile = question_profile(context, tools=REVIEW_TOOLS)
    assert not profile.questions
    assert set(profile.blocked_targets) == {first.ref, context.icons[0].ref}
    assert profile.blocked_targets[context.icons[0].ref]["text"] == "支付"


def test_source_indices_survive_channel_order_vetoes_and_duplicate_boxes(context):
    observation_id = context.observation_id
    context = replace(
        context,
        ocr=(
            DecisionElement(f"{observation_id}:62", (0.1, 0.8, 0.3, 0.9), "搜索"),
            DecisionElement(f"{observation_id}:4", (0.1, 0.4, 0.3, 0.5), "微信"),
            DecisionElement(f"{observation_id}:1", (0.6, 0.1, 0.8, 0.2), "支付"),
        ),
        icons=(
            DecisionElement(f"{observation_id}:40", (0.1, 0.4, 0.3, 0.5)),
            DecisionElement(f"{observation_id}:17", (0.6, 0.5, 0.8, 0.6)),
            DecisionElement(f"{observation_id}:0", (0.6, 0.1, 0.8, 0.2)),
        ),
    )
    profile = question_profile(context, tools=REVIEW_TOOLS)
    targets = profile.questions[2].candidates
    assert [c.id for c in targets] == ["NONE", "b4", "b17", "b40", "b62"]
    assert [c.target_ref for c in targets[1:]] == [
        f"{observation_id}:4",
        f"{observation_id}:17",
        f"{observation_id}:40",
        f"{observation_id}:62",
    ]
    assert (
        profile.blocked_targets[f"{observation_id}:0"]["evidence_ref"]
        == f"{observation_id}:1"
    )
    assert set(profile.blocked_targets) == {
        f"{observation_id}:0",
        f"{observation_id}:1",
    }
    reordered = replace(
        context, ocr=tuple(reversed(context.ocr)), icons=tuple(reversed(context.icons))
    )
    assert question_profile(reordered, tools=REVIEW_TOOLS) == profile


@pytest.mark.parametrize("suffix", ["unknown", "01", "-1", "1:2"])
def test_noncanonical_target_indices_are_rejected_instead_of_renumbered(
    context, suffix
):
    context = replace(
        context,
        ocr=(replace(context.ocr[0], ref=f"{context.observation_id}:{suffix}"),),
    )
    with pytest.raises(ValueError, match="original listing index"):
        question_profile(context, tools=REVIEW_TOOLS)
