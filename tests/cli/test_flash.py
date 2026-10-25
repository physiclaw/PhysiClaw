"""Tests for `physiclaw.cli.flash` — FluidNC firmware flashing command.

The firmware download is mocked with an in-memory zip and esptool runs are
intercepted at the streaming runner, so no network or serial hardware is touched.
"""

from __future__ import annotations

import importlib
import io
import logging
import stat
import subprocess
import sys
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
import typer
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

flash_mod = importlib.import_module("physiclaw.cli.flash")

app = typer.Typer()
app.command()(flash_mod.flash)
runner = CliRunner()
_IMAGE_NAMES = [
    "bootloader.bin",
    "partitions.bin",
    "boot_app0.bin",
    "firmware.bin",
    "littlefs.bin",
]


def _esptool_transcript():
    yield "Installed 13 packages in 25ms"
    yield "esptool.py v4.12.0"
    yield "Serial port COM12"
    yield "Connecting.........."
    yield "Chip is ESP32-D0WD (revision v1.1)"
    yield "MAC: f4:2d:c9:b9:68:d4"
    yield "Uploading stub..."
    yield "Auto-detected Flash size: 8MB"
    for offset, _ in flash_mod.FLASH_LAYOUT:
        yield f"Flash will be erased from {offset}..."
    for offset, name in flash_mod.FLASH_LAYOUT:
        yield "Compressed 1104320 bytes to 629167..."
        for percent in (2, 12, 26, 51, 75, 100):
            yield f"Writing at {offset}... ({percent} %)"
        seconds = "60.9" if name == "firmware.bin" else "0.1"
        yield f"Wrote 1104320 bytes (629167 compressed) at {offset} in {seconds} seconds..."
        yield "Hash of data verified."
    yield "Leaving..."
    yield "Hard resetting via RTS pin..."


def _replay_esptool(cmd, on_line):
    for line in _esptool_transcript():
        on_line(line)


