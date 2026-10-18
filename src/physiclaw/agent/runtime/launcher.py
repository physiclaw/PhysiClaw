"""Runtime launcher — resolves the API model and starts the hook loop.

Invoked via ``python -m physiclaw.agent.runtime``. The server starts this
process separately so long-running engine sessions do not block MCP serving.
``[agent] model`` or ``PHYSICLAW_MODEL`` selects a ``provider/model`` reference;
every provider uses the same PhysiClaw engine. External agents connect to
``physiclaw mcp`` without starting this runtime.
"""

import argparse
import asyncio
import logging
import math
import os
import sys
from functools import partial

from physiclaw.agent.engine.mcp_tool import close_mcp
from physiclaw.agent.runtime import Runtime
from physiclaw.common import paths
from physiclaw.common.config import (
    SERVER_ENV_VAR,
    model_ref_with_source,
    server_url,
)
from physiclaw.common.log_style import report_error
from physiclaw.common.logger import setup_logging
from physiclaw.common.proxy import normalize_proxy_env
from physiclaw.provider import ProviderConfigurationError, validate_model_ref

log = logging.getLogger(__name__)


def engine_label(ref: str) -> str:
    """Human-readable engine, provider, and model label."""
    provider_id, model_id = validate_model_ref(ref)
    return f"engine=physiclaw, provider={provider_id}, model={model_id}"


def resolve() -> tuple[str, str]:
    """Return `(ref, source)` for the active model ref.

    `source` describes where the value came from so log lines and error
    messages can point users at the right knob. Validates that the
    provider id is selectable; the model id is passed through verbatim
    — provider APIs reject unknown ids on the first chat."""
    ref, source = model_ref_with_source()  # raises if not configured
    try:
        validate_model_ref(ref)
    except ValueError as e:
        raise RuntimeError(f"{e} (from {source})") from e
    return ref, source


def launch() -> None:
    # Normally inherited already normalized from the server process, but
    # a direct `python -m physiclaw.agent.runtime` skips the CLI callback
    # — and a raw `socks://` proxy env kills every httpx client here
    # (provider AND the loopback poll/MCP clients). It also pins loopback
    # into NO_PROXY so local status polls and MCP calls stay local.
    normalize_proxy_env()
    parser = argparse.ArgumentParser(description="PhysiClaw runtime loop")
    # None → resolve via config.server_url() ($PHYSICLAW_SERVER, else
    # [server] host/port) so a bare `python -m physiclaw.agent.runtime`
    # dials the same server the CLI would.
    parser.add_argument("--server", default=None)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval < 0:
        parser.error("interval must be finite and non-negative")
    try:
        ref, source = resolve()
    except RuntimeError as e:
        # Friendly exit: print the (already actionable) message to stderr
        # and bail. No Python stack trace bleeds into the parent shell.
        report_error(str(e), tag="runtime")
        sys.exit(1)

    # Hooks read this to know where the MCP server lives. Must be set
    # before load_hooks() imports them. An explicit --server (the server
    # always passes one when spawning this loop) OVERRIDES an inherited
    # PHYSICLAW_SERVER — flag beats env, else a shell-exported var would
    # silently point the runtime at a server nothing is listening on. A
    # bare launch keeps the env-aware server_url() fallback.
    if args.server:
        os.environ[SERVER_ENV_VAR] = args.server
    else:
        os.environ.setdefault(SERVER_ENV_VAR, server_url())

    # Mirror to runtime.log so wake decisions / poll errors survive for
    # post-mortems — the runtime's stderr is gone once the terminal is.
    setup_logging(
        "runtime",
        logging.DEBUG if args.verbose else logging.INFO,
        file_dir=paths.LOG_DIR,
        file_level=logging.DEBUG,
    )

    from physiclaw.agent.engine.engine import run as engine_run

    react = partial(engine_run, model_ref=ref)

    label = engine_label(ref)
    log.info(
        "%s [%s]",
        label,
        source,
        extra={
            "event": "model.selected",
            "model_ref": ref,
            "console": True,
            "console_message": f"Model: {ref}",
        },
    )

    async def _main():
        try:
            await Runtime(
                react=react,
                interval=args.interval,
                label=label,
            ).start()
        finally:
            await close_mcp()

    try:
        asyncio.run(_main())
    except ProviderConfigurationError as e:
        report_error(str(e), tag="runtime")
        sys.exit(1)
    except KeyboardInterrupt:
        pass
