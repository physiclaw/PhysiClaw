"""Keep firmware build paths and flash offsets aligned with the CLI."""

from __future__ import annotations

from pathlib import Path

from firmware import firmware
from physiclaw.cli.flash import FLASH_LAYOUT

REPO = Path(__file__).resolve().parents[2]


def test_build_paths_are_inside_firmware_output():
    assert firmware.OUTPUT_DIR == REPO / "firmware/output"
    assert firmware.CACHE_DIR == firmware.OUTPUT_DIR / ".cache"
    assert firmware.DEFAULT_OUTPUT == firmware.OUTPUT_DIR / "fluidnc.zip"


def test_build_script_layout_matches_flash_command():
    offsets = {name: int(offset, 16) for offset, name in FLASH_LAYOUT}
    assert firmware.LITTLEFS_OFFSET == offsets["littlefs.bin"]
    flashed = [name for name in firmware.ORDER if name not in firmware.REFERENCE_FILES]
    assert flashed == [name for _, name in FLASH_LAYOUT]
