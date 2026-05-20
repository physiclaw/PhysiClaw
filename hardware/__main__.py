"""CLI entry for exporting part STEPs.

Usage from the repo root:

    uv run --group cad python -m hardware             # exports custom (default)
    uv run --group cad python -m hardware --standard
    uv run --group cad python -m hardware --custom --standard
"""
import argparse
import shutil

from hardware.parts.base import STEP_DIR, export_all
from hardware.parts.export_custom import ALL_PARTS as CUSTOM_PARTS
from hardware.parts.export_standard import ALL_PARTS as STANDARD_PARTS


def main():
    parser = argparse.ArgumentParser(prog="python -m hardware")
    parser.add_argument("--custom", action="store_true", help="export custom parts (default)")
    parser.add_argument("--standard", action="store_true", help="export standard parts")
    args = parser.parse_args()
    export_custom = args.custom or not args.standard
    shutil.rmtree(STEP_DIR, ignore_errors=True)
    if args.standard:
        export_all(STANDARD_PARTS)
    if export_custom:
        export_all(CUSTOM_PARTS)


if __name__ == "__main__":
    main()
