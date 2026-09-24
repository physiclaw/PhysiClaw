"""The playbook model — the grammar as dataclasses. A leaf: the
compiler (`route/`), the loader (`load/`), the scaffold, and the walk
all import it; it imports none of them. What a playbook validates
against is `spec/pack.py`; what a wake needs of one is `spec/live.py`.

A playbook is one app task written as a ROUTE: a top-down alternation
of waypoints (`page:` — where the walk must BE, checked every time) and
moves (what it DOES). The grammar, top-down (the YAML keys are the user
vocabulary and the model classes below carry the same names)::

    playbook  ::= description [enabled] [inputs] [pages] [returns] route
    inputs    ::= {id: {description, [default], [example]}}   # ≤ MAX_INPUTS
    route     ::= [agent...] [start] page (move page | ask | tell)*
    entry     ::= "page" name [description] [anchors] [forbid] [scrollable]
                  [recover] [tries]
                | "start" name macro          # the unconditional cold-launch
                | "do" name macro [with] [irreversible]
                | "agent" name context [tools] [returns] [limit]
                  [irreversible]             # the step handed to the model
                | "ask" name approve message yes no [denied] [total_label]
                  [wait] [rounds] [resume]    # payment: resume required when
                                              # a screen move follows
                | "tell" name message
                | "run" name [with] [each] [miss] [revise] [limit]
                                              # a playbook of this pack, walked
                                              # as one move (per item, with each)
                | "select" name [limit]       # channel/boot only, and last:
                                              # read the thread, hand a
                                              # playbook the baton
    recover   ::= hand                        # one hand for any deviation
                | {[covered: hand] [elsewhere: hand] [locked: hand]}
                # `tries:` beside it bounds the page; a manifest page's
                # recover: is inherited by every route, a route's own
                # replaces it whole for that walk
    hand      ::= go_back | force_quit | home_screen | unlock_phone
                | {tap: app.landmarks.<name>} | {macro: app.macros.<name> | own | body}
    macro     ::= name                    # macros/<name>.yml — a recorded macro (pack or playbook)
                | {[inputs] steps}        # inline — the macro-file grammar minus
                                          # name/description/enabled

The route's shape IS the contract: an optional prefix of pure-text
`agent` steps and one `start` opens it, the first page is the start
contract, every `do` and every acting `agent` is followed by the page
it lands on (its landing check — the reflector, enforced by shape),
and a page may declare its own `recover:` hand — one hand for any
deviation, or one per reading (`covered:` a sheet over the page
itself, `locked:` the phone's lock screen, `elsewhere:` any other
screen), each page bounded by its own `tries:` under the walk-wide
ceiling. Moves fall through in
route order — there is no routing and no loop; whatever needs judgment
is an `agent` step inside the author's fence, and whatever needs a
human is an `ask`. Money runs in code: an `irreversible: payment` move
directly follows the `ask` that approves it (`lints.check_money`).

What the playbook declares is what runs — no more, no less. A page
without `recover:` hands over; an agent step runs the author's prompt
with the tools, landmarks, macros, and context the author listed and
nothing else; an ask's reply is read against the `yes:`/`no:` words
the ask declares, and anything they do not cover is the model's to
read; a payment ask names the label its `total_label:` sits beside, and
`wait:` and `rounds:` are its own patience.

Wiring is by placeholder, and every ref is dotted: `{inputs.name}` reads
a declared input, `{move.field}` reads an EARLIER agent step's declared
return field, `{ask.total}` a payment ask's quoted total. A
bare `{name}` is a load error. Dotted refs are playbook-level — resolved
to plain strings before any macro sees them, so pack macros keep the
stock single-name template grammar.

The loader (`load/pack.py`) turns files into these; the compiler
(`route/`) and its lints turn a `route:` into the nodes.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from physiclaw.common import paths
from physiclaw.conductor.spec import specfile
from physiclaw.conductor.spec.limits import (
    DEFAULT_ASK_ROUNDS,
    DEFAULT_ASK_WAIT_SECONDS,
    DEFAULT_RECOVER_LIMIT,
    DEFAULT_RUN_ROUNDS,
)
from physiclaw.conductor.spec.pages import AnchorDecl
from physiclaw.contract.dto import Thinking
from physiclaw.macros import inputs as macro_inputs
from physiclaw.macros.model import Macro, MacroError, MacroInput

# The three readings a page's `recover:` may key its hands by: the page
# itself under an overlay, the phone's lock screen (where taps do not
# land — only `unlock_phone` helps), or any other screen.
READING_COVERED = "covered"
READING_ELSEWHERE = "elsewhere"
READING_LOCKED = "locked"
RECOVER_READINGS = (READING_COVERED, READING_ELSEWHERE, READING_LOCKED)
# The one irreversible class: money. A payment move is entered only as
# the fall-through of an `ask` with `approve: payment` (`lints.py`).
PAYMENT = "payment"
IRREVERSIBLE_CLASSES = (PAYMENT,)
# A route line's `on_fail:` — once its own means are spent, `handover`
# (also when unsaid) briefs the model, `stop` ends the session by the
# walk's own hand. The playbook decides, line by line (README).
ON_FAIL_STOP = "stop"
ON_FAIL_MODES = ("handover", ON_FAIL_STOP)
# A `run`'s `miss:` — the third exit word, legal on a run with `each`
# only: a failed round is recorded as missed and the walk goes on.
ON_FAIL_SKIP = "skip"
MISS_MODES = (ON_FAIL_SKIP,)
# The ref grammar's one global root — `{inputs.name}` — rejected as a
# move name so an agent's `{move.field}` outputs can never shadow it.
# (`ask` is not global: it exists only inside a payment ask's own
# messages, where the money slots win.)
INPUTS_ROOT = "inputs"


class PlaybookError(specfile.SpecError):
    """A playbook (or its pack wiring) is invalid. Message is user-facing:
    `physiclaw playbooks check` prints it verbatim. All-or-nothing."""


# The scalar terminals, bound once for the whole playbook grammar —
# `route/` (the compiler) reads them from here.
require_str, prose, opt_prose, check_name = specfile.bind(PlaybookError)


# ---------- the model ----------


# A playbook's `inputs:` IS the macro grammar's — one shape, one parser
# (`macros.parse.parse_inputs`), and the resolver reads either.
PlaybookInput = MacroInput


@dataclass(frozen=True)
class DoNode:
    """A `do` move (and the `start` move — a do with no `enter`, run
    unconditionally): one recorded macro, framed by the pages before
    and after it."""

    id: str
    macro: str  # the dispatch name — a directory macro or the inline body's
    args: dict  # `with:` — ref templates, filled at run time
    enter: str  # the page the move starts on; "" = unconditional (`start`)
    verify: str  # the page it must land on
    irreversible: str | None = None
    on_fail: str | None = None  # `on_fail:` — see ON_FAIL_MODES

    @property
    def start(self) -> bool:
        return not self.enter

    @property
    def pays(self) -> bool:
        """Whether this move fires the payment its adjacent ask consented to."""
        return self.irreversible == PAYMENT


@dataclass(frozen=True)
class AgentNode:
    """An `agent` move — the model's step, inside the author's fence.
    No `tools` = one pure-text call (`prompt` in, `returns` out); tools =
    an acting EPISODE framed by `enter`/`verify` exactly like a `do`."""

    id: str
    prompt: str
    tools: tuple[str, ...]

    returns: tuple[tuple[str, str], ...]  # (field, description)
    enter: str
    verify: str
    max_calls: int
    max_scrolls: int
    irreversible: str | None = None
    # `never_tap:` — the targets this episode's taps may never press,
    # the opposite of a grant: each the target shape a page anchor takes
    # (readings, and optionally the band it sits in), since the one row
    # matcher finds both. Enforced by `spec.fence.refusal`, which owns
    # the rule and says why they stay unnamed to the model.
    never_tap: tuple[AnchorDecl, ...] = ()
    # `context.given:` — the values the prompt may name, name to ref
    # template: each `{name}` in the prompt is filled from it once when
    # the step opens, and the parser holds the two to each other. A
    # landmark given lives in `landmarks` below, same block, same rule.
    given: dict[str, str] = field(default_factory=dict)
    # `context.memory:` — which parts of the agent's own memory travel
    # (`spec.memory.memory_gap` owns the vocabulary).
    memory: dict[str, Any] = field(default_factory=dict)
    # The landmark givens (`<name>: app.landmarks.<n>`), name to
    # declared landmark: the fixed spots this step may aim a tap at,
    # each rendered into the prompt where it writes `{name}` as the
    # reading and box it is. The permission stays in the tap legend.
    landmarks: dict[str, str] = field(default_factory=dict)
    # Macros this episode may run — `tools: [macros.<name>]` /
    # `[app.macros.<name>]`, one menu with the gesture words.
    macros: tuple[str, ...] = ()
    # `think:` — how much hidden thinking each of its calls asks the
    # model for; None = the vendor's default for that model.
    think: Thinking | None = None
    on_fail: str | None = None  # `on_fail:` — see ON_FAIL_MODES

    @property
    def acts(self) -> bool:
        """Whether this step touches the SCREEN — the one predicate the
        parser and the walker share. A granted macro is a hand like any
        gesture, so a step that only runs one is an episode too; without
        either it is a pure-text call, framed by no pages."""
        return bool(self.tools or self.macros)

    @property
    def return_fields(self) -> tuple[str, ...]:
        return tuple(f for f, _ in self.returns)

    @property
    def pays(self) -> bool:
        """Whether this episode's taps fire the payment its adjacent ask
        consented to."""
        return self.irreversible == PAYMENT


@dataclass(frozen=True)
class AskNode:
    """An `ask` move — message the user and hold for approval. `approve`
    names the class the reply consents to (`payment` binds the quoted
    total); `yes`/`no` are the whole-message replies that open or close
    the gate, in `reply.normalize` space (anything else is the model's);
    `denied` is the line sent back on a no, before the entry's `on_fail`
    word decides what the walk does next; `resume` is the macro that
    re-enters the app afterwards."""

    id: str
    approve: str
    message: str
    yes: tuple[str, ...]
    no: tuple[str, ...]
    # `denied:` — the answer to a no, verbatim like `message:` (a deny
    # is the gate working, not failing: the user is answered by the
    # walk itself, then `on_fail` says whether the session ends or the
    # model is briefed). None = no answer from the walk.
    denied: str | None = None
    resume: str | None = None
    # The waypoint before the ask — the page a payment ask reads its
    # total off ("" when none precedes it; a payment ask requires one).
    enter: str = ""
    # A payment ask's `total_label:` — the label readings the sheet total
    # sits beside (`money.declared_total` reads the amount off that row).
    total_label: tuple[str, ...] = ()
    # The ask's own patience: the in-session poll cadence and how many
    # silent rounds before the session suspends for the next wake.
    wait_seconds: int = DEFAULT_ASK_WAIT_SECONDS
    silence_rounds: int = DEFAULT_ASK_ROUNDS
    # `think:` — as on an agent step, for the one call an ask may make:
    # reading a reply its yes/no words miss (`read_reply`).
    think: Thinking | None = None
    on_fail: str | None = None  # `on_fail:` — see ON_FAIL_MODES

    @property
    def pays(self) -> bool:
        """Whether this ask is the payment gate — the one that binds a
        consented total and precedes the irreversible move."""
        return self.approve == PAYMENT


@dataclass(frozen=True)
class TellNode:
    """A `tell` move — message the user; the walk moves on once the
    send lands (a reply, if any, is the next wake's boot to read)."""

    id: str
    message: str
    on_fail: str | None = None  # `on_fail:` — see ON_FAIL_MODES


@dataclass(frozen=True)
class RunNode:
    """A `run` move — a playbook of this pack walked as ONE move, the
    way a `do` runs a macro: its `with:` fills the playbook's inputs,
    it starts on the page before it (or cold, when the playbook opens
    with its own `start`) and lands on the playbook's last page; its
    `returns:` are read downstream as `{<run>.<field>}`.

    With `each:` it runs once per line of a list an earlier agent
    returned — one round per distinct item, a round's returns joined
    as lines afterwards — bounded by `max_rounds`. `miss: skip` lets a
    round that hands over be recorded as missed while the walk goes
    on (legal only for a playbook that never asks or pays). `revise`
    names an earlier agent of the route: a reply the yes/no words miss
    at any ask INSIDE this run re-runs the walk from there, at most
    `revise_limit` times."""

    id: str  # the run's own word, which is the playbook's name (`run: add`)
    args: dict  # `with:` — ref templates, filled at run time
    sub: "Playbook"
    enter: str  # "" when the playbook opens with its own `start`
    verify: str  # the playbook's last page — the landing
    each: tuple[str, str] | None = None  # (the input it fills, the list ref)
    miss: str | None = None  # `miss: skip`, or None
    revise: str | None = None  # an earlier agent's id, or None
    revise_limit: int = 0
    max_rounds: int = DEFAULT_RUN_ROUNDS
    on_fail: str | None = None

    @property
    def self_starting(self) -> bool:
        return self.sub.self_starting


@dataclass(frozen=True)
class SelectNode:
    """The `select` step — the channel boot's own, and its last: on
    the thread (its `enter`, the page before it), ONE parse_task call
    over the enabled playbooks; a positive answer becomes the walk's
    baton (the program the conductor drives next), anything else ends
    the boot quietly with the model standing on the thread. `max_scrolls`
    bounds parse_task's scroll-for-history escape."""

    id: str
    enter: str
    max_scrolls: int
    irreversible: str | None = None  # `Checked`'s obligation; never set here
    think: Thinking | None = None  # `think:` — as on an agent step
    on_fail: str | None = None  # a `Node`'s obligation; never set here


class Checked(Protocol):
    """A node the walk checks a page for before it runs — a `do`, an
    acting `agent`, the boot's `select`. What `enter_gate` and
    `recover_or_handover` read: where it must be, and whether money
    forbids recovering it."""

    @property
    def id(self) -> str: ...

    @property
    def enter(self) -> str: ...

    @property
    def irreversible(self) -> str | None: ...


@dataclass(frozen=True)
class RecoverHand:
    """One recovery hand — a bare gesture (`tool`), a tap of a declared
    `landmark`, or one argument-less `macro`."""

    tool: str | None = None
    landmark: str | None = None
    macro: str | None = None


@dataclass(frozen=True)
class Recovery:
    """A page's declared failure behaviour: `recover:` — which hand runs
    for which reading of the deviation — with `tries`, how many this
    page gets in one walk, and `on_fail`, what happens once they are
    spent.
    `covered` fires when the page itself reads under a sheet or popup;
    `locked` when the phone shows its lock screen; `elsewhere` for any
    other screen. The flat form (`recover: go_back`) declares one hand
    for all three."""

    covered: RecoverHand | None = None
    elsewhere: RecoverHand | None = None
    locked: RecoverHand | None = None
    tries: int = DEFAULT_RECOVER_LIMIT
    # The page's `on_fail:` — what a page that cannot be reached does
    # once its hand is spent (or it has none); see ON_FAIL_MODES.
    on_fail: str | None = None

    def hand_for(self, reading: str) -> RecoverHand | None:
        """The hand declared for one of `RECOVER_READINGS`."""
        if reading == READING_COVERED:
            return self.covered
        if reading == READING_LOCKED:
            return self.locked
        return self.elsewhere

    @property
    def hands(self) -> tuple[RecoverHand, ...]:
        return tuple(
            h for h in (self.covered, self.elsewhere, self.locked) if h is not None
        )


Node = DoNode | AgentNode | AskNode | TellNode | RunNode | SelectNode


@dataclass(frozen=True)
class Playbook:
    """One validated playbook. `parse_playbook` is the only producer, so a
    Playbook is correct by construction against its pack (declared pages,
    pack macros, landmarks)."""

    app: str
    name: str
    description: str
    enabled: bool
    inputs: tuple[PlaybookInput, ...]
    nodes: tuple[Node, ...]  # the route's MOVES, compiled (waypoints derived away)
    # The route's first waypoint — where the walk must be at start (it
    # is also the first move's derived enter, which is what the runtime
    # actually checks).
    start: str = ""
    # The embedded macros — do bodies (`<playbook>.<move>`) and
    # resume/recover bodies (`<playbook>.<name>.<role>`). The node field
    # holds the same synthesized name, so dispatch is name-keyed either
    # way. `qualified_inline` is the registry door.
    inline_macros: dict[str, Macro] = field(default_factory=dict)
    # Declared recovery, page name → its hands: a mismatched page runs
    # ITS hand for the reading, or hands over when it declares none.
    recovers: dict[str, Recovery] = field(default_factory=dict)
    # The prompt files this route's agent steps read, pack-relative
    # (`buy/prompts/pick.md`) — `playbooks check` names the files no
    # route reads.
    prompts_used: frozenset[str] = frozenset()
    # `returns:` — what a run of this playbook yields, field → template
    # over its own refs, filled when the run's round ends.
    returns: dict[str, str] = field(default_factory=dict)
    # The route's last waypoint — the landing a `run` of it checks; ""
    # when the route ends on a move (then it cannot be run as a move).
    end: str = ""

    @property
    def run_by(self) -> str | None:
        """The entry whose `run:` walks this playbook — None when it IS
        an entry. Where the file sits says who may launch it: an entry
        (`<name>/PLAYBOOK.yml`, `kind: entry`) the boot offers for a
        request; a playbook beside it (`<entry>/<name>.yml`, `kind:
        playbook`, id `<entry>.<name>`) only that entry walks, by `run:`
        — and `playbooks run`, to rehearse it — never the agent, never
        another pack."""
        return paths.run_by(self.name)

    @property
    def offered(self) -> bool:
        """Whether the boot may launch this playbook at all — an entry,
        yes; one an entry runs, never. The STRUCTURAL rule, apart from
        readiness (`live_gap`): such a playbook cannot be enabled
        into being offered, so everything that filters the menu reads
        this, not a gap string."""
        return self.run_by is None

    @property
    def runs(self) -> tuple[RunNode, ...]:
        """The playbooks this route runs as moves, in route order."""
        return tuple(n for n in self.nodes if isinstance(n, RunNode))

    @property
    def self_starting(self) -> bool:
        """Whether the route cold-launches by its own `start` before it
        touches the screen (a `start` sits right before the first page,
        so any pure-text agents above it change nothing) — what a `run`
        of it needs no page before it for."""
        return any(isinstance(n, DoNode) and n.start for n in self.nodes)

    @property
    def activates(self) -> bool:
        """Whether this is the boot — a route ending in `select`, the
        one walk that hands a baton on (the compiler admits the step in
        the channel pack's boot file only)."""
        return any(isinstance(n, SelectNode) for n in self.nodes)

    def first_unsettled(self, outputs: dict[str, str]) -> int:
        """Where a walk opens: the route top, past any COMPLETED
        pure-text agent — its outputs are recorded, and re-deriving them
        (a resumed or stepped walk's opening) could silently change them.
        Never further: a page that happens to match a later move's
        landing proves nothing about the moves before it."""
        for i, node in enumerate(self.nodes):
            settled = (
                isinstance(node, AgentNode)
                and not node.acts
                and all(f"{node.id}.{f}" in outputs for f in node.return_fields)
            )
            if not settled:
                return i
        return len(self.nodes)

    @property
    def with_subs(self) -> list["Playbook"]:
        """This playbook and every playbook it runs — one level, by the
        compiler's rule."""
        return [self, *(r.sub for r in self.runs)]


def resolve_inputs(spec: Playbook, provided: dict[str, str]) -> dict[str, str]:
    """Provided values against the declared inputs — the macro layer's
    resolution contract verbatim (unknown keys, missing required, defaults,
    strings only), translated to this spec's error class at the one seam."""
    try:
        return macro_inputs.resolve_inputs(spec, provided)
    except MacroError as e:
        raise PlaybookError(str(e)) from e
