"""Terminal capabilities and readable text, with no third-party dependencies."""

import errno
import os
import re
import sys
import unicodedata
from typing import TextIO

from physiclaw import port as platform

# CSI colors/cursor commands, OSC titles/hyperlinks, and short ESC commands.
ANSI_RE = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ASCII_SYMBOLS = str.maketrans(
    {
        "✓": "OK",
        "✗": "X",
        "⚠": "!",
        "→": "->",
        "←": "<-",
        "↔": "<->",
        "—": "-",
        "–": "-",
        "…": "...",
        "×": "x",
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "│": "|",
        "─": "-",
        "·": "|",
    }
)


def is_terminal(stream: TextIO | None) -> bool:
    try:
        return stream is not None and stream.isatty()
    except (AttributeError, OSError, ValueError):
        return False


def color_enabled(stream: TextIO | None) -> bool:
    """Respect redirection, monochrome terminals and native console support."""
    return bool(
        is_terminal(stream)
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "").lower() != "dumb"
        and platform.enable_terminal_color(stream)
    )


def terminal_width(stream: TextIO | None) -> int | None:
    """Measure the output stream, not stdout; recheck after terminal resize."""
    if not is_terminal(stream):
        return None
    try:
        assert stream is not None
        columns = os.get_terminal_size(stream.fileno()).columns
        if columns > 0:
            return columns
    except (AttributeError, OSError, ValueError):
        pass
    try:
        columns = int(os.environ.get("COLUMNS", "80"))
        return columns if columns > 0 else 80
    except ValueError:
        return 80


def plain_text(text: str) -> str:
    """Keep line breaks; make controls visible instead of repainting the terminal."""
    text = ANSI_RE.sub("", text).replace("\r\n", "\n").expandtabs(4)
    return _CONTROLS.sub(lambda match: ascii(match[0])[1:-1], text)


def stream_text(text: str, stream: TextIO | None) -> str:
    """Retain Unicode when supported; escape unencodable detail without loss."""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        return (
            text.translate(_ASCII_SYMBOLS)
            .encode(encoding, "backslashreplace")
            .decode(encoding)
        )
    except LookupError:
        return (
            text.translate(_ASCII_SYMBOLS)
            .encode("ascii", "backslashreplace")
            .decode("ascii")
        )


def write_console(text: str, stream: TextIO | None) -> None:
    """A disappearing/closed console must not interrupt remaining file sinks."""
    if stream is None:
        return
    try:
        stream.write(stream_text(text, stream))
        stream.flush()
    except (BrokenPipeError, ValueError):
        pass
    except OSError as error:
        if error.errno not in (errno.EPIPE, errno.EBADF):
            raise


def cell_width(text: str) -> int:
    """Display columns for ordinary text, including CJK and combining marks."""
    return sum(
        0
        if unicodedata.combining(char)
        or unicodedata.category(char) in {"Cf", "Mn", "Me"}
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in text
    )


def wrap_text(text: str, width: int) -> str:
    """Wrap at words, retain explicit indentation and copyable URLs/paths."""
    lines = []
    for line in text.split("\n"):
        if cell_width(line) <= width:
            lines.append(line)
            continue
        indent = line[: len(line) - len(line.lstrip())]
        current = indent
        for word in line.split():
            gap = " " if current.strip() else ""
            if current.strip() and cell_width(current + gap + word) > width:
                lines.append(current)
                current = indent + word
            else:
                current += gap + word
        lines.append(current)
    return "\n".join(lines)


def stdout_text(text: str) -> str:
    """CLI decorations use the same encoding fallback as diagnostic stderr."""
    return stream_text(plain_text(text), sys.stdout)
