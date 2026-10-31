# /// script
# requires-python = ">=3.12"
# dependencies = ["pygments==2.21.0", "rich==14.3.3"]
# ///
"""Report physical lines in the Git working tree: Total = Code + Test + Text."""

from __future__ import annotations

import codecs
import io
import os
import subprocess
import sys
import tokenize
from pathlib import Path, PurePath

from pygments.lexers import find_lexer_class_for_filename
from pygments.lexers.special import TextLexer
from rich.console import Console
from rich.text import Text

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".json",
    ".json5",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".xml",
    ".md",
    ".mdx",
    ".rst",
    ".txt",
    ".csv",
    ".tsv",
}
LOCKFILES = {
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "packages.lock.json",
    "bun.lockb",
}
EXCLUDED_SUFFIXES = set(
    ".lock .svg .png .jpg .jpeg .gif .webp .ico .bmp .tif .tiff .avif .pdf "
    ".woff .woff2 .ttf .otf .eot .zip .gz .bz2 .xz .7z .tar "
    ".mp3 .mp4 .wav .mov .webm .pyc .pyo .so .dll .exe .bin .wasm "
    ".stl .step .stp .fcstd".split()
)
TEST_DIRECTORIES = {"test", "tests", "__tests__"}


def category(path: PurePath) -> str:
    """Config/data/docs stay Text even when used as test fixtures."""
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES and path.name != "CMakeLists.txt":
        return "Text"
    # Normalize extensions without losing special names such as Makefile.
    lexer = find_lexer_class_for_filename(path.with_suffix(suffix).name)
    if lexer is None or issubclass(lexer, TextLexer):
        return "Text"
    name = path.name.lower()
    if (
        any(part.lower() in TEST_DIRECTORIES for part in path.parent.parts)
        or name == "conftest.py"
        or name.startswith("test_")
        or path.stem.lower().endswith("_test")
        or ".test." in name
        or ".spec." in name
    ):
        return "Test"
    return "Code"


def read_content(path: Path) -> str | None:
    """Decode text explicitly; return None for NUL-containing binary data."""
    raw = path.read_bytes()
    try:
        # Check UTF-32 first: its little-endian BOM starts with the UTF-16 BOM.
        if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
            encoding = "utf-32"
        elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            encoding = "utf-16"
        elif b"\0" in raw:
            return None
        elif path.suffix.lower() == ".py":
            encoding = tokenize.detect_encoding(io.BytesIO(raw).readline)[0]
        else:
            encoding = "utf-8-sig"
        return raw.decode(encoding)
    except (UnicodeError, SyntaxError, LookupError) as exc:
        # Never silently drop a text file and present an incomplete total.
        raise ValueError(f"{path}: {exc}") from exc


def collect(root: Path) -> dict[str, int]:
    root = root.resolve()
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    counts = {"Code": 0, "Test": 0, "Text": 0}
    # NUL delimiters handle spaces/newlines; a set prevents double counting.
    for name in sorted(set(result.stdout.split(b"\0")) - {b""}):
        relative = Path(os.fsdecode(name))
        if relative.suffix.lower() in EXCLUDED_SUFFIXES or relative.name in LOCKFILES:
            continue
        path = root / relative
        # Also reject symlinked parent directories for cached Git paths.
        if not path.is_file() or path.resolve() != path:
            continue
        content = read_content(path)
        if content is None:
            continue
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        lines = content.count("\n") + int(bool(content) and not content.endswith("\n"))
        counts[category(relative)] += lines
    return counts


def terminal_text(text: str, console: Console) -> str:
    """Keep Unicode where supported; escape unsupported names on legacy streams."""
    try:
        text.encode(console.encoding)
        return text
    except UnicodeEncodeError:
        return (
            text.replace("·", "|")
            .encode(console.encoding, "backslashreplace")
            .decode(console.encoding)
        )


def show_report(console: Console, counts: dict[str, int], name: str) -> None:
    total = sum(counts.values())
    number_width = len(f"{total:,}")
    title = Text("  ")
    title.append(terminal_text(name, console), style="bold")
    title.append(terminal_text("  ·  Line count", console), style="dim")

    console.print()
    console.print(title)
    console.print()
    for label in ("Total", "Code", "Test", "Text"):
        is_total = label == "Total"
        value = total if is_total else counts[label]
        row = Text("  ")
        row.append(f"{label:<5}    ", style="bold" if is_total else "dim")
        row.append(
            f"{value:,}".rjust(number_width),
            style="bold cyan" if is_total else "cyan",
        )
        console.print(row)
        if is_total:
            console.print()
    console.print()


def main() -> int:
    # Disable styling as well as color when plain output is requested.
    force_terminal = False if "NO_COLOR" in os.environ else None
    console = Console(force_terminal=force_terminal, highlight=False)
    try:
        counts = collect(ROOT)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        error_console = Console(stderr=True, force_terminal=force_terminal)
        error_console.print(
            Text(
                terminal_text(f"Cannot count lines: {exc}", error_console),
                style="bold red",
            ),
            soft_wrap=True,
        )
        return 1
    show_report(console, counts, ROOT.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
