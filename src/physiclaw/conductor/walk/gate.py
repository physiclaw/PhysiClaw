"""The ask-and-hold state — everything between "ask sent" and "consent
spent", in one object with one suspension projection.

The walk (`program.py`) owns the cursor; the gate owns the ask text,
the thread snapshot the reply is diffed against, the money numbers a
payment ask quotes and a `yes:` binds, the silence counter, and the
reply words the last send declared. Consent lives here rather than on
the ask step because the payment move AFTER the ask spends it, and a
suspension in between must carry it across wakes. The money numbers
change only through the gate's own methods (`quote`, `consent`,
`spend`), so the whole consent life reads in this one file.
"""

from dataclasses import dataclass, field


@dataclass
class Gate:
    ask: str = ""
    baseline: set[str] = field(default_factory=set)
    quoted: float | None = None
    consented: float | None = None
    # The ask's `total_label:` readings — the fire-time check reads the
    # total by them, exactly as the ask quoted it.
    total_label: tuple[str, ...] = ()
    silence: int = 0
    awaiting: bool = False  # ask sent, polling for the reply
    # The reply words the last LANDED send declared (already in
    # `reply.normalize` space) — what every read of the thread after it
    # matches: this ask's check, a later send's landing. `next_words`
    # holds the words of a send still in flight.
    yes: tuple[str, ...] = ()
    no: tuple[str, ...] = ()
    next_words: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())
    tried_open: bool = False
    # Every reply an ask of this walk read, verbatim, in order — the
    # `{ask.replies}` slot a prompt may quote (a revision re-reads the
    # request with them) — and how many revisions the walk has taken.
    replies: list[str] = field(default_factory=list)
    revisions: int = 0

    def opened(self, text: str, yes: tuple[str, ...], no: tuple[str, ...]) -> None:
        """An ask goes out: the question, and the words its reply will
        be judged by once it lands."""
        self.ask = text
        self.next_words = (yes, no)
        self.tried_open = False

    def abandon_ask(self) -> "Gate":
        """Leave the ask the cursor was holding (a stepping JUMP): the
        ask text, its reply words, the thread snapshot they were read
        against, and the hold — a later send's landing must not read a
        reply staged for THAT ask as its own. Consent is kept: a jump
        onto the payment move after a confirmed ask is how it is
        stepped. Returns self."""
        self.ask = ""
        self.baseline = set()
        self.awaiting = False
        self.yes = self.no = ()
        return self

    def rewind_ask(self, seen: set[str]) -> "Gate":
        """Leave the ask for a RE-PLAN (`revise`): the hold and the ask
        text go, the conversation does not. The thread snapshot becomes
        the screen just read (`seen`) and the declared words stay, so a
        `no:` typed while the walk is off re-planning is still new, and
        still a no, at the next send's landing — which after a re-plan
        is usually a `tell` with no words of its own. Emptying either,
        as a stepping jump does, would baseline that cancellation away
        unread: the one thing `speak.sent_landed` exists to prevent."""
        self.ask = ""
        self.awaiting = False
        self.baseline = seen
        return self

    # ---- consent: quote, hold, bind, spend — the gate's own moves ----

    def quote(self, total: float, labels: tuple[str, ...]) -> None:
        """A payment ask read its sheet: the amount the message quotes,
        and the label readings the fire-time check reads it by again."""
        self.quoted = total
        self.total_label = labels

    def hold(self) -> None:
        """The ask landed on the thread: poll for the reply from zero."""
        self.awaiting = True
        self.silence = 0

    def consent(self) -> None:
        """A yes ends the hold and binds the quoted total as the
        consented one — None for an ask that quoted nothing."""
        self.consented = self.quoted
        self.awaiting = False

    def spend(self) -> float | None:
        """Consent is CONSUMED by firing: a later payment needs its own
        gate's fresh confirm, never this one's leftovers. Returns the
        amount that fired."""
        amount, self.consented, self.quoted = self.consented, None, None
        self.total_label = ()
        return amount

    def to_suspended(self) -> dict:
        """The persisted projection — the one field list, beside the
        fields. Counters and the in-flight handshake deliberately reset
        on resume; `consented` persists so a post-consent suspension can
        never resume into a refused payment."""
        return {
            "ask_text": self.ask,
            "baseline": sorted(self.baseline),
            "quoted": self.quoted,
            "consented": self.consented,
            "total_label": list(self.total_label),
            "awaiting": self.awaiting,
            "yes": list(self.yes),
            "no": list(self.no),
            "replies": list(self.replies),
            "revisions": self.revisions,
        }

    @classmethod
    def from_suspended(cls, data: dict) -> "Gate":
        return cls(
            ask=str(data.get("ask_text") or ""),
            baseline=set(data.get("baseline") or []),
            quoted=data.get("quoted"),
            consented=data.get("consented"),
            total_label=tuple(str(r) for r in (data.get("total_label") or [])),
            awaiting=bool(data.get("awaiting")),
            yes=tuple(str(w) for w in (data.get("yes") or [])),
            no=tuple(str(w) for w in (data.get("no") or [])),
            replies=[str(r) for r in (data.get("replies") or [])],
            revisions=int(data.get("revisions") or 0),
        )
