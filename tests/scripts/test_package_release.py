"""Software releases work without a POSIX shell and stop on failed steps."""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from textwrap import dedent, indent
from zipfile import ZipFile

import pytest

from scripts import release
from tests.timing_helpers import PROCESS_TIMEOUT

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def project(tmp_path, monkeypatch):
    project = tmp_path / "release checkout"
    project.mkdir()
    monkeypatch.chdir(project)
    for name in (
        "VERSION",
        "VERBOSE",
        "RUN_ID",
        "JSON",
        "RELEASE_FLOW",
        "UV_PUBLISH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "physiclaw"\nversion = "0.8.4"\nrequires-python = ">=3.12"\n',
        encoding="utf-8",
    )
    (project / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.12"\n'
        '[[package]]\nname = "physiclaw"\nversion = "0.8.4"\n'
        'source = { virtual = "." }\n',
        encoding="utf-8",
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
        elif command in {
            ("git", "remote", "get-url", "origin"),
            ("git", "remote", "get-url", "--push", "--all", "origin"),
        }:
            output = "https://github.com/physiclaw/PhysiClaw.git\n"
        elif command[:3] == ("git", "ls-remote", "--exit-code"):
            code = 2
        elif command[:2] == ("git", "rev-parse"):
            output = "a" * 40 + "\n"
        elif command == ("uv", "lock"):
            path = Path("uv.lock")
            text = path.read_text(encoding="utf-8")
            locked = tomllib.loads(text)["package"][0]["version"]
            path.write_text(
                text.replace(
                    f'version = "{locked}"', f'version = "{release.read_version()}"'
                ),
                encoding="utf-8",
            )
        elif command[:4] == ("gh", "api", "--hostname", "github.com"):
            output = json.dumps({"workflow_runs": [successful_ci("a" * 40)]})
        elif command == ("uv", "build", "--no-sources"):
            for path in release.artifact_paths(release.read_version()):
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b"package")
        return subprocess.CompletedProcess(command, code, output)

    return mocker.patch.object(release.subprocess, "run", side_effect=run)


def successful_ci(head):
    return {
        "head_sha": head,
        "head_branch": "main",
        "event": "push",
        "path": ".github/workflows/ci.yml",
        "status": "completed",
        "conclusion": "success",
    }


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
    return subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=PROCESS_TIMEOUT
    )


@pytest.mark.parametrize("action", ["release-start", "release", "publish"])
def test_release_tags_pushed_version_without_bump_build_or_upload(
    commands, capsys, action
):
    assert release.main([action]) == 0
    assert release.read_version() == "0.8.4"
    calls = invoked(commands)
    assert [args for args in calls if args[0] == "uv"] == [("uv", "lock", "--check")]
    assert ("git", "push", "origin", "main") not in calls
    assert calls[-2:] == [
        ("git", "tag", "-a", "v0.8.4", "a" * 40, "-m", "physiclaw v0.8.4"),
        ("git", "push", "origin", "refs/tags/v0.8.4"),
    ]
    output = capsys.readouterr().out
    assert "Release requested for physiclaw 0.8.4" in output
    assert "Released physiclaw" not in output
    assert "actions/workflows/release.yml" in output


@pytest.mark.parametrize("action", ["release-start", "release", "publish"])
@pytest.mark.parametrize(
    ("command", "output", "code", "error"),
    [
        (("git", "status", "--porcelain"), " M README.md\n", 0, "Working tree dirty"),
        (("git", "rev-parse", "--abbrev-ref", "HEAD"), "feature\n", 0, "Not on main"),
        (
            ("git", "rev-parse", "--verify"),
            "fatal: not a git repository\n",
            128,
            "Couldn't check",
        ),
    ],
)
def test_release_guards_leave_version_untouched(
    project, commands, capsys, action, command, output, code, error
):
    original = commands.side_effect
    before = (project / "pyproject.toml").read_bytes()

    def guard(args, **kwargs):
        if args[: len(command)] == command:
            return subprocess.CompletedProcess(args, code, output)
        return original(args, **kwargs)

    commands.side_effect = guard

    assert release.main([action]) != 0

    assert (project / "pyproject.toml").read_bytes() == before
    assert not any(args[0] == "uv" for args in invoked(commands))
    assert invoked(commands)[-1][: len(command)] == command
    assert error in capsys.readouterr().err


def test_bump_rejects_existing_tag(project, commands, capsys):
    original = commands.side_effect
    before = (project / "pyproject.toml").read_bytes()

    def existing_tag(args, **kwargs):
        if args[:3] == ("git", "rev-parse", "--verify"):
            return subprocess.CompletedProcess(args, 0, "b" * 40 + "\n")
        return original(args, **kwargs)

    commands.side_effect = existing_tag

    assert release.main(["bump"]) == 1
    assert "Tag v0.8.5 already exists" in capsys.readouterr().err
    assert (project / "pyproject.toml").read_bytes() == before
    assert not any(args[0] == "uv" for args in invoked(commands))


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
    assert "push the version commit to main" in capsys.readouterr().out


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


