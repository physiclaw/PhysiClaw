"""Shared log formatting and helpers for PhysiClaw.

Exports:
    setup_logging(tag, level, file_dir=None) — colored TTY-aware
    root-logger config, optionally mirrored to a daily log file.
    logged(fn) — announce an MCP call, save completion timing and failures.
"""

import datetime as dt
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TextIO, TypeVar, cast

from physiclaw.common.file_lock import exclusive_file_lock
from physiclaw.common.log_style import PLAYBOOK_ACCENT as PLAYBOOK_ACCENT
from physiclaw.common.log_style import (
    FailureState,
    color_enabled,
    console_handler,
    format_tool_call,
    install_handlers,
    redact_tool_arguments,
)
from physiclaw.common.log_style import (
    OwnDebugFilter as _OwnDebugFilter,
)
from physiclaw.common.log_style import (
    TaggedFormatter as _TaggedFormatter,
)
from physiclaw.common.log_style import set_tag_accent as set_tag_accent
from physiclaw.common.logger.structured import (
    ANSI_RE as _ANSI_RE,
)

# common.text has no logging/config dependencies.
from physiclaw.common.text import read_text, write_text

log = logging.getLogger("physiclaw.tools")

# Bound tight so @logged on a sync function is a type error, not a
# runtime `TypeError` from awaiting a non-coroutine.
AsyncFn = TypeVar("AsyncFn", bound=Callable[..., Awaitable[Any]])


def _colorize() -> bool:
    return color_enabled(sys.stderr)


# The MCP-server process logs under this tag (both its console `[physiclaw]`
# prefix and its daily-log file name); the server's mcp tee reuses it too so
# the tag lives in one place.
SERVER_LOG_TAG = "physiclaw"


def daily_log_path(log_dir: Path, prefix: str, date: str) -> Path:
    """`<dir>/<prefix>-YYYY-MM-DD.log` — the one daily-log naming scheme,
    shared by the writer (`_DailyFileHandler`) and the reader (retention's
    purge), so the format lives in a single place."""
    return log_dir / f"{prefix}-{date}.log"


def _open_log_file(path: Path) -> TextIOWrapper:
    """Append-open a log file as UTF-8 with LF newlines — the one open every
    file-backed handler here shares. Escape unpaired surrogates so one
    model/subprocess string cannot discard a whole log record."""
    return open(path, "a", encoding="utf-8", errors="backslashreplace", newline="\n")


class _DailyFileHandler(RotatingFileHandler):
    """Mirror log records into `<dir>/<prefix>-YYYY-MM-DD.log`, uncolored.

    Rotate by date and UTF-8 bytes; expire old files at startup and midnight.
    Serialize shared CLI writes with a process lock and close each append
    before releasing it, so another process can rotate the file on Windows.
    """

    def __init__(self, dir: Path, prefix: str, tag: str):
        from physiclaw.common.config import CONFIG
        from physiclaw.common.logger.retention import purge_daily_logs

        self._dir = dir
        self._prefix = prefix
        self._date = dt.datetime.now().strftime("%Y-%m-%d")
        self._log_days = CONFIG.retention.log_days
        self._lock_path = dir / f".{prefix}.log.lock"
        self._rotation_failure = FailureState()
        self._rotation_warnings: list[OSError] = []
        self._purge_pending = False
        dir.mkdir(parents=True, exist_ok=True)
        super().__init__(
            daily_log_path(dir, prefix, self._date),
            maxBytes=CONFIG.retention.log_max_bytes,
            backupCount=CONFIG.retention.log_backup_count,
            encoding="utf-8",
            errors="backslashreplace",
            delay=True,
        )
        self.setFormatter(_TaggedFormatter(tag, color=False, diagnostic=True))
        purge_daily_logs(dir, prefix, self._log_days)

    def _open(self) -> TextIOWrapper:
        return _open_log_file(Path(self.baseFilename))

    def handle(self, record: logging.LogRecord) -> bool:
        handled = super().handle(record)
        # Cleanup and warning logging can re-enter the root logger. Drain
        # their work outside both locks, avoiding handler lock inversion.
        self.acquire()
        try:
            warnings, self._rotation_warnings = self._rotation_warnings, []
            purge, self._purge_pending = self._purge_pending, False
        finally:
            self.release()
        try:
            if purge:
                from physiclaw.common.logger.retention import purge_daily_logs

                purge_daily_logs(self._dir, self._prefix, self._log_days)
            for error in warnings:
                log.warning(
                    "Cannot rotate %s — recording continues in the active file: %s",
                    self.baseFilename,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                    extra={
                        "event": "log.rotation_failed",
                        "console_message": "Debug log rotation failed — recording continues; check disk space and directory permissions",
                    },
                )
        except Exception:
            self.handleError(record)
        return handled

    def emit(self, record: logging.LogRecord) -> None:
        try:
            today = dt.datetime.now().strftime("%Y-%m-%d")
            if today != self._date:
                self.baseFilename = str(
                    daily_log_path(self._dir, self._prefix, today).absolute()
                )
                self._date = today
                self._purge_pending = True
            text = self.format(record) + "\n"
            size = len(text.encode("utf-8", "backslashreplace"))
            rotation_error = None
            with exclusive_file_lock(self._lock_path, timeout=1):
                try:
                    self.stream = self._open()
                    current_size = self.stream.tell()
                    if current_size and current_size + size > self.maxBytes:
                        try:
                            self.doRollover()
                        except OSError as error:
                            # A failed rename must not discard the current event.
                            rotation_error = error
                        self.stream = self._open()
                    self.stream.write(text)
                    self.flush()
                finally:
                    if self.stream is not None:
                        self.stream.close()
                        self.stream = None
            if rotation_error is None:
                self._rotation_failure.recovered()
            elif self._rotation_failure.level(rotation_error) == logging.WARNING:
                self._rotation_warnings.append(rotation_error)
        except Exception:
            self.handleError(record)


