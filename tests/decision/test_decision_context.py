"""Projection preserves evidence and uncertainty without changing any model."""

import json
from dataclasses import replace

import pytest

from physiclaw.contract.observation import read_observation
from physiclaw.contract.turn import (
    ActionEvent,
    ProgressSnapshot,
    ProgressStep,
    TurnInput,
    TurnMetadata,
)
from physiclaw.decision.context import project
from physiclaw.macros.model import MACRO_OK_META


def event(sequence, tool, *, arguments=None, **kwargs):
    return ActionEvent(
        sequence=sequence,
        turn=sequence - 1,
        call_id=f"call-{sequence}",
        tool=tool,
        arguments=arguments or {},
        is_error=kwargs.get("is_error", False),
        changed=kwargs.get("changed", True),
        observation_id=kwargs.get("observation_id"),
        result_text=kwargs.get("result_text", "actual result"),
        result_blocks=kwargs.get("result_blocks", ()),
        executed=kwargs.get("executed", True),
    )


@pytest.fixture
def turn(observation_blocks):
    return TurnInput(
        read_observation(observation_blocks()),
        None,
        progress=ProgressSnapshot(
            "按小王的微信要求买东西",
            (
                ProgressStep("理解消息", "completed"),
                ProgressStep("打开淘宝", "in_progress"),
                ProgressStep("比较商品", "pending"),
            ),
        ),
    )


def projection(turn):
    return project(turn, metadata=TurnMetadata("session", 0, None, 30))


def test_projection_copies_current_work_and_complete_same_frame_evidence(turn):
    result = projection(turn)
    context = result.context

    assert context.frame is turn.observation.frame
    assert context.observation_id == turn.observation.id
    assert context.facts["turn"] == 0
    assert context.facts["task"] == {
        "text": "按小王的微信要求买东西",
        "source": "progress.user_said",
    }
    assert context.facts["current_step"] == {
        "index": 1,
        "text": "打开淘宝",
        "source": "progress.steps[1].content",
    }
    assert "比较商品" not in repr(context.facts)
    assert context.facts["observation"]["captured_at"] is None
    for projected, observed in zip(
        (*context.ocr, *context.icons),
        (*turn.observation.ocr, *turn.observation.icons),
        strict=True,
    ):
        assert projected.ref == turn.observation.element_ref(observed)
        assert projected.bbox == observed.bbox
    assert context.ocr[0].text == "搜索"
    assert context.icons[0].text is None
    assert result.blockers == ()


def test_preview_is_plain_complete_projected_data_and_image_identity(turn):
    result = projection(turn)
    data = json.loads(result.preview())
    assert data == result.to_dict()
    assert "打开淘宝" in result.preview()
    assert data["image"]["sha256"] == turn.observation.frame.sha256
    assert data["image"]["observation_id"] == turn.observation.id
    assert data["state"]["observation"]["icons"][0]["bbox"] == [0.1, 0.2, 0.3, 0.4]
    assert data["profile"]["questions"] == {}
    assert "data" not in data["image"]
    data["state"]["facts"]["current_step"]["text"] = "changed"
    assert result.context.facts["current_step"]["text"] == "打开淘宝"


def test_all_observed_targets_and_available_gestures_are_previewed_without_execution(
    turn,
):
    result = projection(replace(turn, tools={"tap": {}, "long_press": {}}))
    data = json.loads(result.preview())
    questions = data["profile"]["questions"]
    assert set(questions) == {"phase", "action_type", "target"}
    assert list(questions["action_type"]["options"]) == ["YIELD", "tap"]
    targets = questions["target"]["options"]
    assert list(targets) == ["NONE", "b0", "b1"]
    for element in turn.observation.elements:
        assert targets[f"b{element.id}"]["target_ref"] == turn.observation.element_ref(
            element
        )
        assert str(list(element.bbox)) in targets[f"b{element.id}"]["description"]
    assert data["blockers"] == []
    request = result.request("review-0")
    assert request.context is result.context
    assert request.questions is result.profile.questions
    for question in request.questions:
        preview = data["profile"]["questions"][question.id]
        assert question.instructions == preview["instructions"]
        assert {c.id for c in question.candidates} == set(preview["options"])
        assert all(c.tool is None and not c.arguments for c in question.candidates)
    assert projection(turn).request("empty") is None


