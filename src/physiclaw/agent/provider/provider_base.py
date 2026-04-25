"""Provider Protocol, model catalog, errors, and the abstract base.

This file is the slim core: no wire-format, no HTTP request flow. Two
concrete bases sit on top of `BaseProvider`:
  - `OpenAICompatibleProvider` (in `openai_compat.py`) — vendors
    speaking the OpenAI `/chat/completions` shape (Qwen, OpenAI,
    Moonshot, Google).
  - `AnthropicCompatibleProvider` (in `anthropic_compat.py`) —
    vendors speaking Anthropic's `/v1/messages` shape (Anthropic).

A new vendor declares `PROVIDER_ID` / `BASE_URL` / `MODELS` /
`API_KEY_ENV_VARS` / `THINKING_FORMAT` on a subclass of one of those
two; this base file owns the shared infra (auth lookup, HTTP client
construction, model-catalog helpers, system-prompt fragments).

Principle 2: normalize at the boundary — providers return
`AssistantMessage` regardless of their wire shape.
Principle 3: preserve the real `finish_reason` — never derive it.
"""
import logging
import os
from dataclasses import dataclass
from typing import Protocol

import httpx

from physiclaw.agent.engine.dto import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolResultMessage,
)

log = logging.getLogger(__name__)


# ---------- catalog / errors / Protocol ----------


@dataclass(frozen=True)
class ModelEntry:
    """One entry in a provider's model catalog.

    `id` is the wire-level model field. `context_window` is the vendor-spec
    limit, useful later for compaction-threshold tuning.

    **PhysiClaw requires both `vision` and `reasoning`.** Every `peek`
    ships a camera frame (vision); the agent loop's quality depends on
    thinking before tool_calls (reasoning). `make_provider` rejects any
    entry missing either flag.

    Both default True because most modern frontier models satisfy both —
    flag the exceptions explicitly with `vision=False` / `reasoning=False`
    so a future maintainer can see at a glance why an entry is unusable.
    """
    id: str
    context_window: int | None = None
    vision: bool = True
    reasoning: bool = True


class ProviderError(Exception):
    """Base for provider failures."""


class ProviderTransientError(ProviderError):
    """Transport issue, timeout, 429, or 5xx — worth retrying."""


class ProviderPermanentError(ProviderError):
    """4xx (except 429) — retries will keep failing, fail fast."""


class Provider(Protocol):
    model: str

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


# 5-minute TTL prefix-cache marker. DashScope + Anthropic both accept
# `{type: "ephemeral"}` (in their respective surrounding shapes). Treat
# as immutable — never mutate the shared dict.
EPHEMERAL_CACHE_CONTROL = {"type": "ephemeral"}


# Reasoning-format snippets shared across providers. Add a new entry when
# a new provider declares a `THINKING_FORMAT` not in this map.
_THINKING_FRAGMENTS: dict[str, str] = {
    "qwen": (
        "Wrap internal reasoning in `<think>...</think>`. Anything outside "
        "`<think>` is interpreted as either a tool call or a user-visible reply.\n"
        "Never put reasoning inside tool arguments — handlers receive `args` "
        "raw, not your scratchpad."
    ),
}


# ---------- BaseProvider ----------


