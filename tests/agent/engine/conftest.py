"""Shared fixtures for the engine test suite."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from physiclaw.agent.engine import memory
from physiclaw.macros.parse import parse_macro


@pytest.fixture
def isolated_job_store(tmp_path, monkeypatch):
    """Keep both job readers and writers on the same per-test path."""
    from physiclaw.agent.engine import job_store, jobs

    path = tmp_path / "jobs.md"
    monkeypatch.setattr(job_store, "JOBS_PATH", path)
    monkeypatch.setattr(jobs, "JOBS_PATH", path)
    return path


@pytest.fixture
def private_macro_pack():
    """A plugin-owned hand, callable only on synthesized turns."""
    spec = parse_macro(
        "kind: macro\nschema: 1\nname: leg\ndescription: Private hand\n"
        "steps:\n  - home_screen\n",
        "leg",
    )
    return {"demo/leg": spec}


@pytest.fixture
def private_macro_run(mocker, private_macro_pack):
    """Real assembly, loop and dispatch with provider/MCP/plugin boundary fakes."""
    from types import SimpleNamespace

    from engine_fakes import FakeMcpClient, FakeProvider, _asst, _tc

    from physiclaw.agent.engine import engine, mcp_tool
    from physiclaw.agent.runtime.sentinel import DONE
    from physiclaw.contract.plugin import SessionSetup

    mcp = FakeMcpClient()
    provider = FakeProvider(
        [
            _asst(
                tool_calls=[
                    _tc("note", {"summary": "private leg finished"}),
                    _tc("end_session", {"status": DONE, "recap": "finished"}),
                ]
            )
        ]
    )
    hand = _asst(tool_calls=[_tc("run_macro", {"name": "demo/leg"})])
    hand.synthesized = True
    plugin = SimpleNamespace(
        session_setup=mocker.AsyncMock(
            return_value=SessionSetup(gated_macros=private_macro_pack)
        ),
        advance=mocker.AsyncMock(side_effect=[hand, None]),
        aclose=mocker.AsyncMock(),
    )
    mocker.patch.object(engine, "get_mcp", new=mocker.AsyncMock(return_value=mcp))
    mocker.patch.object(mcp_tool, "get_mcp", new=mocker.AsyncMock(return_value=mcp))
    mocker.patch.object(
        engine,
        "list_tools_cached",
        new=mocker.AsyncMock(
            return_value=[
                {
                    "name": "remote_probe",
                    "description": "Remote schema validation probe",
                    "input_schema": {
                        "type": "object",
                        "properties": {"value": {"type": "integer", "minimum": 1}},
                        "required": ["value"],
                    },
                }
            ]
        ),
    )
    mocker.patch.object(engine, "make_provider", return_value=provider)
    mocker.patch.object(engine.plugin_loader, "load_plugins", return_value=(plugin,))
    mocker.patch.object(engine.CONFIG.engine, "max_attempts", 1)
    return SimpleNamespace(
        mcp=mcp, provider=provider, plugin=plugin, chat=mocker.spy(provider, "chat")
    )


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
