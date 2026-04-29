"""Auto-detect GRBL devices on serial ports."""

import logging
import time

import serial
import serial.tools.list_ports
from serial.tools.list_ports_common import ListPortInfo

log = logging.getLogger(__name__)

GRBL_BAUDRATE = 115200

# Skip ports that are never GRBL devices
_SKIP_KEYWORDS = {"bluetooth", "bt-", "debug", "wlan", "wifi", "airpods"}

# GRBL boards typically use CH340/CP210x/FTDI USB-serial chips. Match on
# both macOS-style device names (`/dev/tty.usbserial-XXXX`) and Windows-style
# port descriptions (`USB-SERIAL CH340 (COM3)`, `Arduino Uno (COM5)`).
_LIKELY_KEYWORDS = {
    "ch340", "cp210", "ftdi", "usbserial", "usbmodem", "wch",
    "serial", "arduino", "prolific",
}


def _probe_port(port: str, baudrate: int = GRBL_BAUDRATE) -> str | None:
    """Try to connect to a port and identify a GRBL device.

    Uses $I query (no reset side-effect, faster than soft-reset).
    Returns the version string if GRBL is detected, None otherwise.
    """
    try:
        with serial.Serial(port, baudrate, timeout=2) as ser:
            time.sleep(2)
            ser.reset_input_buffer()
            ser.write(b"$I\r\n")
            time.sleep(0.5)
            resp = ser.read(ser.in_waiting or 256).decode("utf-8", errors="ignore")
            if "grbl" in resp.lower() or "[VER:" in resp:
                # Extract version line
                for line in resp.splitlines():
                    if "grbl" in line.lower() or "[VER:" in line:
                        return line.strip()
                return "GRBL"
    except (serial.SerialException, OSError):
        pass
    return None


def _port_priority(port_info) -> int:
    """Lower value = probe first. Likely USB-serial chips first, unlikely ports last."""
    name = (port_info.device + " " + (port_info.description or "")).lower()
    if any(kw in name for kw in _SKIP_KEYWORDS):
        return 99  # skip these entirely
    if any(kw in name for kw in _LIKELY_KEYWORDS):
        return 0  # most likely GRBL
    return 50  # unknown, probe after likely ones


def _candidate_port_infos() -> list[ListPortInfo]:
    """port_info objects that look worth probing (skipping bluetooth/debug etc.).

    Sorted by likelihood — likely USB-serial chips first. Shared by
    detect_grbl() (which probes them) and candidate_ports() (diagnostics).
    """
    return sorted(
        [p for p in serial.tools.list_ports.comports() if _port_priority(p) < 99],
        key=lambda p: (_port_priority(p), p.device),
    )


def candidate_ports() -> list[str]:
    """Device names that detect_grbl() would probe. For diagnostics."""
    return [p.device for p in _candidate_port_infos()]


def detect_grbl() -> str | None:
    """Scan serial ports and return the first GRBL device port, or None.

    Skips Bluetooth/debug ports and probes likely USB-serial ports first.
    """
    ports = _candidate_port_infos()
    if not ports:
        log.warning("No candidate serial ports found.")
        return None

    log.debug(f"Scanning {len(ports)} serial port(s) for GRBL")
    for port_info in ports:
        desc = port_info.description or ""
        log.debug(f"  Probing {port_info.device}  ({desc})")

        version = _probe_port(port_info.device)
        if version:
            log.info(f"GRBL found: {port_info.device}  [{version}]")
            return port_info.device
        else:
            log.debug(f"  {port_info.device}: not GRBL")

    return None
