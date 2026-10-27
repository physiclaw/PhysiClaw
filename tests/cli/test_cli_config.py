"""Public config CLI behavior, persistence, redaction and editor failures.

Mutation review accepts display wording/whitespace and replacing the explicit
post-edit config path with its identical default. Argument handling, editor
selection, exit status, error channels and credential masking are asserted.
"""

from __future__ import annotations

import dataclasses
import importlib
import shlex
import subprocess
import tomllib
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from physiclaw.common import config as _config
from physiclaw.port.backends import posix, windows

config_mod = importlib.import_module("physiclaw.cli.config")
config_app = config_mod.config_app

runner = CliRunner()


@pytest.fixture
def posix_editor_commands(mocker):
    """Exercise the existing POSIX command contract on every test host."""
    mocker.patch.object(config_mod.desktop, "_native", posix)


# ---------- path ----------


def test_path_prints_config_file_path(mocker) -> None:
    mocker.patch.object(config_mod._config, "config_path", return_value="/tmp/cfg.toml")

    result = runner.invoke(config_app, ["path"])

    assert result.exit_code == 0
    assert "/tmp/cfg.toml" in result.stdout


# ---------- show ----------


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    secret=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=60)
)
def test_show_masks_all_provider_secrets_without_mutating_config(
    mocker, secret
) -> None:
    values = {
        field.name: f"fake-secret-{field.name}-{secret}"
        for field in dataclasses.fields(_config.ProviderConfig)
    }
    cfg = dataclasses.replace(
        _config.Config(), provider=_config.ProviderConfig(**values)
    )
    mocker.patch.object(config_mod._config, "load", return_value=cfg)

    result = runner.invoke(config_app, ["show"])

    assert result.exit_code == 0
    assert tomllib.loads(result.stdout)["provider"] == dict.fromkeys(
        values, "<redacted>"
    )
    assert "fake-secret-" not in result.stdout
    assert dataclasses.asdict(cfg.provider) == values


def test_show_preserves_empty_provider_credentials() -> None:
    result = runner.invoke(config_app, ["show"])

    assert result.exit_code == 0
    assert tomllib.loads(result.stdout)["provider"] == dataclasses.asdict(
        _config.ProviderConfig()
    )


def test_show_exits_1_on_config_error(mocker) -> None:
    mocker.patch.object(
        config_mod._config,
        "load",
        side_effect=_config.ConfigError("bad"),
    )

    result = runner.invoke(config_app, ["show"])

    assert result.exit_code == 1
    assert "bad" in result.stderr or "bad" in result.output


# ---------- get ----------


def test_get_prints_value(mocker) -> None:
    mocker.patch.object(config_mod._config, "load", return_value=_config.Config())
    mocker.patch.object(config_mod._config, "get", return_value=42)

    result = runner.invoke(config_app, ["get", "engine.max_turns"])

    assert result.exit_code == 0
    assert "42" in result.stdout


@pytest.mark.parametrize(("value", "expected"), [(True, "true"), (False, "false")])
def test_get_renders_bool_as_lowercase(mocker, value: bool, expected: str) -> None:
    mocker.patch.object(config_mod._config, "load", return_value=_config.Config())
    mocker.patch.object(config_mod._config, "get", return_value=value)

    result = runner.invoke(config_app, ["get", "some.flag"])

    assert result.exit_code == 0
    assert result.stdout.strip() == expected


def test_get_exit_1_on_load_error(mocker) -> None:
    mocker.patch.object(
        config_mod._config,
        "load",
        side_effect=_config.ConfigError("bad"),
    )

    result = runner.invoke(config_app, ["get", "engine.max_turns"])

    assert result.exit_code == 1


def test_get_exit_1_on_unknown_key(mocker) -> None:
    mocker.patch.object(config_mod._config, "load", return_value=_config.Config())
    mocker.patch.object(
        config_mod._config,
        "get",
        side_effect=_config.ConfigError("unknown key"),
    )

    result = runner.invoke(config_app, ["get", "no.such.thing"])

    assert result.exit_code == 1


# ---------- set ----------


def test_set_calls_set_dotted_and_prints_restart_hint(mocker) -> None:
    spy = mocker.patch.object(config_mod._config, "set_dotted")

    result = runner.invoke(config_app, ["set", "engine.max_turns", "10"])

    assert result.exit_code == 0
    spy.assert_called_once_with("engine.max_turns", "10")
    assert "engine.max_turns updated" in result.stdout
    assert "Restart" in result.stdout


