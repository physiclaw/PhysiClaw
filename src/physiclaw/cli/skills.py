"""``physiclaw skills`` — install / list / uninstall community skills.

Convention: a skill source is a git repo with a top-level ``skills/``
directory; ``skills install jd`` looks for ``skills/jd/SKILL.md`` in the
source and copies the whole ``skills/jd/`` tree to
``~/.physiclaw/skills/jd/``.

Source resolution order:
    1. ``--from <url>`` flag (per-call override)
    2. ``CONFIG.skills.default_source`` (set via ``physiclaw config set``)
    3. error with instructions — no implicit fallback

Installed dirs carry a ``.installed-from`` JSON file so ``list`` can
show provenance and ``uninstall`` can distinguish CLI-managed skills
from user-authored ones (the latter need ``--force`` to delete).
"""

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from urllib.request import url2pathname

import typer

from physiclaw.cli._format import exit_error, ok, warn
from physiclaw.common import paths
from physiclaw.common.config import load as _load_config
from physiclaw.common.text import read_text, write_text
from physiclaw.port.locks import FileLockBusy, exclusive_file_lock

skills_app = typer.Typer(
    help="Install, list, and remove skills from a git-repo source.",
    epilog="Example: physiclaw skills install wechat --from physiclaw/PhysiClaw",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
    add_completion=False,
)


PROVENANCE_FILE = ".installed-from"


def installed_skill_dirs() -> list[Path]:
    """Sorted list of installed skill directories under
    ``~/.physiclaw/skills/``. Dot-prefixed entries (including
    in-progress ``.<name>.installing`` staging dirs) are skipped.
    Public so ``doctor`` and future ``status`` commands can enumerate
    installed skills without duplicating the walk."""
    home = paths.skills_dir()
    if not home.exists():
        return []
    return sorted(
        d for d in home.iterdir() if d.is_dir() and not d.name.startswith(".")
    )


def read_provenance(skill_dir: Path) -> dict[str, str] | None:
    """Read a CLI marker; missing, linked, or malformed markers are untrusted."""
    p = skill_dir / PROVENANCE_FILE
    try:
        if skill_dir.is_symlink() or p.is_symlink():
            return None
        data = json.loads(read_text(p))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # Older markers use null for optional fields such as ref and sha.
    data = {key: value for key, value in data.items() if value is not None}
    if (
        not isinstance(data.get("source"), str)
        or not data["source"].strip()
        or any(not isinstance(value, str) for value in data.values())
    ):
        return None
    return data


def _normalize_source(source: str) -> str:
    """Accept several source forms:

    - URLs (``https://…``, ``ssh://…``, ``git@host:…``) — passed through.
    - Local paths (``/…``, ``./…``, ``../…``, ``~/…``) — passed through
      with ``~`` expanded. Explicit prefixes bypass the ``owner/repo``
      rewrite; ``./foo`` would otherwise look like the strict shorthand
      form because ``.`` is a valid char in the shorthand's first segment.
    - ``owner/repo`` shorthand — expanded to
      ``https://github.com/owner/repo.git``. A ``.git`` suffix is kept;
      otherwise added.
    """
    s = source.strip()
    if not s:
        return s
    if "://" in s or s.startswith("git@"):
        return s
    if s.startswith(("/", "./", "../", "~")):
        try:
            return str(Path(s).expanduser())
        except RuntimeError as e:
            raise ValueError(f"invalid local skill source: {s}") from e
    if re.fullmatch(r"[A-Za-z0-9._\-]+/[A-Za-z0-9._\-]+(\.git)?", s):
        if not s.endswith(".git"):
            s += ".git"
        return f"https://github.com/{s}"
    return s


def _resolve_source(from_flag: str | None) -> str:
    """``--from`` wins; config default is the fallback; else error.
    Rejects sources that resolve inside ``paths.HOME`` — skills install
    from an external repo, never from PhysiClaw's own state dir."""
    if from_flag:
        source = _normalize_source(from_flag)
    else:
        cfg = _load_config()
        if not cfg.skills.default_source:
            exit_error(
                "no skill source configured.\n\n"
                "Options:\n"
                "  • Install from a specific repo:\n"
                "      physiclaw skills install <name> --from owner/repo\n"
                "  • Set a permanent default:\n"
                "      physiclaw config set skills.default_source owner/repo\n"
                "  • Repo convention: top-level skills/<name>/SKILL.md."
            )
        source = _normalize_source(cfg.skills.default_source)
    if not source:
        exit_error("skill source must not be empty")
    _reject_physiclaw_home_source(source)
    return source


def _reject_physiclaw_home_source(source: str) -> None:
    """Keep local paths and file:// sources outside PhysiClaw's state directory."""
    if source.startswith("file://"):
        # Git's local file transport ignores the URL hostname.
        source = url2pathname(urlsplit(source).path)
    elif "://" in source or source.startswith("git@"):
        return
    try:
        inside = Path(source).resolve().is_relative_to(paths.HOME.resolve())
    except RuntimeError as e:
        raise ValueError(f"invalid local skill source: {source}") from e
    if not inside:
        return
    exit_error(
        f"--from {source!r} resolves inside {paths.HOME} "
        "(PhysiClaw's own home).\n"
        "Skills install from an external git repo. Pick a path outside "
        "the PhysiClaw home, or use a remote URL."
    )


