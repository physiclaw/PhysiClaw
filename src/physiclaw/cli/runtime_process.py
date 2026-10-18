"""The server's managed runtime: EOF, bounded asynchronous wait, force/reap.

Neither process signals nor a stopped ASGI lifespan are a graceful IPC channel
on Windows. Closing our private stdin pipe asks Python to unwind while HTTP
is still serving. Only deadline expiry or a repeated Ctrl+C forces termination.
"""

import asyncio
import logging
import subprocess
import time
from collections.abc import Callable

log = logging.getLogger(__name__)
GRACE_SECONDS = 30.0
FORCE_SECONDS = 5.0


class ManagedRuntime:
    def __init__(self, proc: subprocess.Popen, *, timeout: float = GRACE_SECONDS):
        self.proc = proc
        self.timeout = timeout
        self._requested = False
        self._reaped = False

    def request_stop(self) -> None:
        if self._requested:
            return
        self._requested = True
        log.info("Requesting runtime exit (stdin EOF)")
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                log.warning("Runtime control pipe close failed", exc_info=True)

    async def stop(self, force: Callable[[], bool] = lambda: False) -> None:
        if self._reaped:
            return
        self.request_stop()
        deadline = time.monotonic() + self.timeout
        while self.proc.poll() is None:
            forced = force()
            if forced or time.monotonic() >= deadline:
                log.warning(
                    "Runtime exit forced (%s)", "interrupt" if forced else "timeout"
                )
                # wait/terminate must not block MCP DELETEs or bridge requests.
                await asyncio.to_thread(terminate_child, self.proc)
                break
            await asyncio.sleep(0.05)
        self._record_exit()

    def close(self) -> None:
        """Abnormal/startup fallback when the serving loop is unavailable."""
        if self._reaped:
            return
        self.request_stop()
        if self.proc.poll() is None:
            log.warning("Runtime exit forced (control server unavailable)")
            terminate_child(self.proc)
        self._record_exit()

    def _record_exit(self) -> None:
        self._reaped = True
        log.info(
            "Runtime child reaped (pid=%s, code=%s)",
            self.proc.pid,
            self.proc.returncode,
            extra={"event": "runtime.reaped"},
        )


def terminate_child(proc: subprocess.Popen, timeout: float = FORCE_SECONDS) -> None:
    """Last resort; Windows terminate skips Python finally. Always reap."""
    if proc.poll() is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("Runtime did not terminate; killing pid=%s", proc.pid)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            proc.wait(timeout=timeout)
