"""The route as the cursor walks it — the `Course` of `Slot`s, and the
`Round`s a `run` expands into. The walk (`program.py`) decides WHEN
the shape changes — a run reached, a round done or missed, a revision
— and `rounds.py` computes what each round's inputs are; this is the
shape itself, the cursor's place in it, and the readings of that place
(label, spec position, the node's prose id) every log, brief and
suspension prints.
"""

from dataclasses import dataclass

from physiclaw.common.text import clip
from physiclaw.conductor.spec.model import (
    Node,
    Playbook,
    PlaybookError,
    Recovery,
    RunNode,
)
from physiclaw.conductor.walk.ledger import round_prefix


@dataclass(frozen=True)
class Round:
    """One round of a `run`: the playbook's nodes walked once, with its
    own inputs and its own record in the ledger (`ledger.rounds`, under
    `ledger.round_prefix`), so a re-plan can tell a finished round from
    one still to do, and a round's own refs read only its own record."""

    run: RunNode
    key: str
    inputs: dict[str, str]  # `inputs.<name>` → value, this round's

    @property
    def prefix(self) -> str:
        return round_prefix(self.run.id, self.key)


@dataclass(frozen=True)
class Slot:
    """One place on the route the cursor walks: the node, the spec index
    it came from (what a suspension stores), and the round it belongs
    to — None for the route's own nodes."""

    node: Node
    origin: int
    round: Round | None = None


def run_keys(run: RunNode, vals: dict[str, str]) -> list[str]:
    """The rounds a run has now: one, or one per distinct line of its
    list (the list's CURRENT value — a re-plan changes it)."""
    if run.each is None:
        return [""]
    lines = vals.get(run.each[1], "").splitlines()
    return list(dict.fromkeys(k for line in lines if (k := line.strip())))


class Course:
    """The live route and the cursor on it. Starts as the spec's nodes,
    one slot each; a `run` the cursor reaches is replaced by its
    rounds' nodes (`expand`), a round that is done or missed leaves
    (`drop_round`), and a revision puts the spec shape back from a
    node on (`reset`). Every slot remembers the spec index it came
    from, so the cursor can be stored in terms a route change cannot
    move (`position`)."""

    def __init__(self, spec: Playbook) -> None:
        self.spec = spec
        self.slots: list[Slot] = [Slot(n, i) for i, n in enumerate(spec.nodes)]
        self.idx = 0

    def __len__(self) -> int:
        return len(self.slots)

    # ---- the cursor's place ----

    @property
    def _slot(self) -> Slot | None:
        return self.slots[self.idx] if self.idx < len(self.slots) else None

    @property
    def node(self) -> Node | None:
        """The node at the cursor — None once the walk is past the last."""
        slot = self._slot
        return slot.node if slot is not None else None

    @property
    def round(self) -> Round | None:
        """The round the cursor stands in — None on the route's own nodes."""
        slot = self._slot
        return slot.round if slot is not None else None

    def step(self) -> Round | None:
        """Move the cursor one slot on. Returns the round it thereby
        LEFT (its last node settled) — None when it stays in the same
        round, or was in none."""
        rd = self.round
        self.idx += 1
        return rd if rd is not None and self.round is not rd else None

    def span(self, rd: Round) -> tuple[int, int]:
        """A round's first and last route index, inclusive."""
        at = [j for j, s in enumerate(self.slots) if s.round is rd]
        return at[0], at[-1]

    def route_index(self, spec_idx: int) -> int:
        """The route index of a spec node — where the cursor lands for a
        spec-level position once runs before it have expanded."""
        return next(
            (j for j, s in enumerate(self.slots) if s.origin >= spec_idx),
            len(self.slots),
        )

    def recovers(self) -> dict[str, Recovery]:
        """The declared hands the cursor's page is under — the run
        playbook's inside a round, else this route's."""
        rd = self.round
        return rd.run.sub.recovers if rd is not None else self.spec.recovers

    # ---- the readings of that place ----

    def position(self) -> tuple[int, str | None, int]:
        """Where the cursor stands, in terms a route change cannot move:
        the SPEC index, the round's key, and the offset inside it. A
        route index is not that — expansions, finished rounds and
        revisions all shift it, so two different nodes can wear one
        number."""
        slot = self._slot
        if slot is None:
            return (len(self.spec.nodes), None, 0)
        if slot.round is None:
            return (slot.origin, None, 0)
        return (slot.origin, slot.round.key, self.idx - self.span(slot.round)[0])

    def node_id(self) -> str | None:
        """Where the cursor stands, as a line of OUR prose — a brief and
        a day line both print it as a sentence of the conductor's. A
        round's key is a line of an agent's answer, read off a screen
        whose text a seller writes, so it is clipped here; the prefix it
        comes from stays verbatim, being an identity the ledger is
        looked up by."""
        slot = self._slot
        if slot is None:
            return None
        if slot.round is None:
            return slot.node.id
        rd = slot.round
        return f"{round_prefix(rd.run.id, clip(rd.key, 60))}/{slot.node.id}"

    def label(self) -> str:
        """Where the walk stands, for logs and the stepping driver:
        `node (i/n)`, a round's node under its run and key."""
        n = len(self.slots)
        if self.idx >= n:
            return f"(end, {n}/{n})"
        return f"{self.node_id()} ({self.idx + 1}/{n})"

    # ---- the cursor moving ----

    def skip_to(self, spec_idx: int) -> None:
        """The cursor moves on to spec node `spec_idx` — never back."""
        self.idx = max(self.route_index(spec_idx), self.idx)

    def seat(self, rd: Round, at: int) -> None:
        """The cursor at offset `at` inside round `rd` (a restored
        suspension's place). An offset outside the round — the
        sub-playbook edited shorter under the suspension — raises: a
        stale offset must not fake a position, seating the cursor in
        the NEXT round, past the run, or on a trailing `tell` that
        reports work nobody did."""
        start, end = self.span(rd)
        if not 0 <= at <= end - start:
            raise PlaybookError(f"suspended round {rd.key!r} offset {at} is outside it")
        self.idx = start + at

    # ---- the shape changing ----

    def expand(self, rounds: list[Round]) -> None:
        """Replace the `run` at the cursor by its rounds' nodes, in
        order — nothing when every round is already done, so the cursor
        lands on what followed."""
        slot = self._slot
        assert slot is not None and isinstance(slot.node, RunNode)
        self.slots[self.idx : self.idx + 1] = [
            Slot(n, slot.origin, rd) for rd in rounds for n in slot.node.sub.nodes
        ]

    def drop_round(self, rd: Round) -> None:
        """A round that is done or missed leaves the route: its record is
        the ledger's, and the live route keeps the one shape a resumed
        walk rebuilds (a finished round never expanded again). The
        cursor lands on what followed."""
        start, end = self.span(rd)
        del self.slots[start : end + 1]
        self.idx = start

    def reset(self, target: int) -> list[Round]:
        """A revision: the route from spec node `target` on goes back to
        its spec shape (a run in progress re-expands when reached), and
        the cursor stands on it. Returns the rounds that left the route,
        each once."""
        at = self.route_index(target)
        dropped = {id(s.round): s.round for s in self.slots[at:] if s.round is not None}
        self.slots[at:] = [
            Slot(n, j) for j, n in enumerate(self.spec.nodes[target:], target)
        ]
        self.idx = at
        return list(dropped.values())
