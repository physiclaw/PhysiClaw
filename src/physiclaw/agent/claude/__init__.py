"""Claude Code engine — spawns `claude -p` for each hook wake.

All claude-code-specific code lives in this package:
  spawn.py   — subprocess lifecycle, stream log, retry
  plugin.py  — per-session --plugin-dir materializer
  CLAUDE.md  — self-contained system prompt (independent of agent/context/*)
  skills/    — claude-only skills (jobs/ — calls engine/jobs.py)

Removal policy: if this package is deleted, the native engine
(agent/engine/) continues to work unchanged. `runtime/launcher.py`
probes for this package at runtime; missing → claude-code is simply
not a selectable engine. No dead code elsewhere.
"""

from physiclaw.agent.claude.spawn import spawn_claude

# Public compatibility label. Model-ref routing uses provider.registry.CLAUDE_CODE_ID.
ENGINE_NAME = "claude-code"

__all__ = ["spawn_claude", "ENGINE_NAME"]
