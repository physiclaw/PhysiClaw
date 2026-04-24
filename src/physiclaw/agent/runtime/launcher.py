"""Runtime launcher — wires args + env + engine choice, starts the Runtime loop.

Invoked via `python -m physiclaw.agent.runtime` (the package's `__main__.py`
shim imports `launch` from here).

Spawned as a subprocess by `physiclaw.main` so the hook loop runs
out-of-process from the MCP server. This isolates long-running hook work
(claude -p subprocess, or the in-process engine) from the FastMCP event
loop.

Two engines, one hook loop:

  - `physiclaw`   — in-process tool-call loop (agent/engine/). Drives one
                    of the providers registered in engine.provider.
  - `claude-code` — subprocess to Anthropic's `claude` CLI. Lives under
                    agent/claude/. Loaded lazily — if the package is
                    deleted, the string `claude-code` is simply not a
                    valid choice; the engine path keeps working.

Single env var `PHYSICLAW_PROVIDER` (or [provider] name in config.toml)
selects the whole stack:
  qwen          → physiclaw engine + Qwen (DashScope, OpenAI-compatible)
  claude-code   → agent/claude/ subprocess

Each in-process provider reads its credential at startup (e.g.
QWEN_API_KEY env var or [provider] qwen_api_key in config.toml) and
fails loudly if missing.

Kimi/OpenAI/Anthropic providers are planned but not yet implemented —
their key fields exist in config.toml as forward-compat placeholders.
"""

import argparse
import asyncio
import logging
import os
from functools import partial

from physiclaw.agent.engine.mcp_tool import close_mcp
from physiclaw.agent.engine.provider import PROVIDER_NAMES
from physiclaw.agent.runtime import Runtime
from physiclaw.config import PROVIDER_ENV_VAR
from physiclaw.core.logger import setup_logging

log = logging.getLogger(__name__)


def _claude_name() -> str | None:
    """Return the Claude engine name if agent/claude/ is installed, else None.

    Lazy + isolated: importing agent.claude touches Claude-specific code
    (plugin dir, spawn). If the package is removed, the import fails and
    `claude-code` is not a selectable choice. Any other exception is a
    real bug in agent/claude/ that we want to surface — don't swallow.
    """
    try:
        from physiclaw.agent.claude import ENGINE_NAME
    except ImportError:
        return None
    return ENGINE_NAME


def _provider_choices() -> tuple[str, ...]:
    """All currently selectable engine names, claude-code included iff
    agent/claude/ is installed."""
    claude = _claude_name()
    return (*PROVIDER_NAMES, *([claude] if claude else ()))


def engine_label(choice: str) -> str:
    """Human-readable label for the resolved engine choice.

    `claude-code` is a whole engine (the subprocess runner); the other
    choices select a provider inside the in-process physiclaw engine.
    Surface the distinction so operators see "engine=..." consistently
    — used by startup logs, doctor, and server-side state.
    """
    claude = _claude_name()
    if claude and choice == claude:
        return f"engine={choice}"
    return f"engine=physiclaw, provider={choice}"


def resolve() -> tuple[str, str]:
    """Return (choice, source). `choice` is either a provider name or the
    claude engine name; source describes where the value came from so
    log lines and error messages can point users to the right knob."""
    from physiclaw.config import CONFIG, provider_name

    # Empty string is treated as unset — `export PHYSICLAW_PROVIDER=`
    # is a common way shells "unset" a var for a single command, and
    # failing membership on `""` yields a confusing error.
    env_val = os.environ.get(PROVIDER_ENV_VAR) or None
    if env_val:
        choice, source = env_val, f"{PROVIDER_ENV_VAR} env"
    elif CONFIG.provider.name:
        choice, source = CONFIG.provider.name, "config.toml [provider] name"
    else:
        choice, source = provider_name(), "default"

    choices = _provider_choices()
    if choice not in choices:
        if not choices:
            raise RuntimeError(
                "no engines available: install agent/claude/ or register a "
                "provider in agent/engine/provider.py"
            )
        raise RuntimeError(
            f"provider {choice!r} (from {source}) is not one of {choices}"
        )
    return choice, source


def launch() -> None:
    parser = argparse.ArgumentParser(description="PhysiClaw runtime loop")
    parser.add_argument("--server", default="http://127.0.0.1:8048")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    choice, source = resolve()

    # Hooks read this to know where the MCP server lives. Must be set
    # before load_hooks() imports them.
    os.environ.setdefault("PHYSICLAW_SERVER", args.server)

    setup_logging("runtime", logging.DEBUG if args.verbose else logging.INFO)
    # Silence noisy per-request logs from httpx/httpcore.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    claude_name = _claude_name()
    if claude_name is not None and choice == claude_name:
        from physiclaw.agent.claude import spawn_claude as react
    else:
        from physiclaw.agent.engine import run as engine_run

        react = partial(engine_run, provider_name=choice)
    label = engine_label(choice)
    log.info("%s [%s]", label, source)

    async def _main():
        try:
            await Runtime(
                react=react, interval=args.interval, label=label,
            ).start()
        finally:
            await close_mcp()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