def _validate_name(name: str) -> None:
    from physiclaw.agent.engine.skill import is_safe_skill_name

    if not is_safe_skill_name(name):
        exit_error(
            f"invalid skill name {name!r}. Allowed: letters, digits, '.', '_', '-'."
        )


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run ``git`` with captured output. Non-zero exit surfaces as Exit(1)
    with stderr echoed — no stack trace for expected failures (bad ref,
    network down, auth prompt). Git-not-installed also short-circuits
    here rather than dumping a FileNotFoundError traceback."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        exit_error(
            "`git` not found on PATH. Install it and retry:\n"
            "  macOS: xcode-select --install  (or `brew install git`)\n"
            "  linux: apt install git  (or your distro's package manager)"
        )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        exit_error(f"git {args[0]} failed: {err}")
    return result


def _clone(source: str, ref: str | None, dest: Path) -> str:
    """Shallow-clone ``source`` into ``dest``. If ``ref`` is given, check
    out that branch or tag. Returns the HEAD sha of the cloned tree."""
    clone_args = ["clone", "--depth=1"]
    if ref:
        clone_args += ["--branch", ref]
    clone_args += ["--", source, str(dest)]
    _git(*clone_args)
    sha = _git("rev-parse", "HEAD", cwd=dest).stdout.strip()
    return sha


def _read_skill_name(skill_md: Path, fallback: str) -> str:
    """Use the runtime's parser so installed names match discovery."""
    from physiclaw.agent.engine.skill import parse_frontmatter

    frontmatter, _ = parse_frontmatter(read_text(skill_md))
    return frontmatter.get("name") or fallback


def _write_provenance(dest: Path, source: str, ref: str | None, sha: str) -> None:
    payload = {
        "source": source,
        "ref": ref or "",
        "sha": sha,
        "installed_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    write_text(dest / PROVENANCE_FILE, json.dumps(payload, indent=2) + "\n")


@contextmanager
def _mutation() -> Iterator[None]:
    """Serialize CLI writers and give expected failures a useful CLI error."""
    try:
        with exclusive_file_lock(paths.skills_dir() / ".install.lock"):
            yield
    except FileLockBusy:
        exit_error("another skills install or uninstall is already running")
    except (OSError, ValueError) as e:
        exit_error(f"cannot update skills: {e}")


def _check_existing(target: Path, force: bool) -> None:
    if force or not (target.exists() or target.is_symlink()):
        return
    prov = read_provenance(target)
    if prov is None:
        exit_error(
            f"{target} exists without a valid {PROVENANCE_FILE} marker. "
            "Refusing to touch user-authored skills. Pass --force to overwrite."
        )
    revision = prov.get("ref") or prov.get("sha", "")[:7] or "?"
    exit_error(
        f"{target.name} is already installed (from {prov['source']} @ {revision}). "
        "Pass --force to reinstall."
    )


def _validate_tree(
    path: Path, root: Path, ancestors: frozenset[Path] = frozenset()
) -> None:
    """Check every dereferenced copy source, including directory symlinks."""
    try:
        resolved = path.resolve(strict=True)
    except RuntimeError as e:
        raise ValueError(f"symlink cycle at {path}") from e
    if not resolved.is_relative_to(root):
        exit_error(f"skill path resolves outside source checkout: {path}")
    if resolved in ancestors:
        exit_error(f"symlink cycle at {path}")
    if resolved.is_dir():
        for child in resolved.iterdir():
            _validate_tree(child, root, ancestors | {resolved})
    elif not resolved.is_file():
        exit_error(f"unsupported skill file: {path}")


def _replace_skill(src: Path, dst: Path) -> None:
    """Replace on the same filesystem; restore the old install on failure.

    Readers may briefly see no target during the swap. If restoration fails,
    leave the backup outside staging and report its path for recovery.
    """
    backup_dir = Path(tempfile.mkdtemp(prefix=f".{dst.name}.backup-", dir=dst.parent))
    backup = backup_dir / "skill"
    parked = preserve = False
    try:
        if dst.exists() or dst.is_symlink():
            os.replace(dst, backup)
            parked = True
        os.replace(src, dst)
    except BaseException:
        if parked:
            try:
                os.replace(backup, dst)
            except OSError as e:
                preserve = True
                typer.echo(
                    warn(f"restore failed: {e}; backup kept at {backup}"), err=True
                )
        raise
    finally:
        if not preserve:
            shutil.rmtree(backup_dir, ignore_errors=True)


@skills_app.command(
    "install",
    no_args_is_help=True,
    epilog="Example: physiclaw skills install wechat --from physiclaw/PhysiClaw",
)
def _install(
    name: Annotated[
        str,
        typer.Argument(
            help="Skill name (matches a skills/<name>/ dir in the source repo)."
        ),
    ],
    from_: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="Git repo to install from. Overrides skills.default_source.",
        ),
    ] = None,
    ref: Annotated[
        str | None,
        typer.Option(
            "--ref",
            help="Branch or tag to check out. Defaults to the repo's default branch.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing install of this skill.",
        ),
    ] = False,
) -> None:
    """Install ``skills/<name>/`` from a Git repo into the user's skills directory."""
    _validate_name(name)
    with _mutation():
        source = _resolve_source(from_)
        home = paths.skills_dir()
        target = home / name
        _check_existing(target, force)
        scratch = Path(tempfile.mkdtemp(prefix=f".{name}.installing-", dir=home))
        try:
            clone_dir = scratch / "repo"
            typer.echo(f"  cloning {source}" + (f" @ {ref}" if ref else "") + " …")
            sha = _clone(source, ref, clone_dir)
            src_skill = clone_dir / "skills" / name
            if not src_skill.is_dir():
                exit_error(f"{source} does not contain skills/{name}/.")
            _validate_tree(src_skill, clone_dir.resolve())
            skill_md = src_skill / "SKILL.md"
            if not skill_md.is_file():
                exit_error(f"skills/{name}/ is missing SKILL.md — not a valid skill.")
            declared_name = _read_skill_name(skill_md, fallback=name)
            if declared_name != name:
                exit_error(
                    f"source skills/{name}/SKILL.md declares name={declared_name!r}, "
                    f"but its directory is {name!r}. Correct the name or rename the "
                    "source directory so runtime discovery and installation agree."
                )
            staging = scratch / "skill"
            shutil.copytree(src_skill, staging, symlinks=False)
            _write_provenance(staging, source, ref, sha)
            # A user may have created a skill manually while Git was cloning.
            _check_existing(target, force)
            _replace_skill(staging, target)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    typer.echo(
        ok(
            f"installed {name} (from {source}"
            + (f" @ {ref}" if ref else "")
            + f", sha {sha[:7]})"
        )
    )
    typer.echo(f"  → {target}")