def setup_logging(
    tag: str,
    level: int = logging.INFO,
    *,
    file_dir: Path | None = None,
    file_level: int | None = None,
    force: bool = True,
) -> None:
    """Configure the root logger with the colored, tagged format.

    With `file_dir`, records are additionally mirrored (uncolored) to a
    daily `<tag>-YYYY-MM-DD.log` there — used by the runtime so wake
    decisions and poll errors survive for post-mortems. `file_level`
    overrides the daily file's threshold (defaults to `level`): the
    server passes DEBUG so its camera/exposure/tune detail is on disk
    regardless of console verbosity. File-handler setup is fail-open:
    logging must never take the process down.

    The root logger is kept permissive (DEBUG); each handler gates its
    own verbosity via its own level. This is what lets a per-session
    capture handler (`attach_session_log`) — or a DEBUG daily file —
    record full DEBUG while the console stays at `level`; with a NOTSET
    root the console would print DEBUG the moment any physiclaw logger
    emitted it."""
    stream = console_handler(tag, verbose=level <= logging.DEBUG, notices_only=True)
    stream.setLevel(level)
    handlers: list[logging.Handler] = [stream]
    if file_dir is not None:
        try:
            fh = _DailyFileHandler(file_dir, tag, tag)
            fh.setLevel(level if file_level is None else file_level)
            fh.addFilter(_OwnDebugFilter())  # gates third-party DEBUG when file<INFO
            handlers.append(fh)
        except OSError:
            pass  # unwritable dir → stderr-only; better than not starting
    install_handlers(handlers, force=force)
    selected = os.environ.get("PHYSICLAW_LOG_FORMAT", "").strip().lower()
    if selected not in {"", "pretty", "json"}:
        log.warning(
            "Unknown PHYSICLAW_LOG_FORMAT=%r; using pretty (choose pretty or json)",
            selected,
        )


class _SessionCaptureHandler(logging.StreamHandler):
    """DEBUG mirror of the process log into one flat per-session file
    (LF newlines, uncolored). Owns its file handle so it closes cleanly
    when detached (a bare StreamHandler would leave the file open). The
    runtime process is the only caller, so the tag is fixed."""

    def __init__(self, path: Path):
        self._file = _open_log_file(path)
        super().__init__(self._file)
        self.setFormatter(_TaggedFormatter("runtime", color=False, diagnostic=True))
        self.setLevel(logging.DEBUG)
        self.addFilter(_OwnDebugFilter())

    def emit(self, record: logging.LogRecord) -> None:
        # Drop records once the handle is closed — logging.shutdown() at exit
        # closes it while other threads may still log. The session is over, so
        # (unlike the long-lived daily file) there's nothing to reopen for.
        if not self._file.closed:
            super().emit(record)

    def close(self) -> None:
        try:
            super().close()
        finally:
            try:
                if not self._file.closed:
                    self._file.close()
            except OSError:
                pass


def attach_session_log(path: Path) -> logging.Handler | None:
    """Mirror the process log stream into `path` for one session's lifetime.

    Adds a DEBUG-level handler to the root logger that captures
    ``physiclaw.*`` records (down to DEBUG) plus any INFO+ record —
    tracebacks and warnings included — so a shared session dir is
    self-contained. Returns the handler to pass to `detach_session_log`,
    or None if the file couldn't be opened (fail-open: the capture is
    best-effort and never blocks the session). Requires the permissive
    (DEBUG) root that `setup_logging` installs."""
    try:
        handler: logging.Handler = _SessionCaptureHandler(path)
    except OSError:
        return None
    logging.getLogger().addHandler(handler)
    return handler


