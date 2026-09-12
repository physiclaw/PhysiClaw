"""The walk's one account — what was asked, decided, said, paid.

Every step writes here through the `Walk` (the parse's inputs are the
task; an agent's return fields are decisions; an ask's or a tell's
message is what was said; a fired payment is what was paid; a landed
move is the macro it ran and the page it reached; a journal line is a
note) and every exit reads here (the handover brief, a stop's
or a suspension's recap, the completion's DONE recap and the daily-log
line the next wake's memory reads). So the task the boot parsed, the
confirmation the ask got, the payment the move fired and the report the
walk closes on are one context, spelled once.

Two views of the same facts: the typed fields (`task`, `decided`,
`said`, `paid`) for a recap, and `events` — every fact as one clause, in
the order it landed — for the session thread, which carries the
unreported tail to the model as "what the playbook did since the last
call". The writers (`decide`, `say`, `note`, `pay`) append to both, so a
new kind of fact is one writer here and reaches every reader, and every
clause has exactly one spelling. A run's rounds are a third typed view,
`rounds`, kept apart from `decided` so the route's own decisions stay
what a recap prints; the thread never reads the typed fields, only
`events`, where a round is one clause like any other fact.

The thread's position lives here too (`offer` / `commit`), not on the
thread: the thread outlives any one ledger — the boot's parse settles
against the boot's, and the walk it activates starts its own at zero —
so a cursor held on the thread could be applied to the wrong account.
"""

from dataclasses import dataclass, field
from typing import Any

from physiclaw.common.text import clip
from physiclaw.conductor.spec.conventions import ROUND_DONE, ROUND_MISS
from physiclaw.conductor.walk.money import plain


def round_prefix(run_id: str, key: str) -> str:
    """The name a run's round keeps its record under: `<run>[<key>]`,
    the key being the item (empty for a plain run). An identity, not
    prose: it is looked up by, so it is the line verbatim."""
    return f"{run_id}[{key}]"


