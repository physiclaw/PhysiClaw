"""Provider configuration resolution; no clients or inference behavior.

Factories resolve application defaults. Adapter construction uses these
helpers for credentials and endpoint overrides, including direct SDK use.
Metadata queries never require credentials or construct a transport.
"""

import os
from dataclasses import dataclass

from physiclaw.common.provider_errors import ProviderConfigurationError


@dataclass(frozen=True)
class CredentialSpec:
    env_vars: tuple[str, ...]
    config_key: str

    def resolve(self) -> tuple[str | None, str | None]:
        from physiclaw.common.config import resolve_provider_key

        return resolve_provider_key(self.env_vars, self.config_key)

    def missing_message(self, provider_id: str) -> str:
        envs = " / ".join(self.env_vars)
        return (
            f"{provider_id} credential not found. Set {envs} env var "
            f"or [provider] {self.config_key} in ~/.physiclaw/config.toml."
        )

    def require_key(self, provider_id: str) -> str:
        key, _ = self.resolve()
        if not key:
            raise ProviderConfigurationError(self.missing_message(provider_id))
        return key


def configured_base_url(provider_id: str, default: str | None) -> str | None:
    """An SDK may supply its own default; explicit instance URLs win upstream."""
    from physiclaw.common.config import provider_base_url_override

    return provider_base_url_override(provider_id) or default


def resolve_chat_model(provider_id: str, model: str | None) -> str:
    """Preserve direct-adapter model defaults; factories supply explicit models."""
    return model or os.environ.get(f"{provider_id.upper()}_MODEL") or ""
