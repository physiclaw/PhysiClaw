"""The `agent` step — the model's move, inside the author's fence.

No tools = one pure-text call filling the declared `returns` (prompt
in, fields out, no screen). Tools = an EPISODE: each turn one
constrained call over the screen as the model's own turns see it — the
frame beside its whole element listing — with append-only, uncompressed
context (every earlier frame and listing stays, so every request's
prefix is byte-identical to the previous one and the provider cache
pays for all but the newest block) answered as a tool call the walk
carries out exactly: a tap (a box — a listed element's, a landmark's
the step's `given:` named, or one read off the screenshot — with the
label the model gives it), a scroll or back, `done`, `escalate`, or a
macro its `tools:` granted, run by name. Both grants are FIXED for the
episode, so each is said once — the landmark in the brief where the
prompt writes its name, the macro in the system prompt's legend — and
every turn after the first carries the screen alone. `done` is audited against the adjacent
verify page by the matcher, never trusted, and a payment episode
re-runs the money predicates before EVERY tap or macro the model
proposes — on a verified own-pack page, as the payment move does.
"""

from physiclaw.common import gesture_vocab
from physiclaw.common.bbox import format_bbox
from physiclaw.common.listing import Element
from physiclaw.common.text import clip
from physiclaw.conductor.micro import prompts
from physiclaw.conductor.micro.blocks import (
    act_block,
    act_rows,
    data_block,
    labelled,
    return_fields,
)
from physiclaw.conductor.micro.compose import settled
from physiclaw.conductor.micro.decision import (
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
)
from physiclaw.conductor.spec import fence
from physiclaw.conductor.spec.calls import (
    ACT_BACK,
    ACT_SCROLL_DOWN,
    ACT_SCROLL_UP,
    AGENT_DONE,
    AGENT_TOOLS,
    ESCALATE,
    TOOL_RUN,
)
from physiclaw.conductor.spec.conventions import LOCKED_ID, page_id
from physiclaw.conductor.spec.limits import REASON_CLIP
from physiclaw.conductor.spec.model import AgentNode
from physiclaw.conductor.spec.pack import qualified_macro
from physiclaw.conductor.spec.refs import fill_names, fill_refs
from physiclaw.conductor.steps import memory
from physiclaw.conductor.walk import money
from physiclaw.conductor.walk.surface import Step, Turn, Walk
from physiclaw.conductor.walk.turns import scroll_args

KIND_TAP = "agent-tap"
KIND_SWIPE = "agent-swipe"
KIND_MACRO = "agent-macro"


