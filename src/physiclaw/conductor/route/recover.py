"""Declared recovery — a page's hands (`recover:` / `tries:` /
`on_fail:`) from the manifest, a route's own `pages:` block or a
waypoint, layered in that order; and the hand grammar itself (a bare
gesture, a landmark tap, a macro).
"""

from dataclasses import replace
from typing import Any

from physiclaw.common import gesture_vocab
from physiclaw.conductor.route.fields import limit_int, on_fail_mode
from physiclaw.conductor.route.resolve import (
    argless_macro,
    landmark_name,
    macro_resolver,
)
from physiclaw.conductor.route.scope import Scope
from physiclaw.conductor.spec.limits import (
    DEFAULT_RECOVER_LIMIT,
    MAX_RECOVER_ACTIONS,
)
from physiclaw.conductor.spec.model import (
    READING_COVERED,
    READING_ELSEWHERE,
    READING_LOCKED,
    RECOVER_READINGS,
    PlaybookError,
    RecoverHand,
    Recovery,
    require_str,
)
from physiclaw.conductor.spec.pack import Pack
from physiclaw.macros.model import (
    Macro,
    MacroError,
)

# The declared-recovery hand, in a macro step's own shape: a bare
# gesture, `{tap: app.landmarks.<name>}`, or `{macro: app.macros.<name>}`. Closed
# vocabulary — a recover hand resets state, it does not navigate (the
# route does that). A page declares one hand for any deviation, or one
# per reading (`covered:` / `elsewhere:` / `locked:`), plus its own
# `tries:`.
BARE_HANDS = tuple(sorted(gesture_vocab.NAV_TOOLS | {gesture_vocab.UNLOCK_PHONE}))

_HAND_KEYS = {"tap", "macro"}

_RECOVER_KEYS = _HAND_KEYS | set(RECOVER_READINGS)


def declared_once(
    prior: Recovery | None, declared: Recovery, where: str, page: str
) -> Recovery:
    """A page's recovery, declared at every waypoint of that page: a
    later waypoint may add what an earlier left unsaid (a hand, an
    `on_fail`), never contradict it."""
    if prior is None:
        return declared
    if declared.hands and prior.hands and _hands_of(declared) != _hands_of(prior):
        raise PlaybookError(
            f"{where}: page {page!r} declares `recover` twice with different "
            "hands — declare it once"
        )
    if (
        declared.on_fail is not None
        and prior.on_fail is not None
        and declared.on_fail != prior.on_fail
    ):
        raise PlaybookError(
            f"{where}: page {page!r} declares `on_fail` twice with different "
            "words — declare it once"
        )
    return _overlay_one(prior, declared)


def _hands_of(r: Recovery) -> tuple:
    return (r.covered, r.elsewhere, r.locked, r.tries)


def _overlay_one(base: Recovery, over: Recovery) -> Recovery:
    """`over`'s hands where it declares any (else `base`'s), and its
    `on_fail` where said."""
    hands = over if over.hands else base
    return replace(
        hands, on_fail=over.on_fail if over.on_fail is not None else base.on_fail
    )


def overlay(
    base: dict[str, Recovery], over: dict[str, Recovery]
) -> dict[str, Recovery]:
    """The manifest's page recovery under the route's own: a route
    inherits a shared page's hands and `on_fail` unless it declares its
    own."""
    out = dict(base)
    for page, r in over.items():
        out[page] = _overlay_one(base[page], r) if page in base else r
    return out


def route_defaults(scope: Scope, own: dict[str, dict]) -> dict[str, Recovery]:
    """What the route's waypoints start from: the manifest's hands every
    route inherits (resolved once, at pack load — `manifest_recovers`)
    under the hands its own `pages:` block declares, parsed with the
    route's own resolver."""
    return overlay(dict(scope.pack.recovers), _page_hands(scope, own, "route"))


def _page_hands(scope: Scope, raw: dict[str, dict], site: str) -> dict[str, Recovery]:
    """The hands a `pages:` mapping declares, by page — the
    `recovery_fields` slice of each, the pages declaring none skipped."""
    return {
        name: parse_recover(scope, fields, f"{site} page {name!r}", name)
        for name, fields in raw.items()
        if fields
    }


def manifest_recovers(pack: Pack, raw: dict[str, dict]) -> dict[str, Recovery]:
    """The manifest's `pages: <name>: recover:` hands, resolved with the
    pack's own resolver — `app.macros.<name>` and `app.landmarks.<name>`,
    the one spelling everywhere. The one hand grammar
    (`parse_recover`), the pack's one resolver. A manifest
    carries settings, never bodies: an inline `macro: {steps: ...}` is
    refused. Raises PlaybookError naming the page."""
    pack_macro = macro_resolver(pack.macros, pack.macro_errors)

    def resolve(raw: Any, where: str, nid: str, role: str | None = None) -> Macro:
        try:
            return pack_macro(require_str(raw, f"{where}: `{role or 'macro'}`"))
        except MacroError as e:
            raise PlaybookError(f"{where}: {role or 'macro'} {e}") from e

    for name, spec in raw.items():
        _refuse_bodies(spec.get("recover"), f"manifest page {name!r}")
    return _page_hands(Scope("", pack, set(), resolve), raw, "manifest")


