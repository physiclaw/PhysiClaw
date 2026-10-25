"""Keep firmware paths, flash offsets, and EN/ZH configuration examples aligned."""

from __future__ import annotations

import re
from pathlib import Path

from firmware import firmware
from physiclaw.cli.flash import FLASH_LAYOUT

REPO = Path(__file__).resolve().parents[2]
BUNDLE_CONFIG = REPO / "firmware" / "config.yaml"
DOCS_EN = REPO / "docs" / "set-up" / "firmware.mdx"
DOCS_ZH = REPO / "docs" / "set-up" / "firmware.zh.mdx"


def test_build_paths_are_inside_firmware_output():
    assert firmware.OUTPUT_DIR == REPO / "firmware/output"
    assert firmware.CACHE_DIR == firmware.OUTPUT_DIR / ".cache"
    assert firmware.DEFAULT_OUTPUT == firmware.OUTPUT_DIR / "fluidnc.zip"


def _yaml_block(mdx: Path) -> str:
    blocks = re.findall(r"^```yaml\n(.*?)^```", mdx.read_text(), re.S | re.M)
    assert len(blocks) == 1, f"expected exactly one yaml block in {mdx.name}"
    return blocks[0]


def _functional_lines(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_docs_yaml_blocks_en_zh_identical():
    assert _yaml_block(DOCS_EN) == _yaml_block(DOCS_ZH)


def test_docs_config_matches_bundle_source():
    # EN only — the identity test above makes the ZH comparison redundant.
    assert _functional_lines(_yaml_block(DOCS_EN)) == _functional_lines(
        BUNDLE_CONFIG.read_text()
    )


def test_build_script_layout_matches_flash_command():
    offsets = {name: int(offset, 16) for offset, name in FLASH_LAYOUT}
    assert firmware.LITTLEFS_OFFSET == offsets["littlefs.bin"]
    flashed = [name for name in firmware.ORDER if name not in firmware.REFERENCE_FILES]
    assert flashed == [name for _, name in FLASH_LAYOUT]
