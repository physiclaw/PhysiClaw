"""Doctor output across terminal widths, encodings, and console capabilities."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from physiclaw.cli._doctor_report import DoctorReport, error, info, ok, warn
from physiclaw.common import terminal
from tests.terminal_fakes import Console


@pytest.fixture
def console(monkeypatch):
    if sys.platform == "win32":
        from colorama import ansitowin32

        # These in-memory streams have no Win32 console handle. Leave ANSI
        # in the capture instead of converting it into native console calls.
        monkeypatch.setattr(ansitowin32, "winapi_test", lambda: False)

    def configure(*, encoding="utf-8", width=80, mode="color"):
        stream = Console(encoding, tty=mode != "pipe")
        monkeypatch.setattr(sys, "stdout", stream)
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "dumb" if mode == "dumb" else "xterm-256color")
        monkeypatch.setenv("COLUMNS", str(width))
        if mode == "no_color":
            monkeypatch.setenv("NO_COLOR", "")
        # Native VT handling is covered by test_portable_output; exercise
        # doctor's use of the returned capability, including plain fallback.
        monkeypatch.setattr(
            terminal.platform, "enable_terminal_color", lambda stream: mode != "no_vt"
        )
        return stream

    return configure


@pytest.mark.parametrize("encoding", ["utf-8", "cp1252", "gbk", "ascii"])
@pytest.mark.parametrize("width", [40, 80])
@pytest.mark.parametrize("mode", ["color", "no_color", "dumb", "pipe", "no_vt"])
def test_report_preserves_status_and_content_on_all_terminal_modes(
    console, encoding, width, mode
):
    stream = console(encoding=encoding, width=width, mode=mode)
    report = DoctorReport()
    report.header("0.8.2", deep=False)
    report.section("Hardware")
    report.check(ok("Arm", "Connected"))
    report.check(info("Server", "Not running"))
    report.check(
        warn("Calibration", "Incomplete", "Check the camera and finish setup.")
    )
    report.check(error("Camera", "Connection check failed", "Device: café — 测试"))
    report.finish(deep=False)

    rendered = stream.getvalue()
    rendered.encode(encoding, "strict")
    assert ("\x1b[" in rendered) is (mode == "color")
    plain = terminal.ANSI_RE.sub("", rendered)
    assert not re.search(r"\b(?:OK|INFO)\b", plain)
    for label, severity in (("Calibration", "WARN"), ("Camera", "ERROR")):
        row = next(line for line in plain.splitlines() if severity in line)
        assert row.lstrip().startswith(label)
        assert terminal.cell_width(row[: row.index(severity)]) == 20
    normalized = " ".join(plain.split())
    assert "1 passed" in normalized and "2 checks need attention" in normalized
    assert "Connection check failed" in normalized
    assert "physiclaw doctor --deep" in normalized
    assert ("测试" in plain) is (encoding in {"utf-8", "gbk"})
    if mode != "pipe":
        assert all(terminal.cell_width(line) <= width for line in plain.splitlines())


def test_report_rechecks_width_and_stdout_after_redirection(console, monkeypatch):
    stream = console(width=80)
    report = DoctorReport()
    check = warn(
        "Camera", "Check the USB cable and camera permissions before retrying."
    )
    report.check(check)
    assert "Check the USB" in stream.getvalue().splitlines()[0]

    stream.seek(0)
    stream.truncate()
    monkeypatch.setenv("COLUMNS", "40")
    report.check(check)
    narrow = terminal.ANSI_RE.sub("", stream.getvalue())
    assert "Check the USB" not in narrow.splitlines()[0]
    assert all(terminal.cell_width(line) <= 40 for line in narrow.splitlines())

    pipe = Console("ascii", tty=False)
    monkeypatch.setattr(sys, "stdout", pipe)
    report.check(warn("Camera", "Check permissions → Settings"))
    assert "\x1b" not in pipe.getvalue()
    assert "Check permissions -> Settings" in pipe.getvalue()


@pytest.mark.parametrize("encoding", ["utf-8", "cp1252", "gbk", "ascii"])
def test_report_aligns_unicode_labels_and_preserves_paths(console, encoding):
    stream = console(encoding=encoding, width=80, mode="no_color")
    report = DoctorReport()
    report.check(warn("状态", "Check setup"))
    path = r"C:\Users\User\.physiclaw\calibration\bundle.json"
    report.check(info("Calibration file", "Saved", path))
    report.check(info("Linux device", "/dev/serial/by-id/usb-controller-1234567890"))
    report.add_action(
        "physiclaw setup local-vision-model", "Download the vision model."
    )
    report.finish(deep=True)

    text = stream.getvalue()
    row = next(line for line in text.splitlines() if "WARN" in line)
    assert terminal.cell_width(row[: row.index("WARN")]) == 20
    assert path in text
    assert "/dev/serial/by-id/usb-controller-1234567890" in text
    assert "\n     physiclaw setup local-vision-model\n" in text


def test_report_keeps_external_controls_out_of_redirected_output(console):
    stream = console(mode="pipe")
    report = DoctorReport()
    report.check(error("Camera", "Failed\x1b[2J\rRetry", "Device: \ud800"))
    text = stream.getvalue()
    assert "\x1b" not in text and "\r" not in text
    assert r"Failed\rRetry" in text
    assert r"Device: \ud800" in text


@pytest.mark.parametrize("encoding", ["utf-8", "cp1252", "gbk", "ascii"])
def test_report_runs_with_real_redirected_stdout_and_strict_encoding(encoding):
    root = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "PYTHONPATH": str(root / "src"),
        "PYTHONIOENCODING": f"{encoding}:strict",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from physiclaw.cli._doctor_report import DoctorReport, error; "
            "r = DoctorReport(); r.header('test', deep=True); "
            "r.check(error('Camera', 'Check permissions → Settings', 'Device: café 测试')); "
            "r.finish(deep=True)",
        ],
        env=env,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr.decode(encoding, errors="replace")
    text = result.stdout.decode(encoding, errors="strict")
    assert "\x1b" not in text
    assert re.search(r"Camera +ERROR +Check permissions", text)
    assert "1 check needs attention" in text
    assert ("测试" in text) is (encoding in {"utf-8", "gbk"})