def detach_session_log(handler: logging.Handler | None) -> None:
    """Remove and close a handler returned by `attach_session_log`. No-op
    on None (the open failed) — so callers store-and-detach unconditionally."""
    if handler is None:
        return
    logging.getLogger().removeHandler(handler)
    try:
        handler.close()
    except OSError:
        pass


# ---------- per-session log sidecars (cross-process) ----------
#
# runtime.log mirrors the runtime process's own log stream (attach_session_log
# above). mcp.log carries the MCP-SERVER process's log for the session — but
# the server is a separate process, so the two can't share an in-memory object.
# Instead the agent publishes the active session ID to a marker file, and the
# server's tee (`attach_server_mcp_tee`) reads it, resolves the id to its
# session dir, and writes mcp.log there live.


def _publish_active_session(sid: str) -> None:
    """Point the cross-process marker at session `sid` so the server's mcp
    tee writes into that session's dir. Fail-open — best-effort."""
    from physiclaw.common import paths

    marker = paths.active_session_marker()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        write_text(marker, sid)
    except (OSError, UnicodeError):
        log.debug("active-session marker write failed", exc_info=True)


def _clear_active_session(sid: str) -> None:
    """Remove the marker if it still names this session — guards against
    clobbering a newer session's marker. Fail-open."""
    from physiclaw.common import paths

    marker = paths.active_session_marker()
    try:
        if read_text(marker).strip() == sid:
            marker.unlink()
    except (OSError, UnicodeError):
        pass


def _active_session_dir(sid: str) -> Path | None:
    """Resolve the active engine session id to its on-disk directory."""
    from physiclaw.common import paths

    if not sid or sid in {".", ".."} or any(c in sid for c in "/\\:\0"):
        return None
    directory = paths.engine_sessions_dir() / sid
    return directory if directory.is_dir() else None


class SessionLogSidecars:
    """The per-session log sidecars owned by the agent session writers:
    `runtime.log` (a mirror of the runtime process log stream) and the
    active-session marker (the session id) that steers the server's mcp.log
    tee into this session's dir. One home for both so they can't drift."""

    def __init__(self, session_dir: Path):
        self._sid = session_dir.name
        self._runtime = attach_session_log(session_dir / "runtime.log")
        _publish_active_session(self._sid)

    def close(self) -> None:
        # Stop steering server logs here first, then detach the runtime
        # mirror last so any close-time warning is still captured.
        _clear_active_session(self._sid)
        detach_session_log(self._runtime)


class _SessionMcpTee(logging.Handler):
    """Server-side: tee the server's own log into the ACTIVE session's
    mcp.log. The agent publishes the active session id to a marker file
    (`_publish_active_session`); this follows it — resolving the id to its
    dir and repointing whenever the id changes — so the server's camera/
    exposure/tune detail lands live in the running session's dir. Between
    sessions (no marker) records are dropped: no session. Fail-open."""

    def __init__(self) -> None:
        from physiclaw.common import paths

        super().__init__(level=logging.DEBUG)
        self.setFormatter(
            _TaggedFormatter(SERVER_LOG_TAG, color=False, diagnostic=True)
        )
        self.addFilter(_OwnDebugFilter())
        self._marker = paths.active_session_marker()  # process-invariant
        self._sid = ""
        self._f: TextIO | None = None

    def _refresh(self) -> None:
        """Repoint the mcp.log handle when the marker names a different session.
        Keying on the session id (not the marker's mtime) so a new session is
        followed even when two markers land in the same mtime tick; the id is
        resolved again when an earlier open failed."""
        try:
            sid = read_text(self._marker).strip()
        except (OSError, UnicodeError):
            sid = ""
        if sid == self._sid and (not sid or self._f is not None):
            return
        self._sid = sid
        self._close_file()
        target = _active_session_dir(sid) if sid else None
        if target is not None:
            try:
                self._f = _open_log_file(target / "mcp.log")
            except OSError:
                self._f = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._refresh()
            if self._f is not None:
                self._f.write(self.format(record) + "\n")
                self._f.flush()
        except Exception:
            self.handleError(record)

    def _close_file(self) -> None:
        if self._f is not None and not self._f.closed:
            try:
                self._f.close()
            except OSError:
                pass
        self._f = None

    def close(self) -> None:
        self._close_file()
        super().close()


def attach_server_mcp_tee() -> logging.Handler:
    """Server-side: attach the handler that tees the server's own log into
    the agent's active session dir as `mcp.log`. Call once after
    `setup_logging` in the MCP-server process (the runtime process uses
    `SessionLogSidecars` to publish which session is active)."""
    handler = _SessionMcpTee()
    logging.getLogger().addHandler(handler)
    return handler


