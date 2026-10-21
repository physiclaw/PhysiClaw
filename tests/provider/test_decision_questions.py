"""One shared observation, named questions, and no partial bundle acceptance."""

import asyncio
import base64
import json
from dataclasses import replace
from email import policy
from email.parser import BytesParser

import pytest

from physiclaw.common.provider_errors import (
    ProviderError,
    ProviderRequestError,
    ProviderTransientError,
)
from physiclaw.contract.decision import ActionCandidate, DecisionRequest
from physiclaw.decision.policy import CONSERVATIVE_PROMPT, question_profile
from physiclaw.provider import make_decision_provider


@pytest.fixture(
    params=[
        ("typesafe", "jev-1.14.0"),
        ("openai", "gpt-6-luna"),
        ("openrouter", "typesafe/jev-1.14"),
        ("openrouter", "liquid/d1-ultra"),
        ("liquid", "d1-ultra"),
    ]
)
async def provider(request, monkeypatch):
    vendor, model = request.param
    monkeypatch.setenv(f"{vendor.upper()}_API_KEY", "fixture-only")
    instance = make_decision_provider(vendor, model, require_images=True)
    yield instance
    await instance.aclose()


@pytest.fixture
def review(image_decision_request):
    context = image_decision_request.context
    profile = question_profile(context, tools={"tap", "long_press"})
    return DecisionRequest("review", context, profile.questions)


def url(provider):
    return {
        "typesafe": "https://api.typesafe.ai/v1/systemone",
        "openai": "https://api.openai.com/v1/decisions",
        "openrouter": "https://openrouter.ai/api/alpha/decisions",
        "liquid": "https://api.liquid.ai/decisions/v1/systemone",
    }[provider.PROVIDER_ID]


def deadline():
    return asyncio.get_running_loop().time() + 5


def reply(provider, request, *, phase="ROUTINE", target="b0"):
    selections = {"phase": phase, "action_type": "tap", "target": target}
    answers = {}
    for index, question in enumerate(request.questions):
        selected = selections[question.id]
        probabilities = {c.id: float(c.id == selected) for c in question.candidates}
        answer = {
            "type": "choice",
            "choice": selected,
            "confidence": 0.7 + index / 10,
            "probabilities": probabilities,
        }
        if provider.PROVIDER_ID == "openai":
            answer["name"] = question.id
            answer["probabilities"] = [
                {"value": key, "probability": value}
                for key, value in probabilities.items()
            ]
        answers[question.id] = answer
    return {
        "model": provider.model,
        "answers": answers
        if provider.PROVIDER_ID != "openai"
        else list(answers.values()),
        "usage": {"input_tokens": 450, "output_tokens": 0},
    }


def named_answers(payload):
    answers = payload["answers"]
    if isinstance(answers, list):
        return {answer["name"]: answer for answer in answers}
    return answers


async def test_preview_questions_reach_all_adapters_with_one_image(
    provider, review, respx_mock
):
    payload = reply(provider, review)
    if isinstance(payload["answers"], list):
        payload["answers"].reverse()  # Bind by name, never by response position.
    route = respx_mock.post(url(provider)).respond(json=payload)
    result = await provider.evaluate(review, deadline=deadline())
    result.validate_for(review)
    assert {key: a.selected_id for key, a in result.answers.items()} == {
        "phase": "ROUTINE",
        "action_type": "tap",
        "target": "b0",
    }
    for index, question in enumerate(review.questions):
        answer = result.answers[question.id]
        assert answer.confidence == 0.7 + index / 10
        assert set(answer.probabilities) == {c.id for c in question.candidates}
    assert result.usage.input_tokens == 450  # Request-level usage, counted once.
    assert route.call_count == 1
    wire = route.calls[0].request
    if provider.PROVIDER_ID == "typesafe":
        multipart = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {wire.headers['content-type']}\r\n\r\n".encode()
            + wire.content
        )
        parts = list(multipart.iter_parts())
        assert len(parts) == 2
        body = json.loads(parts[0].get_payload(decode=True))
        assert parts[1].get_payload(decode=True) == review.context.frame.data
        assert len(body["assets"]) == 1
        questions = body["questions"]
    elif provider.PROVIDER_ID == "openai":
        body = json.loads(wire.content)
        text, image = body["input"][0]["content"]
        state = json.loads(text["text"])
        assert state["observation"]["id"] == review.context.observation_id
        assert (
            base64.b64decode(image["image_url"].split(",", 1)[1])
            == review.context.frame.data
        )
        questions = {q["name"]: q for q in body["questions"]}
        for question in questions.values():
            question["options"] = {
                c["value"]: json.loads(c["description"]) for c in question["choices"]
            }
    else:
        body = json.loads(wire.content)
        if provider.PROVIDER_ID == "liquid":
            state = body["state"]
            assert len(body["images"]) == 1
            image_url = body["images"][0]
        else:
            text, image = body["state"]
            state = json.loads(text)
            assert image["type"] == "image_url"
            image_url = image["image_url"]["url"]
            assert "images" not in body
        assert state["observation"]["id"] == review.context.observation_id
        assert base64.b64decode(image_url.split(",", 1)[1]) == review.context.frame.data
        assert "assets" not in body
        questions = body["questions"]
    if provider.PROVIDER_ID != "openai":
        for question in questions.values():
            question["options"] = question.pop("criteria")
    assert set(questions) == {"phase", "action_type", "target"}
    for question in review.questions:
        assert questions[question.id]["instructions"] == question.instructions
        assert question.instructions.startswith(CONSERVATIVE_PROMPT + "\n\n")
    assert list(questions["action_type"]["options"]) == ["YIELD", "tap"]
    assert list(questions["target"]["options"]) == ["NONE", "b0", "b1"]
    target = questions["target"]["options"]["b0"]
    assert target["target_ref"] == "o17:0"
    assert "微信" in target["description"]
    assert "bbox" not in target["description"]
    assert "tool" not in target and "arguments" not in target
    assert "messages" not in body


