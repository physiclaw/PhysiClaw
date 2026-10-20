"""Bounded-choice capability over the same API lifecycle as chat providers.

The base owns one attempt, deadline/cancellation, capability checks, result
binding and elapsed time. Adapters implement only their native request and
reply translation. It carries no chat history, cache policy or execution.
Implementing the contract's structural protocol without this base remains
supported for providers with a different transport/lifecycle.
"""

import asyncio
import math
from dataclasses import replace
from typing import ClassVar, Literal

import httpx

from physiclaw.common.provider_errors import (
    ProviderConfigurationError,
    ProviderPermanentError,
    ProviderRequestError,
    ProviderTransientError,
)
from physiclaw.contract.decision import (
    DecisionCapabilities,
    DecisionRequest,
    DecisionResult,
)
from physiclaw.provider.provider_base import BaseAPIProvider


class BaseDecisionProvider(BaseAPIProvider):
    api_kind: ClassVar[Literal["decision"]] = "decision"

    def __init__(
        self, model: str, *, timeout: float = 120.0, base_url: str | None = None
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ProviderConfigurationError("a decision model ID is required")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ProviderConfigurationError(
                "decision provider timeout must be finite and positive"
            )
        self.capabilities = self.capabilities_for(model)
        super().__init__(model, timeout=timeout, base_url=base_url)

    @staticmethod
    def capabilities_for(model: str) -> DecisionCapabilities:
        raise NotImplementedError("decision adapter must declare model capabilities")

    @staticmethod
    def _remaining(deadline: float) -> float:
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            raise ProviderRequestError(
                "decision deadline must be finite monotonic time"
            )
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise ProviderTransientError("decision deadline exhausted")
        return remaining

    async def evaluate(
        self, request: DecisionRequest, *, deadline: float
    ) -> DecisionResult:
        if self._closed:
            raise ProviderPermanentError("decision provider is closed")
        self._remaining(deadline)
        maximum_questions = self.capabilities.max_questions
        if maximum_questions is not None and len(request.questions) > maximum_questions:
            raise ProviderRequestError(
                f"decision provider supports at most {maximum_questions} questions"
            )
        maximum = self.capabilities.max_choices
        if maximum is not None and any(
            len(question.candidates) > maximum for question in request.questions
        ):
            raise ProviderRequestError(
                f"decision provider supports at most {maximum} choices per question"
            )
        if request.context.frame is not None and not self.capabilities.images:
            raise ProviderRequestError(
                "decision provider/model does not support images"
            )
        started = asyncio.get_running_loop().time()
        try:
            async with asyncio.timeout_at(deadline):
                result = await self._evaluate(request, deadline=deadline)
                try:
                    if not isinstance(result, DecisionResult):
                        raise ValueError("expected a decision result")
                    result.validate_for(request)
                    if (
                        result.provider_id != self.PROVIDER_ID
                        or result.requested_model_id != self.model
                    ):
                        raise ValueError(
                            "decision result provider/model identity mismatch"
                        )
                except ValueError:
                    raise ProviderTransientError("malformed decision result") from None
                result = replace(
                    result, elapsed_seconds=asyncio.get_running_loop().time() - started
                )
                self._remaining(deadline)
                return result
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderTransientError(
                "decision deadline or transport timeout"
            ) from None
        except httpx.HTTPError:
            raise ProviderTransientError("decision transport failed") from None
        # CancelledError propagates. A later consumer owns fallback; no retry.

    async def _evaluate(
        self, request: DecisionRequest, *, deadline: float
    ) -> DecisionResult:
        raise NotImplementedError("decision adapter must implement _evaluate")
