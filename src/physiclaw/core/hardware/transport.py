"""SerialTransport — the GRBL line protocol over one owned serial port.

Owns the send/ok handshake, the ``?`` status query, and the port lock.
The lock makes reply-stream integrity a property of the transport
itself: two threads interleaving writes would desync GRBL's one-reply-
per-command stream (a later command then reads a stale error), so every
wire interaction serializes here instead of relying on each caller to
hold the rig-level busy lock.

``StylusArm`` composes this the same way it composes ``Solenoid`` —
gesture/G-code logic stays in the arm, wire mechanics live here.
"""

import logging
import re
import threading
import time

from physiclaw.core.hardware.device import DeviceAlarm, DeviceTimeout, ProtocolError

log = logging.getLogger(__name__)

# Error codes that mean "this firmware doesn't take this setting at
# runtime" — the value lives in YAML config (FluidNC) or isn't supported.
# Swallowed only when a command is sent with optional=True. Matches
# scripts/grbl_solenoid_test.py.
_OPTIONAL_ERRORS = frozenset({"error:3", "error:162"})


class SerialTransport:
    """One GRBL wire: an open ``serial.Serial`` plus the line protocol."""

    # Four of the arm's 3s serial timeouts. Diagnostic chatter must not
    # keep the acknowledgment loop alive forever. Each native read also
    # has the port's own timeout and a size bound.
    RESPONSE_TIMEOUT_SECONDS = 12.0
    MAX_REPLY_BYTES = 4096

    def __init__(self, ser) -> None:
        self.ser = ser
        # RLock, not Lock: cheap insurance if a caller ever nests a send
        # inside a send-driven callback; never a behavioral difference on
        # the current straight-line call paths.
        self._lock = threading.RLock()
        self._buffer = bytearray()
        self._synchronized = True

    def send(self, cmd: str, wait_ok: bool = True, optional: bool = False) -> None:
        """Send a single command, block until 'ok'.

        optional=True swallows the "setting not accepted at runtime" error
        codes (error:3 / error:162) so PWM-config writes ($32/$30) work on
        FluidNC (which takes them from YAML) and a bare GRBL board alike.
        A timeout, framing failure, or wait_ok=False leaves acknowledgment
        ownership unknown: reconnect before sending another command. M5
        remains available as an unacknowledged emergency release.
        """
        if (
            not isinstance(cmd, str)
            or not cmd.strip()
            or not cmd.isascii()
            or not cmd.isprintable()
            or any(char in cmd for char in "?!~")
        ):
            raise ValueError(
                "GRBL command must be one ASCII line without realtime controls"
            )
        with self._lock:
            if not self._synchronized:
                # An unacknowledged command makes subsequent replies ambiguous.
                # Still permit an emergency coil-off write, but never claim it
                # was acknowledged or allow another motion until reconnect.
                if cmd == "M5":
                    self.ser.write(b"M5\n")
                raise ProtocolError("GRBL reply stream is unsynchronized; reconnect")
            self._synchronized = False
            log.debug(f"[GRBL] sent {cmd}")
            # LF only — NOT CRLF. FluidNC v4 treats the trailing `\r` + `\n`
            # as two line endings (the command + an empty line) and acks BOTH
            # with `ok`; that extra `ok` lags every reply by one and desyncs
            # the stream (a later command then reads a stale error). `\n` is
            # the canonical GRBL terminator and yields exactly one reply per
            # command.
            self.ser.write((cmd + "\n").encode())

            if not wait_ok:
                return

            deadline = time.monotonic() + self.RESPONSE_TIMEOUT_SECONDS
            empty_reads = 0
            while time.monotonic() < deadline:
                raw = self._read_line(deadline)
                if raw is None:
                    empty_reads += 1
                    if empty_reads > 3:
                        break
                    continue
                empty_reads = 0
                try:
                    line = raw.decode("utf-8").strip()
                except UnicodeError as exc:
                    raise ProtocolError(
                        "Invalid UTF-8 in GRBL reply; reconnect"
                    ) from exc
                log.debug(f"[GRBL] got {line}")
                if line == "ok":
                    self._synchronized = True
                    return
                if re.fullmatch(r"error: *[0-9]+", line):
                    self._synchronized = True
                    if optional and line.replace(" ", "") in _OPTIONAL_ERRORS:
                        # FluidNC follows a rejected setting with a trailing
                        # `[MSG:ERR: ...]` line (no `ok`). It's a
                        # non-terminator, so the next command's read loop
                        # skips it harmlessly — no draining needed now that
                        # we send LF (single reply each).
                        log.debug(
                            f"[GRBL] {cmd}: {line} — not accepted at runtime, skipping"
                        )
                        return
                    raise ProtocolError(f"GRBL error: {line}  command: {cmd}")
                if re.fullmatch(r"ALARM: *[0-9]+", line):
                    # ALARM can be unsolicited, so the command's own reply
                    # may still be queued behind it. Reconnect before unlock.
                    raise DeviceAlarm(f"GRBL alarm: {line}; reconnect before unlocking")
                if line.startswith(("error", "ALARM")):
                    raise ProtocolError(f"Malformed GRBL reply: {line}; reconnect")
            raise DeviceTimeout(f"GRBL not responding, command: {cmd}")

    def _read_line(self, deadline: float) -> bytes | None:
        """Read a complete line under the wire lock, retaining partial bytes.

        Read available chunks, limiting each native read to the remaining
        time. Unlike read_until(), this also bounds the last blocking read.
        None means this read timed out; a caller may retry within its budget.
        """
        while time.monotonic() < deadline:
            end = self._buffer.find(b"\n")
            if end >= 0:
                line = bytes(self._buffer[: end + 1])
                del self._buffer[: end + 1]
                return line
            if len(self._buffer) >= self.MAX_REPLY_BYTES:
                self._synchronized = False
                raise ProtocolError(f"GRBL reply exceeds {self.MAX_REPLY_BYTES} bytes")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            timeout = self.ser.timeout
            read_timeout = remaining if timeout is None else min(timeout, remaining)
            try:
                if read_timeout != timeout:
                    self.ser.timeout = read_timeout
                chunk = self.ser.read(
                    min(
                        self.ser.in_waiting or 1,
                        self.MAX_REPLY_BYTES - len(self._buffer),
                    )
                )
            finally:
                if read_timeout != timeout:
                    self.ser.timeout = timeout
            self._buffer.extend(chunk)
            if not chunk:
                return None
        return None

    def query_status(self) -> str:
        """Query current status (`?`), return a complete `<...>` line or ''."""
        with self._lock:
            self.ser.write(b"?")
            time.sleep(0.1)
            deadline = time.monotonic() + (
                self.ser.timeout or self.RESPONSE_TIMEOUT_SECONDS
            )
            while time.monotonic() < deadline:
                raw = self._read_line(deadline)
                if raw is None:
                    return ""
                try:
                    line = raw.decode("utf-8").strip()
                except UnicodeError:
                    continue  # Corrupted coordinates must not become plausible numbers.
                if line.startswith("<") and line.endswith(">"):
                    log.debug(f"[GRBL] got {line}")
                    return line
        return ""

    def read_startup(self, max_bytes: int = 256) -> str:
        """Drain and return whatever the firmware printed at boot."""
        with self._lock:
            return self.ser.read(self.ser.in_waiting or max_bytes).decode(
                "utf-8", errors="ignore"
            )

    def reset_input_buffer(self) -> None:
        with self._lock:
            self.ser.reset_input_buffer()
            self._buffer.clear()

    def send_bounded(self, cmd: str, *, lock_timeout: float) -> bool:
        """``send``, but give up if the wire lock can't be won in
        ``lock_timeout`` — for teardown, where a holder abandoned inside a
        blocking read (a daemon killed at interpreter exit) must not hang
        an atexit handler (``Camera.close``'s bounded-acquire precedent).
        Returns whether the command was sent. The port close remains the
        true backstop: it unblocks the stuck reader, and the DTR drop
        resets the board."""
        if not self._lock.acquire(timeout=lock_timeout):
            log.warning(
                "[GRBL] wire lock held > %.1fs — skipping %r", lock_timeout, cmd
            )
            return False
        try:
            self.send(cmd)
            return True
        finally:
            self._lock.release()

    def close(self) -> None:
        self.ser.close()
        log.debug("[GRBL] serial port closed")
