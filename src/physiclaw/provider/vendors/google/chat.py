"""Google Gemini — via Google's OpenAI-compatible endpoint.

Google maintains a first-party OpenAI-compatible shim at
`/v1beta/openai/`. We use that rather than the bespoke
`generativelanguage.googleapis.com` REST API; the shim shares the same
`chat()` flow as other compatibility adapters, with two Gemini-3 quirks
routed around at the wire boundary:

  1. **`image_url` is rejected in `role: tool`** ('Invalid content part
     type: image_url'), but accepted in `role: user`. We split a
     `ToolResultMessage` carrying images into [text-only tool,
     synthetic user with images]; the engine layer is unchanged.

  2. **Tool flows require `extra_content.google.thought_signature`** on
     the first tool_call of every assistant message that called a tool
     ('Function call is missing a thought_signature'). The signature is
     an opaque server-signed token Gemini emits on responses to anchor
     its prior internal reasoning across turns. We round-trip it via
     `AssistantMessage.vendor_extra["google"]["thought_signature"]`. A
     synthetic tool_call with no captured signature (cold start, test
     fixtures) falls back to the documented bypass token —
     `"skip_thought_signature_validator"` — which costs thinking-state
     continuity for that one turn but keeps the request legal.

Auth: `GOOGLE_API_KEY` env, or `[provider] google_api_key` in
`~/.physiclaw/config.toml`.

Model ref examples:  `google/gemini-3.1-pro-preview`,
`google/gemini-3-flash-preview`, `google/gemini-2.5-pro`.

Thinking rides `extra_body.google.thinking_config` in the HTTP body;
the field follows the generation — Gemini 3 takes a
`thinking_level` word, Gemini 2.5 a `thinking_budget` in tokens.
The native config preserves each model's supported floor. The HTTP body
shape and tool continuations have been checked against Google's reference
and live Gemini 2.5 / 3 Flash calls through a proxy.
"""

import re
from dataclasses import replace
from typing import Any

import httpx

from physiclaw.contract.dto import (
    AssistantMessage,
    Message,
    Thinking,
    ToolResultMessage,
    Usage,
)
from physiclaw.contract.structured import OutputSpec
from physiclaw.provider.chat.base import NO_CACHE_MARKERS, token_count_or_none
from physiclaw.provider.chat.openai_compat import OpenAICompatibleProvider
from physiclaw.provider.chat.retry import retry_after_seconds
from physiclaw.provider.chat.wire import (
    assistant_to_wire,
    tool_result_with_user_images,
)

# Documented escape hatch from Google's thought-signatures doc. Used
# when no captured signature is available for an outbound tool_call.
_SIG_BYPASS = "skip_thought_signature_validator"


# "Off" maps to the model's supported floor. Gemini 2.5 Pro requires at
# least 128 thinking tokens; Gemini 3/4 floors are selected per model below.
_BUDGETS_25 = {"off": 0, "low": 1024, "medium": 8192, "high": 24576}


