"""Shared API infrastructure must not merge inference capabilities/contexts."""

import asyncio
from dataclasses import replace

import pytest

from physiclaw.contract.decision import (
    ActionCandidate,
    DecisionAnswer,
    DecisionCapabilities,
    DecisionResult,
)
from physiclaw.provider import BaseAPIProvider, BaseChatProvider, BaseDecisionProvider
from physiclaw.provider.provider_base import (
    ProviderConfigurationError,
    ProviderPermanentError,
    ProviderTransientError,
)
from physiclaw.provider.vendors.typesafe.decision import TypeSafeDecisionProvider


class StubDecisionProvider(BaseDecisionProvider):
    PROVIDER_ID = "stubdecision"
    BASE_URL = "https://stub.example/v1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []

    @staticmethod
    def capabilities_for(model):
        return DecisionCapabilities(max_choices=2)

    async def _evaluate(self, request, *, deadline):
        self.requests.append((request, deadline))
        return DecisionResult(
            request.id,
            {q.id: DecisionAnswer(q.candidates[1].id) for q in request.questions},
            self.PROVIDER_ID,
            self.model,
            self.model,
        )


class StubChatProvider(BaseChatProvider):
    PROVIDER_ID = "stubdecision"
    BASE_URL = "https://stub.example/v1"


@pytest.fixture
async def provider(monkeypatch):
    monkeypatch.setenv("STUBDECISION_API_KEY", "fixture-only")
    instance = StubDecisionProvider("bounded-model")
    yield instance
    await instance.aclose()


def deadline():
    return asyncio.get_running_loop().time() + 5


async def test_shared_lifecycle_and_separate_capabilities(provider):
    assert isinstance(provider, BaseAPIProvider)
    assert issubclass(TypeSafeDecisionProvider, BaseDecisionProvider)
    assert not issubclass(TypeSafeDecisionProvider, BaseChatProvider)
    chat = StubChatProvider("chat-model")
    try:
        assert isinstance(chat, BaseAPIProvider)
        assert provider._client is not chat._client
        assert (
            provider._client.headers["authorization"]
            == chat._client.headers["authorization"]
        )
        assert chat._client.headers["content-type"] == "application/json"
        assert "content-type" not in provider._client.headers
        assert provider.ref == "stubdecision/bounded-model"
        assert chat.ref == "stubdecision/chat-model"
        for attribute in (
            "chat",
            "serialize_history",
            "usage_sink",
            "CACHE_MARKERS",
            "COLLAPSE",
        ):
            assert not hasattr(provider, attribute)
        assert not hasattr(chat, "evaluate")
        await provider.aclose()
        assert provider._client.is_closed
        assert not chat._client.is_closed
    finally:
        await chat.aclose()


async def test_generic_evaluate_adds_timing_without_inventing_scores(
    provider, decision_request
):
    limit = deadline()
    result = await provider.evaluate(decision_request, deadline=limit)
    assert provider.requests == [(decision_request, limit)]
    assert result.elapsed_seconds >= 0
    answer = result.answers["next_action"]
    assert answer.confidence is answer.probabilities is None
    assert result.usage.input_tokens is None


@pytest.mark.parametrize(
    "patch",
    [
        {"provider_id": "other-vendor"},
        {"requested_model_id": "other-requested-model"},
        {"request_id": "other-request"},
        {"answers": {"next_action": DecisionAnswer("invented-action")}},
    ],
)
async def test_generic_base_rejects_adapter_identity_and_choice_mismatch(
    provider, decision_request, monkeypatch, patch
):
    native = await provider._evaluate(decision_request, deadline=deadline())
    provider.requests.clear()

    async def broken(request, *, deadline):
        provider.requests.append(request)
        return replace(native, **patch)

    monkeypatch.setattr(provider, "_evaluate", broken)
    with pytest.raises(ProviderTransientError, match="malformed decision result"):
        await provider.evaluate(decision_request, deadline=deadline())
    assert len(provider.requests) == 1


async def test_generic_base_rejects_non_result(provider, decision_request, monkeypatch):
    async def broken(request, *, deadline):
        return None

    monkeypatch.setattr(provider, "_evaluate", broken)
    with pytest.raises(ProviderTransientError, match="malformed decision result"):
        await provider.evaluate(decision_request, deadline=deadline())


async def test_capability_gates_precede_adapter_call(provider, image_decision_request):
    with pytest.raises(ProviderPermanentError, match="does not support images"):
        await provider.evaluate(image_decision_request, deadline=deadline())
    request = replace(
        image_decision_request,
        questions=(
            replace(
                image_decision_request.questions[0],
                candidates=(
                    *image_decision_request.questions[0].candidates,
                    ActionCandidate("a3", "Third alternative"),
                ),
            ),
        ),
    )
    with pytest.raises(ProviderPermanentError, match="at most 2 choices"):
        await provider.evaluate(request, deadline=deadline())
    assert not provider.requests


def test_decision_model_is_explicit_even_with_model_env(monkeypatch):
    monkeypatch.setenv("STUBDECISION_MODEL", "implicit-chat-default")
    with pytest.raises(ProviderConfigurationError, match="model ID is required"):
        StubDecisionProvider("")


async def test_sdk_cleanup_uses_common_idempotent_lifecycle(provider, monkeypatch):
    calls = []

    async def sdk_close():
        calls.append("sdk.close")

    monkeypatch.setattr(provider, "_close_client", sdk_close)
    await provider.aclose()
    await provider.aclose()
    assert calls == ["sdk.close"]
    assert provider._closed
    # The test hook used a fake SDK close; release the owned HTTP fixture too.
    await provider._client.aclose()