def _bundle_bytes(*, skip: str | None = None, prefix: str = "") -> bytes:
    """A flat (or wrapper-folder) firmware bundle zip, built in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        names = [*_IMAGE_NAMES, "fluidnc_version.txt"]
        for name in names:
            if name == skip:
                continue
            # Arbitrary version — the CLI just echoes whatever the bundle says.
            data = b"v9.9.9\n" if name == "fluidnc_version.txt" else b"\x01"
            zf.writestr(prefix + name, data)
    return buf.getvalue()


def _zip_bytes(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


class _Response(io.BytesIO):
    def getheader(self, name: str) -> str:
        return str(len(self.getvalue()))


def _mock_download(mocker, data: bytes | None = None) -> _Response:
    """Fake HTTP opening while exercising the real download stream."""
    if data is None:
        data = _bundle_bytes()
    response = _Response(data)
    mocker.patch.object(flash_mod, "http_get", return_value=response)
    return response


def _invalid_bundle(case: str) -> bytes:
    """Malformed archives that must never reach erase/write."""
    if case == "invalid zip":
        return b"not a zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for index, (_, name) in enumerate(flash_mod.FLASH_LAYOUT):
            prefix = f"{index % 2}/" if case == "split folders" else ""
            entry = zipfile.ZipInfo(prefix + name)
            data = b"image"
            if name == "firmware.bin":
                if case == "empty image":
                    data = b""
                elif case == "directory image":
                    entry.filename += "/"
                elif case == "symlink image":
                    entry.create_system = 3
                    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(entry, data)
        if case == "duplicate image":
            with pytest.warns(UserWarning, match="Duplicate name"):
                zf.writestr("firmware.bin", b"other image")
        elif case == "multiple bundles":
            for _, name in flash_mod.FLASH_LAYOUT:
                zf.writestr("other/" + name, b"other image")
        elif case == "invalid version":
            zf.writestr("fluidnc_version.txt", b"\xff")
    return buf.getvalue()


@pytest.mark.parametrize(
    "case",
    [
        "empty image",
        "directory image",
        "symlink image",
        "split folders",
        "duplicate image",
        "multiple bundles",
        "invalid zip",
        "invalid version",
    ],
)
def test_regression_flash_invalid_bundle_never_erases_or_writes(mocker, case):
    response = _mock_download(mocker, _invalid_bundle(case))
    run_spy = mocker.patch.object(flash_mod, "_run_esptool")

    result = runner.invoke(app, ["--port", "fake-port", "--erase"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Could not prepare firmware bundle" in result.output
    assert "Done" not in result.output
    assert response.closed
    run_spy.assert_not_called()


def test_regression_flash_http_error_closes_response_before_exiting(mocker):
    body = io.BytesIO(b"unavailable")
    error = HTTPError(flash_mod.FIRMWARE_URL, 503, "unavailable", None, body)
    mocker.patch.object(flash_mod, "http_get", side_effect=error)
    run_spy = mocker.patch.object(flash_mod, "_run_esptool")

    result = runner.invoke(app, ["--port", "fake-port", "--erase"])

    assert result.exit_code == 1
    assert "Download failed" in result.output
    assert body.closed
    run_spy.assert_not_called()


@pytest.mark.parametrize("name", [name for _, name in flash_mod.FLASH_LAYOUT])
def test_flash_rejects_each_empty_image_before_erasing(mocker, name):
    entries = [(n, b"image" * (n != name)) for _, n in flash_mod.FLASH_LAYOUT]
    _mock_download(mocker, _zip_bytes(entries))
    run_spy = mocker.patch.object(flash_mod, "_run_esptool")

    result = runner.invoke(app, ["--port", "fake-port", "--erase"])

    assert result.exit_code == 1
    assert f"{name} is empty" in result.output
    run_spy.assert_not_called()


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    folders=st.lists(
        st.text(alphabet="abcXYZ09固件", min_size=1, max_size=8), max_size=3
    ),
    images=st.lists(st.binary(min_size=1, max_size=64), min_size=5, max_size=5),
)
def test_flash_preserves_image_bytes_offsets_and_uv_options(mocker, folders, images):
    prefix = "/".join([*folders, ""])
    entries = [(prefix + n, b) for (_, n), b in zip(flash_mod.FLASH_LAYOUT, images)]
    _mock_download(mocker, _zip_bytes(entries))
    captured = []

    def record(cmd, on_line):
        paths = [Path(p) for p in cmd[-9::2]]
        captured.append((cmd, on_line, paths, [p.read_bytes() for p in paths]))

    mocker.patch.object(flash_mod, "_run_esptool", side_effect=record)

    result = runner.invoke(app, ["--port", "fake-port"])

    assert result.exit_code == 0
    assert len(captured) == 1
    cmd, on_line, paths, contents = captured[0]
    assert callable(on_line)
    assert contents == images
    assert cmd[-10::2] == ["0x1000", "0x8000", "0xe000", "0x10000", "0x3d0000"]
    assert [p.name for p in paths] == [n for _, n in flash_mod.FLASH_LAYOUT]
    assert not any(p.exists() for p in paths)
    assert cmd[:7] == [
        "uv",
        "run",
        "--no-project",
        "--with",
        "esptool>=4,<5",
        "python",
        "-c",
    ]
    assert cmd[8:-10] == [
        "--chip",
        "esp32",
        "--port",
        "fake-port",
        "--baud",
        "115200",
        "--before",
        "default_reset",
        "--after",
        "hard_reset",
        "write_flash",
        "-z",
        "--flash_mode",
        "dio",
        "--flash_freq",
        "80m",
        "--flash_size",
        "detect",
    ]


def test_flash_uses_only_selected_folder_and_its_version(mocker):
    entries = [("bundle/" + name, b"image") for _, name in flash_mod.FLASH_LAYOUT]
    entries += [
        ("fluidnc_version.txt", b"wrong"),
        ("other/firmware.bin", b"unrelated"),
        ("bundle/fluidnc_version.txt", b"  v1.2.3\n"),
        ("../../ignore.txt", b"not an image"),
    ]
    _mock_download(mocker, _zip_bytes(entries))
    writes = mocker.spy(Path, "write_bytes")

    result = runner.invoke(app, ["--port", "fake-port", "--dry-run"])

    assert result.exit_code == 0
    assert "Firmware: FluidNC v1.2.3\n" in result.output
    assert "wrong" not in result.output
    assert [call.args[0].name for call in writes.call_args_list] == [
        name for _, name in flash_mod.FLASH_LAYOUT
    ]


def test_flash_does_not_use_version_from_another_folder(mocker):
    entries = [("bundle/" + name, b"image") for _, name in flash_mod.FLASH_LAYOUT]
    entries.append(("fluidnc_version.txt", b"wrong"))
    _mock_download(mocker, _zip_bytes(entries))

    result = runner.invoke(app, ["--port", "fake-port", "--dry-run"])

    assert result.exit_code == 0
    assert "Firmware: FluidNC" not in result.output


@pytest.mark.parametrize("mode", [0, stat.S_IFREG | 0o644])
def test_flash_accepts_builder_and_unix_regular_file_attributes(mocker, mode):
    entries = [(n, b"image") for _, n in flash_mod.FLASH_LAYOUT if n != "firmware.bin"]
    info = zipfile.ZipInfo("firmware.bin")
    info.create_system = 3
    info.external_attr = (mode | 0o644) << 16
    entries.append((info, b"firmware"))
    _mock_download(mocker, _zip_bytes(entries))

    result = runner.invoke(app, ["--port", "fake-port", "--dry-run"])

    assert result.exit_code == 0


@pytest.mark.parametrize(
    "error", [TimeoutError("timeout"), IncompleteRead(b"partial", 10)]
)
def test_flash_read_failure_closes_response_and_never_erases(mocker, error):
    response = _mock_download(mocker)
    mocker.patch.object(response, "read", side_effect=error)
    run_spy = mocker.patch.object(flash_mod, "_run_esptool")

    result = runner.invoke(app, ["--port", "fake-port", "--erase"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Download failed" in result.output
    assert response.closed
    run_spy.assert_not_called()


def test_flash_local_write_failure_never_erases(mocker):
    _mock_download(mocker)
    mocker.patch.object(Path, "write_bytes", side_effect=OSError("disk full"))
    run_spy = mocker.patch.object(flash_mod, "_run_esptool")

    result = runner.invoke(app, ["--port", "fake-port", "--erase"])

    assert result.exit_code == 1
    assert "Could not prepare firmware bundle: disk full" in result.output
    run_spy.assert_not_called()


def test_flash_corrupt_image_crc_never_erases(mocker):
    archive = _zip_bytes([(n, b"unique-image") for _, n in flash_mod.FLASH_LAYOUT])
    _mock_download(mocker, archive.replace(b"unique-image", b"broken-image", 1))
    run_spy = mocker.patch.object(flash_mod, "_run_esptool")

    result = runner.invoke(app, ["--port", "fake-port", "--erase"])

    assert result.exit_code == 1
    assert "Could not prepare firmware bundle" in result.output
    run_spy.assert_not_called()


def test_flash_failed_erase_does_not_write(mocker):
    _mock_download(mocker)
    run_spy = mocker.patch.object(
        flash_mod, "_run_esptool", side_effect=subprocess.CalledProcessError(2, "uv")
    )

    result = runner.invoke(app, ["--port", "fake-port", "--erase"])

    assert result.exit_code == 2
    assert run_spy.call_count == 1
    assert "erase_flash" in run_spy.call_args.args[0]
    assert "Done" not in result.output


def test_flash_dry_run_overrides_erase(mocker):
    _mock_download(mocker)
    run_spy = mocker.patch.object(flash_mod, "_run_esptool")

    result = runner.invoke(app, ["--port", "fake-port", "--erase", "--dry-run"])

    assert result.exit_code == 0
    run_spy.assert_not_called()


# ---------- dry run ----------


def test_flash_dry_run_lists_images_without_flashing(mocker) -> None:
    _mock_download(mocker)
    run_spy = mocker.patch.object(flash_mod, "_run_esptool")

    result = runner.invoke(app, ["--port", "/dev/cu.usbserial-X", "--dry-run"])

    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "firmware.bin" in result.output
    assert "littlefs.bin" in result.output
    flash_mod.http_get.assert_called_once_with(
        "https://physiclaw.ai/downloads/firmware/fluidnc.zip"
    )
    run_spy.assert_not_called()


def test_flash_dry_run_without_board_uses_autodetect_placeholder(mocker) -> None:
    _mock_download(mocker)
    mocker.patch("physiclaw.core.hardware.grbl.candidate_ports", return_value=[])

    result = runner.invoke(app, ["--dry-run"])

    assert result.exit_code == 0
    assert "Board: auto-detect" in result.output


# ---------- port detection ----------


def test_flash_no_port_found_exits_with_checklist(mocker) -> None:
    _mock_download(mocker)
    mocker.patch("physiclaw.core.hardware.grbl.candidate_ports", return_value=[])

    result = runner.invoke(app, [])

    assert result.exit_code == 1
    assert "No board found" in result.output
    assert "--port" in result.output


def test_flash_auto_detect_single_port_is_used_silently(mocker) -> None:
    _mock_download(mocker)
    mocker.patch(
        "physiclaw.core.hardware.grbl.candidate_ports",
        return_value=["/dev/cu.usbserial-A"],
    )

    result = runner.invoke(app, ["--dry-run"])

    assert result.exit_code == 0
    assert "Board: /dev/cu.usbserial-A" in result.output
    assert "several ports found" not in result.output


def test_flash_auto_detect_uses_first_of_several_ports(mocker) -> None:
    _mock_download(mocker)
    mocker.patch(
        "physiclaw.core.hardware.grbl.candidate_ports",
        return_value=["/dev/cu.usbserial-A", "/dev/cu.usbserial-B"],
    )

    result = runner.invoke(app, ["--dry-run"])

    assert result.exit_code == 0
    assert "several ports found" in result.output
    assert "using /dev/cu.usbserial-A; override with --port" in result.output
    assert "Board: /dev/cu.usbserial-A" in result.output


# ---------- bundle fetch ----------


def test_flash_download_failure_exits_nonzero(mocker) -> None:
    mocker.patch.object(flash_mod, "http_get", side_effect=URLError("boom"))

    result = runner.invoke(app, ["--port", "/dev/cu.usbserial-X", "--dry-run"])

    assert result.exit_code == 1
    assert "Download failed" in result.output
    assert flash_mod.FIRMWARE_URL in result.output


def test_flash_incomplete_bundle_exits_nonzero(mocker) -> None:
    _mock_download(mocker, _bundle_bytes(skip="littlefs.bin"))

    result = runner.invoke(app, ["--port", "/dev/cu.usbserial-X", "--dry-run"])

    assert result.exit_code == 1
    assert "littlefs.bin missing from the firmware bundle" in result.output


def test_flash_bundle_with_wrapper_folder_is_tolerated(mocker) -> None:
    _mock_download(mocker, _bundle_bytes(prefix="wrapper-folder/"))

    result = runner.invoke(app, ["--port", "/dev/cu.usbserial-X", "--dry-run"])

    assert result.exit_code == 0
    assert "firmware.bin" in result.output
    assert "Firmware: FluidNC v9.9.9" in result.output


def test_flash_reports_bundle_firmware_version(mocker) -> None:
    _mock_download(mocker)

    result = runner.invoke(app, ["--port", "/dev/cu.usbserial-X", "--dry-run"])

    assert result.exit_code == 0
    assert "Firmware: FluidNC v9.9.9" in result.output


def test_flash_bundle_without_version_file_stays_quiet(mocker) -> None:
    _mock_download(mocker, _bundle_bytes(skip="fluidnc_version.txt"))

    result = runner.invoke(app, ["--port", "/dev/cu.usbserial-X", "--dry-run"])

    assert result.exit_code == 0
    assert "Firmware: FluidNC" not in result.output


# ---------- flashing ----------


def test_flash_writes_all_images_in_layout_order(mocker) -> None:
    _mock_download(mocker)
    run_spy = mocker.patch.object(flash_mod, "_run_esptool")

    result = runner.invoke(app, ["--port", "/dev/cu.usbserial-X"])

    assert result.exit_code == 0
    assert "Done" in result.output
    assert run_spy.call_count == 1
    cmd = run_spy.call_args.args[0]
    assert "write_flash" in cmd
    assert "erase_flash" not in cmd
    positions = [cmd.index(offset) for offset, _name in flash_mod.FLASH_LAYOUT]
    assert positions == sorted(positions)


def test_flash_erase_runs_before_write(mocker) -> None:
    _mock_download(mocker)
    run_spy = mocker.patch.object(flash_mod, "_run_esptool")

    result = runner.invoke(app, ["--port", "/dev/cu.usbserial-X", "--erase"])

    assert result.exit_code == 0
    assert run_spy.call_count == 2
    first, second = (c.args[0] for c in run_spy.call_args_list)
    assert "erase_flash" in first
    assert "write_flash" not in first
    assert "write_flash" in second


def test_flash_success_points_to_testdrive(mocker) -> None:
    _mock_download(mocker)
    mocker.patch.object(flash_mod, "_run_esptool")

    result = runner.invoke(app, ["--port", "/dev/cu.usbserial-X"])

    assert result.exit_code == 0
    assert "physiclaw testdrive" in result.output


def test_flash_esptool_failure_exits_with_power_hint(mocker) -> None:
    _mock_download(mocker)
    mocker.patch.object(
        flash_mod,
        "_run_esptool",
        side_effect=subprocess.CalledProcessError(2, "uv"),
    )

    result = runner.invoke(app, ["--port", "/dev/cu.usbserial-X"])

    assert result.exit_code == 2
    assert "Flashing failed" in result.output
    assert "12 V power adaptor" in result.output


def test_flash_missing_uv_exits_nonzero(mocker) -> None:
    _mock_download(mocker)
    mocker.patch.object(flash_mod, "_run_esptool", side_effect=FileNotFoundError())

    result = runner.invoke(app, ["--port", "/dev/cu.usbserial-X"])

    assert result.exit_code == 1
    assert "`uv` not found" in result.output


def _run_generated_script(mocker, main):
    """Run the generated child code with a fake esptool, preserving exit semantics."""
    mocker.patch.dict(sys.modules, {"esptool": SimpleNamespace(main=main)})

    def run(cmd, on_line):
        mocker.patch.object(sys, "argv", ["esptool", *cmd[8:]])
        output = io.StringIO()
        try:
            with redirect_stdout(output), redirect_stderr(output):
                exec(compile(cmd[7], "<esptool launch>", "exec"), {})
        except SystemExit as error:
            raise subprocess.CalledProcessError(
                error.code, cmd, output=output.getvalue()
            ) from error
        finally:
            for line in output.getvalue().splitlines():
                on_line(line)

    return mocker.patch.object(flash_mod, "_run_esptool", side_effect=run)


def test_flash_generated_child_forwards_esptool_arguments(mocker):
    _mock_download(mocker)
    main = mocker.Mock()
    process = _run_generated_script(mocker, main)

    result = runner.invoke(app, ["--port", "fake-port"])

    assert result.exit_code == 0
    main.assert_called_once_with(process.call_args.args[0][8:])


@pytest.mark.parametrize(
    ("error", "exit_code", "message"),
    [
        (RuntimeError("synthetic failure"), 1, "esptool: synthetic failure"),
        (SystemExit(7), 7, "Flashing failed"),
    ],
)
def test_flash_generated_child_preserves_failures(mocker, error, exit_code, message):
    _mock_download(mocker)
    main = mocker.Mock(side_effect=error)
    _run_generated_script(mocker, main)

    result = runner.invoke(app, ["--port", "fake-port"])

    assert result.exit_code == exit_code
    assert message in result.output
    assert "Traceback" not in result.output
    assert "Done" not in result.output


# ---------- terminal and platform portability ----------


class _Console(io.StringIO):
    """Strict encoded output with configurable terminal capabilities."""

    def __init__(self, encoding, *, tty):
        super().__init__()
        self.codec, self.tty = encoding, tty

    @property
    def encoding(self):
        return self.codec

    def isatty(self):
        return self.tty

    def write(self, text):
        text.encode(self.encoding, "strict")
        return super().write(text)


@pytest.mark.parametrize(
    "encoding,tty,native_color,env,colored",
    [
        pytest.param("utf-8", True, True, {}, True, id="unicode-console"),
        pytest.param("ascii", True, True, {}, True, id="ascii-console"),
        pytest.param("cp1252", True, True, {}, True, id="western-windows"),
        pytest.param("gbk", True, True, {}, True, id="chinese-windows"),
        pytest.param("utf-8", True, False, {}, False, id="no-vt-support"),
        pytest.param("ascii", False, True, {}, False, id="redirected-output"),
        pytest.param("utf-8", True, True, {"NO_COLOR": ""}, False, id="no-color"),
        pytest.param("utf-8", True, True, {"TERM": "dumb"}, False, id="dumb-terminal"),
    ],
)
def test_flash_runs_with_portable_terminal_output(
    mocker, monkeypatch, encoding, tty, native_color, env, colored
):
    from physiclaw.common import terminal

    _mock_download(mocker)
    process = mocker.patch.object(
        flash_mod, "_run_esptool", side_effect=_replay_esptool
    )
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    for name in ("NO_COLOR", "PHYSICLAW_LOG_FORMAT", "PHYSICLAW_LOG_COMPONENTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    mocker.patch.object(
        terminal.platform, "enable_terminal_color", return_value=native_color
    )
    stdout = _Console(encoding, tty=tty)
    stderr = _Console(encoding, tty=tty)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    flash_mod.flash(port="COM12")

    process.assert_called_once()
    assert stderr.getvalue() == ""
    assert "Done" in stdout.getvalue()
    assert "physiclaw testdrive" in stdout.getvalue()
    assert stdout.getvalue().count("verified") == 5
    assert ("\x1b[" in stdout.getvalue()) is colored
    assert "PhysiClaw" in stdout.getvalue()


@pytest.mark.parametrize("port", ["COM12", "/dev/cu.usbserial-A", "/dev/ttyUSB0"])
def test_flash_detects_platform_ports_and_preserves_unicode_paths(
    mocker, tmp_path, port
):
    from physiclaw.core.hardware import grbl

    _mock_download(mocker)
    root = tmp_path / "固件 files"
    root.mkdir()
    mocker.patch.object(flash_mod.tempfile, "tempdir", str(root))
    mocker.patch.object(
        grbl.serial.tools.list_ports,
        "comports",
        return_value=[
            SimpleNamespace(device=port, description="USB-SERIAL CH340", vid=0x1A86)
        ],
    )
    serial = mocker.patch.object(grbl.serial, "Serial")

    def run(cmd, on_line):
        assert callable(on_line)
        assert cmd[cmd.index("--port") + 1] == port
        for (_, name), filename in zip(flash_mod.FLASH_LAYOUT, cmd[-9::2]):
            path = Path(filename)
            assert path.is_relative_to(root)
            assert path.name == name
            assert path.read_bytes() == b"\x01"
        assert cmd[-10::2] == [offset for offset, _ in flash_mod.FLASH_LAYOUT]

    process = mocker.patch.object(flash_mod, "_run_esptool", side_effect=run)

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert f"Board: {port}" in result.output
    process.assert_called_once()
    serial.assert_not_called()
    assert not list(root.iterdir())  # Temporary images are released after flashing.


@pytest.mark.parametrize("verbose", [False, True])
def test_flash_summarizes_esptool_unless_verbose_is_requested(mocker, verbose):
    _mock_download(mocker)
    mocker.patch.object(flash_mod, "_run_esptool", side_effect=_replay_esptool)

    result = runner.invoke(
        app, ["--port", "COM12", *(["--verbose"] if verbose else [])]
    )

    assert result.exit_code == 0, result.output
    assert "Done" in result.output
    if verbose:
        assert "Installed 13 packages" in result.output
        assert "MAC: f4:2d:c9:b9:68:d4" in result.output
        assert result.output.count("Hash of data verified.") == 5
    else:
        assert "Chip: ESP32-D0WD (revision v1.1)" in result.output
        assert "Flash: 8MB" in result.output
        assert "Firmware verified · 60.9s" in result.output
        assert result.output.count("verified") == 5
        for noise in ("Installed 13 packages", "MAC:", "Compressed", "0x000", "stub"):
            assert noise not in result.output
        assert "Firmware: 26%" in result.output
        assert "Firmware: 12%" not in result.output


def test_flash_keeps_warnings_and_failure_reason_without_claiming_success(mocker):
    _mock_download(mocker)

    def fail(cmd, on_line):
        on_line("WARNING: Lost connection, retrying...")
        on_line("Writing at 0x10000... (51 %)")
        raise subprocess.CalledProcessError(
            2, cmd, output="esptool: MD5 of file does not match data in flash!"
        )

    mocker.patch.object(flash_mod, "_run_esptool", side_effect=fail)
    result = runner.invoke(app, ["--port", "COM12"])

    assert result.exit_code == 2
    assert "Lost connection, retrying" in result.output
    assert "MD5 of file does not match" in result.output
    assert "--verbose" in result.output
    assert "verified" not in result.output and "Done" not in result.output


def test_esptool_runner_streams_both_channels_before_child_exits(tmp_path):
    ack = tmp_path / "确认 progress.txt"
    # The child cannot complete until the parent has rendered its first line.
    # This catches accidental buffering of the entire flash operation.
    script = """
