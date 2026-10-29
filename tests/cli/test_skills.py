"""Tests for `physiclaw.cli.skills` — install/list/uninstall CLI.

Mutation review accepts help/example text, completion toggles, diagnostic
wording, revision abbreviation lengths, JSON indentation, and private names
inside hidden staging. Redundant URL-normalization guards preserve Git's
input; strict=False still rejects missing copy sources as unsupported files.
Initializing rollback flags to None instead of False is truth-value equivalent.
Behavioral survivors cover Git transport results, revision information,
prompt cycle rejection, stderr recovery guidance, hidden staging, and sync flags.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from symlink_helpers import symlink_to
from typer.testing import CliRunner

skills_mod = importlib.import_module("physiclaw.cli.skills")
skills_app = skills_mod.skills_app

runner = CliRunner()


@pytest.fixture
def fake_home(tmp_path: Path, mocker) -> Path:
    """Redirect skill install root to tmp_path."""
    skills_root = tmp_path / "skills"
    mocker.patch.object(skills_mod.paths, "skills_dir", return_value=skills_root)
    mocker.patch.object(skills_mod.paths, "HOME", tmp_path / "home")
    return skills_root


def _git_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------- _normalize_source ----------


@pytest.mark.parametrize(
    "inp,expected",
    [
        ("https://github.com/x/y.git", "https://github.com/x/y.git"),
        ("ssh://git@host/x.git", "ssh://git@host/x.git"),
        ("git@github.com:x/y.git", "git@github.com:x/y.git"),
    ],
)
def test_normalize_source_passes_url_forms_through(inp: str, expected: str) -> None:
    assert skills_mod._normalize_source(inp) == expected


def test_normalize_source_expands_user_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    out = skills_mod._normalize_source("~/repos")

    assert out == str(tmp_path / "repos")


def test_normalize_source_passes_absolute_path() -> None:
    assert skills_mod._normalize_source("/tmp/x") == str(Path("/tmp/x"))


def test_normalize_source_handles_relative_paths() -> None:
    # `./repo` is recognized as a path and Path() collapses leading `./`.
    assert skills_mod._normalize_source("./repo") == "repo"


def test_normalize_source_expands_owner_repo_shorthand() -> None:
    assert skills_mod._normalize_source("foo/bar") == "https://github.com/foo/bar.git"


def test_normalize_source_owner_repo_keeps_existing_dot_git() -> None:
    assert (
        skills_mod._normalize_source("foo/bar.git") == "https://github.com/foo/bar.git"
    )


def test_normalize_source_empty_returns_empty() -> None:
    assert skills_mod._normalize_source("   ") == ""


def test_normalize_source_unrecognized_passes_through() -> None:
    # Not URL, not path, not owner/repo — pass through unchanged.
    assert skills_mod._normalize_source("weird thing") == "weird thing"


# ---------- _validate_name ----------


def test_validate_name_accepts_safe_names() -> None:
    skills_mod._validate_name("jd")
    skills_mod._validate_name("foo-bar")
    skills_mod._validate_name("foo_bar")
    skills_mod._validate_name("foo.v2")


@pytest.mark.parametrize("bad", ["../escape", "/abs", ".dotfile", "has space", ""])
def test_validate_name_rejects_unsafe(bad: str) -> None:
    import typer

    with pytest.raises(typer.Exit):
        skills_mod._validate_name(bad)


# ---------- read_provenance ----------


def test_read_provenance_returns_none_when_missing(tmp_path: Path) -> None:
    assert skills_mod.read_provenance(tmp_path) is None


def test_read_provenance_returns_dict_when_valid(tmp_path: Path) -> None:
    (tmp_path / skills_mod.PROVENANCE_FILE).write_text(
        json.dumps({"source": "x", "sha": "abc"}),
        encoding="utf-8",
    )

    out = skills_mod.read_provenance(tmp_path)

    assert out == {"source": "x", "sha": "abc"}


def test_read_provenance_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / skills_mod.PROVENANCE_FILE).write_text("not json", encoding="utf-8")

    assert skills_mod.read_provenance(tmp_path) is None


# ---------- installed_skill_dirs ----------


def test_installed_skill_dirs_empty_when_root_missing(
    tmp_path: Path,
    mocker,
) -> None:
    mocker.patch.object(
        skills_mod.paths,
        "skills_dir",
        return_value=tmp_path / "missing",
    )

    assert skills_mod.installed_skill_dirs() == []


def test_installed_skill_dirs_lists_dirs_skipping_dotfiles(fake_home: Path) -> None:
    fake_home.mkdir(parents=True)
    (fake_home / "alpha").mkdir()
    (fake_home / "beta").mkdir()
    (fake_home / ".hidden").mkdir()
    (fake_home / "not-a-dir.txt").write_text("x", encoding="utf-8")

    out = skills_mod.installed_skill_dirs()

    assert [d.name for d in out] == ["alpha", "beta"]


# ---------- _resolve_source ----------


def test_resolve_source_from_flag_wins(mocker) -> None:
    out = skills_mod._resolve_source("foo/bar")

    assert out == "https://github.com/foo/bar.git"


def test_resolve_source_falls_back_to_config(mocker) -> None:
    cfg = MagicMock()
    cfg.skills.default_source = "alpha/beta"
    mocker.patch.object(skills_mod, "_load_config", return_value=cfg)

    out = skills_mod._resolve_source(None)

    assert out == "https://github.com/alpha/beta.git"


def test_resolve_source_exits_1_when_unconfigured(mocker) -> None:
    cfg = MagicMock()
    cfg.skills.default_source = ""
    mocker.patch.object(skills_mod, "_load_config", return_value=cfg)
    import typer

    with pytest.raises(typer.Exit):
        skills_mod._resolve_source(None)


def test_resolve_source_rejects_path_inside_physiclaw_home(
    tmp_path: Path,
    mocker,
) -> None:
    mocker.patch.object(skills_mod.paths, "HOME", tmp_path)
    inside = tmp_path / "skills-source"
    inside.mkdir()
    import typer

    with pytest.raises(typer.Exit):
        skills_mod._resolve_source(str(inside))


def test_resolve_source_url_skips_home_check(mocker) -> None:
    # URLs bypass the home-check.
    out = skills_mod._resolve_source("https://github.com/x/y.git")

    assert out == "https://github.com/x/y.git"


# ---------- _git ----------


def test_git_runs_command(mocker) -> None:
    spy = mocker.patch.object(
        skills_mod.subprocess,
        "run",
        return_value=_git_completed(stdout="ok"),
    )

    out = skills_mod._git("rev-parse", "HEAD")

    assert out.stdout == "ok"
    assert spy.call_args.args[0] == ["git", "rev-parse", "HEAD"]


def test_git_exits_1_when_not_found(mocker) -> None:
    mocker.patch.object(
        skills_mod.subprocess,
        "run",
        side_effect=FileNotFoundError,
    )
    import typer

    with pytest.raises(typer.Exit):
        skills_mod._git("status")


def test_git_exits_1_on_nonzero(mocker) -> None:
    mocker.patch.object(
        skills_mod.subprocess,
        "run",
        return_value=_git_completed(stderr="bad ref", returncode=1),
    )
    import typer

    with pytest.raises(typer.Exit):
        skills_mod._git("clone", "x")


# ---------- _read_skill_name ----------


def test_read_skill_name_no_frontmatter_uses_fallback(tmp_path: Path) -> None:
    md = tmp_path / "SKILL.md"
    md.write_text("# Title\n\nbody", encoding="utf-8")

    assert skills_mod._read_skill_name(md, "dirname") == "dirname"


def test_read_skill_name_extracts_from_frontmatter(tmp_path: Path) -> None:
    md = tmp_path / "SKILL.md"
    md.write_text("---\nname: actual\nother: x\n---\n\nbody", encoding="utf-8")

    assert skills_mod._read_skill_name(md, "dirname") == "actual"


def test_read_skill_name_strips_quotes(tmp_path: Path) -> None:
    md = tmp_path / "SKILL.md"
    md.write_text('---\nname: "with-quotes"\n---\n', encoding="utf-8")

    assert skills_mod._read_skill_name(md, "fallback") == "with-quotes"


def test_read_skill_name_unclosed_frontmatter(tmp_path: Path) -> None:
    md = tmp_path / "SKILL.md"
    md.write_text("---\nname: x", encoding="utf-8")  # no closing ---

    assert skills_mod._read_skill_name(md, "fb") == "fb"


# ---------- list ----------


def test_list_empty_skills_dir(fake_home: Path) -> None:
    result = runner.invoke(skills_app, ["list"])

    assert result.exit_code == 0
    assert "no skills installed" in result.output


def test_list_with_provenance_and_local(fake_home: Path) -> None:
    fake_home.mkdir(parents=True)
    a = fake_home / "alpha"
    a.mkdir()
    (a / skills_mod.PROVENANCE_FILE).write_text(
        json.dumps({"source": "x/y", "ref": "main", "sha": "abcdef0"}), encoding="utf-8"
    )
    (fake_home / "beta").mkdir()  # no provenance

    result = runner.invoke(skills_app, ["list"])

    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "x/y" in result.output
    assert "beta" in result.output
    assert "no .installed-from" in result.output


def test_list_uses_sha_when_no_ref(fake_home: Path) -> None:
    fake_home.mkdir(parents=True)
    a = fake_home / "alpha"
    a.mkdir()
    (a / skills_mod.PROVENANCE_FILE).write_text(
        json.dumps({"source": "x/y", "sha": "abcdef01234"}), encoding="utf-8"
    )

    result = runner.invoke(skills_app, ["list"])

    assert "abcdef0" in result.output


# ---------- uninstall ----------


def test_uninstall_missing_skill(fake_home: Path) -> None:
    result = runner.invoke(skills_app, ["uninstall", "ghost"])

    assert result.exit_code == 1
    assert "is not installed" in result.output


def test_uninstall_user_authored_without_force_refuses(fake_home: Path) -> None:
    fake_home.mkdir(parents=True)
    (fake_home / "myskill").mkdir()

    result = runner.invoke(skills_app, ["uninstall", "myskill"])

    assert result.exit_code == 1
    assert "user-authored" in result.output
    assert (fake_home / "myskill").exists()


def test_uninstall_user_authored_with_force_removes(fake_home: Path) -> None:
    fake_home.mkdir(parents=True)
    (fake_home / "myskill").mkdir()

    result = runner.invoke(skills_app, ["uninstall", "myskill", "--force"])

    assert result.exit_code == 0
    assert not (fake_home / "myskill").exists()
    assert "no provenance marker" in result.output


def test_uninstall_provenanced_skill_removes(fake_home: Path) -> None:
    fake_home.mkdir(parents=True)
    skill = fake_home / "alpha"
    skill.mkdir()
    (skill / skills_mod.PROVENANCE_FILE).write_text(
        json.dumps({"source": "x"}), encoding="utf-8"
    )

    result = runner.invoke(skills_app, ["uninstall", "alpha"])

    assert result.exit_code == 0
    assert not skill.exists()


def test_uninstall_validates_name(fake_home: Path) -> None:
    result = runner.invoke(skills_app, ["uninstall", "../escape"])

    assert result.exit_code == 1


# ---------- install ----------


def _populate_clone(
    clone_dir: Path,
    *,
    name: str = "myskill",
    declared_name: str | None = None,
    skill_md: bool = True,
) -> None:
    """Build a fake cloned repo at `clone_dir` with skills/<name>/."""
    sk = clone_dir / "skills" / name
    sk.mkdir(parents=True)
    if skill_md:
        if declared_name is not None:
            (sk / "SKILL.md").write_text(
                f"---\nname: {declared_name}\n---\n", encoding="utf-8"
            )
        else:
            (sk / "SKILL.md").write_text("# Title\nbody\n", encoding="utf-8")


def _stub_clone(mocker, populate):
    """Stub `_clone` to populate the clone dir + return a dummy sha."""

    def _fake_clone(source, ref, dest):
        populate(dest)
        return "abcdef0123456789"

    mocker.patch.object(skills_mod, "_clone", side_effect=_fake_clone)


def test_install_happy_path(fake_home: Path, mocker) -> None:
    _stub_clone(mocker, lambda d: _populate_clone(d, name="myskill"))

    result = runner.invoke(
        skills_app,
        ["install", "myskill", "--from", "owner/repo"],
    )

    assert result.exit_code == 0
    assert (fake_home / "myskill" / "SKILL.md").exists()
    prov = json.loads(
        (fake_home / "myskill" / skills_mod.PROVENANCE_FILE).read_text(encoding="utf-8")
    )
    assert prov["source"] == "https://github.com/owner/repo.git"
    assert prov["sha"].startswith("abcdef")


def test_install_invalid_name_exit_1(fake_home: Path) -> None:
    result = runner.invoke(skills_app, ["install", "../bad", "--from", "o/r"])

    assert result.exit_code == 1


def test_install_existing_provenanced_without_force(
    fake_home: Path,
    mocker,
) -> None:
    fake_home.mkdir(parents=True)
    target = fake_home / "myskill"
    target.mkdir()
    (target / skills_mod.PROVENANCE_FILE).write_text(
        json.dumps({"source": "x/y", "ref": "main"}), encoding="utf-8"
    )

    result = runner.invoke(skills_app, ["install", "myskill", "--from", "o/r"])

    assert result.exit_code == 1
    assert "already installed" in result.output


def test_install_existing_user_authored_without_force(
    fake_home: Path,
    mocker,
) -> None:
    fake_home.mkdir(parents=True)
    target = fake_home / "myskill"
    target.mkdir()

    result = runner.invoke(skills_app, ["install", "myskill", "--from", "o/r"])

    assert result.exit_code == 1
    assert "user-authored" in result.output


def test_install_no_skill_in_source(fake_home: Path, mocker) -> None:
    _stub_clone(mocker, lambda d: None)

    result = runner.invoke(
        skills_app,
        ["install", "missing", "--from", "owner/repo"],
    )

    assert result.exit_code == 1
    assert "does not contain skills/missing" in result.output


def test_install_missing_skill_md(fake_home: Path, mocker) -> None:
    _stub_clone(
        mocker,
        lambda d: _populate_clone(d, name="myskill", skill_md=False),
    )

    result = runner.invoke(
        skills_app,
        ["install", "myskill", "--from", "owner/repo"],
    )

    assert result.exit_code == 1
    assert "missing SKILL.md" in result.output


def test_install_declared_name_mismatch(fake_home: Path, mocker) -> None:
    _stub_clone(
        mocker,
        lambda d: _populate_clone(d, name="myskill", declared_name="other"),
    )

    result = runner.invoke(
        skills_app,
        ["install", "myskill", "--from", "owner/repo"],
    )

    assert result.exit_code == 1
    assert "name='other'" in result.output or 'name="other"' in result.output


def test_install_with_ref_flag(fake_home: Path, mocker) -> None:
    refs_seen = []

    def _fake_clone(source, ref, dest):
        refs_seen.append(ref)
        _populate_clone(dest, name="myskill")
        return "abcdef0"

    mocker.patch.object(skills_mod, "_clone", side_effect=_fake_clone)

    result = runner.invoke(
        skills_app,
        ["install", "myskill", "--from", "owner/repo", "--ref", "v1.0"],
    )

    assert result.exit_code == 0
    assert refs_seen == ["v1.0"]


def test_install_force_overwrites_existing(fake_home: Path, mocker) -> None:
    fake_home.mkdir(parents=True)
    target = fake_home / "myskill"
    target.mkdir()
    (target / "OLD").write_text("old", encoding="utf-8")
    (target / skills_mod.PROVENANCE_FILE).write_text(
        json.dumps({"source": "old", "ref": "main"}), encoding="utf-8"
    )

    _stub_clone(mocker, lambda d: _populate_clone(d, name="myskill"))

    result = runner.invoke(
        skills_app,
        ["install", "myskill", "--from", "owner/repo", "--force"],
    )

    assert result.exit_code == 0
    assert not (target / "OLD").exists()
    assert (target / "SKILL.md").exists()


# ---------- _clone ----------


def test_clone_invokes_git_with_depth_and_branch(mocker, tmp_path: Path) -> None:
    spy = mocker.patch.object(
        skills_mod,
        "_git",
        side_effect=[
            _git_completed(),  # clone
            _git_completed(stdout="abc123\n"),  # rev-parse
        ],
    )

    sha = skills_mod._clone("o/r", "v1", tmp_path / "dest")

    assert sha == "abc123"
    clone_call = spy.call_args_list[0]
    assert "--depth=1" in clone_call.args
    assert "--branch" in clone_call.args


def test_clone_no_branch_when_ref_omitted(mocker, tmp_path: Path) -> None:
    spy = mocker.patch.object(
        skills_mod,
        "_git",
        side_effect=[
            _git_completed(),
            _git_completed(stdout="def456\n"),
        ],
    )

    skills_mod._clone("o/r", None, tmp_path / "dest")

    assert "--branch" not in spy.call_args_list[0].args


# ---------- sync official (CLI surface) ----------


def test_sync_official_converts_sync_error_to_exit_1(mocker) -> None:
    """The interactive command surfaces a SyncError as a clean stderr
    message + exit 1, never a traceback."""
    from physiclaw.cli import sync_official_skills as osk

    mocker.patch.object(osk, "sync", side_effect=osk.SyncError("checksum mismatch"))

    result = runner.invoke(skills_app, ["sync", "official"])

    assert result.exit_code == 1
    assert "checksum mismatch" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.fixture
def source_repo(tmp_path, mocker):
    """Replace only the Git subprocess boundary; use a real copied skill tree."""
    source = tmp_path / "source"
    _populate_clone(source)

    def git(args, **kwargs):
        if args[1] == "clone":
            shutil.copytree(source, args[-1], symlinks=True)
            return _git_completed()
        if args[1:] != ["rev-parse", "HEAD"]:
            return _git_completed(returncode=1, stderr="unknown Git command")
        stdout = "abcdef0123456789\n" if kwargs.get("text") else b"abcdef0123456789\n"
        return _git_completed(stdout=stdout if kwargs.get("capture_output") else None)

    mocker.patch.object(skills_mod.subprocess, "run", side_effect=git)
    return source


@pytest.fixture
def previous_skill(fake_home):
    target = fake_home / "myskill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("previous skill", encoding="utf-8")
    (target / skills_mod.PROVENANCE_FILE).write_text(
        '{"source":"old"}', encoding="utf-8"
    )
    return target


@pytest.mark.parametrize(
    "marker",
    [
        [],
        1,
        True,
        "bad",
        {},
        {"source": "x", "sha": 42},
        {"source": None},
        {"source": "x", "ref": []},
    ],
)
def test_regression_corrupt_provenance_does_not_allow_uninstall(previous_skill, marker):
    (previous_skill / skills_mod.PROVENANCE_FILE).write_text(
        json.dumps(marker), encoding="utf-8"
    )

    result = runner.invoke(skills_app, ["uninstall", "myskill"])

    assert result.exit_code == 1
    assert "--force" in result.output
    assert (previous_skill / "SKILL.md").read_text(encoding="utf-8") == "previous skill"


@pytest.mark.parametrize(
    "marker",
    [
        [],
        {"source": "x", "sha": 42},
        {"source": "x", "sha": None},
        {"source": "x", "ref": []},
    ],
)
def test_regression_corrupt_provenance_does_not_crash_list(previous_skill, marker):
    (previous_skill / skills_mod.PROVENANCE_FILE).write_text(
        json.dumps(marker), encoding="utf-8"
    )

    result = runner.invoke(skills_app, ["list"])

    assert result.exit_code == 0
    assert "myskill" in result.output


@pytest.mark.parametrize(
    "error", [OSError("install blocked"), KeyboardInterrupt("cancel")]
)
def test_regression_failed_replacement_preserves_previous_skill(
    previous_skill, source_repo, mocker, error
):
    replace = skills_mod.os.replace

    def fail(source, destination):
        if Path(destination) == previous_skill and ".backup-" not in str(source):
            raise error
        return replace(source, destination)

    mocker.patch.object(skills_mod.os, "replace", side_effect=fail)

    result = runner.invoke(
        skills_app, ["install", "myskill", "--from", str(source_repo), "--force"]
    )

    assert result.exit_code != 0
    assert (previous_skill / "SKILL.md").read_text(encoding="utf-8") == "previous skill"
    assert not list(previous_skill.parent.glob("*.installing*"))


def test_regression_target_created_during_clone_requires_force(
    fake_home, source_repo, mocker
):
    git = skills_mod.subprocess.run.side_effect
    target = fake_home / "myskill"

    def competing_install(args, **kwargs):
        result = git(args, **kwargs)
        if args[1] == "clone":
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("competing skill", encoding="utf-8")
        return result

    mocker.patch.object(skills_mod.subprocess, "run", side_effect=competing_install)

    result = runner.invoke(
        skills_app, ["install", "myskill", "--from", str(source_repo)]
    )

    assert result.exit_code == 1
    assert "--force" in result.output
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "competing skill"


@pytest.mark.parametrize("entry", ["outside.txt", "SKILL.md", "references"])
def test_regression_source_symlink_cannot_copy_outside_clone(
    fake_home, source_repo, tmp_path, entry
):
    external = tmp_path / "external"
    external.mkdir()
    (external / "SKILL.md").write_text("synthetic private contents", encoding="utf-8")
    link = source_repo / "skills" / "myskill" / entry
    link.unlink(missing_ok=True)
    symlink_to(link, external)

    result = runner.invoke(
        skills_app, ["install", "myskill", "--from", str(source_repo)]
    )

    assert result.exit_code == 1
    assert "outside" in result.output
    assert not (fake_home / "myskill").exists()


@pytest.mark.parametrize(
    "header",
    [
        "name: 'myskill'",
        'name: "myskill"',
        "name: other\nname: myskill",
        "name: ''",
        'name: ""',
    ],
)
def test_regression_installed_frontmatter_name_matches_runtime(
    fake_home, source_repo, mocker, header
):
    from physiclaw.agent.engine import skill

    (source_repo / "skills/myskill/SKILL.md").write_text(
        f"---\n{header}\n---\nbody\n", encoding="utf-8"
    )
    mocker.patch.object(skill, "HOME_SKILLS_DIR", fake_home)
    mocker.patch.object(skill, "OFFICIAL_SKILLS_DIR", source_repo / "absent")

    result = runner.invoke(
        skills_app, ["install", "myskill", "--from", str(source_repo)]
    )

    assert result.exit_code == 0
    assert skill.discover_user_skills()["myskill"].body == "body"


def test_install_copy_failure_is_clean_error_and_keeps_previous_skill(
    previous_skill, source_repo, mocker
):
    copy = skills_mod.shutil.copytree

    def fail(source, dest, *args, **kwargs):
        if ".installing-" in str(dest) and "repo" not in Path(dest).parts:
            raise OSError("copy blocked")
        return copy(source, dest, *args, **kwargs)

    mocker.patch.object(skills_mod.shutil, "copytree", side_effect=fail)

    result = runner.invoke(
        skills_app, ["install", "myskill", "--from", str(source_repo), "--force"]
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert (previous_skill / "SKILL.md").read_text(encoding="utf-8") == "previous skill"


def _link_skill(target, external, marker_only):
    if marker_only:
        target.mkdir()
        symlink_to(
            target / skills_mod.PROVENANCE_FILE, external / skills_mod.PROVENANCE_FILE
        )
    else:
        symlink_to(target, external, target_is_directory=True)


@pytest.mark.parametrize("marker_link", [False, True])
def test_linked_provenance_does_not_authorize_removal(fake_home, tmp_path, marker_link):
    external = tmp_path / "external-skill"
    external.mkdir()
    (external / skills_mod.PROVENANCE_FILE).write_text(
        '{"source":"outside"}', encoding="utf-8"
    )
    target = fake_home / "myskill"
    fake_home.mkdir()
    _link_skill(target, external, marker_link)

    result = runner.invoke(skills_app, ["uninstall", "myskill"])

    assert result.exit_code == 1
    assert "--force" in result.output
    assert target.exists()
    assert external.exists()


@pytest.mark.parametrize("source", ["   ", "\t"])
def test_blank_source_is_a_clean_error(fake_home, source):
    result = runner.invoke(skills_app, ["install", "myskill", "--from", source])

    assert result.exit_code == 1
    assert "source must not be empty" in result.output
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("authority", ["", "localhost", "server"])
def test_local_file_uri_inside_state_directory_is_refused(fake_home, authority):
    source = (
        f"file://{authority}/{skills_mod.paths.HOME.as_posix().lstrip(chr(47))}/source"
    )

    result = runner.invoke(skills_app, ["install", "myskill", "--from", source])

    assert result.exit_code == 1
    assert "own home" in result.output


@pytest.mark.parametrize(
    "command",
    [["install", "myskill", "--from", "o/r"], ["uninstall", "myskill", "--force"]],
)
def test_overlapping_mutation_is_refused_before_changing_skill(previous_skill, command):
    from physiclaw.port.locks import exclusive_file_lock

    with exclusive_file_lock(previous_skill.parent / ".install.lock"):
        result = runner.invoke(skills_app, command)

    assert result.exit_code == 1
    assert "already running" in result.output
    assert (previous_skill / "SKILL.md").read_text(encoding="utf-8") == "previous skill"


@pytest.mark.parametrize(
    "error", [OSError("install blocked"), KeyboardInterrupt("cancel")]
)
def test_failed_restoration_keeps_recoverable_backup(
    previous_skill, source_repo, mocker, error
):
    replace = skills_mod.os.replace

    def fail(source, destination):
        if Path(destination) == previous_skill:
            if ".backup-" in str(source):
                raise PermissionError("restore blocked")
            raise error
        return replace(source, destination)

    mocker.patch.object(skills_mod.os, "replace", side_effect=fail)

    result = runner.invoke(
        skills_app, ["install", "myskill", "--from", str(source_repo), "--force"]
    )

    backups = list(previous_skill.parent.glob(".myskill.backup-*/skill"))
    assert result.exit_code != 0
    assert len(backups) == 1
    assert (backups[0] / "SKILL.md").read_text(encoding="utf-8") == "previous skill"
    assert str(backups[0]) in result.stderr
    assert "restore blocked" in result.output


@pytest.mark.parametrize("target", [".", "cycle"])
def test_source_symlink_cycle_is_refused(fake_home, source_repo, target, mocker):
    symlink_to(source_repo / "skills/myskill/cycle", target)
    walk = mocker.spy(Path, "iterdir")

    result = runner.invoke(
        skills_app, ["install", "myskill", "--from", str(source_repo)]
    )

    assert result.exit_code == 1
    assert "cycle" in result.output
    assert not (fake_home / "myskill").exists()
    assert walk.call_count <= 2


def test_internal_symlink_is_copied_as_an_independent_file(fake_home, source_repo):
    skill = source_repo / "skills/myskill"
    (skill / "reference.txt").write_text("reference", encoding="utf-8")
    symlink_to(skill / "alias.txt", "reference.txt")

    result = runner.invoke(
        skills_app, ["install", "myskill", "--from", str(source_repo)]
    )

    assert result.exit_code == 0
    assert (fake_home / "myskill/alias.txt").read_text(encoding="utf-8") == "reference"
    assert not (fake_home / "myskill/alias.txt").is_symlink()


def test_first_install_rename_failure_leaves_no_partial_skill(
    fake_home, source_repo, mocker
):
    mocker.patch.object(skills_mod.os, "replace", side_effect=OSError("rename blocked"))

    result = runner.invoke(
        skills_app, ["install", "myskill", "--from", str(source_repo)]
    )

    assert result.exit_code == 1
    assert "rename blocked" in result.output
    assert isinstance(result.exception, SystemExit)
    assert not (fake_home / "myskill").exists()
    assert list(fake_home.iterdir()) == [fake_home / ".install.lock"]


def test_listing_permission_failure_is_a_clean_cli_error(fake_home, mocker):
    fake_home.mkdir()
    mocker.patch.object(Path, "iterdir", side_effect=PermissionError("listing blocked"))

    result = runner.invoke(skills_app, ["list"])

    assert result.exit_code == 1
    assert "listing blocked" in result.output
    assert isinstance(result.exception, SystemExit)


def test_forced_uninstall_of_link_keeps_external_tree(fake_home, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    fake_home.mkdir()
    symlink_to(fake_home / "myskill", external, target_is_directory=True)

    result = runner.invoke(skills_app, ["uninstall", "myskill", "--force"])

    assert result.exit_code == 0
    assert external.is_dir()
    assert not (fake_home / "myskill").is_symlink()


def test_successful_install_cleanup_failure_does_not_report_failure(
    fake_home, source_repo, mocker
):
    rmdir = skills_mod.os.rmdir

    def blocked(path, *args, **kwargs):
        if ".installing-" in str(path) or ".backup-" in str(path):
            raise PermissionError("cleanup blocked")
        return rmdir(path, *args, **kwargs)

    mocker.patch.object(skills_mod.os, "rmdir", side_effect=blocked)

    result = runner.invoke(
        skills_app, ["install", "myskill", "--from", str(source_repo)]
    )

    assert result.exit_code == 0
    assert (fake_home / "myskill/SKILL.md").is_file()


def test_source_starting_with_dash_is_after_git_option_terminator(
    fake_home, source_repo
):
    result = runner.invoke(skills_app, ["install", "myskill", "--from", "--unwanted"])

    assert result.exit_code == 0
    assert skills_mod.subprocess.run.call_args_list[0].args[0][-3:-1] == [
        "--",
        "--unwanted",
    ]


@given(
    source=st.from_regex(r"[A-Za-z][A-Za-z0-9/_.-]{0,40}", fullmatch=True),
    ref=st.text(),
    sha=st.text(),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_provenance_roundtrips_string_metadata(tmp_path, source, ref, sha):
    data = {"source": source, "ref": ref, "sha": sha}
    (tmp_path / skills_mod.PROVENANCE_FILE).write_text(
        json.dumps(data), encoding="utf-8"
    )

    result = skills_mod.read_provenance(tmp_path)

    assert result == data


@freeze_time("2026-10-15 12:34:56")
def test_install_records_selected_source_ref_commit_and_time(fake_home, source_repo):
    result = runner.invoke(
        skills_app, ["install", "myskill", "--from", str(source_repo), "--ref", "v1"]
    )

    assert result.exit_code == 0
    assert skills_mod.read_provenance(fake_home / "myskill") == {
        "source": str(source_repo),
        "ref": "v1",
        "sha": "abcdef0123456789",
        "installed_at": "2026-10-15T12:34:56",
    }


def test_network_file_uri_is_preserved_for_git(fake_home, source_repo):
    result = runner.invoke(
        skills_app, ["install", "myskill", "--from", "file://server/share/repo"]
    )

    assert result.exit_code == 0
    assert (
        skills_mod.read_provenance(fake_home / "myskill")["source"]
        == "file://server/share/repo"
    )


def test_nonregular_source_entry_is_refused(fake_home, source_repo, mocker):
    is_file = Path.is_file

    def special(path):
        return False if path.name == "special" else is_file(path)

    (source_repo / "skills/myskill/special").write_text("fixture", encoding="utf-8")
    mocker.patch.object(Path, "is_file", special)

    result = runner.invoke(
        skills_app, ["install", "myskill", "--from", str(source_repo)]
    )

    assert result.exit_code == 1
    assert "unsupported skill file" in result.output
    assert not (fake_home / "myskill").exists()


@pytest.mark.parametrize("operation", ["expanduser", "resolve"])
def test_unresolvable_source_is_clean_cli_error(fake_home, mocker, operation):
    mocker.patch.object(Path, operation, side_effect=RuntimeError("invalid path"))

    result = runner.invoke(skills_app, ["install", "myskill", "--from", "~/source"])

    assert result.exit_code == 1
    assert "invalid local skill source" in result.output
    assert isinstance(result.exception, SystemExit)


def test_legacy_null_optional_provenance_is_treated_as_missing(tmp_path):
    (tmp_path / skills_mod.PROVENANCE_FILE).write_text(
        '{"source":"owner/repo","ref":null,"sha":null}', encoding="utf-8"
    )

    provenance = skills_mod.read_provenance(tmp_path)

    assert provenance == {"source": "owner/repo"}


@pytest.mark.parametrize("source", ["", "  ", "\t\n"])
def test_blank_provenance_source_does_not_authorize_uninstall(previous_skill, source):
    (previous_skill / skills_mod.PROVENANCE_FILE).write_text(
        json.dumps({"source": source}), encoding="utf-8"
    )

    result = runner.invoke(skills_app, ["uninstall", "myskill"])

    assert result.exit_code == 1
    assert "--force" in result.output
    assert previous_skill.exists()


@pytest.mark.parametrize("where", ["stderr", "stdout"])
def test_git_failure_reports_operation_and_diagnostic(fake_home, mocker, where):
    result = _git_completed(returncode=1, **{where: "requested ref not found"})
    mocker.patch.object(skills_mod.subprocess, "run", return_value=result)

    result = runner.invoke(skills_app, ["install", "myskill", "--from", "o/r"])

    assert result.exit_code == 1
    assert "git clone failed: requested ref not found" in result.output


def test_parent_relative_source_is_not_rewritten_as_github_shorthand(
    fake_home, source_repo
):
    result = runner.invoke(skills_app, ["install", "myskill", "--from", "../repo"])

    assert result.exit_code == 0
    assert skills_mod.read_provenance(fake_home / "myskill")["source"] == str(
        Path("../repo")
    )


def test_install_without_ref_records_empty_ref(fake_home, source_repo):
    result = runner.invoke(
        skills_app, ["install", "myskill", "--from", str(source_repo)]
    )

    assert result.exit_code == 0
    assert skills_mod.read_provenance(fake_home / "myskill")["ref"] == ""


@pytest.mark.parametrize("command", [["list"], ["install", "myskill", "--from", "o/r"]])
@pytest.mark.parametrize(
    "metadata,revision",
    [
        ({"ref": "stable", "sha": "abcdef01234"}, "stable"),
        ({"sha": "abcdef01234"}, "abcdef0"),
        ({}, "?"),
    ],
)
def test_installed_revision_is_shown_to_user(
    previous_skill, command, metadata, revision
):
    (previous_skill / skills_mod.PROVENANCE_FILE).write_text(
        json.dumps({"source": "owner/repo", **metadata}), encoding="utf-8"
    )

    result = runner.invoke(skills_app, command)

    assert f"@ {revision}" in result.output


def test_staging_is_hidden_from_installed_skill_listing(fake_home, source_repo, mocker):
    git = skills_mod.subprocess.run.side_effect
    observed = []

    def clone(args, **kwargs):
        observed.extend(skills_mod.installed_skill_dirs())
        return git(args, **kwargs)

    mocker.patch.object(skills_mod.subprocess, "run", side_effect=clone)

    result = runner.invoke(
        skills_app, ["install", "myskill", "--from", str(source_repo)]
    )

    assert result.exit_code == 0
    assert observed == []


@pytest.mark.parametrize(
    "flags,dry_run,force",
    [
        ([], False, False),
        (["--dry-run"], True, False),
        (["--force"], False, True),
        (["--dry-run", "--force"], True, True),
    ],
)
def test_sync_cli_passes_selected_flags(flags, dry_run, force, mocker):
    from physiclaw.cli import sync_official_skills

    sync = mocker.patch.object(sync_official_skills, "sync")

    result = runner.invoke(skills_app, ["sync", "official", *flags])

    assert result.exit_code == 0
    sync.assert_called_once_with(dry_run=dry_run, force=force)


@pytest.mark.parametrize("command", [[], ["install"], ["uninstall"], ["sync"]])
def test_command_without_required_input_displays_help(command):
    result = runner.invoke(skills_app, command)

    assert result.exit_code == 2
    assert "Usage:" in result.output
    assert "Options" in result.output


@pytest.mark.parametrize(
    "source", ["https://example.test/owner/repo.git", "git@example.test:owner/repo.git"]
)
def test_remote_source_is_not_resolved_against_state_directory(
    fake_home, source_repo, monkeypatch, source
):
    skills_mod.paths.HOME.mkdir()
    monkeypatch.chdir(skills_mod.paths.HOME)

    result = runner.invoke(skills_app, ["install", "myskill", "--from", source])

    assert result.exit_code == 0
    assert skills_mod.read_provenance(fake_home / "myskill")["source"] == source
