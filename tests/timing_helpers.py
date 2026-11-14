"""Timing policy for the entire test suite.

Use Events, Conditions, barriers or observed lock contention to establish
ordering. A sleep is not evidence that another worker reached a particular
state. Join owned workers and release their gates in finally blocks.

Make dates, freshness and application budgets explicit test inputs. Patch a
module's clock/asyncio reference, not the shared event loop's clock. When a
deadline crosses module boundaries, those application clocks share an epoch.
ManualTimeouts preserves real asyncio cancellation and TimeoutError conversion.

WAIT_TIMEOUT and PROCESS_TIMEOUT are hang watchdogs, never speed assertions.
Keep real timeout integration tests for empty waits, held native locks, process
termination and delayed HTTP responses. Polling external processes/files is
valid when bounded by a real monotonic watchdog and paired with owned cleanup.
Zero-second yields are valid when delivering cancellation or testing context
isolation; readiness and lock-order tests instead observe the actual operation.
Deliberately blocked fakes/children stay blocked until their owner releases or
cancels them. Hypothesis correctness tests inherit deadline=None in conftest.

References: https://docs.pytest.org/en/stable/explanation/flaky.html and
https://docs.python.org/3.12/library/asyncio-task.html#asyncio.Timeout.
"""

import asyncio
import threading
from types import SimpleNamespace

WAIT_TIMEOUT = 10
PROCESS_TIMEOUT = 60


class ManualTimeouts:
    """A module-local asyncio proxy with real, explicitly expired timeouts.

    Patch the application's asyncio reference, never the shared asyncio module.
    Expire a scope only after the operation under test has actually started.
    Cancellation, TimeoutError conversion and expired() remain asyncio's own.
    """

    def __init__(self, *, now=None):
        self.delays = []
        self.scopes = []
        self.deadlines = []
        self.now = now
        self.loop = SimpleNamespace(time=lambda: self.now)

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def timeout(self, delay):
        scope = asyncio.timeout(None)
        self.delays.append(delay)
        self.scopes.append(scope)
        return scope

    def get_running_loop(self):
        if self.now is None:
            return asyncio.get_running_loop()
        return self.loop

    def timeout_at(self, deadline):
        self.deadlines.append(deadline)
        return self.timeout(
            None if deadline is None else deadline - self.get_running_loop().time()
        )

    async def wait_for(self, awaitable, timeout):  # noqa: ASYNC109 -- asyncio API proxy.
        assert timeout is None or timeout > 0
        async with self.timeout(timeout):
            return await awaitable

    def expire(self, index=-1):
        assert self.delays[index] is not None
        self.scopes[index].reschedule(asyncio.get_running_loop().time())


async def wait_for(awaitable):
    """Bound a test's readiness/completion wait, independently of app clocks."""
    return await asyncio.wait_for(awaitable, WAIT_TIMEOUT)


class ObservedAsyncLock:
    """Signal actual lock contention without guessing how many turns to yield."""

    def __init__(self, lock):
        self.lock = lock
        self.contended = asyncio.Event()

    def locked(self):
        return self.lock.locked()

    async def __aenter__(self):
        if self.lock.locked():
            self.contended.set()
        await self.lock.acquire()
        return self

    async def __aexit__(self, *args):
        self.lock.release()


class ObservedLock:
    """Keep the real lock, with a signal when a contender reaches acquire()."""

    def __init__(self, lock):
        self.lock = lock
        self.contended = threading.Event()

    def acquire(self, *args, **kwargs):
        # RLock has no locked() on Python 3.12. A successful probe owns the
        # lock already; never release/reacquire it and open another race.
        if self.lock.acquire(blocking=False):
            return True
        self.contended.set()
        return self.lock.acquire(*args, **kwargs)

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()
