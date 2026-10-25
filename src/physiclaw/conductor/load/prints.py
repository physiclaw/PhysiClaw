"""Page prints off the disk — the declared pages of one app pack
(`scan_app_decls`: the `pages:` appendix plus every route-declared
waypoint) and the learned geometry beside them (`learned/pages/<app>.json`,
never authored or shipped: written by `bench.capture`, read here
fail-open — a bad file degrades to declaration-only matching, never
takes a session down). `prints_for_app` merges the two into what the
matcher reads.
"""

import json
import logging
from pathlib import Path
from typing import Any

from physiclaw.common import paths
from physiclaw.common.logger import write_json_atomic
from physiclaw.common.text import read_text
from physiclaw.conductor.load.files import load_pack_doc, load_playbook_docs
from physiclaw.conductor.spec import specfile
from physiclaw.conductor.spec.conventions import IOS_APP
from physiclaw.conductor.spec.pages import (
    LearnedAnchor,
    LearnedPage,
    PageDecl,
    PagePrint,
    PagesError,
    collect_page_decls,
    parse_pages_data,
)

log = logging.getLogger(__name__)

_check_name = specfile.bind(PagesError)[3]


def scan_app_decls(app: str) -> dict[str, PageDecl]:
    """The declared pages of one app pack — the `pages:` appendix PLUS
    every route-declared waypoint (`collect_page_decls`); {} when the
    pack doesn't exist or declares none. Raises PagesError on a
    malformed file (the CLI surfaces it; runtime callers catch and
    treat the app as undeclared). The name is validated BEFORE any path
    is built from it. Every pack reads the same way, `ios` included —
    declarations live on disk, under the user's hand, never in the
    wheel."""
    _check_name(app, "app name")
    doc = load_pack_doc(app, PagesError)
    if doc is None:
        return {}
    docs, _errors = load_playbook_docs(app, PagesError)
    return parse_pages_data(collect_page_decls(doc, docs), app)


_LEARNED_SCHEMA = 1


def learned_file(app: str) -> Path:
    """`learned/pages/<app>.json` — keyed by the folder too when a pack
    loads from one named differently (the channel's IM folder:
    `channel-wechat.json`), so one IM's calibrated thread never stands
    in for another's."""
    folder = paths.pack_root(app).name
    stem = app if folder == app else f"{app}-{folder}"
    return paths.learned_pages_dir() / f"{stem}.json"


def load_learned(app: str) -> dict[str, LearnedPage]:
    """The captured geometry for one app — {} on missing/unreadable/stale
    file (fail-open: a bad learned file degrades to declaration-only
    matching, never takes a session down)."""
    p = learned_file(app)
    if not p.exists():
        return {}
    try:
        data = json.loads(read_text(p))
        if data.get("schema") != _LEARNED_SCHEMA:
            log.warning("learned pages %s: unknown schema; ignoring", p)
            return {}
        out: dict[str, LearnedPage] = {}
        for name, lp in data["pages"].items():
            anchors = {}
            for raw in lp["anchors"]:
                anchor = _learned_anchor(raw)
                anchors[anchor.text] = anchor
            observations = lp["observations"]
            if type(observations) is not int or observations < 1:
                raise ValueError("observations must be a positive integer")
            # A file written before scores were retired carries
            # `threshold` and per-anchor `weight`; both are ignored.
            out[name] = LearnedPage(anchors=anchors, observations=observations)
        return out
    except Exception:
        log.warning("learned pages %s unreadable; ignoring", p, exc_info=True)
        return {}


def _learned_anchor(raw: dict[str, Any]) -> LearnedAnchor:
    """Validate cached text and geometry before the matcher can use them."""
    text = raw["text"]
    variants = raw.get("variants", [])
    if not isinstance(text, str) or not text.strip():
        raise ValueError("anchor text must be a non-empty string")
    if not isinstance(variants, list) or not all(
        isinstance(v, str) and v.strip() for v in variants
    ):
        raise ValueError("anchor variants must be a list of non-empty strings")
    geometry = {}
    for key in ("cx", "cy", "pos_tol", "freq"):
        value = raw[key]
        # The interval check also rejects NaN and infinities. Booleans
        # are JSON flags, not geometry, even though bool subclasses int.
        if type(value) not in (int, float) or not 0 <= value <= 1:
            raise ValueError(f"anchor {key} must be a number between 0 and 1")
        geometry[key] = float(value)
    return LearnedAnchor(text=text, variants=tuple(variants), **geometry)


def save_learned(app: str, pages: dict[str, LearnedPage]) -> None:
    paths.learned_pages_dir().mkdir(parents=True, exist_ok=True)
    obj = {
        "schema": _LEARNED_SCHEMA,
        "app": app,
        "pages": {
            name: {
                "observations": lp.observations,
                "anchors": [
                    {
                        "text": a.text,
                        "cx": a.cx,
                        "cy": a.cy,
                        "pos_tol": a.pos_tol,
                        "freq": a.freq,
                        "variants": list(a.variants),
                    }
                    for a in lp.anchors.values()
                ],
            }
            for name, lp in pages.items()
        },
    }
    write_json_atomic(learned_file(app), obj)


def prints_for_app(
    app: str, decls: dict[str, PageDecl] | None = None
) -> list[PagePrint]:
    """Declarations merged with learned geometry — the matcher's candidate
    set for one app. Callers already holding the Pack pass
    `decls=pack.pages` so the spec file is not re-read (the
    `scan_playbooks` rule)."""
    if decls is None:
        decls = scan_app_decls(app)
    learned = load_learned(app)
    return [
        PagePrint(app=app, decl=d, learned=learned.get(name))
        for name, d in decls.items()
    ]


def os_prints() -> list[PagePrint]:
    """The OS states every walk matches beside its own pages — the ios
    pack's declared lock screen (the matcher reads the cover by shape
    regardless; a declared hint is the sharper belt on a device that
    prints one). Fail-open: missing or malformed just means the shape
    read stands alone."""
    try:
        return prints_for_app(IOS_APP)
    except Exception as e:
        log.warning("ios pages unavailable (%s) — the lock screen reads by shape", e)
        return []
