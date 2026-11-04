"""Direct OpenAI generation via stateless Responses and native JSON schemas.

Endpoint, schema, thinking, replay state and cache controls stay in this
adapter. Other OpenAI-compatible vendors keep their own wire protocol.
"""

import re
from typing import Any, Literal, cast

from physiclaw.contract.decision import decision_json
from physiclaw.contract.dto import AssistantMessage, Message, Thinking
from physiclaw.contract.structured import OutputSpec
from physiclaw.contract.turn import TurnMetadata
from physiclaw.provider.chat.history import NO_CACHE_MARKERS, CacheMarkers
from physiclaw.provider.chat.http import HTTPChatProvider
from physiclaw.provider.chat.openai_cache import (
    OpenAIPromptCacheMarkers,
    supports_explicit_cache,
)
from physiclaw.provider.chat.schema import strict_output_schema
from physiclaw.provider.chat.wire import tool_to_wire
from physiclaw.provider.vendors.openai.responses import encode_message, parse_response

# Exact aliases plus dated snapshots; unknown variants keep API defaults.
_MINIMAL_MODELS = {"gpt-5", "gpt-5-mini", "gpt-5-nano"}
_NONE_MODELS = {
    "gpt-5.1",
    "gpt-5.2",
    "gpt-5.4",
    "gpt-5.5",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-6.1-terra",
}
_LOW_MODELS = {"gpt-6-astra", "gpt-6.1-sol"}


class OpenAIChatProvider(HTTPChatProvider):
    PROVIDER_ID = "openai"
    BASE_URL = "https://api.openai.com/v1"
    ENDPOINT = "/responses"

    def _select_cache_markers(self) -> CacheMarkers:
        return (
            OpenAIPromptCacheMarkers()
            if supports_explicit_cache(self.model)
            else NO_CACHE_MARKERS
        )

    def _select_output_transport(self) -> Literal["tool", "native"]:
        return "native"

    def _encode_message(self, msg: Message) -> dict | list[dict]:
        return encode_message(msg, model=self.model)

    def validate_output_spec(
        self, output_spec: OutputSpec, *, thinking: Thinking | None = None
    ) -> None:
        strict_output_schema(cast(dict, decision_json(output_spec.schema)))

    def build_request(
        self,
        history: list[Message],
        tools: list[dict],
        *,
        metadata: TurnMetadata | None = None,
        thinking: Thinking | None = None,
        output_spec: OutputSpec | None = None,
    ) -> dict:
        if output_spec is not None:
            history = self._structured_history(history, output_spec)
        body: dict[str, Any] = {
            "model": self.model,
            "input": self.serialize_history(history, metadata=metadata),
            "store": False,
        }
        if isinstance(self.CACHE_MARKERS, OpenAIPromptCacheMarkers):
            body["prompt_cache_options"] = {"mode": "explicit"}
        if thinking is not None:
            body.update(self.thinking_params(thinking))
        if self.max_output_tokens is not None:
            body["max_output_tokens"] = self.max_output_tokens
        if output_spec is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": output_spec.name,
                    "strict": True,
                    "schema": strict_output_schema(
                        cast(dict, decision_json(output_spec.schema))
                    ),
                }
            }
        elif tools:
            body["tools"] = [
                {"type": "function", **tool_to_wire(tool)["function"], "strict": False}
                for tool in tools
            ]
            body["tool_choice"] = "auto"
        return body

    def _decode_response(self, raw: object, *, structured: bool) -> AssistantMessage:
        return parse_response(raw, model=self.model, structured=structured)

    def thinking_params(self, thinking: Thinking) -> dict[str, Any]:
        model = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", self.model)
        effort: str
        if model in _MINIMAL_MODELS:
            effort = "minimal" if thinking == "off" else thinking
        elif model in _NONE_MODELS:
            effort = "none" if thinking == "off" else thinking
        elif model in _LOW_MODELS or self.model.startswith(("o1", "o3", "o4")):
            effort = "low" if thinking == "off" else thinking
        else:
            return {}
        return {"reasoning": {"effort": effort}}
