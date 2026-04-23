"""Thin redirector — prefer `physiclaw setup hardware`.

Kept so the old `uv run python scripts/setup.py [-y] [--trace]` workflow
still works from a repo checkout.
"""

import typer

from physiclaw.cli.setup.hardware import hardware

if __name__ == "__main__":
    typer.run(hardware)