class BaseProvider:
    """Catalog declarations + auth resolution + HTTP-client construction.

    Subclasses (`OpenAICompatibleProvider`, `AnthropicCompatibleProvider`)
    layer wire-format and request/response flow on top. Vendors
    (`QwenProvider`, `AnthropicProvider`, …) inherit from one of those
    two and only declare class attrs.

    Subclass MUST set:
      - `PROVIDER_ID` — short id, also the `make_provider` key
      - `BASE_URL` — endpoint, e.g. `"https://api.openai.com/v1"`
      - `MODELS` — tuple of `ModelEntry`; `MODELS[0]` is the implicit
        default when caller doesn't pass a model

    Subclass MAY set:
      - `API_KEY_ENV_VARS` — env vars to check (in order, first hit
        wins). Defaults to `("<PROVIDER_ID>_API_KEY",)` — only declare
        explicitly when the convention doesn't fit (e.g. Qwen accepts
        both `QWEN_API_KEY` and `DASHSCOPE_API_KEY`).
      - `THINKING_FORMAT` — name of a fragment in `_THINKING_FRAGMENTS`
        to inject into the system prompt (e.g. `"qwen"`); `None` = no
        fragment

    Subclass MAY override:
      - `_build_client()` if the wire client isn't a stock
        `httpx.AsyncClient` (e.g. Anthropic's `AsyncAnthropic` SDK)
      - `_api_key()` if auth doesn't fit the env-var/config pattern
      - `_missing_key_message()` for a richer error string
      - `_model_env_var()` if the env override isn't `<ID>_MODEL`
      - `chat()` and `serialize_history()` — provided by the wire-shape
        intermediate base; vendors normally don't touch them
    """

    PROVIDER_ID: str = ""
    BASE_URL: str = ""
    MODELS: tuple[ModelEntry, ...] = ()
    API_KEY_ENV_VARS: tuple[str, ...] = ()
    THINKING_FORMAT: str | None = None

    def __init__(
        self,
        model: str | None = None,
        timeout: float = 120.0,
        base_url: str | None = None,
    ):
        if not (self.PROVIDER_ID and self.BASE_URL and self.MODELS):
            raise RuntimeError(
                f"{type(self).__name__}: PROVIDER_ID / BASE_URL / MODELS "
                "must all be set on the subclass"
            )
        key = self._api_key()
        if not key:
            raise RuntimeError(self._missing_key_message())
        # Resolution: explicit arg → env override → first catalog entry.
        # Validation against MODELS lives in `make_provider` (config layer);
        # direct instantiation accepts any string for tests / scripts.
        self.model = (
            model
            or os.environ.get(self._model_env_var())
            or self.MODELS[0].id
        )
        self._client = self._build_client(key, timeout=timeout, base_url=base_url)

    # ---------- HTTP client ----------

    def _build_client(self, key: str, *, timeout: float, base_url: str | None):
        """Construct the underlying HTTP client. Default is a stock
        `httpx.AsyncClient` with Bearer auth (works for every OpenAI-
        compatible vendor). Override when a vendor uses an SDK or a
        non-Bearer auth scheme."""
        return httpx.AsyncClient(
            base_url=base_url or self.BASE_URL,
            timeout=timeout,
            headers={"Content-Type": "application/json", **self._auth_headers(key)},
        )

    def _auth_headers(self, key: str) -> dict[str, str]:
        """HTTP headers for authentication. Default is OpenAI's
        `Authorization: Bearer <key>`. Used by the default
        `_build_client`; vendors that override `_build_client` (e.g.
        Anthropic's SDK) typically don't need this hook."""
        return {"Authorization": f"Bearer {key}"}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- catalog helpers ----------

    @classmethod
    def has_model(cls, model_id: str) -> bool:
        return any(m.id == model_id for m in cls.MODELS)

    @classmethod
    def find_model(cls, model_id: str) -> ModelEntry | None:
        return next((m for m in cls.MODELS if m.id == model_id), None)

    @classmethod
    def default_model(cls) -> ModelEntry:
        if not cls.MODELS:
            raise RuntimeError(f"{cls.__name__} has no models in catalog")
        return cls.MODELS[0]

    @classmethod
    def system_prompt_fragment(cls) -> str:
        """Provider-specific system-prompt addition (reasoning wrapper, etc.).
        Empty string if no fragment applies."""
        return _THINKING_FRAGMENTS.get(cls.THINKING_FORMAT or "", "")

    # ---------- auth lookup ----------

    def _api_key(self) -> str | None:
        """Default lookup: env vars (in `API_KEY_ENV_VARS` order, defaulting
        to `<ID>_API_KEY` by convention) → config.toml
        `[provider] <PROVIDER_ID>_api_key`. Override for non-standard
        auth (OAuth, sigv4, etc.)."""
        from physiclaw.config import resolve_provider_key
        return resolve_provider_key(self._env_vars(), self._config_key())[0]

    def _missing_key_message(self) -> str:
        envs = " / ".join(self._env_vars())
        return (
            f"{self.PROVIDER_ID} credential not found. Set {envs} env var "
            f"or [provider] {self._config_key()} in ~/.physiclaw/config.toml."
        )

    def _env_vars(self) -> tuple[str, ...]:
        """Env vars to consult, defaulting to the `<ID>_API_KEY` convention
        when the subclass doesn't override `API_KEY_ENV_VARS`."""
        return self.API_KEY_ENV_VARS or (f"{self.PROVIDER_ID.upper()}_API_KEY",)

    def _config_key(self) -> str:
        return f"{self.PROVIDER_ID}_api_key"

    def _model_env_var(self) -> str:
        return f"{self.PROVIDER_ID.upper()}_MODEL"

    # ---------- DTO → wire (template method, shared across wire shapes) ----------

    def serialize_history(self, history: list[Message]) -> list[dict]:
        """Single-pass DTO history → provider wire-format messages, with
        cache markers attached to:
          - the `SystemMessage` at index 0 (via `_mark_system`), and
          - the latest `ToolResultMessage` flagged `is_superseded`
            (via `_mark_stub`).

        Subclasses implement `_encode_message` (DTO → wire dict, or
        `None` to skip — e.g. Anthropic's `SystemMessage` rides outside
        the messages array, not as an entry). The marker hooks default
        to no-ops; wire-shape subclasses override them to attach
        provider-specific `cache_control`. The base body stays small
        because the algorithm is wire-shape-agnostic."""
        out: list[dict] = []
        last_stub_idx: int | None = None
        for i, msg in enumerate(history):
            entry = self._encode_message(msg)
            if entry is None:
                continue
            if i == 0 and isinstance(msg, SystemMessage):
                entry = self._mark_system(entry)
            elif isinstance(msg, ToolResultMessage) and msg.is_superseded:
                last_stub_idx = len(out)
            out.append(entry)
        if last_stub_idx is not None:
            out[last_stub_idx] = self._mark_stub(out[last_stub_idx])
        return out

    def _encode_message(self, msg: Message) -> dict | None:
        """Encode one DTO into a provider wire dict, or return `None`
        to skip (e.g. when the DTO rides outside the messages array).
        MUST be implemented by wire-shape subclasses."""
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
