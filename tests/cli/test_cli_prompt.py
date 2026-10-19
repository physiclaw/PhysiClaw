"""Tests for `physiclaw.cli.prompt` — the SYSTEM prompt / request dump.

The headline test (`test_cli_request_matches_engine_turn0`) runs the REAL
engine for one turn with only MCP + provider mocked, captures the exact
message array the engine hands the provider, and asserts the CLI's
`prompt system` / `prompt request` output reproduces it byte-for-byte — so the
dump can never drift from what the running agent actually sends.

Mutation review accepts six display/temporary-name variants, one annotation-only
variant, and missing_ok=False under suppress(OSError), which is equivalent.
Content, normalization, error exits and preservation of existing dumps are tested.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from physiclaw.agent.engine import assemble as assemble_mod
from physiclaw.agent.engine import engine as engine_mod
from physiclaw.agent.engine.session import Session
from physiclaw.agent.runtime.hook import Trigger
from physiclaw.agent.runtime.sentinel import DONE
from physiclaw.cli.prompt import prompt_app
from physiclaw.contract.dto import (
    AssistantMessage,
    CollapsePolicy,
    FinishReason,
    ImageBlock,
    TextBlock,
    ToolCall,
    Usage,
    UserMessage,
)

runner = CliRunner()


# ---------- smoke ----------


def test_prompt_system_prints_the_system_prompt() -> None:
    result = runner.invoke(prompt_app, ["system"])

    assert result.exit_code == 0
    assert "# Doctrine" in result.stdout
    assert "## Built-in Skills" in result.stdout


def test_prompt_request_prints_system_then_user_messages() -> None:
    result = runner.invoke(prompt_app, ["request"])

    assert result.exit_code == 0
    assert "===== [system] =====" in result.stdout
    assert "===== [user] =====" in result.stdout
    # a fresh HOME → layout unlearned → the first-run reminder rides the tail
    assert "First-run setup needed" in result.stdout


def test_prompt_system_save_as_writes_file(tmp_path) -> None:
    dest = tmp_path / "sys.md"
    result = runner.invoke(prompt_app, ["system", "--save-as", str(dest)])

    assert result.exit_code == 0
    assert "Wrote " in result.stdout and str(dest) in result.stdout
    assert "# Doctrine" not in result.stdout  # content went to the file, not stdout
    assert dest.read_text(encoding="utf-8").startswith("# Doctrine")


def test_prompt_request_save_as_writes_file(tmp_path) -> None:
    dest = tmp_path / "req.txt"
    result = runner.invoke(prompt_app, ["request", "--save-as", str(dest)])

    assert result.exit_code == 0
    assert str(dest) in result.stdout
    body = dest.read_text(encoding="utf-8")
    assert "===== [system] =====" in body and "===== [user] =====" in body


# ---------- equivalence with the real engine turn-0 ----------


class _FrozenDatetime:
    """Fixed clock so the trigger's `Now:` stamp is identical in the engine
    run and the CLI build (both go through `assemble.dt.datetime.now`)."""

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003
        return _dt.datetime(2026, 7, 2, 10, 0, 0)


class _FakeDtModule:
    datetime = _FrozenDatetime


class _CaptureProvider:
    """Records the turn-0 request, then ends the session on the first turn."""

    PROVIDER_ID = "fake"
    # Collapse policy read by `loop.drive` each turn (provider contract).
    COLLAPSE = CollapsePolicy(first_at=100, keep=10, interval=100)

    def __init__(self) -> None:
        self.model = "fake-model"
        self.captured: list | None = None

    def serialize_history(self, messages):
        return [{"role": "fake"} for _ in messages]

    async def chat(self, messages, tools):
        if self.captured is None:
            self.captured = list(messages)
        return AssistantMessage(
            content="",
            tool_calls=[
                ToolCall(id="t1", name="note", arguments={"summary": "done"}),
                ToolCall(
                    id="t2",
                    name="end_session",
                    arguments={"status": DONE, "recap": "fin"},
                ),
            ],
            finish_reason=FinishReason.TOOL_CALLS,
            usage=Usage(),
        )

    async def aclose(self) -> None:
        pass


def _aret(value):
    async def _coro(*_a, **_k):
        return value

    return _coro


def _role(msg) -> str:
    return type(msg).__name__.removesuffix("Message").lower()


def _content(msg) -> str:
    content = msg.content
    if isinstance(content, str):
        return content
    return "\n".join(
        b.text if isinstance(b, TextBlock) else f"[{type(b).__name__}]" for b in content
    )


async def test_cli_request_matches_engine_turn0(monkeypatch) -> None:
    capture = _CaptureProvider()
    # Real assembly (skills, prompt render, placeholders, tails); only the
    # outside world is stubbed.
    monkeypatch.setattr(assemble_mod, "dt", _FakeDtModule)
    monkeypatch.setattr(engine_mod, "get_mcp", _aret(MagicMock()))
    monkeypatch.setattr(engine_mod, "list_tools_cached", _aret([]))
    monkeypatch.setattr(engine_mod, "make_chat_provider", lambda *_a, **_k: capture)
    monkeypatch.setattr(engine_mod, "Trace", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(engine_mod, "RawLog", lambda *_a, **_k: MagicMock())
    # CLI resolves the provider id from config; align it with the engine's.
    monkeypatch.setattr("physiclaw.common.config.model_ref", lambda: "qwen/fake-model")

    trigger = Trigger(description="phone screen changed", source="phone")
    await engine_mod._run_session(
        [trigger],
        model_ref="qwen/fake-model",
        session=Session(),
    )

    engine_req = capture.captured
    assert engine_req is not None, "engine never called provider.chat"
    assert _role(engine_req[0]) == "system"

    # `prompt system` == the engine's turn-0 SYSTEM message, exactly.
    sys_out = runner.invoke(prompt_app, ["system"])
    assert sys_out.exit_code == 0
    assert sys_out.stdout.rstrip("\n") == _content(engine_req[0])

    # `prompt request` reproduces the whole turn-0 message array, in order.
    req_out = runner.invoke(prompt_app, ["request"])
    assert req_out.exit_code == 0
    expected = "".join(
        f"\n===== [{_role(m)}] =====\n{_content(m)}\n" for m in engine_req
    )
    assert req_out.stdout == expected


@pytest.mark.parametrize("command", ["system", "request"])
def test_regression_prompt_save_directory_reports_error(tmp_path, command):
    result = runner.invoke(prompt_app, [command, "--save-as", str(tmp_path)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "could not save prompt" in result.stderr
    assert "Wrote" not in result.stdout
    assert tmp_path.is_dir()


@pytest.mark.parametrize("command", ["system", "request"])
def test_regression_prompt_save_missing_parent_reports_error(tmp_path, command):
    dest = tmp_path / "absent" / "prompt.txt"

    result = runner.invoke(prompt_app, [command, "--save-as", str(dest)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "could not save prompt" in result.stderr
    assert "Wrote" not in result.stdout
    assert not dest.exists()


@pytest.mark.parametrize("command", ["system", "request"])
def test_regression_prompt_partial_write_preserves_previous_dump(
    tmp_path, mocker, command
):
    dest = tmp_path / "prompt.txt"
    dest.write_text("previous complete dump", encoding="utf-8")
    before = set(tmp_path.iterdir())
    write_bytes = Path.write_bytes

    def disk_full(path, data):
        write_bytes(path, data[:8])
        raise OSError("disk full")

    mocker.patch.object(Path, "write_bytes", disk_full)

    result = runner.invoke(prompt_app, [command, "--save-as", str(dest)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert dest.read_text(encoding="utf-8") == "previous complete dump"
    assert "disk full" in result.stderr
    assert "Wrote" not in result.stdout
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("command", ["system", "request"])
def test_regression_prompt_replace_failure_preserves_previous_dump(
    tmp_path, mocker, command
):
    dest = tmp_path / "prompt.txt"
    dest.write_text("previous complete dump", encoding="utf-8")
    before = set(tmp_path.iterdir())
    mocker.patch.object(Path, "replace", side_effect=PermissionError("replace denied"))

    result = runner.invoke(prompt_app, [command, "--save-as", str(dest)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert dest.read_text(encoding="utf-8") == "previous complete dump"
    assert "replace denied" in result.stderr
    assert "Wrote" not in result.stdout
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("text", ["text", "text\n\n", "", "你好🙂\n\n"])
def test_regression_prompt_reports_written_character_count(tmp_path, mocker, text):
    dest = tmp_path / "prompt.txt"
    mocker.patch.object(
        assemble_mod,
        "build_prompt_bundle",
        return_value=SimpleNamespace(system_prompt=text),
    )

    result = runner.invoke(prompt_app, ["system", "--save-as", str(dest)])

    assert result.exit_code == 0
    assert dest.read_text(encoding="utf-8") == text.rstrip("\n") + "\n"
    assert f"Wrote {len(dest.read_text(encoding='utf-8'))} chars" in result.stdout


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ([], ""),
        ([TextBlock("")], ""),
        ([TextBlock("first"), TextBlock("")], "first\n"),
        ([ImageBlock("image/png", "opaque-image-payload")], "[ImageBlock]"),
        (
            [
                TextBlock("first"),
                ImageBlock("image/png", "opaque-image-payload"),
                TextBlock("last"),
            ],
            "first\n[ImageBlock]\nlast",
        ),
    ],
)
def test_request_renders_multipart_content_in_order(mocker, content, expected):
    mocker.patch.object(
        assemble_mod, "build_initial_messages", return_value=[UserMessage(content)]
    )

    result = runner.invoke(prompt_app, ["request"])

    assert result.exit_code == 0
    assert result.stdout.startswith(f"\n===== [user] =====\n{expected}\n")
    assert "opaque-image-payload" not in result.stdout


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(text=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=120))
@example(text="line\r\n")
@example(text="textX\n\n")
def test_system_preserves_stdout_and_normalizes_saved_unicode(tmp_path, mocker, text):
    dest = tmp_path / "unicode.txt"
    mocker.patch.object(
        assemble_mod,
        "build_prompt_bundle",
        return_value=SimpleNamespace(system_prompt=text),
    )

    printed = runner.invoke(prompt_app, ["system"])
    saved = runner.invoke(prompt_app, ["system", "--save-as", str(dest)])

    assert printed.exit_code == saved.exit_code == 0
    assert printed.stdout_bytes == text.encode("utf-8")
    assert dest.read_bytes() == (text.rstrip("\n") + "\n").encode("utf-8")
    assert f"Wrote {len(text.rstrip(chr(10))) + 1} chars" in saved.stdout


@pytest.mark.parametrize("existing", [False, True])
def test_prompt_save_follows_destination_symlink(tmp_path, mocker, existing):
    target = _seed_symlink_target(tmp_path / "target.txt", existing)
    link = tmp_path / "link.txt"
    link.symlink_to(target.name)
    expected_files = set(tmp_path.iterdir()) | {target}
    mocker.patch.object(
        assemble_mod,
        "build_prompt_bundle",
        return_value=SimpleNamespace(system_prompt="new dump"),
    )

    result = runner.invoke(prompt_app, ["system", "--save-as", str(link)])

    assert result.exit_code == 0
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "new dump\n"
    assert set(tmp_path.iterdir()) == expected_files


def _seed_symlink_target(path: Path, existing: bool) -> Path:
    if existing:
        path.write_text("previous dump", encoding="utf-8")
    return path


def test_prompt_save_symlink_loop_reports_error(tmp_path):
    link = tmp_path / "loop"
    link.symlink_to(link.name)

    result = runner.invoke(prompt_app, ["system", "--save-as", str(link)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "could not save prompt" in result.stderr
    assert link.is_symlink()
    assert "Wrote" not in result.stdout


def test_prompt_save_invalid_unicode_preserves_previous_dump(tmp_path, mocker):
    dest = tmp_path / "prompt.txt"
    dest.write_text("previous", encoding="utf-8")
    before = set(tmp_path.iterdir())
    mocker.patch.object(
        assemble_mod,
        "build_prompt_bundle",
        return_value=SimpleNamespace(system_prompt="bad\ud800"),
    )

    result = runner.invoke(prompt_app, ["system", "--save-as", str(dest)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "could not save prompt" in result.stderr
    assert dest.read_text(encoding="utf-8") == "previous"
    assert set(tmp_path.iterdir()) == before


def test_prompt_cleanup_error_does_not_hide_write_error(tmp_path, mocker):
    dest = tmp_path / "prompt.txt"
    dest.write_text("previous", encoding="utf-8")
    mocker.patch.object(Path, "write_bytes", side_effect=OSError("disk full"))
    mocker.patch.object(Path, "unlink", side_effect=PermissionError("cleanup denied"))

    result = runner.invoke(prompt_app, ["system", "--save-as", str(dest)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "disk full" in result.stderr
    assert "cleanup denied" not in result.stderr
    assert dest.read_text(encoding="utf-8") == "previous"


@pytest.mark.parametrize(
    ("model", "provider"),
    [("openai/example", "openai"), ("qwen/nested/model", "qwen")],
)
def test_prompt_uses_resolved_provider_id(mocker, model, provider):
    mocker.patch("physiclaw.common.config.model_ref", return_value=model)
    build = mocker.patch.object(
        assemble_mod,
        "build_prompt_bundle",
        return_value=SimpleNamespace(system_prompt="offline prompt"),
    )

    result = runner.invoke(prompt_app, ["system"])

    assert result.exit_code == 0
    build.assert_called_once_with(provider)
    assert result.stdout == "offline prompt"


def test_prompt_interrupted_write_preserves_previous_dump(tmp_path, mocker):
    dest = tmp_path / "prompt.txt"
    dest.write_text("previous complete dump", encoding="utf-8")
    before = set(tmp_path.iterdir())
    write_bytes = Path.write_bytes

    def interrupted(path, data):
        write_bytes(path, data[:8])
        raise KeyboardInterrupt

    mocker.patch.object(Path, "write_bytes", interrupted)

    result = runner.invoke(prompt_app, ["system", "--save-as", str(dest)])

    assert result.exit_code == 130
    assert dest.read_text(encoding="utf-8") == "previous complete dump"
    assert set(tmp_path.iterdir()) == before
    assert "Wrote" not in result.stdout


def test_prompt_supports_long_valid_destination_filename(tmp_path):
    dest = tmp_path / ("p" * 240 + ".txt")

    result = runner.invoke(prompt_app, ["system", "--save-as", str(dest)])

    assert result.exit_code == 0
    assert dest.read_text(encoding="utf-8").startswith("# Doctrine")


@pytest.mark.parametrize(("args", "code"), [([], 2), (["--help"], 0)])
def test_prompt_help_lists_available_commands(args, code):
    result = runner.invoke(prompt_app, args)

    assert result.exit_code == code
    assert "Commands" in result.output
    assert "system" in result.output
    assert "request" in result.output


def test_prompt_save_writes_the_allocated_temporary_file(tmp_path, mocker):
    dest = tmp_path / "prompt.txt"
    write_bytes = Path.write_bytes
    existed_before_write = []

    def write_existing(path, data):
        existed_before_write.append(path.exists())
        return write_bytes(path, data)

    mocker.patch.object(Path, "write_bytes", write_existing)

    result = runner.invoke(prompt_app, ["system", "--save-as", str(dest)])

    assert result.exit_code == 0
    assert existed_before_write == [True]
    assert dest.read_text(encoding="utf-8").startswith("# Doctrine")


@pytest.mark.parametrize("command", ["system", "request"])
@pytest.mark.parametrize("model", ["claude-code/model", "unknown/model", "invalid"])
def test_prompt_invalid_model_preserves_previous_dump(mocker, tmp_path, command, model):
    mocker.patch("physiclaw.common.config.model_ref", return_value=model)
    destination = tmp_path / "prompt.txt"
    destination.write_text("previous dump", encoding="utf-8")

    result = runner.invoke(prompt_app, [command, "--save-as", str(destination)])

    assert result.exit_code == 1
    assert model in result.stderr
    assert destination.read_text(encoding="utf-8") == "previous dump"
    assert result.stdout == ""
