"""Streaming, terminal-aware presentation for the doctor report."""

import sys
from dataclasses import dataclass
from typing import Literal

import typer

from physiclaw.common.terminal import (
    cell_width,
    color_enabled,
    stdout_text,
    terminal_width,
    wrap_text,
)


@dataclass(frozen=True)
class Check:
    label: str
    message: str
    status: Literal["OK", "WARN", "ERROR", "INFO"] = "OK"
    details: tuple[str, ...] = ()
    action: tuple[str | None, str] | None = None


def ok(label: str, message: str, *details: str) -> Check:
    return Check(label, message, details=details)


def warn(
    label: str,
    message: str,
    *details: str,
    action: tuple[str | None, str] | None = None,
) -> Check:
    return Check(label, message, "WARN", details, action)


def info(label: str, message: str, *details: str) -> Check:
    return Check(label, message, "INFO", details)


def error(
    label: str,
    message: str,
    *details: str,
    action: tuple[str | None, str] | None = None,
) -> Check:
    return Check(label, message, "ERROR", details, action)


def _style(text: str, **styles) -> str:
    text = stdout_text(text)
    return typer.style(text, **styles) if color_enabled(sys.stdout) else text


def _lines(text: str, indent: str = "  ", *, dim: bool = False) -> None:
    width = terminal_width(sys.stdout) or 100
    for line in wrap_text(stdout_text(text), max(20, width - len(indent))).splitlines():
        typer.echo(indent + _style(line, dim=dim))


class DoctorReport:
    def __init__(self) -> None:
        self.passed = 0
        self.attention = 0
        self.actions: dict[str | None, list[str]] = {}

    def header(self, version: str, *, deep: bool) -> None:
        typer.echo()
        typer.echo(
            "  "
            + _style("PhysiClaw doctor", bold=True)
            + _style(f"  v{version}", dim=True)
        )
        _lines("Deep check" if deep else "Standard check", dim=True)

    def section(self, title: str) -> None:
        typer.echo()
        width = min(72, terminal_width(sys.stdout) or 72)
        rule = "─" * max(0, width - cell_width(title) - 4)
        typer.echo(
            "  "
            + _style(title, fg=typer.colors.CYAN, bold=True)
            + _style(f" {rule}", dim=True)
        )

    def note(self, text: str) -> None:
        _lines(text, dim=True)

    def check(self, check: Check) -> None:
        self.passed += check.status == "OK"
        needs_attention = check.status in {"WARN", "ERROR"}
        self.attention += needs_attention
        if check.action:
            self.add_action(*check.action)

        badge = " " * 5
        if needs_attention:
            color = typer.colors.RED if check.status == "ERROR" else typer.colors.YELLOW
            badge = _style(f"{check.status:<5}", fg=color, bold=True)
        label = stdout_text(check.label)
        padded_label = label + " " * max(0, 16 - cell_width(label))
        heading = "  " + _style(padded_label, bold=True) + "  " + badge
        width = terminal_width(sys.stdout) or 100
        if width < 64 or cell_width(label) > 16:
            typer.echo(heading.rstrip())
            _lines(check.message, "    ")
            detail_indent = "    "
        else:
            lines = wrap_text(stdout_text(check.message), width - 27).splitlines()
            typer.echo(heading + "  " + lines[0])
            for line in lines[1:]:
                typer.echo(" " * 27 + line)
            detail_indent = " " * 27
        for detail in check.details:
            _lines(detail, detail_indent, dim=True)

    def add_action(self, command: str | None, description: str) -> None:
        descriptions = self.actions.setdefault(command, [])
        if description not in descriptions:
            descriptions.append(description)

    def finish(self, *, deep: bool) -> None:
        self.section("Summary")
        if self.attention:
            noun = "check needs" if self.attention == 1 else "checks need"
            _lines(f"{self.passed} passed · {self.attention} {noun} attention")
        else:
            _lines(f"{self.passed} checks passed.")
        if self.actions:
            self.section("Next steps")
            for index, (command, descriptions) in enumerate(self.actions.items(), 1):
                _lines(f"{index}. {' '.join(descriptions)}")
                if command:
                    typer.echo(
                        "     " + _style(command, fg=typer.colors.MAGENTA, bold=True)
                    )
        elif self.attention:
            self.note("Review the flagged rows above for details.")
        if not deep:
            typer.echo()
            self.note(
                "More checks: physiclaw doctor --deep (includes a model API request)"
            )
        typer.echo()
