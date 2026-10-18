"""The pack door — `playbooks/<app>/` on disk → validated `Playbook`s.

A pack is a folder: `APP.yml`, the MANIFEST (what the app is and what
its routes share — meta, placeholders, landmarks, pages; every section
optional, the file may be empty), one `<name>/PLAYBOOK.yml` folder per
playbook beside it (the folder is the name, referenced as
`<app>/<name>`, and `name:` must agree with it), `macros/<name>.yml`
for the recorded hands routes share, and inside a playbook folder its
own `macros/` and `prompts/`. The manifest never carries a route.

This module loads and scans packs. The model is `spec/model.py`, the
pack it builds `spec/pack.py`; the compiler is `route/`.
"""

import logging
from dataclasses import dataclass, replace
from pathlib import Path

from physiclaw.common import paths
from physiclaw.common.paths import (
    PACK_FILENAME,
    PACK_MACROS_DIRNAME,
    PACK_PROMPTS_DIRNAME,
    PROMPT_SUFFIX,
)
from physiclaw.common.placeholders import placeholder_values, resolve_placeholders
from physiclaw.common.text import read_text
from physiclaw.conductor.load import scaffold
from physiclaw.conductor.load.files import load_pack_doc, load_playbook_docs
from physiclaw.conductor.load.prints import prints_for_app
from physiclaw.conductor.route.playbook import playbook_by_id
from physiclaw.conductor.route.recover import manifest_recovers
from physiclaw.conductor.route.resolve import claim_inline, macro_resolver
from physiclaw.conductor.spec.conventions import CHANNEL_APP, RESERVED_APPS
from physiclaw.conductor.spec.live import live_gap
from physiclaw.conductor.spec.match import page_resolver
from physiclaw.conductor.spec.model import Playbook, PlaybookError, check_name
from physiclaw.conductor.spec.pack import (
    Files,
    Pack,
    Scanned,
    qualified_inline,
    qualified_pack,
)
from physiclaw.conductor.spec.pages import (
    PagePrint,
    PagesError,
    collect_page_recovers,
    pack_landmarks,
    page_sites,
    parse_pages_data,
)
from physiclaw.macros import parse as macro_parse
from physiclaw.macros import store as macro_store
from physiclaw.macros.model import Macro

log = logging.getLogger(__name__)


def qualified_all(app: str, pack: Pack) -> dict[str, Macro]:
    """Everything a walk of this pack can dispatch: the directory macros
    plus every playbook's inline bodies — disabled playbooks included
    (gating is the caller's filter, never the dispatch table)."""
    macros = qualified_pack(app, pack)
    for entry in scan_playbooks(app, pack):
        if entry.spec is not None:
            macros.update(qualified_inline(app, entry.spec))
    return macros


@dataclass(frozen=True)
class PlaybookEntry:
    """One playbook file as found on disk — parsed, or the reason it was
    excluded (the macro ScanEntry shape, named so downstream consumers get
    fields instead of tuple positions)."""

    app: str
    name: str
    spec: "Playbook | None" = None
    error: str | None = None