def test_set_exit_1_on_invalid_value(mocker) -> None:
    mocker.patch.object(
        config_mod._config,
        "set_dotted",
        side_effect=_config.ConfigError("not an int"),
    )

    result = runner.invoke(config_app, ["set", "engine.max_turns", "abc"])

    assert result.exit_code == 1


# ---------- unset ----------


def test_unset_when_present(mocker) -> None:
    mocker.patch.object(config_mod._config, "unset_dotted", return_value=True)

    result = runner.invoke(config_app, ["unset", "engine.max_turns"])

    assert result.exit_code == 0
    assert "reverted to default" in result.stdout


def test_unset_when_already_default(mocker) -> None:
    mocker.patch.object(config_mod._config, "unset_dotted", return_value=False)

    result = runner.invoke(config_app, ["unset", "engine.max_turns"])

    assert result.exit_code == 0
    assert "already at default" in result.stdout


def test_unset_exit_1_on_error(mocker) -> None:
    mocker.patch.object(
        config_mod._config,
        "unset_dotted",
        side_effect=_config.ConfigError("bad key"),
    )

    result = runner.invoke(config_app, ["unset", "x.y"])

    assert result.exit_code == 1


# ---------- edit ----------


def test_edit_uses_editor_env(
    mocker, monkeypatch: pytest.MonkeyPatch, posix_editor_commands
) -> None:
    mocker.patch.object(
        config_mod._config, "write_default", return_value="/tmp/cfg.toml"
    )
    mocker.patch.object(config_mod._config, "load", return_value=_config.Config())
    monkeypatch.setenv("EDITOR", "myeditor")
    spy = mocker.patch.object(config_mod.subprocess, "run")

    result = runner.invoke(config_app, ["edit"])

    assert result.exit_code == 0
    spy.assert_called_once_with(["myeditor", "/tmp/cfg.toml"], check=True)
    assert "config OK" in result.stdout


def test_edit_falls_back_to_visual(
    mocker, monkeypatch: pytest.MonkeyPatch, posix_editor_commands
) -> None:
    mocker.patch.object(
        config_mod._config, "write_default", return_value="/tmp/cfg.toml"
    )
    mocker.patch.object(config_mod._config, "load", return_value=_config.Config())
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("VISUAL", "subl")
    spy = mocker.patch.object(config_mod.subprocess, "run")

    runner.invoke(config_app, ["edit"])

    assert spy.call_args.args[0] == ["subl", "/tmp/cfg.toml"]


@pytest.mark.parametrize(
    ("backend", "available", "expected"),
    [
        (posix, ("nano", "vim", "vi"), "vim"),
        (posix, ("nano", "vi"), "vi"),
        (posix, ("nano",), "nano"),
        (windows, ("notepad",), "notepad"),
    ],
)
def test_edit_falls_back_to_first_available_editor(
    mocker,
    monkeypatch: pytest.MonkeyPatch,
    backend,
    available: tuple[str, ...],
    expected: str,
) -> None:
    mocker.patch.object(config_mod.desktop, "_native", backend)
    mocker.patch.object(
        config_mod._config, "write_default", return_value="/tmp/cfg.toml"
    )
    mocker.patch.object(config_mod._config, "load", return_value=_config.Config())
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    mocker.patch.object(
        config_mod.desktop.shutil,
        "which",
        side_effect=lambda name: name if name in available else None,
    )
    spy = mocker.patch.object(config_mod.subprocess, "run")

    result = runner.invoke(config_app, ["edit"])

    assert result.exit_code == 0
    spy.assert_called_once_with([expected, "/tmp/cfg.toml"], check=True)


@pytest.mark.parametrize("backend", [posix, windows])
def test_edit_exit_1_when_no_editor_found(
    mocker, monkeypatch: pytest.MonkeyPatch, backend
) -> None:
    mocker.patch.object(config_mod.desktop, "_native", backend)
    mocker.patch.object(
        config_mod._config, "write_default", return_value="/tmp/cfg.toml"
    )
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    mocker.patch.object(config_mod.desktop.shutil, "which", return_value=None)

    result = runner.invoke(config_app, ["edit"])

    assert result.exit_code == 1
    assert "no $EDITOR" in result.stderr
    assert "/tmp/cfg.toml" in result.stderr


