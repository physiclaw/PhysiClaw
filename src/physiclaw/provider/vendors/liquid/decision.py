"""Liquid's hosted d1 Choice API. Images are separate from shared state.

Each question is billed for its text and images; keep reported usage intact.
No price estimates or automatic model substitutions belong in this adapter.
"""

from physiclaw.contract.decision import DecisionRequest
from physiclaw.provider.decision.systemone_compat import (
    LIQUID_MAX_REQUEST_BYTES,
    SystemOneDecisionProvider,
    check_liquid_image,
    image_data_url,
)


class LiquidDecisionProvider(SystemOneDecisionProvider):
    PROVIDER_ID = "liquid"
    BASE_URL = "https://api.liquid.ai/decisions/v1"
    MAX_REQUEST_BYTES = LIQUID_MAX_REQUEST_BYTES

    def _body(self, request: DecisionRequest) -> dict[str, object]:
        if frame := request.context.frame:
            check_liquid_image(frame)
        body = super()._body(request)
        if frame:
            body["images"] = [image_data_url(frame)]
        return body
