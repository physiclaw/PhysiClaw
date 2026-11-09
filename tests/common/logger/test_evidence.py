"""Evidence is scoped and cannot alter the operation it observes."""

import asyncio

import pytest

from physiclaw.common.logger.evidence import (
    current_evidence,
    evidence_scope,
    record_event,
    record_frame,
    record_upload,
)


class Sink:
    def __init__(self, *, broken=False, broken_reporter=False):
        self.records = []
        self.errors = []
        self.broken = broken
        self.broken_reporter = broken_reporter

    def event(self, event, **fields):
        self._add("event", event, fields)

    def frame(self, frame, **fields):
        self._add("frame", frame, fields)

    def upload(self, data, **fields):
        self._add("upload", data, fields)

    def _add(self, method, value, fields):
        if self.broken:
            raise OSError("evidence unavailable")
        self.records.append((method, value, fields))

    def recording_failure(self, exc):
        if self.broken_reporter:
            raise OSError("reporter unavailable")
        self.errors.append(str(exc))


@pytest.mark.parametrize(
    "record,method,value",
    [
        (record_event, "event", "detected"),
        (record_frame, "frame", None),
        (record_upload, "upload", b"image"),
    ],
)
def test_hook_forwards_value_and_metadata(record, method, value):
    sink = Sink()

    with evidence_scope(sink):
        record(value, source="test")

    assert sink.records == [(method, value, {"source": "test"})]
    assert current_evidence() is None


@pytest.mark.parametrize("record", [record_event, record_frame, record_upload])
def test_hook_failure_is_counted_without_replacing_operation_error(record):
    sink = Sink(broken=True)

    def run_operation():
        record("input")
        raise ValueError("physical error")

    with pytest.raises(ValueError, match="physical error"), evidence_scope(sink):
        run_operation()

    assert sink.errors == ["evidence unavailable"]
    assert current_evidence() is None


@pytest.mark.parametrize("record", [record_event, record_frame, record_upload])
def test_hook_failed_failure_reporter_does_not_escape(record):
    sink = Sink(broken=True, broken_reporter=True)

    with evidence_scope(sink):
        record("input")

    assert sink.records == []


@pytest.mark.parametrize("record", [record_event, record_frame, record_upload])
def test_hook_outside_scope_is_inert(record):
    record("input")

    assert current_evidence() is None


def test_nested_scope_restores_outer_sink_after_exception():
    outer, inner = Sink(), Sink()

    with evidence_scope(outer):

        def run_operation():
            record_event("inner")
            raise ValueError("failed")

        with pytest.raises(ValueError, match="failed"), evidence_scope(inner):
            run_operation()
        record_event("outer")

    assert outer.records == [("event", "outer", {})]
    assert inner.records == [("event", "inner", {})]


async def test_scope_reaches_worker_thread():
    sink = Sink()

    with evidence_scope(sink):
        await asyncio.to_thread(record_event, "worker")

    assert sink.records == [("event", "worker", {})]
