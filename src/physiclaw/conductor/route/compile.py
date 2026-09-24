"""The route compiler — `route:` → the compiled moves, the start page,
and each page's declared recovery hand; the whole-route lints are
`lints.py`, and each move's own parser sits beside this file, one per
kind (`MOVES`: its field vocabulary, its parser, whether it may sit
above the first page), every one reading the same `Line`.

Waypoints do not become nodes — they become the adjacent moves' checks:
a `do`'s (and an acting `agent`'s) enter is the nearest preceding page,
its verify the page that must follow it. The route opens with an
optional prefix of pure-text `agent` steps and an optional `start` (the
unconditional cold launch); the first page is the start contract. Moves
fall through in route order — an `ask` once approved, a `tell` once its
message landed — and past the last entry the walk is done.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from physiclaw.common.paths import (
    PACK_FILENAME,
    entry_of,
    playbook_file,
)
from physiclaw.conductor.route import lints
from physiclaw.conductor.route.agent import parse_agent
from physiclaw.conductor.route.ask import parse_ask
from physiclaw.conductor.route.do import parse_do, parse_start
from physiclaw.conductor.route.recover import (
    declared_once,
    overlay,
    parse_recover,
    route_defaults,
)
from physiclaw.conductor.route.resolve import local_registry, pack_hint, route_resolver
from physiclaw.conductor.route.run import parse_run, sub_playbook
from physiclaw.conductor.route.scope import Line, Scope, Waypoint
from physiclaw.conductor.route.select import is_boot, parse_select
from physiclaw.conductor.route.tell import parse_tell
from physiclaw.conductor.spec.conventions import (
    RESERVED_APPS,
)
from physiclaw.conductor.spec.limits import (
    MAX_NODES,
)
from physiclaw.conductor.spec.model import (
    INPUTS_ROOT,
    Node,
    Playbook,
    PlaybookError,
    Recovery,
    check_name,
    require_str,
)
from physiclaw.conductor.spec.pack import Pack
from physiclaw.conductor.spec.pages import (
    PAGE_DECL_FIELDS,
    PAGE_RECOVERY_FIELDS,
    PagesError,
    decl_fields,
    page_menu,
    parse_pages_data,
    recovery_fields,
    route_decl,
)
from physiclaw.macros.model import (
    PAGES_KIND,
    Macro,
    app_ref,
    parse_ref,
)


@dataclass(frozen=True)
class Move:
    """One move kind of the route: the field vocabulary beside its
    leading key, its parser, and whether it may sit above the first
    page (`cold`: it needs no screen the route has reached — a
    pure-text agent, the start, a tell over the channel, a
    self-starting run; the parser refuses the ones that do)."""

    keys: frozenset[str]
    parse: Callable[[Scope, Line], Node]
    cold: bool = False


# An entry's KIND is its leading key and the value is the entry's name
# — the map-key-is-the-name doctrine, applied to the route. One record
# per move kind: adding a kind is one line here and one parser file.
MOVES: dict[str, Move] = {
    "start": Move(frozenset({"start", "macro", "on_fail"}), parse_start, cold=True),
    "do": Move(frozenset({"do", "with", "macro", "irreversible", "on_fail"}), parse_do),
    "agent": Move(
        frozenset(
            {
                "agent",
                "context",
                "tools",
                "never_tap",
                "returns",
                "limit",
                "irreversible",
                "think",
                "on_fail",
            }
        ),
        parse_agent,
        cold=True,
    ),
    "ask": Move(
        frozenset(
            {
                "ask",
                "approve",
                "message",
                "yes",
                "no",
                "denied",
                "total_label",
                "wait",
                "rounds",
                "resume",
                "think",
                "on_fail",
            }
        ),
        parse_ask,
    ),
    "tell": Move(frozenset({"tell", "message", "on_fail"}), parse_tell, cold=True),
    "run": Move(
        frozenset({"run", "with", "each", "miss", "revise", "limit", "on_fail"}),
        parse_run,
        cold=True,
    ),
    "select": Move(frozenset({"select", "limit", "think"}), parse_select),
}
# A page is a waypoint, not a move. Its declaration fields come from
# `pages.py`'s ONE spelling (PAGE_DECL_FIELDS) — their content is
# validated there; they appear here only so the unknown-key check names
# them as legal.
_PAGE_KEYS = frozenset({"page", *PAGE_RECOVERY_FIELDS, *PAGE_DECL_FIELDS})
LINE_KINDS = ("page", *MOVES)


@dataclass(frozen=True)
class CompiledRoute:
    """What `compile_route` hands back: the moves, the start page, each
    page's declared recovery hand, and the inline macro bodies under
    their synthesized names."""

    nodes: list[Node]
    start: str
    recovers: dict[str, Recovery]
    inline: dict[str, Macro]
    prompts_used: frozenset[str] = frozenset()
    # The last waypoint ("" when the route ends on a move) and every
    # move's declared outputs — what the playbook's own `returns:` and a
    # `run` of it read.
    end: str = ""
    payloads: dict[str, tuple[str, ...]] = field(default_factory=dict)


def compile_route(
    raw: Any,
    *,
    playbook: str,
    input_names: set[str],
    pack: Pack,
    resolve_playbook: Callable[[str], Playbook] | None = None,
    pages: Any = None,
) -> CompiledRoute:
    """`route:` → the compiled route (see `CompiledRoute`): the shape
    prepass first (every rule about WHERE an entry may sit), then one
    forward pass compiling the moves against the waypoints around them,
    then the lints that need the whole route."""
    own = _own_pages(pages, pack)
    lines, start = _shape(raw, pack, set(own))
    local = pack.local_for(playbook)
    # The entry's recorded hands enter the dispatch table here, once,
    # under their `<entry>.<name>` spelling (the playbooks it runs read
    # the same folder) — the inline bodies the route embeds join them as
    # the compile pass meets them.
    inline = local_registry(entry_of(playbook), pack, local.macros)
    scope = Scope(
        playbook,
        pack,
        input_names,
        route_resolver(playbook, pack, inline, local.macros),
        prompts_local=local.prompts,
        prompts_pack=pack.prompts,
        resolve_playbook=resolve_playbook,
    )
    # A run's returns are known before the route is walked, so a text
    # ABOVE the run may quote them (empty until its rounds end — the
    # one forward ref, for a plan that re-reads what a run already
    # did). Resolved here, once, and the bodies kept for the parse.
    for line in lines:
        if isinstance(line, Line) and line.kind == "run":
            sub = sub_playbook(scope, f"route line {line.pos}", line.name)
            scope.subs[line.name] = sub
            scope.payloads[line.name] = tuple(sub.returns)
    recovers: dict[str, Recovery] = {}  # this route's own hands, by page
    for line in lines:
        if isinstance(line, Waypoint):
            _declare_hand(scope, line, recovers)
        else:
            scope.compiled.append(MOVES[line.kind].parse(scope, line))
    moves = scope.compiled
    if len(moves) > MAX_NODES:
        raise PlaybookError(f"too many moves ({len(moves)} > {MAX_NODES})")
    # Money reads the walked route whole (a pay move's predecessor, in
    # every round); resume reads the pairs this route creates.
    lints.check_money(lints.flatten(moves))
    lints.check_resume(moves)
    if is_boot(scope):
        lints.check_boot(moves)
    # The route's defaults beneath its waypoints' hands: a waypoint that
    # declares a page's hand replaces the default whole.
    return CompiledRoute(
        nodes=moves,
        start=start,
        recovers=overlay(route_defaults(scope, own), recovers),
        inline=inline,
        prompts_used=frozenset(scope.prompts_used),
        end=lines[-1].id if isinstance(lines[-1], Waypoint) else "",
        payloads=dict(scope.payloads),
    )


def _declare_hand(scope: Scope, wp: Waypoint, recovers: dict[str, Recovery]) -> None:
    """A waypoint's `recover:` block, if it carries one — this route's
    own hand for that page, declared once."""
    fields = recovery_fields(wp.entry)
    if not fields:
        return
    if "." in wp.id:
        raise PlaybookError(
            f"{wp.where}: {wp.id!r} is a reserved built-in "
            "— packs declare recovery for their own pages only"
        )
    declared = parse_recover(scope, fields, wp.where, wp.id)
    recovers[wp.id] = declared_once(recovers.get(wp.id), declared, wp.where, wp.id)


def _shape(
    raw: Any, pack: Pack, own_names: set[str]
) -> tuple[list[Line | Waypoint], str]:
    """The route's shape, proved before any move is compiled: a
    non-empty list whose first page is the start contract, at most one
    `start` sitting right before it, only cold moves above it, every
    waypoint's id resolved (`_waypoint_id`, one grammar at every door)
    and every move's name legal and unique. Returns the lines — each
    move a `Line` that already knows the waypoints around it, each page
    a `Waypoint` — and the start page id."""
    if not isinstance(raw, list) or not raw:
        raise PlaybookError("`route` must be a non-empty list")
    entries = [_classify_line(i, e) for i, e in enumerate(raw, start=1)]
    first_page = next((i for i, (k, _, _) in enumerate(entries) if k == "page"), None)
    if first_page is None:
        raise PlaybookError(
            "the route needs a page waypoint — the walk's start contract"
        )
    starts = [i for i, (k, _, _) in enumerate(entries) if k == "start"]
    if len(starts) > 1:
        raise PlaybookError("at most one `start` — a route cold-launches once")
    if starts and starts[0] != first_page - 1:
        raise PlaybookError(
            "`start` must sit immediately before the first page — the page "
            "that follows it is the landing it must reach"
        )
    for i in range(first_page):
        kind, _, _ = entries[i]
        # A tell speaks over the channel from any screen; a run up here
        # must open with its playbook's own start (`parse_run`); an
        # acting agent fails in `parse_agent`, having no page to start on.
        if not MOVES[kind].cold:
            raise PlaybookError(
                f"route line {i + 1}: only pure-text `agent` steps (no "
                "tools), `start`, a `tell` and a self-starting `run` may "
                f"precede the first page — a `{kind}` needs a screen the "
                "route has not reached yet"
            )
    if all(kind == "page" for kind, _, _ in entries):
        raise PlaybookError(
            "the route needs at least one move (start/do/agent/ask/tell)"
        )

    # Waypoint prepass: every page id resolved and its in-place
    # declaration validated up front (`_waypoint_id` — one grammar at
    # every door), so a `do` can read the page that follows it in one
    # forward look. `pages.route_decl` is the one declaration predicate,
    # shared with `collect_page_decls` so the two doors cannot disagree.
    declared_here = own_names | {
        name
        for kind, name, entry in entries
        if kind == "page" and route_decl(entry) is not None
    }
    wp_ids: list[str | None] = [
        _waypoint_id(i + 1, name, entry, pack, declared_here)
        if kind == "page"
        else None
        for i, (kind, name, entry) in enumerate(entries)
    ]
    page_names = {pid for pid in wp_ids if pid is not None}
    lines: list[Line | Waypoint] = []
    seen: dict[str, int] = {}
    before: str | None = None
    for i, (kind, name, entry) in enumerate(entries):
        pos, pid = i + 1, wp_ids[i]
        if pid is not None:
            before = pid
            lines.append(Waypoint(pos, pid, entry))
            continue
        _check_move_name(pos, kind, name, page_names, seen)
        after = wp_ids[i + 1] if i + 1 < len(entries) else None
        lines.append(Line(pos, kind, name, entry, before=before, after=after))
    start = wp_ids[first_page]
    assert start is not None  # first_page indexes a page entry
    return lines, start


def _check_move_name(
    pos: int, kind: str, name: str, page_names: set[str], seen: dict[str, int]
) -> None:
    """A move's name: legal, not the reserved ref root, not a page of
    this route (moves and pages share one namespace), not used twice
    (refs address moves by name)."""
    where = f"route line {pos}"
    check_name(name, f"{where}: `{kind}`")
    if name == INPUTS_ROOT:
        raise PlaybookError(
            f"{where}: name {name!r} is a reserved ref root — "
            "{inputs.*} always reads the declared inputs"
        )
    if name in page_names:
        raise PlaybookError(
            f"{where}: {name!r} is also a page on this route — moves "
            "and pages share one namespace, so the names must not collide"
        )
    if name in seen:
        raise PlaybookError(
            f"{where}: duplicate move name {name!r} (line {seen[name]} "
            "already uses it) — refs address moves by name, so they "
            "must be unique"
        )
    seen[name] = pos


def _classify_line(i: int, entry: Any) -> tuple[str, str, dict]:
    """(kind, name, entry) for one route line — the kind is its leading
    key, the value the name; exactly one kind key, and only that kind's
    field vocabulary beside it."""
    where = f"route line {i}"
    if not isinstance(entry, dict):
        raise PlaybookError(f"{where} must be a mapping")
    kinds = [k for k in LINE_KINDS if k in entry]
    if len(kinds) != 1:
        raise PlaybookError(
            f"{where} must carry exactly one of {', '.join(LINE_KINDS)} "
            f"(got: {', '.join(map(str, sorted(entry))) or '(empty)'})"
        )
    kind = kinds[0]
    keys = _PAGE_KEYS if kind == "page" else MOVES[kind].keys
    unknown = sorted(set(map(str, entry.keys())) - keys)
    if unknown:
        raise PlaybookError(
            f"{where}: unknown key(s) for `{kind}`: {', '.join(unknown)}"
        )
    return kind, require_str(entry.get(kind), f"{where}: `{kind}`"), entry


def _own_pages(raw: Any, pack: Pack) -> dict[str, dict]:
    """A route's `pages:` block — the manifest's page shape, this
    route's ownership — as page name → its recovery fields ({} when it
    declares none): the names seed `_shape`'s declared set, the fields
    are parsed once the route's context exists (`route_defaults`)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PlaybookError("`pages` must be a mapping of page name → spec")
    own: dict[str, dict] = {}
    for name, spec in raw.items():
        name = str(name)
        where = f"route page {name!r}"
        if not isinstance(spec, dict):
            raise PlaybookError(f"{where}: spec must be a mapping")
        _check_decl(where, name, decl_fields(spec), pack)
        own[name] = recovery_fields(spec)
    return own


