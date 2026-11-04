"""OpenRouter chat completions, independent of its Decisions service.

Model IDs pass through unchanged; discovery supplies the live chat catalog.
Opaque reasoning stays in this provider's metadata for tool continuations.
"""

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from physiclaw.common.provider_errors import ProviderConfigurationError
from physiclaw.contract.dto import (
    AssistantMessage,
    Message,
    SystemMessage,
    Thinking,
)
from physiclaw.contract.structured import OutputSpec
from physiclaw.contract.turn import TurnMetadata
from physiclaw.provider.catalog import cache as discovery
from physiclaw.provider.chat.history import NO_CACHE_MARKERS, CacheMarkers
from physiclaw.provider.chat.openai_cache import (
    OpenAIPromptCacheMarkers,
    supports_explicit_cache,
)
from physiclaw.provider.chat.openai_compat import (
    OpenAICacheMarkers,
    OpenAICompatibleProvider,
)
from physiclaw.provider.chat.schema import strict_output_schema


class OpenRouterChatProvider(OpenAICompatibleProvider):
    PROVIDER_ID = "openrouter"
    BASE_URL = "https://openrouter.ai/api/v1"
    CACHE_MARKERS = NO_CACHE_MARKERS
    _catalog: dict[str, dict] | None = None

    def _select_cache_markers(self) -> CacheMarkers:
        if self.model.startswith("openai/") and supports_explicit_cache(
            self.model.removeprefix("openai/")
        ):
            return OpenAIPromptCacheMarkers()
        return NO_CACHE_MARKERS

    def _supported_parameters(self) -> list[str] | None:
        supported = (
            (self._catalog or {}).get(self.model, {}).get("supported_parameters")
        )
        return (
            supported
            if isinstance(supported, list)
            and all(isinstance(p, str) for p in supported)
            else None
        )

    def validate_output_spec(
        self, output_spec: OutputSpec, *, thinking: Thinking | None = None
    ) -> None:
        super().validate_output_spec(output_spec, thinking=thinking)
        if self._catalog is None:
            self._catalog = {m["id"]: m for m in discovery.load(self.PROVIDER_ID)}
        supported = self._supported_parameters()
        if supported is not None and "tools" not in supported:
            raise ProviderConfigurationError(
                f"{self.ref} does not advertise tool support"
            )
        self._output_tool(output_spec)

    def _output_tool(self, spec: OutputSpec) -> dict:
        tool = super()._output_tool(spec)
        supported = self._supported_parameters() or []
        if "structured_outputs" in supported:
            tool["function"]["parameters"] = strict_output_schema(
                tool["function"]["parameters"]
            )
            tool["function"]["strict"] = True
        return tool

    def _tool_params(
        self, spec: OutputSpec | None, *, thinking: Thinking | None
    ) -> dict:
        params = super()._tool_params(spec, thinking=thinking)
        supported = self._supported_parameters() or []
        if spec is not None and "parallel_tool_calls" in supported:
            params["parallel_tool_calls"] = False
        return params

    def _request_params(self, *, metadata: TurnMetadata | None) -> dict[str, Any]:
        params: dict[str, Any] = {"provider": {"require_parameters": True}}
        if metadata is not None:
            params["session_id"] = metadata.session_id
        if self.model.startswith("anthropic/"):
            params["cache_control"] = {"type": "ephemeral"}
        if isinstance(self.CACHE_MARKERS, OpenAIPromptCacheMarkers):
            params["prompt_cache_options"] = {"mode": "explicit"}
        return params

    def thinking_params(self, thinking: Thinking) -> dict[str, Any]:
        model = (self._catalog or {}).get(self.model, {})
        supported = self._supported_parameters()
        if supported is not None and not {
            "reasoning",
            "reasoning_effort",
        }.intersection(supported):
            return {}
        reasoning = model.get("reasoning") or {}
        if thinking == "off" and reasoning.get("mandatory"):
            # "Off" means the model's supported floor, as for native Gemini.
            efforts = reasoning.get("supported_efforts") or ["low"]
            floor = next(
                (e for e in ("minimal", "low", "medium", "high") if e in efforts), "low"
            )
            return {"reasoning": {"effort": floor}}
        return {
            "reasoning": {"enabled": False}
            if thinking == "off"
            else {"effort": thinking}
        }

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
        if thinking is not None and self.model not in (self._catalog or {}):
            cached = {m["id"]: m for m in discovery.load(self.PROVIDER_ID)}
            if self.model in cached:
                self._catalog = cached
            else:
                await self.list_models()
        return await super()._chat(
            history,
            tools,
            metadata=metadata,
            thinking=thinking,
            output_spec=output_spec,
            on_request=on_request,
        )

    async def list_models(self) -> list[dict]:
        models = await super().list_models()
        models = [
            model
            for model in models
            if isinstance(model.get("architecture"), dict)
            and model["architecture"].get("output_modalities") == ["text"]
        ]
        self._catalog = {model["id"]: model for model in models}
        return models

    def _parse_response(self, raw: dict) -> AssistantMessage:
        message = super()._parse_response(raw)
        native = raw["choices"][0]["message"]
        extra = {
            key: deepcopy(native[key])
            for key in ("reasoning", "reasoning_details")
            if native.get(key) is not None
        }
        if extra:
            message.vendor_extra[self.PROVIDER_ID] = {"model": self.model, **extra}
        return message

    def _encode_message(self, msg: Message) -> dict | list[dict] | None:
        entry = super()._encode_message(msg)
        if (
            isinstance(msg, SystemMessage)
            and isinstance(entry, dict)
            and self.model.startswith("anthropic/")
        ):
            # Automatic caching lands on volatile per-turn tails. Keep a
            # reusable system prefix even when those tails and images change.
            entry = OpenAICacheMarkers().mark_system(entry)
        if isinstance(msg, AssistantMessage) and isinstance(entry, dict):
            extra = msg.vendor_extra.get(self.PROVIDER_ID, {})
            if extra.get("model") == self.model:
                entry.update(
                    {
                        key: deepcopy(extra[key])
                        for key in ("reasoning", "reasoning_details")
                        if key in extra
                    }
                )
        return entry
