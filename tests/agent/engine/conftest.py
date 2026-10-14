"""Shared fixtures for the engine test suite."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from physiclaw.agent.engine import memory


@pytest.fixture
def validator_recursion_limit() -> Iterator[None]:
    """Keep the depth regression independent of libraries raising Python's limit."""
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)
    try:
        yield
    finally:
        sys.setrecursionlimit(previous)


@pytest.fixture
def mem_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated memory dir: the module-level paths in `memory` are
    monkeypatched to a tmp dir for the duration of the test."""
    mem = tmp_path / "memory"
    monkeypatch.setattr(memory, "MEMORY_DIR", mem)
    monkeypatch.setattr(memory, "MEMORY_FILE", mem / "memory.md")
    monkeypatch.setattr(memory, "USER_FILE", mem / "USER.md")
    return mem
