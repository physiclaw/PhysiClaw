"""Stage and validate a template pack before replacing its installed files.

Pack files and installation constants commit under one writer lock. A failed
replace restores both; failed restoration keeps the recovery directory. The
two paths cannot be swapped atomically for readers, so this guarantees rollback
on handled failures, not a filesystem-wide snapshot or crash recovery.
"""

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from physiclaw.common import paths, text
from physiclaw.common.placeholders import (
    PLACEHOLDER_VALUES_FILENAME,
    find_placeholders,
    placeholder_values,
    write_placeholder_values,
)
from physiclaw.conductor.load.files import read_template_manifest
from physiclaw.conductor.load.pack import load_pack, scan_playbooks
from physiclaw.conductor.spec.model import PlaybookError, check_name
from physiclaw.conductor.spec.pack import Pack
from physiclaw.port.locks import exclusive_file_lock

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Template:
    """A snapshot of the template's eligible text files, keyed by relative path."""

    app: str
    relative: Path
    files: dict[Path, str]
    metadata: dict

    @property
    def tokens(self) -> list[str]:
        return list(
            dict.fromkeys(
                t for body in self.files.values() for t in find_placeholders(body)
            )
        )

    def destination(self, *, force: bool) -> Path:
        """Check the target before prompting and again immediately before commit."""
        home = paths.playbooks_dir()
        dest = home / self.relative
        if not dest.parent.resolve().is_relative_to(home.resolve()):
            raise PlaybookError(
                f"destination parent resolves outside {home}: {dest.parent}"
            )
        if not force and (dest.exists() or dest.is_symlink()):
            raise PlaybookError(
                f"{dest} already exists — remove it or pass --force to replace it"
            )
        return dest


def read_template(src: Path) -> Template:
    """Resolve `.`/`..` before deriving the destination; never copy outside src."""
    try:
        src = src.resolve(strict=True)
    except RuntimeError as e:
        raise PlaybookError(f"invalid template path: {src}") from e
    if not src.is_dir():
        raise PlaybookError(f"{src} is not a directory")
    check_name(src.name, "pack directory name")
    channel = src.parent.name == paths.CHANNEL_DIRNAME
    app = paths.CHANNEL_DIRNAME if channel else src.name
    relative = Path(paths.CHANNEL_DIRNAME) / src.name if channel else Path(src.name)
    files = _read_files(src)
    if Path(paths.PACK_FILENAME) not in files:
        raise PlaybookError(f"{src} is missing {paths.PACK_FILENAME}")
    return Template(app, relative, files, read_template_manifest(src))


def _read_files(root: Path) -> dict[Path, str]:
    files: dict[Path, str] = {}

    def walk(folder: Path, ancestors: frozenset[Path]) -> None:
        for path in sorted(folder.iterdir()):
            if paths.is_skipped(path.name):
                continue
            directory = path.is_dir()
            if not directory and path.suffix not in {".yml", ".md"}:
                continue
            try:
                resolved = path.resolve(strict=True)
            except RuntimeError as e:
                raise PlaybookError(f"symlink cycle at {path}") from e
            if not resolved.is_relative_to(root):
                raise PlaybookError(f"template path resolves outside source: {path}")
            if directory:
                if resolved in ancestors:
                    raise PlaybookError(f"symlink cycle at {path}")
                walk(path, ancestors | {resolved})
            elif resolved.is_file():
                files[path.relative_to(root)] = text.read_text(path)
            else:
                raise PlaybookError(f"unsupported template file: {path}")

    walk(root, frozenset({root}))
    return files


def install_template(
    template: Template, overrides: dict[str, str], *, force: bool = False
) -> tuple[Path, Pack]:
    """Validate privately, then publish the pack and merged placeholder values.

    Reload constants under the lock: a different pack installed while this
    caller prompted must not lose its values. Existing constants are unchanged
    when the caller supplied no new values.
    """
    home = paths.playbooks_dir()
    with exclusive_file_lock(home / ".install.lock"):
        dest = template.destination(force=force)
        values = {**placeholder_values(), **overrides}
        work = Path(tempfile.mkdtemp(prefix=".install-", dir=home))
        changed: list[tuple[Path, Path, bool]] = []
        preserve = False
        try:
            staged = work / "pack" / template.relative.name
            staged.mkdir(parents=True)
            for relative, body in template.files.items():
                target = staged / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                text.write_text(target, body)
            pack = load_pack(template.app, root=staged, values=values, learned=False)
            errors = pack.file_errors()
            errors.extend(
                (e.name, e.error or "invalid")
                for e in scan_playbooks(template.app, pack)
                if e.spec is None
            )
            if errors:
                raise PlaybookError(
                    "\n".join(
                        f"{template.relative}/{name}: {error}" for name, error in errors
                    )
                )
            replacements = [(staged, dest)]
            if overrides:
                staged_values = work / PLACEHOLDER_VALUES_FILENAME
                write_placeholder_values(values, path=staged_values)
                replacements.append((staged_values, home / PLACEHOLDER_VALUES_FILENAME))
            template.destination(force=force)
            dest.parent.mkdir(parents=True, exist_ok=True)
            for i, (source, target) in enumerate(replacements):
                backup = work / f"backup-{i}"
                existed = target.exists() or target.is_symlink()
                # Journal before either rename so interruption after a completed
                # OS call cannot orphan a backup that cleanup would delete.
                changed.append((target, backup, existed))
                if existed:
                    os.replace(target, backup)
                os.replace(source, target)
            return dest, pack
        except BaseException as error:
            # Keep backups until rollback has actually finished. A second
            # interrupt during restoration must not trigger their cleanup.
            preserve = bool(changed)
            try:
                failures = _restore(changed)
            except BaseException:
                log.error("rollback interrupted; recovery files kept at %s", work)
                raise
            preserve = bool(failures)
            if failures:
                message = f"install failed: {error}; restore failed: {'; '.join(failures)}; recovery files kept at {work}"
                if isinstance(error, Exception):
                    raise PlaybookError(message) from error
                log.error(message)
            raise
        finally:
            if not preserve:
                shutil.rmtree(work, ignore_errors=True)


def _restore(changed: list[tuple[Path, Path, bool]]) -> list[str]:
    failures = []
    for target, backup, existed in reversed(changed):
        try:
            parked = backup.exists() or backup.is_symlink()
            if parked or not existed:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
            if parked:
                os.replace(backup, target)
        except OSError as e:
            failures.append(f"{target}: {e}")
    return failures
