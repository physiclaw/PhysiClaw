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
clause has exactly one spelling.

The thread's position lives here too (`offer` / `commit`), not on the
thread: the thread outlives any one ledger — the boot's parse settles
against the boot's, and the walk it activates starts its own at zero —
so a cursor held on the thread could be applied to the wrong account.
"""

from dataclasses import dataclass, field
from typing import Any

from physiclaw.common.text import clip


@dataclass
class Ledger:
    ref: str  # the playbook ref, as every line names it
    nodes: int  # the route's length, for "(n/m nodes)"
    task: dict[str, str]  # the playbook's inputs — what was asked
    decided: dict[str, str] = field(default_factory=dict)  # node.field → value
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
        self.events.append(f"decided {key}={clip(value, 120)!r}")

    def say(self, text: str) -> None:
        self.said.append(text)
        self.events.append(f"sent to the user: {clip(text, 160)!r}")

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
        self.paid = amount
        self.events.append(f"paid ¥{amount:g}")

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
            parts.append(f"user consented to ¥{consented:g}, not paid")
        if self.paid is not None:
            parts.append(f"paid ¥{self.paid:g}")
        return "; ".join(parts)

    def to_suspended(self) -> dict[str, Any]:
        """The parts a suspension carries to the next wake (the task
        rides as the walk's own `values`)."""
        return {
            "outputs": dict(self.decided),
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
