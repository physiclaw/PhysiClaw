"""TypeSafe's native bounded-choice API (including experimental images).

The model is explicit and passed through unchanged. No tool execution,
acceptance thresholds, price assumptions, chat state or retry policy live
here. Errors deliberately omit server bodies, headers and transport text.
"""

from __future__ import annotations

import json
from hashlib import sha256
from uuid import uuid4

import httpx

from physiclaw.contract.decision import (
    DecisionAnswer,
    DecisionCapabilities,
    DecisionRequest,
    DecisionResult,
    candidate_to_json,
    context_to_json,
)
from physiclaw.provider.decision.decision_compat import BaseDecisionProvider
from physiclaw.provider.decision.wire import (
    check_response_status,
    parse_usage,
    unique_object,
)
from physiclaw.provider.provider_base import (
    ProviderConfigurationError,
    ProviderPermanentError,
    ProviderTransientError,
)

_IMAGE_PART = "current-frame"
# Image support is experimental. Extend this set only with verified vendor
# support; an arbitrary new model ID must not silently inherit that modality.
_IMAGE_MODELS = frozenset({"jev-1.14.0"})


class TypeSafeDecisionProvider(BaseDecisionProvider):
    PROVIDER_ID = "typesafe"
    BASE_URL = "https://api.typesafe.ai/v1"

    @staticmethod
    def capabilities_for(model: str) -> DecisionCapabilities:
        if not isinstance(model, str) or not model.strip():
            raise ProviderConfigurationError("a decision model ID is required")
        # Native Choice endpoint capabilities, not a claim of calibrated
        # accuracy for an arbitrary model ID. Unsupported IDs fail at API use.
        return DecisionCapabilities(
            images=model in _IMAGE_MODELS,
            probabilities=True,
            confidence=True,
            max_choices=255,
        )

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
            # TypeSafe substitutes asset keys wherever they occur. Keep this
            # short placeholder absent from all evidence, hashes and refs.
            wire_text = json.dumps(body, ensure_ascii=False, allow_nan=False)
            image_ref = uuid4().hex[:7]
            while image_ref in wire_text:
                image_ref = uuid4().hex[:7]
            body["assets"] = {image_ref: {"type": "image", "part": _IMAGE_PART}}
            state["current_image"]["image"] = image_ref
        return body

    async def _evaluate(
        self, request: DecisionRequest, *, deadline: float
    ) -> DecisionResult:
        response = await self._send(request, deadline=deadline)
        check_response_status(response, vendor="TypeSafe")
        try:
            return self._result(request, response)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ProviderTransientError(
                "malformed TypeSafe decision response"
            ) from None

    async def _send(
        self, request: DecisionRequest, *, deadline: float
    ) -> httpx.Response:
        frame = request.context.frame
        if frame is not None:
            if not (280 <= frame.width <= 1568 and 280 <= frame.height <= 1568):
                raise ProviderPermanentError(
                    "TypeSafe image dimensions must be within [280, 1568]"
                )
            if len(frame.data) > 16 * 1024 * 1024:
                raise ProviderPermanentError("TypeSafe image exceeds 16 MiB")
        encoded = json.dumps(
            self._body(request), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        if len(encoded) + (len(frame.data) if frame else 0) > 32 * 1024 * 1024 - 4096:
            raise ProviderPermanentError(
                "TypeSafe request exceeds 32 MiB with multipart overhead"
            )
        timeout = min(self._timeout, self._remaining(deadline))
        if frame is None:
            return await self._client.post(
                "systemone",
                content=encoded,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
        suffix = "jpg" if frame.media_type == "image/jpeg" else "png"
        return await self._client.post(
            "systemone",
            headers={"TypeSafe-Danger-Zone-Images": "true"},
            files=[
                ("request", (None, encoded, "application/json")),
                (_IMAGE_PART, (f"frame.{suffix}", frame.data, frame.media_type)),
            ],
            timeout=timeout,
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
            if probabilities is not None and not isinstance(probabilities, dict):
                raise ValueError("expected probability object")
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
            provider_request_id=response.headers.get("x-request-id"),
        )
