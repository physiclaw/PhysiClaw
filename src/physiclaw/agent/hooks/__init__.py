"""Built-in PhysiClaw runtime hooks.

Every module in this package is auto-imported by `Runtime.start()` via
`physiclaw.agent.runtime.hook.load_hooks()`. To add a new hook, create a new
`.py` file here that uses `@register` from `physiclaw.agent.runtime.hook` —
no other wiring required.
"""
