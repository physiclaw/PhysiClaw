"""Prepare a version change or tag a merged release; CI owns PyPI uploads."""

import argparse
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

VERSION_PATTERN = (
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:(a|b|rc)(0|[1-9][0-9]*))?"
)
REPOSITORY = "physiclaw/PhysiClaw"


def version_key(version: str) -> tuple[int, ...]:
    """Our public version policy: X.Y.Z, optionally followed by aN/bN/rcN."""
    match = re.fullmatch(VERSION_PATTERN, version)
    if match is None:
        raise ReleaseError(f"Invalid version: {version}; use X.Y.Z or X.Y.ZrcN")
    major, minor, patch, stage, number = match.groups()
    return (
        int(major),
        int(minor),
        int(patch),
        {"a": 0, "b": 1, "rc": 2, None: 3}[stage],
        int(number or 0),
    )


class ReleaseError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def read_version() -> str:
    with Path("pyproject.toml").open("rb") as source:
        version = tomllib.load(source).get("project", {}).get("version")
    if not isinstance(version, str):
        raise ReleaseError("Couldn't read version from pyproject.toml")
    version_key(version)
    return version


def artifact_paths(version: str) -> tuple[Path, Path]:
    return (
        Path(f"dist/physiclaw-{version}-py3-none-any.whl"),
        Path(f"dist/physiclaw-{version}.tar.gz"),
    )


