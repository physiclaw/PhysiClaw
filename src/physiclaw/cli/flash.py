"""``physiclaw flash`` — flash FluidNC firmware onto the MKS DLC32 V2.1 (ESP32).

Downloads a prebuilt firmware bundle and writes it with esptool at the fixed
offsets FluidNC uses. Nothing to choose — the board, baud, offsets, and the
firmware itself are all decided by the bundle, to match the firmware guide:
https://docs.physiclaw.ai/en/set-up/firmware/

The bundle (``FIRMWARE_URL``) is a flat zip of ready-to-flash images:

    bootloader.bin   -> 0x1000
    partitions.bin   -> 0x8000
    boot_app0.bin    -> 0xe000
    firmware.bin     -> 0x10000    (FluidNC noradio app)
    littlefs.bin     -> 0x3d0000   (filesystem image holding config.yaml)

plus, for reference only: ``config.yaml`` and ``fluidnc_version.txt`` —
the bundle name and URL are version-free; the txt records what's inside.

To build the bundle from a FluidNC release, run
``make firmware`` (or ``uv run firmware/firmware.py``) in a repo checkout — it
assembles the zip from upstream release assets plus
``firmware/config.yaml`` (pass ``--tag`` for a newer FluidNC).

esptool runs in an ephemeral ``uv`` environment, so it isn't a permanent
dependency of the CLI.

NOTE — disconnect the stepper motors before flashing: the board can back-feed
odd voltages during the bootloader reset. Standard MKS DLC32 boards flash
fine; a board with secure-boot / flash-encryption eFuses burned would brick,
so this command does not touch those.
"""

from __future__ import annotations

import io
import os
import stat
import subprocess
import tempfile
import zipfile
import zlib
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from typing import Annotated
from urllib.error import HTTPError

import typer

from physiclaw.cli._flash_output import FlashOutput
from physiclaw.cli._format import exit_error, info, next_hint, ok, section, warn
from physiclaw.cli._http import http_get, stream
from physiclaw.common.terminal import plain_text

# Version-stable URL — the served bundle is whichever release the site last
# published (see firmware/README.md for the build + publish flow).
FIRMWARE_URL = "https://physiclaw.ai/downloads/firmware/fluidnc.zip"
CHIP = "esp32"  # MKS DLC32 V2.1
BAUD = 115200  # matches the firmware guide's install speed

# (esptool offset, filename inside the bundle).
FLASH_LAYOUT = [
    ("0x1000", "bootloader.bin"),
    ("0x8000", "partitions.bin"),
    ("0xe000", "boot_app0.bin"),
    ("0x10000", "firmware.bin"),
    ("0x3d0000", "littlefs.bin"),  # filesystem — carries config.yaml
]

# Run esptool without making it a permanent dependency. Catch its errors in
# the child so a FatalError (e.g. "No serial data received") exits cleanly with
# a one-line reason instead of dumping a Python traceback to the terminal —
# the parent then prints the friendly checklist. SystemExit (esptool's own
# clean exits) is a BaseException, so it passes straight through.
_ESPTOOL_RUN = (
    "import esptool, sys\n"
    "try:\n"
    "    esptool.main(sys.argv[1:])\n"
    "except Exception as e:\n"
    "    print(f'esptool: {e}', file=sys.stderr)\n"
    "    sys.exit(1)\n"
)


def _bundle_members(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """Select one complete folder; never combine images from different builds."""
    required = {name for _, name in FLASH_LAYOUT}
    folders: dict[PurePosixPath, list[zipfile.ZipInfo]] = {}
    for member in zf.infolist():
        path = PurePosixPath(member.filename)
        if path.name in required or path.name == "fluidnc_version.txt":
            folders.setdefault(path.parent, []).append(member)

    complete = [
        members
        for members in folders.values()
        if required <= {PurePosixPath(member.filename).name for member in members}
    ]
    if len(complete) != 1:
        present = {PurePosixPath(member.filename).name for member in zf.infolist()}
        for _, name in FLASH_LAYOUT:
            if name not in present:
                raise ValueError(f"{name} missing from the firmware bundle.")
        raise ValueError("Expected exactly one complete firmware bundle folder.")

    selected = {}
    for member in complete[0]:
        name = PurePosixPath(member.filename).name
        if name in selected:
            raise ValueError(f"Duplicate {name} in the firmware bundle.")
        mode = stat.S_IFMT(member.external_attr >> 16)
        if member.is_dir() or (
            member.create_system == 3 and mode not in (0, stat.S_IFREG)
        ):
            raise ValueError(f"{name} must be a regular file.")
        selected[name] = member
    return selected


def _fetch_bundle(into: Path) -> tuple[list[tuple[str, Path]], str | None]:
    """Prepare every image and its version before any board operation starts."""
    buf = io.BytesIO()
    try:
        with http_get(FIRMWARE_URL) as r:
            stream(r, buf.write, "  firmware")
    except (OSError, HTTPException) as e:
        if isinstance(e, HTTPError):
            with suppress(OSError):
                e.close()
        exit_error(f"Download failed: {e}\n  {FIRMWARE_URL}")

    try:
        with zipfile.ZipFile(buf) as zf:
            members = _bundle_members(zf)
            files = []
            for offset, name in FLASH_LAYOUT:
                data = zf.read(members[name])
                if not data:
                    raise ValueError(f"{name} is empty.")
                path = into / name
                path.write_bytes(data)
                files.append((offset, path))
            version_member = members.get("fluidnc_version.txt")
            version = (
                zf.read(version_member).decode("utf-8").strip()
                if version_member is not None
                else None
            )
        return files, version
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, zlib.error) as e:
        exit_error(f"Could not prepare firmware bundle: {e}")


