"""Scoped evidence hooks for server operations; inert outside a recording scope.

Hardware and vision depend on this leaf interface, never on a setup recorder.
The scope travels through asyncio.to_thread; long-lived device threads must
publish their diagnostics through an explicitly bound sink instead.
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from typing import Any, Protocol


class EvidenceSink(Protocol):
    def recording_failure(self, exc: Exception) -> None: ...

    def event(self, event: str, **fields: Any) -> None: ...

    def frame(self, frame: Any, **fields: Any) -> None: ...

    def upload(self, data: bytes, **fields: Any) -> None: ...


class EvidenceOperation(EvidenceSink, Protocol):
    result: Any


class OperationRecorder(Protocol):
    """Optional recorder injected by the application, independent of hardware."""

    def begin(self) -> None: ...

    def operation(
        self, step: str, parameters: dict | None = None
    ) -> AbstractContextManager[EvidenceOperation | None]: ...

    def close(self) -> None: ...


_sink: ContextVar[EvidenceSink | None] = ContextVar("operation_evidence", default=None)


@contextmanager
def evidence_scope(sink: EvidenceSink) -> Iterator[None]:
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def _record(method: str, value: Any, fields: dict) -> None:
    if (sink := _sink.get()) is None:
        return
    try:
        getattr(sink, method)(value, **fields)
    except Exception as exc:
        # Diagnostics must never replace a device outcome, even when the
        # failure reporter itself is broken. The owner tracks missing evidence.
        try:
            sink.recording_failure(exc)
        except Exception:
            pass


def record_event(event: str, **fields: Any) -> None:
    _record("event", event, fields)


def record_frame(frame: Any, **fields: Any) -> None:
    _record("frame", frame, fields)


def record_upload(data: bytes, **fields: Any) -> None:
    _record("upload", data, fields)


def current_evidence() -> EvidenceSink | None:
    return _sink.get()