class Release:
    def __init__(self):
        self.verbose = os.environ.get("VERBOSE") == "1"
        self.color = (
            sys.stdout.isatty()
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
        )
        self.started = time.monotonic()

    def style(self, text: str, code: int) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def stage(self, text: str) -> None:
        self.started = time.monotonic()
        print(f"\n  {self.style(text, 36)}", flush=True)

    def done(self, text: str) -> None:
        elapsed = int(time.monotonic() - self.started)
        print(f"        {self.style('OK', 32)} {text} ({elapsed}s)", flush=True)

    def run(self, *command: str, capture: bool = False, check: bool = True):
        """Pass argument lists directly; show successful logs only with VERBOSE=1."""
        if self.verbose:
            print(f"        $ {' '.join(command)}", flush=True)
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE if capture or not self.verbose else None,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and result.returncode:
            if result.stdout:
                print(result.stdout, end="", file=sys.stderr)
            raise ReleaseError(
                f"Failed (exit {result.returncode}): {' '.join(command)}",
                result.returncode,
            )
        return result

    def check_tag(self, version: str) -> None:
        result = self.run(
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/tags/v{version}",
            capture=True,
            check=False,
        )
        if result.returncode == 0:
            raise ReleaseError(f"Tag v{version} already exists")
        if result.returncode != 1:
            if result.stdout:
                print(result.stdout, end="", file=sys.stderr)
            raise ReleaseError("Couldn't check release tag", result.returncode)

    def check_checkout(self, *, main: bool = False) -> None:
        if self.run("git", "status", "--porcelain", capture=True).stdout.strip():
            raise ReleaseError("Working tree dirty - commit or stash first")
        branch = self.run("git", "rev-parse", "--abbrev-ref", "HEAD", capture=True)
        if branch.stdout.strip() == "HEAD":
            raise ReleaseError("Detached HEAD - switch to a branch first")
        if main and branch.stdout.strip() != "main":
            raise ReleaseError("Not on main branch")

    def bump(self) -> None:
        self.check_checkout()
        current = read_version()
        new = os.environ.get("VERSION")
        if not new:
            prefix, separator, last = current.rpartition(".")
            if not re.fullmatch(r"[0-9]+", last):
                raise ReleaseError(
                    f"Can't auto-bump '{current}'; use make bump VERSION=X.Y.Z"
                )
            new = f"{prefix}{separator}{int(last) + 1}"
        if version_key(new) <= version_key(current):
            raise ReleaseError(f"Version must increase: {current} -> {new}")
        self.check_tag(new)
        self.stage(f"Version {current} -> {new}")
        path = Path("pyproject.toml")
        with path.open(encoding="utf-8", newline="") as source:
            text = source.read()
        # Edit only [project].version, retaining comments and line endings.
        project = re.search(
            r"(?ms)^\[project\][^\S\r\n]*(?:#[^\r\n]*)?\r?\n(.*?)(?=^\[|\Z)", text
        )
        version = (
            re.search(
                r"""(?m)^[ \t]*version[ \t]*=[ \t]*(["'])([^"'\r\n]*)\1""", project[1]
            )
            if project
            else None
        )
        if version is None or version[2] != current:
            raise ReleaseError("Couldn't update [project].version in pyproject.toml")
        start = project.start(1) + version.start(2)
        end = project.start(1) + version.end(2)
        updated = text[:start] + new + text[end:]
        expected = tomllib.loads(text)
        expected["project"]["version"] = new
        # A regex can also match examples inside TOML multiline strings. Validate
        # the semantic change before writing or running any release commands.
        if tomllib.loads(updated) != expected:
            raise ReleaseError("Couldn't update [project].version in pyproject.toml")
        with path.open("w", encoding="utf-8", newline="") as target:
            target.write(updated)
        self.run("uv", "lock")
        self.run("git", "add", "pyproject.toml", "uv.lock")
        self.run("git", "commit", "-m", f"chore: bump version to {new}")
        self.done("Lockfile updated; version committed locally")
        print(
            "\n  Next: review the version change in a PR and merge it; then make release on main.\n"
        )

    def build(self) -> None:
        current = read_version()
        artifacts = artifact_paths(current)
        self.stage(f"Build packages - {current}")
        for path in artifacts:
            path.unlink(missing_ok=True)
        self.run("uv", "build", "--no-sources")
        for path in artifacts:
            if not path.is_file():
                raise ReleaseError(f"Expected artifact missing: {path.as_posix()}")
            size = path.stat().st_size
            label = (
                f"{size / 1048576:.1f} MiB"
                if size >= 1048576
                else f"{size / 1024:.1f} KiB"
            )
            print(f"        {path.as_posix()}  {self.style(label, 2)}")
        self.done("Built wheel + source archive")
        print(
            "\n  Local build only. CI builds and tests the distributions uploaded to PyPI.\n"
        )

    def publish(self) -> None:
        if os.environ.get("VERSION"):
            raise ReleaseError(
                "Release does not bump versions; use make bump VERSION=X.Y.Z in a PR first"
            )
        current = read_version()
        self.check_checkout(main=True)
        self.check_tag(current)
        remote = self.run(
            "git", "remote", "get-url", "origin", capture=True
        ).stdout.strip()
        if remote.removesuffix(".git") not in {
            f"https://github.com/{REPOSITORY}",
            f"git@github.com:{REPOSITORY}",
        }:
            raise ReleaseError(f"origin must be the official {REPOSITORY} repository")
        self.run("git", "fetch", "--no-tags", "origin", "main")
        head = self.run("git", "rev-parse", "HEAD", capture=True).stdout.strip()
        upstream = self.run(
            "git", "rev-parse", "FETCH_HEAD", capture=True
        ).stdout.strip()
        if head != upstream:
            raise ReleaseError(
                "HEAD must equal origin/main; merge the release PR and update main first"
            )
        tag = f"refs/tags/v{current}"
        remote_tag = self.run(
            "git",
            "ls-remote",
            "--exit-code",
            "--tags",
            "origin",
            tag,
            capture=True,
            check=False,
        )
        if remote_tag.returncode == 0:
            raise ReleaseError(
                f"Remote tag v{current} already exists; inspect or rerun its workflow"
            )
        if remote_tag.returncode != 2:
            raise ReleaseError(
                "Couldn't check remote release tag", remote_tag.returncode
            )
        self.stage(f"Request CI release - {current}")
        self.run("git", "tag", "-a", f"v{current}", "-m", f"physiclaw v{current}")
        self.run("git", "push", "origin", tag)
        self.done(f"Pushed v{current}; CI must pass and publication must be approved")
        self.summary()

    def validate_tag(self) -> None:
        """Release CI checks the immutable tag before granting publishing access."""
        current = read_version()
        if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
            raise ReleaseError("Releases must run in the official repository")
        if os.environ.get("GITHUB_REF") != f"refs/tags/v{current}":
            raise ReleaseError("Release tag must match pyproject.toml version")
        self.run("git", "fetch", "--no-tags", "origin", "main")
        self.run("git", "merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD")
        self.run("uv", "lock", "--check")

    def summary(self) -> None:
        current = read_version()
        print(f"\n  {self.style(f'Release requested for physiclaw {current}', 32)}\n")
        print(
            f"  Workflow https://github.com/{REPOSITORY}/actions/workflows/release.yml"
        )
        print(f"  Git tag  https://github.com/physiclaw/PhysiClaw/tree/v{current}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("bump", "build", "publish", "release", "validate-tag")
    )
    args = parser.parse_args(argv)
    release = Release()
    try:
        action = (
            "publish" if args.action == "release" else args.action.replace("-", "_")
        )
        getattr(release, action)()
    except (ReleaseError, OSError, ValueError) as exc:
        print(f"\n  ERROR {exc}", file=sys.stderr)
        return exc.code if isinstance(exc, ReleaseError) else 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