def load_pack(
    app: str,
    *,
    root: Path | None = None,
    values: dict[str, str] | None = None,
    learned: bool = True,
) -> Pack:
    """The app pack, whole: the manifest (`APP.yml` — what the app
    is and what its routes share: meta, placeholders, landmarks,
    pages), the playbook files beside it (raw, parsed per entry by
    `scan_playbooks`), and the recorded macros. A broken pack macro is
    carried as its error string so the playbook referencing it fails
    with the cause; a broken playbook file rides the same way.

    Installers supply a staged `root` and placeholder `values`, with
    `learned=False`, to validate the template without changing the user's
    installed files or borrowing the active channel's calibration.
    """
    root = paths.pack_root(app) if root is None else root
    if values is None:
        try:
            values = placeholder_values()  # one snapshot for every pack file
        except ValueError as e:
            raise PlaybookError(str(e)) from e
    doc = load_pack_doc(app, PlaybookError, root=root, values=values)
    if doc is None:
        raise PlaybookError(paths.pack_gap(app))
    if app == CHANNEL_APP:
        # The boot file is a template the user owns, materialized beside
        # an existing channel pack on first look (the ios pack's
        # pattern): a channel recorded before the boot was a file keeps
        # its wake, and every door — wake, step, run, check — sees the
        # same pack.
        scaffold.ensure_channel_boot(root)
    docs, pb_errors = load_playbook_docs(app, PlaybookError, root, values)
    try:
        # Appendix + every route's own declarations across every
        # playbook file, ONE walk — the matcher and every playbook
        # validate against the same set, and the same walk says whose
        # a route-declared page is.
        sites = page_sites(doc, docs)
        pages = parse_pages_data(
            {n: spec for n, (spec, _) in sites.items()},
            app,
            {
                n: PACK_FILENAME if pb is None else paths.playbook_file(pb)
                for n, (_, pb) in sites.items()
            },
            PACK_FILENAME,
        )
    except PagesError as e:
        raise PlaybookError(f"{app}/{e}") from e
    try:
        landmarks = pack_landmarks(doc)
    except PagesError as e:
        raise PlaybookError(f"{app}/{PACK_FILENAME}: {e}") from e
    # One scanner per leaf kind, run on the pack's folders and on each
    # playbook's: traversal guard, skip convention, and the broad-except
    # lesson live in `store.scan` and `paths.leaf_files`. A macro's jump
    # reads a page of THIS pack, through the one resolver over the one
    # candidate set the walk will match against.
    prints = tuple(
        prints_for_app(app, decls=pages)
        if learned
        else [PagePrint(app=app, decl=decl) for decl in pages.values()]
    )
    page_of = page_resolver(app, pages, prints)
    macros = _scan_macros(root / PACK_MACROS_DIRNAME, page_of, values=values)
    shared = macro_resolver(macros.ok, macros.errors)
    pack = Pack(
        app=app,
        pages=pages,
        macros=macros.ok,
        prints=prints,
        macro_errors=macros.errors,
        playbook_docs=docs,
        playbook_errors=pb_errors,
        prompts=_scan_prompts(root / PACK_PROMPTS_DIRNAME, values),
        local={
            name: Files(
                macros=_scan_macros(
                    root / name / PACK_MACROS_DIRNAME, page_of, shared, values=values
                ),
                prompts=_scan_prompts(root / name / PACK_PROMPTS_DIRNAME, values),
            )
            for name in {paths.entry_of(n) for n in docs}  # an entry's serve its own
        },
        landmarks=landmarks,
        route_pages={n: pb for n, (_, pb) in sites.items() if pb is not None},
    )
    # The manifest's hands, resolved once here with the pack's own
    # resolver: a broken one is the pack's load error, not every
    # route's.
    return replace(pack, recovers=manifest_recovers(pack, collect_page_recovers(doc)))


def _scan_macros(
    root: Path,
    pages: macro_parse.PageResolver,
    fallback: macro_parse.MacroResolver | None = None,
    *,
    values: dict[str, str] | None = None,
) -> Scanned[Macro]:
    """The macro files under one `macros/` root, folded from `store.scan`."""
    out: Scanned[Macro] = Scanned()
    for entry in macro_store.scan(root, pages, fallback, values=values):
        if entry.spec is not None:
            out.ok[entry.name] = entry.spec
        else:
            out.errors[entry.name] = entry.error or "invalid"
    return out


def _scan_prompts(root: Path, values: dict[str, str]) -> Scanned[str]:
    """The prompt files under one `prompts/` root — `<name>.md`, the
    whole file verbatim as the model's prose: placeholders filled,
    trailing whitespace trimmed. An empty file, an unfillable token, or
    a name the grammar refuses rides as its error, so the route naming
    it fails with the cause. Nothing else in the folder is read."""
    out: Scanned[str] = Scanned()
    for path in paths.leaf_files(root, PROMPT_SUFFIX):
        try:
            check_name(path.stem, "prompt file name")
            text = resolve_placeholders(read_text(path), PlaybookError, values).rstrip()
            if not text:
                raise PlaybookError("the prompt file is empty")
            out.ok[path.stem] = text
        except Exception as e:  # broad: exclude the file, never the pack
            out.errors[path.stem] = str(e) or type(e).__name__
    return out


def macros_root(app: str, playbook: str | None = None) -> Path:
    """Where a recorded hand is written: the pack's `macros/`, or a
    playbook's own — the layout rule spelled once for every door that
    scaffolds into a pack (`macros init --app`). Raises PlaybookError
    when the pack or the playbook is not on disk."""
    root = paths.pack_root(app)
    if not (root / PACK_FILENAME).exists():
        raise PlaybookError(paths.pack_gap(app))
    if playbook is not None:
        playbook = paths.entry_of(playbook)  # the entry's hands are its own too
        if not (root / playbook / paths.PLAYBOOK_FILENAME).is_file():
            raise PlaybookError(
                f"no playbook {app}/{playbook} on disk ({root / playbook})"
            )
        root = root / playbook
    return root / PACK_MACROS_DIRNAME


def scan_playbooks(app: str, pack: Pack | None = None) -> list[PlaybookEntry]:
    """Every playbook file of the pack, parsed against it — plus the
    files that would not load, as invalid entries. Callers that already
    hold the Pack thread it through so the spec file and macros are
    not re-read."""
    if pack is None:
        if not (paths.pack_root(app) / PACK_FILENAME).exists():
            return []
        pack = load_pack(app)
    out: list[PlaybookEntry] = [
        PlaybookEntry(app=app, name=n, error=e)
        for n, e in sorted(pack.playbook_errors.items())
    ]
    parsed: dict[str, Playbook] = {}
    # An inline body is named `<playbook id>.<move>[.<role>]`, and a
    # playbook an entry runs carries a dot in its id — so two FILES of
    # one pack can reach the same dispatch name (an entry's page `leg`
    # with a `recover:` body, and `leg.yml`'s own move `recover`). The
    # walk's table is one namespace: refuse the second file by name
    # rather than let it shadow the first silently.
    claimed: dict[str, str] = {}
    for name in pack.playbook_docs:
        name = str(name)
        try:
            spec = playbook_by_id(name, pack, parsed)
            claim_inline(spec, pack, claimed)
            out.append(PlaybookEntry(app=app, name=name, spec=spec))
        except Exception as e:  # broad: exclude whole, never take a session down
            out.append(
                PlaybookEntry(app=app, name=name, error=str(e) or type(e).__name__)
            )
    return out


