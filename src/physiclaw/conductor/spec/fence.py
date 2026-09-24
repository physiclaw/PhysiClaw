"""The never_tap fence, judged on a live screen — the runtime half of
`never_tap:` (`AgentNode.never_tap` is the declaration, each target an
`AnchorDecl`): why a tap on a box is refused, or None, as a pure rule
over the screen's rows, so it reads and audits without the state
machine around it. The agent step asks before every tap it forwards
and, tap by recorded tap, before every granted macro it runs
(`macro_refusal`).

A target found on the screen is refused across the CONTROL its label
sits on, not just the label's own box: sideways to the next listed
element on the same row, or to the band's or the screen's edge
(`_control`). A word standing alone on its bar refuses the whole bar;
one beside another button ends where that button's label begins.

A target's `within` says where the TARGET sits, as it does on a page
anchor. SKETCH IT GENEROUSLY: it gates the tap's centre and the row's
centre alike, so a band drawn tight around its target can miss a row
sitting on the edge. Omitting it means anywhere, which is always the
safe reading — declare one only to keep a word that ALSO reads
somewhere harmless from standing in for the real control.
"""

from physiclaw.common.bbox import Bbox, center_of, inside, same_line
from physiclaw.common.listing import Element
from physiclaw.conductor.spec import match
from physiclaw.conductor.spec.pages import AnchorDecl
from physiclaw.macros.model import Macro

# How far outside a listed row's own box a tap may still be pressing it.
# A row is the TEXT's box; the button around it is taller, so a press
# aimed at the button can land just past the text. Measured against every
# tap in the recorded sessions: no legitimate move is refused anywhere up
# to 0.05, so this sits well inside the headroom.
_NEAR_ENOUGH = 0.02


def _centred_in(box: Bbox, region: Bbox, *, slack: float = 0.0) -> bool:
    """Whether a tap on `box` presses inside `region`. The press lands at
    the box's CENTRE (`core.server.tools.tap`), so that is the whole
    question — a box merely overlapping a region presses wherever its own
    centre is, which may be nothing at all."""
    center = center_of(box)
    return center is not None and inside(center, list(region), margin=slack)


def _control(row: Element, rows: tuple[Element, ...], band: Bbox | None) -> Bbox:
    """The extent of the control a target row sits on. OCR reads the
    LABEL's box; the button around it reaches sideways as far as the
    next listed element on the same row (text or icon), the band's edge,
    or the screen's — a pay bar standing alone in its footer is the whole
    footer's width, while the pay button beside the cart button ends
    where the cart button's label begins. Never narrower than the text
    plus the slack, which is what a nudge past the label needs."""
    left, top, right, bottom = row.bbox
    lo, hi = (band[0], band[2]) if band is not None else (0.0, 1.0)
    for other in rows:
        if other is row:
            continue
        if not same_line(row.bbox, other.bbox):
            continue
        o_left, _, o_right, _ = other.bbox
        if o_right <= left:
            lo = max(lo, o_right)
        elif o_left >= right:
            hi = min(hi, o_left)
        # An element overlapping the label sideways (a box drawn around
        # the whole button) bounds nothing — it IS the control.
    return (
        min(left - _NEAR_ENOUGH, lo),
        top - _NEAR_ENOUGH,
        max(right + _NEAR_ENOUGH, hi),
        bottom + _NEAR_ENOUGH,
    )


def refusal(
    targets: tuple[AnchorDecl, ...], rows: tuple[Element, ...], box: Bbox
) -> str | None:
    """Why a tap on `box` is refused, or None — a pure rule over what the
    step declared, what the screen shows and where the tap would land,
    so it reads and audits without the state machine around it. The box
    is all that matters: what the model called it is narration.

    The targets are never shown to the model: naming the pay button would
    tell it where the pay button is, so this is a guard rail and not an
    instruction."""
    for target in targets:
        # Not where this target lives — allow, next target. The band is a
        # sketch (the module docstring), so a tap centred outside it is not on
        # the target and the rest is skipped outright.
        if target.within is not None and not _centred_in(box, target.within):
            continue
        # Which rows ARE the target: its readings, inside that same band.
        # A row found is refused across the control it labels
        # (`_control`), not just its own text: the model boxes what it
        # sees, and a button is wider than its word.
        for row in match.candidate_rows(target, rows, ()):
            if _centred_in(box, _control(row, rows, target.within)):
                return f"that box presses {' / '.join(target.readings)}, not this step's to tap."
        # The target's text is not in the listing — which is NOT the same
        # as not on the screen. An orange pay pill can be detected as an
        # unlabelled icon, or lose its text to glare, and the episode is
        # told it may read a box off the screenshot: absent from the
        # listing, addressable on the frame, unbannable by the rule
        # above. So inside a band the pack declared off limits, the walk
        # presses only what it can READ: a box no listed row accounts
        # for is refused, whatever it turns out to be.
        if target.within is not None and not _accounted(box, rows, target.within):
            return (
                f"that box is in the band {' / '.join(target.readings)} sits in, and "
                "no row of the screen reads as what it would press — scroll the "
                "row it is on higher up the screen, out of that band, or aim at "
                "a listed element."
            )
    return None


def _accounted(box: Bbox, rows: tuple[Element, ...], band: Bbox) -> bool:
    """Whether some LABELLED row of the screen accounts for a tap on
    `box` — its control covers where the press would land. Icons are not
    rows for this: an unlabelled detection says something is there, not
    what it is, and the whole question here is whether we know."""
    # `_control` widens a row sideways only, so a row whose height does
    # not reach the press cannot account for it — one pass keeps the
    # O(rows²) widening to the handful of rows at that height.
    centre = center_of(box)
    if centre is None:
        return False
    at_height = [
        row
        for row in rows
        if row.label
        and row.bbox[1] - _NEAR_ENOUGH <= centre[1] <= row.bbox[3] + _NEAR_ENOUGH
    ]
    return any(_centred_in(box, _control(row, rows, band)) for row in at_height)


def macro_refusal(
    targets: tuple[AnchorDecl, ...], rows: tuple[Element, ...], macro: Macro
) -> str | None:
    """Why running a granted macro is refused, or None: each tap the
    macro records (`Macro.taps`) is judged as if the model had proposed
    it, against the screen the macro would start on. Parse refused a
    macro whose label NAMES a target (`route.agent._guard_grants`); this is
    the box the label could not tell — the same recorded coordinates
    under another name."""
    for tap in macro.taps():
        said = refusal(targets, rows, tap.bbox)
        if said is not None:
            return f"macro {macro.name!r} taps {' / '.join(tap.label)!r} — {said}"
    return None
