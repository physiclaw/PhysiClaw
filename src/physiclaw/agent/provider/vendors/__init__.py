"""Concrete provider implementations.

One file per vendor — each subclasses one of the two wire-shape bases
(`OpenAICompatibleProvider` from `provider/openai_compat.py`, or
`AnthropicCompatibleProvider` from `provider/anthropic_compat.py`)
and declares its endpoint, model catalog, auth lookup, and (optionally)
a `THINKING_FORMAT` flag.

Registry assembly (id → class map, lookup helpers) lives in
`agent/provider/registry.py`; this file stays empty so tooling that
scans the vendors/ directory (e.g. `ls`, import-by-discovery patterns
later) finds only vendor files.
"""
