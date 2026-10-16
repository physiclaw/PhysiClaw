"""Build events: one collector owns terminal output and the JSONL run log.

CAD workers send prefixed JSON over their own stdout pipes. Only the collector
writes the run log, so concurrent workers cannot interleave records. Unprefixed
native/library output is retained as diagnostics, never parsed as build events.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import traceback
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from hardware.scheme import REPO_ROOT

WORKER_PREFIX = "PHYSICLAW_BUILD_EVENT "
CRASH_LOG_ENV = "PHYSICLAW_HW_CRASH_LOG"
_CONTEXT_WIDTH = 26


def _single_line(value: object) -> str:
    # Escape control characters (including ANSI escapes) from library errors.
    return "".join(
        char if char.isprintable() else f"\\x{ord(char):02x}" for char in str(value)
    )


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
        progress = (
            f"[{record['completed']}/{record['total']}]"
            if record.get("total") is not None
            else f"[{record['completed']}]"
        )
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
        and record.get("event") not in ("build_finished", "command_finished")
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
        self._file = None
        self._complete = set()
        self._successful = set()
        self._total = None
        self._summary = None
        self._tasks = 0
        self.command = "build"

    def __enter__(self):
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("x", encoding="utf-8")
        return self

    def __exit__(self, *exc):
        if self._file is not None:
            self._file.close()

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
            encoded = json.dumps(record, ensure_ascii=False)
            if self._file is not None:
                self._file.write(encoded + "\n")
                self._file.flush()
            if self.worker:
                print(WORKER_PREFIX + encoded, flush=True)
            elif self.log_format == "json":
                print(encoded, flush=True)
            else:
                terminal = self._terminal_record(record, repeated=repeated)
                if terminal is not None:
                    print(self._render_terminal(terminal), flush=True)

    def _render_terminal(self, record):
        color = (
            sys.stdout.isatty()
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
        )

        def style(text, code):
            return f"\033[{code}m{text}\033[0m" if color else text

        columns = max(24, shutil.get_terminal_size(fallback=(120, 24)).columns)
        level_width = 8 if self.verbose else 0
        progress_width, context_width = _table_widths(
            columns - level_width, self._total
        )
        table_width = level_width + progress_width + 1 + context_width + 2 + 8
        rule = style("-" * min(table_width, columns), "2")
        name = record["event"]
        if not self.verbose and name in (
            "build_started",
            "build_plan",
            "command_started",
        ):
            count = self._total
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

        text = _text_lines({"total": self._total, **record}, show_level=self.verbose)
        if record["level"] in ("error", "critical"):
            text = style(text, "1;31")
        elif name in ("build_finished", "command_finished"):
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
        if name in ("build_finished", "command_finished"):
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

    def _terminal_record(self, record, *, repeated):
        # Presentation only: the original event has already been saved in full.
        if record["level"] == "warning" or record["event"] == "worker_running":
            return None
        name = record["event"]
        if self.verbose and name not in ("build_finished", "command_finished"):
            return record
        if record["level"] == "debug" or name in (
            "log_opened",
            "outputs_cleared",
            "build_summary",
        ):
            return None
        if name == "step_finished" and repeated and record.get("status") == "success":
            return None
        if name == "build_started" and not record.get("stems"):
            return None  # The dispatcher prints its plan once it knows the total.
        message = record["message"]
        if name == "command_error":
            message = message.splitlines()[0] if message else "Command failed"
        elif name == "cache_checked":
            if not record["cached"]:
                return None
            message = f"{record['cached']} cached; {record['to_build']} to build"
        elif name in ("build_finished", "command_finished"):
            message = record["status"].upper()
            details = list(record.get("details", []))
            if record.get("summary"):
                message += f"  {record['summary']}"
            if self._summary is not None:
                summary = self._summary
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
            elif self._total is not None:
                message += f"  {len(self._complete)}/{self._total} complete"
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
