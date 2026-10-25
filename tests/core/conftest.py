"""Shared helpers for core-subsystem tests."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace


def stream_request(data: bytes, **fields) -> SimpleNamespace:
    """Minimal request with a streamed body and caller-supplied metadata."""

    async def stream():
        yield data

    return SimpleNamespace(stream=stream, **fields)


def wire_locked(rig):
    """Wire a mock rig's ``locked()`` to delegate to its ``acquire()`` /
    ``release()``, mirroring ``HardwareRig.locked``.

    The connect / calibration / warm-start handlers serialize hardware work
    through ``with rig.locked():``, so tests that assert the acquire→release
    bracket (and its call ordering in ``mock_calls``) need the mock to record
    those calls. Returns ``rig`` so callers can write
    ``rig = wire_locked(MagicMock(name="rig"))``.
    """

    @contextmanager
    def _locked():
        rig.acquire()
        try:
            yield
        finally:
            rig.release()

    rig.locked.side_effect = _locked
    return rig