def _check_decl(where: str, page: str, decl: Any, pack: Pack) -> None:
    """A page declaration's CONTENT, checked at the text door
    (`parse_playbook` — tests, tooling) with the page grammar the pack
    door runs via `collect_page_decls`, so a playbook green at one door
    is never red at the other. The pack door already parsed it when the
    pack knows the page."""
    if page in pack.pages:
        return
    try:
        parse_pages_data({page: decl}, pack.app)
    except PagesError as e:
        raise PlaybookError(f"{where}: {e}") from e


def _waypoint_id(pos: int, name: str, entry: dict, pack: Pack, declared: set) -> str:
    """One page waypoint's id. Three spellings: a bare name is a page
    THIS route declares (in its `pages:` block, or beside a waypoint —
    there, and at every later waypoint); `app.pages.<name>` is the manifest's; the
    reserved built-ins stay `ios.<page>` / `channel.<page>` and can only
    be referenced, never declared here. So a bare `page:` with no
    declaration in the file is an error, never a lookup elsewhere."""
    where = f"route line {pos}"
    r = parse_ref(name)
    shared = r is not None and r.shared
    reserved = not shared and "." in name
    page = (
        r.name
        if r is not None and shared
        else name.partition(".")[2]
        if reserved
        else name
    )
    declares = route_decl(entry) is not None
    owner = pack.route_pages.get(page)
    if r is not None and shared and r.kind != PAGES_KIND:
        raise PlaybookError(
            f"{where}: page {name!r} — the manifest's pages are `{app_ref(PAGES_KIND, '<name>')}`"
        )
    if reserved and name.partition(".")[0] not in RESERVED_APPS:
        raise PlaybookError(
            f"{where}: page {name!r} — a waypoint names a page declared in this "
            f"route bare, the manifest's as `{app_ref(PAGES_KIND, '<page>')}`, or a "
            f"reserved namespace ({', '.join(sorted(RESERVED_APPS))}).<page>"
        )
    check_name(page, f"{where}: `page`")
    if "description" in entry and not declares:
        raise PlaybookError(
            f"{where}: a `description:` says what a page IS — it belongs where "
            f"the page is declared, with its anchors"
        )
    if declares and shared:
        raise PlaybookError(
            f"{where}: {name!r} carries a declaration — a page declared beside "
            f"its waypoint is this route's own, written bare: `page: {page}`"
        )
    if declares and reserved:
        raise PlaybookError(
            f"{where}: {name!r} is a reserved built-in — it cannot be declared from a pack"
        )
    if shared and owner is not None:
        hint = (
            f"write `page: {page}`"
            if page in declared
            else f"a route's page is its own — move it to {PACK_FILENAME} to share it"
        )
        raise PlaybookError(
            f"{where}: page {page!r} is declared in {playbook_file(owner)}, "
            f"not in {PACK_FILENAME} — {hint}"
        )
    if shared and page not in pack.pages:
        raise PlaybookError(
            f"{where}: page {page!r} is not declared in {PACK_FILENAME}. "
            f"Declared:{page_menu(pack.shared_pages())}"
        )
    if declares:
        _check_decl(where, page, route_decl(entry), pack)
    if not shared and not reserved and not declares and page not in declared:
        raise PlaybookError(
            f"{where}: page {page!r} is not declared in this route — declare it "
            f"in the route's `pages:` block or beside a waypoint (anchors under `page:`)"
            + pack_hint(PAGES_KIND, page, page in pack.pages and owner is None)
        )
    return name if reserved else page
