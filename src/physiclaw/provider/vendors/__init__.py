"""Vendor API services.

Each vendor has a package. Its chat.py and decision.py modules are separate
adapters for separate API services, sharing credentials and lifecycle only.
For example, openai/chat.py speaks Chat Completions while openai/decision.py
speaks Decisions. A decision-only vendor needs no chat.py.

`BASE_URL` is overridable per-instance via `~/.physiclaw/config.toml`'s
`[providers.<id>] base_url = "..."`.

Registry assembly (id → class map, lookup helpers) lives in
`provider/registry.py`.
"""
