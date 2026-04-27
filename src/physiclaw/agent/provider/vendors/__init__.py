"""Concrete provider implementations.

One file per vendor — each subclasses one of the two wire-shape bases
(`OpenAICompatibleProvider` from `provider/openai_compat.py`, or
`AnthropicCompatibleProvider` from `provider/anthropic_compat.py`)
and declares `PROVIDER_ID`, `BASE_URL`, plus any wire-shape quirk
overrides. `BASE_URL` is overridable per-instance via
`~/.physiclaw/config.toml`'s `[providers.<id>] base_url = "..."`.

Registry assembly (id → class map, lookup helpers) lives in
`agent/provider/registry.py`.
"""
