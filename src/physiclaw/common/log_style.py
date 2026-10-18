"""Repository-wide logging contract; standard library only.

Runtime, CLI, Studio and standalone diagnostic scripts share these formatters,
JSON envelope, color policy, component selection and task-local correlation.
Artifact journals keep their own payload schemas; operational logs use this one.
"""

import datetime as dt
import json
import logging
import os
import sys
import threading
from collections.abc import Callable
from contextvars import ContextVar, Token
from fnmatch import fnmatchcase
from typing import Any

from physiclaw.common.terminal import ANSI_RE as ANSI_RE
from physiclaw.common.terminal import (
    cell_width,
    color_enabled,
    plain_text,
    stream_text,
    terminal_width,
    wrap_text,
    write_console,
)
from physiclaw.common.text import clip

_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("physiclaw_log_context", default={})

_FIELDS = (
    "event",
    "session_id",
    "turn",
    "tool_call_id",
    "tool",
    "duration_ms",
    "status",
    "model_ref",
    "turn_count",
    "tool_count",
    "input_tokens",
    "output_tokens",
    "cache_hit_pct",
    "exit_code",
    "app",
    "playbook",
    "node",
    "attempt",
    "http_status",
    "retry_in_ms",
    "device_id",
)


def bind_log_context(**fields: Any) -> Token[dict[str, Any]]:
    """Merge correlation fields for this task; retain the token for its scope."""
    return _CONTEXT.set({**_CONTEXT.get(), **fields})


def reset_log_context(token: Token[dict[str, Any]]) -> None:
    """Restore the parent scope, including when a session is cancelled."""
    _CONTEXT.reset(token)


def record_fields(record: logging.LogRecord) -> dict[str, Any]:
    """Only publish known diagnostic fields; never dump arbitrary log extras."""
    context = _CONTEXT.get()
    return {
        key: value
        for key in _FIELDS
        if (value := getattr(record, key, context.get(key))) is not None
    }


def json_console_enabled() -> bool:
    return os.environ.get("PHYSICLAW_LOG_FORMAT", "").strip().lower() == "json"


def console_text(value: Any, limit: int = 180) -> str:
    """Bound diagnostic prose in notices; never use for receipts or paths."""
    return clip(" ".join(plain_text(str(value)).split()), limit)


class FailureState:
    """Warn on a new/changed outage; keep repeated failures at DEBUG on disk.

    The owner resets this on success, so a later outage is announced again.
    Store exception types and messages, never instances retaining live frames.
    """

    def __init__(self) -> None:
        self._failure: tuple[type, str] | None = None
        self._lock = threading.Lock()

    def level(self, error: BaseException, level: int = logging.WARNING) -> int:
        signature = (type(error), str(error))
        with self._lock:
            changed = signature != self._failure
            self._failure = signature
        return level if changed else logging.DEBUG

    def recovered(self) -> bool:
        with self._lock:
            failed = self._failure is not None
            self._failure = None
        return failed


