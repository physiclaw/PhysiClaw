"""Runtime launcher — wires args + env + engine choice, starts the Runtime loop.

Invoked via `python -m agent.runtime` (the package's `__main__.py` shim
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

from agent.engine.provider import PROVIDER_NAMES
from agent.runtime import Runtime
from agent.runtime.config import EXTERNAL, PROVIDER_DEFAULT, PROVIDER_ENV_VAR
from physiclaw.logger import setup_logging

log = logging.getLogger(__name__)

# Single env var picks the whole stack. The value is either a provider name
# (engine=physiclaw + that provider) or "claude-code" (external CLI loop).
PROVIDER_CHOICES = (*PROVIDER_NAMES, EXTERNAL)


def resolve() -> tuple[str, str]:
    """Return (choice, source). `choice` is either a provider name or the
    sentinel "claude-code"; default is "claude-code" when env is unset."""
    explicit = os.environ.get(PROVIDER_ENV_VAR)
    if explicit is None:
        return PROVIDER_DEFAULT, "default"
    if explicit not in PROVIDER_CHOICES:
        raise RuntimeError(
            f"{PROVIDER_ENV_VAR}={explicit!r} is not one of {PROVIDER_CHOICES}"
        )
    return explicit, f"{PROVIDER_ENV_VAR} override"


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
        from agent.runtime.claude import spawn_claude as react

        label = "engine=claude-code"
    else:
        from agent.engine import run as engine_run

        react = partial(engine_run, provider_name=choice)
        label = f"engine=physiclaw, provider={choice}"
    log.info("%s [%s]", label, source)

    try:
        asyncio.run(
            Runtime(react=react, interval=args.interval, label=label).start()
        )
    except KeyboardInterrupt:
        pass
