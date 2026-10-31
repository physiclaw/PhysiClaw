"""Bounded line/reply handling shared by the standalone GRBL diagnostics."""

import logging
import math
import time

import serial

_OPTIONAL_ERRORS = frozenset(
    {
        "error:3",  # setting/command not recognized (GRBL + FluidNC)
        "error:162",  # FluidNC: setting disabled (now lives in YAML)
    }
)


RESPONSE_TIMEOUT_SECONDS = 12.0  # four of the original 3-second serial reads
MAX_REPLY_BYTES = 4096


def read_line(ser: serial.Serial, deadline: float) -> str | None:
    """Read one complete line with bounded bytes and native read timeouts."""
    buffer = bytearray()
    saved_timeout = ser.timeout
    try:
        while (remaining := deadline - time.monotonic()) > 0:
            timeout = (
                remaining if saved_timeout is None else min(saved_timeout, remaining)
            )
            if ser.timeout != timeout:
                ser.timeout = timeout
            chunk = ser.read(1)
            if time.monotonic() >= deadline:
                return None
            if not chunk:
                continue
            buffer.extend(chunk)
            if chunk == b"\n":
                return buffer.decode("utf-8").strip()
            if len(buffer) >= MAX_REPLY_BYTES:
                raise RuntimeError(f"GRBL reply exceeds {MAX_REPLY_BYTES} bytes")
        return None
    finally:
        if ser.timeout != saved_timeout:
            ser.timeout = saved_timeout


def send(
    ser: serial.Serial,
    cmd: str,
    *,
    log: logging.Logger,
    optional: bool = False,
    timeout: float = RESPONSE_TIMEOUT_SECONDS,
) -> None:
    """Send one line, block until 'ok'. Raise on error/alarm.

    optional=True swallows error codes that mean "this firmware doesn't
    take this setting at runtime" (`error:3`, `error:162`) — the value
    lives in YAML config (FluidNC) or simply isn't supported. Only the
    error codes we've observed empirically are swallowed.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("response timeout must be finite and positive")
    log.debug("TX %s", cmd)
    # LF gives one acknowledgment per command; FluidNC can acknowledge
    # CRLF twice, leaving a stale 'ok' for the next command.
    ser.write((cmd + "\n").encode())
    deadline = time.monotonic() + timeout
    while (line := read_line(ser, deadline)) is not None:
        log.debug("RX %s", line)
        if line == "ok":
            return
        if line.startswith("error"):
            if optional and line.replace(" ", "") in _OPTIONAL_ERRORS:
                log.warning("%s not supported by this firmware — skipping", cmd)
                return
            raise RuntimeError(f"GRBL error on {cmd!r}: {line}")
        if line.startswith("ALARM"):
            raise RuntimeError(f"GRBL alarm on {cmd!r}: {line} (try $X first)")
    raise RuntimeError(f"no reply to: {cmd}")


def query_status(ser: serial.Serial, *, timeout: float = 3.0) -> str:
    """Send '?' and return the first '<...>' status line."""
    ser.write(b"?")
    deadline = time.monotonic() + timeout
    while (line := read_line(ser, deadline)) is not None:
        if line.startswith("<") and line.endswith(">"):
            return line
    return ""