def _detect_port() -> str | None:
    """Best-guess USB-serial port. Doesn't probe (a blank board won't reply)."""
    from physiclaw.core.hardware.grbl import candidate_ports

    ports = candidate_ports()
    if not ports:
        return None
    if len(ports) > 1:
        typer.echo(warn("several ports found"))
        typer.echo(info(f"using {ports[0]}; override with --port"))
    return ports[0]


def _run_esptool(cmd: list[str], on_line: Callable[[str], None]) -> None:
    """Drain both child streams together; UTF-8 and pipes work on every host."""
    tail: deque[str] = deque(maxlen=12)
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
    ) as process:
        assert process.stdout is not None
        try:
            for raw in process.stdout:
                line = plain_text(raw).strip()
                if line:
                    tail.append(line)
                    on_line(line)
            code = process.wait()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
    if code:
        raise subprocess.CalledProcessError(code, cmd, output="\n".join(tail))


def _uv_esptool(args: list[str], *, verbose: bool = False) -> None:
    cmd = [
        "uv",
        "run",
        "--no-project",
        "--with",
        "esptool>=4,<5",
        "python",
        "-c",
        _ESPTOOL_RUN,
        *args,
    ]
    output = FlashOutput(verbose=verbose)
    if not verbose:
        typer.echo(info("Preparing flasher and connecting to board …"))
    try:
        try:
            _run_esptool(cmd, output.feed)
        finally:
            output.close()
    except FileNotFoundError:
        exit_error("`uv` not found on PATH — esptool runs inside uv.")
    except subprocess.CalledProcessError as e:
        detail = (
            "\n".join(f"  {line}" for line in e.output.splitlines()) + "\n"
            if e.output and not verbose
            else ""
        )
        exit_error(
            "Flashing failed.\n"
            + detail
            + "  Check the USB connection and close any app using the serial port.\n"
            "  Switch on the 12 V power adaptor.\n"
            "  Full output: physiclaw flash --verbose",
            code=e.returncode or 1,
        )


def flash(
    port: Annotated[
        str | None,
        typer.Option("--port", help="Serial port (default: auto-detect)."),
    ] = None,
    erase: Annotated[
        bool,
        typer.Option(
            "--erase", help="Erase the chip before writing (fixes boot loops)."
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview firmware images without flashing."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show the full uv and esptool output."),
    ] = False,
) -> None:
    """Flash FluidNC firmware onto the MKS DLC32 control board."""
    typer.echo(section("PhysiClaw · Firmware flash"))
    typer.echo(info("MKS DLC32 V2.1 · FluidNC firmware"))
    if dry_run:
        typer.echo(warn("Dry run — preview only; the board will not be changed."))
    typer.echo()
    steps = 4 if erase and not dry_run else 3

    with tempfile.TemporaryDirectory(prefix="physiclaw-fw-") as td:
        typer.echo(section(f"[1/{steps}] Downloading firmware …"))
        files, version = _fetch_bundle(Path(td))
        typer.echo(
            ok(f"Firmware: FluidNC {version}" if version else "Firmware bundle ready.")
        )

        typer.echo()
        typer.echo(section(f"[2/{steps}] Selecting control board"))
        dev = port or _detect_port() or ("auto-detect" if dry_run else None)
        if not dev:
            exit_error(
                "No board found.\n"
                "  Switch on the 12 V power adaptor and connect the MKS DLC32 over USB.\n"
                "  Install the CH340 driver if needed, or pass --port."
            )
        if dev == "auto-detect" and dry_run:
            typer.echo(info(f"Board: {dev} (no board detected)"))
        else:
            typer.echo(ok(f"Board: {dev}"))

        if dry_run:
            typer.echo()
            typer.echo(section("[3/3] Dry run — would flash these images:"))
            typer.echo(info(f"{'Image':<18} {'Address':>10}"))
            for offset, p in files:
                typer.echo(info(f"{p.name:<18} {offset:>10}"))
            if erase:
                typer.echo()
                typer.echo(info("--erase: would erase the chip before writing."))
            return

        base = ["--chip", CHIP, "--port", dev, "--baud", str(BAUD)]
        if erase:
            typer.echo()
            typer.echo(section(f"[3/{steps}] Erasing flash …"))
            _uv_esptool([*base, "erase_flash"], verbose=verbose)
            typer.echo(ok("Flash erased."))

        write = [
            *base,
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
        for offset, p in files:
            write += [offset, str(p)]

        typer.echo()
        typer.echo(section(f"[{steps}/{steps}] Flashing firmware …"))
        typer.echo(info("About a minute. Keep the board connected."))
        typer.echo()
        _uv_esptool(write, verbose=verbose)

    typer.echo()
    typer.echo(ok("Done — the board now runs as a PhysiClaw machine."))
    typer.echo()
    typer.echo(
        next_hint("physiclaw testdrive", description="take the board for a spin.")
    )
