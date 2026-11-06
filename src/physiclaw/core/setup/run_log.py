"""Server-owned setup journals, exact input images, and per-attempt diagnostics.

One rig owns one current setup run. Retrying a step appends a new attempt;
only confirmed readiness completes the run. Everything is local to the server
under log/setup/<run-id>/, independent of the wizard's lifetime.
"""

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import math
import os
import secrets
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from physiclaw.common import paths
from physiclaw.common.config import CONFIG
from physiclaw.common.logger import (
    bind_log_context,
    ensure_readme,
    env_snapshot,
    reset_log_context,
)
from physiclaw.common.logger.evidence import current_evidence, evidence_scope
from physiclaw.common.logger.retention import purge_old_sessions
from physiclaw.common.logger.session_artifacts import write_json_atomic
from physiclaw.common.text import append_text
from physiclaw.core.calibration._common import PreconditionError
from physiclaw.core.hardware.device import DeviceNotFound
from physiclaw.core.setup.errors import ManualPositionRequired

log = logging.getLogger(__name__)

README = """# Setup diagnostics

Each directory is one server-owned setup run, named
YYYYMMDD-HHMMSS-<6 hex digits> in server local time, matching agent sessions.
Retries stay in that run until the server confirms readiness. A later setup
starts a new directory.

Start with summary.json: check recording_complete and recording_errors first.
attention lists current failures and advisories, and
steps links to each latest attempt's summary and journal. Read the attempt's
summary.json for its result/error, timings and warning messages, then its
events.jsonl for measurements, traceback and artifact.saved image paths.
diagnostics contains summarized decisions with status, reason, thresholds and
evidence references (seq, frame_id, input_artifact_id and input_path). Failed
diagnostics appear in attention even when an optional check falls back and the
step succeeds.
For focus, camera.focus_search_result links the peak sample and confirmation
samples, states the search stage and failed gate, and records autofocus cleanup.
camera.focus_sample labels coarse/refinement, lens-response and final
verification readings. A frame_id in a decision's peak_sample is the peak
image; the decision event's own frame_id is only the most recent input.
All paths in these JSON files are relative to the run directory. An images
directory is absent when the attempt captured no images. Earlier attempt
summaries remain beside the latest one; a successful retry does not erase them.
Warning message previews are capped at 20; the journal retains every message.
Ignore hidden OS files such as .DS_Store.
The root events.jsonl joins all attempts with background camera
and phone-bridge diagnostics in sequence order. run.json records environment,
camera settings, schema version and capture limits. No browser logging is needed.
Run status is in_progress, succeeded or interrupted. Individual steps can be
running, succeeded, failed, blocked, waiting_for_input or interrupted.

Events use UTC ISO timestamps, a run id and sequence number, and step/attempt
ids where known. step.started and step.ended include calibration state;
step.exception preserves the traceback; step.result includes unsuccessful
validation/shortcut results even when the HTTP request itself succeeded.
Measurements include expected values, actual observations, thresholds and units.
For AT failures, assistive_touch.tap_requested records the press duration and
assistive_touch.screenshot_wait records the deadline, elapsed time, phone polling
and page touches. Background bridge.screenshot_upload_started/accepted and
bridge.request_rejected logs distinguish a missing upload from a rejected one.
An acknowledged arm command does not prove that iOS registered the touch.
input_artifact_id identifies the most recent captured input at an event. Use
detector/measurement events to find the failing input; later cleanup checkpoints
retain their own images and never replace earlier artifacts. artifact.saved
contains a relative path and SHA-256; artifact.queued alone does not prove a save.

Camera PNGs preserve decoded, unrotated BGR pixels before masks, crops, rotation
or annotations. They are not sensor RAW files. Phone uploads retain their exact
original bytes, including images that fail decoding. Recording covers consumed
setup inputs, not continuous video. Images are encoded by a bounded background
writer. A full queue, the per-run image budget or an I/O error is explicit;
recording_complete=false must never be interpreted as complete evidence.

Manual rig placement is not observable by the server. A requested phone phase
is not proof that the phone rendered it. Background events have run scope rather
than an invented step association. Logs establish observations and failed gates;
they cannot prove an unmeasured physical cause, such as a loose cable.

Setup completes at confirmed readiness. Earlier checks may have been reused;
inspect recorded attempts and initial state rather than assuming every step ran.
Warnings can accompany readiness: inspect their details.
An unclosed step after a process crash has an unknown outcome; an in-progress
run may instead be waiting for input. Clean server shutdown records interruption.
Retention follows retention.trace_days. Images may show the phone screen;
inspect a run before sharing it.
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _failure_status(error: BaseException) -> str:
    if isinstance(error, ManualPositionRequired):
        return "waiting_for_input"
    if isinstance(error, (PreconditionError, DeviceNotFound)):
        return "blocked"
    if isinstance(error, Exception):
        return "failed"
    return "interrupted"


def _json_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_value(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if type(value).__module__.startswith("numpy") and hasattr(value, "tolist"):
        return _json_value(value.tolist())
    return str(value)


class _SetupLogs(logging.Handler):
    _physiclaw_managed = True

    def __init__(self, run: "SetupRecorder") -> None:
        super().__init__(logging.DEBUG)
        self.run = run
        self.run_id = run.run_id

    def handle(self, record: logging.LogRecord) -> bool:
        # emit serializes with the journal lock. A second handler lock would
        # invert that order when a disk error logs while holding the journal.
        if self.filter(record):
            self.emit(record)
        return True

    def emit(self, record: logging.LogRecord) -> None:
        sink = current_evidence()
        attempt = (
            sink if isinstance(sink, SetupAttempt) and sink.run is self.run else None
        )
        background = sink is None and record.name.startswith(
            ("physiclaw.core.hardware.", "physiclaw.core.bridge.")
        )
        if attempt is None and not background:
            return
        with self.run.lock:
            # A logging call may retain this handler while it is detached.
            # It must not append to a completed or subsequently rotated run.
            if self.run_id != self.run.run_id or (
                self.run.closed and not self.run.active
            ):
                return
            try:
                self._emit(record, attempt)
            except Exception as exc:
                self.run.recording_failure(exc)

    def _emit(self, record: logging.LogRecord, attempt: "SetupAttempt | None") -> None:
        fields: dict[str, Any] = dict(
            level=record.levelname.lower(),
            logger=record.name,
            message=record.getMessage(),
            source_event=getattr(record, "event", None),
            device_id=getattr(record, "device_id", None),
            source_timestamp=dt.datetime.fromtimestamp(
                record.created, dt.timezone.utc
            ).isoformat(timespec="milliseconds"),
            pid=record.process,
            thread=record.threadName,
            source_file=record.pathname,
            source_line=record.lineno,
            exception="".join(traceback.format_exception(*record.exc_info))
            if record.exc_info
            else None,
        )
        if attempt is not None:
            if record.levelno >= logging.WARNING:
                attempt.warnings += 1
                if len(attempt.warning_messages) < 20:
                    attempt.warning_messages.append(fields["message"])
            attempt.event("diagnostic.log", **fields)
        else:
            # Reader/phone HTTP tasks outlive individual request contexts.
            # Preserve them at run scope; don't pretend to know their step.
            self.run.emit("diagnostic.log", scope="background", **fields)


class SetupAttempt:
    def __init__(self, run: "SetupRecorder", step: str, number: int) -> None:
        self.run = run
        self.step = step
        self.number = number
        self.relative = Path("steps") / step / f"attempt-{number:03d}"
        self.paths = {
            "summary_path": str(self.relative / "summary.json"),
            "events_path": str(self.relative / "events.jsonl"),
            "images_dir": str(self.relative / "images"),
        }
        self.latest_frame: str | None = None
        self.latest_input: str | None = None
        self.latest_input_path: str | None = None
        self.warnings = 0
        self.warning_messages: list[str] = []
        self.diagnostics: list[dict] = []
        self.result: Any = None
        self.futures: list[Future] = []

    def event(self, event: str, *, summarize: bool = False, **fields: Any) -> dict:
        fields.setdefault("frame_id", self.latest_frame)
        fields.setdefault("input_artifact_id", self.latest_input)
        fields.update(
            step=self.step,
            attempt=self.number,
        )
        with self.run.lock:
            seq = self.run.emit(event, self.relative, **fields)
            reference = {
                "seq": seq,
                "frame_id": fields["frame_id"],
                "input_artifact_id": fields["input_artifact_id"],
                "input_path": self.latest_input_path
                if fields["input_artifact_id"] == self.latest_input
                else None,
            }
            if summarize:
                self.diagnostics.append({"event": event, **fields, **reference})
            return reference

    def recording_failure(self, exc: Exception) -> None:
        self.run.recording_failure(exc)

    def frame(self, frame: Any, **fields: Any) -> None:
        self.latest_frame = secrets.token_hex(8)
        self.latest_input = self.latest_frame
        self.latest_input_path = None
        if frame is None:
            self.event("camera.frame_missing", **fields)
            return
        self._submit(self.latest_frame, frame, "camera", ".png", **fields)

    def upload(self, data: bytes, **fields: Any) -> None:
        artifact_id = secrets.token_hex(8)
        self.latest_input = artifact_id
        self.latest_input_path = None
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif data.startswith(b"\xff\xd8"):
            suffix = ".jpg"
        else:
            suffix = ".bin"
        self._submit(artifact_id, data, "phone_screenshot", suffix, **fields)

    def _submit(
        self, artifact_id: str, value: Any, kind: str, suffix: str, **fields: Any
    ) -> None:
        run = self.run
        if not run.slots.acquire(blocking=False):
            run.recording_failure(OSError("setup image writer queue full"))
            self.event(
                "artifact.skipped", artifact_id=artifact_id, reason="writer_queue_full"
            )
            return
        relative = self.relative / "images" / f"{artifact_id}{suffix}"
        self.latest_input_path = str(relative)
        # Freeze capture metadata here, before a later frame changes the scope.
        captured = {
            **fields,
            "step": self.step,
            "attempt": self.number,
            "artifact_id": artifact_id,
            "path": str(relative),
            "kind": kind,
        }

        def save() -> None:
            tmp: Path | None = None
            reserved_bytes = 0
            try:
                import cv2

                if kind == "camera":
                    ok, encoded = cv2.imencode(".png", value)
                    if not ok:
                        raise OSError("PNG encoder returned no image")
                    data = encoded.tobytes()
                else:
                    data = value
                with run.lock:
                    if run.image_bytes + len(data) > run.MAX_IMAGE_BYTES:
                        run.recording_failure(
                            OSError("setup run image budget exceeded")
                        )
                        run.emit(
                            "artifact.skipped",
                            self.relative,
                            **captured,
                            reason="run_image_budget",
                        )
                        return
                    run.image_bytes += len(data)
                    reserved_bytes = len(data)
                if run.directory is None:
                    raise OSError("setup run has no artifact directory")
                path = run.directory / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
                # Use the captured id, never the latest frame at write time.
                run.emit(
                    "artifact.saved",
                    self.relative,
                    **captured,
                    bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            except Exception as exc:
                with run.lock:
                    run.image_bytes -= reserved_bytes
                run.recording_failure(exc)
                run.emit(
                    "artifact.failed",
                    self.relative,
                    **captured,
                    exception_type=type(exc).__name__,
                    message=str(exc),
                )
            finally:
                if tmp is not None:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                run.slots.release()

        try:
            if kind == "camera":
                # Reserve capacity before copying, but freeze pixels before
                # returning to the detector. Encode/write in the worker.
                value = value.copy()
                captured.update(
                    shape=list(value.shape), dtype=str(value.dtype), color_space="BGR"
                )
            self.event("artifact.queued", **captured)
            self.futures.append(run.writer.submit(save))
        except Exception as exc:
            run.slots.release()
            run.recording_failure(exc)
            self.event(
                "artifact.failed",
                **captured,
                exception_type=type(exc).__name__,
                message=str(exc),
            )


class SetupRecorder:
    # Bounds apply only to setup evidence, never to the camera reader. Missing
    # evidence is counted and reported rather than silently dropping images.
    MAX_IMAGE_BYTES = 512 * 1024 * 1024

    def __init__(self, snapshot: Callable[[], dict]) -> None:
        self.snapshot = snapshot
        self.lock = threading.RLock()
        self.directory: Path | None = None
        self.run_id: str | None = None
        self.closed = True
        self.incomplete = 0
        self.image_bytes = 0
        self.sequence = 0
        self.attempts: dict[str, int] = {}
        self.steps: dict[str, dict] = {}
        self.active = 0
        self.completion_requested = False
        self.stopped = False
        self.handler: _SetupLogs | None = None
        self.recording_errors: list[dict] = []
        self.slots = threading.BoundedSemaphore(8)
        self.writer = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="setup-images"
        )

    def recording_failure(self, exc: Exception) -> None:
        with self.lock:
            self.incomplete += 1
            first_failure = self.incomplete == 1
            if len(self.recording_errors) < 20:
                self.recording_errors.append(
                    {"type": type(exc).__name__, "message": str(exc)}
                )
        # Avoid recursively feeding a broken journal through its log handler.
        if first_failure:
            log.warning("Setup evidence is incomplete: %s", exc)

    def _start(self) -> None:
        root = paths.setup_log_dir()
        self.directory = (
            root / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        )
        self.run_id = self.directory.name
        self.closed = False
        self.incomplete = self.image_bytes = self.sequence = 0
        self.attempts = {}
        self.steps = {}
        self.recording_errors = []
        self.completion_requested = False
        try:
            purge_old_sessions(root, days=CONFIG.retention.trace_days)
            self.directory.mkdir(parents=True, exist_ok=False)
            ensure_readme(root, README)
            ensure_readme(self.directory, README)
            write_json_atomic(
                self.directory / "run.json",
                {
                    "schema_version": 1,
                    "run_id": self.run_id,
                    "started_at": _now(),
                    "pid": os.getpid(),
                    "environment": env_snapshot(),
                    "manual_steps": {"position_rig": "not_observable_by_server"},
                    "capture_policy": "all frames consumed by setup; lossless decoded BGR, not sensor RAW",
                    "max_image_bytes": self.MAX_IMAGE_BYTES,
                },
            )
        except Exception as exc:
            self.recording_failure(exc)
        self.emit("setup.started")
        self.emit("setup.initial_state", state=self._state())
        self.handler = _SetupLogs(self)
        logging.getLogger().addHandler(self.handler)
        log.info("Setup evidence: %s", self.directory, extra={"console": True})

    def begin(self) -> None:
        """Opening the wizard starts observation, including phone connection.

        A refresh of a completed wizard is observational; only another setup
        operation starts a new run after readiness.
        """
        with self.lock:
            if self.run_id is None and not self.stopped:
                self._start()
                self._write_summary("in_progress")

    def emit(
        self, event: str, relative: Path | None = None, **fields: Any
    ) -> int | None:
        with self.lock:
            if self.directory is None:
                return None
            self.sequence += 1
            payload = {
                "schema_version": 1,
                "run_id": self.run_id,
                "seq": self.sequence,
                "timestamp": _now(),
                "event": event,
                **fields,
            }
            try:
                line = (
                    json.dumps(_json_value(payload), ensure_ascii=True, allow_nan=False)
                    + "\n"
                )
                self.directory.mkdir(parents=True, exist_ok=True)
                append_text(self.directory / "events.jsonl", line)
                if relative is not None:
                    folder = self.directory / relative
                    folder.mkdir(parents=True, exist_ok=True)
                    append_text(folder / "events.jsonl", line)
                return self.sequence
            except Exception as exc:
                self.recording_failure(exc)
                return None

    def _state(self) -> dict:
        try:
            return _json_value(self.snapshot())
        except Exception as exc:
            self.recording_failure(exc)
            return {"snapshot_error": str(exc)}

    def _write_summary(self, status: str) -> None:
        if self.directory is None:
            return
        try:
            write_json_atomic(
                self.directory / "summary.json",
                {
                    "schema_version": 1,
                    "run_id": self.run_id,
                    "updated_at": _now(),
                    "status": status,
                    "steps": self.steps,
                    "attention": [
                        {"step": step, **entry}
                        for step, entry in self.steps.items()
                        if entry["status"]
                        in ("failed", "blocked", "waiting_for_input", "interrupted")
                        or entry.get("warnings")
                        or entry.get("issues")
                        or any(
                            d.get("status") == "failed"
                            for d in entry.get("diagnostics", [])
                        )
                    ],
                    "recording_complete": self.incomplete == 0,
                    "recording_errors_or_skips": self.incomplete,
                    "recording_errors": self.recording_errors,
                    "image_bytes": self.image_bytes,
                },
            )
        except Exception as exc:
            self.recording_failure(exc)

    def _update_attempt(
        self, attempt: SetupAttempt, entry: dict, **details: Any
    ) -> None:
        """Publish the attempt and latest-step index under the caller's lock."""
        if self.directory is not None:
            try:
                path = self.directory / attempt.paths["summary_path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                write_json_atomic(
                    path,
                    {
                        "schema_version": 1,
                        "run_id": self.run_id,
                        "step": attempt.step,
                        "updated_at": _now(),
                        **entry,
                        **self._json_details(details),
                    },
                )
            except Exception as exc:
                self.recording_failure(exc)
        if attempt.number == self.attempts[attempt.step]:
            self.steps[attempt.step] = entry

    @contextmanager
    def operation(
        self, step: str, parameters: dict | None = None
    ) -> Iterator[SetupAttempt | None]:
        with self.lock:
            stopped = self.stopped
            if not stopped:
                if self.closed and not self.active:
                    self._start()
                number = self.attempts.get(step, 0) + 1
                self.attempts[step] = number
                self.active += 1
        if stopped:
            yield None
            return
        attempt = SetupAttempt(self, step, number)
        token = bind_log_context(setup_run_id=self.run_id, step=step, attempt=number)
        started = time.monotonic()
        status = "failed"
        failure: dict = {}
        with evidence_scope(attempt):
            try:
                attempt.event(
                    "step.started", parameters=parameters or {}, state=self._state()
                )
                with self.lock:
                    entry = {"attempt": number, "status": "running", **attempt.paths}
                    self._update_attempt(attempt, entry, parameters=parameters or {})
                    self._write_summary("in_progress")
                yield attempt
                result = attempt.result
                failed = isinstance(result, dict) and (
                    result.get("status") == "error"
                    or any(
                        result.get(key) is False
                        for key in ("passed", "calibrated", "ready")
                    )
                )
                status = "failed" if failed else "succeeded"
                if failed:
                    failure = {"result": result}
                attempt.event("step.result", status=status, result=result)
            except BaseException as exc:
                failure = {"exception_type": type(exc).__name__, "message": str(exc)}
                status = _failure_status(exc)
                attempt.event(
                    "step.exception",
                    status=status,
                    **failure,
                    traceback=traceback.format_exc(),
                )
                raise
            finally:
                # The physical operation/cleanup has finished before waiting
                # for encoding. Never flush images before parking the stylus.
                work_duration_ms = round((time.monotonic() - started) * 1000)
                for future in attempt.futures:
                    try:
                        future.result()
                    except Exception as exc:
                        self.recording_failure(exc)
                attempt.event(
                    "step.ended",
                    status=status,
                    state=self._state(),
                    work_duration_ms=work_duration_ms,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                with self.lock:
                    outcome = self._json_details({"result": attempt.result, **failure})
                    result = outcome.get("result")
                    entry = {
                        "attempt": number,
                        "status": status,
                        "warnings": attempt.warnings,
                        "warning_messages": attempt.warning_messages,
                        "issues": result.get("issues", [])
                        if isinstance(result, dict)
                        else [],
                        **attempt.paths,
                        **(
                            self._json_details({"diagnostics": attempt.diagnostics})
                            if attempt.diagnostics
                            else {}
                        ),
                        **(outcome if failure else {}),
                    }
                    self._update_attempt(
                        attempt,
                        entry,
                        **outcome,
                        parameters=parameters or {},
                        work_duration_ms=work_duration_ms,
                        duration_ms=round((time.monotonic() - started) * 1000),
                    )
                    self.active -= 1
                    self.completion_requested |= (
                        step == "finish.ready"
                        and status == "succeeded"
                        and isinstance(attempt.result, dict)
                        and attempt.result.get("ready") is True
                    )
                    complete = (
                        self.completion_requested
                        and self.active == 0
                        and not self.stopped
                    )
                    if complete:
                        # Check AFTER all tracked workers finish. Another
                        # worker can invalidate readiness after finish.ready
                        # releases the rig lock but before its images flush.
                        hardware = self._state().get("hardware", {})
                        complete = (
                            isinstance(hardware, dict) and hardware.get("ready") is True
                        )
                        self.completion_requested = False
                        if complete:
                            self.closed = True
                            self.emit("setup.completed")
                            if self.handler is not None:
                                logging.getLogger().removeHandler(self.handler)
                                self.handler.close()
                        else:
                            self.emit("setup.completion_deferred", state=hardware)
                    self._write_summary(
                        "interrupted"
                        if self.stopped
                        else "succeeded"
                        if complete
                        else "in_progress"
                    )
                reset_log_context(token)

    def _json_details(self, details: dict) -> dict:
        try:
            return _json_value(details)
        except Exception as exc:
            self.recording_failure(exc)
            return {"serialization_error": str(exc)}

    def close(self) -> None:
        with self.lock:
            self.stopped = True
            if not self.closed:
                self.emit("setup.interrupted", reason="server_shutdown")
                self._write_summary("interrupted")
                self.closed = True
            if self.handler is not None:
                logging.getLogger().removeHandler(self.handler)
                self.handler.close()
        self.writer.shutdown(wait=False)


def run_setup_step(
    rig: Any, step: str, call: Callable[[], Any], *, parameters: dict | None = None
) -> Any:
    """Bracket the actual worker, so a client timeout cannot end its journal."""
    recorder = getattr(rig, "setup_recording", None)
    if recorder is None:
        return call()
    with recorder.operation(step, parameters) as attempt:
        result = call()
        if attempt is not None:
            attempt.result = result
        return result


def record_rejection(rig: Any, step: str, message: str, http_status: int = 400) -> None:
    """Record admission failures too, without treating them as server crashes."""
    run_setup_step(
        rig,
        step,
        lambda: {
            "status": "error",
            "reason": "invalid_request",
            "message": message,
            "http_status": http_status,
        },
    )
