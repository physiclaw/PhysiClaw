"""Shared JSON HTTP flow; each chat adapter owns its wire protocol."""

import logging
from collections.abc import Callable

import httpx

from physiclaw.common.provider_errors import (
    ProviderPermanentError,
    ProviderTransientError,
)
from physiclaw.contract.dto import AssistantMessage, Message, Thinking
from physiclaw.contract.structured import OutputSpec
from physiclaw.contract.turn import TurnMetadata
from physiclaw.provider.chat.base import BaseChatProvider
from physiclaw.provider.chat.retry import retry_after_seconds
from physiclaw.provider.provider_base import describe, quota_exhausted

log = logging.getLogger(__name__)


class HTTPChatProvider(BaseChatProvider):
    ENDPOINT: str

    def _retry_after(self, response: httpx.Response) -> float | None:
        return retry_after_seconds(response.headers.get("retry-after"))

    async def _chat(
        self,
        history: list[Message],
        tools: list[dict],
        *,
        metadata: TurnMetadata | None = None,
        thinking: Thinking | None = None,
        output_spec: OutputSpec | None = None,
        on_request: Callable[[dict], None] | None = None,
    ) -> AssistantMessage:
        payload = self.build_request(
            history,
            tools,
            metadata=metadata,
            thinking=thinking,
            output_spec=output_spec,
        )
        self._notify_request(payload, on_request)

        try:
            r = await self._client.post(self.ENDPOINT, json=payload)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            raise ProviderTransientError(f"transport: {describe(e)}") from e

        if r.status_code == 429 and quota_exhausted(r):
            raise ProviderPermanentError(
                f"HTTP 429: {r.text[:500]}", status_code=r.status_code
            )
        if r.status_code in (408, 409, 429) or r.status_code >= 500:
            log.debug(
                "HTTP %s (transient): %s",
                r.status_code,
                r.text[:200],
                extra={"event": "provider.http_failed", "http_status": r.status_code},
            )
            raise ProviderTransientError(
                f"HTTP {r.status_code}: {r.text[:200]}",
                status_code=r.status_code,
                retry_after=self._retry_after(r),
            )
        if r.status_code >= 400:
            log.debug(
                "HTTP %s (permanent): %s",
                r.status_code,
                r.text[:500],
                extra={"event": "provider.http_failed", "http_status": r.status_code},
            )
            raise ProviderPermanentError(
                f"HTTP {r.status_code}: {r.text[:500]}", status_code=r.status_code
            )

        try:
            raw = r.json()
        except ValueError as e:
            # A 200 with a non-JSON body (gateway/proxy page mid-restart)
            # is a retryable provider hiccup, not an engine crash.
            log.debug(
                "HTTP 200 with non-JSON body: %s",
                r.text[:200],
                extra={
                    "event": "provider.invalid_response",
                    "http_status": r.status_code,
                },
            )
            raise ProviderTransientError(f"non-JSON response: {r.text[:200]}") from e
        return self._decode_response(raw, structured=output_spec is not None)

    def _decode_response(self, raw: object, *, structured: bool) -> AssistantMessage:
        raise NotImplementedError

    async def list_models(self) -> list[dict]:
        """OpenAI-compatible providers all expose `GET /models`. Returns
        the `data` array verbatim — typically a list of dicts with `id`,
        `object`, `created`, `owned_by`."""
        try:
            r = await self._client.get("/models")
        except (httpx.TransportError, httpx.TimeoutException) as e:
            raise ProviderTransientError(f"transport: {describe(e)}") from e
        if r.status_code == 429 and quota_exhausted(r):
            raise ProviderPermanentError(f"HTTP 429: {r.text[:300]}", status_code=429)
        if r.status_code == 429 or r.status_code >= 500:
            raise ProviderTransientError(f"HTTP {r.status_code}: {r.text[:300]}")
        if r.status_code >= 400:
            raise ProviderPermanentError(f"HTTP {r.status_code}: {r.text[:300]}")
        try:
            body = r.json()
        except ValueError as e:
            raise ProviderTransientError(f"non-JSON response: {r.text[:200]}") from e
        if not isinstance(body, dict):
            raise ProviderTransientError("invalid model list response: expected object")
        models = body.get("data")
        if models is None:
            return []
        if not isinstance(models, list) or any(
            not isinstance(m, dict)
            or not isinstance(m.get("id"), str)
            or not m["id"].strip()
            for m in models
        ):
            raise ProviderTransientError(
                "invalid model list response: expected model ids"
            )
        return models
