"""Tests for `physiclaw.cli.flash` — FluidNC firmware flashing command.

The firmware download is mocked with an in-memory zip and esptool runs are
intercepted at `subprocess.run`, so no network or serial hardware is touched.

Mutation review: 116 of 144 variants are detected. Accepted survivors only
change display text/style, the temporary directory prefix, or the fallback
exit code for CalledProcessError(0), which subprocess.run(check=True) cannot
produce. The firmware URL/names/bytes, offsets/options, bundle selection and
validation, response cleanup, and generated child code are behavior-checked.
"""

from __future__ import annotations

import importlib
import io
import stat
import subprocess
import sys
import zipfile
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
    run_spy = mocker.patch.object(flash_mod.subprocess, "run")

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
    run_spy = mocker.patch.object(flash_mod.subprocess, "run")

    result = runner.invoke(app, ["--port", "fake-port", "--erase"])

    assert result.exit_code == 1
    assert "Download failed" in result.output
    assert body.closed
    run_spy.assert_not_called()


@pytest.mark.parametrize("name", [name for _, name in flash_mod.FLASH_LAYOUT])
def test_flash_rejects_each_empty_image_before_erasing(mocker, name):
    entries = [(n, b"image" * (n != name)) for _, n in flash_mod.FLASH_LAYOUT]
    _mock_download(mocker, _zip_bytes(entries))
    run_spy = mocker.patch.object(flash_mod.subprocess, "run")

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

    def record(cmd, *, check):
        paths = [Path(p) for p in cmd[-9::2]]
        captured.append((cmd, check, paths, [p.read_bytes() for p in paths]))

    mocker.patch.object(flash_mod.subprocess, "run", side_effect=record)

    result = runner.invoke(app, ["--port", "fake-port"])

    assert result.exit_code == 0
    assert len(captured) == 1
    cmd, check, paths, contents = captured[0]
    assert check is True
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
    run_spy = mocker.patch.object(flash_mod.subprocess, "run")

    result = runner.invoke(app, ["--port", "fake-port", "--erase"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Download failed" in result.output
    assert response.closed
    run_spy.assert_not_called()


def test_flash_local_write_failure_never_erases(mocker):
    _mock_download(mocker)
    mocker.patch.object(Path, "write_bytes", side_effect=OSError("disk full"))
    run_spy = mocker.patch.object(flash_mod.subprocess, "run")

    result = runner.invoke(app, ["--port", "fake-port", "--erase"])

    assert result.exit_code == 1
    assert "Could not prepare firmware bundle: disk full" in result.output
    run_spy.assert_not_called()


def test_flash_corrupt_image_crc_never_erases(mocker):
    archive = _zip_bytes([(n, b"unique-image") for _, n in flash_mod.FLASH_LAYOUT])
    _mock_download(mocker, archive.replace(b"unique-image", b"broken-image", 1))
    run_spy = mocker.patch.object(flash_mod.subprocess, "run")

    result = runner.invoke(app, ["--port", "fake-port", "--erase"])

    assert result.exit_code == 1
    assert "Could not prepare firmware bundle" in result.output
    run_spy.assert_not_called()


def test_flash_failed_erase_does_not_write(mocker):
    _mock_download(mocker)
    run_spy = mocker.patch.object(
        flash_mod.subprocess, "run", side_effect=subprocess.CalledProcessError(2, "uv")
    )

    result = runner.invoke(app, ["--port", "fake-port", "--erase"])

    assert result.exit_code == 2
    assert run_spy.call_count == 1
    assert "erase_flash" in run_spy.call_args.args[0]
    assert "Done" not in result.output


def test_flash_dry_run_overrides_erase(mocker):
    _mock_download(mocker)
    run_spy = mocker.patch.object(flash_mod.subprocess, "run")

    result = runner.invoke(app, ["--port", "fake-port", "--erase", "--dry-run"])

    assert result.exit_code == 0
    run_spy.assert_not_called()


# ---------- dry run ----------


def test_flash_dry_run_lists_images_without_flashing(mocker) -> None:
    _mock_download(mocker)
    run_spy = mocker.patch.object(flash_mod.subprocess, "run")

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
    run_spy = mocker.patch.object(flash_mod.subprocess, "run")

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
    run_spy = mocker.patch.object(flash_mod.subprocess, "run")

    result = runner.invoke(app, ["--port", "/dev/cu.usbserial-X", "--erase"])

    assert result.exit_code == 0
    assert run_spy.call_count == 2
    first, second = (c.args[0] for c in run_spy.call_args_list)
    assert "erase_flash" in first
    assert "write_flash" not in first
    assert "write_flash" in second


def test_flash_success_points_to_testdrive(mocker) -> None:
    _mock_download(mocker)
    mocker.patch.object(flash_mod.subprocess, "run")

    result = runner.invoke(app, ["--port", "/dev/cu.usbserial-X"])

    assert result.exit_code == 0
    assert "physiclaw testdrive" in result.output


def test_flash_esptool_failure_exits_with_power_hint(mocker) -> None:
    _mock_download(mocker)
    mocker.patch.object(
        flash_mod.subprocess,
        "run",
        side_effect=subprocess.CalledProcessError(2, "uv"),
    )

    result = runner.invoke(app, ["--port", "/dev/cu.usbserial-X"])

    assert result.exit_code == 2
    assert "Couldn't reach the board" in result.output
    assert "12 V power adaptor" in result.output


def test_flash_missing_uv_exits_nonzero(mocker) -> None:
    _mock_download(mocker)
    mocker.patch.object(flash_mod.subprocess, "run", side_effect=FileNotFoundError())

    result = runner.invoke(app, ["--port", "/dev/cu.usbserial-X"])

    assert result.exit_code == 1
    assert "`uv` not found" in result.output


def _run_generated_script(mocker, main):
    """Run the generated child code with a fake esptool, preserving exit semantics."""
    mocker.patch.dict(sys.modules, {"esptool": SimpleNamespace(main=main)})

    def run(cmd, *, check):
        mocker.patch.object(sys, "argv", ["esptool", *cmd[8:]])
        try:
            exec(compile(cmd[7], "<esptool launch>", "exec"), {})
        except SystemExit as error:
            raise subprocess.CalledProcessError(error.code, cmd) from error

    return mocker.patch.object(flash_mod.subprocess, "run", side_effect=run)


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
        (SystemExit(7), 7, "Couldn't reach the board"),
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
