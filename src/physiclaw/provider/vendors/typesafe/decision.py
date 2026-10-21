"""TypeSafe's native bounded-choice API (including experimental images).

The model is explicit and passed through unchanged. No tool execution,
acceptance thresholds, price assumptions, chat state or retry policy live
here. Errors deliberately omit server bodies, headers and transport text.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx

from physiclaw.common.provider_errors import (
    ProviderConfigurationError,
    ProviderRequestError,
)
from physiclaw.contract.decision import (
    DecisionCapabilities,
    DecisionRequest,
)
from physiclaw.provider.decision.systemone_compat import SystemOneDecisionProvider

_IMAGE_PART = "current-frame"


class TypeSafeDecisionProvider(SystemOneDecisionProvider):
    PROVIDER_ID = "typesafe"
    BASE_URL = "https://api.typesafe.ai/v1"

    @staticmethod
    def capabilities_for(model: str) -> DecisionCapabilities:
        if not isinstance(model, str) or not model.strip():
            raise ProviderConfigurationError("a decision model ID is required")
        return SystemOneDecisionProvider.capabilities_for(model)

    def _body(self, request: DecisionRequest) -> dict[str, object]:
        body = super()._body(request)
        if request.context.frame:
            # TypeSafe substitutes asset keys wherever they occur. Keep this
            # placeholder absent from all evidence, hashes and refs.
            wire_text = json.dumps(body, ensure_ascii=False, allow_nan=False)
            image_ref = uuid4().hex[:7]
            while image_ref in wire_text:
                image_ref = uuid4().hex[:7]
            body["assets"] = {image_ref: {"type": "image", "part": _IMAGE_PART}}
            state = body["state"]
            assert isinstance(state, dict)
            state["current_image"]["image"] = image_ref
        return body

    async def _send(
        self, request: DecisionRequest, *, deadline: float
    ) -> httpx.Response:
        frame = request.context.frame
        if frame is not None:
            if not (280 <= frame.width <= 1568 and 280 <= frame.height <= 1568):
                raise ProviderRequestError(
                    "TypeSafe image dimensions must be within [280, 1568]"
                )
            if len(frame.data) > 16 * 1024 * 1024:
                raise ProviderRequestError("TypeSafe image exceeds 16 MiB")
        encoded = json.dumps(
            self._body(request), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        if (
            len(encoded) + (len(frame.data) if frame else 0)
            > self.MAX_REQUEST_BYTES - 4096
        ):
            raise ProviderRequestError(
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
