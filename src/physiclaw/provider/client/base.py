"""Shared credentials, endpoints and client lifecycle.

Inference interfaces live in physiclaw.contract. This base owns no
chat history, cache policy, decision context, or inference endpoint.
"""

import httpx

from physiclaw.provider.client.configuration import CredentialSpec, configured_base_url


class BaseAPIProvider:
    """Credentials, endpoint resolution and owned client; no inference API.

    Vendors may override auth, client creation or cleanup for an SDK. The
    common client honors external-endpoint proxy settings. Content-Type is
    capability-specific: multipart users must let httpx choose its boundary.
    Each factory call creates its own client and model identity.
    """

    PROVIDER_ID: str = ""
    BASE_URL: str = ""
    API_KEY_ENV_VARS: tuple[str, ...] = ()
    CONTENT_TYPE: str | None = None

    def __init__(
        self, model: str, *, timeout: float = 120.0, base_url: str | None = None
    ) -> None:
        if not (self.PROVIDER_ID and self.BASE_URL):
            raise RuntimeError(
                f"{type(self).__name__}: PROVIDER_ID and BASE_URL must "
                "be set on the subclass"
            )
        key = self.credentials().require_key(self.PROVIDER_ID)
        self.model = model
        self._timeout = timeout
        self._closed = False
        self._client = self._build_client(
            key, timeout=timeout, base_url=base_url or self._resolved_base_url()
        )

    @property
    def ref(self) -> str:
        return f"{self.PROVIDER_ID}/{self.model}"

    def _build_client(self, key: str, *, timeout: float, base_url: str | None):
        # Native SDKs can use their own default; the HTTP implementation needs
        # the endpoint resolved by construction before it creates a client.
        assert base_url is not None
        headers = self._auth_headers(key)
        if self.CONTENT_TYPE is not None:
            headers = {"Content-Type": self.CONTENT_TYPE, **headers}
        return httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers=headers,
            # External endpoints honor the system proxy, unlike localhost
            # MCP/control clients. Keep that distinction across capabilities.
            trust_env=True,
        )

    @classmethod
    def _resolved_base_url(cls) -> str | None:
        return configured_base_url(cls.PROVIDER_ID, cls.BASE_URL)

    def _auth_headers(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    async def aclose(self) -> None:
        if not self._closed:
            await self._close_client()
            self._closed = True

    async def _close_client(self) -> None:
        """SDK adapters override the close operation, not lifecycle state."""
        await self._client.aclose()

    @classmethod
    def credentials(cls) -> CredentialSpec:
        return CredentialSpec(
            cls.API_KEY_ENV_VARS or (f"{cls.PROVIDER_ID.upper()}_API_KEY",),
            f"{cls.PROVIDER_ID}_api_key",
        )
