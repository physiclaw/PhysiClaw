"""The handover brief — the conductor's distilled report to the model.

A walk that stops mints ONE last synthesized ``[note, peek]`` turn
whose note carries these renderings, then answers None forever. The
note summary is exactly what compaction preserves, so the brief
outlives the turns it summarizes and the model never resumes blind;
the peek hands it the fresh view it would have had to take anyway (and
on a dead phone its error result is itself the evidence).

Pure text: the walk passes state in, nothing here reads the world.
The reason string arrives verbatim; when a path owes the model an
imperative (the deny back-out, the unlock doctrine), the caller already
wrote it into the reason, so this module never invents instructions.
"""

from physiclaw.conductor.walk.ledger import Ledger


def walk_brief(
    reason: str,
    *,
    ledger: Ledger,
    node: str | None,
    idx: int,
    consented: float | None,
) -> str:
    """A handed-over walk's report: why, where, and the walk's account
    (`Ledger.account` — the same facts every other exit reports) with
    the two money warnings a model taking over must read."""
    where = (
        f"node {node} ({idx + 1}/{ledger.nodes})"
        if node is not None
        else f"past the last node ({ledger.nodes}/{ledger.nodes})"
    )
    parts = [
        f"conductor handing over: {reason}.",
        f"Walk {ledger.ref} stopped at {where}.",
    ]
    account = ledger.account()
    if account:
        parts.append("So far: " + "; ".join(account) + ".")
    if consented is not None:
        # Consent is CONSUMED by firing (program.py), so a consented
        # value surviving to the brief proves the payment did NOT fire.
        parts.append(
            f"The user consented to ¥{consented:g}; the payment has NOT been made."
        )
    if ledger.paid is not None:
        parts.append(
            f"A payment of ¥{ledger.paid:g} was FIRED before this stop and its result "
            "is unverified — check the order before any further payment."
        )
    parts.append(
        "The synthesized turns above are the walk so far; this turn's "
        "peek shows the current screen. Verify state before acting."
    )
    return " ".join(parts)