@pytest.mark.parametrize(
    "editor",
    [
        r"C:\Tools\editor.exe --wait",
        r'"C:\Program Files\My Editor\editor.exe" --wait',
        r'"\\server\shared tools\editor.exe" --label "hello world"',
        r'C:\Tools\editor.exe --label "a&b" --directory "C:\work dir\\"',
    ],
)
def test_edit_preserves_windows_command_and_quotes_config_path(
    mocker, monkeypatch, editor
):
    mocker.patch.object(config_mod.desktop, "_native", windows)
    path = Path(r"C:\Users\Example User\config.toml")
    mocker.patch.object(config_mod._config, "write_default", return_value=path)
    load = mocker.patch.object(config_mod._config, "load")
    monkeypatch.setenv("EDITOR", editor)
    monkeypatch.setenv("VISUAL", "ignored-editor")
    run = mocker.patch.object(config_mod.subprocess, "run")

    result = runner.invoke(config_app, ["edit"])

    assert result.exit_code == 0
    run.assert_called_once_with(f'{editor} "{path}"', check=True)
    load.assert_called_once_with(path)


def test_edit_validates_after_save(mocker, monkeypatch: pytest.MonkeyPatch) -> None:
    mocker.patch.object(
        config_mod._config, "write_default", return_value="/tmp/cfg.toml"
    )
    monkeypatch.setenv("EDITOR", "ed")
    mocker.patch.object(config_mod.subprocess, "run")
    mocker.patch.object(
        config_mod._config,
        "load",
        side_effect=_config.ConfigError("post-edit invalid"),
    )

    result = runner.invoke(config_app, ["edit"])

    assert result.exit_code == 1


