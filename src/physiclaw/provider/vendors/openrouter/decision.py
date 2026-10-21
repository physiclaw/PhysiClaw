"""OpenRouter's Decisions service, separate from chat completions.

Model IDs pass through to the API. Images must be actual state parts:
TypeSafe assets and Liquid's top-level images are not accepted here.
"""

import json

from physiclaw.common.provider_errors import ProviderRequestError
from physiclaw.contract.decision import DecisionRequest
from physiclaw.provider.decision.systemone_compat import (
    LIQUID_MAX_REQUEST_BYTES,
    SystemOneDecisionProvider,
    check_liquid_image,
    image_data_url,
)


class OpenRouterDecisionProvider(SystemOneDecisionProvider):
    PROVIDER_ID = "openrouter"
    BASE_URL = "https://openrouter.ai/api/alpha"
    ENDPOINT = "decisions"

    def _request_limit(self) -> int:
        return (
            LIQUID_MAX_REQUEST_BYTES
            if self.model.startswith("liquid/")
            else self.MAX_REQUEST_BYTES
        )

    def _body(self, request: DecisionRequest) -> dict[str, object]:
        if frame := request.context.frame:
            if self.model.startswith("liquid/"):
                check_liquid_image(frame)
            elif self.model.startswith("typesafe/") and not (
                256 <= frame.width <= 1568
                and 256 <= frame.height <= 1568
                and 78_400 <= frame.width * frame.height <= 2_458_624
                and len(frame.data) <= 16 * 1024 * 1024
            ):
                raise ProviderRequestError(
                    "OpenRouter Jev image exceeds dimension or size limits"
                )
        body = super()._body(request)
        if frame:
            body["state"] = [
                json.dumps(body["state"], ensure_ascii=False, allow_nan=False),
                {"type": "image_url", "image_url": {"url": image_data_url(frame)}},
            ]
        return body
