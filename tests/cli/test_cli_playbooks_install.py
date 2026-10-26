"""Tests for `physiclaw playbooks install` — template packs copied into
the user home VERBATIM (tokens stay in the files), placeholder values
recorded once in `playbooks/placeholders.yml`, disabled state preserved."""

from __future__ import annotations

from pathlib import Path

import pytest
from symlink_helpers import symlink_to
from typer.testing import CliRunner

from physiclaw.cli import app
from physiclaw.common import paths
from physiclaw.common.placeholders import (
    PLACEHOLDER_VALUES_FILENAME,
    placeholder_values,
)

runner = CliRunner()


def _install(template: Path, *args: str, input: str | None = None):
    return runner.invoke(
        app, ["playbooks", "install", str(template), *args], input=input
    )


PAGES = (
    "thread:\n  description: the chat thread\n"
    '  anchors:\n    - {text: "<<CONTACT>>", within: top}\n'
)
MACRO = 'kind: macro\nschema: 1\nname: open\ndescription: open the <<CONTACT>> thread\nenabled: false\nsteps:\n  - send_to_clipboard: "<<CONTACT>>"\n'
MANIFEST = (
    "kind: manifest\nschema: 1\napp: wechat\ndescription: reach the user thread\n"
    "placeholders:\n  CONTACT:\n    description: the contact\n"
    "    example: SomeOne\n"
    "pages:\n" + "\n".join("  " + line for line in PAGES.splitlines()) + "\n"
)


@pytest.fixture()
def template(tmp_path: Path) -> Path:
    src = tmp_path / "template" / "channel" / "wechat"
    (src / "macros").mkdir(parents=True)
    (src / "macros" / "open.yml").write_text(MACRO, encoding="utf-8")
    (src / "README.md").write_text("# channel\nnotes for people\n", encoding="utf-8")
    (src / "APP.yml").write_text(MANIFEST, encoding="utf-8")
    return src


def test_install_copies_verbatim_and_records_the_value(template: Path) -> None:
    result = _install(template, "--set", "CONTACT=Alice")

    assert result.exit_code == 0, result.output
    assert "reach the user thread" in result.output  # tier-1 description echoed
    assert "installed channel →" in result.output  # the pack, not the IM folder
    dest = paths.playbooks_dir() / "channel" / "wechat"
    # Tokens stay in the installed files — diffable against the template.
    assert "<<CONTACT>>" in (dest / "APP.yml").read_text("utf-8")
    assert (
        (dest / "README.md").read_text("utf-8").startswith("# channel")
    )  # notes travel
    macro = (dest / "macros/open.yml").read_text("utf-8")
    assert "<<CONTACT>>" in macro and "Alice" not in macro
    assert "enabled: false" in macro  # disabled state preserved
    # The value went to the ONE local values file instead.
    assert placeholder_values() == {"CONTACT": "Alice"}
    assert PLACEHOLDER_VALUES_FILENAME in result.output


def test_installed_macro_resolves_from_the_values_file(template: Path) -> None:
    # The end-to-end contract: parsers fill <<TOKEN>>s at load, so the
    # verbatim copy parses to the VALUE.
    from physiclaw.macros import store

    _install(template, "--set", "CONTACT=Alice")

    # Pack-private location — scan the pack's macro root directly.
    entries = store.scan(paths.playbooks_dir() / "channel" / "wechat" / "macros")
    spec = entries[0].spec
    assert spec is not None and spec.steps[0].args == {"text": "Alice"}


def test_install_prompts_for_missing_placeholders(template: Path) -> None:
    result = _install(template, input="Bob\n")

    assert result.exit_code == 0, result.output
    assert "the contact" in result.output  # manifest prose reached the prompt
    assert "SomeOne" in result.output  # and the example
    assert placeholder_values() == {"CONTACT": "Bob"}


def test_install_skips_the_prompt_when_a_value_exists(template: Path) -> None:
    from physiclaw.common.placeholders import write_placeholder_values

    write_placeholder_values({"CONTACT": "Carol"})

    result = _install(template)  # no --set, no stdin — must not prompt

    assert result.exit_code == 0, result.output
    assert placeholder_values() == {"CONTACT": "Carol"}


def test_install_refuses_an_existing_pack_without_force(template: Path) -> None:
    _install(template, "--set", "CONTACT=A")

    result = _install(template, "--set", "CONTACT=B")

    assert result.exit_code != 0
    assert "--force" in result.output


