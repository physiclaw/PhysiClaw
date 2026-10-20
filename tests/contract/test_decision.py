"""Decision DTO boundaries: immutable facts, grounded choices, honest scores."""

from dataclasses import FrozenInstanceError, replace
from math import inf, nan

import pytest

from physiclaw.contract.decision import (
    ActionCandidate,
    DecisionAnswer,
    DecisionCapabilities,
    DecisionContext,
    DecisionElement,
    DecisionFrame,
    DecisionQuestion,
    DecisionRequest,
    DecisionResult,
    DecisionUsage,
    decision_json,
)


@pytest.fixture
def request_dto():
    return DecisionRequest(
        id="r1",
        context=DecisionContext(observation_id="o1"),
        questions=(
            DecisionQuestion(
                "next_action",
                "Choose an offered action.",
                (
                    ActionCandidate("YIELD", "Return control"),
                    ActionCandidate("home", "Go home", "home_screen"),
                ),
            ),
        ),
    )


def result_dto(**kwargs):
    return DecisionResult(
        request_id="r1",
        answers={"next_action": DecisionAnswer("home")},
        provider_id="fake",
        model_id="actual",
        requested_model_id="alias",
        **kwargs,
    )


def test_deep_freeze_copies_every_nested_container():
    facts = {"goal": {"inputs": ["微信", {"status": "resolved"}]}}
    args = {"bbox": [0.1, 0.2, 0.3, 0.4]}
    context = DecisionContext("o1", facts=facts)
    candidate = ActionCandidate("a1", "Tap icon", "tap", args)
    facts["goal"]["inputs"][1]["status"] = "unresolved"
    args["bbox"][0] = 0.9
    assert decision_json(context.facts) == {
        "goal": {"inputs": ["微信", {"status": "resolved"}]}
    }
    assert decision_json(candidate.arguments) == {"bbox": [0.1, 0.2, 0.3, 0.4]}
    with pytest.raises(TypeError):
        context.facts["goal"]["inputs"][1]["status"] = "changed"
    with pytest.raises(TypeError):
        candidate.arguments["bbox"][0] = 0.7
    wire = decision_json(context.facts)
    wire["goal"]["inputs"].clear()
    assert len(context.facts["goal"]["inputs"]) == 2
    with pytest.raises(FrozenInstanceError):
        candidate.tool = "send_money"


def test_request_and_element_sequences_are_copied(request_dto):
    bbox = [0, 0, 0.3, 0.3]
    elements = [DecisionElement("o1:e1", bbox, "微信")]
    context = DecisionContext("o1", ocr=elements)
    choices = list(request_dto.questions[0].candidates)
    questions = [replace(request_dto.questions[0], candidates=choices)]
    request = replace(request_dto, context=context, questions=questions)
    bbox[2] = 0.8
    choices.clear()
    questions.clear()
    elements.clear()
    assert len(request.questions[0].candidates) == 2
    assert request.context.ocr[0].bbox == (0, 0, 0.3, 0.3)


@pytest.mark.parametrize(
    "value", [nan, inf, -inf, b"bytes", object(), {1: "bad"}, {"a": {1, 2}}]
)
def test_non_json_facts_rejected(value):
    with pytest.raises(ValueError):
        DecisionContext("o1", facts={"bad": value})


def test_cycles_and_overdeep_facts_rejected():
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="depth"):
        DecisionContext("o1", facts={"cyclic": cyclic})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"id": ""},
        {"data": bytearray(b"x")},
        {"data": b""},
        {"media_type": "image/gif"},
        {"width": True},
        {"height": -1},
    ],
)
def test_invalid_frame_rejected(kwargs):
    fields = dict(
        id="o1", data=b"pixels", media_type="image/jpeg", width=300, height=300
    )
    with pytest.raises(ValueError):
        DecisionFrame(**(fields | kwargs))


def test_frame_digest_and_identity():
    frame = DecisionFrame("o1", b"pixels", "image/jpeg", 300, 300)
    assert len(frame.sha256) == 64
    assert "pixels" not in repr(frame)
    with pytest.raises(ValueError, match="identity"):
        DecisionContext("o2", frame=frame)


@pytest.mark.parametrize(
    "bbox", [[0, 0, 0, 1], [0, 0, 2, 1], [0, nan, 1, 1], [0, 0, True, 1]]
)
def test_bad_element_geometry_rejected(bbox):
    with pytest.raises(ValueError):
        DecisionElement("o1:e1", bbox)


def test_elements_must_be_unique_and_current():
    element = DecisionElement("o1:e1", (0, 0, 1, 1))
    with pytest.raises(ValueError, match="current observation"):
        DecisionContext("o2", ocr=(element,))
    with pytest.raises(ValueError, match="duplicate"):
        DecisionContext("o1", ocr=(element,), icons=(element,))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"arguments": {"text": "x"}},
        {"target_ref": ""},
        {"id": ""},
        {"description": ""},
        {"tool": ""},
    ],
)
def test_bad_candidate_rejected(kwargs):
    with pytest.raises(ValueError):
        ActionCandidate(**(dict(id="YIELD", description="Return control") | kwargs))


def test_target_choice_references_evidence_without_an_executable_tool(request_dto):
    target = ActionCandidate("b0", "Select this observed box", target_ref="o1:e1")
    assert target.tool is None
    assert not target.arguments
    with pytest.raises(ValueError, match="action arguments"):
        replace(target, arguments={"bbox": [0, 0, 1, 1]})
    question = replace(
        request_dto.questions[0],
        candidates=(request_dto.questions[0].candidates[0], target),
    )
    bound = replace(
        request_dto,
        context=replace(
            request_dto.context, ocr=(DecisionElement("o1:e1", (0, 0, 1, 1)),)
        ),
        questions=(question,),
    )
    assert bound.questions[0].candidates[1] is target
    # Reference validation applies even to non-executable target choices.
    with pytest.raises(ValueError, match="current observation"):
        replace(
            request_dto,
            questions=(question,),
        )


