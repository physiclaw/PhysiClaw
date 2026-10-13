"""Concrete provider implementations.

One file per vendor. Qwen, Moonshot, OpenAI, and DeepSeek declare
endpoints and capabilities, and translate the engine's thinking level
into model-specific request fields. Other wire differences stay close
to the vendor that requires them:

  - `google.py`    — `GoogleProvider` (Gemini via the OpenAI shim):
                     tool-result image splits, thought signatures,
                     cache markers disabled.
  - `anthropic.py` — `AnthropicProvider` (Claude): Anthropic-compat
                     wire shape via the official SDK.

`BASE_URL` is overridable per-instance via `~/.physiclaw/config.toml`'s
`[providers.<id>] base_url = "..."`.

Registry assembly (id → class map, lookup helpers) lives in
`provider/registry.py`.
"""
