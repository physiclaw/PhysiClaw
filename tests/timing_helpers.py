"""Control ordering explicitly; wall-clock limits only catch hung tests."""

import asyncio
import threading

WAIT_TIMEOUT = 10


class ManualTimeouts:
    """A module-local asyncio proxy with real, explicitly expired timeouts.

    Patch the application's asyncio reference, never the shared asyncio module.
    Expire a scope only after the operation under test has actually started.
    Cancellation, TimeoutError conversion and expired() remain asyncio's own.
    """

    def __init__(self):
        self.delays = []
        self.scopes = []

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def timeout(self, delay):
        scope = asyncio.timeout(None)
        self.delays.append(delay)
        self.scopes.append(scope)
        return scope

    async def wait_for(self, awaitable, timeout):  # noqa: ASYNC109 -- asyncio API proxy.
        assert timeout is None or timeout > 0
        async with self.timeout(timeout):
            return await awaitable

    def expire(self, index=-1):
        assert self.delays[index] is not None
        self.scopes[index].reschedule(asyncio.get_running_loop().time())


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
