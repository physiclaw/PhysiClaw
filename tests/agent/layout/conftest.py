"""Persist real-shaped learned layouts through the filesystem boundary."""

import json
from pathlib import Path

import pytest

from physiclaw.common import paths


@pytest.fixture
def learned_layout():
    fixture = Path(__file__).parents[2] / "data/agent/learned_layout.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    target = paths.screen_layout_json()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data), encoding="utf-8")
    return data
