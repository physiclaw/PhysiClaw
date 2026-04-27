"""Provider Protocol, errors, and the abstract base.

This file is the slim core: no wire-format, no HTTP request flow. Two
concrete bases sit on top of `BaseProvider`:
  - `OpenAICompatibleProvider` (in `openai_compat.py`) — vendors
    speaking the OpenAI `/chat/completions` shape (Qwen, OpenAI,
    Moonshot, Google).
  - `AnthropicCompatibleProvider` (in `anthropic_compat.py`) —
    vendors speaking Anthropic's `/v1/messages` shape (Anthropic).

Vendor classes declare `PROVIDER_ID`, `BASE_URL`, and (when the
default `<ID>_API_KEY` env-var convention doesn't fit) `API_KEY_ENV_VARS`.
`BASE_URL` is overridable per-instance via `~/.physiclaw/config.toml`'s
`[providers.<id>] base_url = "..."` (e.g. Moonshot's .cn vs .ai split,
or pointing at a proxy).

Principle 2: normalize at the boundary — providers return
`AssistantMessage` regardless of their wire shape.
Principle 3: preserve the real `finish_reason` — never derive it.
"""
import logging
import os
from typing import Protocol

import httpx

from physiclaw.agent.engine.dto import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolResultMessage,
)

log = logging.getLogger(__name__)


# ---------- errors / Protocol ----------


class ProviderError(Exception):
    """Base for provider failures."""


class ProviderTransientError(ProviderError):
    """Transport issue, timeout, 429, or 5xx — worth retrying."""


class ProviderPermanentError(ProviderError):
    """4xx (except 429) — retries will keep failing, fail fast."""


class Provider(Protocol):
    model: str
    COLLAPSE_FIRST_AT_TURN: int
    KEEP_RECENT_TURNS: int
    COLLAPSE_INTERVAL_TURNS: int

    async def chat(
        self,
        history: list[Message],
        tools: list[dict],
    ) -> AssistantMessage: ...

    def serialize_history(self, history: list[Message]) -> list[dict]:
        """Convert engine DTOs into the wire-format messages this provider
        will send, with cache-control markers applied. Engine calls this
        for trace logging; `chat()` calls the same internally before
        POSTing — the two calls produce equivalent fresh wire so the log
        faithfully captures what hits the API."""
        ...

    async def aclose(self) -> None: ...


# ---------- shared constants ----------


# 5-minute TTL prefix-cache marker. DashScope, Moonshot K2, and
# Anthropic all accept `{type: "ephemeral"}` in their respective
# surrounding shapes. Treat as immutable — never mutate.
EPHEMERAL_CACHE_CONTROL = {"type": "ephemeral"}


# ---------- BaseProvider ----------


