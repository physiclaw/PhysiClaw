"""The `agent` step — the model's move, inside the author's fence.

No tools = one pure-text call filling the declared `returns` (prompt
in, fields out, no screen). Tools = an EPISODE: each turn one
constrained call over the screen as the model's own turns see it — the
frame beside its whole element listing — with append-only, uncompressed
context (every earlier frame and listing stays, so every request's
prefix is byte-identical to the previous one and the provider cache
pays for all but the newest block) answered as a tool call the walk
carries out exactly: a tap (a box — a listed element's, a granted
landmark's, or one read off the screenshot — with the label the model
gives it), a scroll or back, `done`, `escalate`, or a granted pack
macro run by name. A landmark scoped to a page is shown only while
that page is the verified reading. `done` is audited against the adjacent
verify page by the matcher, never trusted, and a payment episode
re-runs the money predicates before EVERY tap or macro the model
proposes — on a verified own-pack page, as the payment move does.
"""

from physiclaw.common import gesture_vocab
from physiclaw.common.bbox import Bbox, center_of, format_bbox, inside, same_line
from physiclaw.common.listing import Element
from physiclaw.common.text import clip
from physiclaw.conductor.spec import context, match
from physiclaw.conductor.spec.calls import (
    ACT_BACK,
    ACT_SCROLL_DOWN,
    ACT_SCROLL_UP,
    AGENT_DONE,
    AGENT_TOOLS,
    ESCALATE,
    TOOL_RUN,
    TOOL_TAP,
)
from physiclaw.conductor.spec.conventions import LOCKED_ID, page_id
from physiclaw.conductor.spec.model import AgentNode, NeverTap
from physiclaw.conductor.spec.pack import qualified_macro
from physiclaw.conductor.spec.pages import Landmark
from physiclaw.conductor.spec.refs import fill_refs
from physiclaw.conductor.walk import money
from physiclaw.conductor.walk.micro import (
    ACT_ARM,
    AGENT_ACT,
    AGENT_FIELDS,
    BLOCK,
    FIELDS,
    LEAD,
    PROMPT,
    Content,
    DecisionRequest,
    Macro,
    MicroOutcome,
    Tap,
    act_block,
    act_rows,
    data_block,
    return_fields,
    settled,
)
from physiclaw.conductor.walk.step import Step, Turn, Walk
from physiclaw.conductor.walk.turns import scroll_args
from physiclaw.conductor.walk.walklog import REASON_CLIP
from physiclaw.macros.model import Macro as PackMacro

KIND_TAP = "agent-tap"
KIND_SWIPE = "agent-swipe"
KIND_MACRO = "agent-macro"


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
    targets: tuple[NeverTap, ...], rows: tuple[Element, ...], box: Bbox
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
        # sketch (see `NeverTap`), so a tap centred outside it is not on
        # the target and the rest is skipped outright.
        if target.within is not None and not _centred_in(box, target.within):
            continue
        # Which rows ARE the target: its readings, inside that same band.
        # Nothing there means nothing to refuse — it cannot be tapped
        # when it is not on the screen. A row found is refused across the
        # control it labels (`_control`), not just its own text: the
        # model boxes what it sees, and a button is wider than its word.
        for row in match.candidate_rows(target.anchor, rows, ()):
            if _centred_in(box, _control(row, rows, target.within)):
                return f"that box presses {' / '.join(target.label)}, not this step's to tap."
    return None


def macro_refusal(
    targets: tuple[NeverTap, ...], rows: tuple[Element, ...], macro: PackMacro
) -> str | None:
    """Why running a granted macro is refused, or None: each tap the
    macro records (`Macro.taps`) is judged as if the model had proposed
    it, against the screen the macro would start on. Parse refused a
    macro whose label NAMES a target (`route._guard_grants`); this is
    the box the label could not tell — the same recorded coordinates
    under another name."""
    for tap in macro.taps():
        said = refusal(targets, rows, tap.bbox)
        if said is not None:
            return f"macro {macro.name!r} taps {' / '.join(tap.label)!r} — {said}"
    return None


