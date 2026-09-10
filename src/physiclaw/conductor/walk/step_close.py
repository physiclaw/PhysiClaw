"""The walk's close — the last step of a completed route.

The task was the playbook's and it is done; what remains is the record.
One call in the session's thread (`summarize`) asks the model that read
the request and any vague reply to write the recap the session closes
on and the memory line the next wake reads — one context from the
parse to the memory; the thread's history and its since-block already
carry the whole walk, so the call adds no material of its own. Then the
walk closes the session DONE by its own hand, the ledger's deterministic
recap standing in when the call fails, is under-confident, or nobody is
wired to make it: completion never waits on a model.
"""

from physiclaw.conductor.walk.micro import SUMMARIZE, DecisionRequest, MicroOutcome
from physiclaw.conductor.walk.step import Step, Turn, Walk


class CloseStep(Step[None]):
    def __init__(self, walk: Walk) -> None:
        super().__init__(walk, None)
        self.sent: DecisionRequest | None = None
        n = walk.ledger.nodes
        self.fallback = walk.ledger.recap(
            f"{walk.ledger.ref} completed ({n}/{n} nodes)"
        )

    def open(self) -> Turn:
        walk = self.walk
        self.sent = walk.thread.request(SUMMARIZE, "close", (), {}, ledger=walk.ledger)
        return self.sent

    def resolve(self, outcome: MicroOutcome | None) -> Turn:
        walk = self.walk
        if outcome is None:
            return walk.close_done(self.fallback, None)
        assert self.sent is not None
        walk.thread.settle(self.sent, outcome, walk.ledger)
        payload = outcome.payload or {}
        return walk.close_done(
            payload.get("recap") or self.fallback, payload.get("memory") or None
        )