@pytest.mark.parametrize(
    "failure", ["preflight", "lock", "metadata", "dependencies", "invalid", "interrupt"]
)
def test_failed_bump_restores_files_without_staging(project, commands, failure):
    before = {
        name: (project / name).read_bytes() for name in ("pyproject.toml", "uv.lock")
    }
    original = commands.side_effect

    def fail(args, **kwargs):
        if args == ("uv", "lock", "--check") and failure == "preflight":
            return subprocess.CompletedProcess(
                args, 7, "Incompatible uv or stale lockfile"
            )
        if args == ("uv", "lock"):
            original(args, **kwargs)
            lock = Path("uv.lock")
            if failure == "metadata":
                lock.write_text(
                    lock.read_text(encoding="utf-8").replace(
                        "version = 1\n", "version = 1\nrevision = 999\n", 1
                    ),
                    encoding="utf-8",
                )
            if failure == "dependencies":
                lock.write_text(
                    lock.read_text(encoding="utf-8")
                    + '\ndependencies = [{ name = "unexpected" }]\n',
                    encoding="utf-8",
                )
            if failure == "lock":
                return subprocess.CompletedProcess(args, 7, "Failed to resolve")
            if failure == "interrupt":
                raise KeyboardInterrupt
            if failure == "invalid":
                Path("uv.lock").write_text("version = [broken", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "")
        return original(args, **kwargs)

    commands.side_effect = fail
    assert release.main(["bump"]) != 0
    assert {name: (project / name).read_bytes() for name in before} == before
    assert not any(
        args[:2] in {("git", "add"), ("git", "commit")} for args in invoked(commands)
    )


def test_bump_rejects_missing_project_in_lockfile(project, commands, capsys):
    lock = project / "uv.lock"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace('"physiclaw"', '"other"'),
        encoding="utf-8",
    )
    before = (project / "pyproject.toml").read_bytes()
    assert release.main(["bump"]) == 1
    assert "current version" in capsys.readouterr().err
    assert (project / "pyproject.toml").read_bytes() == before
    assert ("uv", "lock") not in invoked(commands)


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
        '[project]\nversion = "1.0.0rc1"\n', encoding="utf-8"
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
        assert args == ("uv", "build", "--no-sources")
        assert not any(path.exists() for path in artifacts)
        return original(args, **kwargs)

    commands.side_effect = build

    assert release.main(["build"]) == 0

    assert older.read_bytes() == b"older"
    assert all(path.read_bytes() == b"package" for path in artifacts)


def test_build_rejects_missing_artifact(commands, capsys):
    original = commands.side_effect

    def build(args, **kwargs):
        if args == ("uv", "build", "--no-sources"):
            return subprocess.CompletedProcess(args, 0, "")
        return original(args, **kwargs)

    commands.side_effect = build

    assert release.main(["build"]) == 1

    assert invoked(commands)[-1] == ("uv", "build", "--no-sources")
    assert "Expected artifact missing" in capsys.readouterr().err


@pytest.mark.parametrize(
    "failed",
    [
        ("git", "status"),
        ("git", "fetch"),
        ("uv", "lock", "--check"),
        ("gh", "api"),
        ("git", "tag", "-a"),
        ("git", "push", "origin", "refs/tags/v0.8.4"),
    ],
)
def test_release_stops_after_failed_command(commands, capsys, failed):
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
    assert "Release requested" not in output.out


def test_interrupted_release_does_not_continue(commands, capsys):
    commands.side_effect = KeyboardInterrupt

    assert release.main(["release"]) == 130

    assert len(invoked(commands)) == 1
    assert "Released physiclaw" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "version",
    ["0.8.4", "0.8.3", "0.8.4rc1", "01.0.0", "1.0", "1.0.0+local", "1.0.0.post1"],
)
def test_bump_rejects_non_increasing_or_unsupported_versions(
    project, commands, monkeypatch, version
):
    monkeypatch.setenv("VERSION", version)
    original = (project / "pyproject.toml").read_bytes()
    assert release.main(["bump"]) == 1
    assert (project / "pyproject.toml").read_bytes() == original
    assert not any(args[0] == "uv" for args in invoked(commands))