class GoogleChatProvider(OpenAICompatibleProvider):
    PROVIDER_ID = "google"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

    def _retry_after(self, response: httpx.Response) -> float | None:
        delay = super()._retry_after(response)
        try:
            payload = response.json()
        except ValueError:
            return delay
        # The compatibility endpoint also returns list-wrapped RPC errors.
        for item in payload if isinstance(payload, list) else [payload]:
            error = item.get("error") if isinstance(item, dict) else None
            details = error.get("details") if isinstance(error, dict) else None
            for detail in details if isinstance(details, list) else []:
                if (
                    not isinstance(detail, dict)
                    or detail.get("@type") != "type.googleapis.com/google.rpc.RetryInfo"
                ):
                    continue
                value = detail.get("retryDelay")
                if isinstance(value, str) and re.fullmatch(
                    r"\d+(?:\.\d{1,9})?s", value
                ):
                    seconds = retry_after_seconds(value[:-1])
                    if seconds is not None:
                        delay = max(delay or 0.0, seconds)
        return delay

    def _tool_params(
        self, spec: OutputSpec | None, *, thinking: Thinking | None
    ) -> dict:
        if spec is not None:
            return {
                "tool_choice": {
                    "type": "function",
                    "function": {"name": spec.function_name},
                }
            }
        return super()._tool_params(spec, thinking=thinking)

    def thinking_params(self, thinking: Thinking) -> dict[str, Any]:
        model = self.model
        if model.startswith(("gemini-3", "gemini-4-argon")):
            if thinking == "off":
                # Newer Flash models don't necessarily retain the original
                # Flash floor: 3.8 rejects minimal, as does Gemini 4 Argon.
                # Keep that setting limited to the documented model lines.
                minimal = model.startswith(
                    (
                        "gemini-3-flash",
                        "gemini-3.1-flash",
                        "gemini-3.5-flash",
                        "gemini-3.6-flash",
                    )
                )
                level = "minimal" if minimal else "low"
            elif thinking == "medium" and model.startswith("gemini-3-pro"):
                level = "low"
            else:
                level = thinking
            config: dict[str, str | int] = {"thinking_level": level}
        elif model.startswith("gemini-2.5"):
            budget = _BUDGETS_25[thinking]
            if "pro" in model:
                budget = max(budget, 128)
            config = {"thinking_budget": budget}
        else:
            return {}
        return {"extra_body": {"google": {"thinking_config": config}}}

    # Cache markers: disabled. Gemini's shim ignores Anthropic-style
    # `cache_control`. The bigger reason for stripping is that the stub
    # marker tracks the *latest* superseded tool_result, whose position
    # shifts forward every screen-obs turn — even the additive key the
    # base `mark_stub` now toggles on/off is a byte delta at exactly
    # the prefix position Gemini's implicit cache wants to anchor on,
    # with zero upside when the shim ignores the field anyway. Hits
    # surface via standard
    # `usage.prompt_tokens_details.cached_tokens`; the base usage parser
    # reads it, and the override below normalizes paid output thinking.
    # Implicit caching itself is non-deterministic
    # across both Flash and Pro (intermittent zero-hit turns on stable
    # prefixes; see issue googleapis/python-genai#2064 and
    # tests/probe_google_cache_*) — don't bank on the theoretical 75%.
    CACHE_MARKERS = NO_CACHE_MARKERS

    async def list_models(self) -> list[dict]:
        """Strip Gemini's `models/` id prefix so the user-facing form
        is uniform across providers (`gemini-2.5-flash`, not
        `models/gemini-2.5-flash`). The OpenAI shim accepts both forms
        on the chat side, so no re-prefixing is needed when sending."""
        raw = await super().list_models()
        return [{**m, "id": m.get("id", "").removeprefix("models/")} for m in raw]

    # ---------- request side: shim quirk overrides ----------

    def _encode_message(self, msg: Message) -> dict | list[dict] | None:
        if isinstance(msg, ToolResultMessage):
            return tool_result_with_user_images(msg)
        if isinstance(msg, AssistantMessage) and msg.tool_calls:
            entry = assistant_to_wire(msg)
            sig = (msg.vendor_extra.get("google") or {}).get(
                "thought_signature"
            ) or _SIG_BYPASS
            entry["tool_calls"][0]["extra_content"] = {
                "google": {"thought_signature": sig},
            }
            return entry
        return super()._encode_message(msg)

    # ---------- response side: capture signature for next turn ----------

    def _parse_usage(self, raw: dict) -> Usage:
        usage = super()._parse_usage(raw)
        counters = raw.get("usage")
        if not isinstance(counters, dict):
            return usage
        prompt = token_count_or_none(counters.get("prompt_tokens"))
        completion = token_count_or_none(counters.get("completion_tokens"))
        total = token_count_or_none(counters.get("total_tokens"))
        # Require source counts: Usage's zero fallback also means unreported.
        if prompt is None or completion is None or total is None:
            return usage
        # The shim can include paid thinking only in total_tokens. An already
        # inclusive completion produces a zero difference and isn't counted twice.
        missing = max(0, total - prompt - completion)
        return replace(
            usage,
            completion_tokens=usage.completion_tokens + missing,
            reasoning_tokens=max(usage.reasoning_tokens, missing),
        )

    def _parse_response(self, raw: dict) -> AssistantMessage:
        asst = super()._parse_response(raw)
        sig = _extract_thought_signature(raw)
        if sig:
            asst.vendor_extra.setdefault("google", {})["thought_signature"] = sig
        return asst


# ---------- helpers ----------


def _extract_thought_signature(raw: dict) -> str | None:
    """Locate the thought_signature in a Gemini OpenAI-shim response.

    When the response carries tool_calls the signature rides on the
    first one (`tool_calls[0].extra_content.google.thought_signature`);
    text-only responses put it on the message itself
    (`message.extra_content.google.thought_signature`).

    Also probes `tool_calls[0].function.thought_signature` as a
    defensive fallback — OpenClaw's `extractGoogleThoughtSignature`
    checks both paths, suggesting some shim variants stash it nested
    under `function`. Costs nothing and saves us from a silent miss
    if the shape changes.
    """
    msg = (raw.get("choices") or [{}])[0].get("message") or {}
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        tc = tool_calls[0]
        extra = (tc.get("extra_content") or {}).get("google") or {}
        if sig := extra.get("thought_signature"):
            return sig
        if sig := (tc.get("function") or {}).get("thought_signature"):
            return sig
    extra = (msg.get("extra_content") or {}).get("google") or {}
    return extra.get("thought_signature")
