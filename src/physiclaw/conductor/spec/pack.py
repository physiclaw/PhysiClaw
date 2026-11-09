"""The pack — what a playbook validates against: the app's declared
pages, its macros, its landmarks, its prose, and each playbook's own
leaf folders as the loader read them (`load/pack.py` builds one); and
the pack's `app/<name>` addressing — the `<app>/<playbook>` ref every
skin parses, and the qualified key every macro dispatches under.
"""

from dataclasses import dataclass, field

from physiclaw.common import paths
from physiclaw.conductor.spec.model import Playbook, PlaybookError, Recovery
from physiclaw.conductor.spec.pages import Landmark, PageDecl, PagePrint
from physiclaw.macros.model import MACRO_SUFFIX, Macro


@dataclass(frozen=True)
class Scanned[T]:
    """One leaf folder, read: what parsed, by bare name, and what did
    not, with its reason — so a route naming a broken file fails with
    the cause instead of "not found"."""

    ok: dict[str, T] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Files:
    """The leaf folders a playbook owns beside its PLAYBOOK.yml: its
    recorded hands (`macros/*.yml`) and the model's prose
    (`prompts/*.md`, placeholders filled, trailing whitespace trimmed)."""

    macros: Scanned[Macro] = field(default_factory=Scanned)
    prompts: Scanned[str] = field(default_factory=Scanned)


@dataclass(frozen=True)
class Pack:
    """What a playbook validates against: the app's declared pages, its
    private macros (name → parsed Macro, or the parse error string), and
    its landmarks."""

    app: str
    pages: dict[str, PageDecl]
    macros: dict[str, Macro]
    macro_errors: dict[str, str]
    # The pages merged with their learned geometry — the matcher's
    # candidate set, built ONCE at load and shared by the walk's verdicts
    # and every macro jump's page read (`match.PageCheck`).
    prints: tuple[PagePrint, ...] = ()
    # The raw playbook files (`<name>/PLAYBOOK.yml`) — parsed per entry
    # by `scan_playbooks`, so one broken walk excludes itself, never the
    # pack; the files that would not load ride as errors.
    playbook_docs: dict = field(default_factory=dict)
    playbook_errors: dict[str, str] = field(default_factory=dict)
    # The pack's shared prose (`prompts/*.md`, `app.prompts.<name>`)
    # and each playbook's own leaf folders, by playbook — the route's
    # compiler registers a playbook's recorded hands under
    # `<playbook>.<name>` beside its inline bodies.
    prompts: Scanned[str] = field(default_factory=Scanned)
    local: dict[str, Files] = field(default_factory=dict)
    # The manifest's `pages: <name>: recover:` hands, resolved once at
    # load through the pack's own resolver (`route.recover.manifest_recovers`);
    # every route inherits them for a shared page unless it declares
    # its own.
    recovers: dict[str, Recovery] = field(default_factory=dict)
    # Pages a route declared itself (its `pages:` block or beside a
    # waypoint), by route — a route's page is its own: another route
    # may not name it.
    route_pages: dict[str, str] = field(default_factory=dict)

    # The pack's declared fixed spots (`landmarks:`) — recover hands and
    # agent grants name them. See `pages.Landmark`.
    landmarks: dict[str, Landmark] = field(default_factory=dict)

    def shared_pages(self) -> dict[str, PageDecl]:
        """The pages the whole pack may name — the manifest's, which is
        every declaration that is not some route's own. The one spelling
        of the rule `route_pages` is the other half of."""
        return {n: d for n, d in self.pages.items() if n not in self.route_pages}

    def local_for(self, playbook: str) -> Files:
        """A playbook's own leaf folders — its entry's, for a playbook
        that entry runs — empty when it has none."""
        return self.local.get(paths.entry_of(playbook), Files())

    def file_errors(self) -> list[tuple[str, str]]:
        """Every leaf file that would not load, as (pack-relative path,
        reason) — the one list `check` prints, spelled with the layout's
        own names so a folder rename never leaves a stale message."""
        macros, prompts = paths.PACK_MACROS_DIRNAME, paths.PACK_PROMPTS_DIRNAME
        out = [
            (f"{macros}/{n}{MACRO_SUFFIX}", e)
            for n, e in sorted(self.macro_errors.items())
        ]
        out += [
            (f"{prompts}/{n}{paths.PROMPT_SUFFIX}", e)
            for n, e in sorted(self.prompts.errors.items())
        ]
        for pb, files in sorted(self.local.items()):
            out += [
                (f"{pb}/{macros}/{n}{MACRO_SUFFIX}", e)
                for n, e in sorted(files.macros.errors.items())
            ]
            out += [
                (f"{pb}/{prompts}/{n}{paths.PROMPT_SUFFIX}", e)
                for n, e in sorted(files.prompts.errors.items())
            ]
        return out


def qualified_macro(app: str, name: str) -> str:
    """The qualified `app/name` dispatch key — the ONE spelling of the
    convention the run_macro handler resolves (user macro names can
    never contain "/", so no collision)."""
    return f"{app}/{name}"


def split_ref(ref: str) -> tuple[str, str]:
    """`<app>/<playbook>` → (app, playbook) — the one parse of the ref
    every skin takes (the CLI exits on the error, the studio answers
    400); a run-only playbook's is `<app>/<entry>.<name>`. Raises
    PlaybookError."""
    app, sep, name = ref.partition("/")
    if not sep or not app or not name or "/" in name:
        raise PlaybookError(
            f"{ref!r} is not <app>/<playbook> (one an entry runs: <app>/<entry>.<name>)"
        )
    return app, name


def macro_app(name: str) -> str:
    """The app half of a qualified dispatch key — `qualified_macro`'s
    inverse, kept beside it so the "/" convention has one spelling.
    "" for an unqualified name (user macros never carry an app)."""
    app, sep, _ = name.partition("/")
    return app if sep else ""


def qualified_pack(app: str, pack: Pack) -> dict[str, Macro]:
    """A pack's macros under their qualified dispatch keys."""
    return {qualified_macro(app, n): m for n, m in pack.macros.items()}


def qualified_inline(app: str, spec: Playbook) -> dict[str, Macro]:
    """A playbook's inline macros under their qualified dispatch keys —
    `qualified_pack`'s sibling for the hands that live in the playbook
    itself, the playbooks it runs included. Every registry a walk can
    dispatch through takes both."""
    return {
        qualified_macro(app, n): m
        for pb in spec.with_subs
        for n, m in pb.inline_macros.items()
    }