@pytest.mark.parametrize(
    "current,new",
    [("0.9.0rc1", "0.9.0rc2"), ("0.9.0rc2", "0.9.0"), ("0.9.0", "0.10.0a1")],
)
def test_bump_accepts_prerelease_progression_on_release_branch(
    project, commands, monkeypatch, current, new
):
    (project / "pyproject.toml").write_text(
        f'[project]\nname = "physiclaw"\nversion = "{current}"\n', encoding="utf-8"
    )
    lock = project / "uv.lock"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace('"0.8.4"', f'"{current}"'),
        encoding="utf-8",
    )
    monkeypatch.setenv("VERSION", new)
    original = commands.side_effect

    def branch(args, **kwargs):
        if args == ("git", "rev-parse", "--abbrev-ref", "HEAD"):
            return subprocess.CompletedProcess(args, 0, "release/candidate\n")
        return original(args, **kwargs)

    commands.side_effect = branch
    assert release.main(["bump"]) == 0
    assert release.read_version() == new
    assert not any(
        args[:2] in {("git", "push"), ("git", "tag")} for args in invoked(commands)
    )


def test_release_rejects_inline_version_bump(commands, monkeypatch):
    monkeypatch.setenv("VERSION", "0.9.0")
    assert release.main(["release"]) == 1
    assert not commands.called


@pytest.mark.parametrize(
    "command,code,output,error",
    [
        (
            ("git", "remote", "get-url", "origin"),
            0,
            "https://github.com/other/fork.git",
            "official",
        ),
        (("git", "rev-parse", "FETCH_HEAD"), 0, "b" * 40, "HEAD must equal"),
        (
            ("git", "ls-remote", "--exit-code", "--tags", "origin", "refs/tags/v0.8.4"),
            0,
            "existing",
            "Remote tag",
        ),
        (
            ("git", "ls-remote", "--exit-code", "--tags", "origin", "refs/tags/v0.8.4"),
            128,
            "network error",
            "Couldn't check remote",
        ),
    ],
)
def test_release_preflight_stops_before_creating_tag(
    commands, capsys, command, code, output, error
):
    original = commands.side_effect

    def guard(args, **kwargs):
        if args == command:
            return subprocess.CompletedProcess(args, code, output)
        return original(args, **kwargs)

    commands.side_effect = guard
    assert release.main(["release"]) != 0
    assert error in capsys.readouterr().err
    assert not any(args[:2] == ("git", "tag") for args in invoked(commands))


@pytest.mark.parametrize("action", ["release-check", "release-start"])
@pytest.mark.parametrize(
    "change",
    [
        None,
        {"status": "queued", "conclusion": None},
        {"status": "in_progress", "conclusion": None},
        {"conclusion": "failure"},
        {"conclusion": "cancelled"},
        {"head_sha": "b" * 40},
        {"head_branch": "other"},
        {"event": "workflow_dispatch"},
        {"path": ".github/workflows/other.yml"},
    ],
)
def test_release_requires_successful_ci_for_exact_commit(
    commands, capsys, action, change
):
    original = commands.side_effect

    def ci(args, **kwargs):
        if args[0] == "gh":
            runs = [{**successful_ci("a" * 40), **change}] if change is not None else []
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"workflow_runs": runs})
            )
        return original(args, **kwargs)

    commands.side_effect = ci
    assert release.main([action]) == 1
    assert "Latest main CI must succeed" in capsys.readouterr().err
    assert not any(
        args[:2] in {("git", "tag"), ("git", "push")} for args in invoked(commands)
    )


def test_release_check_does_not_create_or_push_tags(commands, capsys):
    assert release.main(["release-check"]) == 0
    assert "Ready to tag v0.8.4" in capsys.readouterr().out
    assert not any(
        args[:2] in {("git", "tag"), ("git", "push")} for args in invoked(commands)
    )


def test_release_rejects_head_moving_during_preflight(commands, capsys):
    original = commands.side_effect
    checked_ci = False

    def move(args, **kwargs):
        nonlocal checked_ci
        if args[0] == "gh":
            checked_ci = True
        if checked_ci and args == ("git", "rev-parse", "HEAD"):
            return subprocess.CompletedProcess(args, 0, "b" * 40)
        return original(args, **kwargs)

    commands.side_effect = move
    assert release.main(["release-start"]) == 1
    assert "HEAD changed" in capsys.readouterr().err
    assert not any(
        args[:2] in {("git", "tag"), ("git", "push")} for args in invoked(commands)
    )


@pytest.mark.parametrize(
    "repository,ref,valid",
    [
        ("physiclaw/PhysiClaw", "refs/tags/v0.8.4", True),
        ("other/fork", "refs/tags/v0.8.4", False),
        ("physiclaw/PhysiClaw", "refs/tags/v0.8.5", False),
        ("physiclaw/PhysiClaw", "refs/heads/main", False),
    ],
)
def test_ci_validates_tag_identity_before_checking_ancestry(
    commands, monkeypatch, repository, ref, valid
):
    original = commands.side_effect

    def annotated_tag(args, **kwargs):
        if args[:3] == ("git", "rev-parse", "--verify"):
            return subprocess.CompletedProcess(args, 0, "b" * 40 + "\n")
        if args == ("git", "cat-file", "tag", "b" * 40):
            return subprocess.CompletedProcess(
                args, 0, f"object {'a' * 40}\ntype commit\n"
            )
        return original(args, **kwargs)

    commands.side_effect = annotated_tag
    monkeypatch.setenv("GITHUB_REPOSITORY", repository)
    monkeypatch.setenv("GITHUB_REF", ref)
    assert (release.main(["validate-tag"]) == 0) is valid
    assert bool(commands.called) is valid
    if valid:
        assert (
            "git",
            "merge-base",
            "--is-ancestor",
            "a" * 40,
            "FETCH_HEAD",
        ) in invoked(commands)
        assert invoked(commands)[-1] == ("uv", "lock", "--check")