class AgentStep(Step[AgentNode]):
    kinds = frozenset({KIND_TAP, KIND_SWIPE, KIND_MACRO})

    def __init__(self, walk: Walk, node: AgentNode) -> None:
        super().__init__(walk, node)
        # Episode state. `history` is the append-only transcript of
        # settled (user content, model reply) pairs — replayed verbatim
        # on every call; `lead` and `elements` are the pending user turn
        # the next call sends (what happened or the brief, then the
        # screen: the walk's frame between them, the listing after);
        # `sent` is the request in flight, settled whole at resolve.
        # `consented` is a payment episode's bound, stashed at open so
        # the per-tap predicates keep checking after the gate's consent
        # is consumed by the first fire.
        self.lead = ""
        self.sent: DecisionRequest | None = None
        self.history: list[tuple[str, Content]] = []
        self.elements: tuple[Element, ...] = ()
        self.calls = 0
        self.scrolls = 0
        self.consented: float | None = None
        self.total_label: tuple[str, ...] = ()
        self.pending_desc = ""

    def open(self) -> Turn:
        if not self.node.acts:
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
        if self.node.acts:
            return self._episode_resolve(outcome)
        return self._fields_done(outcome)

    # ---- shared ----

    def _fields(self) -> str:
        return "\n".join(labelled(n, d) for n, d in self.node.returns)

    def _brief(self, vals: dict[str, str]) -> str:
        """The prompt with its `given:` filled ONCE — each `{name}` the
        value its ref reads now, or a landmark's reading and box, stated
        as the fact it is (what MAY be tapped is the tap legend's to
        say) — then frozen for the step."""
        node, walk = self.node, self.walk
        where = f"agent {node.id!r} `context.given`"
        given = {
            name: str(fill_refs(ref, vals, where=f"{where}.{name}"))
            for name, ref in node.given.items()
        }
        given |= {
            name: walk.landmarks[spot].describe()
            for name, spot in node.landmarks.items()
        }
        return fill_names(
            node.prompt, given, where=f"agent {node.id!r} `context.prompt`"
        )

    def _context(self) -> str:
        """The step's `context.memory:` read NOW, rendered one `- name:
        body` per part (a body of several lines indented under its
        name). "" when the step declared none, so there is never an
        empty heading. The caller stamps the whole thing as data — the
        agent's own memory is fact to judge, never instruction, and
        nothing else of it travels. The brief is NOT in here."""
        return "\n".join(
            labelled(part, body) for part, body in memory.load(self.node.memory).items()
        )

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
                PROMPT: self._brief(self.walk.ref_values()),
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
        The prompt's givens fill ONCE here — the brief (prompt, return
        fields, declared context) heads the first block and rides the
        replayed history verbatim; a payment episode additionally gets
        {ask.total} — the consented amount its adjacent gate bound — and
        stashes that bound for the per-tap predicates."""
        node, walk = self.node, self.walk
        vals = walk.ref_values()
        if node.pays:
            if walk.gate.consented is None:
                return walk.handover(
                    f"agent {node.id!r}: payment episode without bound consent"
                )
            self.consented = walk.gate.consented
            self.total_label = walk.gate.total_label
            vals = {**vals, "ask.total": money.plain(self.consented)}
        brief = [self._brief(vals)]
        if self.node.returns:
            brief.append(return_fields(self._fields()))
        reads = self._context()
        if reads:
            brief.append(data_block(prompts.CONTEXT_HEADER, reads))
        self._screen_block("\n\n".join(brief))
        return self._request()

    def _screen_block(self, lead: str) -> None:
        """Read the CURRENT screen into the pending user turn: `lead`
        (what happened, or the brief), then the screen — the walk's
        frame rides between lead and listing when the read carried one."""
        walk = self.walk
        assert walk.screen is not None
        self.elements = act_rows(walk.screen.rows)
        self.lead = lead

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
            material={
                LEAD: self.lead,
                BLOCK: act_block("Current screen", self.elements),
            },
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
                walk.verdict.mismatch(page_id(walk.app, node.verify))
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
            refused = fence.refusal(node.never_tap, walk.screen.rows, picked.bbox)
            what, named = "a tap", picked.label
        else:
            macro = walk.pack_macros.get(qualified_macro(walk.app, picked.name))
            refused = (
                fence.macro_refusal(node.never_tap, walk.screen.rows, macro)
                if macro is not None
                else None
            )
            what, named = "a macro", picked.name
        if refused is not None:
            walk.ledger.refuse(named)
            walk.journal(f"agent {node.id}: refused {what} — {refused}")
            return self._again(refused)
        if node.pays:
            # The purse stays with the walker: the payment move's guard,
            # before every tap or macro the model proposes — the enter
            # gate verified the page once at the episode's start, and a
            # scroll or a tap since may have landed anywhere. Against
            # the consent stashed at the start, which the first tap
            # spends from the gate.
            blocked = money.payment_block(
                walk.verdict,
                walk.screen,
                walk.app,
                f"payment agent {node.id!r}",
                consented=self.consented,
                total_label=self.total_label,
            )
            if blocked is not None:
                return walk.handover(blocked)
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
    (`limits.REASON_CLIP`): it lands in the brief the driving model
    resumes from, so a sentence's worth, never a page."""
    return f"agent {node_id!r} escalated, saying {clip(reason, REASON_CLIP)!r}"
