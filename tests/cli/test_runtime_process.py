"""Portable lifecycle checks for the managed runtime's public ownership API."""

import asyncio
import threading
from collections import deque
from types import SimpleNamespace

import pytest

from physiclaw.cli import runtime_process


class ControlPipe:
    def __init__(self, events, error=None):
        self.events = events
        self.error = error

    def close(self):
        self.events.append("stdin.close")
        if self.error is not None:
            raise self.error


class ChildProcess:
    def __init__(self, polls=()):
        self.pid = 12345
        self.returncode = None
        self.events = []
        self.stdin = ControlPipe(self.events)
        self.polls = deque(polls)
        self.termination_thread = None
        self.on_poll = lambda: None

    def poll(self):
        self.events.append("poll")
        self.on_poll()
        if self.polls:
            self.returncode = self.polls.popleft()
        return self.returncode

    def terminate(self):
        self.events.append("terminate")
        self.termination_thread = threading.get_ident()
        self.returncode = -15

    def wait(self, *, timeout):
        self.events.append("wait")
        assert self.returncode is not None, "wait before child termination"
        return self.returncode


@pytest.fixture
def clock(monkeypatch):
    ticks = SimpleNamespace(now=100.0)
    monkeypatch.setattr(
        runtime_process, "time", SimpleNamespace(monotonic=lambda: ticks.now)
    )
    return ticks


async def test_stop_waits_for_graceful_exit_without_terminating_child(clock):
    child = ChildProcess(polls=(None, 0))
    owner = runtime_process.ManagedRuntime(child)

    await owner.stop()

    assert child.events == ["stdin.close", "poll", "poll"]
    assert child.returncode == 0


@pytest.mark.parametrize(
    "timeout, force, reason", [(0.0, False, "timeout"), (30.0, True, "interrupt")]
)
async def test_stop_forces_and_reaps_child_off_the_event_loop_thread(
    clock, caplog, timeout, force, reason
):
    child = ChildProcess()
    owner = runtime_process.ManagedRuntime(child, timeout=timeout)
    loop_thread = threading.get_ident()

    await asyncio.wait_for(owner.stop(force=lambda: force), timeout=1.0)

    assert child.events == ["stdin.close", "poll", "poll", "terminate", "wait"]
    assert child.termination_thread != loop_thread
    assert f"Runtime exit forced ({reason})" in caplog.text


async def test_stop_rechecks_deadline_after_waiting(clock, caplog):
    child = ChildProcess()
    owner = runtime_process.ManagedRuntime(child, timeout=1.0)
    times = deque((100.0, 101.0))

    def advancing_force_check():
        clock.now = times.popleft()
        return False

    await owner.stop(force=advancing_force_check)

    assert child.events == ["stdin.close", "poll", "poll", "poll", "terminate", "wait"]
    assert times == deque()
    assert "Runtime exit forced (timeout)" in caplog.text


async def test_repeated_stop_and_close_do_not_touch_reaped_child(clock):
    child = ChildProcess(polls=(0,))
    owner = runtime_process.ManagedRuntime(child)
    await owner.stop()
    completed = list(child.events)

    await owner.stop()
    owner.close()

    assert child.events == completed


async def test_stop_after_prior_request_does_not_close_control_pipe_twice(clock):
    child = ChildProcess(polls=(0,))
    owner = runtime_process.ManagedRuntime(child)
    owner.request_stop()

    await owner.stop()

    assert child.events == ["stdin.close", "poll"]


async def test_stop_reaps_already_exited_child_without_control_pipe(clock):
    child = ChildProcess(polls=(0,))
    child.stdin = None
    owner = runtime_process.ManagedRuntime(child)

    await owner.stop()
    owner.close()

    assert child.events == ["poll"]


async def test_stop_continues_reaping_when_control_pipe_close_fails(clock, caplog):
    child = ChildProcess(polls=(0,))
    child.stdin = ControlPipe(child.events, OSError("broken control pipe"))
    owner = runtime_process.ManagedRuntime(child)

    await owner.stop()
    owner.close()

    assert child.events == ["stdin.close", "poll"]
    assert "Runtime control pipe close failed" in caplog.text


async def test_cancelled_async_stop_leaves_child_owned_for_fallback_close(clock):
    child = ChildProcess()
    started = asyncio.Event()
    child.on_poll = started.set
    owner = runtime_process.ManagedRuntime(child)
    stopping = asyncio.create_task(owner.stop())
    await started.wait()

    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    owner.close()

    assert child.events == ["stdin.close", "poll", "poll", "poll", "terminate", "wait"]
    assert child.returncode == -15