@dataclass
class Ledger:
    ref: str  # the playbook ref, as every line names it
    nodes: int  # the route's length, for "(n/m nodes)"
    task: dict[str, str]  # the playbook's inputs — what was asked
    decided: dict[str, str] = field(default_factory=dict)  # node.field → value
    # A revised step's last answer (`unsettle`): no longer a decision
    # the walk stands on, still what that step's own prompt re-reads.
    previous: dict[str, str] = field(default_factory=dict)
    # A run's rounds, each its own small record under `round_prefix`:
    # the round's agents' outputs (`node.field`), its returns (`field`),
    # and `done` ("done" / "missed", with `miss` the reason). Kept
    # apart from `decided`, which is the route's own and what recaps
    # and the stepping position print.
    rounds: dict[str, dict[str, str]] = field(default_factory=dict)
    said: list[str] = field(default_factory=list)  # messages sent to the user
    answers: dict[str, str] = field(default_factory=dict)  # ask id → yes/no
    refused: dict[str, int] = field(default_factory=dict)  # never_tap target → tries
    paid: float | None = None  # the amount a payment move fired with
    events: list[str] = field(default_factory=list)  # every fact, one clause, in order
    reported: int = 0  # how many events the thread has carried to the model
    _offered: int = 0  # how many the call in flight carried

    def __post_init__(self) -> None:
        if self.task and not self.events:
            self._seed()

    def _seed(self) -> None:
        """The account always opens with what was asked — the one place
        that clause is spelled."""
        self.events.append(_asked(self.task))

    # ---- the writers: one fact, both views ----

    def decide(self, key: str, value: str) -> None:
        self.decided[key] = value
        self.previous.pop(key, None)
        self.events.append(f"decided {key}={clip(value, 120)!r}")

    def unsettle(self, node_id: str) -> None:
        """A revision re-runs `node_id`: its outputs leave `decided` (an
        opening walks past a pure-text agent whose outputs are on
        record) and become its `previous` answer until it answers again.
        The events already say why."""
        for key in [k for k in self.decided if k.split(".", 1)[0] == node_id]:
            self.previous[key] = self.decided.pop(key)

    def say(self, text: str) -> None:
        self.said.append(text)
        self.events.append(f"sent to the user: {clip(text, 160)!r}")

    # ---- a run's rounds: one record per round, under its prefix ----

    def decide_in_round(self, prefix: str, key: str, value: str) -> None:
        """An agent's return field inside a round — the round's record,
        and the same clause `decide` writes, named by the round."""
        self.rounds.setdefault(prefix, {})[key] = value
        self.events.append(f"decided {prefix}.{key}={clip(value, 120)!r}")

    def round_done(self, prefix: str, returns: dict[str, str]) -> None:
        """A round of a `run` ended: its returns and the done mark, one
        clause for the account."""
        record = self.rounds.setdefault(prefix, {})
        record.update(returns)
        record[ROUND_DONE] = "done"
        self.events.append(
            f"round {prefix} done" + (": " + _pairs(returns, 80) if returns else "")
        )

    def round_missed(self, prefix: str, reason: str) -> None:
        """A round the run declared skippable handed over: recorded as
        missed, with the reason its returns will carry."""
        record = self.rounds.setdefault(prefix, {})
        record[ROUND_DONE] = "missed"
        record[ROUND_MISS] = reason
        self.events.append(f"round {prefix} missed: {clip(reason, 120)}")

    def round_finished(self, prefix: str) -> bool:
        return ROUND_DONE in self.rounds.get(prefix, {})

    def round_count(self, run_id: str) -> int:
        """How many rounds this run has a record for — what it has already
        WALKED, which is what a `rounds:` budget bounds."""
        head = round_prefix(run_id, "")[:-1]  # `<run>[`
        return sum(p.startswith(head) for p in self.rounds)

    def round_values(self, prefix: str) -> dict[str, str]:
        """A round's own record, keys as the round's refs spell them
        (`node.field`, `field` for its returns)."""
        return dict(self.rounds.get(prefix, {}))

    def round_return(self, prefix: str, field: str) -> str | None:
        """A done round's return; None for a round still to do or one
        that missed (its `miss` reason stays in the record)."""
        record = self.rounds.get(prefix, {})
        return record.get(field) if record.get(ROUND_DONE) == "done" else None

    def note(self, text: str) -> None:
        """What a step reported, in its own words — an event only: no
        exit renders notes as a field, the thread reads them in order
        with everything else. Capped like `say`: most of these are
        model-written prose, and they ride the next call's block."""
        self.events.append(clip(text, 160))

    def answered(self, ask: str, verdict: str) -> None:
        """How the user answered an ask. No event of its own: the
        journal line written beside it already says it in words, with
        the reply verbatim and who read it. The field is what an exit
        renders — a payment ask left a consent amount, but any other
        ask left nothing an exit could see."""
        self.answers[ask] = verdict

    def refuse(self, target: str) -> None:
        """A guard rail turned a move away (`never_tap`). Counted rather
        than noted, because a stop's day line would otherwise read like
        any other failed step — and a walk where this fires is a prompt
        or a route to fix, told to the reader who would fix it."""
        self.refused[target] = self.refused.get(target, 0) + 1

    def pay(self, amount: float) -> None:
        """A payment fired. Accumulated, not assigned: `paid` is what
        this walk has spent, not what the last tap cost."""
        self.paid = (self.paid or 0.0) + amount
        self.events.append(f"paid ¥{plain(amount)}")

    # ---- the thread's position ----

    def offer(self) -> list[str]:
        """The clauses no model call has carried yet, and a note of how
        far this one reaches. A fact landing while the model answers is
        left for the next call rather than lost."""
        self._offered = len(self.events)
        return self.events[self.reported :]

    def commit(self) -> None:
        """The offered call came back: everything it carried is
        reported."""
        self.reported = self._offered

    # ---- the readers ----

    def account(self) -> list[str]:
        """The walk's doings as clauses, one per fact that exists: what
        was asked, decided, said. Money is deliberately absent — each
        exit warns about it in its own voice (`recap` for the plain
        ones, the brief and a stop for the loud ones)."""
        parts: list[str] = []
        if self.task:
            parts.append(_asked(self.task))
        if self.decided:
            parts.append("decided " + _pairs(self.decided, 120))
        if self.rounds:
            parts.append(
                "rounds "
                + _pairs(
                    {p: r.get(ROUND_DONE, "open") for p, r in self.rounds.items()}, 40
                )
            )
        if self.answers:
            parts.append("answered " + _pairs(self.answers, 40))
        if self.refused:
            parts.append(
                "refused "
                + ", ".join(f"{n}× a tap on {t}" for t, n in self.refused.items())
            )
        if self.said:
            n = len(self.said)
            parts.append(
                f"said {n} message{'s' if n != 1 else ''}, last {clip(self.said[-1], 120)!r}"
            )
        return parts

    def recap(self, lead: str, *, consented: float | None = None) -> str:
        """One line: how the walk ended, its account, then the money —
        the plain rendering, for an exit that is not a warning."""
        parts = [lead, *self.account()]
        if consented is not None:
            parts.append(f"user consented to ¥{plain(consented)}, not paid")
        if self.paid is not None:
            parts.append(f"paid ¥{plain(self.paid)}")
        return "; ".join(parts)

    def to_suspended(self) -> dict[str, Any]:
        """The parts a suspension carries to the next wake (the task
        rides as the walk's own `values`)."""
        return {
            "outputs": dict(self.decided),
            "previous": dict(self.previous),
            "rounds": {p: dict(r) for p, r in self.rounds.items()},
            "said": list(self.said),
            "answers": dict(self.answers),
            "refused": dict(self.refused),
            "paid": self.paid,
            "events": list(self.events),
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Overlay a suspension's projection. The typed fields are
        replayed through the writers that own their wording, never
        re-spelled here; the stored event log then takes over when the
        record has one. A record from before it was kept carries
        `outputs` only, so its account is rebuilt grouped rather than in
        landing order."""
        self.decided, self.said, self.paid = {}, [], None
        self.rounds = {
            str(p): {str(k): str(v) for k, v in (r or {}).items()}
            for p, r in (data.get("rounds") or {}).items()
        }
        self.previous = {
            str(k): str(v) for k, v in (data.get("previous") or {}).items()
        }
        self.answers = {str(k): str(v) for k, v in (data.get("answers") or {}).items()}
        self.refused = {str(k): int(v) for k, v in (data.get("refused") or {}).items()}
        self.events, self.reported, self._offered = [], 0, 0
        if self.task:
            self._seed()
        for key, value in (data.get("outputs") or {}).items():
            self.decide(str(key), str(value))
        for said in data.get("said") or []:
            self.say(str(said))
        if data.get("paid") is not None:
            self.pay(float(data["paid"]))
        if data.get("events") is not None:
            self.events = [str(s) for s in data["events"]]


def _asked(task: dict[str, str]) -> str:
    return "asked " + _pairs(task, 80)


def _pairs(mapping: dict[str, str], width: int) -> str:
    return ", ".join(f"{k}={clip(v, width)!r}" for k, v in mapping.items())
