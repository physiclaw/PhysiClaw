"""OpenAI's native /v1/decisions service, independent of its chat adapter.

This adapter implements bounded Choice only. It never calls a generation
endpoint or falls back to chat. Refusals and failures return control through
provider errors; the host decides what to do next.
"""

import base64
import json

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


class OpenAIDecisionProvider(BaseDecisionProvider):
    PROVIDER_ID = "openai"
    BASE_URL = "https://api.openai.com/v1"

    @staticmethod
    def capabilities_for(model: str) -> DecisionCapabilities:
        # Native Decisions currently supports only this model. Keep this
        # explicit rather than accepting a chat model and changing endpoints.
        if model != "gpt-6-luna":
            raise ProviderConfigurationError("unsupported OpenAI decision model")
        return DecisionCapabilities(
            images=True,
            probabilities=True,
            confidence=True,
            max_choices=255,
            max_questions=200,
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
        parts = [
            {
                "type": "input_text",
                "text": json.dumps(state, ensure_ascii=False, allow_nan=False),
            }
        ]
        if frame is not None:
            encoded = base64.b64encode(frame.data).decode("ascii")
            parts.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{frame.media_type};base64,{encoded}",
                }
            )
        return {
            "model": self.model,
            "input": [{"role": "user", "content": parts}],
            "questions": [
                {
                    "type": "choice",
                    "name": question.id,
                    "instructions": question.instructions,
                    "choices": [
                        {
                            "value": c.id,
                            "description": json.dumps(
                                candidate_to_json(c),
                                ensure_ascii=False,
                                allow_nan=False,
                            ),
                        }
                        for c in question.candidates
                    ],
                }
                for question in request.questions
            ],
        }

    async def _evaluate(
        self, request: DecisionRequest, *, deadline: float
    ) -> DecisionResult:
        encoded = json.dumps(
            self._body(request), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        response = await self._client.post(
            "decisions",
            content=encoded,
            headers={"Content-Type": "application/json"},
            timeout=min(self._timeout, self._remaining(deadline)),
        )
        check_response_status(response, vendor="OpenAI")
        try:
            return self._result(request, response)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ProviderTransientError("malformed OpenAI decision response") from None

    def _result(
        self, request: DecisionRequest, response: httpx.Response
    ) -> DecisionResult:
        payload = response.json(object_pairs_hook=unique_object)
        if not isinstance(payload, dict):
            raise ValueError("expected response object")
        answers = payload.get("answers")
        if not isinstance(answers, list):
            raise ValueError("expected answer array")
        parsed = {}
        for answer in answers:
            if not isinstance(answer, dict) or not isinstance(answer.get("name"), str):
                raise ValueError("expected named answer")
            name = answer["name"]
            if name in parsed:
                raise ValueError("duplicate question answer")
            if answer.get("type") == "refusal":
                raise ProviderPermanentError("OpenAI decision refused")
            if answer.get("type") != "choice":
                raise ValueError("expected Choice answer")
            raw = answer.get("probabilities")
            probabilities: dict[str, float] | None = None
            if raw is not None:
                if not isinstance(raw, list):
                    raise ValueError("expected probability array")
                probabilities = {}
                for item in raw:
                    if not isinstance(item, dict) or not isinstance(
                        item.get("value"), str
                    ):
                        raise ValueError("expected string option value")
                    key = item["value"]
                    if key in probabilities:
                        raise ValueError("duplicate probability option")
                    probabilities[key] = item["probability"]
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