@pytest.mark.parametrize("exit_code", [1, 7, -15])
def test_regression_edit_failed_editor_does_not_claim_success(
    mocker, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    monkeypatch.setenv("EDITOR", "test-editor")
    mocker.patch.object(
        config_mod.subprocess,
        "run",
        side_effect=subprocess.CalledProcessError(exit_code, ["test-editor"]),
    )

    result = runner.invoke(config_app, ["edit"])

    assert result.exit_code == 1
    assert "ERROR [" in result.stderr
    assert "config OK" not in result.stdout
    assert "Restart" not in result.stdout


def test_regression_edit_checks_editor_exit_status(
    mocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EDITOR", "test-editor")
    run = mocker.patch.object(config_mod.subprocess, "run")

    result = runner.invoke(config_app, ["edit"])

    assert result.exit_code == 0
    assert run.call_args.kwargs["check"] is True


@pytest.mark.parametrize(
    ("editor", "arguments"),
    [
        ("code --wait", ["code", "--wait"]),
        (
            '"/Applications/My Editor/bin/edit" --wait',
            ["/Applications/My Editor/bin/edit", "--wait"],
        ),
        ("editor --label 'hello world'", ["editor", "--label", "hello world"]),
    ],
)
def test_regression_edit_accepts_editor_arguments(
    mocker,
    monkeypatch: pytest.MonkeyPatch,
    editor: str,
    arguments: list[str],
    posix_editor_commands,
) -> None:
    monkeypatch.setenv("EDITOR", editor)
    run = mocker.patch.object(config_mod.subprocess, "run")

    result = runner.invoke(config_app, ["edit"])

    assert result.exit_code == 0
    assert run.call_args.args[0] == [*arguments, str(_config.config_path())]


@pytest.mark.parametrize("editor", ["   ", "'unterminated"])
def test_regression_edit_invalid_editor_reports_error(
    mocker, monkeypatch: pytest.MonkeyPatch, editor: str, posix_editor_commands
) -> None:
    monkeypatch.setenv("EDITOR", editor)
    run = mocker.patch.object(config_mod.subprocess, "run")

    result = runner.invoke(config_app, ["edit"])

    assert result.exit_code == 1
    assert "ERROR [" in result.stderr
    assert "config OK" not in result.stdout
    run.assert_not_called()


@pytest.mark.parametrize(
    "error", [FileNotFoundError("editor missing"), PermissionError("editor denied")]
)
def test_regression_edit_launch_error_is_reported(
    mocker, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    monkeypatch.setenv("EDITOR", "test-editor")
    mocker.patch.object(config_mod.subprocess, "run", side_effect=error)

    result = runner.invoke(config_app, ["edit"])

    assert result.exit_code == 1
    assert str(error) in result.stderr
    assert "config OK" not in result.stdout


@pytest.mark.parametrize(
    ("command", "boundary"),
    [
        (["show"], "load"),
        (["get", "engine.max_turns"], "load"),
        (["set", "engine.max_turns", "10"], "set_dotted"),
        (["unset", "engine.max_turns"], "unset_dotted"),
        (["edit"], "write_default"),
    ],
)
def test_regression_config_io_failure_reports_error(
    mocker, command: list[str], boundary: str
) -> None:
    mocker.patch.object(_config, boundary, side_effect=PermissionError("denied"))

    result = runner.invoke(config_app, command)

    assert result.exit_code == 1
    assert "denied" in result.stderr
    assert "ERROR [" in result.stderr
    assert "Restart" not in result.stdout


@pytest.mark.parametrize("backend", [posix, windows])
@pytest.mark.parametrize("editor_name", ["My Editor.exe", "editor"])
def test_edit_preserves_existing_executable_path(
    tmp_path: Path, mocker, monkeypatch: pytest.MonkeyPatch, editor_name: str, backend
) -> None:
    mocker.patch.object(config_mod.desktop, "_native", backend)
    editor = tmp_path / editor_name
    editor.touch()
    monkeypatch.setenv("EDITOR", str(editor))
    run = mocker.patch.object(config_mod.subprocess, "run")

    result = runner.invoke(config_app, ["edit"])

    assert result.exit_code == 0
    run.assert_called_once_with([str(editor), str(_config.config_path())], check=True)


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    arguments=st.lists(st.text(alphabet="abc xyz012-_$;\\\"'", max_size=15), max_size=4)
)
def test_edit_preserves_quoted_arguments_without_a_shell(
    mocker, monkeypatch, posix_editor_commands, arguments
):
    command = ["fixture-editor", *arguments]
    monkeypatch.setenv("EDITOR", shlex.join(command))
    run = mocker.patch.object(config_mod.subprocess, "run")

    result = runner.invoke(config_app, ["edit"])

    assert result.exit_code == 0
    run.assert_called_once_with([*command, str(_config.config_path())], check=True)


@settings(
    deadline=None,
    max_examples=25,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(value=st.integers(min_value=1, max_value=1000))
def test_set_persists_integer_and_preserves_unrelated_comments(monkeypatch, value):
    monkeypatch.setattr(_config, "CONFIG", _config.CONFIG)
    path = _config.config_path()
    path.write_text(
        "# keep this comment\n[engine]\nmax_turns = 20\n[agent]\nmodel = 'openai/test-model'\n",
        encoding="utf-8",
    )

    result = runner.invoke(config_app, ["set", "engine.max_turns", str(value)])

    assert result.exit_code == 0
    assert _config.load(path).engine.max_turns == value
    assert _config.load(path).agent.model == "openai/test-model"
    assert "# keep this comment" in path.read_text(encoding="utf-8")
    assert "Restart" in result.stdout


def test_unset_persists_default_and_preserves_unrelated_values(monkeypatch):
    monkeypatch.setattr(_config, "CONFIG", _config.CONFIG)
    path = _config.config_path()
    path.write_text(
        "# keep\n[engine]\nmax_turns = 123\n[agent]\nmodel = 'openai/test-model'\n",
        encoding="utf-8",
    )

    result = runner.invoke(config_app, ["unset", "engine.max_turns"])

    assert result.exit_code == 0
    assert _config.load(path).engine.max_turns == _config.Config().engine.max_turns
    assert _config.load(path).agent.model == "openai/test-model"
    assert "max_turns" not in path.read_text(encoding="utf-8")
    assert "# keep" in path.read_text(encoding="utf-8")


def test_get_reads_effective_config_value():
    _config.config_path().write_text("[engine]\nmax_turns = 123\n", encoding="utf-8")

    result = runner.invoke(config_app, ["get", "engine.max_turns"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "123"


def test_edit_rejects_invalid_saved_config(mocker, monkeypatch):
    monkeypatch.setenv("EDITOR", "test-editor")

    def save_invalid_config(*args, **kwargs):
        _config.config_path().write_text("invalid = [", encoding="utf-8")

    mocker.patch.object(config_mod.subprocess, "run", side_effect=save_invalid_config)

    result = runner.invoke(config_app, ["edit"])

    assert result.exit_code == 1
    assert "failed to read" in result.stderr
    assert "config OK" not in result.stdout


@pytest.mark.parametrize(("args", "code"), [([], 2), (["-h"], 0), (["--help"], 0)])
def test_config_help_lists_commands_without_completion_options(args, code):
    result = runner.invoke(config_app, args)

    assert result.exit_code == code
    assert "Commands" in result.output
    assert "edit" in result.output
    assert "install-completion" not in result.output
    assert "show-completion" not in result.output