@pytest.mark.parametrize(
    "problem", ["missing", "extra", "wrong_choice", "wrong_scores"]
)
async def test_a_bad_secondary_answer_rejects_the_entire_bundle(
    provider, review, respx_mock, problem
):
    payload = reply(provider, review)
    answers = named_answers(payload)
    if problem == "missing":
        del answers["target"]
    elif problem == "extra":
        extra = dict(answers["target"], name="unexpected")
        answers["unexpected"] = extra
    elif problem == "wrong_choice":
        answers["target"]["choice"] = "tap"  # Valid only in another question.
    else:
        probabilities = {"tap": 1.0, "YIELD": 0.0}
        answers["target"]["probabilities"] = (
            probabilities
            if provider.PROVIDER_ID != "openai"
            else [
                {"value": key, "probability": value}
                for key, value in probabilities.items()
            ]
        )
    payload["answers"] = (
        answers if provider.PROVIDER_ID != "openai" else list(answers.values())
    )
    route = respx_mock.post(url(provider)).respond(json=payload)
    with pytest.raises(ProviderTransientError, match="malformed"):
        await provider.evaluate(review, deadline=deadline())
    assert route.call_count == 1


@pytest.mark.parametrize(
    "status,kind",
    [
        (400, ProviderRequestError),
        (408, ProviderTransientError),
        (413, ProviderRequestError),
        (422, ProviderRequestError),
    ],
)
async def test_request_or_temporary_http_failure_allows_a_later_request(
    provider, review, respx_mock, status, kind
):
    private_body = "private server response"
    route = respx_mock.post(url(provider)).respond(status, text=private_body)
    with pytest.raises(kind, match=f"HTTP {status}") as error:
        await provider.evaluate(review, deadline=deadline())
    assert private_body not in str(error.value)
    assert route.call_count == 1

    following = replace(review, id="next-request")
    route.respond(json=reply(provider, following))
    result = await provider.evaluate(following, deadline=deadline())
    assert result.request_id == following.id
    assert route.call_count == 2


async def test_refused_secondary_answer_never_returns_a_partial_result(
    provider, review, respx_mock
):
    payload = reply(provider, review)
    answer = named_answers(payload)["target"]
    answer.clear()
    answer.update(type="refusal", name="target")
    route = respx_mock.post(url(provider)).respond(json=payload)
    with pytest.raises(ProviderError):
        await provider.evaluate(review, deadline=deadline())
    assert route.call_count == len(respx_mock.calls) == 1


async def test_disagreement_and_unknown_scores_are_preserved_per_question(
    provider, review, respx_mock
):
    payload = reply(provider, review, phase="SEMANTIC", target="NONE")
    answer = named_answers(payload)["target"]
    answer.pop("confidence")
    answer.pop("probabilities")
    respx_mock.post(url(provider)).respond(json=payload)
    result = await provider.evaluate(review, deadline=deadline())
    assert result.answers["action_type"].selected_id == "tap"
    assert result.answers["phase"].selected_id == "SEMANTIC"
    assert result.answers["target"].selected_id == "NONE"
    assert result.answers["target"].confidence is None
    assert result.answers["target"].probabilities is None
    assert result.answers["phase"].confidence == 0.7


async def test_duplicate_answer_names_are_rejected(provider, review, respx_mock):
    payload = reply(provider, review)
    if provider.PROVIDER_ID == "openai":
        payload["answers"].append(payload["answers"][-1])
        raw = json.dumps(payload)
    else:
        raw = json.dumps(payload)
        duplicate = json.dumps(payload["answers"]["target"])
        raw = raw.replace('"answers": {', '"answers": {"target": ' + duplicate + ",")
    respx_mock.post(url(provider)).respond(text=raw)
    with pytest.raises(ProviderTransientError, match="malformed"):
        await provider.evaluate(review, deadline=deadline())


async def test_choice_limit_applies_to_every_question_before_http(
    provider, review, respx_mock
):
    questions = list(review.questions)
    questions[-1] = replace(
        questions[-1],
        candidates=tuple(ActionCandidate(f"c{i}", "Candidate") for i in range(256)),
    )
    request = replace(review, questions=questions)
    with pytest.raises(ProviderRequestError, match="choices per question"):
        await provider.evaluate(request, deadline=deadline())
    assert not respx_mock.calls
