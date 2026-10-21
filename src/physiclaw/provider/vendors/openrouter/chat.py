"""OpenRouter chat completions, independent of its Decisions service.

Model IDs pass through unchanged; discovery supplies the live chat catalog.
Opaque reasoning stays in this provider's metadata for tool continuations.
"""

from copy import deepcopy
from dataclasses import replace
from typing import Any

from physiclaw.common.provider_errors import ProviderTransientError
from physiclaw.contract.dto import AssistantMessage, Message, Thinking, Usage
from physiclaw.contract.turn import TurnMetadata
from physiclaw.provider.chat import discovered
from physiclaw.provider.chat.base import NO_CACHE_MARKERS, token_count
from physiclaw.provider.chat.openai_compat import OpenAICompatibleProvider


class OpenRouterChatProvider(OpenAICompatibleProvider):
    PROVIDER_ID = "openrouter"
    BASE_URL = "https://openrouter.ai/api/v1"
    CACHE_MARKERS = NO_CACHE_MARKERS
    _catalog: dict[str, dict] | None = None

    def _request_params(self, *, metadata: TurnMetadata | None) -> dict[str, Any]:
        params: dict[str, Any] = {"provider": {"require_parameters": True}}
        if metadata is not None:
            params["session_id"] = metadata.session_id
        if self.model.startswith("anthropic/"):
            params["cache_control"] = {"type": "ephemeral"}
        return params

    def thinking_params(self, thinking: Thinking) -> dict[str, Any]:
        model = (self._catalog or {}).get(self.model, {})
        supported = model.get("supported_parameters")
        if isinstance(supported, list) and not {
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
    ) -> AssistantMessage:
        if thinking is not None and self._catalog is None:
            cached = {m["id"]: m for m in discovered.load(self.PROVIDER_ID)}
            if self.model in cached:
                self._catalog = cached
            else:
                await self.list_models()
        return await super()._chat(history, tools, metadata=metadata, thinking=thinking)

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
        if raw.get("error") or raw["choices"][0].get("finish_reason") == "error":
            raise ProviderTransientError("OpenRouter generation failed")
        native = raw["choices"][0]["message"]
        extra = {
            key: deepcopy(native[key])
            for key in ("reasoning", "reasoning_details")
            if native.get(key) is not None
        }
        if extra:
            message.vendor_extra[self.PROVIDER_ID] = {"model": self.model, **extra}
        return message

    def _parse_usage(self, raw: dict) -> Usage:
        usage = super()._parse_usage(raw)
        native = raw.get("usage")
        details = (
            native.get("prompt_tokens_details") if isinstance(native, dict) else None
        )
        if isinstance(details, dict) and "cache_write_tokens" in details:
            return replace(
                usage, cache_creation_tokens=token_count(details["cache_write_tokens"])
            )
        return usage

    def _encode_message(self, msg: Message) -> dict | list[dict] | None:
        entry = super()._encode_message(msg)
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
