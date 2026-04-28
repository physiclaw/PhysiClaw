"""Tests for `physiclaw.agent.engine.prompt` — system prompt assembly.

Module-level `CONTEXT_DIR` points at `agent/context/` in the repo;
tests redirect it to a per-test tmp dir so doctrine slot rendering
is deterministic.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from physiclaw.agent.engine import memory, prompt


@pytest.fixture(autouse=True)
def _isolate_context_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    ctx = tmp_path / "context"
    ctx.mkdir()
    monkeypatch.setattr(prompt, "CONTEXT_DIR", ctx)
    return ctx


@pytest.fixture(autouse=True)
def _stub_memory_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: empty USER.md unless a test overrides."""
    monkeypatch.setattr(memory, "load_user", lambda: "")


@pytest.fixture(autouse=True)
def _stub_mcp_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: no MCP tools unless a test overrides."""
    from physiclaw.agent.engine import mcp_inventory

    monkeypatch.setattr(mcp_inventory, "discover_mcp_tools", list)


# ---------- DOCTRINE_FILE_ORDER ----------


def test_doctrine_file_order_pinned() -> None:
    assert prompt.DOCTRINE_FILE_ORDER == (
        "IDENTITY.md", "USER.md", "SOUL.md", "AGENT.md",
        "PHYSICLAW.md", "TOOLS.md", "PERSISTENCE.md", "JOBS.md",
        "CONVENTION.md",
    )


# ---------- _render_doctrine ----------


def test_render_doctrine_empty_when_no_files_exist(_isolate_context_dir) -> None:
    assert prompt._render_doctrine() == []


def test_render_doctrine_emits_section_per_existing_file(
    _isolate_context_dir: Path,
) -> None:
    (_isolate_context_dir / "IDENTITY.md").write_text("I am PhysiClaw.\n")
    (_isolate_context_dir / "AGENT.md").write_text("Be helpful.\n\n")

    out = prompt._render_doctrine()

    text = "\n".join(out)
    assert text.startswith("# Doctrine")
    assert "## IDENTITY.md" in text
    assert "I am PhysiClaw." in text
    assert "## AGENT.md" in text
    assert "Be helpful." in text


def test_render_doctrine_emits_files_in_pinned_order(
    _isolate_context_dir: Path,
) -> None:
    (_isolate_context_dir / "AGENT.md").write_text("agent body")
    (_isolate_context_dir / "IDENTITY.md").write_text("identity body")

    out = "\n".join(prompt._render_doctrine())

    # IDENTITY appears before AGENT in DOCTRINE_FILE_ORDER.
    assert out.index("## IDENTITY.md") < out.index("## AGENT.md")


def test_render_doctrine_user_md_loads_via_memory(
    _isolate_context_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory, "load_user", lambda: "User profile here")

    out = "\n".join(prompt._render_doctrine())

    assert "## USER.md" in out
    assert "User profile here" in out


def test_render_doctrine_skips_empty_files(_isolate_context_dir: Path) -> None:
    (_isolate_context_dir / "IDENTITY.md").write_text("")
    (_isolate_context_dir / "AGENT.md").write_text("body")

    out = "\n".join(prompt._render_doctrine())

    assert "## IDENTITY.md" not in out
    assert "## AGENT.md" in out


# ---------- _render_tooling ----------


def test_render_tooling_empty_when_no_tools(_isolate_context_dir) -> None:
    assert prompt._render_tooling([]) == []


def test_render_tooling_lists_local_tool_with_first_sentence(
    _isolate_context_dir,
) -> None:
    schemas = [{"name": "tap", "description": "Tap a coordinate. Then peek."}]

    out = prompt._render_tooling(schemas)

    text = "\n".join(out)
    assert "## Tooling" in text
    assert "- **tap** — Tap a coordinate." in text


def test_render_tooling_skips_tools_without_a_name(_isolate_context_dir) -> None:
    out = prompt._render_tooling([{"description": "anonymous"}])

    text = "\n".join(out)
    # Header still emitted because list is non-empty, but the tool has no name.
    assert "anonymous" not in text


def test_render_tooling_handles_tool_without_description() -> None:
    out = prompt._render_tooling([{"name": "noop"}])

    text = "\n".join(out)
    assert "- **noop**" in text
    assert "—" not in text.split("- **noop**")[1].split("\n")[0]


def test_render_tooling_includes_mcp_inventory_tools(
    _isolate_context_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    from physiclaw.agent.engine import mcp_inventory

    monkeypatch.setattr(
        mcp_inventory, "discover_mcp_tools",
        lambda: [{"name": "peek", "description": "Annotated camera frame."}],
    )

    out = "\n".join(prompt._render_tooling([{"name": "tap", "description": "Tap."}]))

    assert "- **peek** — Annotated camera frame." in out
    assert "- **tap** — Tap." in out


# ---------- _render_skills ----------


def test_render_skills_empty_when_no_context() -> None:
    assert prompt._render_skills("") == []


def test_render_skills_wraps_context_with_header_and_decision_text() -> None:
    out = "\n".join(prompt._render_skills("## Available skills\n- foo"))

    assert out.startswith("## Skill selection")
    assert "App-specific skills are mandatory" in out
    assert "## Available skills" in out


# ---------- _render_examples ----------


def test_render_examples_returns_non_empty_block() -> None:
    out = prompt._render_examples()

    text = "\n".join(out)
    assert text.startswith("## Examples")
    assert "❌ Wrong" in text
    assert "✅ Right" in text


# ---------- _render_reasoning_format ----------


def test_render_reasoning_format_empty_when_provider_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from physiclaw.agent import provider as provider_pkg

    monkeypatch.setattr(provider_pkg, "provider_class", lambda pid: None)

    assert prompt._render_reasoning_format("mystery") == []


def test_render_reasoning_format_empty_when_fragment_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeProvider:
        @classmethod
        def system_prompt_fragment(cls) -> str:
            return ""

    from physiclaw.agent.provider import registry

    monkeypatch.setattr(registry, "provider_class", lambda pid: _FakeProvider)

    assert prompt._render_reasoning_format("any") == []


def test_render_reasoning_format_via_real_anthropic_provider() -> None:
    # The real AnthropicProvider has system_prompt_fragment="" (default)
    # — so the section is NOT emitted.
    out = prompt._render_reasoning_format("anthropic")

    assert out == []


# ---------- _render_memory ----------


def test_render_memory_empty_when_no_context() -> None:
    assert prompt._render_memory("") == []


def test_render_memory_wraps_context_with_header() -> None:
    out = "\n".join(prompt._render_memory("user prefers metric"))

    assert out.startswith("## memory.md")
    assert "user prefers metric" in out


# ---------- _first_sentence ----------


def test_first_sentence_takes_first_line() -> None:
    assert prompt._first_sentence("first\nsecond") == "first"


def test_first_sentence_cuts_at_period_space() -> None:
    assert prompt._first_sentence("Tap a coord. Then peek.") == "Tap a coord."


def test_first_sentence_cuts_at_semicolon_space() -> None:
    assert prompt._first_sentence("Foo; bar") == "Foo;"


def test_first_sentence_returns_empty_for_blank_input() -> None:
    assert prompt._first_sentence("") == ""
    assert prompt._first_sentence("   ") == ""


def test_first_sentence_truncates_at_200_chars_when_no_separator() -> None:
    long = "x" * 250
    assert len(prompt._first_sentence(long)) == 200


# ---------- prefix_hash ----------


def test_prefix_hash_returns_64_char_hex_string() -> None:
    h = prompt.prefix_hash("hello")

    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_prefix_hash_deterministic_for_same_input() -> None:
    assert prompt.prefix_hash("hello") == prompt.prefix_hash("hello")


def test_prefix_hash_different_for_different_inputs() -> None:
    assert prompt.prefix_hash("a") != prompt.prefix_hash("b")


def test_prefix_hash_raises_on_non_string_input() -> None:
    with pytest.raises(
        ValueError, match=r"^prefix_hash: system_prompt must be str, got int$"
    ):
        prompt.prefix_hash(42)  # type: ignore[arg-type]


# ---------- render_system / dump (integration) ----------


def test_render_system_assembles_all_present_sections(
    _isolate_context_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (_isolate_context_dir / "IDENTITY.md").write_text("ID body")
    monkeypatch.setattr(memory, "load_user", lambda: "user profile")

    out = prompt.render_system(
        local_tool_schemas=[{"name": "tap", "description": "Tap."}],
        memory_ctx="recent fact",
        skills_ctx="## Available skills\n- foo",
        provider_id="",
    )

    assert "# Doctrine" in out
    assert "## IDENTITY.md" in out
    assert "## USER.md" in out
    assert "user profile" in out
    assert "## Tooling" in out
    assert "## Skill selection" in out
    assert "## Examples" in out
    assert "## memory.md" in out
    assert "recent fact" in out


def test_render_system_returns_empty_when_all_sections_disabled(
    _isolate_context_dir,
) -> None:
    out = prompt.render_system()

    # Examples is the only section that always emits.
    assert "## Examples" in out
    # No doctrine, no tooling, no skills, no memory.
    assert "# Doctrine" not in out
    assert "## Tooling" not in out
    assert "## Skill selection" not in out
    assert "## memory.md" not in out


def test_dump_delegates_to_render_system_with_same_args(
    _isolate_context_dir: Path,
) -> None:
    a = prompt.dump(memory_ctx="x")
    b = prompt.render_system(memory_ctx="x")

    assert a == b
