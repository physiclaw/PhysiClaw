"""Standalone hardware log sinks and presentation; standard library only.

BuildLog owns event collection and progress. These handlers independently save
complete events, forward the worker protocol, or render the existing task table.
No application logger or global logging configuration is needed.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import sys
import textwrap
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from hardware.scheme import REPO_ROOT

WORKER_PREFIX = "PHYSICLAW_BUILD_EVENT "
LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}
_FINAL_EVENTS = {"build_finished", "command_finished"}

_CONTEXT_WIDTH = 26


def _color_enabled():
    try:
        return bool(
            sys.stdout is not None
            and sys.stdout.isatty()
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
        )
    except (AttributeError, OSError, ValueError):
        return False


def _single_line(value: object) -> str:
    # Escape control characters (including ANSI escapes) from library errors.
    return "".join(
        char if char.isprintable() else f"\\x{ord(char):02x}" for char in str(value)
    )


def _cell_width(text: str) -> int:
    return sum(
        0
        if unicodedata.category(char) in {"Cf", "Mn", "Me"}
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in text
    )


def _fit_line(text: str, width: int) -> str:
    if _cell_width(text) <= width:
        return text
    suffix = "." * min(3, width)
    shown = ""
    for char in text:
        if _cell_width(shown + char) > width - len(suffix):
            break
        shown += char
    return shown + suffix


def _progress_label(completed: int, total: int | None) -> str:
    return f"[{completed}/{total}]" if total is not None else f"[{completed}]"


def _progress_line(event: dict, stream, columns: int) -> str:
    """Fit one live row without letting long names wrap onto another row."""
    progress_width, context_width = _table_widths(columns, event.get("total"))
    progress = _progress_label(event["completed"], event.get("total"))
    context = _fit_line(
        _encode(_single_line(event.get("stem") or event["item"]), stream),
        context_width,
    )
    padding = " " * max(0, context_width - _cell_width(context))
    return _fit_line(
        f"{progress:>{progress_width}} {context}{padding}  {event['elapsed_s']:7.1f}s",
        columns,
    )


def _encode(text: str, stream) -> str:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        return text.encode(encoding, "backslashreplace").decode(encoding)
    except LookupError:
        return text.encode("ascii", "backslashreplace").decode("ascii")


def _live_terminal(stream) -> bool:
    try:
        return bool(
            stream is not None
            and stream.isatty()
            and os.environ.get("TERM", "").lower() != "dumb"
            and os.environ.get("CI", "").lower() not in {"1", "true", "yes"}
        )
    except (AttributeError, OSError, ValueError):
        return False


def _table_widths(columns: int, total: int | None) -> tuple[int, int]:
    progress_width = max(7, len(f"[{total}/{total}]")) if total is not None else 7
    progress_width = min(progress_width, max(1, columns - 12))
    # Reserve the same elapsed column for headers, progress and summaries.
    context_width = min(_CONTEXT_WIDTH, columns - progress_width - 1 - 2 - 8)
    return progress_width, max(1, context_width)


def _text_lines(record: dict, *, show_level: bool = True) -> str:
    """Align columns and wrap cells without truncating diagnostic text."""
    columns = max(24, shutil.get_terminal_size(fallback=(120, 24)).columns)
    label = "WARN" if record["level"] == "warning" else record["level"].upper()
    prefix = f"{label:7s} " if show_level else ""
    elapsed = f"{record['elapsed_s']:7.1f}s"
    progress = ""
    if "completed" in record:
        progress = _progress_label(record["completed"], record.get("total"))
    context = record.get("stem") or record.get("item")
    if not context and record.get("worker_pid"):
        context = f"worker {record['worker_pid']}"
    message = _single_line(record["message"])
    if record.get("event") in ("step_finished", "task_finished"):
        message = "" if record.get("status") == "success" else "Failed"
    if record.get("variant"):
        message = f"{_single_line(record['variant'])}: {message}"
    if (
        not show_level
        and record["level"] in ("error", "critical")
        and record.get("event") not in _FINAL_EVENTS
    ):
        message = f"{label}: {message}"
    progress_width, context_width = _table_widths(
        columns - len(prefix), record.get("total")
    )
    span_width = progress_width + 1 + context_width
    continuation = " " * len(prefix)
    if not context:
        texts = textwrap.wrap(message, width=span_width, break_on_hyphens=False) or [""]
        return "\n".join(
            f"{prefix}{text:<{span_width}}  {elapsed}"
            if index == 0
            else continuation + text
            for index, text in enumerate(texts)
        )
    contexts = textwrap.wrap(
        _single_line(context), width=context_width, break_on_hyphens=False
    )
    indent = continuation + " " * (progress_width + 1)
    lines = [
        f"{prefix}{progress:>{progress_width}} {contexts[0]:<{context_width}}  {elapsed}"
    ]
    lines.extend(indent + name for name in contexts[1:])
    lines.extend(
        indent + text
        for text in textwrap.wrap(message, width=context_width, break_on_hyphens=False)
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class BuildState:
    """Presentation snapshot; never included in saved or worker events."""

    total: int | None
    completed: int
    summary: dict | None
    repeated: bool


class JsonFormatter(logging.Formatter):
    def __init__(self, *, worker=False):
        super().__init__()
        self.worker = worker

    def format(self, record):
        # File and stdout must serialize exactly the same event. ASCII escaping
        # also keeps JSON valid on limited console encodings and for surrogates.
        if not hasattr(record, "hardware_json"):
            record.hardware_json = json.dumps(record.hardware_event, ensure_ascii=True)
        return (WORKER_PREFIX if self.worker else "") + record.hardware_json


class ConsoleFilter(logging.Filter):
    def __init__(self, *, verbose=False):
        super().__init__()
        self.verbose = verbose

    def filter(self, record):
        event = record.hardware_event
        name = event["event"]
        if event["level"] == "warning" or name == "worker_running":
            return False
        if self.verbose and name not in _FINAL_EVENTS:
            return True
        if event["level"] == "debug" or name in {
            "log_opened",
            "outputs_cleared",
            "build_summary",
        }:
            return False
        if (
            name == "step_finished"
            and record.hardware_state.repeated
            and event.get("status") == "success"
        ):
            return False
        if name == "build_started" and not event.get("stems"):
            return False
        if name == "cache_checked" and not event["cached"]:
            return False
        return True


class BuildFormatter(logging.Formatter):
    def __init__(self, *, verbose=False):
        super().__init__()
        self.verbose = verbose

    def format(self, record):
        event = self._terminal_record(record.hardware_event, record.hardware_state)
        return self._render_terminal(event, record.hardware_state)

    def _render_terminal(self, record, state):
        color = _color_enabled()

        def style(text, code):
            return f"\033[{code}m{text}\033[0m" if color else text

        columns = max(24, shutil.get_terminal_size(fallback=(120, 24)).columns)
        level_width = 8 if self.verbose else 0
        progress_width, context_width = _table_widths(
            columns - level_width, state.total
        )
        table_width = level_width + progress_width + 1 + context_width + 2 + 8
        rule = style("-" * min(table_width, columns), "2")
        name = record["event"]
        if not self.verbose and name in (
            "build_started",
            "build_plan",
            "command_started",
        ):
            count = state.total
            title = record.get("title") or (
                f"Hardware build / {count} {'assembly' if count == 1 else 'assemblies'}"
            )
            name_label = "Task" if name == "command_started" else "Assembly"
            if context_width < 8:
                name_label = "Name"
            heading = f"{'Step':>{progress_width}} {name_label:<{context_width}}  {'Elapsed':>8}"
            return "\n".join(
                (
                    style(textwrap.fill(title, width=columns), "1"),
                    "",
                    style(
                        textwrap.fill(heading, width=columns, replace_whitespace=False),
                        "2",
                    ),
                    rule,
                )
            )

        text = _text_lines({"total": state.total, **record}, show_level=self.verbose)
        if record["level"] in ("error", "critical"):
            text = style(text, "1;31")
        elif name in _FINAL_EVENTS:
            text = style(text, "1;32" if record.get("status") == "success" else "1;33")
        elif name == "cache_checked":
            text = style(text, "2")
        elif (
            color
            and name in ("step_finished", "task_finished")
            and record.get("status") == "success"
        ):
            lines = text.splitlines()
            elapsed = f"{record['elapsed_s']:7.1f}s"
            first = lines[0]
            count_start, count_end = first.find("["), first.find("]")
            if count_start >= 0 and count_end > count_start:
                first = (
                    first[:count_start]
                    + style(first[count_start : count_end + 1], "2")
                    + first[count_end + 1 :]
                )
            if first.endswith(elapsed):
                first = first[: -len(elapsed)] + style(elapsed, "2")
            text = "\n".join([first, *lines[1:]])
        if name in _FINAL_EVENTS:
            details = [
                style(
                    textwrap.fill(
                        _single_line(detail), width=columns, subsequent_indent="  "
                    ),
                    "2",
                )
                for detail in record.get("terminal_details", [])
            ]
            return "\n".join((rule, text, *details))
        return text

    def _terminal_record(self, record, state):
        name = record["event"]
        if self.verbose and name not in _FINAL_EVENTS:
            return record
        message = record["message"]
        if name == "command_error":
            message = message.splitlines()[0] if message else "Command failed"
        elif name == "cache_checked":
            message = f"{record['cached']} cached; {record['to_build']} to build"
        elif name in _FINAL_EVENTS:
            message = record["status"].upper()
            details = list(record.get("details", []))
            if record.get("summary"):
                message += f"  {record['summary']}"
            if state.summary is not None:
                summary = state.summary
                message += f"  {summary['complete']}/{summary['total']} complete"
                counts = [
                    f"{summary[key]} {label}"
                    for key, label in (
                        ("cached", "cached"),
                        ("rebuilt", "built"),
                        ("reannotated", "updated"),
                    )
                    if summary.get(key)
                ]
                if counts:
                    details.append(", ".join(counts))
            elif state.total is not None:
                message += f"  {state.completed}/{state.total} complete"
            path = (
                record.get("log_path")
                if record.get("exit_code")
                else record.get("output_dir")
            )
            if path:
                path = Path(path)
                shown = (
                    path.relative_to(REPO_ROOT)
                    if path.is_relative_to(REPO_ROOT)
                    else path
                )
                details.append(
                    f"{'Details' if record.get('exit_code') else 'Output'}: {shown}"
                )
            record = {**record, "terminal_details": details}
        return {**record, "message": message}


class EventFileHandler(logging.FileHandler):
    """A failed durable write must fail the build, never be swallowed by logging."""

    def __init__(self, path):
        super().__init__(path, mode="x", encoding="utf-8", errors="backslashreplace")
        self.setLevel(logging.DEBUG)
        self.setFormatter(JsonFormatter())

    def emit(self, record):
        if self.stream is None:
            raise ValueError("Build log is closed")
        self.stream.write(self.format(record) + "\n")
        self.flush()


class ConsoleHandler(logging.StreamHandler):
    """Follow stdout redirection; a lost worker protocol remains a hard failure."""

    def __init__(self, *, worker=False, log_format="text", verbose=False):
        super().__init__(sys.stdout)
        self.strict = worker or log_format == "json"
        self.live = not self.strict and not verbose
        self._progress_stream = None
        self._progress_width = 0
        self.setLevel(logging.DEBUG)
        if self.strict:
            self.setFormatter(JsonFormatter(worker=worker))
        else:
            self.setFormatter(BuildFormatter(verbose=verbose))
            self.addFilter(ConsoleFilter(verbose=verbose))

    def emit(self, record):
        self.stream = sys.stdout
        event = record.hardware_event
        if (
            self._progress_stream is not None
            and self._progress_stream is not self.stream
        ):
            self._finish_progress()
        if (
            self.live
            and _live_terminal(self.stream)
            and event["event"] == "step_finished"
            and event.get("status") == "success"
        ):
            columns = max(1, shutil.get_terminal_size(fallback=(120, 24)).columns - 1)
            text = _progress_line(
                {"completed": record.hardware_state.completed, **event},
                self.stream,
                columns,
            )
            width = _cell_width(text)
            self._write(
                self.stream,
                "\r" + text + " " * max(0, min(self._progress_width, columns) - width),
            )
            self._progress_stream = self.stream
            self._progress_width = width
        else:
            self._finish_progress()
            self._write(self.stream, self.format(record) + self.terminator)

    def _finish_progress(self):
        stream, self._progress_stream = self._progress_stream, None
        self._progress_width = 0
        if stream is not None:
            self._write(stream, self.terminator)

    def close(self):
        self.acquire()
        try:
            self._finish_progress()
        finally:
            self.release()
            super().close()

    def _write(self, stream, text):
        if stream is None:
            if self.strict:
                raise OSError("Build event output is unavailable")
            return
        try:
            stream.write(_encode(text, stream))
            stream.flush()
        except (BrokenPipeError, ValueError):
            if self.strict:
                raise
        except OSError as error:
            if self.strict or error.errno not in (errno.EPIPE, errno.EBADF):
                raise