@pytest.fixture
def approval_api(commands, monkeypatch):
    monkeypatch.setenv("RUN_ID", "42")
    monkeypatch.setenv("JSON", "1")
    base = f"repos/{release.REPOSITORY}"
    endpoint = f"{base}/actions/runs/42"
    responses = {
        endpoint: {
            "id": 42,
            "run_attempt": 2,
            "repository": {"full_name": release.REPOSITORY},
            "path": ".github/workflows/release.yml",
            "event": "push",
            "head_branch": "v0.8.4",
            "head_sha": "a" * 40,
            "status": "waiting",
            "conclusion": None,
        },
        f"{endpoint}/pending_deployments": [
            {
                "environment": {"id": 7, "name": "pypi"},
                "current_user_can_approve": True,
            },
            {
                "environment": {"id": 8, "name": "docs"},
                "current_user_can_approve": True,
            },
        ],
        f"{base}/git/ref/tags/v0.8.4": {"object": {"type": "tag", "sha": "b" * 40}},
        f"{base}/git/tags/{'b' * 40}": {"object": {"type": "commit", "sha": "a" * 40}},
        f"{endpoint}/attempts/1/jobs?per_page=100": [{"jobs": []}],
        f"{endpoint}/attempts/2/jobs?per_page=100": [
            {"jobs": []},
            {
                "jobs": [
                    {
                        "name": "prepublish",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
        ],
    }
    original = commands.side_effect

    def respond(args, **kwargs):
        if args[0] != "gh":
            return original(args, **kwargs)
        assert args[:4] == ("gh", "api", "--hostname", "github.com")
        payload = [] if "POST" in args else responses[args[4]]
        return subprocess.CompletedProcess(args, 0, json.dumps(payload))

    commands.side_effect = respond
    return responses


@pytest.fixture
def automatic_run(approval_api, monkeypatch):
    monkeypatch.delenv("RUN_ID")
    query = (
        f"repos/{release.REPOSITORY}/actions/workflows/release.yml/runs"
        f"?event=push&branch=v0.8.4&head_sha={'a' * 40}&per_page=2"
    )
    result = {"total_count": 1, "workflow_runs": [{"id": 42}]}
    approval_api[query] = result
    return result


@pytest.mark.parametrize("action", ["release-status", "release-approve"])
def test_release_finds_run_for_exact_version_and_commit(
    automatic_run, commands, capsys, action
):
    assert release.main([action]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result.get("id", result.get("run_id")) == 42
    assert invoked(commands)[0] == ("git", "rev-parse", "HEAD")
    assert f"branch=v0.8.4&head_sha={'a' * 40}" in invoked(commands)[1][4]


@pytest.mark.parametrize("count", [0, 2])
def test_release_never_guesses_when_run_is_missing_or_ambiguous(
    automatic_run, commands, capsys, count
):
    automatic_run.update(total_count=count, workflow_runs=[{"id": 42}] * count)
    assert release.main(["release-approve"]) == 1
    assert len(invoked(commands)) == 2
    error = capsys.readouterr().err
    assert ("No release run found" if count == 0 else "Multiple matching runs") in error
    assert not any("POST" in args for args in invoked(commands))


@pytest.mark.parametrize("explicit_run", [False, True])
def test_release_status_explains_pending_approval_by_default(
    automatic_run, commands, monkeypatch, capsys, explicit_run
):
    if explicit_run:
        monkeypatch.setenv("RUN_ID", "42")
    monkeypatch.delenv("JSON")
    assert release.main(["release-status"]) == 0
    output = capsys.readouterr().out
    assert "Release: v0.8.4" in output
    assert "Run:     42 (attempt 2)" in output
    assert "Status:  waiting" in output
    selector = " RUN_ID=42" if explicit_run else ""
    assert f"make release-approve{selector} — allows CI to publish to PyPI" in output
    assert not any("POST" in args for args in invoked(commands))


@pytest.mark.parametrize("explicit_run", [False, True])
def test_release_status_preserves_selected_run_when_suggesting_watch(
    automatic_run, approval_api, monkeypatch, capsys, explicit_run
):
    if explicit_run:
        monkeypatch.setenv("RUN_ID", "42")
    monkeypatch.delenv("JSON")
    approval_api[f"repos/{release.REPOSITORY}/actions/runs/42/pending_deployments"] = []
    assert release.main(["release-status"]) == 0
    selector = " RUN_ID=42" if explicit_run else ""
    assert f"Next: make release-watch{selector}\n" in capsys.readouterr().out


@pytest.mark.parametrize("explicit_run", [False, True])
def test_release_approval_explains_that_publication_will_follow(
    automatic_run, monkeypatch, capsys, explicit_run
):
    if explicit_run:
        monkeypatch.setenv("RUN_ID", "42")
    monkeypatch.delenv("JSON")
    assert release.main(["release-approve"]) == 0
    output = capsys.readouterr().out
    assert "Approved v0.8.4 (run 42); CI can now publish to PyPI." in output
    selector = " RUN_ID=42" if explicit_run else ""
    assert f"Next: make release-watch{selector}\n" in output


@pytest.mark.parametrize("exit_code", [0, 1])
def test_release_watch_streams_selected_run_and_propagates_failure(
    automatic_run, commands, exit_code
):
    original = commands.side_effect

    def watch(args, **kwargs):
        if args[:3] == ("gh", "run", "watch"):
            assert kwargs["stdout"] is None
            return subprocess.CompletedProcess(args, exit_code, "")
        return original(args, **kwargs)

    commands.side_effect = watch
    assert release.main(["release-watch"]) == exit_code
    assert invoked(commands)[-1] == (
        "gh",
        "run",
        "watch",
        "42",
        "--repo",
        "github.com/physiclaw/PhysiClaw",
        "--exit-status",
    )
    assert not any(
        "POST" in args or any("pending_deployments" in arg for arg in args)
        for args in invoked(commands)
    )


def test_json_remains_parseable_with_verbose_logging(approval_api, monkeypatch, capsys):
    monkeypatch.setenv("VERBOSE", "1")
    assert release.main(["release-status"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["id"] == 42
    assert "gh api" in output.err


def test_release_status_is_read_only_json(approval_api, commands, capsys):
    assert release.main(["release-status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["id"] == 42
    assert status["head_sha"] == "a" * 40
    assert status["status"] == "waiting"
    assert status["pending_deployments"][0] == {
        "environment": "pypi",
        "can_approve": True,
    }
    assert len(invoked(commands)) == 2
    assert all(args[0] == "gh" and "POST" not in args for args in invoked(commands))


def test_release_approval_targets_only_checked_run_and_pypi_environment(
    approval_api, commands, capsys
):
    assert release.main(["release-approve"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"approved": True, "run_id": 42, "tag": "v0.8.4", "sha": "a" * 40}
    calls = invoked(commands)
    assert calls[-2][-2:] == ("--paginate", "--slurp")
    assert "/attempts/2/jobs?" in calls[-2][4]
    assert calls[-1] == (
        "gh",
        "api",
        "--hostname",
        "github.com",
        f"repos/{release.REPOSITORY}/actions/runs/42/pending_deployments",
        "--method",
        "POST",
        "-F",
        "environment_ids[]=7",
        "-f",
        "state=approved",
        "-f",
        f"comment=Approve v0.8.4 at {'a' * 40} after release checks passed.",
    )
    assert [args for args in calls if args[0] == "git"] == [
        ("git", "rev-parse", "HEAD")
    ]


@pytest.mark.parametrize(
    "action", ["release-status", "release-approve", "release-watch"]
)
@pytest.mark.parametrize("run_id", ["0", "-1", "42/other", "42\n", "42 & echo bad"])
def test_release_run_requires_explicit_numeric_id(
    commands, monkeypatch, action, run_id
):
    monkeypatch.setenv("RUN_ID", run_id)
    assert release.main([action]) == 1
    assert not commands.called


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", {"full_name": "other/fork"}),
        ("path", ".github/workflows/ci.yml"),
        ("event", "workflow_dispatch"),
        ("head_branch", "main"),
        ("head_branch", "v0.9.0"),
        ("head_sha", "c" * 40),
    ],
)
def test_release_approval_rejects_wrong_run(approval_api, commands, field, value):
    approval_api[f"repos/{release.REPOSITORY}/actions/runs/42"][field] = value
    assert release.main(["release-approve"]) == 1
    assert not any("POST" in args for args in invoked(commands))


@pytest.mark.parametrize(
    "case",
    [
        "no-pypi",
        "not-reviewer",
        "duplicate",
        "lightweight",
        "nested",
        "moved",
        "failed",
        "running",
        "missing-check",
    ],
)
def test_release_approval_fails_closed(approval_api, commands, case):
    base = f"repos/{release.REPOSITORY}"
    endpoint = f"{base}/actions/runs/42"
    pending = approval_api[f"{endpoint}/pending_deployments"]
    jobs = approval_api[f"{endpoint}/attempts/2/jobs?per_page=100"][1]["jobs"]
    if case == "no-pypi":
        pending.pop(0)
    elif case == "not-reviewer":
        pending[0]["current_user_can_approve"] = False
    elif case == "duplicate":
        pending.append(pending[0])
    elif case == "lightweight":
        approval_api[f"{base}/git/ref/tags/v0.8.4"]["object"]["type"] = "commit"
    elif case == "nested":
        approval_api[f"{base}/git/tags/{'b' * 40}"]["object"]["type"] = "tag"
    elif case == "moved":
        approval_api[f"{base}/git/tags/{'b' * 40}"]["object"]["sha"] = "c" * 40
    elif case == "failed":
        jobs[0]["conclusion"] = "failure"
    elif case == "running":
        jobs[0]["status"] = "in_progress"
    else:
        jobs.clear()
    assert release.main(["release-approve"]) == 1
    assert not any("POST" in args for args in invoked(commands))


@pytest.mark.parametrize("newer_failed", [False, True])
def test_release_approval_handles_reused_checks_without_ignoring_newer_failures(
    approval_api, commands, newer_failed
):
    endpoint = f"repos/{release.REPOSITORY}/actions/runs/42"
    jobs = approval_api[f"{endpoint}/attempts/2/jobs?per_page=100"][1]["jobs"]
    previous = approval_api[f"{endpoint}/attempts/1/jobs?per_page=100"][0]["jobs"]
    previous.append(jobs[0].copy())
    if newer_failed:
        jobs[0]["conclusion"] = "failure"
    else:
        jobs.clear()
    assert (release.main(["release-approve"]) == 0) is not newer_failed
    assert any("POST" in args for args in invoked(commands)) is not newer_failed


@pytest.mark.parametrize("failure", ["missing-gh", "unauthorized", "invalid-json"])
def test_release_approval_api_errors_cannot_approve(approval_api, commands, failure):
    def fail(args, **kwargs):
        if failure == "missing-gh":
            raise FileNotFoundError("gh")
        if failure == "unauthorized":
            return subprocess.CompletedProcess(args, 1, "HTTP 403")
        return subprocess.CompletedProcess(args, 0, "not JSON")

    commands.side_effect = fail
    assert release.main(["release-approve"]) == 1
    assert not any("POST" in args for args in invoked(commands))


def test_release_approval_denied_by_github_is_not_reported_as_success(
    approval_api, commands, capsys
):
    original = commands.side_effect

    def deny(args, **kwargs):
        if "POST" in args:
            return subprocess.CompletedProcess(args, 1, "HTTP 403: review denied")
        return original(args, **kwargs)

    commands.side_effect = deny
    assert release.main(["release-approve"]) == 1
    result = capsys.readouterr()
    assert '"approved": true' not in result.out
    assert "review denied" in result.err


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
        ("publish", "Working tree dirty"),
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
    assert 'version = "0.8.4"' in (project / "uv.lock").read_text(encoding="utf-8")
    assert not (project / ".venv").exists()


@pytest.fixture
def committed_project(project, monkeypatch):
    # Isolate Git before adding files so CRLF handling stays unchanged throughout.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("UV_CACHE_DIR", str(project / "cache"))
    monkeypatch.setenv("UV_PYTHON", sys.executable)
    monkeypatch.setenv("UV_OFFLINE", "1")
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], check=True)
    for key, value in (
        ("user.name", "Release Test"),
        ("user.email", "release@example.invalid"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(project / "no-hooks")),
    ):
        subprocess.run(["git", "config", "--local", key, value], check=True)
    (project / ".gitignore").write_text("cache/\n", encoding="utf-8")
    # Replace the unit-test stub with a complete lock from the actual toolchain.
    (project / "uv.lock").unlink()
    subprocess.run(
        ["uv", "lock", "--offline"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "add",
            "pyproject.toml",
            "uv.lock",
            ".gitignore",
            "Makefile",
            "scripts",
        ],
        check=True,
    )
    subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], check=True)
    return project


def test_native_make_bump_commits_version_and_lockfile(committed_project):
    project = committed_project

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


def test_native_bump_rejects_incompatible_uv_without_editing(committed_project):
    project = committed_project
    with (project / "pyproject.toml").open("a", encoding="utf-8") as source:
        source.write('\n[tool.uv]\nrequired-version = ">=999.0.0"\n')
    subprocess.run(["git", "add", "pyproject.toml"], check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "require a newer uv"], check=True)
    before = {
        name: (project / name).read_bytes() for name in ("pyproject.toml", "uv.lock")
    }

    result = run_make(project, "bump")

    assert result.returncode != 0
    assert "999.0.0" in result.stdout + result.stderr
    assert {name: (project / name).read_bytes() for name in before} == before
    assert subprocess.check_output(["git", "status", "--porcelain"], text=True) == ""


@pytest.fixture(scope="session")
def fake_gh_bin(tmp_path_factory):
    # Keep native release preflight offline while exercising its real gh process.
    # Only the main-CI endpoint is implemented; unexpected calls fail closed.
    cli = tmp_path_factory.mktemp("release cli")
    source = dedent("""\
            import json
            import subprocess
            import sys

            assert sys.argv[1:4] == ['api', '--hostname', 'github.com']
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
            assert sys.argv[4] == (
                'repos/physiclaw/PhysiClaw/actions/workflows/ci.yml/runs'
                f'?event=push&branch=main&head_sha={head}&per_page=1'
            )
            print(json.dumps({'workflow_runs': [{
                'head_sha': head, 'head_branch': 'main', 'event': 'push',
                'path': '.github/workflows/ci.yml',
                'status': 'completed', 'conclusion': 'success',
            }]}))
            """)
    # Let uv generate a native console launcher, including gh.exe on Windows.
    # A .cmd shim would send the query's ampersands through cmd.exe parsing.
    dist = "release_cli_fixture-0.0.0.dist-info"
    files = {
        "release_cli_fixture.py": "def main():\n" + indent(source, "    "),
        f"{dist}/METADATA": "Metadata-Version: 2.1\nName: release-cli-fixture\nVersion: 0.0.0\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist}/entry_points.txt": "[console_scripts]\ngh = release_cli_fixture:main\n",
    }
    files[f"{dist}/RECORD"] = "".join(
        f"{name},,\n" for name in [*files, f"{dist}/RECORD"]
    )
    wheel = cli / "release_cli_fixture-0.0.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    environment = cli / "venv"
    env = {**os.environ, "UV_CACHE_DIR": str(cli / "cache"), "UV_OFFLINE": "1"}
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(environment)],
        check=True,
        env=env,
    )
    bin_dir = environment / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            "--no-index",
            str(wheel),
        ],
        check=True,
        env=env,
    )
    return bin_dir


