"""Public CLI tests for resetting calibration and learned screen layout.

Mutation review accepts prose/decoration variants; confirmation decisions,
target selection, failures and preservation of unrelated data are asserted.
"""

import importlib

import pytest
import typer
from typer.testing import CliRunner

from physiclaw.common import paths

reset_mod = importlib.import_module("physiclaw.cli.reset")
app = typer.Typer()
app.command()(reset_mod.reset)
runner = CliRunner()


def _seed(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "state.json"
    path.write_text("{}", encoding="utf-8")
    return path


def test_reset_without_state_reports_nothing_to_reset():
    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "Nothing to reset" in result.output


def test_reset_empty_directories_remain_unchanged():
    paths.calibration_dir().mkdir(parents=True)
    paths.screen_layout_dir().mkdir(parents=True)

    result = runner.invoke(app, ["--yes"])

    assert result.exit_code == 0
    assert "Nothing to reset" in result.output
    assert paths.calibration_dir().is_dir()
    assert paths.screen_layout_dir().is_dir()


@pytest.mark.parametrize("flag", ["--yes", "-y"])
def test_reset_removes_state_and_preserves_unrelated_data(flag):
    _seed(paths.calibration_dir())
    _seed(paths.screen_layout_dir())
    model = _seed(paths.model_cache())
    config = paths.HOME / "config.toml"
    config.write_text("# keep", encoding="utf-8")

    result = runner.invoke(app, [flag])

    assert result.exit_code == 0
    assert "Cleared" in result.output
    assert not paths.calibration_dir().exists()
    assert not paths.screen_layout_dir().exists()
    assert model.read_text(encoding="utf-8") == "{}"
    assert config.read_text(encoding="utf-8") == "# keep"


@pytest.mark.parametrize("answer", ["n\n", "\n"])
def test_reset_declined_confirmation_preserves_state(answer):
    calibration = _seed(paths.calibration_dir())
    layout = _seed(paths.screen_layout_dir())

    result = runner.invoke(app, [], input=answer)

    assert result.exit_code == 1
    assert "Cancelled" in result.output
    assert calibration.exists() and layout.exists()


def test_reset_accepts_confirmation_and_lists_affected_directories():
    _seed(paths.calibration_dir())
    _seed(paths.screen_layout_dir())

    result = runner.invoke(app, [], input="y\n")

    assert result.exit_code == 0
    assert f"{paths.calibration_dir()}  (1 file(s))" in result.output
    assert f"{paths.screen_layout_dir()}  (1 file(s))" in result.output
    assert not paths.calibration_dir().exists()
    assert not paths.screen_layout_dir().exists()


@pytest.mark.parametrize("kind", ["calibration", "screen-layout"])
def test_reset_removes_single_populated_target(kind):
    directory = paths.HOME / kind
    _seed(directory)

    result = runner.invoke(app, ["--yes"])

    assert result.exit_code == 0
    assert not directory.exists()


def test_regression_reset_failed_removal_is_not_reported_as_success(mocker):
    state = _seed(paths.calibration_dir())
    mocker.patch.object(
        reset_mod.shutil, "rmtree", side_effect=PermissionError("denied")
    )

    result = runner.invoke(app, ["--yes"])

    assert result.exit_code == 1
    assert "could not remove" in result.output
    assert str(state.parent) in result.output and "denied" in result.output
    assert "Cleared" not in result.output
    assert state.exists()


def test_regression_reset_root_symlink_reports_refusal_without_touching_target(
    tmp_path,
):
    outside = tmp_path / "outside"
    state = _seed(outside)
    directory = paths.calibration_dir()
    directory.symlink_to(outside, target_is_directory=True)

    result = runner.invoke(app, ["--yes"])

    assert result.exit_code == 1
    assert "could not remove" in result.output
    assert "Cleared" not in result.output
    assert directory.is_symlink()
    assert state.read_text(encoding="utf-8") == "{}"