class BaseProvider:
    """Auth resolution + HTTP-client construction.

    Subclasses (`OpenAICompatibleProvider`, `AnthropicCompatibleProvider`)
    layer wire-format and request/response flow on top.

    Subclass MUST set:
      - `PROVIDER_ID` — short id, also the registry key.
      - `BASE_URL` — endpoint, e.g. `"https://api.openai.com/v1"`.

    Subclass MAY set:
      - `API_KEY_ENV_VARS` — env vars to consult (in order, first hit
        wins). Defaults to `("<PROVIDER_ID>_API_KEY",)`. Override only
        when a vendor accepts more than one (e.g. Qwen takes both
        `QWEN_API_KEY` and `DASHSCOPE_API_KEY`).

    Subclass MAY override:
      - `_build_client()` if the wire client isn't a stock
        `httpx.AsyncClient` (e.g. Anthropic's `AsyncAnthropic` SDK)
      - `_api_key()` if auth doesn't fit the env-var/config pattern
      - `_missing_key_message()` for a richer error string
      - `_model_env_var()` if the env override isn't `<ID>_MODEL`
      - `system_prompt_fragment()` to inject a per-vendor reasoning
        wrapper into the system prompt (e.g. Qwen's `<think>...</think>`)
      - `chat()` and `serialize_history()` — provided by the wire-shape
        intermediate base; vendors normally don't touch them

    `BASE_URL` is overridable per-instance via `~/.physiclaw/config.toml`'s
    `[providers.<id>] base_url = "..."` so users can point at proxies
    or alt endpoints without code changes.
    """

    PROVIDER_ID: str = ""
    BASE_URL: str = ""
    API_KEY_ENV_VARS: tuple[str, ...] = ()

    # Turn-age summary collapse — see `compact.collapse_old_turns`.
    # All three knobs live here so vendor-specific tuning (cache
    # mechanics differ per provider) can override any of them in one
    # place. Current defaults match every shipping provider; Moonshot
    # carries the highest cost-per-collapse (whole-prefix invalidation
    # vs anchored caches on Anthropic/Qwen) but accepts the tax in
    # exchange for tighter prompts on long sessions.
    #
    #   F = COLLAPSE_FIRST_AT_TURN    first collapse threshold
    #   K = KEEP_RECENT_TURNS         recent turns kept per collapse
    #   I = COLLAPSE_INTERVAL_TURNS   subsequent collapse cadence
    COLLAPSE_FIRST_AT_TURN: int = 30
    KEEP_RECENT_TURNS: int = 10
    COLLAPSE_INTERVAL_TURNS: int = 20

    def __init__(
        self,
        model: str | None = None,
        timeout: float = 120.0,
        base_url: str | None = None,
    ):
        if not (self.PROVIDER_ID and self.BASE_URL):
            raise RuntimeError(
                f"{type(self).__name__}: PROVIDER_ID and BASE_URL must "
                "be set on the subclass"
            )
        key = self._api_key()
        if not key:
            raise RuntimeError(self._missing_key_message())
        # Model resolution: explicit arg → env override → empty (chat()
        # will surface a clear API error if invoked without a model).
        self.model = model or os.environ.get(self._model_env_var()) or ""
        self._client = self._build_client(key, timeout=timeout, base_url=base_url)

    # ---------- HTTP client ----------

    def _build_client(self, key: str, *, timeout: float, base_url: str | None):
        """Construct the underlying HTTP client. Default is a stock
        `httpx.AsyncClient` with Bearer auth (works for every OpenAI-
        compatible vendor). Override when a vendor uses an SDK or a
        non-Bearer auth scheme."""
        return httpx.AsyncClient(
            base_url=base_url or self._resolved_base_url(),
            timeout=timeout,
            headers={"Content-Type": "application/json", **self._auth_headers(key)},
        )

    @classmethod
    def _resolved_base_url(cls) -> str:
        """Class `BASE_URL` unless the user has set
        `[providers.<id>] base_url = "..."` in `~/.physiclaw/config.toml`."""
        from physiclaw.config import provider_base_url_override
        return provider_base_url_override(cls.PROVIDER_ID) or cls.BASE_URL

    def _auth_headers(self, key: str) -> dict[str, str]:
        """HTTP headers for authentication. Default is OpenAI's
        `Authorization: Bearer <key>`. Used by the default
        `_build_client`; vendors that override `_build_client` (e.g.
        Anthropic's SDK) typically don't need this hook."""
        return {"Authorization": f"Bearer {key}"}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- auth lookup ----------

    def _api_key(self) -> str | None:
        """Default lookup: env vars (in `API_KEY_ENV_VARS` order, defaulting
        to `<ID>_API_KEY` by convention) → config.toml `[provider]
        <PROVIDER_ID>_api_key`. Override for non-standard auth (OAuth,
        sigv4, etc.)."""
        from physiclaw.config import resolve_provider_key
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

    @classmethod
    def _model_env_var(cls) -> str:
        return f"{cls.PROVIDER_ID.upper()}_MODEL"

    @classmethod
    def system_prompt_fragment(cls) -> str:
        """Per-vendor system-prompt addendum (e.g. Qwen's `<think>...</think>`
        wrapper instructions). Default: empty. Override on the vendor
        class when the model needs an explicit reasoning convention."""
        return ""

    # ---------- DTO → wire (template method, shared across wire shapes) ----------

    def serialize_history(self, history: list[Message]) -> list[dict]:
        """Single-pass DTO history → provider wire-format messages, with
        cache markers attached to:
          - the `SystemMessage` at index 0 (via `_mark_system`), and
          - the latest `ToolResultMessage` flagged `is_superseded`
            (via `_mark_stub`).

        Subclasses implement `_encode_message` (DTO → wire dict, list of
        wire dicts, or `None` to skip). Most encodings are 1:1; the list
        form is for vendor-specific splits — e.g. Google's shim rejects
        `image_url` parts in `role: tool`, so `GoogleProvider` splits a
        ToolResultMessage with images into [text-only tool, synthetic
        user with images]. Anthropic's `SystemMessage` returns `None`
        (system rides outside the messages array). Cache markers attach
        to the first entry of any split; superseded results don't carry
        images, so their list is always single-element."""
        out: list[dict] = []
        last_stub_idx: int | None = None
        for i, msg in enumerate(history):
            entries = self._encode_message(msg)
            if entries is None:
                continue
            if isinstance(entries, dict):
                entries = [entries]
            if not entries:
                continue
            if i == 0 and isinstance(msg, SystemMessage):
                entries[0] = self._mark_system(entries[0])
            elif isinstance(msg, ToolResultMessage) and msg.is_superseded:
                last_stub_idx = len(out)
            out.extend(entries)
        if last_stub_idx is not None:
            out[last_stub_idx] = self._mark_stub(out[last_stub_idx])
        return out

    def _encode_message(self, msg: Message) -> dict | list[dict] | None:
        """Encode one DTO into a provider wire dict (or list of dicts
        for vendor splits, or `None` to skip). MUST be implemented by
        wire-shape subclasses."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement _encode_message"
        )

    def _mark_system(self, entry: dict) -> dict:
        """Attach `cache_control` to the system entry. Default: no-op
        (Anthropic marks system on the top-level `system` field, not on
        a messages-array entry). OpenAI-shape providers override to
        wrap the string content in a cache-controlled text block."""
        return entry

    def _mark_stub(self, entry: dict) -> dict:
        """Attach `cache_control` to a stubbed `tool_result` entry.
        Default: no-op. Override per wire shape — OpenAI wraps the
        whole entry's content; Anthropic annotates the inner
        `tool_result` block."""
        return entry

    # ---------- request flow: provided by wire-shape subclasses ----------

    async def chat(
        self,
        history: list[Message],
        tools: list[dict],
    ) -> AssistantMessage:
        raise NotImplementedError(
            f"{type(self).__name__} must inherit from a wire-shape base "
            "(OpenAICompatibleProvider or AnthropicCompatibleProvider)"
        )

    async def list_models(self) -> list[dict]:
        """Live model list from the provider's `/v1/models` endpoint.

        Each entry is a dict with at least an `id` field; vendors may
        include `display_name`, `created_at`, `owned_by`, etc. — caller
        normalizes for display. Implemented by the wire-shape bases;
        BaseProvider raises so a missing implementation surfaces clearly."""
        raise NotImplementedError(
            f"{type(self).__name__} must inherit from a wire-shape base"
        )
