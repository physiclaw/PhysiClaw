"""Fresh CAD imports must use compatible font support without shell workarounds."""

import os
import subprocess
import sys
from importlib.util import find_spec

import pytest

from hardware.scheme import REPO_ROOT


@pytest.mark.parametrize(
    "module", ["hardware.parts.standard.board", "hardware.assembly.projection"]
)
def test_cad_startup_builds_engraved_board_without_fontconfig_diagnostics(module):
    if find_spec("build123d") is None:
        pytest.skip("CAD group not installed")
    env = dict(os.environ)
    env.pop("LD_PRELOAD", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import {module}\n"
            "from math import pi\n"
            "from hardware.parts.standard import board\n"
            "shape = board.MksBoard()._build()\n"
            "pcb = next(p for p in shape.children if p.label == 'pcb')\n"
            "assert pcb.is_valid and len(pcb.solids()) == 1\n"
            # Verify that the label removes material: dropping empty text
            # would make the build pass but leave an unengraved board.
            "blank = (board.board_x * board.board_y "
            "- (4 - pi) * board.corner_r**2 "
            "- 4 * pi * (board.hole_dia / 2)**2) * board.pcb_th\n"
            "assert 0 < pcb.volume < blank - 1\n",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    assert result.stdout == ""