def _refuse_bodies(raw: Any, where: str) -> None:
    """A manifest hand names a pack macro; it never embeds one."""
    if not isinstance(raw, dict):
        return
    if isinstance(raw.get("macro"), dict):
        raise PlaybookError(
            f"{where}: the manifest names a pack macro for a recover hand — "
            "record the body as macros/<name>.yml and name it here"
        )
    for reading in RECOVER_READINGS:
        _refuse_bodies(raw.get(reading), where)


def parse_recover(scope: Scope, fields: dict, where: str, page: str) -> Recovery:
    """A page's `recover:`, `tries:` and `on_fail:` (the
    `PAGE_RECOVERY_FIELDS` slice of its mapping, in a route waypoint or
    the manifest alike). `recover:` is one hand for any deviation (a
    bare gesture, `{tap: app.landmarks.<name>}`, or `{macro: app.macros.<name>}`), or
    one per reading (`covered:` the page itself under a sheet or popup,
    `locked:` the phone's lock screen, `elsewhere:` any other screen);
    `tries:` beside it is the page's own bound under the walk-wide
    ceiling, and means nothing without a hand to count; `on_fail:` is
    the page's word once they are spent — legal on its own."""
    on_fail = on_fail_mode(fields, where)
    if "recover" not in fields:
        if "tries" in fields:
            raise PlaybookError(
                f"{where}: `tries` bounds a `recover:` — declare the hand it counts"
            )
        return Recovery(on_fail=on_fail)
    raw = fields["recover"]
    tries = limit_int(
        fields.get("tries", DEFAULT_RECOVER_LIMIT),
        f"{where}: `tries`",
        1,
        MAX_RECOVER_ACTIONS,
    )
    if isinstance(raw, dict):
        unknown = sorted(set(map(str, raw)) - _RECOVER_KEYS)
        if unknown:
            hint = (
                " — `tries` sits beside `recover`, not inside"
                if "tries" in unknown
                else ""
            )
            raise PlaybookError(
                f"{where}: `recover`: unknown key(s): {', '.join(unknown)}{hint}"
            )
        keyed = [k for k in RECOVER_READINGS if k in raw]
        if keyed:
            if set(raw) - set(keyed):
                raise PlaybookError(
                    f"{where}: `recover` declares one hand OR one per reading "
                    f"({', '.join(RECOVER_READINGS)}), not both"
                )
            hands = {
                k: _parse_hand(
                    scope, raw[k], f"{where}: `recover.{k}`", page, role=f"recover.{k}"
                )
                for k in keyed
            }
            return Recovery(
                covered=hands.get(READING_COVERED),
                elsewhere=hands.get(READING_ELSEWHERE),
                locked=hands.get(READING_LOCKED),
                tries=tries,
                on_fail=on_fail,
            )
    # A bare gesture, `{tap: ...}` / `{macro: ...}`, or a non-hand — the
    # one hand parser judges the shape and names the alternatives.
    hand = _parse_hand(scope, raw, f"{where}: `recover`", page)
    return Recovery(
        covered=hand, elsewhere=hand, locked=hand, tries=tries, on_fail=on_fail
    )


def _parse_hand(
    scope: Scope, raw: Any, where: str, page: str, *, role: str = "recover"
) -> RecoverHand:
    """One recovery hand, in a step's shape: a bare gesture that takes no
    object (`go_back`), `{tap: app.landmarks.<name>}` (the declared spot,
    as declared), or `{macro: app.macros.<name>}` (argument-less)."""
    if isinstance(raw, str):
        if raw == "tap":
            raise PlaybookError(
                f"{where}: a tap names its spot — `{{tap: app.landmarks.<name>}}`"
            )
        if raw not in BARE_HANDS:
            raise PlaybookError(
                f"{where}: {raw!r} is not a hand — one of {', '.join(BARE_HANDS)}, "
                "{tap: app.landmarks.<name>}, or {macro: app.macros.<name>}"
            )
        return RecoverHand(tool=raw)
    if not isinstance(raw, dict) or len(raw) != 1 or not set(raw) <= _HAND_KEYS:
        raise PlaybookError(
            f"{where} is one hand — a bare gesture ({', '.join(BARE_HANDS)}), "
            "{tap: app.landmarks.<name>}, or {macro: app.macros.<name>}"
        )
    if "macro" in raw:
        return RecoverHand(
            macro=argless_macro(raw["macro"], role, where, page, scope.resolve).name
        )
    return RecoverHand(
        tool="tap", landmark=landmark_name(scope, raw["tap"], f"{where}: `tap`")
    )