def test_invalid_request_choice_set_and_target(request_dto):
    question = request_dto.questions[0]
    for choices in ((), question.candidates[:1], (question.candidates[0],) * 2):
        with pytest.raises(ValueError, match="uniquely identified"):
            replace(question, candidates=choices)
    with pytest.raises(ValueError, match="current observation"):
        replace(
            request_dto,
            questions=(
                replace(
                    question,
                    candidates=(
                        question.candidates[0],
                        ActionCandidate("a1", "Tap", "tap", target_ref="o0:e1"),
                    ),
                ),
            ),
        )


@pytest.mark.parametrize("value", [nan, inf, True, "0.8", -0.1, 1.1])
def test_invalid_confidence_rejected(value):
    with pytest.raises(ValueError):
        DecisionAnswer("home", confidence=value)


@pytest.mark.parametrize(
    "probabilities",
    [
        {},
        {"home": 0.4},
        {"home": nan},
        {"home": inf},
        {"home": True},
        {"home": "1"},
        {"home": 1.1, "YIELD": -0.1},
    ],
)
def test_invalid_distribution_rejected(probabilities):
    with pytest.raises(ValueError):
        DecisionAnswer("home", probabilities=probabilities)


def test_result_matching_and_probability_ties(request_dto):
    answer = DecisionAnswer("home", probabilities={"home": 0.5, "YIELD": 0.5})
    result = replace(result_dto(), answers={"next_action": answer})
    result.validate_for(request_dto)
    with pytest.raises(TypeError):
        result.answers["next_action"].probabilities["home"] = 0
    with pytest.raises(ValueError):
        replace(result, request_id="r2").validate_for(request_dto)
    for broken in (
        replace(answer, selected_id="unknown"),
        replace(answer, probabilities={"home": 0.5, "unknown": 0.5}),
        replace(answer, probabilities={"home": 0.1, "YIELD": 0.9}),
    ):
        with pytest.raises(ValueError):
            replace(result, answers={"next_action": broken}).validate_for(request_dto)


def test_rounding_tolerance_and_scores_are_optional(request_dto):
    DecisionAnswer(
        "home", probabilities={"home": 0.6, "YIELD": 0.3999999}
    ).validate_for(request_dto.questions[0])
    result = result_dto()
    result.validate_for(request_dto)
    assert (
        result.answers["next_action"].confidence
        is result.answers["next_action"].probabilities
        is result.usage.input_tokens
        is result.usage.output_tokens
        is None
    )
    assert DecisionUsage(input_tokens=0).input_tokens == 0


@pytest.mark.parametrize(
    "kwargs", [{"input_tokens": True}, {"output_tokens": -1}, {"input_tokens": 1.5}]
)
def test_invalid_usage(kwargs):
    with pytest.raises(ValueError):
        DecisionUsage(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [{"elapsed_seconds": nan}, {"elapsed_seconds": -1}, {"provider_request_id": ""}],
)
def test_invalid_metadata(kwargs):
    with pytest.raises(ValueError):
        result_dto(**kwargs)


@pytest.mark.parametrize("maximum", [1, True, 1.5])
def test_invalid_capabilities(maximum):
    with pytest.raises(ValueError):
        DecisionCapabilities(max_choices=maximum)


@pytest.mark.parametrize("maximum", [0, True, 1.5])
def test_invalid_question_limit(maximum):
    with pytest.raises(ValueError):
        DecisionCapabilities(max_questions=maximum)


def test_questions_have_unique_names_and_question_local_choice_ids(request_dto):
    question = request_dto.questions[0]
    for questions in ((), (question, question)):
        with pytest.raises(ValueError, match="uniquely identified questions"):
            replace(request_dto, questions=questions)
    with pytest.raises(ValueError):
        replace(question, id=" ")
    with pytest.raises(ValueError):
        replace(question, instructions="")
    request = replace(request_dto, questions=(question, replace(question, id="check")))
    # The same option IDs may have different meanings in different questions.
    result = replace(
        result_dto(),
        answers={
            "next_action": DecisionAnswer("home"),
            "check": DecisionAnswer("YIELD"),
        },
    )
    result.validate_for(request)


def test_result_copies_named_answers_and_requires_the_exact_question_set(request_dto):
    distribution = {"home": 0.9, "YIELD": 0.1}
    answers = {"next_action": DecisionAnswer("home", probabilities=distribution)}
    result = replace(result_dto(), answers=answers)
    answers.clear()
    distribution["home"] = 0
    result.validate_for(request_dto)
    assert result.answers["next_action"].probabilities["home"] == 0.9
    with pytest.raises(TypeError):
        result.answers["next_action"] = DecisionAnswer("YIELD")
    with pytest.raises(FrozenInstanceError):
        result.answers["next_action"].selected_id = "YIELD"
    for broken in (
        {},
        {"other": DecisionAnswer("home")},
        {**result.answers, "extra": DecisionAnswer("home")},
    ):
        with pytest.raises(ValueError, match="requested questions"):
            replace(result, answers=broken).validate_for(request_dto)
    with pytest.raises(ValueError, match="immutable DecisionAnswer"):
        replace(result, answers={"next_action": {"selected_id": "home"}})
