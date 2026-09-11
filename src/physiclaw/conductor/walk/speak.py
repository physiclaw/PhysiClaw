"""The walk's voice — how a step speaks to the user and reads what
comes back, over the channel pack (`channel.py`).

Both `ask` (`step_ask.py`) and `tell` (`step_tell.py`) send the
playbook's `message:` VERBATIM (only the author knows the user's
language) through the channel's send macro, land on the thread, and
baseline it; an ask later reads the bubbles that arrived since against
its declared words (`reply.py`), and any later send sweeps them for a
deny first. What is shared lives here once — the send door, the
landing rule with its deny sweep, the thread check, the new-reply
read, the verdict, the deny disposition — so the two steps cannot
word one rule two ways.
"""

from collections.abc import Callable

from physiclaw.common import gesture_vocab
from physiclaw.conductor.spec import reply
from physiclaw.conductor.spec.conventions import THREAD_ID
from physiclaw.conductor.walk.step import Turn, Walk


def thread_mismatch(walk: Walk) -> str | None:
    """None when the current verdict IS the user thread; else why not."""
    if walk.channel is None:
        return "no channel pack"
    assert walk.verdict is not None
    return walk.mismatch(walk.verdict, THREAD_ID)


def send(
    walk: Walk,
    kind: str,
    text: str,
    yes: tuple[str, ...] = (),
    no: tuple[str, ...] = (),
) -> Turn:
    """The one door to the user: the channel's send macro with the
    authored text — or the handover when there is no send to run.
    `yes`/`no` are this send's declared reply words (an ask's; a tell
    reads no reply and declares none); they take over from the
    previous send's when this one LANDS (`sent_landed`), so the sweep
    for replies to the previous send judges by its words."""
    if walk.channel is None or walk.channel.send is None:
        return walk.handover(
            "no channel send macro — record playbooks/channel to enable asks and tells"
        )
    walk.gate.ask = text
    walk.gate.next_words = (yes, no)
    walk.ledger.say(text)
    walk.gate.tried_open = False
    return walk.synth(
        kind,
        "conductor: messaging the user",
        gesture_vocab.RUN_MACRO,
        {"name": walk.channel.send, "inputs": {"message": text}},
        channel=True,
    )


def new_replies(walk: Walk, *, after_ask: bool = True) -> list[str]:
    """The user's messages since the gate's baseline (`reply.py` owns
    the diff), off the current thread screen."""
    assert walk.screen is not None
    gate = walk.gate
    return reply.new_incoming(
        walk.screen.rows, gate.baseline, gate.ask, after_ask=after_ask
    )


def verdict(walk: Walk, messages: list[str]) -> reply.Answer | None:
    """The gate's own words over `messages` — the newest one is the
    answer; an uncovered newest message defers."""
    return reply.classify_all(
        messages, frozenset(walk.gate.yes), frozenset(walk.gate.no)
    )


def deny(walk: Walk, *, answered: bool = False) -> Turn:
    """The one deny disposition: no re-asks, and no second chance this
    session. The cursor entry's `on_fail` word decides the exit
    (`handover`): a stop records the fact, a brief adds what the model
    owes — the user's acknowledgement too, unless the ask already sent
    its `denied:` line (`answered`).

    Who records the refusal: the ask step, which knows WHICH ask was
    refused and writes `ledger.answered` beside its journal line. The
    sweep in `sent_landed` (a refusal typed while the walk was off in
    the app) writes nothing — an ask landing there still answers with
    its own line, a tell has no ask in scope — and its reason string
    carries the fact to the model instead. Giving the disposition the
    write would mean the gate carrying the ask's id, which it does not."""
    walk.gate.awaiting = False
    advice = (
        "back out of any open checkout or cart state this task created, and wrap up"
    )
    if not answered:
        advice = f"acknowledge them, {advice}"
    return walk.handover(
        "user declined the ask" + (", answered" if answered else ""), advice=advice
    )


def sent_landed(
    walk: Walk,
    on_deny: Callable[[], Turn] | None = None,
    on_other: Callable[[str], Turn] | None = None,
) -> Turn:
    """A send's landing, shared by ask and tell: it must be on the
    thread; anything the user sent while the walk was off in the app (an
    earlier ask's baseline) lands here as new, and a deny among it must
    stop the walk NOW — overwriting the baseline would swallow it
    forever (deny only: an old confirm word above the fresh ask is never
    treated as consent). `on_deny` is that disposition when the caller
    has a better one than the bare `deny` (an ask answers with its own
    `denied:` line); `on_other` reads anything else the user sent
    meanwhile, an ask's chance to revise before it asks a question the
    user already answered (None from it = nothing to revise, carry on).
    Then the thread is baselined. None = carry on."""
    wrong = thread_mismatch(walk)
    if wrong is not None:
        return walk.handover(f"channel send did not land on the thread ({wrong})")
    gate = walk.gate
    # The sweep needs a baseline to diff against; the deny among it needs
    # deny words to judge by — after a tell (which declares none) only
    # the revision read is left.
    # Judged by the previous send's words AND this one's: a bare yes or
    # no typed early is the gate's vocabulary either way — a no stops the
    # walk, a yes is never consent and never a revision.
    news = new_replies(walk, after_ask=False) if gate.baseline else []
    yes = frozenset(gate.yes) | frozenset(gate.next_words[0])
    no = frozenset(gate.no) | frozenset(gate.next_words[1])
    if reply.any_deny(news, yes, no):
        return on_deny() if on_deny is not None else deny(walk)
    if on_other is not None:
        others = [m for m in news if reply.normalize(m) not in yes]
        if others and (revised := on_other("\n".join(others))) is not None:
            return revised
    # The send landed: its words and its thread snapshot take over together.
    gate.yes, gate.no = gate.next_words
    assert walk.screen is not None
    gate.baseline = {label.strip() for label in walk.screen.labels}
    return None
