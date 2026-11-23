"""Boot the dryrun worker with its own data directory and provider endpoint."""

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
from pathlib import Path

from physiclaw.dryrun import (
    CAPTURE_SIZE,
    DAILY_ACTIONS,
    HOST,
    KEY,
    MODEL,
    PHONE_ACTIONS,
    DryRunError,
)


def _write_config(home: Path, port: int) -> None:
    width, height = CAPTURE_SIZE
    # Keep both the session and provider retry budgets bounded. The runtime
    # imports config independently in its own managed process.
    contents = f"""[agent]
model = "openai/{MODEL}"

[engine]
max_turns = {max(len(PHONE_ACTIONS), len(DAILY_ACTIONS))}
max_attempts = 1
provider_retry_attempts = 1
provider_timeout_seconds = 8.0
max_session_seconds = 120

[camera]
width = {width}
height = {height}

[provider]
openai_api_key = "{KEY}"

[providers.openai]
base_url = "http://{HOST}:{port}/v1"
"""
    with (home / "config.toml").open("x", encoding="utf-8") as config:
        config.write(contents)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    home = args.workspace
    if os.environ.get("PHYSICLAW_HOME") != str(home) or (home / "config.toml").exists():
        parser.error(
            "Launch the isolated worker into a fresh workspace through physiclaw dryrun"
        )

    try:
        with socket.socket() as listener:
            listener.bind((HOST, 0))
            port = listener.getsockname()[1]
            _write_config(home, port)
            # HTTP clients honoring the environment cannot fall through to a
            # live endpoint. The loopback app does not proxy requests.
            local = f"http://{HOST}:{port}"
            os.environ.update(
                HTTP_PROXY=local, HTTPS_PROXY=local, ALL_PROXY=local, NO_PROXY=HOST
            )
            # CONFIG and PHYSICLAW_HOME are read at import time.
            from physiclaw.dryrun.runner import run

            asyncio.run(run(listener, verbose=args.verbose))
    except KeyboardInterrupt:
        return 130
    except DryRunError as exc:
        error = str(exc)
    except Exception as exc:
        logging.exception("Dryrun worker failed")
        error = str(exc)
    else:
        return 0

    (home / "failure.json").write_text(json.dumps(error), encoding="utf-8")
    return 1


if __name__ == "__main__":
    sys.exit(main())
