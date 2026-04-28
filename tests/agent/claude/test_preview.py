"""Tests for `physiclaw.agent.claude.preview`."""
from __future__ import annotations

from pathlib import Path

import pytest
import typer

from physiclaw.agent.claude import preview
from physiclaw.agent.claude.preview import (
    _arg_after,
    _print_tree,
    claude_preview,
)


# ---------- _arg_after ----------


def test_arg_after_returns_value_following_flag() -> None:
    argv = ["bin", "--mcp-config", "/tmp/x.json", "--other", "y"]

    assert _arg_after(argv, "--mcp-config") == "/tmp/x.json"


def test_arg_after_raises_when_flag_missing() -> None:
    with pytest.raises(RuntimeError, match="--mcp-config not found in argv"):
        _arg_after(["bin", "-p", "trigger"], "--mcp-config")


def test_arg_after_raises_when_flag_is_last_token() -> None:
    with pytest.raises(RuntimeError, match="--p not found in argv"):
        _arg_after(["bin", "--p"], "--p")


# ---------- _print_tree ----------


def test_print_tree_lists_dir_and_file_with_size(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    f = sub / "x.txt"
    f.write_text("hello")  # 5 bytes

    _print_tree(tmp_path)
    out = capsys.readouterr().out

    assert "sub/" in out
    assert "x.txt  (5b)" in out


def test_print_tree_shows_symlink_target(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(target)

    _print_tree(tmp_path)
    out = capsys.readouterr().out

    assert "alias →" in out
    assert "real" in out


def test_print_tree_indents_by_depth(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "leaf.txt").write_text("x")

    _print_tree(tmp_path)
    out = capsys.readouterr().out

    # "a" is depth-1, "b" is depth-2 ("  " * 2 = 4 spaces),
    # leaf.txt at depth-3 has six leading spaces.
    assert "  a/" in out
    assert "    b/" in out
    assert "      leaf.txt  (1b)" in out


# ---------- claude_preview wiring ----------


def _patch_preview(mocker, *, build_cmd_side_effect=None,
                   model_ref_raise=None, parse_model_raise=None) -> dict:
    """Stub model lookup, spawn helpers, plugin dir."""
    if model_ref_raise is not None:
        mocker.patch.object(
            preview, "model_ref_with_source",
            side_effect=model_ref_raise, create=True,
        )
    else:
        mocker.patch(
            "physiclaw.config.model_ref_with_source",
            return_value=("anthropic:claude-x", "config"),
        )

    if parse_model_raise is not None:
        mocker.patch(
            "physiclaw.config.parse_model_ref", side_effect=parse_model_raise,
        )
    else:
        mocker.patch(
            "physiclaw.config.parse_model_ref",
            return_value=("anthropic", "claude-x"),
        )

    mocker.patch.object(preview._spawn, "_mcp_tools", return_value=[
        {"name": "physiclaw__peek"},
        {"name": "physiclaw__tap"},
    ])
    mocker.patch.object(preview.skill, "discover", return_value={})

    fake_plugin_dir = Path("/tmp/fake-plugin-dir")
    mocker.patch.object(preview, "prepare_plugin_dir", return_value=fake_plugin_dir)
    mocker.patch.object(preview, "_print_tree")  # don't try to walk fake path

    mocker.patch.object(
        preview._spawn, "_render_system_prompt",
        return_value="\n".join(f"line-{i}" for i in range(60)),
    )

    fake_cmd = [
        "claude",
        "--append-system-prompt", "PROMPT",
        "--plugin-dir", str(fake_plugin_dir),
        "--mcp-config", "/tmp/mcp.json",
        "-p", "trigger prompt body",
        "--allowedTools", "Read,Edit,Bash",
        "--disallowedTools", "WebSearch,WebFetch",
        "--model", "claude-x",
    ]
    if build_cmd_side_effect is not None:
        mocker.patch.object(
            preview._spawn, "_build_cmd", side_effect=build_cmd_side_effect,
        )
    else:
        mocker.patch.object(preview._spawn, "_build_cmd", return_value=fake_cmd)

    return {"cmd": fake_cmd, "plugin_dir": fake_plugin_dir}


def test_claude_preview_happy_path(
    mocker, capsys: pytest.CaptureFixture
) -> None:
    _patch_preview(mocker)

    claude_preview(trigger="hello", full=False)

    out = capsys.readouterr().out
    assert "System prompt" in out
    assert "Plugin dir" in out
    assert "MCP" in out
    assert "Permissions" in out
    assert "Trigger prompt" in out
    assert "Final argv" in out
    assert "trigger prompt body" in out
    assert "Read" in out and "Edit" in out
    assert "WebSearch" in out
    assert "physiclaw__peek" in out


def test_claude_preview_truncates_long_argv_without_full(
    mocker, capsys: pytest.CaptureFixture
) -> None:
    """An argv value over 80 chars (other than the prompt itself) is shown truncated."""
    stubs = _patch_preview(mocker)
    long_tail = "z" * 200
    # Append a long extra arg the truncate path will hit.
    stubs["cmd"].extend(["--something-long", long_tail])

    claude_preview(trigger="t", full=False)

    out = capsys.readouterr().out
    # In the "Final argv" block, this long arg is shown with size suffix.
    assert "200 chars total" in out
    # Truncated form (first 77 chars) appears, but full 200 doesn't.
    assert long_tail not in out


def test_claude_preview_full_mode_does_not_truncate_long_argv(
    mocker, capsys: pytest.CaptureFixture
) -> None:
    stubs = _patch_preview(mocker)
    long_tail = "y" * 150
    stubs["cmd"].extend(["--something-long", long_tail])

    claude_preview(trigger="t", full=True)

    out = capsys.readouterr().out
    # In --full, even long args go through verbatim.
    assert long_tail in out
    assert "150 chars total" not in out


def test_claude_preview_full_mode_dumps_complete_system_prompt(
    mocker, capsys: pytest.CaptureFixture
) -> None:
    stubs = _patch_preview(mocker)
    sixty_line_prompt = "\n".join(f"line-{i}" for i in range(60))
    # System prompt comes from --append-system-prompt in the built cmd.
    idx = stubs["cmd"].index("--append-system-prompt")
    stubs["cmd"][idx + 1] = sixty_line_prompt

    claude_preview(trigger="t", full=True)

    out = capsys.readouterr().out
    # Full prompt printed in --full mode.
    assert "line-0" in out and "line-59" in out


def test_claude_preview_non_full_truncates_prompt_to_30_lines(
    mocker, capsys: pytest.CaptureFixture
) -> None:
    stubs = _patch_preview(mocker)
    sixty_line_prompt = "\n".join(f"line-{i}" for i in range(60))
    idx = stubs["cmd"].index("--append-system-prompt")
    stubs["cmd"][idx + 1] = sixty_line_prompt

    claude_preview(trigger="t", full=False)

    out = capsys.readouterr().out
    # First 30 lines (line-0..line-29) shown.
    assert "line-29" in out
    # Lines 30..59 hidden behind a "more lines" notice.
    assert "line-30" not in out
    assert "30 more lines" in out


def test_claude_preview_exit_2_when_model_ref_invalid(mocker) -> None:
    _patch_preview(mocker, model_ref_raise=RuntimeError("not set"))

    with pytest.raises(typer.Exit) as exc:
        claude_preview(trigger="t", full=False)

    assert exc.value.exit_code == 2


def test_claude_preview_exit_2_when_parse_model_invalid(mocker) -> None:
    _patch_preview(mocker, parse_model_raise=ValueError("bad ref"))

    with pytest.raises(typer.Exit) as exc:
        claude_preview(trigger="t", full=False)

    assert exc.value.exit_code == 2


def test_claude_preview_exit_2_on_filenotfound_from_build_cmd(mocker) -> None:
    _patch_preview(
        mocker, build_cmd_side_effect=FileNotFoundError("missing CLAUDE.md"),
    )

    with pytest.raises(typer.Exit) as exc:
        claude_preview(trigger="t", full=False)

    assert exc.value.exit_code == 2
