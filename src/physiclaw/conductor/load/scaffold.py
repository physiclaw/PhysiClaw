"""Writing packs to disk: the `init` scaffold, the channel and ios packs
the conductor needs beside a user's, and the format README kept
current at ``playbooks/README.md`` via ``ensure_readme``. The texts
themselves are `stubs.py`; the scaffold IS the format reference a new
user edits in place (the macro-scaffold doctrine)."""

import logging
from pathlib import Path

from physiclaw.common import paths
from physiclaw.common.logger import ensure_readme
from physiclaw.common.paths import (
    PACK_FILENAME,
    PACK_MACROS_DIRNAME,
    PACK_SCHEMA,
    PLAYBOOK_FILENAME,
)
from physiclaw.common.text import write_text
from physiclaw.conductor.load.files import pack_path
from physiclaw.conductor.load.stubs import (
    CHANNEL_OPEN_STUB,
    CHANNEL_PACK_STUB,
    CHANNEL_SEND_STUB,
    EXAMPLE_MACRO,
    EXAMPLE_PLAYBOOK,
    IOS_PACK_STUB,
    README_CONTENT,
    channel_boot_stub,
    render_example_macro,
    render_manifest_stub,
    render_pack_readme,
    render_playbook_readme,
    render_playbook_stub,
)
from physiclaw.conductor.spec.conventions import (
    BOOT_PLAYBOOK,
    CHANNEL_APP,
    IOS_APP,
    OPEN_MACRO,
    SEND_MACRO,
    THREAD_PAGE,
)
from physiclaw.conductor.spec.model import (
    PlaybookError,
    check_name,
)
from physiclaw.macros import store as macro_store

log = logging.getLogger(__name__)


def _init_channel_pack(root: Path) -> Path:
    """`playbooks/channel/<im>/` — the infrastructure pack: the thread
    page and its reading box, the send/open macros the conductor's asks
    run, and the boot playbook. App packs never name it."""

    macros_root = root / PACK_MACROS_DIRNAME
    macros_root.mkdir(parents=True)
    for name, stub in (
        (SEND_MACRO, CHANNEL_SEND_STUB),
        (OPEN_MACRO, CHANNEL_OPEN_STUB),
    ):
        write_text(macro_store.macro_path(macros_root, name), stub)
    write_text(
        root / PACK_FILENAME,
        CHANNEL_PACK_STUB.format(
            im=root.name,
            boot=BOOT_PLAYBOOK,
            thread=THREAD_PAGE,
            schema=PACK_SCHEMA,
        ),
    )
    write_text(root / "README.md", render_pack_readme(CHANNEL_APP))
    (root / BOOT_PLAYBOOK).mkdir()
    write_text(root / BOOT_PLAYBOOK / PLAYBOOK_FILENAME, channel_boot_stub(root))
    ensure_format_readme()
    return root


def ensure_channel_boot(root: Path) -> None:
    """Materialize `boot/PLAYBOOK.yml` in the channel pack at `root` when
    it has none — the `ensure_ios_pack` pattern, called as the pack loads: the
    boot is a template the user owns and edits (its hands, its limits),
    so it lives on disk, but a channel pack recorded before the boot
    was a file must not lose its wake. Written once, then never
    touched. Fail-open."""
    _ensure_file(
        pack_path(root, root / BOOT_PLAYBOOK / PLAYBOOK_FILENAME, PlaybookError),
        channel_boot_stub(root),
        "no boot at wake",
    )


def ensure_format_readme() -> None:
    """Keep ``playbooks/README.md`` current — the macro-store pattern:
    rewritten whenever the shipped constant changed, fail-open, called
    from every user-facing CLI moment."""

    ensure_readme(paths.playbooks_dir(), README_CONTENT)


def init_pack(app: str) -> Path:
    """Scaffold ``playbooks/<app>/`` — the manifest, its README, a
    disabled example playbook folder (route, README), an example pack
    macro — and return the pack root. Raises
    PlaybookError on a bad name or an existing directory; `ios` is the
    exception, being idempotent (see below)."""

    # A pack's address is its path under playbooks/: one part for an
    # app, `channel/<im>` for the channel (an IM folder under channel/).
    head, sep, im = app.partition("/")
    if head == CHANNEL_APP and not sep:
        raise PlaybookError(
            f"name the IM app: physiclaw playbooks init {CHANNEL_APP}/<im>"
        )
    if sep and (head != CHANNEL_APP or not im or "/" in im):
        raise PlaybookError(f"{app!r} is not an app name or {CHANNEL_APP}/<im>")
    check_name(im if sep else app, "IM app name" if sep else "app name")
    root = paths.playbooks_dir() / app
    if app == IOS_APP:
        # Idempotent on purpose: `session_setup` materializes this pack on
        # the first qualifying wake, so by the time anyone runs
        # `playbooks init ios` it usually exists — and erroring there would
        # withhold the next-steps text, which is the command's whole value.
        ensure_ios_pack()
        ensure_format_readme()
        return root
    if root.exists():
        raise PlaybookError(f"pack directory already exists: {root}")
    if root.parent.name == CHANNEL_APP:
        return _init_channel_pack(root)
    (root / PACK_MACROS_DIRNAME).mkdir(parents=True)
    (root / EXAMPLE_PLAYBOOK).mkdir()
    write_text(root / PACK_FILENAME, render_manifest_stub(app))
    write_text(root / "README.md", render_pack_readme(app))
    write_text(root / EXAMPLE_PLAYBOOK / PLAYBOOK_FILENAME, render_playbook_stub(app))
    write_text(
        root / EXAMPLE_PLAYBOOK / "README.md",
        render_playbook_readme(app, EXAMPLE_PLAYBOOK),
    )
    write_text(
        macro_store.macro_path(root / PACK_MACROS_DIRNAME, EXAMPLE_MACRO),
        render_example_macro(),
    )
    ensure_format_readme()
    return root


# ---------- the ios pack (OS states the conductor must name) ----------


def ensure_ios_pack() -> None:
    """Materialize `playbooks/ios/` if it does not exist — the
    `ensure_format_readme` pattern, called the first time the conductor
    goes looking for OS pages.

    It is a template the user OWNS and edits (the lock-screen reading
    depends on their phone's language), so it must exist on disk rather
    than be read out of the wheel — but it must not depend on them having
    run `playbooks init ios` either, because the conductor needs those
    pages on the very first wake. Written once, then never touched:
    a user file, including a deliberately emptied one, is theirs.

    Fail-open: a read-only home degrades to "no ios pages", which the
    conductor already handles (every OS state reads as unknown)."""

    root = paths.playbooks_dir() / IOS_APP
    # Keyed on the FILE, not the dir: an existing APP.yml — including a
    # deliberately emptied one — is theirs.
    _ensure_file(root / PACK_FILENAME, IOS_PACK_STUB, "OS pages unavailable")


def _ensure_file(path: Path, stub: str, cost: str) -> None:
    """Write `stub` at `path` unless a file is there — a user-owned
    template materialized once, then never touched. Fail-open: an
    unwritable home logs `cost` and the conductor runs without it."""

    if path.exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, stub)
    except OSError:
        log.warning("could not write %s — %s", path, cost, exc_info=True)
