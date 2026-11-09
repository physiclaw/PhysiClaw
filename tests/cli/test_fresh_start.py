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
    "model,expected",
    [
        (None, "no model configured"),
        ("openai/gpt-5", "physiclaw setup local-vision-model"),
    ],
    ids=["missing-model", "missing-vision-model"],
)
def test_fresh_cli_reports_required_setup_without_traceback(
    physiclaw_home, model, expected
):
    env = {
        **os.environ,
        "PHYSICLAW_HOME": str(physiclaw_home),
        "PHYSICLAW_MODEL": model or "",
    }

    result = subprocess.run(
        [sys.executable, "-c", ENTRYPOINT],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert expected in result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert not (physiclaw_home / "run" / "server.json").exists()
