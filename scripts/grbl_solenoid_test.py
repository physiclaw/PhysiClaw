"""Smoke-test a GRBL-driven solenoid using the hit-and-keep pattern.

Only external dependency: pyserial. Uses the repository logging contract.

Solenoids need brief peak current (~500 mA) to pull the iron core in,
then a much smaller hold current (~100 mA) to keep it seated.
Continuous peak current burns the coil out in 30-60 s.

Usage:
    .venv/bin/python scripts/grbl_solenoid_test.py             # 3 taps + 2s hold
    .venv/bin/python scripts/grbl_solenoid_test.py --keep-on 5 # diagnostic: 5s on, no timing
    .venv/bin/python scripts/grbl_solenoid_test.py --hold 10 --s-hold 150
    .venv/bin/python scripts/grbl_solenoid_test.py --laser     # PWM on laser pin instead

Empirical baseline (MKS DLC32, 2026-05-06):
    $32=0 (spindle mode required — laser mode emits PWM only during motion)
    Hit S1000, 80 ms settle, hold S150 — coil stays cool 10 s+, drops out at S100.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# Run directly from a checkout without requiring the full application install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from physiclaw.common.log_style import (  # noqa: E402
    add_log_arguments,
    configure_console,
    report_error,
    run_diagnostic,
)

log = logging.getLogger("physiclaw.scripts.grbl_solenoid_test")

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    report_error(
        "pyserial not installed; install pyserial to run this diagnostic.",
        tag="diagnostic",
    )
    raise SystemExit(1) from None


# Support both `python scripts/...py` and package imports from tests/tools.
if __package__:
    from . import grbl_protocol
else:
    import grbl_protocol


# ─── GRBL connection helpers ───────────────────────────────────────

BAUD = 115200
_SKIP = ("bluetooth", "bt-", "debug", "wlan", "wifi", "airpods")
_LIKELY = (
    "ch340",
    "cp210",
    "ftdi",
    "usbserial",
    "usbmodem",
    "wch",
    "arduino",
    "prolific",
)


def find_port() -> str | None:
    """Probe likely USB-serial ports for a GRBL board, return first hit."""
    candidates = []
    for p in serial.tools.list_ports.comports():
        name = (p.device + " " + (p.description or "")).lower()
        if any(k in name for k in _SKIP):
            continue
        priority = 0 if any(k in name for k in _LIKELY) else 1
        candidates.append((priority, p.device))
    candidates.sort()

    for _, dev in candidates:
        log.debug("Probing %s", dev)
        try:
            with serial.Serial(dev, BAUD, timeout=2, write_timeout=2) as ser:
                time.sleep(2)
                ser.reset_input_buffer()
                ser.write(b"$I\n")
                time.sleep(0.5)
                resp = ser.read(ser.in_waiting or 256).decode("utf-8", "ignore")
                if "grbl" in resp.lower() or "[VER:" in resp:
                    first = resp.strip().splitlines()[0] if resp.strip() else "ok"
                    log.debug("GRBL detected on %s: %s", dev, first)
                    return dev
                log.debug("No GRBL response on %s", dev)
        except (serial.SerialException, OSError) as e:
            log.debug("Cannot open %s: %s", dev, e)
    return None


def connect(port: str | None) -> serial.Serial:
    """Resolve port (auto-detect if None), open, drain the reset banner."""
    if port is None:
        log.info("Scanning serial ports for a GRBL board")
        port = find_port()
        if port is None:
            raise SystemExit("No GRBL device found. Pass --port explicitly.")
    log.info("Opening %s at %d baud", port, BAUD)
    ser = serial.Serial(port, BAUD, timeout=3, write_timeout=3)
    try:
        time.sleep(2)  # wait for board reset (DTR pulse on open)
        banner = (
            ser.read(min(ser.in_waiting or 256, 4096)).decode("utf-8", "ignore").strip()
        )
        if banner:
            log.debug("Reset banner: %r", banner)
        ser.reset_input_buffer()
    except BaseException:
        ser.close()
        raise
    return ser


RESPONSE_TIMEOUT_SECONDS = grbl_protocol.RESPONSE_TIMEOUT_SECONDS


def send(
    ser: serial.Serial,
    cmd: str,
    *,
    optional: bool = False,
    timeout: float = RESPONSE_TIMEOUT_SECONDS,
) -> None:
    """Send a command through the shared protocol with this diagnostic's logger."""
    grbl_protocol.send(ser, cmd, log=log, optional=optional, timeout=timeout)


def query_status(ser: serial.Serial, *, timeout: float = 3.0) -> str:
    """Send '?' and return the first complete status line."""
    return grbl_protocol.query_status(ser, timeout=timeout)


def unlock_if_alarmed(ser: serial.Serial) -> None:
    """Print status; if Alarm, send $X to clear it."""
    status = query_status(ser)
    log.debug("Status: %s", status)
    if "Alarm" in status:
        log.warning("Board alarm — sending $X to unlock")
        send(ser, "$X")


