"""Portable package release workflow; invoked by make or directly with Python."""

import argparse
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path


class ReleaseError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def read_version() -> str:
    with Path("pyproject.toml").open("rb") as source:
        version = tomllib.load(source).get("project", {}).get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9a-zA-Z.+!_-]+", version):
        raise ReleaseError("Couldn't read version from pyproject.toml")
    return version


def artifact_paths(version: str) -> tuple[Path, Path]:
    return (
        Path(f"dist/physiclaw-{version}-py3-none-any.whl"),
        Path(f"dist/physiclaw-{version}.tar.gz"),
    )


class Release:
    def __init__(self, *, flow: bool = False):
        self.flow = flow or bool(os.environ.get("RELEASE_FLOW"))
        self.verbose = os.environ.get("VERBOSE") == "1"
        self.color = (
            sys.stdout.isatty()
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
        )
        self.started = time.monotonic()

    def style(self, text: str, code: int) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def stage(self, number: int, text: str) -> None:
        self.started = time.monotonic()
        prefix = f"[{number}/4] " if self.flow else ""
        print(f"\n  {self.style(prefix + text, 36)}", flush=True)

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

    def bump(self) -> None:
        if self.run("git", "status", "--porcelain", capture=True).stdout.strip():
            raise ReleaseError("Working tree dirty - commit or stash first")
        branch = self.run("git", "rev-parse", "--abbrev-ref", "HEAD", capture=True)
        if branch.stdout.strip() != "main":
            raise ReleaseError("Not on main branch")
        current = read_version()
        new = os.environ.get("VERSION")
        if not new:
            prefix, separator, last = current.rpartition(".")
            if not re.fullmatch(r"[0-9]+", last):
                raise ReleaseError(
                    f"Can't auto-bump '{current}'; use make bump VERSION=X.Y.Z"
                )
            new = f"{prefix}{separator}{int(last) + 1}"
        if not re.fullmatch(r"[0-9a-zA-Z.+!_-]+", new):
            raise ReleaseError(f"Invalid version: {new}")
        self.check_tag(new)
        self.stage(1, f"Version {current} -> {new}")
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
        if not self.flow:
            print("\n  Next: make build && make publish\n")

    def build(self) -> None:
        current = read_version()
        artifacts = artifact_paths(current)
        self.stage(2, f"Build packages - {current}")
        for path in artifacts:
            path.unlink(missing_ok=True)
        self.run("uv", "build")
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
        if not self.flow:
            print("\n  Next: make publish\n")

    def publish(self) -> None:
        current = read_version()
        artifacts = artifact_paths(current)
        if not os.environ.get("UV_PUBLISH_TOKEN"):
            raise ReleaseError(
                "UV_PUBLISH_TOKEN not set. Create a token at "
                "https://pypi.org/manage/account/token/ and set UV_PUBLISH_TOKEN"
            )
        for path in artifacts:
            if not path.is_file():
                raise ReleaseError(
                    f"{path.as_posix()} not found - run make build first"
                )
        self.check_tag(current)
        self.stage(3, f"Publish to PyPI - {current}")
        self.run("uv", "publish", *(path.as_posix() for path in artifacts))
        self.done("Uploaded 2 files to PyPI")
        self.stage(4, "Push to GitHub")
        self.run("git", "tag", "-a", f"v{current}", "-m", f"physiclaw v{current}")
        self.run("git", "push", "origin", "main")
        self.run("git", "push", "origin", f"v{current}")
        self.done(f"Pushed main and tag v{current}")
        if not self.flow:
            self.summary()

    def summary(self) -> None:
        current = read_version()
        print(f"\n  {self.style(f'OK Released physiclaw {current}', 32)}\n")
        print(f"  PyPI     https://pypi.org/project/physiclaw/{current}/")
        print(f"  Git tag  https://github.com/physiclaw/PhysiClaw/tree/v{current}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("bump", "build", "publish", "release"))
    args = parser.parse_args(argv)
    release = Release(flow=args.action == "release")
    try:
        if args.action == "release":
            print(f"\n  {release.style('PhysiClaw - release', 1)}", flush=True)
            release.bump()
            release.build()
            release.publish()
            release.summary()
        else:
            getattr(release, args.action)()
    except (ReleaseError, OSError, ValueError) as exc:
        print(f"\n  ERROR {exc}", file=sys.stderr)
        return exc.code if isinstance(exc, ReleaseError) else 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