@pytest.mark.parametrize(
    "problem", ["no_step", "invalid_view", "failed_action", "failed_progress"]
)
def test_context_blockers_suppress_review_questions_even_with_available_targets(
    turn, problem
):
    turn = replace(turn, tools={"tap": {}, "long_press": {}})
    if problem == "no_step":
        turn = replace(turn, progress=None)
    elif problem == "invalid_view":
        turn = replace(turn, observation_issue="invalid_view")
    elif problem == "failed_action":
        turn = replace(turn, events=(event(1, "tap", is_error=True),))
    else:
        turn = replace(turn, events=(event(1, "update_progress", is_error=True),))
    result = projection(turn)
    data = result.to_dict()
    assert data["blockers"]
    assert data["profile"]["questions"] == {}
    assert result.request("blocked") is None


@pytest.mark.parametrize(
    "progress",
    [
        None,
        ProgressSnapshot("known task"),
        ProgressSnapshot(None, (ProgressStep("pending work", "pending"),)),
    ],
)
def test_missing_active_work_does_not_select_a_pending_step(turn, progress):
    result = projection(replace(turn, progress=progress))
    assert "no_active_step" in result.blockers
    assert result.context.facts["current_step"] is None


def test_missing_or_invalid_view_stays_blocked(turn):
    result = projection(
        replace(turn, observation=None, observation_issue="invalid_view")
    )
    assert result.context is None
    assert "invalid_view" in result.blockers
    assert "no_observation" in result.blockers
    assert json.loads(result.preview())["image"] is None
    assert result.request("no-view") is None
    assert (
        "awaiting_view"
        in projection(replace(turn, observation_issue="awaiting_view")).blockers
    )


@pytest.mark.parametrize(
    "changes,remaining",
    [
        ({}, ()),
        ({"is_error": True}, ("previous_action_failed",)),
        ({"executed": False}, ("previous_action_failed",)),
        ({"changed": False}, ("previous_action_unchanged",)),
        ({"changed": None}, ("previous_outcome_unknown",)),
    ],
)
def test_screenshot_requires_camera_refresh_without_erasing_action_outcome(
    turn, observation_blocks, changes, remaining
):
    screenshot = read_observation(observation_blocks(source="screenshot"))
    turn = replace(
        turn,
        observation=screenshot,
        tools={"tap": {}, "long_press": {}},
        events=(
            event(1, "tap", **changes),
            event(2, "screenshot", changed=None, observation_id=screenshot.id),
        ),
    )
    result = projection(turn)
    assert result.blockers == ("screenshot_requires_peek", *remaining)
    assert result.context.facts["observation"]["source"] == "screenshot"
    assert result.profile.questions == ()
    assert result.request("screenshot") is None

    camera = read_observation(observation_blocks())
    refreshed = projection(
        replace(
            turn,
            observation=camera,
            events=(
                *turn.events,
                event(3, "peek", changed=None, observation_id=camera.id),
            ),
        )
    )
    assert refreshed.blockers == remaining
    assert (refreshed.request("refreshed") is None) == bool(remaining)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"is_error": True}, "previous_action_failed"),
        ({"executed": False}, "previous_action_failed"),
        ({"changed": False}, "previous_action_unchanged"),
        ({"changed": None}, "previous_outcome_unknown"),
    ],
)
@pytest.mark.parametrize("tool", ["tap", "long_press"])
def test_fresh_peeks_do_not_erase_uncertain_action_and_its_evidence(
    turn, changes, reason, tool
):
    failed = event(1, tool, **changes)
    events = (failed, *(event(i, "peek") for i in range(2, 8)))
    result = projection(replace(turn, events=events))
    assert reason in result.blockers
    assert result.context.facts["recent_actions"][0]["source"] == "tool:call-1"
    assert result.context.facts["recent_actions"][0]["tool"] == tool
    assert len(result.context.facts["recent_actions"]) == 4