def test_install_force_replaces_and_set_overrides_the_value(template: Path) -> None:
    _install(template, "--set", "CONTACT=A")

    result = _install(template, "--set", "CONTACT=B", "--force")

    assert result.exit_code == 0, result.output
    assert placeholder_values() == {"CONTACT": "B"}


def test_install_rejects_unknown_set_key(template: Path) -> None:
    result = _install(template, "--set", "NOPE=x")

    assert result.exit_code != 0
    assert "NOPE" in result.output


def test_install_rejects_a_placeholder_valued_value(template: Path) -> None:
    result = _install(template, "--set", "CONTACT=<<X>>")

    assert result.exit_code != 0


def _destination() -> Path:
    return paths.playbooks_dir() / "channel" / "wechat"


def _installed(template):
    result = _install(template, "--set", "CONTACT=Original")
    assert result.exit_code == 0, result.output
    marker = _destination() / "local-only.md"
    marker.write_text("keep local work", encoding="utf-8")
    return marker


@pytest.mark.parametrize(("cwd_suffix", "source"), [("", "."), ("nested", "..")])
def test_regression_relative_source_never_replaces_playbooks_root(
    template, monkeypatch, cwd_suffix, source
):
    unrelated = paths.playbooks_dir() / "other" / "keep.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("other pack", encoding="utf-8")
    cwd = template / cwd_suffix
    cwd.mkdir(exist_ok=True)
    monkeypatch.chdir(cwd)

    result = _install(Path(source), "--force", "--set", "CONTACT=Alice")

    assert result.exit_code == 0, result.output
    assert unrelated.read_text(encoding="utf-8") == "other pack"
    assert (_destination() / "APP.yml").is_file()


def test_regression_invalid_pack_cannot_replace_previous_install(template):
    marker = _installed(template)
    values_path = paths.playbooks_dir() / PLACEHOLDER_VALUES_FILENAME
    original_values = values_path.read_bytes()
    (template / "macros" / "open.yml").write_text("steps: [invalid", encoding="utf-8")

    result = _install(template, "--force", "--set", "CONTACT=Replacement")

    assert result.exit_code == 1
    assert "invalid YAML" in result.output
    assert marker.read_text(encoding="utf-8") == "keep local work"
    assert values_path.read_bytes() == original_values


def test_regression_copy_failure_preserves_pack_and_placeholder_values(
    template, mocker
):
    from physiclaw.common import text

    marker = _installed(template)
    values_path = paths.playbooks_dir() / PLACEHOLDER_VALUES_FILENAME
    original_values = values_path.read_bytes()
    write = text.write_text

    def fail_macro(path, body):
        if path.name == "open.yml":
            raise OSError("simulated copy failure")
        return write(path, body)

    mocker.patch.object(text, "write_text", fail_macro)

    result = _install(template, "--force", "--set", "CONTACT=Replacement")

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "simulated copy failure" in result.output
    assert marker.read_text(encoding="utf-8") == "keep local work"
    assert values_path.read_bytes() == original_values


def test_regression_invalid_inactive_channel_is_checked_instead_of_active_one(template):
    import shutil

    marker = _installed(template)
    (paths.playbooks_dir() / "channel" / "ACTIVE.txt").write_text(
        "wechat\n", encoding="utf-8"
    )
    other = template.with_name("whatsapp")
    shutil.copytree(template, other)
    (other / "APP.yml").write_text(
        MANIFEST.replace("app: wechat", "app: whatsapp"), encoding="utf-8"
    )
    (other / "macros" / "open.yml").write_text("steps: [broken", encoding="utf-8")

    result = _install(other)

    assert result.exit_code == 1
    assert "invalid YAML" in result.output
    assert not (paths.playbooks_dir() / "channel" / "whatsapp").exists()
    assert marker.exists()


def test_install_rejects_file_symlink_outside_template(template, tmp_path):
    outside = tmp_path / "private.md"
    outside.write_text("private contents", encoding="utf-8")
    symlink_to(template / "leaked.md", outside)

    result = _install(template, "--set", "CONTACT=Alice")

    assert result.exit_code == 1
    assert "outside" in result.output
    assert not _destination().exists()


def test_install_refuses_existing_destination_before_prompting(template):
    marker = _installed(template)
    (paths.playbooks_dir() / PLACEHOLDER_VALUES_FILENAME).unlink()

    result = _install(template)

    assert result.exit_code == 1
    assert "--force" in result.output
    assert "CONTACT —" not in result.output
    assert marker.exists()
