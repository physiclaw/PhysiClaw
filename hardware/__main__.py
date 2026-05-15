"""CLI entry for building parts.

Usage from the repo root:

    uv run --group cad python -m hardware             # builds custom (default)
    uv run --group cad python -m hardware --standard
    uv run --group cad python -m hardware --custom --standard
"""
import argparse
import shutil

from hardware.parts.base import STEP_DIR, build_all
from hardware.parts.build_custom import ALL_PARTS as CUSTOM_PARTS
from hardware.parts.build_standard import ALL_PARTS as STANDARD_PARTS


def main():
    parser = argparse.ArgumentParser(prog="python -m hardware")
    parser.add_argument("--custom", action="store_true", help="build custom parts (default)")
    parser.add_argument("--standard", action="store_true", help="build standard parts")
    args = parser.parse_args()
    build_custom = args.custom or not args.standard
    shutil.rmtree(STEP_DIR, ignore_errors=True)
    if args.standard:
        build_all(STANDARD_PARTS)
    if build_custom:
        build_all(CUSTOM_PARTS)


if __name__ == "__main__":
    main()
