"""Software releases work without a POSIX shell and stop on failed steps."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from textwrap import dedent

import pytest

from scripts import release

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def project(tmp_path, monkeypatch):
    project = tmp_path / "release checkout"
    project.mkdir()
    monkeypatch.chdir(project)
    for name in ("VERSION", "VERBOSE", "RELEASE_FLOW", "UV_PUBLISH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "physiclaw"\nversion = "0.8.4"\n', encoding="utf-8"
    )
    (project / "Makefile").write_bytes((REPO_ROOT / "Makefile").read_bytes())
    (project / "scripts").mkdir()
    (project / "scripts" / "release.py").write_bytes(
        (REPO_ROOT / "scripts" / "release.py").read_bytes()
    )
    return project


@pytest.fixture
def commands(project, mocker):
    def run(command, **kwargs):
        output, code = "", 0
        if command[:3] == ("git", "rev-parse", "--abbrev-ref"):
            output = "main\n"
        elif command[:3] == ("git", "rev-parse", "--verify"):
            code = 1
        elif command == ("uv", "build"):
            for path in release.artifact_paths(release.read_version()):
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b"package")
        return subprocess.CompletedProcess(command, code, output)

    return mocker.patch.object(release.subprocess, "run", side_effect=run)


def invoked(commands):
    return [call.args[0] for call in commands.call_args_list]


def run_make(project, action, *options):
    env = {
        **os.environ,
        "UV_OFFLINE": "1",
        "UV_PYTHON": sys.executable,
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_CACHE_DIR": str(project / "cache"),
    }
    command = [
        "make",
        "--no-print-directory",
        "-j4",
        "-f",
        str(project / "Makefile"),
        action,
        *options,
    ]
    if os.name == "nt":
        command.append("SHELL=cmd.exe")
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)


def test_full_release_uses_new_version_and_runs_in_order(commands, monkeypatch, capsys):
    monkeypatch.setenv("UV_PUBLISH_TOKEN", "test-token")

    assert release.main(["release"]) == 0

    assert invoked(commands) == [
        ("git", "status", "--porcelain"),
        ("git", "rev-parse", "--abbrev-ref", "HEAD"),
        ("git", "rev-parse", "--verify", "--quiet", "refs/tags/v0.8.5"),
        ("uv", "lock"),
        ("git", "add", "pyproject.toml", "uv.lock"),
        ("git", "commit", "-m", "chore: bump version to 0.8.5"),
        ("uv", "build"),
        ("git", "rev-parse", "--verify", "--quiet", "refs/tags/v0.8.5"),
        (
            "uv",
            "publish",
            "dist/physiclaw-0.8.5-py3-none-any.whl",
            "dist/physiclaw-0.8.5.tar.gz",
        ),
        ("git", "tag", "-a", "v0.8.5", "-m", "physiclaw v0.8.5"),
        ("git", "push", "origin", "main"),
        ("git", "push", "origin", "v0.8.5"),
    ]
    output = capsys.readouterr().out
    assert all(f"[{stage}/4]" in output for stage in range(1, 5))
    assert "Next:" not in output
    assert output.count("Released physiclaw 0.8.5") == 1
    assert "test-token" not in output
    assert "\033" not in output


@pytest.mark.parametrize(
    ("command", "output", "code", "error"),
    [
        (("git", "status", "--porcelain"), " M README.md\n", 0, "Working tree dirty"),
        (("git", "rev-parse", "--abbrev-ref", "HEAD"), "feature\n", 0, "Not on main"),
        (("git", "rev-parse", "--verify"), "abc\n", 0, "already exists"),
        (
            ("git", "rev-parse", "--verify"),
            "fatal: not a git repository\n",
            128,
            "Couldn't check",
        ),
    ],
)
def test_bump_guards_leave_version_untouched(
    project, commands, capsys, command, output, code, error
):
    original = commands.side_effect
    before = (project / "pyproject.toml").read_bytes()

    def guard(args, **kwargs):
        if args[: len(command)] == command:
            return subprocess.CompletedProcess(args, code, output)
        return original(args, **kwargs)

    commands.side_effect = guard

    assert release.main(["release"]) != 0

    assert (project / "pyproject.toml").read_bytes() == before
    assert not any(args[0] == "uv" for args in invoked(commands))
    assert error in capsys.readouterr().err


def test_bump_override_preserves_other_fields_comments_and_crlf(
    project, commands, monkeypatch, capsys
):
    text = (
        '# 中文\r\n[project]\r\nname = "physiclaw"\r\n'
        "version = '0.8.4' # release\r\n\r\n[tool.example]\r\nversion = '9.9'\r\n"
    )
    path = project / "pyproject.toml"
    path.write_bytes(text.encode("utf-8"))
    monkeypatch.setenv("VERSION", "1.0.0rc1")

    assert release.main(["bump"]) == 0

    assert path.read_bytes() == text.replace("'0.8.4'", "'1.0.0rc1'").encode("utf-8")
    assert invoked(commands)[-1][-1] == "chore: bump version to 1.0.0rc1"
    assert "Next: make build && make publish" in capsys.readouterr().out


def test_bump_rejects_version_match_inside_multiline_text(project, commands, capsys):
    path = project / "pyproject.toml"
    text = (
        '[project]\nname = "physiclaw"\n'
        "description = '''\nversion = \"0.8.4\"\n'''\n"
        'version = "0.8.4"\n'
    )
    path.write_text(text, encoding="utf-8")
    before = path.read_bytes()

    assert release.main(["bump"]) == 1

    assert path.read_bytes() == before
    assert not any(args[0] == "uv" for args in invoked(commands))
    assert "Couldn't update [project].version" in capsys.readouterr().err


@pytest.mark.parametrize("version", ["1.0; echo bad", "$(echo bad)", "../1", "1\n2"])
def test_invalid_version_never_reaches_commands(
    project, commands, monkeypatch, version
):
    monkeypatch.setenv("VERSION", version)
    before = (project / "pyproject.toml").read_bytes()

    assert release.main(["bump"]) == 1

    assert (project / "pyproject.toml").read_bytes() == before
    assert len(invoked(commands)) == 2


def test_non_numeric_last_component_needs_explicit_version(project, commands, capsys):
    (project / "pyproject.toml").write_text(
        '[project]\nversion = "1.0rc1"\n', encoding="utf-8"
    )

    assert release.main(["bump"]) == 1

    assert "Can't auto-bump" in capsys.readouterr().err
    assert len(invoked(commands)) == 2


def test_build_removes_only_current_artifacts_before_build(project, commands):
    artifacts = release.artifact_paths("0.8.4")
    (project / "dist").mkdir()
    for path in artifacts:
        path.write_bytes(b"stale")
    older = project / "dist" / "physiclaw-0.8.3.tar.gz"
    older.write_bytes(b"older")
    original = commands.side_effect

    def build(args, **kwargs):
        assert args == ("uv", "build")
        assert not any(path.exists() for path in artifacts)
        return original(args, **kwargs)

    commands.side_effect = build

    assert release.main(["build"]) == 0

    assert older.read_bytes() == b"older"
    assert all(path.read_bytes() == b"package" for path in artifacts)


def test_missing_build_artifact_stops_before_publish(commands, monkeypatch, capsys):
    original = commands.side_effect
    monkeypatch.setenv("UV_PUBLISH_TOKEN", "test-token")

    def build(args, **kwargs):
        if args == ("uv", "build"):
            return subprocess.CompletedProcess(args, 0, "")
        return original(args, **kwargs)

    commands.side_effect = build

    assert release.main(["release"]) == 1

    assert invoked(commands)[-1] == ("uv", "build")
    assert "Expected artifact missing" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["token", "wheel", "sdist"])
def test_publish_checks_prerequisites_before_upload(
    project, commands, monkeypatch, capsys, missing
):
    if missing != "token":
        monkeypatch.setenv("UV_PUBLISH_TOKEN", "test-token")
    for label, path in zip(("wheel", "sdist"), release.artifact_paths("0.8.4")):
        if label != missing:
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"package")

    assert release.main(["publish"]) == 1

    assert not commands.called
    error = capsys.readouterr().err
    if missing == "token":
        assert "UV_PUBLISH_TOKEN not set" in error
    else:
        assert "not found - run make build first" in error


@pytest.mark.parametrize(
    "failed",
    [
        ("git", "status"),
        ("git", "rev-parse", "--abbrev-ref"),
        ("uv", "lock"),
        ("git", "add"),
        ("git", "commit"),
        ("uv", "build"),
        ("uv", "publish"),
        ("git", "tag", "-a"),
        ("git", "push", "origin", "main"),
        ("git", "push", "origin", "v0.8.5"),
    ],
)
def test_release_stops_after_failed_command(commands, monkeypatch, capsys, failed):
    monkeypatch.setenv("UV_PUBLISH_TOKEN", "test-token")
    original = commands.side_effect

    def fail(args, **kwargs):
        if args[: len(failed)] == failed:
            return subprocess.CompletedProcess(args, 7, "command diagnostics\n")
        return original(args, **kwargs)

    commands.side_effect = fail

    assert release.main(["release"]) == 7

    assert invoked(commands)[-1][: len(failed)] == failed
    output = capsys.readouterr()
    assert "command diagnostics" in output.err
    assert "Failed (exit 7)" in output.err
    assert "Released physiclaw" not in output.out


@pytest.mark.parametrize("code", [0, 128])
def test_publish_stops_on_existing_tag_or_git_error(
    commands, monkeypatch, capsys, code
):
    monkeypatch.setenv("UV_PUBLISH_TOKEN", "test-token")
    for path in release.artifact_paths("0.8.4"):
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"package")
    commands.side_effect = None
    commands.return_value = subprocess.CompletedProcess([], code, "tag diagnostics\n")

    assert release.main(["publish"]) != 0

    assert len(invoked(commands)) == 1
    assert invoked(commands)[0][:3] == ("git", "rev-parse", "--verify")
    output = capsys.readouterr()
    assert (
        "already exists" in output.err if code == 0 else "Couldn't check" in output.err
    )
    assert "Released physiclaw" not in output.out


def test_interrupted_release_does_not_continue(commands, capsys):
    commands.side_effect = KeyboardInterrupt

    assert release.main(["release"]) == 130

    assert len(invoked(commands)) == 1
    assert "Released physiclaw" not in capsys.readouterr().out


@pytest.mark.parametrize("verbose", [False, True])
def test_command_logging_and_exit_status(project, monkeypatch, capfd, verbose):
    monkeypatch.setenv("VERBOSE", "1" if verbose else "0")
    workflow = release.Release()
    workflow.run(sys.executable, "-c", "print('success details')")
    output = capfd.readouterr()
    assert ("success details" in output.out) is verbose

    with pytest.raises(release.ReleaseError) as failure:
        workflow.run(
            sys.executable, "-c", "print('failure details'); raise SystemExit(7)"
        )

    assert failure.value.code == 7
    output = capfd.readouterr()
    assert "failure details" in output.out + output.err


@pytest.mark.parametrize(
    ("action", "error"),
    [
        ("bump", "Working tree dirty"),
        ("release", "Working tree dirty"),
        ("publish", "UV_PUBLISH_TOKEN not set"),
    ],
)
def test_native_make_enters_python_workflow(project, action, error):
    # Real make + Git, but a temporary repository and a guard that prevents edits.
    # This unavailable dependency must not be resolved before the release guard.
    with (project / "pyproject.toml").open("a", encoding="utf-8") as source:
        source.write('dependencies = ["physiclaw-missing-release-fixture==0"]\n')
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    result = run_make(project, action)

    assert result.returncode != 0
    assert error in result.stderr, result.stdout + result.stderr
    assert "CreateProcess" not in result.stderr
    assert "Traceback" not in result.stderr
    assert release.read_version() == "0.8.4"
    assert not (project / "uv.lock").exists()
    assert not (project / ".venv").exists()


def test_native_make_bump_commits_version_and_lockfile(project):
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], check=True)
    for key, value in (
        ("user.name", "Release Test"),
        ("user.email", "release@example.invalid"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(project / "no-hooks")),
    ):
        subprocess.run(["git", "config", "--local", key, value], check=True)
    (project / ".gitignore").write_text("cache/\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "pyproject.toml", ".gitignore", "Makefile", "scripts"],
        check=True,
    )
    subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], check=True)

    result = run_make(project, "bump", "VERSION=1.2.3")

    assert result.returncode == 0, result.stdout + result.stderr
    assert release.read_version() == "1.2.3"
    with (project / "uv.lock").open("rb") as lock:
        packages = tomllib.load(lock)["package"]
    assert next(p for p in packages if p["name"] == "physiclaw")["version"] == "1.2.3"
    status = subprocess.check_output(["git", "status", "--porcelain"], text=True)
    assert status == ""
    subject = subprocess.check_output(["git", "log", "-1", "--format=%s"], text=True)
    assert subject.strip() == "chore: bump version to 1.2.3"
    assert subprocess.check_output(["git", "tag", "--list"], text=True) == ""


def test_native_make_build_without_unix_utilities(project):
    # Let the real uv build a tiny stdlib-only backend, with no network or app deps.
    (project / "pyproject.toml").write_text(
        '[project]\nname = "physiclaw"\nversion = "0.8.4"\n'
        '[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n',
        encoding="utf-8",
    )
    (project / "backend.py").write_text(
        dedent("""\
            from pathlib import Path
            import tarfile

            def build_sdist(sdist_directory, config_settings=None):
                name = "physiclaw-0.8.4.tar.gz"
                with tarfile.open(Path(sdist_directory) / name, "w:gz") as archive:
                    for source in ("pyproject.toml", "backend.py"):
                        archive.add(source, arcname="physiclaw-0.8.4/" + source)
                return name

            def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
                name = "physiclaw-0.8.4-py3-none-any.whl"
                (Path(wheel_directory) / name).write_bytes(b"wheel fixture")
                return name
            """),
        encoding="utf-8",
    )
    result = run_make(project, "build")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Built wheel + source archive" in result.stdout
    assert all(path.is_file() for path in release.artifact_paths("0.8.4"))
