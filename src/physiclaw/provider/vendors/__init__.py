"""Vendor API services.

Each vendor has a package. Its chat.py and decision.py modules are separate
adapters for separate API services. They share credential declarations and
client infrastructure; each adapter owns its client lifetime.
For example, openai/chat.py speaks Responses while openai/decision.py
speaks Decisions. A decision-only vendor needs no chat.py.

`BASE_URL` is overridable per-instance via `~/.physiclaw/config.toml`'s
`[providers.<id>] base_url = "..."`.

Registry assembly and capability metadata live in `provider/services/registry.py`;
application client construction lives in `provider/services/factory.py`.
"""