import sys, time
from pathlib import Path
print('Chip is ESP32')
deadline = time.monotonic() + 5
while not Path(sys.argv[1]).exists():
    if time.monotonic() > deadline:
        sys.exit(9)
    time.sleep(0.01)
sys.stdout.write('Writing at 0x10000... (51 %)\\rfinal partial line')
sys.stdout.flush()
sys.stderr.write('\\nWARNING: 固件 sample\\n')
"""
    lines = []

    def receive(line):
        lines.append(line)
        ack.touch()

    flash_mod._run_esptool([sys.executable, "-c", script, str(ack)], receive)

    assert lines == [
        "Chip is ESP32",
        "Writing at 0x10000... (51 %)",
        "final partial line",
        "WARNING: 固件 sample",
    ]


def test_esptool_runner_retains_bounded_diagnostics_and_exit_code():
    lines = []
    script = (
        "import sys; [print(f'line {i}') for i in range(30)]; "
        "sys.stderr.buffer.write(b'failure: \\xff\\n'); sys.exit(7)"
    )

    with pytest.raises(subprocess.CalledProcessError) as error:
        flash_mod._run_esptool([sys.executable, "-c", script], lines.append)

    assert error.value.returncode == 7
    assert len(lines) == 31
    assert len(error.value.output.splitlines()) == 12
    assert error.value.output.endswith("failure: \ufffd")


@pytest.mark.parametrize("slow_exit", [False, True])
def test_esptool_runner_stops_and_reaps_child_on_interruption(mocker, slow_exit):
    process = mocker.MagicMock()
    process.__enter__.return_value = process
    process.stdout = iter(["Chip is ESP32\n"])
    process.wait.side_effect = (
        [subprocess.TimeoutExpired("uv", 3), 0] if slow_exit else [0]
    )
    mocker.patch.object(flash_mod.subprocess, "Popen", return_value=process)

    with pytest.raises(KeyboardInterrupt):
        flash_mod._run_esptool(["uv"], mocker.Mock(side_effect=KeyboardInterrupt))

    process.terminate.assert_called_once()
    assert process.kill.call_count == int(slow_exit)
    process.wait.assert_any_call(timeout=3)
