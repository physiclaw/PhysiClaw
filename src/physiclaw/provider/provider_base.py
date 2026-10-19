"""Shared credentials, endpoints, client lifecycle, and provider errors.

Inference interfaces live under chat/ and decision/. This base owns no
chat history, cache policy, decision context, or inference endpoint.
"""

import httpx


class ProviderError(Exception):
    """Base for provider failures."""


class ProviderTransientError(ProviderError):
    """Transport issue, timeout, 429, or 5xx — worth retrying."""


class ProviderPermanentError(ProviderError):
    """4xx (except 429) — retries will keep failing, fail fast."""


class ProviderConfigurationError(RuntimeError):
    """Local provider setup needs correction before the runtime can run."""


def describe(e: BaseException) -> str:
    """`ClassName: message`, or just `ClassName` when the message is
    empty — httpx timeouts commonly str() to '', which made wrapped
    errors log as the famously unhelpful `transport: `."""
    msg = str(e)
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


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
        key = self._api_key()
        if not key:
            raise ProviderConfigurationError(self._missing_key_message())
        self.model = model
        self._timeout = timeout
        self._closed = False
        self._client = self._build_client(key, timeout=timeout, base_url=base_url)

    @property
    def ref(self) -> str:
        return f"{self.PROVIDER_ID}/{self.model}"

    def _build_client(self, key: str, *, timeout: float, base_url: str | None):
        headers = self._auth_headers(key)
        if self.CONTENT_TYPE is not None:
            headers = {"Content-Type": self.CONTENT_TYPE, **headers}
        return httpx.AsyncClient(
            base_url=base_url or self._resolved_base_url(),
            timeout=timeout,
            headers=headers,
            # External endpoints honor the system proxy, unlike localhost
            # MCP/control clients. Keep that distinction across capabilities.
            trust_env=True,
        )

    @classmethod
    def _resolved_base_url(cls) -> str:
        from physiclaw.common.config import provider_base_url_override

        return provider_base_url_override(cls.PROVIDER_ID) or cls.BASE_URL

    def _auth_headers(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    async def aclose(self) -> None:
        if not self._closed:
            await self._close_client()
            self._closed = True

    async def _close_client(self) -> None:
        """SDK adapters override the close operation, not lifecycle state."""
        await self._client.aclose()

    def _api_key(self) -> str | None:
        from physiclaw.common.config import resolve_provider_key

        return resolve_provider_key(self._env_vars(), self._config_key())[0]

    def _missing_key_message(self) -> str:
        envs = " / ".join(self._env_vars())
        return (
            f"{self.PROVIDER_ID} credential not found. Set {envs} env var "
            f"or [provider] {self._config_key()} in ~/.physiclaw/config.toml."
        )

    @classmethod
    def _env_vars(cls) -> tuple[str, ...]:
        return cls.API_KEY_ENV_VARS or (f"{cls.PROVIDER_ID.upper()}_API_KEY",)

    @classmethod
    def _config_key(cls) -> str:
        return f"{cls.PROVIDER_ID}_api_key"