def redact_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Copy tool arguments with clipboard bodies replaced by character counts."""
    values = dict(arguments)
    if name == "send_to_clipboard":
        values["text"] = f"<{len(str(values.get('text') or ''))} chars>"
    elif name == "sequence" and isinstance(values.get("actions"), list):
        values["actions"] = [
            {**step, "arg": f"<{len(str(step.get('arg') or ''))} chars>"}
            if isinstance(step, dict) and step.get("tool_name") == "send_to_clipboard"
            else step
            for step in values["actions"]
        ]
    return values


def format_tool_call(name: str, arguments: dict[str, Any], limit: int = 220) -> str:
    """Readable call arguments, bounded without echoing clipboard bodies."""
    body = ", ".join(
        f"{key}={console_text(repr(value), 140)}"
        for key, value in redact_tool_arguments(name, arguments).items()
    )
    budget = max(1, limit - len(name) - 2)
    return f"{name}({clip(body, budget)})"


def format_console_error(message: str, *, tag: str, exit_code: int = 1) -> str:
    """Keep expected startup/CLI errors parseable even before logging starts."""
    record = logging.getLogger("physiclaw.cli").makeRecord(
        "physiclaw.cli",
        logging.ERROR,
        "",
        0,
        message,
        (),
        None,
        extra={"event": "process.error", "exit_code": exit_code},
    )
    formatter = (
        JsonLogFormatter(tag)
        if json_console_enabled()
        else TaggedFormatter(
            tag, color_enabled(sys.stderr), verbose=False, adaptive=True
        )
    )
    return formatter.format(record)


class JsonLogFormatter(logging.Formatter):
    def __init__(self, tag: str):
        super().__init__()
        self.tag = tag

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "type": "log",
            "schema_version": 1,
            "timestamp": dt.datetime.fromtimestamp(record.created)
            .astimezone()
            .isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "service": self.tag,
            "logger": record.name,
            "path": record.pathname,
            "line": record.lineno,
            "pid": record.process,
            "thread": record.threadName,
            "message": ANSI_RE.sub("", record.getMessage()),
            "event": "log.message",
            **record_fields(record),
        }
        # JSON consumers receive the complete diagnostic record, independent
        # of the bounded prose and traceback choices of the pretty renderer.
        if record.exc_info:
            payload["exception"] = ANSI_RE.sub(
                "", self.formatException(record.exc_info)
            )
        elif record.exc_text:
            payload["exception"] = ANSI_RE.sub("", record.exc_text)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        # ASCII escaping preserves unpaired surrogates and guarantees that
        # embedded newlines/control characters cannot split a record.
        return json.dumps(
            payload, ensure_ascii=True, default=str, separators=(",", ":")
        )


# Use the terminal's own foreground for prose and standard colors for emphasis.
# This respects light/dark themes and terminals with only a basic palette.
_DIM = "2"
_YELLOW = "33"
_RED = "31"
_GREEN = "32"
_GREEN_BRIGHT = "92"

# Tag → accent color. Each entry-point picks its own so devs can skim
# interleaved output at a glance.
_TAG_COLORS = {
    "physiclaw": "36",  # cyan — hardware server
    "runtime": "35",  # magenta — agent loop
    "setup wizard": _GREEN,  # `physiclaw auto` calibration steps
}

# Who is driving, painted on the tag: while a playbook walk produces the
# turns the `[runtime]` tag turns green, so a terminal reader sees the
# hand-off between playbook and model down the left edge without
# reading a line. The engine sets it as each turn's producer is known
# and clears it at session end; file sinks are plain and never see it.
PLAYBOOK_ACCENT = _GREEN_BRIGHT  # a green of its own, not the wizard's
_tag_accent: str | None = None


def _component(name: str) -> str:
    source = name.removeprefix("physiclaw.")
    source = source.removeprefix("agent.").removeprefix("core.")
    parts = source.split(".")
    return ".".join(parts[:-1]) if len(parts) > 1 and parts[-1] == parts[-2] else source


def set_tag_accent(code: str | None) -> None:
    """Override the tag's accent color (an ANSI code) for lines from now
    on; None restores the tag's own."""
    global _tag_accent
    _tag_accent = code


