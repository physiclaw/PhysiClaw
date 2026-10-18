"""Build events: one collector owns terminal output and the JSONL run log.

CAD workers send prefixed JSON over their own stdout pipes. Only the collector
writes the run log, so concurrent workers cannot interleave records. Unprefixed
native/library output is retained as diagnostics, never parsed as build events.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
import traceback
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from hardware.log_style import (
    LEVELS,
    WORKER_PREFIX,
    BuildState,
    ConsoleHandler,
    EventFileHandler,
)

CRASH_LOG_ENV = "PHYSICLAW_HW_CRASH_LOG"


class BuildLog:
    def __init__(self, *, log_format="text", verbose=False, path=None, worker=False):
        self.log_format = log_format
        self.verbose = verbose
        self.worker = worker
        self.started = time.monotonic()
        self.run_id = uuid4().hex[:12]
        self.path = path
        crash_path = os.environ.get(CRASH_LOG_ENV)
        self.crash_path = Path(crash_path) if crash_path is not None else None
        if path is not None:
            self.crash_path = path.with_suffix(".crash.log")
        self._lock = threading.Lock()
        self._sequence = 0
        self._logger = logging.Logger("hardware.build", logging.DEBUG)
        self._logger.propagate = False
        self._console = ConsoleHandler(
            log_format=log_format, verbose=verbose, worker=worker
        )
        self._logger.addHandler(self._console)
        self._complete = set()
        self._successful = set()
        self._total = None
        self._summary = None
        self._tasks = 0
        self.command = "build"

    def __enter__(self):
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = EventFileHandler(self.path)
            # Persist the original event before applying console-only policy.
            self._logger.removeHandler(self._console)
            self._logger.addHandler(file_handler)
            self._logger.addHandler(self._console)
        return self

    def __exit__(self, *exc):
        with self._lock:
            for handler in self._logger.handlers:
                handler.close()

    def emit(self, event, message, *, level="info", **fields):
        self.write(
            {
                **fields,
                "event": event,
                "message": message,
                "level": level,
                "timestamp": datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds"
                ),
                "pid": os.getpid(),
            }
        )

    def write(self, record):
        with self._lock:
            self._sequence += 1
            repeated = (
                record["event"] == "step_finished"
                and record.get("stem") in self._complete
            )
            if record["event"] == "build_summary":
                self._summary = record
            if record["event"] in ("step_started", "step_finished"):
                self._successful.discard(record["stem"])
                if (
                    record["event"] == "step_finished"
                    and record.get("status") == "success"
                ):
                    self._successful.add(record["stem"])
            if (
                record["event"] in ("build_started", "build_plan")
                and record.get("total") is not None
                and "worker_pid" not in record
            ):
                self._total = record["total"]
            if record["event"] in ("step_cached", "annotations_replayed") or (
                record["event"] == "step_finished" and record.get("status") == "success"
            ):
                self._complete.add(record["stem"])
            if self._total is not None and record.get("stem"):
                record = {
                    **record,
                    "completed": len(self._complete),
                    "total": self._total,
                }
            record = {
                **record,
                "command": self.command,
                "schema_version": 1,
                "run_id": self.run_id,
                "sequence": self._sequence,
                "elapsed_s": round(time.monotonic() - self.started, 3),
            }
            entry = self._logger.makeRecord(
                self._logger.name,
                LEVELS.get(record["level"], logging.INFO),
                "",
                0,
                record["message"],
                (),
                None,
                extra={
                    "hardware_event": record,
                    "hardware_state": BuildState(
                        self._total, len(self._complete), self._summary, repeated
                    ),
                },
            )
            # The run owns this logger; host/root verbosity must not discard events.
            self._logger.handle(entry)

    def worker_line(self, line, *, stems, worker_pid):
        if line.startswith(WORKER_PREFIX):
            try:
                record = json.loads(line[len(WORKER_PREFIX) :])
                if isinstance(record, dict) and all(
                    isinstance(record.get(k), str)
                    for k in ("event", "message", "level")
                ):
                    self.write({**record, "worker_pid": worker_pid})
                    return
            except ValueError:
                pass
        self.emit(
            "worker_output",
            line.rstrip("\n"),
            level="debug",
            stems=stems,
            worker_pid=worker_pid,
        )


_current = BuildLog()


def event(name, message, *, level="info", **fields):
    _current.emit(name, message, level=level, **fields)


def worker_line(line, *, stems, worker_pid):
    _current.worker_line(line, stems=stems, worker_pid=worker_pid)


def successful_stems():
    """Snapshot steps whose latest attempt finished successfully."""
    with _current._lock:
        return frozenset(_current._successful)


def add_arguments(
    parser, *, format_help=None, verbose_help="show saved diagnostic events"
):
    """Common presentation options; keep command-specific flags in their CLI."""
    parser.add_argument(
        "--log-format", choices=("text", "json"), default="text", help=format_help
    )
    parser.add_argument("--verbose", action="store_true", help=verbose_help)


class Command:
    def __init__(self, output_dir=None):
        self.output_dir = output_dir
        self.exit_code = 0
        self.summary = ""
        self.details = []

    def artifacts(self, paths):
        paths = list(paths)
        for path in paths:
            event("artifact_written", str(path), level="debug", path=str(path))
        self.summary = f"{len(paths)} {'file' if len(paths) == 1 else 'files'}"


@contextmanager
def command(title, args=None, *, name=None, output_dir=None):
    """One lifecycle for hardware CLIs, including saved errors and cancellation."""
    from hardware.scheme import OUTPUT_DIR

    with session(
        OUTPUT_DIR,
        log_format=getattr(args, "log_format", "text"),
        verbose=getattr(args, "verbose", False),
    ) as log:
        result = Command(output_dir)
        log.command = name or title.removeprefix("Hardware ").lower().replace(" ", "_")
        event("command_started", title, title=title)
        event(
            "log_opened",
            f"Detailed event log: {log.path}",
            level="debug",
            log_path=str(log.path),
        )
        try:
            yield result
        except KeyboardInterrupt:
            result.exit_code = 130
            event(
                "command_interrupted",
                "Interrupted by user",
                level="warning",
                traceback=traceback.format_exc(),
            )
            raise SystemExit(130) from None
        except (Exception, SystemExit) as exc:
            code = exc.code if isinstance(exc, SystemExit) else 1
            result.exit_code = code if isinstance(code, int) else int(bool(code))
            if result.exit_code:
                event(
                    "command_error",
                    str(exc),
                    level="error",
                    traceback=traceback.format_exc(),
                )
            raise SystemExit(result.exit_code) from None
        finally:
            status = (
                "cancelled"
                if result.exit_code == 130
                else "failed"
                if result.exit_code
                else "success"
            )
            event(
                "command_finished",
                status,
                status=status,
                level="error" if status == "failed" else "info",
                exit_code=result.exit_code,
                summary=result.summary,
                details=result.details,
                output_dir=str(result.output_dir) if result.output_dir else None,
                log_path=str(log.path),
            )


@contextmanager
def step(label):
    """Save start/duration details and print one completed task with total elapsed time."""
    started = time.monotonic()
    event("task_started", label, level="debug", item=label)
    try:
        yield
    except BaseException:
        event(
            "task_failed",
            label,
            level="debug",
            item=label,
            duration_s=time.monotonic() - started,
            traceback=traceback.format_exc(),
        )
        raise
    _current._tasks += 1
    event(
        "task_finished",
        label,
        item=label,
        status="success",
        completed=_current._tasks,
        duration_s=time.monotonic() - started,
    )


def run_process(args, *, timeout=None, merge_stderr=True, **kwargs):
    """Stream child diagnostics to the run log and clean up its process group."""
    from hardware.process import ignore_interrupts, interrupt_process

    log = _current
    stdout, stderr = [], []
    reader_errors = []

    def collect(pipe, lines, stream):
        try:
            for line in pipe:
                lines.append(line)
                fields = {"child_pid": child.pid, "stream": stream}
                try:
                    child_event = json.loads(line)
                except ValueError:
                    child_event = None
                if (
                    isinstance(child_event, dict)
                    and "event" in child_event
                    and "schema_version" in child_event
                ):
                    log.emit(
                        "subprocess_event",
                        child_event.get("message", ""),
                        level="debug",
                        child_event=child_event,
                        **fields,
                    )
                else:
                    log.emit(
                        "subprocess_output", line.rstrip("\n"), level="debug", **fields
                    )
        except Exception as exc:
            reader_errors.append(exc)
            child.kill()

    with subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
        **kwargs,
    ) as child:
        readers = [
            threading.Thread(target=collect, args=(child.stdout, stdout, "stdout"))
        ]
        if child.stderr is not None:
            readers.append(
                threading.Thread(target=collect, args=(child.stderr, stderr, "stderr"))
            )
        try:
            for reader in readers:
                reader.start()
            child.wait(timeout=timeout)
        finally:
            with ignore_interrupts():
                interrupt_process(child, group=True, timeout=5)
                for reader in readers:
                    if reader.ident is not None:
                        reader.join()
            log.emit(
                "subprocess_exited",
                str(args[0]),
                level="debug",
                returncode=child.returncode,
                child_pid=child.pid,
            )
        if reader_errors:
            raise reader_errors[0]
        output = "".join(stdout)
        if child.returncode in (130, -signal.SIGINT):
            raise KeyboardInterrupt
        if child.returncode:
            raise subprocess.CalledProcessError(
                child.returncode, args, output=output, stderr="".join(stderr)
            )
        return output


def retain_logs(directory):
    """Keep stage logs after a release's private output tree is removed."""
    if _current.path is None:
        return
    destination = _current.path.parent / f"{_current.path.stem}.children"
    for source in sorted(Path(directory).glob("build-*")):
        if not source.is_file() or source.is_symlink():
            continue
        destination.mkdir(exist_ok=True)
        saved = destination / source.name
        shutil.copy2(source, saved)
        event(
            "diagnostic_saved",
            source.name,
            level="debug",
            source=str(source),
            path=str(saved),
        )


def _save_warning(message, category, filename, lineno, file=None, line=None):
    event(
        "python_warning",
        f"{category.__name__}: {message}",
        level="warning",
        category=category.__name__,
        filename=filename,
        lineno=lineno,
    )


@contextmanager
def session(output_dir: Path, *, log_format="text", verbose=False, worker=False):
    global _current
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = (
        None
        if worker
        else output_dir / "logs" / f"build-{stamp}-{uuid4().hex[:8]}.jsonl"
    )
    previous = _current
    with BuildLog(
        log_format=log_format, verbose=verbose, path=path, worker=worker
    ) as log:
        _current = log
        previous_crash = os.environ.get(CRASH_LOG_ENV)
        if not worker:
            os.environ[CRASH_LOG_ENV] = str(log.crash_path)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("always")
                warnings.showwarning = _save_warning
                yield log
        finally:
            _current = previous
            if previous_crash is None:
                os.environ.pop(CRASH_LOG_ENV, None)
            else:
                os.environ[CRASH_LOG_ENV] = previous_crash