def make_tagged_logger(
    name: str, tag: str, level: int = logging.INFO
) -> logging.Logger:
    """A standalone logger that emits in the ``[tag]`` format, independent of
    the root handler (``propagate=False``) so its lines carry ``tag`` rather
    than the process-wide one. Lets a sub-stream — e.g. the setup wizard's
    output under ``physiclaw auto`` — be badged distinctly in interleaved
    logs. Idempotent: re-calling returns the same logger without stacking
    handlers."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = console_handler(
            tag, verbose=level <= logging.DEBUG, notices_only=False
        )
        handler.setLevel(logging.NOTSET)  # The standalone logger owns its level.
        logger.addHandler(handler)
    return logger


class LineLogStream:
    """A line-buffered file-like object that re-emits written text through a
    logger, one record per line. Pair with ``contextlib.redirect_stdout`` to
    fold a component's ``print()`` output into the tagged log stream — e.g.
    the setup wizard under ``physiclaw auto``. ANSI escapes are stripped (the
    formatter re-colours) and blank lines dropped (they'd render as an empty
    tagged prefix)."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)
        return len(s)

    def flush(self) -> None:
        line, self._buf = self._buf, ""
        self._emit(line)

    def _emit(self, line: str) -> None:
        line = _ANSI_RE.sub("", line).rstrip()
        if line:
            self._logger.info(line)


# Caps a tool-call log line so a 100KB clipboard body doesn't flood it.
_MAX_ARG_LOG_LEN = 80


def _format_args(fn_name: str, kwargs: dict) -> str:
    """Redact clipboard text (IM bodies, search queries, anything pasted)
    and summarize sequence steps to tool names only, since a step may
    itself be a send_to_clipboard."""
    if fn_name == "send_to_clipboard":
        return f"text={redact_tool_arguments(fn_name, kwargs)['text']}"
    if fn_name == "sequence":
        # The schema's single param is a LIST of step dicts:
        # actions=[{...}, ...] — scanning kwargs.values() for dicts finds
        # nothing and logged every batch as "0 steps".
        actions = kwargs.get("actions") or []
        names = [
            s.get("tool_name", "?") if isinstance(s, dict) else "?" for s in actions
        ]
        return f"{len(names)} steps: {', '.join(names)}"
    arg_str = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    if len(arg_str) > _MAX_ARG_LOG_LEN:
        arg_str = arg_str[: _MAX_ARG_LOG_LEN - 3] + "..."
    return arg_str


def logged(fn: AsyncFn) -> AsyncFn:
    """Announce each MCP call once; save its completion timing on disk.

    Success timings are DEBUG (the runtime/client displays the action result).
    Failures are WARNING with the exception type, message and a saved traceback.
    Cancellation
    (BaseException) propagates unlogged — it isn't a tool completion.
    Formatting or sink failures never replace the tool's result/error."""

    # MCPServer dispatches tool calls with keyword args only (positional
    # args land in `args` but never in practice); the log reads kwargs.
    @wraps(fn)
    async def wrapper(*args, **kwargs):
        t0 = time.monotonic()
        try:
            if log.isEnabledFor(logging.INFO):
                log.info(
                    "tool %s(%s) started",
                    fn.__name__,
                    _format_args(fn.__name__, kwargs),
                    extra={
                        "event": "tool.started",
                        "tool": fn.__name__,
                        "console": True,
                        "console_message": "Tool: "
                        + format_tool_call(fn.__name__, kwargs),
                    },
                )
        except Exception:
            pass  # Presentation must never prevent an action.
        try:
            result = await fn(*args, **kwargs)
        except Exception as e:
            _log_completion(fn.__name__, kwargs, t0, e)
            raise
        _log_completion(fn.__name__, kwargs, t0)
        return result

    return cast(AsyncFn, wrapper)


def _log_completion(
    name: str, kwargs: dict, started: float, error: Exception | None = None
) -> None:
    try:
        level = logging.WARNING if error is not None else logging.DEBUG
        if not log.isEnabledFor(level):
            return
        elapsed = time.monotonic() - started
        # The call was already announced. Keep completion timing on disk
        # without echoing every successful action in the terminal.
        log.log(
            level,
            "tool %s(%s) — %.1fs%s",
            name,
            _format_args(name, kwargs),
            elapsed,
            f" FAILED: {type(error).__name__}: {error}" if error is not None else "",
            exc_info=(type(error), error, error.__traceback__)
            if error is not None
            else None,
            extra={
                "console": False,
                "console_traceback": False,
                "console_message": f"Tool {name} failed ({elapsed:.1f}s)"
                if error is not None
                else "",
                "event": "tool.failed" if error is not None else "tool.completed",
                "tool": name,
                "duration_ms": int(elapsed * 1000),
            },
        )
    except Exception:
        # Reporting is best-effort; retrying the log here could recurse.
        pass