class AgentStep(Step[AgentNode]):
    kinds = frozenset({KIND_TAP, KIND_SWIPE, KIND_MACRO})

    def __init__(self, walk: Walk, node: AgentNode) -> None:
        super().__init__(walk, node)
        # Episode state. `history` is the append-only transcript of
        # settled (user content, model reply) pairs — replayed verbatim
        # on every call; `lead` and `block` are the pending user turn
        # the next call sends (what happened or the brief, then the
        # screen: the walk's frame between them, the listing after);
        # `sent` is the request in flight, settled whole at resolve.
        # `consented` is a payment episode's bound, stashed at open so
        # the per-tap predicates keep checking after the gate's consent
        # is consumed by the first fire.
        self.lead = ""
        self.block = ""
        self.sent: DecisionRequest | None = None
        self.history: list[tuple[str, Content]] = []
        self.elements: tuple[Element, ...] = ()
        self.calls = 0
        self.scrolls = 0
        self.consented: float | None = None
        self.seen: tuple[float, ...] = ()
        self.pending_desc = ""

    def open(self) -> Turn:
        if not self.node.tools:
            # A pure-text call: no screen, no page contract — prompt in,
            # declared fields out.
            return self._fields_request()
        gate = self.walk.enter_gate(self.node)
        if gate is not None:
            return gate
        return self._episode_start()

    def landed(self, kind: str) -> Turn:
        # An episode action landed: its own result view is the next
        # turn's screen block.
        return self._episode_landed()

    def resolve(self, outcome: MicroOutcome | None) -> Turn:
        if self.node.tools:
            return self._episode_resolve(outcome)
        return self._fields_done(outcome)

    # ---- shared ----

    def _prompt(self, vals: dict[str, str]) -> str:
        """The authored prompt with its refs filled ONCE — then frozen."""
        return str(
            fill_refs(self.node.prompt, vals, where=f"agent {self.node.id!r} `prompt`")
        )

    def _fields(self) -> str:
        return "\n".join(f"- {n}: {d}" for n, d in self.node.returns)

    def _context(self) -> str:
        """What the author declared beside the prompt (`context:`),
        loaded now — nothing else of the agent's memory travels."""
        return context.load(self.node.context)

    def _close(self, outcome: MicroOutcome, *, calls: int = 0) -> Turn:
        """The one done tail both forms share: record the returns,
        journal, advance the cursor."""
        node, walk = self.node, self.walk
        payload = outcome.payload or {}
        missing = [n for n in node.return_fields if not payload.get(n, "").strip()]
        if missing:
            # A missing field hands over instead of retrying: escalation
            # is the default, never a guess.
            return walk.handover(
                f"agent {node.id!r} answered done without return field(s) "
                f"{', '.join(missing)}"
            )
        for n in node.return_fields:
            walk.decide(node.id, n, payload[n].strip())
        after = f" after {calls} calls" if calls else ""
        walk.journal(f"agent {node.id}: done{after} — {outcome.reason}")
        return walk.advance_cursor()

    # ---- the pure-text call ----

    def _fields_request(self) -> Turn:
        node = self.node
        return DecisionRequest(
            call=AGENT_FIELDS,
            node_id=node.id,
            outcomes=(),
            material={
                PROMPT: self._prompt(self.walk.ref_values()),
                FIELDS: self._fields(),
            },
            context=self._context(),
            thinking=node.think,
        )

    def _fields_done(self, outcome: MicroOutcome | None) -> Turn:
        node = self.node
        if outcome is None:
            return self.walk.handover(
                f"agent {node.id!r}: call failed or under-confident"
            )
        if outcome.out != AGENT_DONE:
            return self.walk.handover(_escalated(node.id, outcome.reason))
        return self._close(outcome)

    # ---- the episode ----

    def _episode_start(self) -> Turn:
        """Open an acting episode on the current (enter-verified) screen.
        The prompt's refs fill ONCE here — the brief (prompt, return
        fields, declared context) heads the first block and rides the
        replayed history verbatim; a payment episode additionally gets
        {ask.total} — the consented amount its adjacent gate bound — and
        stashes that bound for the per-tap predicates."""
        node, walk = self.node, self.walk
        vals = walk.ref_values()
        if node.irreversible == "payment":
            if walk.gate.consented is None:
                return walk.handover(
                    f"agent {node.id!r}: payment episode without bound consent"
                )
            self.consented = walk.gate.consented
            self.seen = walk.gate.seen
            vals = {**vals, "ask.total": f"{self.consented:g}"}
        brief = [self._prompt(vals)]
        if self.node.returns:
            brief.append(return_fields(self._fields()))
        loaded = self._context()
        if loaded:
            brief.append(data_block("Context", loaded))
        self._screen_block("\n\n".join(brief))
        return self._request()

    def _screen_block(self, lead: str) -> None:
        """Read the CURRENT screen into the pending user turn: `lead` (what happened, or
        the brief), then the screen — the walk's frame rides between
        lead and listing when the read carried one. When the episode
        may tap, the model answers a box (a listed element's, a granted
        landmark's, or one it reads off the frame) and the journal keeps
        the model's own label for what it tapped; a page-scoped landmark is shown
        only while its page is the verified reading; with no tap tool
        there is nothing to tap. Granted macros are always answerable,
        by name."""
        walk, node = self.walk, self.node
        assert walk.screen is not None
        rows = act_rows(walk.screen.rows)
        can_tap = TOOL_TAP in node.tools
        give = tuple(n for n in node.give if self._granted(n)) if can_tap else ()
        self.elements = rows
        parts = [act_block("Current screen", rows)]
        if give:
            # Each with what it reads and where it sits: the model taps a
            # landmark's box like any other box.
            parts.append(
                "Granted landmarks (spots the playbook knows; tap their box):\n"
                + "\n".join(f"- {n}: {_landmark_line(walk.landmarks[n])}" for n in give)
            )
        if node.macros:
            parts.append(
                'Granted macros (run_macro with "name"): ' + ", ".join(node.macros)
            )
        self.lead = lead
        self.block = "\n".join(parts)

    def _granted(self, name: str) -> bool:
        """Whether a landmark is on offer NOW: declared, and either
        unscoped or scoped to the page the current verdict reads."""
        walk = self.walk
        landmark = walk.landmarks.get(name)
        if landmark is None:
            return False
        if landmark.page is None:
            return True
        return walk.verdict is not None and walk.verdict.matches(
            page_id(walk.app, landmark.page)
        )

    def _again(self, why: str) -> Turn:
        """A move the walker would not make: say why and re-ask over the
        same screen. One call spent, the episode goes on — never a
        handover, which is what put a free model beside a pay button."""
        self.lead = f"{why} Continue toward the goal, or escalate."
        return self._request()

    def _request(self) -> Turn:
        node = self.node
        self.calls += 1
        if self.calls > node.max_calls:
            return self.walk.handover(
                f"agent {node.id!r} exceeded its call limit ({node.max_calls})"
            )
        # The tools this turn may call: the granted ones, the macro run
        # when macros are granted, and the two exits.
        actions = [AGENT_DONE, ESCALATE]
        actions.extend(t for t in AGENT_TOOLS if t in node.tools)
        if node.macros:
            actions.append(TOOL_RUN)
        self.sent = DecisionRequest(
            call=AGENT_ACT,
            node_id=node.id,
            outcomes=tuple(actions),
            material={LEAD: self.lead, BLOCK: self.block},
            macros=tuple(node.macros),
            elements=self.elements,
            frame=self.walk.frame,
            history=tuple(self.history),
            thinking=node.think,
        )
        return self.sent

    def _episode_resolve(self, outcome: MicroOutcome | None) -> Turn:
        node, walk = self.node, self.walk
        if outcome is None:
            return walk.handover(f"agent {node.id!r}: call failed or under-confident")
        # Settle the turn into the append-only history: the user turn
        # exactly as micro composed and sent it (frame included), then
        # the reply in the contract's canonical spelling (micro
        # re-serializes it, so repair-retry noise never enters the
        # replayed prefix).
        assert self.sent is not None  # resolve follows the request it answers
        self.history.extend(settled(self.sent, outcome))
        if outcome.out == ESCALATE:
            return walk.handover(_escalated(node.id, outcome.reason))
        if outcome.out == ACT_BACK:
            # The OS back edge-swipe — the reliable pop on iOS (a corner
            # chevron tap misses too often to trust the stylus with).
            self.pending_desc = "went back"
            return walk.synth(
                KIND_SWIPE,
                f"conductor: agent {node.id} — went back",
                gesture_vocab.GO_BACK,
                {},
            )
        if outcome.out in (ACT_SCROLL_DOWN, ACT_SCROLL_UP):
            self.scrolls += 1
            if self.scrolls > node.max_scrolls:
                return walk.handover(
                    f"agent {node.id!r} exceeded its scroll limit ({node.max_scrolls})"
                )
            # scroll_down = see content further down = the swipe goes up.
            down = outcome.out == ACT_SCROLL_DOWN
            self.pending_desc = "scrolled down" if down else "scrolled up"
            return walk.synth(
                KIND_SWIPE,
                f"conductor: agent {node.id} — {self.pending_desc}",
                gesture_vocab.SWIPE,
                scroll_args(down=down),
            )
        if outcome.out == AGENT_DONE:
            # The exit contract is the matcher's, never the model's: done
            # counts only on the adjacent verify page. A rejection costs
            # one call and the episode continues.
            wrong = (
                walk.mismatch(walk.verdict, page_id(walk.app, node.verify))
                if walk.verdict is not None
                else "no screen observed"
            )
            if wrong is not None:
                # Same screen, same listing: only the lead changes.
                return self._again(
                    f"done rejected: the walk must be on {node.verify!r} — {wrong}."
                )
            return self._close(outcome, calls=self.calls)
        assert outcome.out == ACT_ARM and outcome.picked is not None
        assert walk.screen is not None
        picked = outcome.picked
        # The model's own tap, or the taps a granted macro records —
        # one rule (`refusal`), BEFORE the payment block, so a refusal
        # cannot spend the consent it was guarding. A granted macro the
        # walk cannot find has nothing to judge; its run fails on its
        # own when dispatched.
        if isinstance(picked, Tap):
            refused = refusal(node.never_tap, walk.screen.rows, picked.bbox)
            what, named = "a tap", picked.label
        else:
            macro = walk.pack_macros.get(qualified_macro(walk.app, picked.name))
            refused = (
                macro_refusal(node.never_tap, walk.screen.rows, macro)
                if macro is not None
                else None
            )
            what, named = "a macro", picked.name
        if refused is not None:
            walk.ledger.refuse(named)
            walk.journal(f"agent {node.id}: refused {what} — {refused}")
            return self._again(refused)
        if node.irreversible == "payment":
            # The purse stays with the walker: the same two checks the
            # payment move runs, before every tap or macro the model
            # proposes. The page first — the enter gate verified it once
            # at the episode's start, and a scroll or a tap since may
            # have landed on a screen the pack never declared, where a
            # matching amount proves nothing. Then the amounts: one while
            # no visible amount equals the consented total, or with any
            # amount above it, is refused.
            blocked = walk.money_page_block(f"payment agent {node.id!r}")
            if blocked is None:
                blocked = money.fire_block(
                    consented=self.consented, seen=self.seen, screen=walk.screen
                )
            if blocked is not None:
                return walk.handover(f"payment agent {node.id!r}: {blocked}")
            # Consent is consumed by the FIRST fire — a later payment
            # needs its own gate (the move rule, episode-shaped).
            walk.spend_consent()
        if isinstance(picked, Macro):
            # A granted pack macro: the recorded gesture sequence runs
            # whole, argument-less; its result view is the next screen.
            self.pending_desc = f"ran macro {picked.name!r}"
            return walk.synth(
                KIND_MACRO,
                f"conductor: agent {node.id} — {self.pending_desc}",
                gesture_vocab.RUN_MACRO,
                {"name": qualified_macro(walk.app, picked.name)},
            )
        assert isinstance(picked, Tap)
        bbox = picked.bbox
        # Exactly what the model said: its label, its box.
        self.pending_desc = f"tapped {picked.label!r} at {format_bbox(bbox)}"
        return walk.synth(
            KIND_TAP,
            f"conductor: agent {node.id} — {self.pending_desc}",
            "tap",
            {"bbox": list(bbox)},
        )

    def _episode_landed(self) -> Turn:
        """An episode action's own result view becomes the next turn's
        screen block — no extra peek."""
        node, walk = self.node, self.walk
        assert walk.screen is not None
        # Whatever happens next, money may have moved: the purchase line
        # lands the moment the first consented tap's result does.
        walk.log_purchase()
        if walk.verdict is not None and walk.verdict.matches(LOCKED_ID):
            return walk.handover(f"agent {node.id!r}: phone locked mid-episode")
        self._screen_block(f"[you {self.pending_desc}]")
        return self._request()


def _escalated(node_id: str, reason: str) -> str:
    """The handover reason for a model's escalate — its own words,
    quoted as such and clipped to what any record keeps of a reason
    (`walklog.REASON_CLIP`): it lands in the brief the driving model
    resumes from, so a sentence's worth, never a page."""
    return f"agent {node_id!r} escalated, saying {clip(reason, REASON_CLIP)!r}"


def _landmark_line(landmark: Landmark) -> str:
    """A granted landmark as the block shows it: its readings (the text
    it carries, or the author's description of the spot) and its
    declared box, the listing's spelling."""
    reads = " / ".join(f'"{r}"' for r in landmark.label) or '""'
    return f"reads {reads}, box {format_bbox(landmark.bbox)}"