@pytest.fixture
def release_remote(committed_project, tmp_path, monkeypatch, fake_gh_bin):
    project = committed_project
    bare = tmp_path / "origin.git"
    transport = tmp_path / "local_ssh.py"
    # Exercise real Git fetch/push with the official SSH URL, but route transport
    # to a local bare repository. No connection to GitHub is made.
    transport.write_text(
        dedent("""\
            import os
            import shlex
            import subprocess
            import sys

            command = shlex.split(sys.argv[-1])
            assert sys.argv[-2] == "git@github.com"
            assert len(command) == 2 and command[1] == "physiclaw/PhysiClaw.git"
            assert command[0] in {"git-upload-pack", "git-receive-pack"}
            if command[0] == "git-receive-pack" and os.environ.get("RELEASE_TEST_REJECT_PUSH"):
                sys.exit(7)
            result = subprocess.run([
                "git", command[0].removeprefix("git-"), sys.argv[1]
            ])
            raise SystemExit(result.returncode)
            """),
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_SSH_VARIANT", "ssh")
    monkeypatch.setenv(
        "GIT_SSH_COMMAND",
        f'"{Path(sys.executable).as_posix()}" "{transport.as_posix()}" "{bare.as_posix()}"',
    )
    monkeypatch.setenv("PATH", str(fake_gh_bin) + os.pathsep + os.environ["PATH"])

    def git(*args):
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=PROCESS_TIMEOUT
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    git("init", "--bare", "--initial-branch=main", str(bare))
    git("remote", "add", "origin", "git@github.com:physiclaw/PhysiClaw.git")
    git("push", "--set-upstream", "origin", "main")
    git("switch", "main")
    git("pull", "--ff-only")
    return project, bare, git


def test_native_make_release_retries_failed_tag_push(release_remote, monkeypatch):
    project, bare, git = release_remote

    bumped = run_make(project, "bump", "VERSION=1.2.3")
    assert bumped.returncode == 0, bumped.stdout + bumped.stderr
    head = git("rev-parse", "HEAD")
    assert release.read_version() == "1.2.3"
    with (project / "uv.lock").open("rb") as source:
        packages = tomllib.load(source)["package"]
    assert next(p for p in packages if p["name"] == "physiclaw")["version"] == "1.2.3"

    # An unpushed version must not leave a release tag behind.
    unpushed = run_make(project, "release")
    assert unpushed.returncode != 0
    assert "HEAD must equal origin/main" in unpushed.stderr
    assert git("tag", "--list") == ""

    git("push", "origin", "main")
    monkeypatch.setenv("RELEASE_TEST_REJECT_PUSH", "1")
    failed = run_make(project, "release-start")
    assert failed.returncode != 0
    assert "Release requested" not in failed.stdout
    tag_object = git("rev-parse", "refs/tags/v1.2.3")
    assert git("cat-file", "-t", tag_object) == "tag"
    assert git("--git-dir", str(bare), "tag", "--list") == ""
    monkeypatch.delenv("RELEASE_TEST_REJECT_PUSH")

    started = run_make(project, "release-start")
    assert started.returncode == 0, started.stdout + started.stderr
    assert "Release requested for physiclaw 1.2.3" in started.stdout
    assert git("--git-dir", str(bare), "rev-parse", "refs/heads/main") == head
    assert git("--git-dir", str(bare), "rev-parse", "v1.2.3^{commit}") == head
    assert git("--git-dir", str(bare), "cat-file", "-t", "v1.2.3") == "tag"
    assert git("rev-parse", "refs/tags/v1.2.3") == tag_object
    assert git("--git-dir", str(bare), "rev-parse", "refs/tags/v1.2.3") == tag_object
    assert git("status", "--porcelain") == ""

    repeated = run_make(project, "release-start")
    assert repeated.returncode != 0
    assert (
        "Remote tag v1.2.3 already exists; inspect or rerun its workflow"
        in repeated.stderr
    )


@pytest.mark.parametrize("tag_kind", ["lightweight", "other-commit", "nested"])
def test_native_make_release_rejects_unsafe_local_tag(release_remote, tag_kind):
    project, bare, git = release_remote
    if tag_kind == "lightweight":
        git("tag", "v0.8.4")
    elif tag_kind == "other-commit":
        git("tag", "-a", "v0.8.4", "-m", "old commit")
        git("commit", "--allow-empty", "-m", "later commit")
        git("push", "origin", "main")
    else:
        git("tag", "-a", "inner", "-m", "inner tag")
        git("tag", "-a", "v0.8.4", "inner", "-m", "nested tag")
    tag_object = git("rev-parse", "refs/tags/v0.8.4")

    result = run_make(project, "release-start")

    assert result.returncode != 0
    assert "must be an annotated tag pointing directly to HEAD" in result.stderr
    assert git("rev-parse", "refs/tags/v0.8.4") == tag_object
    assert git("--git-dir", str(bare), "tag", "--list") == ""


@pytest.mark.parametrize(
    "tag_kind", ["annotated", "lightweight", "other-commit", "nested", "missing"]
)
def test_ci_requires_annotated_tag_at_checked_out_commit(
    release_remote, monkeypatch, tag_kind
):
    project, _, git = release_remote
    bumped = run_make(project, "bump", "VERSION=0.8.5")
    assert bumped.returncode == 0, bumped.stdout + bumped.stderr
    head = git("rev-parse", "HEAD")
    git("push", "origin", "main")
    if tag_kind == "lightweight":
        git("tag", "v0.8.5")
    elif tag_kind == "nested":
        git("tag", "-a", "inner", "-m", "inner tag")
        git("tag", "-a", "v0.8.5", "inner", "-m", "nested tag")
    elif tag_kind != "missing":
        target = "HEAD~1" if tag_kind == "other-commit" else head
        git("tag", "-a", "v0.8.5", target, "-m", "release tag")
    # GitHub checks out the event commit with a detached HEAD.
    git("switch", "--detach", head)
    monkeypatch.setenv("GITHUB_REPOSITORY", release.REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v0.8.5")
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_PYTHON", sys.executable)
    monkeypatch.setenv("UV_CACHE_DIR", str(project / "cache"))

    assert (release.main(["validate-tag"]) == 0) is (tag_kind == "annotated")


def test_release_tags_checked_commit_if_head_moves(release_remote, mocker):
    project, bare, git = release_remote
    bumped = run_make(project, "bump", "VERSION=0.8.5")
    assert bumped.returncode == 0, bumped.stdout + bumped.stderr
    head = git("rev-parse", "HEAD")
    git("push", "origin", "main")
    run = subprocess.run

    def move_head(args, **kwargs):
        if args[:3] == ("git", "tag", "-a"):
            git("commit", "--allow-empty", "-m", "concurrent work")
        return run(args, **kwargs)

    mocker.patch.object(release.subprocess, "run", side_effect=move_head)

    assert release.main(["release-start"]) == 0
    assert git("rev-parse", "HEAD") != head
    assert git("--git-dir", str(bare), "rev-parse", "v0.8.5^{commit}") == head


@pytest.mark.parametrize("multiple", [False, True])
def test_release_rejects_other_push_destination(release_remote, tmp_path, multiple):
    project, bare, git = release_remote
    other = tmp_path / "other.git"
    git("init", "--bare", str(other))
    if multiple:
        git(
            "remote",
            "set-url",
            "--push",
            "origin",
            "git@github.com:physiclaw/PhysiClaw.git",
        )
    git("remote", "set-url", "--add", "--push", "origin", str(other))

    result = run_make(project, "release-start")

    assert result.returncode != 0
    assert "official" in result.stderr
    assert git("tag", "--list") == ""
    assert git("--git-dir", str(bare), "tag", "--list") == ""
    assert git("--git-dir", str(other), "tag", "--list") == ""


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
