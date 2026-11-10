"""The installed CLI must explain first-run setup without importing CAD."""

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.slow

ENTRYPOINT = """\
from importlib.metadata import distribution
import sys
entry = next(e for e in distribution('physiclaw').entry_points if e.name == 'physiclaw')
try:
    entry.load()()
finally:
    assert not {'build123d', 'OCP'} & sys.modules.keys()
"""


@pytest.mark.parametrize(
    "model", [None, "openai/gpt-5"], ids=["missing-model", "selected-model"]
)
@pytest.mark.parametrize("argv", [[], ["mcp"]], ids=["default", "explicit-mcp"])
def test_fresh_cli_only_requires_vision_setup_without_traceback(
    physiclaw_home, model, argv
):
    env = {
        **{k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")},
        "PHYSICLAW_HOME": str(physiclaw_home),
        "PHYSICLAW_MODEL": model or "",
    }

    result = subprocess.run(
        [sys.executable, "-c", ENTRYPOINT, *argv],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert ("No provider API key configured" in result.stderr) is (not argv)
    assert ("Starting MCP-only" in result.stderr) is (not argv)
    assert "physiclaw setup local-vision-model" in result.stderr
    assert "Cannot start PhysiClaw: no model configured" not in result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert not (physiclaw_home / "run" / "server.json").exists()
