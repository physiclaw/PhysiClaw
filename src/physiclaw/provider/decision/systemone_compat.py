"""Shared System One Choice encoding, validation and JSON transport.

Only the wire protocol is shared. Vendors declare limits and encode images;
consumer policy, action execution and LLM context do not belong here.
"""

from __future__ import annotations

import base64
import json
import math
from hashlib import sha256

import httpx

from physiclaw.common.provider_errors import (
    ProviderRequestError,
    ProviderTransientError,
)
from physiclaw.contract.decision import (
    DecisionAnswer,
    DecisionCapabilities,
    DecisionFrame,
    DecisionRequest,
    DecisionResult,
    candidate_to_json,
    context_to_json,
)
from physiclaw.provider.decision.base import BaseDecisionProvider
from physiclaw.provider.decision.wire import (
    check_response_status,
    parse_usage,
    unique_object,
)

LIQUID_MAX_REQUEST_BYTES = 4_500_000


def image_data_url(frame: DecisionFrame) -> str:
    return (
        f"data:{frame.media_type};base64,{base64.b64encode(frame.data).decode('ascii')}"
    )


def check_liquid_image(frame: DecisionFrame) -> None:
    """Liquid's documented patch and aspect limits, shared by both routes."""
    patches = ((frame.width + 31) // 32) * ((frame.height + 31) // 32)
    if patches > 10_000 or max(frame.width, frame.height) > 100 * min(
        frame.width, frame.height
    ):
        raise ProviderRequestError("Liquid image exceeds patch or aspect ratio limit")


class SystemOneDecisionProvider(BaseDecisionProvider):
    ENDPOINT = "systemone"
    MAX_REQUEST_BYTES = 32 * 1024 * 1024

    @staticmethod
    def capabilities_for(model: str) -> DecisionCapabilities:
        return DecisionCapabilities(
            images=True, probabilities=True, confidence=True, max_choices=255
        )

    def _request_limit(self) -> int:
        return self.MAX_REQUEST_BYTES

    def _body(self, request: DecisionRequest) -> dict[str, object]:
        context = request.context
        # Aliases exist only in this wire copy. Answers select unchanged
        # candidate IDs, so execution still binds to the full internal refs.
        aliases: dict[str, str] = {}

        def short_id(value: str) -> str:
            if value not in aliases:
                alias = value[:7]
                salt = 0
                while alias in aliases.values():
                    salt += 1
                    alias = sha256(f"{value}:{salt}".encode()).hexdigest()[:7]
                aliases[value] = alias
            return aliases[value]

        observation_id = short_id(context.observation_id)

        def short_refs(text: str) -> str:
            return text.replace(context.observation_id + ":", observation_id + ":")

        state: dict = context_to_json(context)
        state["observation"]["id"] = observation_id
        for element in (*state["observation"]["ocr"], *state["observation"]["icons"]):
            element["ref"] = short_refs(element["ref"])
        # Receipt provenance is a reference; literal text and action arguments
        # remain evidence and must not be rewritten.
        recent = state["facts"].get("recent_actions")
        if isinstance(recent, list):
            for receipt in recent:
                if isinstance(receipt, dict) and isinstance(
                    receipt.get("observation_id"), str
                ):
                    receipt["observation_id"] = short_id(receipt["observation_id"])

        def criterion(candidate) -> dict:
            option = candidate_to_json(candidate)
            option["description"] = short_refs(candidate.description)
            if candidate.target_ref is not None:
                option["target_ref"] = short_refs(candidate.target_ref)
            return option

        body: dict[str, object] = {
            "model": self.model,
            "state": state,
            "questions": {
                question.id: {
                    "type": "choice",
                    "instructions": short_refs(question.instructions),
                    "criteria": {c.id: criterion(c) for c in question.candidates},
                }
                for question in request.questions
            },
        }
        if frame := context.frame:
            state["current_image"] = {
                "width": frame.width,
                "height": frame.height,
                "sha256": frame.sha256,
            }
        return body

    async def _evaluate(
        self, request: DecisionRequest, *, deadline: float
    ) -> DecisionResult:
        response = await self._send(request, deadline=deadline)
        check_response_status(response, vendor=self.PROVIDER_ID)
        try:
            return self._result(request, response)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ProviderTransientError(
                f"malformed {self.PROVIDER_ID} decision response"
            ) from None

    async def _send(
        self, request: DecisionRequest, *, deadline: float
    ) -> httpx.Response:
        encoded = json.dumps(
            self._body(request), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        if len(encoded) >= self._request_limit():
            raise ProviderRequestError(
                f"{self.PROVIDER_ID} decision request exceeds byte limit"
            )
        return await self._client.post(
            self.ENDPOINT,
            content=encoded,
            headers={"Content-Type": "application/json"},
            timeout=min(self._timeout, self._remaining(deadline)),
        )

    def _result(
        self, request: DecisionRequest, response: httpx.Response
    ) -> DecisionResult:
        payload = response.json(object_pairs_hook=unique_object)
        if not isinstance(payload, dict):
            raise ValueError("expected response object")
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("expected named answers")
        parsed = {}
        for name, answer in answers.items():
            if not isinstance(answer, dict) or answer.get("type") != "choice":
                raise ValueError("expected Choice answer")
            probabilities = answer.get("probabilities")
            if probabilities is not None:
                if not isinstance(probabilities, dict):
                    raise ValueError("expected probability object")
                if any(
                    type(value) not in (int, float) or not 0 <= value <= 1
                    for value in probabilities.values()
                ):
                    raise ValueError("expected finite probabilities in [0, 1]")
                total = math.fsum(probabilities.values())
                # Normalize rounded values within our 1% interoperability
                # limit, not a vendor precision guarantee. Confidence remains
                # provider-reported; no scores are invented when absent.
                if abs(total - 1.0) > 0.01 + 1e-12:
                    raise ValueError("probability total exceeds rounding tolerance")
                probabilities = {
                    key: value / total for key, value in probabilities.items()
                }
            parsed[name] = DecisionAnswer(
                selected_id=answer["choice"],
                confidence=answer.get("confidence"),
                probabilities=probabilities,
            )
        return DecisionResult(
            request_id=request.id,
            answers=parsed,
            provider_id=self.PROVIDER_ID,
            model_id=payload["model"],
            requested_model_id=self.model,
            usage=parse_usage(payload.get("usage")),
            provider_request_id=response.headers.get("x-request-id")
            or payload.get("id"),
        )
