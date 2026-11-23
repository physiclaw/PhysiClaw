"""The serial and camera devices used by the dryrun scenario."""

import re
import time
from collections.abc import Callable
from typing import TextIO

import cv2
import numpy as np
from serial import SerialException
from serial.tools.list_ports_common import ListPortInfo

from physiclaw.core.hardware.camera import Capture

_PORT = "dryrun-serial"
_NUMBER = r"-?\d+(?:\.\d+)?"
_MOVE = re.compile(rf"G9([01])G[01]X({_NUMBER})Y({_NUMBER})(?:F\d+)?")
_ORIGIN = re.compile(rf"G92X({_NUMBER})Y({_NUMBER})Z0")
_PRESS = re.compile(r"M3S[1-9]\d*")
_DWELL = re.compile(rf"G4P({_NUMBER})")


class SerialDevice:
    """A FluidNC serial endpoint with a small, stateful motion planner."""

    def __init__(
        self,
        parked: complex,
        touch: Callable[[complex, complex, float], None],
        transcript: TextIO,
    ) -> None:
        self.position = parked
        self.touch = touch
        self.transcript = transcript
        self.failure: str | None = None
        self.port: _SerialPort | None = None

    def list_ports(self) -> list[ListPortInfo]:
        info = ListPortInfo(_PORT)
        info.description = "Dryrun CH340"
        info.vid = 0x1A86
        return [info]

    def open(
        self,
        port: str,
        baudrate: int,
        *,
        timeout: float,
        write_timeout: float | None = None,
    ) -> "_SerialPort":
        if port != _PORT or (self.port is not None and self.port.is_open):
            raise SerialException(f"Cannot open virtual serial port {port}")
        self.port = _SerialPort(self, timeout)
        return self.port


class _SerialPort:
    def __init__(self, device: SerialDevice, timeout: float) -> None:
        self.device = device
        self.timeout: float | None = timeout
        self.is_open = True
        self._reply = bytearray(b"Grbl 1.1h [FluidNC dryrun]\r\n")
        self._offset = 0j
        self._pending: complex | None = None
        self._contact_start: complex | None = None
        self._contact_time = 0.0

    @property
    def in_waiting(self) -> int:
        return len(self._reply)

    def reset_input_buffer(self) -> None:
        self._reply.clear()

    def read(self, size: int = 1) -> bytes:
        reply = bytes(self._reply[:size])
        del self._reply[:size]
        return reply

    def write(self, data: bytes, /) -> int:
        if not self.is_open:
            raise SerialException("Virtual serial port is closed")
        self.device.transcript.write(f"> {data!r}\n")
        if data == b"?":
            self._send(self._status())
        elif data.endswith(b"\n"):
            for line in data.decode("ascii").splitlines():
                self._send(self._command("".join(line.split())))
        else:
            self._send(self._unexpected(data))
        self.device.transcript.flush()
        return len(data)

    def _send(self, reply: str) -> None:
        self.device.transcript.write(f"< {reply}\n")
        self._reply.extend(reply.encode("ascii") + b"\r\n")

    def _status(self) -> str:
        work = self.device.position - self._offset
        state = "Idle" if self._pending is None else "Run"
        self._finish_move()
        return f"<{state}|WPos:{work.real:.3f},{work.imag:.3f},0.000>"

    def _finish_move(self) -> None:
        if self._pending is not None:
            self.device.position, self._pending = self._pending, None

    def _command(self, command: str) -> str:
        if command == "$I":
            return "[VER:FluidNC dryrun]\r\nok"
        if command in {"", "G21", "G90", "F8000", "$X", "$10=0", "$1=250"}:
            return "ok"
        if command in {"$32=0", "$30=1000"}:
            return "error:162"  # FluidNC settings are owned by its YAML.
        if match := _ORIGIN.fullmatch(command):
            self._finish_move()
            self._offset = self.device.position - _xy(*match.groups())
        elif match := _MOVE.fullmatch(command):
            self._finish_move()
            relative, x, y = match.groups()
            origin = self.device.position if relative == "1" else self._offset
            self._pending = origin + _xy(x, y)
        elif _PRESS.fullmatch(command):
            self._finish_move()
            if self._contact_start is None:
                self._contact_start = self.device.position
        elif match := _DWELL.fullmatch(command):
            self._finish_move()
            if self._contact_start is not None:
                self._contact_time += float(match[1])
        elif command == "M5":
            self._finish_move()
            if self._contact_start is not None:
                self.device.touch(
                    self._contact_start, self.device.position, self._contact_time
                )
            self._contact_start = None
            self._contact_time = 0.0
        else:
            return self._unexpected(command)
        return "ok"

    def _unexpected(self, command: object) -> str:
        if self.device.failure is None:
            self.device.failure = f"Unexpected virtual serial command {command!r}"
        return "error:20"

    def close(self) -> None:
        self.is_open = False


def _xy(x: str, y: str) -> complex:
    return complex(float(x), float(y))


class CaptureDevice:
    """An OpenCV-shaped capture handle returning raw BGR frames."""

    def __init__(self, frame: Callable[[], np.ndarray]) -> None:
        self._frame = frame
        self._open = True
        self._properties: dict[int, float] = {}

    def isOpened(self) -> bool:
        return self._open

    def read(self) -> tuple[bool, np.ndarray | None]:
        time.sleep(1 / 60)  # Pace the real FrameReader like a capture device.
        return (True, self._frame()) if self._open else (False, None)

    def get(self, prop: int, /) -> float:
        return self._properties.get(prop, 0.0)

    def set(self, prop: int, value: float, /) -> bool:
        self._properties[prop] = value
        return True

    def release(self) -> None:
        self._open = False


class CaptureControls:
    """Software camera controls with no native device or permission lookup."""

    def ensure_camera_permission(self) -> None:
        pass

    def camera_backend(self) -> int:
        return cv2.CAP_ANY

    def camera_size_cap(self) -> None:
        return None

    def camera_reset_controls(self) -> None:
        pass

    def camera_exposure_tunable(self) -> bool:
        return False

    def camera_set_auto_exposure(self, cap: Capture, /) -> None:
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)

    def camera_set_manual_exposure(self, cap: Capture, exposure: int, /) -> None:
        cap.set(cv2.CAP_PROP_EXPOSURE, exposure)

    def camera_focus_search_range(self, index: int, /) -> None:
        return None

    def camera_focus_lockable(self) -> bool:
        return True

    def camera_lock_focus(self, cap: Capture, /) -> bool:
        return cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)

    def camera_unlock_focus(self, cap: Capture, /) -> bool:
        return cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)

    def camera_read_focus(self, cap: Capture, /) -> float:
        return cap.get(cv2.CAP_PROP_FOCUS)

    def camera_apply_focus(self, cap: Capture, value: float, /) -> bool:
        return self.camera_lock_focus(cap) and cap.set(cv2.CAP_PROP_FOCUS, value)
