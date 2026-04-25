"""Moonshot — OpenAI-compatible endpoint. Provider id is the API
surface (`moonshot`), not the brand (`kimi`).

Auth: `MOONSHOT_API_KEY` env, or `[provider] moonshot_api_key` in
`~/.physiclaw/config.toml`. `BASE_URL` defaults to the China endpoint;
override per-instance with `base_url=` for `api.moonshot.ai` (a key
minted for one domain returns 401 on the other).

Model ref examples: `moonshot/kimi-k2.6`, `moonshot/kimi-k2.5`. Catalog
omits `kimi-latest` (text-only, fails our vision requirement) and the
legacy `moonshot-v1-*` family (uses an explicit `/v1/caching` flow we
don't implement).

Caching: Moonshot honors `cache_control: {type: ephemeral}` markers on
text blocks — same shape as DashScope. We INHERIT the parent's
`_mark_system` / `_mark_stub` overrides; they're load-bearing for
cross-wake hits. Empirically (A/B against the API):

  - Markers OFF + identical request          : 100% hit (full byte match)
  - Markers OFF + user-message change        :   0% hit (cache busted)
  - Markers ON  + user-message change        : 100% hit (anchors honored)

PhysiClaw rewrites the wake-volatile content (cron stamps, trigger
text) into the user message right after the system. Without markers,
every wake starts cold even when the system is byte-stable. With
markers, every wake hits cache up to the marked anchors. The earlier
"K2 is purely auto-prefix, markers redundant" reading turned out to
match only the byte-identical case.

We still override `_parse_usage` because K2 sometimes places
`cached_tokens` at the top of the `usage` block instead of nested.
"""
from dataclasses import replace

from physiclaw.agent.engine.dto import Usage
from physiclaw.agent.provider.openai_compat import OpenAICompatibleProvider
from physiclaw.agent.provider.provider_base import ModelEntry


class MoonshotProvider(OpenAICompatibleProvider):
    PROVIDER_ID = "moonshot"
    BASE_URL = "https://api.moonshot.cn/v1"
    # API_KEY_ENV_VARS defaults to ("MOONSHOT_API_KEY",) by convention.
    MODELS = (
        ModelEntry("kimi-k2.6", context_window=256_000),
        ModelEntry("kimi-k2.5", context_window=256_000),
    )

    def _parse_usage(self, raw: dict) -> Usage:
        """K2 may surface `cached_tokens` at the top of the `usage`
        block instead of nested under `prompt_tokens_details`. Inherit
        the base parser, then fall through to the top-level location
        only if the nested read came up empty."""
        base = super()._parse_usage(raw)
        if base.cached_tokens:
            return base
        top_level = int(((raw or {}).get("usage") or {}).get("cached_tokens", 0) or 0)
        return replace(base, cached_tokens=top_level) if top_level else base