def stray_dirs() -> list[str]:
    """Folders under a playbooks root holding YAML beside no manifest —
    an author who wrote a pack and forgot `APP.yml`. Skips the `_` and
    `.` prefixes every lister does, and a name a home pack already
    claims (the home layer shadows the tree's)."""
    packs = set(list_apps())
    out: list[str] = []
    for root in paths.playbooks_dirs():
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if (
                d.is_dir()
                and not paths.is_skipped(d.name)
                and d.name not in packs
                and any(d.rglob("*.yml"))
            ):
                out.append(f"{root.name}/{d.name}")
    return out


def list_apps() -> list[str]:
    """Packs across the search path (the `paths.playbooks_dirs` layering),
    sorted — an APP.yml marks a pack. The channel is listed whenever a
    `channel/` dir exists at all, so `check` can report why it does
    not resolve."""
    names = paths.marked_subdirs(paths.playbooks_dirs(), PACK_FILENAME)
    if any((d / CHANNEL_APP).is_dir() for d in paths.playbooks_dirs()):
        names.add(CHANNEL_APP)
    return sorted(names)


@dataclass(frozen=True)
class Discovery:
    """Every pack on disk, read once at wake.

    `entries`: ref → (spec, pack) for the live playbooks only — what the
    boot may offer. `macros`: the whole dispatch table, disabled
    playbooks included (gating is the entries filter, never the table).
    `roster`: one line per playbook (and per unusable pack) with its
    state — `app/name (live)`, `(disabled)`, `(disabled macro: x)`,
    `(invalid: …)` — the wake log's answer to "why no playbook?"."""

    entries: dict[str, tuple[Playbook, Pack]]
    macros: dict[str, Macro]
    roster: list[str]


def playbook_gap(entry: PlaybookEntry, pack: Pack) -> str | None:
    """Why the boot cannot offer this playbook — None when it can: the
    file did not parse, or `spec.live.live_gap` names the readiness gap."""
    if entry.spec is None:
        return f"invalid: {entry.error or 'unreadable'}"
    return live_gap(entry.spec, pack)


def discover() -> Discovery:
    """Every pack on disk, once — see `Discovery`. Fail-open per pack: a
    pack that will not load is a roster line, never a failed wake."""
    entries: dict[str, tuple[Playbook, Pack]] = {}
    macros: dict[str, Macro] = {}
    roster: list[str] = []
    for app in list_apps():
        if app in RESERVED_APPS:
            # Infrastructure namespaces, not task packs: `channel` is the
            # conductor's own hands, and a user override of a built-in
            # (`ios`) is page declarations only — neither holds app
            # playbooks.
            continue
        try:
            pack = load_pack(app)
        except Exception as e:
            log.warning(
                "pack %s unusable at wake (%s) — skipped",
                app,
                e,
                exc_info=True,
                extra={
                    "console_message": f"Playbooks for {app} could not load — skipping this pack"
                },
            )
            roster.append(f"{app} (pack unusable: {e})")
            continue
        # One scan per pack: the dispatch table (every playbook's inline
        # bodies, disabled ones included) and the roster off the same
        # entries.
        macros.update(qualified_pack(app, pack))
        for entry in scan_playbooks(app, pack):
            ref = f"{app}/{entry.name}"
            gap = playbook_gap(entry, pack)
            roster.append(f"{ref} ({gap or 'live'})")
            if entry.spec is None:
                continue
            macros.update(qualified_inline(app, entry.spec))
            if gap is None:
                entries[ref] = (entry.spec, pack)
    return Discovery(entries=entries, macros=macros, roster=roster)


def load_spec(app: str, name: str) -> tuple[Playbook, Pack]:
    """The parsed playbook and its pack, by ref. Whether it is LIVE is the
    caller's question (`spec.live.require_live`): a resuming suspension
    must ask it, a rehearsal deliberately does not (you rehearse BEFORE
    you enable)."""
    loaded = load_pack(app)
    entry = next((e for e in scan_playbooks(app, loaded) if e.name == name), None)
    if entry is None:
        raise PlaybookError(f"no playbook {app}/{name} on disk")
    if entry.spec is None:
        raise PlaybookError(f"{app}/{name} is invalid: {entry.error}")
    return entry.spec, loaded