class TaggedFormatter(logging.Formatter):
    """Stable text columns for terminals; full provenance for saved logs."""

    def __init__(
        self,
        tag: str,
        color: bool,
        *,
        diagnostic: bool = False,
        verbose: bool = True,
        adaptive: bool = False,
        width: int | None = None,
    ):
        super().__init__(datefmt="%Y-%m-%dT%H:%M:%S" if diagnostic else "%H:%M:%S")
        self.color = color
        self.tag = tag
        self.diagnostic = diagnostic
        self.verbose = verbose
        self.adaptive = adaptive
        self.width = width
        self._tag_color = _TAG_COLORS.get(tag, "36")

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, self.datefmt)
        level_name = {logging.WARNING: "WARN", logging.CRITICAL: "FATAL"}.get(
            record.levelno, record.levelname
        )
        level = f"{level_name:5}"
        source = _component(record.name) if self.verbose or self.diagnostic else ""
        if self.diagnostic:
            ts += f".{int(record.msecs):03d}" + self.formatTime(record, "%z")
            source = (
                f"{record.name}:{record.lineno} "
                f"pid={record.process} thread={record.threadName}"
            )
        prefix = f"{ts} {level} [{self.tag}] "
        if source:
            prefix += f"{source} | "
        if self.diagnostic or self.verbose or not hasattr(record, "console_message"):
            msg = record.getMessage()
        else:
            msg = getattr(record, "console_message")
        if self.diagnostic and (fields := record_fields(record)):
            first, sep, rest = msg.partition("\n")
            metadata = " ".join(f"{key}={value}" for key, value in fields.items())
            msg = f"{first} | {metadata}{sep}{rest}"
        # Append exception / stack traceback the way logging.Formatter does —
        # otherwise log.exception() / exc_info=True tracebacks vanish from
        # every sink (console, daily files, per-session runtime.log). The
        # continuation indent below then aligns the traceback under the tag.
        show_traceback = (
            self.diagnostic
            or (self.adaptive and self.verbose)
            or getattr(record, "console_traceback", self.verbose)
        )
        if show_traceback and record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text and show_traceback:
            msg = f"{msg}\n{record.exc_text}"
        elif record.exc_info and record.exc_info[1] is not None:
            error = record.exc_info[1]
            cause = str(error)
            detail = f"{type(error).__name__}: {cause}"
            # Some emitters already include the cause without its type. Avoid
            # repeating it, and keep multi-line SDK errors out of normal output.
            if detail not in msg and (not cause or cause not in msg):
                msg += " — " + (
                    detail if self.verbose or self.diagnostic else console_text(detail)
                )
        if record.stack_info:
            msg = f"{msg}\n{self.formatStack(record.stack_info)}"
        msg = plain_text(msg)
        if not self.verbose and not self.diagnostic:
            # Legacy calibration diagnostics indent their own messages. The
            # normal renderer owns layout; retain source indentation on disk.
            msg = msg.lstrip()
        if self.adaptive:
            msg = stream_text(msg, sys.stderr)
        color = color_enabled(sys.stderr) if self.adaptive else self.color
        width = terminal_width(sys.stderr) if self.adaptive else self.width
        prefix_width = cell_width(prefix)
        # Don't impose terminal wrapping on redirected output or diagnostic files.
        # Keep URLs/paths intact; very long tokens may use native terminal wrapping.
        stacked = (
            not self.diagnostic
            and width is not None
            and (
                prefix_width + 24 >= width
                or (
                    prefix_width > width * 0.45
                    and any(
                        cell_width(line) + prefix_width >= width
                        for line in msg.split("\n")
                    )
                )
            )
        )
        if stacked:
            assert width is not None
            header = wrap_text(prefix.removesuffix(" | ").rstrip(), max(width - 1, 1))
            msg = wrap_text(msg, max(width - 3, 1)).replace("\n", "\n  ")
            if not color:
                return header + "\n  " + msg
        else:
            if width is not None and not self.diagnostic:
                msg = wrap_text(msg, max(width - prefix_width - 1, 1))
            # Normal output is a short status feed, with readable blocks near
            # the left edge. Verbose/file output retains aligned provenance.
            indent = prefix_width if self.verbose or self.diagnostic else 2
            msg = msg.replace("\n", "\n" + " " * indent)
            if not color:
                return prefix + msg
        if record.levelno >= logging.ERROR:
            msg_color = _RED
        elif record.levelno >= logging.WARNING:
            msg_color = _YELLOW
        else:
            msg_color = ""
        tag_color = _tag_accent or self._tag_color
        body = f"\033[{msg_color}m{msg}\033[0m" if msg_color else msg
        if stacked:
            return f"\033[{tag_color}m{header}\033[0m\n  {body}"
        return (
            f"\033[{_DIM}m{ts}\033[0m "
            f"\033[{msg_color or _GREEN}m{level}\033[0m "
            f"\033[{tag_color}m[{self.tag}]\033[0m "
            + (f"\033[{_DIM}m{source} |\033[0m " if source else "")
            + body
        )


class OwnDebugFilter(logging.Filter):
    """Admit every ``physiclaw.*`` record (down to DEBUG) plus any INFO+
    record from other loggers. Keeps a DEBUG capture full-detail for our
    own code without drowning it in third-party DEBUG spew — or leaking
    SDK request bodies some libraries log at DEBUG. A no-op on an INFO
    handler (nothing below INFO reaches it), so it's safe to attach
    unconditionally."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Test the level first: INFO+ is the common case and skips the
        # per-record startswith on the majority of records.
        return record.levelno >= logging.INFO or record.name.startswith("physiclaw")


class ConsoleFilter(logging.Filter):
    """Task-level notices by default; full technical records in verbose/JSON.

    INFO is saved diagnostics unless explicitly marked ``console=True``.
    Interactive diagnostic commands may opt into their own progress feed.
    Warnings/errors always reach the user.
    """

    def __init__(self, *, verbose: bool, notices_only: bool = True):
        super().__init__()
        self.verbose = verbose
        self.notices_only = notices_only
        self.components = tuple(
            part.strip()
            for part in os.environ.get("PHYSICLAW_LOG_COMPONENTS", "").split(",")
            if part.strip()
        )

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        if not self.verbose:
            visible = getattr(record, "console", None)
            if visible is False:
                return False
            if self.notices_only and visible is not True:
                return False
        if not self.components:
            return True
        return any(
            fnmatchcase(name, pattern) or name.startswith(pattern + ".")
            for pattern in self.components
            for name in (record.name, _component(record.name))
        )


class ConsoleHandler(logging.StreamHandler):
    """Follow stderr redirection without retaining an expired capture stream."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        try:
            write_console(self.format(record) + self.terminator, self.stream)
        except RecursionError:
            raise
        except Exception:
            if self.stream is not None:
                self.handleError(record)


