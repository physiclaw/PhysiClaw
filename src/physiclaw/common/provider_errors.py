"""Shared provider failure vocabulary, independent of any transport or service.

Consumers can distinguish request rejection, temporary failure and unusable
provider setup without importing provider implementations.
"""


class ProviderError(Exception):
    """Base for provider failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class ProviderTransientError(ProviderError):
    """Temporary transport, timeout or service failure; later calls may succeed."""


class ProviderPermanentError(ProviderError):
    """Provider setup or service is unusable; changing requests cannot fix it."""


class ProviderRequestError(ProviderError):
    """This request is unsupported or refused; other requests may succeed."""


class ProviderConfigurationError(RuntimeError):
    """Local provider setup needs correction before the runtime can run."""
