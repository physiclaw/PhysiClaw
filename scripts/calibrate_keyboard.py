"""Thin redirector — prefer `physiclaw setup phone`.

Kept so the old `uv run python scripts/calibrate_keyboard.py [images...]`
workflow still works from a repo checkout.
"""

import typer

from physiclaw.cli.setup.phone import phone

if __name__ == "__main__":
    typer.run(phone)