def console_handler(
    tag: str,
    *,
    verbose: bool = False,
    log_format: str | None = None,
    notices_only: bool | None = None,
) -> logging.StreamHandler:
    handler = ConsoleHandler()
    selected = log_format or os.environ.get("PHYSICLAW_LOG_FORMAT", "pretty")
    formatter = (
        JsonLogFormatter(tag)
        if selected.strip().lower() == "json"
        else TaggedFormatter(
            tag, color_enabled(sys.stderr), verbose=verbose, adaptive=True
        )
    )
    handler.setFormatter(formatter)
    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler.addFilter(OwnDebugFilter())
    # JSON is an explicit diagnostic stream. --verbose additionally admits
    # DEBUG records; the task-level filter applies only to normal pretty output.
    handler.addFilter(
        ConsoleFilter(
            verbose=verbose or isinstance(formatter, JsonLogFormatter),
            notices_only=tag not in {"diagnostic", "vision", "setup wizard"}
            if notices_only is None
            else notices_only,
        )
    )
    handler.__dict__["_physiclaw_managed"] = True
    handler.__dict__["_physiclaw_console"] = True
    return handler


def install_handlers(handlers: list[logging.Handler], *, force: bool = False) -> None:
    """Replace our own sinks when embedded; preserve a host's capture handlers."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if force or getattr(handler, "_physiclaw_managed", False):
            root.removeHandler(handler)
            handler.close()
    for handler in handlers:
        handler.__dict__["_physiclaw_managed"] = True
        root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    # Poll/request chatter should not flood saved application diagnostics.
    for name in ("httpx", "httpcore", "httpx2", "httpcore2", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_console(
    tag: str, *, verbose: bool = False, log_format: str | None = None
) -> None:
    """Import-light entry point for standalone diagnostic scripts."""
    install_handlers([console_handler(tag, verbose=verbose, log_format=log_format)])


def add_log_arguments(parser: Any) -> None:
    parser.add_argument(
        "--verbose", action="store_true", help="Show diagnostic details and tracebacks."
    )
    parser.add_argument(
        "--log-format",
        choices=("pretty", "json"),
        default=None,
        help="Diagnostic format (default: PHYSICLAW_LOG_FORMAT or pretty).",
    )


def run_diagnostic(main: Callable[[], int]) -> int:
    """Give standalone diagnostics the same single-record failure behavior."""
    try:
        return main()
    except SystemExit as error:
        if not isinstance(error.code, str):
            raise
        report_error(error.code, tag="diagnostic")
        return 1
    except KeyboardInterrupt:
        logging.getLogger("physiclaw.diagnostic").info(
            "Diagnostic interrupted",
            extra={"event": "diagnostic.interrupted", "exit_code": 130},
        )
        return 130
    except Exception:
        logging.getLogger("physiclaw.diagnostic").exception(
            "Diagnostic failed", extra={"event": "diagnostic.failed", "exit_code": 1}
        )
        return 1


def report_error(message: str, *, tag: str = "cli", exit_code: int = 1) -> None:
    """One expected-error path, recorded to files when logging is configured."""
    root = logging.getLogger()
    if any(getattr(handler, "_physiclaw_console", False) for handler in root.handlers):
        logging.getLogger("physiclaw.cli").error(
            message,
            extra={"event": "process.error", "exit_code": exit_code},
            stacklevel=2,
        )
    else:
        write_console(
            format_console_error(message, tag=tag, exit_code=exit_code) + "\n",
            sys.stderr,
        )
