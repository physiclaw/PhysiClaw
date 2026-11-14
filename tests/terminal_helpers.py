"""Real POSIX PTYs and Windows ConPTY sessions for interactive regressions."""

import codecs
import errno
import os
import re
import select
import signal
import subprocess
import sys
import time
from contextlib import contextmanager

from physiclaw.common.terminal import ANSI_RE
from tests.timing_helpers import PROCESS_TIMEOUT, WAIT_TIMEOUT


class Terminal:
    def __init__(self, reader, write, poll, interrupt):
        self.reader = reader
        self.write = write
        self.poll = poll
        self.interrupt = interrupt
        self.output = ""
        self._position = 0
        self._decode = codecs.getincrementaldecoder("utf-8")()

    def read(self):
        if select.select([self.reader], [], [], 0.05)[0]:
            try:
                data = (
                    self.reader.recv(65536)
                    if sys.platform == "win32"
                    else os.read(self.reader, 65536)
                )
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                data = b""
            self.output += self._decode.decode(data)

    def expect(self, pattern, timeout=PROCESS_TIMEOUT):
        deadline = time.monotonic() + timeout
        while True:
            match = re.search(
                pattern, ANSI_RE.sub("", self.output)[self._position :], re.S
            )
            if match:
                self._position += match.end()
                return
            assert time.monotonic() < deadline, self.output
            self.read()

    def wait(self, timeout=PROCESS_TIMEOUT):
        deadline = time.monotonic() + timeout
        while (code := self.poll()) is None:
            assert time.monotonic() < deadline, self.output
            self.read()
        self.read()
        return code


@contextmanager
def terminal_process(argv, *, env, columns=80):
    if sys.platform == "win32":
        from winpty import Backend, PtyProcess

        process = PtyProcess.spawn(
            argv, env=env, dimensions=(24, columns), backend=Backend.ConPTY
        )
        try:
            yield Terminal(
                process.fileobj,
                process.write,
                process.pty.get_exitstatus,
                process.sendintr,
            )
        finally:
            process.close(force=True)
    else:
        import pty
        import termios

        master, slave = pty.openpty()
        process = None
        try:
            termios.tcsetwinsize(slave, (24, columns))
            process = subprocess.Popen(
                argv, stdin=slave, stdout=slave, stderr=slave, env=env
            )
            yield Terminal(
                master,
                lambda text: os.write(master, text.encode()),
                process.poll,
                lambda: process.send_signal(signal.SIGINT),
            )
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=WAIT_TIMEOUT)
            os.close(master)
            os.close(slave)


def input_mode():
    """Capture the native input mode inside the child to check restoration."""
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetConsoleMode.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        mode = wintypes.DWORD()
        if not kernel.GetConsoleMode(
            msvcrt.get_osfhandle(sys.stdin.fileno()), ctypes.byref(mode)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return mode.value
    import termios

    mode = termios.tcgetattr(sys.stdin.fileno())
    mode[3] &= ~termios.PENDIN  # macOS sets PENDIN when restoring canonical input.
    return mode