sync_app = typer.Typer(
    help="Sync curated skill packs published by PhysiClaw.",
    epilog="Example: physiclaw skills sync official",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
    add_completion=False,
)


@sync_app.command(
    "official",
    epilog="Example: physiclaw skills sync official --dry-run",
)
def _sync_official(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Only check whether a sync would run; download nothing.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Re-download and re-extract even if already up to date.",
        ),
    ] = False,
) -> None:
    """Pull the official skill pack from the site into
    ``~/.physiclaw/official/``. Idempotent — a matching commit is a no-op."""
    # Imported lazily so the heavy stdlib bits (zipfile/hashlib) load only
    # when this command actually runs, keeping `physiclaw skills` startup lean.
    from physiclaw.cli import sync_official_skills

    try:
        sync_official_skills.sync(force=force, dry_run=dry_run)
    except sync_official_skills.SyncError as e:
        exit_error(str(e))


skills_app.add_typer(sync_app, name="sync")


@skills_app.command(
    "list",
    epilog="Example: physiclaw skills list",
)
def _list() -> None:
    """List skills installed in ``~/.physiclaw/skills/``."""
    try:
        entries = installed_skill_dirs()
    except OSError as e:
        exit_error(f"cannot list skills: {e}")
    if not entries:
        typer.echo(f"(no skills installed in {paths.skills_dir()})")
        return
    for d in entries:
        prov = read_provenance(d)
        if prov is None:
            typer.echo(f"  {d.name}  (local — no {PROVENANCE_FILE})")
        else:
            ref_or_sha = prov.get("ref") or prov.get("sha", "")[:7] or "?"
            typer.echo(f"  {d.name}  ← {prov.get('source', '?')} @ {ref_or_sha}")


@skills_app.command(
    "uninstall",
    no_args_is_help=True,
    epilog="Example: physiclaw skills uninstall wechat",
)
def _uninstall(
    name: Annotated[
        str,
        typer.Argument(help="Skill name to remove."),
    ],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Remove even if the skill has no provenance marker (user-authored).",
        ),
    ] = False,
) -> None:
    """Remove a skill from ``~/.physiclaw/skills/``. Refuses to delete
    user-authored skills (no ``.installed-from`` marker) unless ``--force``
    is set."""
    _validate_name(name)
    with _mutation():
        target = paths.skills_dir() / name
        if not (target.exists() or target.is_symlink()):
            exit_error(f"{name} is not installed (no {target}).")
        prov = read_provenance(target)
        if prov is None and not force:
            exit_error(
                f"{name} has no valid {PROVENANCE_FILE} marker — looks user-authored. "
                "Pass --force to remove it anyway."
            )
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)
    typer.echo(ok(f"removed {name}"))
    if prov is None:
        typer.echo(warn("no provenance marker — removed user-authored skill."))


__all__ = ["skills_app"]
