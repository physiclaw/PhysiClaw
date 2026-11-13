"""Native Gemini Decisions API, separate from Google's chat compatibility API.

Bounded Choice over text and one observation image; no generation or SDK is
needed. Wire format: https://aistudio.google.com/docs/decisions-api
"""

import base64
import json

import httpx

from physiclaw.common.provider_errors import ProviderTransientError
from physiclaw.contract.decision import (
    DecisionAnswer,
    DecisionCapabilities,
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


class GoogleDecisionProvider(BaseDecisionProvider):
    PROVIDER_ID = "google"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def _auth_headers(self, key: str) -> dict[str, str]:
        return {"x-goog-api-key": key}

    @staticmethod
    def capabilities_for(model: str) -> DecisionCapabilities:
        return DecisionCapabilities(
            images=True,
            probabilities=True,
            confidence=True,
            max_choices=255,
            max_questions=1000,
        )

    def _body(self, request: DecisionRequest) -> dict[str, object]:
        context = request.context
        state = context_to_json(context)
        frame = context.frame
        if frame is not None:
            state["current_image"] = {
                "id": frame.id,
                "width": frame.width,
                "height": frame.height,
                "sha256": frame.sha256,
            }
        parts: list[dict[str, object]] = [
            {
                "type": "text",
                "text": json.dumps(state, ensure_ascii=False, allow_nan=False),
            }
        ]
        if frame is not None:
            parts.append(
                {
                    "type": "image",
                    "data": base64.b64encode(frame.data).decode("ascii"),
                    "mime_type": frame.media_type,
                }
            )
        return {
            "model": self.model,
            "input": parts,
            "questions": {
                question.id: {
                    "type": "choice",
                    "instructions": question.instructions,
                    "criteria": {
                        c.id: json.dumps(
                            candidate_to_json(c), ensure_ascii=False, allow_nan=False
                        )
                        for c in question.candidates
                    },
                }
                for question in request.questions
            },
        }

    async def _evaluate(
        self, request: DecisionRequest, *, deadline: float
    ) -> DecisionResult:
        response = await self._client.post(
            "decisions",
            json=self._body(request),
            timeout=min(self._timeout, self._remaining(deadline)),
        )
        check_response_status(response, vendor="Google")
        try:
            return self._result(request, response)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ProviderTransientError("malformed Google decision response") from None

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
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        return DecisionResult(
            request_id=request.id,
            answers=parsed,
            provider_id=self.PROVIDER_ID,
            # Google currently omits model identity in the response. Attribute
            # it to the explicit request, preserving a reported version if added.
            model_id=payload.get("model", self.model),
            requested_model_id=self.model,
            usage=parse_usage(
                {
                    "input_tokens": usage.get("total_input_tokens"),
                    "output_tokens": usage.get("total_output_tokens"),
                }
            ),
            provider_request_id=response.headers.get("x-request-id"),
        )
