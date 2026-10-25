"""Compact presentation of esptool 4's streamed output."""

import re
import sys

import typer

from physiclaw.cli._format import info, section, step_ok, warn
from physiclaw.common.terminal import color_enabled, stdout_text, terminal_width

_IMAGES = {
    0x1000: "Bootloader",
    0x8000: "Partition table",
    0xE000: "Boot settings",
    0x10000: "Firmware",
    0x3D0000: "Machine configuration",
}
_PROGRESS = re.compile(r"Writing at (0x[\da-fA-F]+).*\((\d+)\s*%\)")
_WRITTEN = re.compile(r"Wrote .* at (0x[\da-fA-F]+) in ([\d.]+) seconds")


class FlashOutput:
    def __init__(self, *, verbose: bool = False):
        self.verbose = verbose
        self.live = (
            color_enabled(sys.stdout) and (terminal_width(sys.stdout) or 0) >= 40
        )
        self.current: str | None = None
        self.pending: tuple[str, str] | None = None
        self.percent = -1
        self.line_width = 0

    def close(self) -> None:
        """Clear an unfinished progress line without implying success."""
        if self.line_width:
            typer.echo("\r" + " " * self.line_width + "\r", nl=False)
            self.line_width = 0

    def feed(self, line: str) -> None:
        if self.verbose:
            typer.echo(stdout_text(line))
            return
        if line.startswith("Chip is "):
            self.close()
            typer.echo(info("Chip: " + line.removeprefix("Chip is ")))
        elif line.startswith("Auto-detected Flash size: "):
            typer.echo(info("Flash: " + line.split(": ", 1)[1]))
        elif match := _PROGRESS.search(line):
            address, percent = int(match[1], 16), min(int(match[2]), 100)
            name = next(
                (
                    name
                    for offset, name in reversed(_IMAGES.items())
                    if address >= offset
                ),
                "Firmware",
            )
            if name != self.current:
                self.close()
                self.current, self.percent = name, -1
            if self.live:
                text = f"  {name:<22} {percent:>3}%"
                self.line_width = len(text)
                typer.echo("\r" + section(text), nl=False)
            elif name == "Firmware" and percent // 25 != self.percent // 25:
                typer.echo(info(f"{name}: {percent}%"))
            self.percent = percent
        elif match := _WRITTEN.search(line):
            name = _IMAGES.get(int(match[1], 16), "Firmware")
            self.pending = (name, match[2])
        elif line == "Hash of data verified." and self.pending:
            self.close()
            name, seconds = self.pending
            typer.echo(step_ok(f"{name} verified · {seconds}s"))
            self.pending = None
        elif line.startswith("Hard resetting via RTS"):
            self.close()
            typer.echo(info("Restarting board …"))
        elif line.startswith(("WARNING", "Warning", "ERROR", "Lost connection")):
            self.close()
            typer.echo(warn(line))
