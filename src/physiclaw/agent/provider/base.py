"""Provider Protocol, model catalog, errors, and OpenAI-compatible base.

Principle 2: normalize at the boundary.
  - Request: build wire-format from standard chat messages + tool schemas.
  - Response: parse into `AssistantMessage` (with `ToolCall` list and real
    `finish_reason`). Strip provider-specific fields like Qwen's
    `reasoning_content` before returning — they MUST NOT leak into engine
    history, or re-serializing will break the prefix cache or confuse the
    next turn's model.

Principle 3: preserve the real `finish_reason`. Do not derive it from
content. The engine routes differently on "length" / "content_filter" /
"tool_calls" / "stop".

Each concrete provider declares (declarations only — no methods needed
in the typical case):
  - `PROVIDER_ID`, `BASE_URL` — what URL to talk to
  - `MODELS` — tuple of `ModelEntry`; first entry is the implicit default
  - `API_KEY_ENV_VARS` — env vars to check, in order (first hit wins);
    config.toml `[provider] <PROVIDER_ID>_api_key` is the fallback
  - `THINKING_FORMAT` — flag consumed by `prompt.py` to render the right
    system-prompt fragment (replaces ad-hoc `if "qwen" in name` checks)

Adding a provider: drop a file in `agent/provider/vendors/` subclassing
`OpenAICompatibleProvider`, set the five class attrs above, register in
`__init__.py`. The default `_api_key()` and `_missing_key_message()`
read from the env-vars / config-key declarations — no methods need
overriding for vanilla OpenAI-compatible vendors.
"""
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from physiclaw.agent.engine.dto import AssistantMessage, FinishReason, ToolCall
from physiclaw.agent.provider.wire import tool_to_wire

log = logging.getLogger(__name__)


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
        messages: list[dict],
        tools: list[dict],
    ) -> AssistantMessage: ...

    async def aclose(self) -> None: ...


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


class OpenAICompatibleProvider:
    """Base for providers that speak the OpenAI `/chat/completions` wire
    format. Concrete provider files (qwen.py, moonshot.py, openai.py)
    just declare endpoint + catalog + auth; this base owns the
    request/response flow.

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
      - `chat()` for provider-specific request/response quirks
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

    def _build_client(self, key: str, *, timeout: float, base_url: str | None):
        """Construct the underlying HTTP client. Default is an
        OpenAI-compatible `httpx.AsyncClient` with Bearer auth. Override
        when a vendor uses an SDK or non-Bearer auth (e.g. Anthropic
        returns `AsyncAnthropic`)."""
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

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> AssistantMessage:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = [tool_to_wire(t) for t in tools]
            # tool_choice "auto" is the OpenAI default; being explicit avoids
            # provider surprises.
            payload["tool_choice"] = "auto"

        try:
            r = await self._client.post("/chat/completions", json=payload)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            raise ProviderTransientError(f"transport: {e}") from e

        if r.status_code == 429 or r.status_code >= 500:
            log.warning("provider HTTP %s (transient): %s", r.status_code, r.text[:200])
            raise ProviderTransientError(f"HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code >= 400:
            log.error("provider HTTP %s (permanent): %s", r.status_code, r.text[:500])
            raise ProviderPermanentError(f"HTTP {r.status_code}: {r.text[:500]}")

        return parse_openai_response(r.json())

    async def aclose(self) -> None:
        await self._client.aclose()


def parse_openai_response(raw: dict) -> AssistantMessage:
    """OpenAI chat completion → `AssistantMessage`. Drops provider-specific
    fields (e.g. Qwen's `reasoning_content`) from the returned content so
    they never leak into engine history; the raw dict is preserved on the
    return value for log-side inspection."""
    choice = raw.get("choices", [{}])[0]
    message = choice.get("message") or {}
    finish_raw = choice.get("finish_reason") or "stop"

    content = message.get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)

    raw_tool_calls = message.get("tool_calls") or []
    tool_calls: list[ToolCall] = []
    for tc in raw_tool_calls:
        try:
            fn = tc.get("function") or {}
            args_str = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
                if not isinstance(args, dict):
                    args = {"_raw": args}
            except json.JSONDecodeError:
                # Principle 4/5: don't silently drop; pass malformed args
                # through so the validator can flag it as an error on
                # dispatch (and pair a tool_result).
                args = {"_malformed_json": args_str}
            tool_calls.append(ToolCall(
                id=tc.get("id") or f"auto_{uuid.uuid4().hex[:8]}",
                name=fn.get("name") or "",
                arguments=args,
            ))
        except Exception:
            log.exception("failed to parse tool_call: %s", tc)

    return AssistantMessage(
        content=content,
        tool_calls=tool_calls,
        finish_reason=_normalize_finish(finish_raw),
        raw=raw,
    )


def _normalize_finish(r: str) -> FinishReason:
    # OpenAI surfaces: stop, length, tool_calls, content_filter, function_call.
    if r == "function_call":
        return FinishReason.TOOL_CALLS
    try:
        return FinishReason(r)
    except ValueError:
        log.warning("unknown finish_reason %r — treating as stop", r)
        return FinishReason.STOP
