"""The step executor — what every route entry kind implements, and the
`Walk` contract a step may rely on.

The walk (`program.py`) owns the cursor, the one action in flight, the
page verdict, and recovery; a step owns everything about ONE node at
the cursor. The walk asks a step three questions and routes by the
pending-action kinds the step declares:

  - `open()`       the cursor arrived — the step's first turn
  - `landed(kind)` one of its actions landed; the result view is in
                   `walk.screen` / `walk.verdict`
  - `resolve()`    the micro request it returned came back

A blocked or errored action is not a question: the walk hands over
with the error (nothing retries in the background). A step ends by
`walk.advance_cursor()` or `walk.handover(...)`; the walk never
inspects a step's private state.

`Walk` is the whole surface a step sees of the program — listed here
so the boundary is explicit and type-checked, and so a reader of any
executor knows exactly what it may read and call without opening
`program.py`.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from physiclaw.conductor.walk.micro import DecisionRequest, MicroOutcome
from physiclaw.contract.dto import AssistantMessage, ImageBlock, Thinking

if TYPE_CHECKING:
    from physiclaw.common.listing import Screen
    from physiclaw.conductor.spec.channel import Channel
    from physiclaw.conductor.spec.match import Verdict
    from physiclaw.conductor.spec.model import Checked, Playbook
    from physiclaw.conductor.spec.pages import Landmark
    from physiclaw.conductor.walk.gate import Gate
    from physiclaw.conductor.walk.ledger import Ledger
    from physiclaw.conductor.walk.program import Program
    from physiclaw.conductor.walk.recover import Mode
    from physiclaw.conductor.walk.thread import Thread
    from physiclaw.macros.model import Macro


@dataclass(frozen=True)
class Paused:
    """The stepping pause: the walk's cursor left the node this run was
    for, and the walk answers nothing more THIS run. A later run
    rebuilds it from its projection. Distinct from None on purpose —
    None is the walk spent for good (handed over, completed,
    crashed): the conductor drops it and the model speaks."""


# What every walk call answers: a synthesized turn, a micro request for
# the conductor to broker, a stepping pause, or None — spent.
Turn = AssistantMessage | DecisionRequest | Paused | None


class Activator(Protocol):
    """What the boot's `select` step needs of the activation the wake
    wires in (`drive/activation.Activation` is the one implementation):
    the parse_task request over a thread screen, and the program its
    outcome builds — or None when nothing activates."""

    def request(
        self, walk: "Walk", node_id: str, thinking: "Thinking | None" = None
    ) -> DecisionRequest: ...

    def build(
        self, outcome: MicroOutcome | None, thread: "Thread"
    ) -> "Program | None": ...


class Walk(Protocol):
    """The program as a step sees it — four small contracts. Nothing
    else of the program is a step's business."""

    # ---- the reading: where the walk is and what it has seen ----
    app: str
    idx: int
    spec: "Playbook"
    gate: "Gate"
    screen: "Screen | None"
    frame: ImageBlock | None
    verdict: "Verdict | None"
    ledger: "Ledger"  # the walk's one account — what a step did lands here
    thread: "Thread"  # the session's conversation with the model about the errand
    landmarks: "dict[str, Landmark]"
    pack_macros: "dict[str, Macro]"  # every hand the walk can dispatch, qualified
    channel: "Channel | None"
    # The boot's two extras: the activation (menu, parse_task, build)
    # its `select` step runs, and the program that step hands on —
    # None on every other walk.
    activation: "Activator | None"
    baton: "Program | None"

    @property
    def outputs(self) -> dict[str, str]: ...  # the ledger's decisions, `node.field`

    # An agent's return field, recorded under the node — inside a run's
    # round, under that round's prefix.
    def decide(self, node_id: str, field: str, value: str) -> None: ...

    def ref_values(self) -> dict[str, str]: ...

    def mismatch(self, verdict: "Verdict", expected_id: str) -> str | None: ...

    # ---- the moves: one synthesized turn, and the note it carries ----
    def synth(
        self, kind: str, summary: str, tool: str, args: dict, *, channel: bool = False
    ) -> AssistantMessage: ...

    def journal(self, text: str) -> None: ...

    # ---- money: the page a payment may fire on, consent, the record ----
    def money_page_block(self, what: str) -> str | None: ...

    def spend_consent(self) -> None: ...

    def log_purchase(self) -> None: ...

    # ---- recovery: the page check before a node, and its declared hand ----
    def enter_gate(self, node: "Checked") -> Turn: ...

    def recover_or_handover(
        self, node: "Checked", expected_id: str, mode: "Mode", reason: str
    ) -> Turn: ...

    # ---- the terminal moments, and the one way forward ----
    def advance_cursor(self) -> Turn: ...

    def handover(self, reason: str, *, advice: str = "") -> Turn: ...

    # A reply an ask's words missed, as a revision of the request: the
    # walk re-runs from the agent the enclosing `run` named, when it
    # named one and its revisions are not spent. None = no revision here.
    def revise(self, replies: str) -> Turn: ...

    def conclude(self, reason: str) -> None: ...

    def suspend(self) -> AssistantMessage: ...

    def close_done(self, recap: str, memory: str | None) -> AssistantMessage: ...


N = TypeVar("N")


class Step(Generic[N]):
    kinds: frozenset[str] = frozenset()

    def __init__(self, walk: Walk, node: N) -> None:
        self.walk = walk
        self.node = node

    def open(self) -> Turn:
        raise NotImplementedError

    def landed(self, kind: str) -> Turn:
        raise NotImplementedError

    def resolve(self, outcome: MicroOutcome | None) -> Turn:
        name = getattr(self.node, "id", type(self).__name__)
        return self.walk.handover(f"{name!r} brokered no decision")