# ─── Script-specific helpers ─────────────────────────────────────────


def dump_settings(ser: serial.Serial, duration: float = 2.0) -> None:
    """Send `$$` and print every line received over `duration` seconds.

    Time-bounded — doesn't try to detect a trailing `ok`, since stale
    `ok`s from prior commands can stop a stop-on-ok loop early.
    """
    log.debug("Settings dump ($$)")
    ser.reset_input_buffer()
    ser.write(b"$$\n")
    deadline = time.monotonic() + duration
    while (line := grbl_protocol.read_line(ser, deadline)) is not None:
        if line:
            log.debug("Setting: %s", line)
    log.debug("Settings dump complete")


# ─── Main ────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Hit-and-keep solenoid test via M3/M5.")
    ap.add_argument("--port", help="serial device (default: auto-detect)")
    ap.add_argument("--taps", type=int, default=3, help="short taps (default: 3)")
    ap.add_argument(
        "--tap-ms", type=int, default=80, help="tap contact ms (default: 80)"
    )
    ap.add_argument(
        "--gap", type=float, default=0.5, help="seconds between taps (default: 0.5)"
    )
    ap.add_argument(
        "--hold",
        type=float,
        default=2.0,
        help="long-hold seconds, 0 to skip (default: 2.0)",
    )
    ap.add_argument(
        "--s-hit", type=int, default=1000, help="peak S-value (default: 1000)"
    )
    ap.add_argument(
        "--s-hold",
        type=int,
        default=150,
        help="hold S-value (default: 150 = 15%% duty)",
    )
    ap.add_argument(
        "--settle-ms", type=int, default=80, help="settle dwell after hit (default: 80)"
    )
    ap.add_argument(
        "--keep-on",
        type=float,
        default=0.0,
        help="diagnostic: hold M3 on for N seconds, skip taps + hold",
    )
    ap.add_argument(
        "--laser",
        action="store_true",
        help="use laser mode ($32=1) instead of spindle mode ($32=0)",
    )
    add_log_arguments(ap)
    args = ap.parse_args()
    configure_console("diagnostic", verbose=args.verbose, log_format=args.log_format)
    laser_mode = 1 if args.laser else 0

    with connect(args.port) as ser:
        send(ser, "$I")
        unlock_if_alarmed(ser)

        # PWM setup. Optional because each firmware accepts a different
        # subset — FluidNC v4 takes these from YAML config and rejects
        # the live writes, the legacy MKS GRBL fork accepts $32 and
        # ignores the rest. We try the writes anyway so this script also
        # works on a bare GRBL board with no config.
        send(ser, f"$32={laser_mode}", optional=True)
        send(ser, "$33=20000", optional=True)  # 20 kHz, above adult hearing
        send(ser, "$30=1000", optional=True)  # S-range 0..1000

        dump_settings(ser)
        send(ser, "M5")  # failsafe — start with coil off

        try:
            if args.keep_on > 0:
                pin = "LASER" if laser_mode else "SPINDLE"
                log.info(
                    "Keep on for %.1fs at S%d ($32=%d, %s pin)",
                    args.keep_on,
                    args.s_hit,
                    laser_mode,
                    pin,
                )
                send(ser, f"M3 S{args.s_hit}")
                time.sleep(args.keep_on)
                send(ser, "M5")
                return 0

            # Production sequence: short taps + optional long hit-and-keep hold.
            tap_s = args.tap_ms / 1000.0
            for i in range(args.taps):
                log.info("Tap %d/%d (%d ms)", i + 1, args.taps, args.tap_ms)
                send(ser, f"M3 S{args.s_hit}")
                send(ser, f"G4 P{tap_s:.3f}", timeout=RESPONSE_TIMEOUT_SECONDS + tap_s)
                send(ser, "M5")
                time.sleep(args.gap)

            if args.hold > 0:
                settle_s = args.settle_ms / 1000.0
                log.info(
                    "Hold %.2fs at S%d (hit S%d, settle %d ms)",
                    args.hold,
                    args.s_hold,
                    args.s_hit,
                    args.settle_ms,
                )
                send(ser, f"M3 S{args.s_hit}")
                send(
                    ser,
                    f"G4 P{settle_s:.3f}",
                    timeout=RESPONSE_TIMEOUT_SECONDS + settle_s,
                )
                send(ser, f"M3 S{args.s_hold}")
                send(
                    ser,
                    f"G4 P{args.hold:.3f}",
                    timeout=RESPONSE_TIMEOUT_SECONDS + args.hold,
                )
                send(ser, "M5")
        finally:
            # Belt-and-braces — never exit with the coil energized.
            try:
                send(ser, "M5")
            except Exception:
                pass

    log.info("Diagnostic completed", extra={"event": "diagnostic.completed"})
    return 0


if __name__ == "__main__":
    sys.exit(run_diagnostic(main))
