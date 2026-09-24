"""The manifest's declared sections — pages, landmarks — and the two
halves of a page fingerprint, declared and learned.

A page fingerprint is split by audience:

  - DECLARATIONS (the pack file's ``pages:`` section, human-authored): the page's
    name, which label texts identify it, forbid terms, coarse region hints.
    Semantics only — portable across devices and app versions.
  - LEARNED (``learned/pages/<app>.json``, machine-written by capture):
    anchor positions and tolerances, OCR variants. Geometry is ALWAYS
    captured on-device, never authored or shipped — iPhone models, iOS
    and app versions make shipped geometry infeasible (the same reason
    first-run layout learning exists).

``PagePrint`` merges the two (`load.prints` reads both off the disk); a
declared page with no learned geometry yet still matches, text-only — every anchor must show, no
forbid term may — which is what lets capture bootstrap from
declarations alone. Geometry adds position checks, the scroll vote,
the overlay reading, and mined OCR variants; it never adds a score.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from physiclaw.common import paths
from physiclaw.common.bbox import Bbox, format_bbox, parse_box, parse_within
from physiclaw.conductor.spec import specfile
from physiclaw.conductor.spec.conventions import page_id
from physiclaw.conductor.spec.limits import (
    MAX_ANCHOR_LEN,
    MAX_ANCHORS,
    MAX_FORBID,
    MAX_LANDMARKS,
    MAX_PAGES,
)
from physiclaw.macros.model import (
    PAGES_KIND,
    app_ref,
    checked_readings,
)


class PagesError(specfile.SpecError):
    """A `pages:` section is invalid. Message is user-facing: the
    conductor CLI prints it verbatim. All-or-nothing — a pack failing
    any check is excluded whole, never partially loaded."""


# Shared spec substrate (`specfile`): the macro naming/prose rules bound
# to this spec's error class.
_require_str, _prose, _, _check_name = specfile.bind(PagesError)


@dataclass(frozen=True)
class AnchorDecl:
    """One declared identity anchor: the label text that should be on the
    page, optionally pinned to where it must sit (`within`). The same
    shape declares a page's `forbid:` terms — the text that must NOT
    show, pinned the same way.

    `alts` are further acceptable READINGS of that SAME anchor — any one
    satisfies it, and it still counts ONCE toward the page score. They are
    the authored counterpart of `LearnedAnchor.variants` (the readings
    capture mines on-device), and carry that same field shape on purpose:
    a mixed-locale phone (English system UI, Chinese apps) or a known OCR
    confusion is declared up front instead of waiting for capture to find
    it. The two converge — an alternate that does show up on this device
    gets mined into `variants` as well.

    Alternates must never be written as separate anchors: every declared
    anchor must show, so two spellings of one label would demand both
    on every device — and read the page unknown while it is in fact
    right there.

    `text` stays the canonical reading: learned geometry keys off it, and
    it is the name hits/missing report.
    """

    text: str
    alts: tuple[str, ...] = ()
    # Where the anchor must sit (a band resolved to its box at parse), or
    # None = anywhere. A pinned anchor is chrome — it does not scroll.
    within: "Bbox | None" = None

    @property
    def readings(self) -> tuple[str, ...]:
        """Canonical first, then the alternates — what the matcher tries."""
        return (self.text, *self.alts)


@dataclass(frozen=True)
class PageDecl:
    name: str
    anchors: tuple[AnchorDecl, ...]
    # What this page IS, in the author's words — the one home for a
    # meaning that would otherwise be retyped as a comment at every
    # `page:` that names it, and what the refusals that list a pack's
    # pages read. Required, as a macro's and a playbook's are: a page
    # nobody can say in prose is one nobody can tell from its neighbour.
    description: str
    # Terms that read the page OUT while one shows — the anchor shape
    # (readings of one text, `within:`), the other polarity.
    forbid: tuple[AnchorDecl, ...] = ()
    scrollable: bool = False


# The pack-level fixed spots: `landmarks:`, an OPEN vocabulary of named
# spots the author knows — recover hands tap them, agent episodes read
# them as a `given:`. No name is reserved: what a landmark is for is
# said where it is used. (`MAX_LANDMARKS` caps them, in `limits.py`.)


@dataclass(frozen=True)
class Landmark:
    """One declared landmark — the gesture-target shape ({label, bbox})
    as PACK knowledge: prior the author holds about the app's fixed
    chrome, consumed by recover hands and agent-episode grants (never by
    money paths). `label` is the readings tuple; on-screen text lets the
    tap be located live, a description documents the coordinates.
    `page` (optional) scopes it: an episode may read the landmark only
    when that is the page it opens on, since its `context:` — grants and
    all — is resolved once, there."""

    label: tuple[str, ...]
    bbox: Bbox
    page: str | None = None

    def describe(self) -> str:
        """The landmark as a brief shows it: its readings (the text it
        carries, or the author's description of the spot) and its
        declared box, the listing's spelling."""
        reads = " / ".join(f'"{r}"' for r in self.label) or '""'
        return f"reads {reads}, box {format_bbox(self.bbox)}"


@dataclass(frozen=True)
class LearnedAnchor:
    """Captured geometry for one declared anchor on this device."""

    text: str
    cx: float
    cy: float
    pos_tol: float
    freq: float  # fraction of capture observations that contained it
    variants: tuple[str, ...] = ()  # OCR misreads observed for this label


@dataclass(frozen=True)
class LearnedPage:
    anchors: dict[str, LearnedAnchor]  # keyed by declared anchor text
    observations: int


@dataclass(frozen=True)
class PagePrint:
    """One matchable page: declaration merged with whatever geometry has
    been captured (None until the first capture)."""

    app: str
    decl: PageDecl
    learned: LearnedPage | None = None

    @property
    def page_id(self) -> str:
        return page_id(self.app, self.decl.name)


# ---------- `pages:` parsing ----------


def parse_pages(text: str, app: str) -> dict[str, PageDecl]:
    """Parse + validate one `pages:` section given as YAML text — the
    text-shaped door tests and tooling use; `scan_app_decls` reads the
    live section out of the pack file. Raises PagesError naming the
    offending field; never returns a partially-valid set."""
    data = specfile.load_yaml(text, PagesError)
    return parse_pages_data(data, app)


# The page-field vocabulary, spelled ONCE. What IDENTIFIES a page is a
# reading of the screen, and that alone is what makes a route waypoint
# a declaration rather than a reference (`route_decl`): a `description:`
# says what an already-declared page is and declares nothing. The wider
# set is what `_parse_page` validates and what `route/compile.py` admits on a
# waypoint — a new page field lands there and reaches every door.
PAGE_IDENTITY_FIELDS = ("anchors", "forbid", "scrollable")
PAGE_DECL_FIELDS = ("description", *PAGE_IDENTITY_FIELDS)
# A manifest page's one non-declaration key: the recover hand every
# route of the pack inherits for it (a route may declare its own).
PAGE_RECOVERY_FIELDS = ("recover", "tries", "on_fail")


def recovery_fields(spec: dict) -> dict:
    """The recovery half of a page mapping — its PAGE_RECOVERY_FIELDS
    subset ({} when it declares none). `route_decl`'s mirror: the pack
    door (`collect_page_recovers`) and the route compiler read the
    same slice, so what "declares recovery" means has one home."""
    return {k: spec[k] for k in PAGE_RECOVERY_FIELDS if k in spec}


def decl_fields(spec: Any) -> Any:
    """The declaration half of a page mapping in the manifest's shape
    (the manifest's `pages:` and a route's `pages:` block alike): all
    but the recovery fields, unknown keys kept for the page parser to
    refuse. A non-mapping passes through for the same reason."""
    if not isinstance(spec, dict):
        return spec
    return {k: v for k, v in spec.items() if k not in PAGE_RECOVERY_FIELDS}


def collect_page_recovers(doc: dict) -> dict[str, dict]:
    """The RAW recovery the manifest's `pages:` declare, by page name —
    `{recover: hand(s), tries: n, on_fail: word}`, whichever keys the
    page carries —
    resolved by the route compiler (`route.recover.route_defaults`) against
    the pack's macros and landmarks, so the grammar has one home. Shape
    errors surface at that parse; this only collects."""
    appendix = doc.get("pages")
    if not isinstance(appendix, dict):
        return {}
    out: dict[str, dict] = {}
    for name, spec in appendix.items():
        if not isinstance(spec, dict):
            continue
        picked = recovery_fields(spec)
        if picked:
            out[str(name)] = picked
    return out


def page_menu(decls: "Mapping[str, PageDecl]") -> str:
    """The pack's pages for a refusal — one per line, each with what its
    `description:` says it is, so an author picks from a menu instead of
    a wall of names. The ONE rendering: a waypoint's refusal and a
    macro's read alike."""
    if not decls:
        return " (none)"
    return "".join(
        f"\n  {app_ref(PAGES_KIND, n)} — {d.description}"
        for n, d in sorted(decls.items())
    )


def route_decl(entry: dict) -> "dict | None":
    """The declaration half of one route waypoint — its PAGE_DECL_FIELDS
    subset, or None for a bare reference. The ONE predicate for "does
    this waypoint declare": `collect_page_decls` (the pack door) and the
    playbook parser's prepass (the text door) must never disagree on
    it. What DECLARES is a reading (`PAGE_IDENTITY_FIELDS`); a
    `description:` rides along and never declares on its own."""
    if not any(k in entry for k in PAGE_IDENTITY_FIELDS):
        return None
    return {k: entry[k] for k in PAGE_DECL_FIELDS if k in entry}


def collect_page_decls(doc: dict, playbook_docs: dict | None = None) -> dict:
    """The pack's RAW page declarations, wherever they were written —
    `page_sites` without the sites."""
    return {name: spec for name, (spec, _) in page_sites(doc, playbook_docs).items()}


def page_sites(
    doc: dict, playbook_docs: dict | None = None
) -> dict[str, tuple[Any, str | None]]:
    """Every page declaration with where it was written — the raw spec
    and the declaring route (None: the manifest's `pages:` appendix; a
    route declares in its own `pages:` block or beside a waypoint),
    across every playbook file. Data-level on purpose — this runs at
    the pack door (`scan_app_decls`, `load_pack`) before any playbook
    parses, so the matcher sees route-declared pages through every
    door and pack.py never re-owns the page grammar. A page is
    DECLARED exactly once per pack; a second site raises with both
    named — the same page declared in two files is a pack error, never
    a silent merge. Malformed playbook shapes are skipped here — each
    playbook excludes itself at its own parse, never the pack."""
    out: dict[str, tuple[Any, str | None]] = {}
    sites: dict[str, str] = {}

    def declare(name: str, decl: Any, owner: str | None, site: str) -> None:
        # Led by the site that declares it a second time: the author just
        # wrote that one, and every refusal from this door names a file
        # first.
        if name in sites:
            raise PagesError(
                f"{site}: page {name!r} declared twice — also in {sites[name]}; "
                f"declare once (the manifest's is `{app_ref(PAGES_KIND, name)}`, "
                f"a route's own is bare)"
            )
        out[name] = (decl, owner)
        sites[name] = site

    appendix = doc.get("pages")
    if appendix is not None:
        if not isinstance(appendix, dict):
            raise PagesError(
                f"{paths.PACK_FILENAME}: `pages` must be a YAML mapping of "
                "page name → spec"
            )
        for name, spec in appendix.items():
            declare(str(name), decl_fields(spec), None, paths.PACK_FILENAME)
    for pb_name, pb in (playbook_docs or {}).items():
        if not isinstance(pb, dict):
            continue
        block = pb.get("pages")
        if isinstance(block, dict):
            for name, spec in block.items():
                declare(
                    str(name),
                    decl_fields(spec),
                    pb_name,
                    f"{paths.playbook_file(pb_name)}'s `pages:`",
                )
        route = pb.get("route")
        if not isinstance(route, list):
            continue
        for entry in route:
            if not isinstance(entry, dict) or "page" not in entry:
                continue
            decl = route_decl(entry)
            name = str(entry["page"])
            if decl is None or "." in name:
                # A bare waypoint is a reference, not a declaration; a
                # dotted one names the manifest's page or a reserved
                # built-in — its own route's parse refuses a declaration
                # there with the exact reason, never the whole pack.
                continue
            declare(name, decl, pb_name, f"{paths.playbook_file(pb_name)}'s route")
    return out


_LANDMARK_KEYS = frozenset({"label", "at", "page"})


def parse_landmarks(data: Any, pages: "set[str] | None" = None) -> dict[str, Landmark]:
    """The `landmarks:` section → validated Landmarks. Each entry is the
    gesture-target shape (`{label, at}` — a macro step's pairing rule,
    at pack level) plus an optional `page:` scope, under any valid
    name; its consumers are the pack's own recover hands and agent
    grants. `pages` (the pack's declared page names) validates the
    scope when given."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise PagesError("`landmarks` must be a mapping of name → target")
    if len(data) > MAX_LANDMARKS:
        raise PagesError(f"`landmarks`: {len(data)} entries > max {MAX_LANDMARKS}")
    out: dict[str, Landmark] = {}
    for name, spec in data.items():
        where = f"landmark {name!r}"
        _check_name(name, where)
        if (
            not isinstance(spec, dict)
            or not {"label", "at"} <= set(spec.keys())
            or not set(spec.keys()) <= _LANDMARK_KEYS
        ):
            raise PagesError(f"{where} must be a {{label, at, [page]}} mapping")
        label = checked_readings(spec, where, _require_str, PagesError)
        try:
            left, top, right, bottom = parse_box(spec["at"])
        except (ValueError, TypeError) as e:
            raise PagesError(f"{where}: {e}") from e
        page = spec.get("page")
        if page is not None:
            _check_name(page, f"{where}: `page`")
            if pages is not None and page not in pages:
                known = ", ".join(sorted(pages)) or "(none)"
                raise PagesError(
                    f"{where}: `page` {page!r} is not a declared page of this "
                    f"pack. Declared: {known}"
                )
        out[name] = Landmark(label=label, bbox=(left, top, right, bottom), page=page)
    return out


def pack_landmarks(doc: dict) -> dict[str, Landmark]:
    """The pack document's `landmarks:` section, parsed (empty when
    absent) — with every `page:` scope checked against the pages the
    same document declares, so no door can accept a scope another
    refuses."""
    return parse_landmarks(doc.get("landmarks"), set(collect_page_decls(doc)))


def parse_pages_data(
    data: Any,
    app: str,
    files: "Mapping[str, str] | None" = None,
    section: str = "",
) -> dict[str, PageDecl]:
    """The `pages:` section of a pack file → validated declarations.
    A pack's pages come from several files, so a refusal must send the
    author to the right one: `files` names the pack-relative file each
    page was declared in (a route declares its own beside a waypoint or
    in its `pages:` block), and `section` names the file the `pages:`
    key itself lives in, for what is wrong with the section rather than
    with one page. Both empty when the caller has one file anyway."""
    _check_name(app, "app name")
    if data is None:
        return {}
    at = f"{section}: " if section else ""
    if not isinstance(data, dict):
        raise PagesError(f"{at}`pages` must be a YAML mapping of page name → spec")
    if len(data) > MAX_PAGES:
        raise PagesError(f"{at}{len(data)} pages > max {MAX_PAGES}")

    out: dict[str, PageDecl] = {}
    for name, spec in data.items():
        file = (files or {}).get(name, "")
        _check_name(name, f"{file}: page name" if file else "page name")
        out[name] = _parse_page(name, spec, file)
    return out


def _parse_page(name: str, spec: Any, file: str = "") -> PageDecl:
    where = f"{file}: page `{name}`" if file else f"page `{name}`"
    if not isinstance(spec, dict):
        raise PagesError(f"{where}: spec must be a mapping")
    unknown = sorted(set(spec.keys()) - set(PAGE_DECL_FIELDS))
    if unknown:
        raise PagesError(f"{where}: unknown key(s): {', '.join(map(str, unknown))}")

    anchors = _parse_anchors(spec.get("anchors"), where)

    raw_forbid = spec.get("forbid", [])
    if not isinstance(raw_forbid, list):
        raise PagesError(f"{where}: `forbid` must be a list of terms")
    if len(raw_forbid) > MAX_FORBID:
        raise PagesError(f"{where}: {len(raw_forbid)} forbid terms > max {MAX_FORBID}")
    forbid = tuple(_parse_anchor(t, f"{where} forbid") for t in raw_forbid)

    scrollable = spec.get("scrollable", False)
    if not isinstance(scrollable, bool):
        raise PagesError(f"{where}: `scrollable` must be true or false")

    return PageDecl(
        name=name,
        anchors=anchors,
        description=_prose(spec.get("description"), f"{where}: `description`"),
        forbid=forbid,
        scrollable=scrollable,
    )


def _parse_anchors(raw: Any, where: str) -> tuple[AnchorDecl, ...]:
    """`anchors:` — a LIST of anchors, all of which the page should show
    (every anchor must show; a forbid term must not).
    Each anchor is a text, a list of alternate readings of ONE text, or
    `{text, within}` with `within` a band (`top`, `bottom`, `left`,
    `right`) or a box — the check shape a macro step's `require:` uses,
    and the shape each `forbid:` term takes too.
    Alternates go INSIDE an anchor, never as separate anchors: every
    declared anchor must show."""
    if isinstance(raw, str):
        raise PagesError(f"{where}: `anchors` is a list — write `anchors: [{raw!r}]`")
    if not isinstance(raw, list):
        raise PagesError(f"{where}: `anchors` must be a list of anchors")
    if not raw:
        raise PagesError(f"{where}: `anchors` must be non-empty")
    if len(raw) > MAX_ANCHORS:
        raise PagesError(f"{where}: {len(raw)} anchors > max {MAX_ANCHORS}")
    return tuple(_parse_anchor(a, where) for a in raw)


def parse_target(
    raw: Any,
    where: str,
    *,
    key: str,
    require_str: "Callable[[object, str], str]",
    err: type[Exception],
) -> AnchorDecl:
    """The target shape every readings-plus-band declaration takes — a
    page anchor and a `forbid:` term (`text`), an episode's `never_tap:`
    (`label`): one reading, a list of alternate readings of ONE target,
    or a mapping of the readings under `key` with an optional `within`
    band or box. The ONE parser of that shape, raising the caller's
    error class: the readings grammar (`checked_readings`), the text
    rules (single-line, under `MAX_ANCHOR_LEN`) and the band can never
    drift between the declarations one row matcher reads."""
    within: Bbox | None = None
    if isinstance(raw, (str, list)):
        spec: dict = {key: raw}
    elif isinstance(raw, dict):
        unknown = sorted(set(raw.keys()) - {key, "within"})
        if unknown:
            raise err(f"{where}: unknown key(s): {', '.join(map(str, unknown))}")
        if key not in raw:
            raise err(f"{where}: a target mapping needs `{key}`")
        spec = raw
        if raw.get("within") is not None:
            try:
                within = parse_within(raw["within"])
            except (ValueError, TypeError) as e:
                raise err(f"{where}: `within` {e}") from e
    else:
        raise err(
            f"{where} must be a text, a list of readings, or a "
            f"{{{key}, within}} mapping"
        )
    readings = checked_readings(spec, where, require_str, err, key=key)
    for text in readings:
        if len(text) > MAX_ANCHOR_LEN:
            raise err(f"{where}: `{key}` {len(text)} chars > max {MAX_ANCHOR_LEN}")
        if "".join(text.splitlines()) != text:
            raise err(f"{where}: `{key}` must be single-line: {text!r}")
    return AnchorDecl(text=readings[0], alts=readings[1:], within=within)


def _parse_anchor(raw: Any, where: str) -> AnchorDecl:
    """An anchor or forbid term: the target shape, plus the identity
    rule — a single character as a whole-screen anchor would match
    inside almost any label, so it needs a `within`. Per reading: one
    loose alternate opens the same door as one loose anchor."""
    decl = parse_target(
        raw, f"{where} anchor", key="text", require_str=_require_str, err=PagesError
    )
    if decl.within is None:
        for text in decl.readings:
            if len(text) == 1:
                raise PagesError(
                    f"{where}: single-character anchor {text!r} needs a `within`"
                )
    return decl
