"""Bounded subprocess cleanup for hardware CLI cancellation."""

from __future__ import annotations

import os
import signal
import subprocess
from contextlib import contextmanager
from threading import Event


def interrupt_process(process: subprocess.Popen, *, group=False, timeout=1.0) -> None:
    """Interrupt an owned child, escalating if native code does not respond.

    ``group`` is only valid for children started with ``start_new_session``.
    Reap the child before returning so inherited output locks are released.
    """

    def send(sig):
        try:
            if group:
                os.killpg(process.pid, sig)
            elif process.poll() is None:
                process.send_signal(sig)
        except ProcessLookupError:
            pass

    try:
        for sig, wait in ((signal.SIGINT, timeout), (signal.SIGTERM, 1)):
            send(sig)
            try:
                process.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                pass
        send(signal.SIGKILL)
        process.wait()
    finally:
        if group:
            # A descendant can still hold stdout/the output lock after the
            # leader exits. Only this child's private process group is touched.
            send(signal.SIGKILL)


@contextmanager
def ignore_interrupts():
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


@contextmanager
def cancel_on_interrupt(cancelled: Event):
    """Let supervising threads stop children before the main thread unwinds."""

    def cancel(*_):
        # Event.set holds a non-reentrant lock. A second Ctrl+C must not
        # re-enter it while the first signal is setting cancellation.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        cancelled.set()

    cancelled.clear()
    previous = signal.signal(signal.SIGINT, cancel)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
        cancelled.clear()
