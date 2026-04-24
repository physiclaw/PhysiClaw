"""Runtime launcher — wires args + env + engine choice, starts the Runtime loop.

Invoked via `python -m physiclaw.agent.runtime` (the package's `__main__.py` shim
imports `launch` from here).

Spawned as a subprocess by `physiclaw.main` so the hook loop runs
out-of-process from the MCP server. This isolates long-running hook
work (claude -p subprocess, or the in-process engine) from the FastMCP
event loop.

Two engines:
  - `physiclaw`   — in-process tool-call loop (this repo's). Drives one
                    of the providers below.
  - `claude-code` — subprocess to Anthropic's `claude` CLI; the loop is
                    Claude Code's own.

Single env var `PHYSICLAW_PROVIDER` selects the whole stack:
  qwen | kimi | chatgpt | claude   → physiclaw engine + that provider
                                     (`claude` = Anthropic's API direct)
  claude-code                      → external Claude Code subprocess

Default (unset) is `claude-code`. Each provider needs its own credential
env var (QWEN_API_KEY, KIMI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY) —
the chosen `Provider` reads its credential at startup and fails loudly if
missing.
"""

import argparse
import asyncio
import logging
import os
from functools import partial

from physiclaw.agent.engine.mcp_tool import close_mcp
from physiclaw.agent.engine.provider import PROVIDER_NAMES
from physiclaw.agent.runtime import Runtime
from physiclaw.agent.runtime.config import EXTERNAL, PROVIDER_DEFAULT, PROVIDER_ENV_VAR
from physiclaw.core.logger import setup_logging

log = logging.getLogger(__name__)

# Single env var picks the whole stack. The value is either a provider name
# (engine=physiclaw + that provider) or "claude-code" (external CLI loop).
PROVIDER_CHOICES = (*PROVIDER_NAMES, EXTERNAL)


def resolve() -> tuple[str, str]:
    """Return (choice, source). `choice` is either a provider name or the
    sentinel "claude-code"; source describes where the value came from
    so log lines and error messages can point users to the right knob."""
    from physiclaw.config import CONFIG

    env_val = os.environ.get(PROVIDER_ENV_VAR)
    if env_val is not None:
        choice, source = env_val, f"{PROVIDER_ENV_VAR} env"
    elif CONFIG.provider.name:
        choice, source = CONFIG.provider.name, "config.toml [provider] name"
    else:
        choice, source = PROVIDER_DEFAULT, "default"

    if choice not in PROVIDER_CHOICES:
        raise RuntimeError(
            f"provider {choice!r} (from {source}) is not one of {PROVIDER_CHOICES}"
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

    if choice == EXTERNAL:
        from physiclaw.agent.runtime.claude import spawn_claude as react

        label = "engine=claude-code"
    else:
        from physiclaw.agent.engine import run as engine_run

        react = partial(engine_run, provider_name=choice)
        label = f"engine=physiclaw, provider={choice}"
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