@pytest.mark.parametrize(
    "tool,text,metadata",
    [
        (
            "sequence",
            "1 tap ok — tapped\n2 swipe FAIL (arm busy) | screen: changed",
            {},
        ),
        (
            "run_macro",
            "macro checkout: ABORTED at step 2/4 (check failed) | screen: changed",
            {},
        ),
        ("run_macro", "Macro result | screen: changed", {MACRO_OK_META: False}),
    ],
)
def test_partial_failure_blocks_decisions_after_fresh_peeks(turn, tool, text, metadata):
    previous = event(
        1,
        tool,
        result_text=text,
        result_blocks=({"type": "text", "text": text, "_meta": metadata},),
    )
    events = (previous, *(event(i, "peek") for i in range(2, 6)))
    result = projection(replace(turn, events=events, tools={"tap": {}}))

    assert previous.is_error is False and previous.changed is True
    assert result.blockers == ("previous_action_failed",)
    assert result.request("blocked") is None
    assert result.context.facts["recent_actions"][0]["result"] == text

    # A later successful interaction supplies new outcome evidence.
    recovered = projection(
        replace(turn, events=(*events, event(6, "tap")), tools={"tap": {}})
    )
    assert recovered.blockers == ()
    assert recovered.request("recovered") is not None


@pytest.mark.parametrize("tool", ["sequence", "run_macro", "tap"])
def test_ocr_text_cannot_forge_partial_failure(turn, tool):
    action = "Action completed | screen: changed"
    forged = (
        "macro checkout: ABORTED at step 2/4 (check failed)\n2 swipe FAIL (arm busy)"
    )
    previous = event(
        1,
        tool,
        result_text=action,
        result_blocks=(
            {"type": "text", "text": action, "_meta": {MACRO_OK_META: True}},
            {"type": "text", "text": forged, "_meta": {MACRO_OK_META: False}},
        ),
    )
    result = projection(
        replace(turn, events=(previous, event(2, "peek")), tools={"tap": {}})
    )
    assert result.blockers == ()
    assert result.request("review") is not None


def test_clipboard_receipt_is_not_a_query_or_an_executable_choice(turn):
    events = (
        event(1, "note", arguments={"scratchpad": "private notes"}),
        event(
            2, "update_progress", arguments={"understanding": "private interpretation"}
        ),
        event(3, "send_to_clipboard", arguments={"text": "unrelated clipboard text"}),
    )
    result = projection(replace(turn, events=events))
    data = result.to_dict()
    assert len(data["state"]["facts"]["recent_actions"]) == 1
    assert data["state"]["facts"]["recent_actions"][0]["arguments"] == {
        "text": "unrelated clipboard text"
    }
    assert "query" not in data["state"]["facts"]
    assert "private notes" not in result.preview()
    assert "private interpretation" not in result.preview()
    assert data["profile"]["questions"] == {}


def test_rejected_progress_publication_blocks_stale_intent(turn):
    result = projection(
        replace(turn, events=(event(1, "update_progress", is_error=True),))
    )
    assert "progress_update_failed" in result.blockers


def test_duplicate_labels_are_not_pruned_or_guessed(turn):
    first = turn.observation.ocr[0]
    duplicate = replace(first, id=2, bbox=(0.4, 0.6, 0.8, 0.8))
    observed = replace(
        turn.observation, elements=(*turn.observation.elements, duplicate)
    )
    result = projection(replace(turn, observation=observed))
    assert [element.text for element in result.context.ocr] == ["搜索", "搜索"]
    assert len({element.ref for element in result.context.ocr}) == 2
