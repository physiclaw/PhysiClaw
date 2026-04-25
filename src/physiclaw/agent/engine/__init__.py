"""Engine — provider-agnostic tool-use loop (low-level replacement for `claude -p`).

Submodules import lazily — there's no eager re-export of `engine.run`
because that triggered a cycle with `agent.provider` (which imports
`engine.dto`, which loads this `__init__`). Callers should
`from physiclaw.agent.engine.engine import run` directly.
"""
