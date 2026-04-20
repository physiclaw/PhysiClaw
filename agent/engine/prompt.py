"""SYSTEM prompt composition + prefix-cache verification.

With native tool-calling (principle 1), tool schemas flow through the
provider's `tools=` parameter — they are NOT rendered into the prompt.
The prompt carries only behavior (AGENT.md doctrine), engine conventions
that override AGENT.md's JSON-response section, memory, and cron context.

`prefix_hash` anchors the provider's cache breakpoint at the SYSTEM message.
"""
import hashlib
from pathlib import Path

_INSTRUCTIONS_DIR = Path(__file__).resolve().parent.parent / "instructions"
_AGENT = (_INSTRUCTIONS_DIR / "AGENT.md").read_text().rstrip()
_ENGINE_CONVENTIONS = (_INSTRUCTIONS_DIR / "CONVENTION.md").read_text().rstrip()


def render_system(
    *,
    memory_ctx: str,
    cron_ctx: str,
    skills_ctx: str = "",
    mcp_instructions: str = "",
) -> str:
    """Compose the full SYSTEM for one session. Tool schemas are provided
    to the model via the native `tools=` API, not rendered here.

    Order is identity → device → engine rules → state:
      AGENT.md             who you are + loop + soul
      mcp_instructions     how to operate this tool surface (server-authored)
      CONVENTION.md        engine turn-level rules (overrides conflicts)
      skills / memory / cron
    """
    parts: list[str] = [_AGENT]
    if mcp_instructions:
        parts.append(mcp_instructions)
    parts.append(_ENGINE_CONVENTIONS)
    if skills_ctx:
        parts.append(skills_ctx)
    if memory_ctx:
        parts.append("# Memory\n\n" + memory_ctx)
    if cron_ctx:
        parts.append(cron_ctx)
    return "\n\n".join(parts)


def prefix_hash(messages: list[dict]) -> str:
    """sha256 of the SYSTEM message — provider prompt-cache anchor."""
    if not messages or messages[0].get("role") != "system":
        raise ValueError("prefix_hash: messages[0] must be the system message")
    content = messages[0].get("content", "")
    if not isinstance(content, str):
        raise ValueError(
            f"prefix_hash: system content must be str, got {type(content).__name__}"
        )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
