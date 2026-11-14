"""Setup evidence survives failures, preserves exact inputs, and isolates runs."""

import asyncio
import hashlib
import json
import logging
import threading
from concurrent.futures import Future
from contextlib import nullcontext
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.common.logger.evidence import (
    current_evidence,
    record_event,
    record_frame,
    record_upload,
)
from physiclaw.core.calibration._common import PreconditionError
from physiclaw.core.setup import calibration_handlers, run_log
from physiclaw.core.setup.errors import ManualPositionRequired
from physiclaw.core.setup.run_log import SetupRecorder, run_setup_step
from tests.core.conftest import stream_request
from tests.timing_helpers import WAIT_TIMEOUT, wait_for


def events(recorder):
    return [
        json.loads(line)
        for line in (recorder.directory / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def summary(recorder, path="summary.json"):
    return json.loads((recorder.directory / path).read_text(encoding="utf-8"))


@pytest.fixture
def recorder():
    run = SetupRecorder(
        lambda: {"hardware": {"ready": True}, "origin_pinned": False, "camera_index": 2}
    )
    yield run
    run.close()


def test_operation_exception_keeps_traceback_and_state_before_and_after(recorder):
    def fail():
        record_event("camera.grid_detection", detected_count=8, expected_dots=15)
        raise RuntimeError("only eight dots")

    with pytest.raises(RuntimeError, match="eight dots"):
        run_setup_step(
            SimpleNamespace(setup_recording=recorder), "camera.mapping", fail
        )

    failure = next(e for e in events(recorder) if e["event"] == "step.exception")
    ended = events(recorder)[-1]
    assert failure["exception_type"] == "RuntimeError"
    assert "RuntimeError: only eight dots" in failure["traceback"]
    assert ended["state"] == {
        "hardware": {"ready": True},
        "origin_pinned": False,
        "camera_index": 2,
    }
    assert ended["status"] == "failed"
    assert (
        recorder.directory / "steps/camera.mapping/attempt-001/events.jsonl"
    ).exists()


@pytest.mark.parametrize(
    "result",
    [{"calibrated": False}, {"passed": False}, {"ready": False}, {"status": "error"}],
)
def test_operation_failed_result_is_failure_without_exception(recorder, result):
    rig = SimpleNamespace(setup_recording=recorder)

    returned = run_setup_step(rig, "validate", lambda: result)

    assert returned == result
    assert summary(recorder)["steps"]["validate"]["status"] == "failed"


def test_run_and_attempt_summaries_point_directly_to_failure_details(recorder):
    rig = SimpleNamespace(setup_recording=recorder)

    run_setup_step(rig, "validate", lambda: {"passed": False, "reason": "no touch"})

    entry = summary(recorder)["attention"][0]
    detail = summary(recorder, entry["summary_path"])
    assert (recorder.directory / "README.md").is_file()
    assert entry["step"] == "validate"
    assert detail["result"] == {"passed": False, "reason": "no touch"}
    assert detail["status"] == "failed"
    assert detail["duration_ms"] >= detail["work_duration_ms"]
    assert (recorder.directory / detail["events_path"]).is_file()


def test_camera_advisory_is_visible_without_becoming_a_failed_step(recorder):
    result = {"setup_ok": False, "issues": ["Phone covers 23%; minimum is 25%"]}

    run_setup_step(
        SimpleNamespace(setup_recording=recorder), "camera.frame", lambda: result
    )

    entry = summary(recorder)["attention"][0]
    assert entry["status"] == "succeeded"
    assert entry["issues"] == result["issues"]


def test_successful_retry_clears_attention_and_keeps_failed_attempt_summary(recorder):
    rig = SimpleNamespace(setup_recording=recorder)
    run_setup_step(rig, "validate", lambda: {"passed": False})
    first = summary(recorder)["steps"]["validate"]["summary_path"]

    run_setup_step(rig, "validate", lambda: {"passed": True})

    assert summary(recorder)["attention"] == []
    assert summary(recorder, first)["result"] == {"passed": False}


def test_warning_message_is_visible_in_run_attention(recorder, caplog):
    caplog.set_level(logging.WARNING)

    with recorder.operation("camera.mapping"):
        logging.getLogger("physiclaw.core.hardware.camera").warning(
            "Focus readback missing"
        )

    entry = summary(recorder)["attention"][0]
    assert entry["warnings"] == 1
    assert entry["warning_messages"] == ["Focus readback missing"]


def test_optional_failed_check_links_summary_directly_to_its_input_image(recorder):
    pixels = np.zeros((4, 5, 3), dtype=np.uint8)

    with recorder.operation("camera.mapping"):
        record_frame(pixels)
        sample = record_event("camera.focus_sample", position=42, sharpness=2)
        decision = record_event(
            "camera.focus_search_result",
            summarize=True,
            status="failed",
            reason="peak_below_minimum_sharpness",
            peak_sample=sample,
            thresholds={"minimum_sharpness": 80},
        )

    entry = summary(recorder)["attention"][0]
    finding = entry["diagnostics"][0]
    artifact = next(e for e in events(recorder) if e["event"] == "artifact.saved")
    assert entry["status"] == "succeeded"
    assert finding["reason"] == "peak_below_minimum_sharpness"
    assert finding["seq"] == decision["seq"]
    assert finding["input_path"] == artifact["path"]
    assert finding["peak_sample"]["frame_id"] == artifact["artifact_id"]
    assert finding["peak_sample"]["input_path"] == artifact["path"]
    assert (recorder.directory / artifact["path"]).is_file()
    assert summary(recorder, entry["summary_path"])["diagnostics"] == [finding]


@pytest.mark.parametrize("status", ["succeeded", "skipped"])
def test_nonfailing_diagnostics_stay_available_without_entering_attention(
    recorder, status
):
    with recorder.operation("camera.mapping"):
        record_event(
            "camera.focus_result", summarize=True, status=status, reason="test"
        )

    result = summary(recorder)

    assert result["attention"] == []
    assert result["steps"]["camera.mapping"]["diagnostics"][0]["status"] == status


def test_missing_measurement_does_not_claim_the_previous_input_frame(recorder):
    with recorder.operation("camera.mapping"):
        record_frame(np.zeros((4, 5, 3), dtype=np.uint8))
        reference = record_event(
            "camera.focus_meter",
            reason="frame_settle_timeout",
            frame_id=None,
            input_artifact_id=None,
        )

    observation = next(
        e for e in events(recorder) if e["event"] == "camera.focus_meter"
    )

    assert reference["frame_id"] is None
    assert reference["input_path"] is None
    assert observation["frame_id"] is None
    assert observation["input_artifact_id"] is None


def test_cyclic_diagnostic_preserves_physical_exception_and_finalizes_attempt(recorder):
    details = {}
    details["cycle"] = details

    def run_operation():
        record_event("test.decision", summarize=True, details=details)
        raise ValueError("physical failure")

    with (
        pytest.raises(ValueError, match="physical failure"),
        recorder.operation("camera.mapping"),
    ):
        run_operation()

    assert recorder.active == 0
    assert current_evidence() is None
    assert summary(recorder)["recording_complete"] is False
    assert summary(recorder)["steps"]["camera.mapping"]["message"] == "physical failure"


def test_attempt_summary_is_available_while_worker_runs(recorder):
    with recorder.operation("camera.frame", {"rotation": 2}):
        entry = summary(recorder)["steps"]["camera.frame"]
        detail = summary(recorder, entry["summary_path"])

    assert detail["status"] == "running"
    assert detail["parameters"] == {"rotation": 2}
    assert "result" not in detail


def test_summary_write_failure_preserves_original_physical_error(recorder, mocker):
    mocker.patch.object(run_log, "write_json_atomic", side_effect=OSError("disk full"))

    with (
        pytest.raises(ValueError, match="physical failure"),
        recorder.operation("validate"),
    ):
        raise ValueError("physical failure")

    assert recorder.active == 0
    assert current_evidence() is None
    assert recorder.incomplete > 0


def test_operation_retry_preserves_both_attempts(recorder):
    rig = SimpleNamespace(setup_recording=recorder)

    run_setup_step(rig, "validate", lambda: {"calibrated": False})
    run_setup_step(rig, "validate", lambda: {"calibrated": True})

    results = [e for e in events(recorder) if e["event"] == "step.result"]
    assert [(e["attempt"], e["status"]) for e in results] == [
        (1, "failed"),
        (2, "succeeded"),
    ]
    assert summary(recorder)["status"] == "in_progress"


def test_active_step_and_idle_run_share_in_progress_run_status(recorder):
    recorder.begin()
    idle = summary(recorder)

    with recorder.operation("camera.frame"):
        active = summary(recorder)

    assert (
        idle["status"]
        == active["status"]
        == summary(recorder)["status"]
        == "in_progress"
    )
    assert active["steps"]["camera.frame"]["status"] == "running"
    assert summary(recorder)["steps"]["camera.frame"]["status"] == "succeeded"


@pytest.mark.parametrize(
    "error,status",
    [
        (PreconditionError("connect camera"), "blocked"),
        (ManualPositionRequired("position stylus"), "waiting_for_input"),
        (KeyboardInterrupt(), "interrupted"),
    ],
)
def test_operation_classifies_blocked_and_interrupted_steps(recorder, error, status):
    with pytest.raises(type(error)), recorder.operation("camera.frame"):
        raise error

    ended = events(recorder)[-1]

    assert ended["status"] == status
    assert current_evidence() is None


def test_frame_saved_losslessly_before_caller_mutates_pixels(recorder):
    pixels = np.arange(60, dtype=np.uint8).reshape(4, 5, 3)
    expected = pixels.copy()

    with recorder.operation("camera.frame"):
        record_frame(pixels, published_at=123.5, device_id="Camera-2")
        pixels[:] = 0
        record_event("camera.marker", detected=[2, 3])

    artifact = next(e for e in events(recorder) if e["event"] == "artifact.saved")
    marker = next(e for e in events(recorder) if e["event"] == "camera.marker")
    saved = recorder.directory / artifact["path"]
    assert np.array_equal(cv2.imread(str(saved)), expected)
    assert marker["frame_id"] == artifact["artifact_id"]
    assert artifact["published_at"] == 123.5
    assert artifact["sha256"] == hashlib.sha256(saved.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "data,suffix",
    [
        (b"\x89PNG\r\n\x1a\noriginal", ".png"),
        (b"\xff\xd8original", ".jpg"),
        (b"invalid upload", ".bin"),
    ],
)
def test_upload_keeps_original_bytes_even_if_decode_will_fail(recorder, data, suffix):
    with recorder.operation("viewport"):
        record_upload(data, source="cached")

    artifact = next(e for e in events(recorder) if e["event"] == "artifact.saved")

    assert artifact["path"].endswith(suffix)
    assert (recorder.directory / artifact["path"]).read_bytes() == data


def test_missing_frame_is_recorded_without_fake_image(recorder):
    with recorder.operation("camera.frame"):
        record_frame(None, device_id="Camera-2")

    missing = next(e for e in events(recorder) if e["event"] == "camera.frame_missing")

    assert missing["device_id"] == "Camera-2"
    assert not list(recorder.directory.rglob("*.png"))


def test_image_budget_marks_recording_incomplete_without_failing_step(
    recorder, monkeypatch
):
    monkeypatch.setattr(recorder, "MAX_IMAGE_BYTES", 1)

    with recorder.operation("camera.frame"):
        record_frame(np.zeros((2, 2, 3), dtype=np.uint8))

    skipped = next(e for e in events(recorder) if e["event"] == "artifact.skipped")
    assert skipped["reason"] == "run_image_budget"
    assert summary(recorder)["recording_complete"] is False
    assert summary(recorder)["steps"]["camera.frame"]["status"] == "succeeded"


def test_full_writer_queue_is_explicit_without_copying_pixels(
    recorder, monkeypatch, mocker
):
    monkeypatch.setattr(recorder, "slots", SimpleNamespace(acquire=lambda **kw: False))
    pixels = SimpleNamespace(copy=mocker.Mock())

    with recorder.operation("camera.frame"):
        record_frame(pixels)

    skipped = next(e for e in events(recorder) if e["event"] == "artifact.skipped")
    pixels.copy.assert_not_called()
    assert skipped["reason"] == "writer_queue_full"
    assert summary(recorder)["recording_errors_or_skips"] == 1


def test_frame_copy_failure_releases_capacity_for_next_artifact(
    recorder, monkeypatch, mocker
):
    monkeypatch.setattr(recorder, "slots", threading.BoundedSemaphore(1))
    pixels = SimpleNamespace(
        copy=mocker.Mock(side_effect=MemoryError("no frame memory"))
    )

    with recorder.operation("camera.frame"):
        record_frame(pixels)
        record_upload(b"original upload")

    failed = next(e for e in events(recorder) if e["event"] == "artifact.failed")
    saved = next(e for e in events(recorder) if e["event"] == "artifact.saved")
    assert failed["exception_type"] == "MemoryError"
    assert (recorder.directory / saved["path"]).read_bytes() == b"original upload"
    assert summary(recorder)["recording_errors_or_skips"] == 1


def test_encoder_failure_does_not_replace_physical_result(recorder, mocker):
    mocker.patch.object(cv2, "imencode", return_value=(False, None))

    with recorder.operation("camera.frame") as attempt:
        record_frame(np.zeros((2, 2, 3), dtype=np.uint8))
        attempt.result = {"rotation": 0}

    assert summary(recorder)["recording_complete"] is False
    assert summary(recorder)["steps"]["camera.frame"]["status"] == "succeeded"


def test_journal_disk_failure_preserves_original_exception(recorder, mocker):
    mocker.patch.object(run_log, "append_text", side_effect=OSError("disk full"))

    with (
        pytest.raises(ValueError, match="original failure"),
        recorder.operation("arm.calibrate"),
    ):
        raise ValueError("original failure")

    assert summary(recorder)["recording_complete"] is False
    assert summary(recorder)["steps"]["arm.calibrate"]["status"] == "failed"


def test_readiness_finishes_run_and_next_operation_gets_new_directory(recorder):
    rig = SimpleNamespace(setup_recording=recorder)
    run_setup_step(rig, "finish.ready", lambda: {"ready": True})
    first = recorder.directory

    run_setup_step(rig, "arm.connect", lambda: None)

    assert first != recorder.directory
    assert first.parent.name == "setup"
    assert (
        json.loads((first / "summary.json").read_text(encoding="utf-8"))["status"]
        == "succeeded"
    )
    assert summary(recorder)["status"] == "in_progress"


def test_wizard_refresh_does_not_restart_completed_run(recorder):
    run_setup_step(
        SimpleNamespace(setup_recording=recorder),
        "finish.ready",
        lambda: {"ready": True},
    )
    first = recorder.directory

    recorder.begin()

    assert recorder.directory == first
    assert summary(recorder)["status"] == "succeeded"


def test_server_shutdown_marks_incomplete_run_interrupted(recorder):
    recorder.begin()

    recorder.close()

    assert summary(recorder)["status"] == "interrupted"
    assert events(recorder)[-1]["reason"] == "server_shutdown"


def test_older_attempt_finishing_late_does_not_overwrite_latest_attempt(recorder):
    with recorder.operation("camera.mapping") as first:
        with recorder.operation("camera.mapping") as second:
            second.result = {"status": "error", "message": "hardware busy"}
        first.result = {"dots": 15}

    latest = summary(recorder)["steps"]["camera.mapping"]

    assert latest["attempt"] == 2
    assert latest["status"] == "failed"


def test_worker_finishing_after_shutdown_cannot_mark_run_successful(recorder):
    with recorder.operation("finish.ready") as attempt:
        recorder.close()
        attempt.result = {"ready": True}

    result = summary(recorder)

    assert result["status"] == "interrupted"


def test_closed_recorder_never_starts_or_writes_to_working_directory(
    recorder, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    recorder.close()
    rig = SimpleNamespace(setup_recording=recorder)

    recorder.begin()
    result = run_setup_step(rig, "camera.connect", lambda: "connected")

    assert result == "connected"
    assert recorder.run_id is None
    assert not (tmp_path / "events.jsonl").exists()
    assert not (tmp_path / "summary.json").exists()


def test_overlapping_disconnect_prevents_false_setup_completion(recorder):
    state = {"hardware": {"ready": True}}
    recorder.snapshot = lambda: state

    with recorder.operation("camera.disconnect"):
        run_setup_step(
            SimpleNamespace(setup_recording=recorder),
            "finish.ready",
            lambda: {"ready": True},
        )
        state["hardware"]["ready"] = False

    assert summary(recorder)["status"] == "in_progress"
    assert not recorder.closed
    assert not any(e["event"] == "setup.completed" for e in events(recorder))


@pytest.mark.parametrize("result", [None, {}, {"ready": 1}])
def test_readiness_requires_explicit_success_result(recorder, result):
    rig = SimpleNamespace(setup_recording=recorder)

    run_setup_step(rig, "finish.ready", lambda: result)

    assert summary(recorder)["status"] == "in_progress"


@pytest.mark.parametrize("state", [{}, {"hardware": {"ready": False}}])
def test_readiness_requires_current_hardware_confirmation(recorder, state):
    recorder.snapshot = lambda: state
    rig = SimpleNamespace(setup_recording=recorder)

    run_setup_step(rig, "finish.ready", lambda: {"ready": True})

    assert summary(recorder)["status"] == "in_progress"


def test_failed_writer_future_preserves_physical_exception_and_releases_scope(recorder):
    future = Future()
    future.set_exception(OSError("worker failed"))

    def run_operation():
        attempt.futures.append(future)
        raise ValueError("physical failure")

    with (
        pytest.raises(ValueError, match="physical failure"),
        recorder.operation("camera.frame") as attempt,
    ):
        run_operation()

    assert current_evidence() is None
    assert recorder.active == 0
    assert summary(recorder)["recording_complete"] is False
    assert summary(recorder)["steps"]["camera.frame"]["status"] == "failed"


def test_cyclic_failed_result_preserves_return_value_and_finalizes_attempt(recorder):
    result = {"passed": False}
    result["details"] = result

    returned = run_setup_step(
        SimpleNamespace(setup_recording=recorder), "validate", lambda: result
    )

    assert returned is result
    assert recorder.active == 0
    assert current_evidence() is None
    assert summary(recorder)["recording_complete"] is False


def test_log_format_error_cannot_fail_physical_operation(recorder):
    recorder.begin()
    malformed = logging.LogRecord(
        "physiclaw.core.hardware.camera",
        logging.ERROR,
        __file__,
        1,
        "%d",
        ("bad",),
        None,
    )

    returned = run_setup_step(
        SimpleNamespace(setup_recording=recorder),
        "camera.frame",
        lambda: recorder.handler.handle(malformed),
    )

    assert returned is True
    assert summary(recorder)["recording_complete"] is False


def test_old_background_log_handler_cannot_append_to_new_run(recorder):
    recorder.begin()
    old_handler = recorder.handler
    run_setup_step(
        SimpleNamespace(setup_recording=recorder),
        "finish.ready",
        lambda: {"ready": True},
    )
    run_setup_step(
        SimpleNamespace(setup_recording=recorder), "camera.connect", lambda: None
    )
    old_log = logging.LogRecord(
        "physiclaw.core.hardware.camera",
        logging.ERROR,
        __file__,
        1,
        "old USB error",
        (),
        None,
    )

    old_handler.handle(old_log)

    assert not any(e.get("message") == "old USB error" for e in events(recorder))


def test_scoped_and_background_device_logs_survive_with_tracebacks(recorder, caplog):
    caplog.set_level(logging.DEBUG)
    recorder.begin()

    with recorder.operation("camera.frame"):
        logging.getLogger("physiclaw.core.calibration.camera_frame").debug(
            "marker missing"
        )
    logging.getLogger("physiclaw.core.hardware.frame_reader").error(
        "USB disconnected", extra={"device_id": "Camera-2"}
    )

    logs = [e for e in events(recorder) if e["event"] == "diagnostic.log"]
    assert logs[0]["step"] == "camera.frame"
    assert logs[1]["scope"] == "background"
    assert logs[1]["device_id"] == "Camera-2"


def test_bridge_rejection_keeps_structured_status_without_arbitrary_extras(
    recorder, caplog
):
    caplog.set_level(logging.INFO)
    recorder.begin()

    logging.getLogger("physiclaw.core.bridge.handler").info(
        "Phone bridge request rejected: upload window expired",
        extra={
            "event": "bridge.request_rejected",
            "http_status": 403,
            "private_payload": "must not be recorded",
        },
    )

    entry = events(recorder)[-1]
    assert entry["event"] == "diagnostic.log"
    assert entry["scope"] == "background"
    assert entry["source_fields"]["http_status"] == 403
    assert entry["source_fields"]["event"] == "bridge.request_rejected"
    assert "private_payload" not in json.dumps(entry)
    assert "step" not in entry


@pytest.mark.parametrize("level", [logging.WARNING, logging.ERROR, logging.CRITICAL])
def test_idle_background_warning_updates_summary_and_retry_run_resets_it(
    recorder, caplog, level
):
    caplog.set_level(logging.WARNING)
    recorder.begin()
    logger = logging.getLogger("physiclaw.core.hardware.frame_reader")

    for i in range(23):
        logger.log(level, "Camera stalled %s", i)

    report = summary(recorder)
    assert report["status"] == "in_progress"
    assert report["background_warnings"] == 23
    assert len(report["background_warning_messages"]) == 20
    first = report["background_warning_messages"][0]
    assert first["level"] == logging.getLevelName(level).lower()
    assert first["message"] == "Camera stalled 0"
    assert events(recorder)[first["seq"] - 1]["message"] == first["message"]
    assert "step" not in first
    assert len([e for e in events(recorder) if e["event"] == "diagnostic.log"]) == 23

    rig = SimpleNamespace(setup_recording=recorder)
    run_setup_step(rig, "finish.ready", lambda: {"ready": True})
    run_setup_step(rig, "camera.connect", lambda: None)
    assert summary(recorder)["background_warnings"] == 0
    assert summary(recorder)["background_warning_messages"] == []


@pytest.mark.parametrize("level", [logging.INFO, logging.WARNING])
def test_idle_background_journal_failure_refreshes_recording_health(
    recorder, mocker, level
):
    recorder.begin()
    mocker.patch.object(run_log, "append_text", side_effect=OSError("disk full"))
    record = logging.LogRecord(
        "physiclaw.core.hardware.frame_reader",
        level,
        __file__,
        1,
        "Camera recovered",
        (),
        None,
    )

    recorder.handler.handle(record)

    assert summary(recorder)["recording_complete"] is False
    assert summary(recorder)["recording_errors"][0]["message"] == "disk full"
    if level == logging.WARNING:
        assert summary(recorder)["background_warnings"] == 1
        assert summary(recorder)["background_warning_messages"][0]["seq"] is None


def test_malformed_idle_log_refreshes_recording_health_without_failing_setup(recorder):
    recorder.begin()
    malformed = logging.LogRecord(
        "physiclaw.core.hardware.frame_reader",
        logging.ERROR,
        __file__,
        1,
        "%d",
        ("bad",),
        None,
    )

    recorder.handler.handle(malformed)

    assert summary(recorder)["recording_complete"] is False
    assert summary(recorder)["recording_errors"][0]["type"] == "TypeError"
    assert (
        run_setup_step(
            SimpleNamespace(setup_recording=recorder),
            "camera.connect",
            lambda: "connected",
        )
        == "connected"
    )


def test_background_warning_freezes_fields_before_caller_mutates_them(recorder):
    recorder.begin()
    status = {"value": "stalled"}
    warning = logging.LogRecord(
        "physiclaw.core.hardware.frame_reader",
        logging.WARNING,
        __file__,
        1,
        "Camera stalled",
        (),
        None,
    )
    warning.status = status
    recorder.handler.handle(warning)
    status["value"] = "changed after logging"

    run_setup_step(
        SimpleNamespace(setup_recording=recorder), "camera.connect", lambda: None
    )

    preview = summary(recorder)["background_warning_messages"][0]
    journal = events(recorder)[preview["seq"] - 1]
    assert preview["source_fields"]["status"] == {"value": "stalled"}
    assert preview["source_fields"] == journal["source_fields"]


def test_cyclic_background_fields_cannot_poison_future_summaries(recorder):
    recorder.begin()
    status = {}
    status["cycle"] = status
    warning = logging.LogRecord(
        "physiclaw.core.hardware.frame_reader",
        logging.WARNING,
        __file__,
        1,
        "Camera stalled",
        (),
        None,
    )
    warning.status = status
    recorder.handler.handle(warning)

    run_setup_step(
        SimpleNamespace(setup_recording=recorder), "camera.connect", lambda: "connected"
    )

    report = summary(recorder)
    assert report["recording_complete"] is False
    assert report["steps"]["camera.connect"]["status"] == "succeeded"


def test_setup_metadata_records_dependency_versions_and_missing_distributions(
    recorder, mocker
):
    def version(name):
        if name == "opencv-python-headless":
            raise run_log.importlib.metadata.PackageNotFoundError(name)
        return "test-version"

    mocker.patch.object(run_log.importlib.metadata, "version", side_effect=version)
    recorder.begin()

    versions = summary(recorder, "run.json")["dependency_versions"]
    assert versions["opencv-python"] == "test-version"
    assert versions["numpy"] == "test-version"
    assert versions["pyserial"] == "test-version"
    assert versions["opencv-python-headless"] is None
    assert summary(recorder)["recording_complete"] is True


async def test_concurrent_worker_scopes_keep_their_own_step_and_order(recorder):
    rig = SimpleNamespace(setup_recording=recorder)

    await asyncio.gather(
        asyncio.to_thread(
            run_setup_step, rig, "camera.frame", lambda: record_event("worker.one")
        ),
        asyncio.to_thread(
            run_setup_step, rig, "arm.connect", lambda: record_event("worker.two")
        ),
    )

    journal = events(recorder)
    assert (
        next(e for e in journal if e["event"] == "worker.one")["step"] == "camera.frame"
    )
    assert (
        next(e for e in journal if e["event"] == "worker.two")["step"] == "arm.connect"
    )
    assert [e["seq"] for e in journal] == list(range(1, len(journal) + 1))


async def test_client_cancellation_does_not_close_server_worker_journal(recorder):
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    rig = SimpleNamespace(setup_recording=recorder)

    def physical_step():
        started.set()
        assert release.wait(WAIT_TIMEOUT)
        return {"calibrated": True}

    def worker():
        try:
            run_setup_step(rig, "validate", physical_step)
        finally:
            done.set()

    task = asyncio.create_task(asyncio.to_thread(worker))
    try:
        assert await asyncio.to_thread(started.wait, WAIT_TIMEOUT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wait_for(task)
    finally:
        release.set()
        assert await asyncio.to_thread(done.wait, WAIT_TIMEOUT)

    assert events(recorder)[-1]["event"] == "step.ended"
    assert events(recorder)[-1]["status"] == "succeeded"


async def test_invalid_request_is_recorded_before_any_hardware_work(recorder):
    rig = SimpleNamespace(setup_recording=recorder)
    request = stream_request(b'{"fresh":"false"}')

    response = await calibration_handlers.handle_measure_viewport_shift(
        request, rig, None, None, None
    )

    assert response.status_code == 400
    result = next(e for e in events(recorder) if e["event"] == "step.result")
    assert result["result"]["reason"] == "invalid_request"
    assert result["result"]["message"] == "fresh must be a boolean"


async def test_cleanup_failure_does_not_hide_original_calibration_failure(
    recorder, mocker, caplog
):
    caplog.set_level(logging.DEBUG)
    rig = SimpleNamespace(
        setup_recording=recorder,
        cam=object(),
        calibration=SimpleNamespace(),
        locked=nullcontext,
        unmark_ready=lambda: None,
        park=mocker.Mock(side_effect=[None, RuntimeError("park also failed")]),
    )
    mocker.patch.object(
        calibration_handlers,
        "calibrate_camera_frame",
        side_effect=RuntimeError("red marker missing"),
    )

    response = await calibration_handlers.handle_calibrate_camera_frame(
        object(), rig, object()
    )

    assert json.loads(response.body)["message"] == "red marker missing"
    exception = next(e for e in events(recorder) if e["event"] == "step.exception")
    assert exception["message"] == "red marker missing"
    assert any("park also failed" in str(e.get("exception")) for e in events(recorder))


@given(st.floats(allow_nan=True, allow_infinity=True))
def test_numeric_evidence_always_serializes_as_strict_json(value):
    # Exercise the public journal surface with a fresh recorder per example.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        run = SetupRecorder(dict)
        run.directory = Path(folder)
        run.emit("measurement", value=value)
        data = events(run)[0]
        run.close()

    assert data["event"] == "measurement"
    assert isinstance(data["value"], (float, str))
