"""The session's thread — one conversation with the model about THIS
errand, from the boot's parse to the closing memory.

Three moments of a walk need judgment about the user's request rather
than about a screen: reading the request off the chat thread
(`parse_task`), reading a reply the ask's own words could not
(`read_reply`), and writing the record when the walk is done
(`summarize`). They share one thread: one system prompt for the whole
session, turns appended and never rewritten, so each call's request is
the previous call's whole request plus one block and the provider's
prefix cache pays for everything but the news. What the playbook did
between two calls — the pick's decisions, the ask sent, the payment —
rides as one block from the ledger (`Ledger.offer`), not as turns: the
mechanical steps stay out of the model's context, their outcome stays
in. The boot opens the thread and hands it to the walk it activates.
"""

from dataclasses import dataclass, field
from typing import Any

from physiclaw.conductor.walk.ledger import Ledger
from physiclaw.conductor.walk.micro import (
    SINCE,
    Content,
    DecisionRequest,
    MicroOutcome,
    settled,
)
from physiclaw.contract.dto import THINKING_LEVELS, ImageBlock, Thinking


@dataclass
class Thread:
    turns: list[tuple[str, Content]] = field(default_factory=list)
    # The think level the thread's first call was asked with (the boot's
    # `select` step declares it); later calls that name none inherit
    # it, so one conversation thinks at one depth — a call left at the
    # vendor's default on a thinking model would ruminate for minutes
    # over a record that needs none.
    thinking: Thinking | None = None

    def request(
        self,
        call: str,
        node_id: str,
        outcomes: tuple[str, ...],
        material: dict[str, str],
        *,
        ledger: Ledger,
        listing: str = "",
        frame: ImageBlock | None = None,
        context: str = "",
        thinking: Thinking | None = None,
    ) -> DecisionRequest:
        """One call in the thread: the row's material, the ledger's
        delta since the last call, and every settled turn as history."""
        since = ledger.offer()
        # A step that names its own level wins for its own call; only
        # the FIRST level is remembered as the thread's. That honours
        # what the playbook wrote, at a price worth knowing: some
        # vendors invalidate the message-block cache when the thinking
        # parameter changes, so a thread whose calls disagree replays at
        # full price. Every shipped playbook says `off` throughout.
        if thinking is None:
            thinking = self.thinking
        elif self.thinking is None:
            self.thinking = thinking
        return DecisionRequest(
            call=call,
            node_id=node_id,
            outcomes=outcomes,
            material={**material, SINCE: "\n".join(f"- {c}" for c in since)},
            listing=listing,
            context=context,
            frame=frame,
            history=tuple(self.turns),
            thinking=thinking,
        )

    def settle(
        self, req: DecisionRequest, outcome: MicroOutcome, ledger: Ledger
    ) -> None:
        """Append the exchange verbatim — the block exactly as sent, the
        reply in the contract's canonical spelling — and mark the ledger
        reported up to here, so the next call carries only what came
        after."""
        self.turns.extend(settled(req, outcome))
        ledger.commit()

    def to_suspended(self) -> dict[str, Any]:
        """What survives a wake. The turns do NOT: a new wake is a new
        conversation. The think level does — it is a declaration, and
        without it a walk that resumes and finishes with no model call
        of its own would close at the vendor's default."""
        return {"think": self.thinking}

    def restore(self, data: dict[str, Any]) -> None:
        """A level a record from before this field was kept lacks, or one
        a hand-edited record garbles, reads as unset."""
        think = data.get("think")
        self.thinking = think if think in THINKING_LEVELS else None
