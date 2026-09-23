"""The `do` step — one recorded macro, framed by its pages.

Before the move: its enter page (when declared — a `start` has none
and runs unconditionally) must match the current screen. After it: its
verify page must match the screen the macro result carries. A payment
move additionally runs the fire-time money predicates in code on the
current screen, after the human's consent (`money.py` owns the rules),
and consumes that consent by firing.

A macro that fails hands over — nothing re-runs in the background;
the author who wants a popup cleared or a locked phone woken declares
it (a `skip_when` in the macro, a `recover:` on the page).
"""

from physiclaw.common import gesture_vocab
from physiclaw.conductor.spec.conventions import page_id
from physiclaw.conductor.spec.model import DoNode
from physiclaw.conductor.spec.pack import qualified_macro
from physiclaw.conductor.spec.refs import fill_args
from physiclaw.conductor.walk import money, recover
from physiclaw.conductor.walk.surface import Step, Turn

KIND_RUN = "do"


class DoStep(Step[DoNode]):
    kinds = frozenset({KIND_RUN})

    def open(self) -> Turn:
        walk, node = self.walk, self.node
        gate = walk.enter_gate(node)
        if gate is not None:
            return gate
        if node.pays:
            blocked = money.payment_block(
                walk.verdict,
                walk.screen,
                walk.app,
                f"payment move {node.id!r}",
                consented=walk.gate.consented,
                total_label=walk.gate.total_label,
            )
            if blocked is not None:
                return walk.handover(blocked)
        inputs = fill_args(node.args, walk.ref_values(), f"move {node.id!r}")
        args: dict = {"name": qualified_macro(walk.app, node.macro)}
        if inputs:
            args["inputs"] = inputs
        if node.pays:
            walk.spend_consent()
        return walk.synth(
            KIND_RUN,
            f"conductor: move {walk.course.label()} — "
            f"macro {node.macro}, verify {node.verify}",
            gesture_vocab.RUN_MACRO,
            args,
        )

    def landed(self, kind: str) -> Turn:
        walk, node = self.walk, self.node
        # Whatever the verify check says next, money may have moved:
        # the purchase line lands the moment the result does.
        walk.log_purchase()
        assert walk.verdict is not None
        expected = page_id(walk.app, node.verify)
        wrong = walk.verdict.mismatch(expected)
        if wrong is not None:
            return walk.recover_or_handover(
                node,
                expected,
                recover.Mode.VERIFY,
                f"move {node.id!r} did not land on {node.verify!r} ({wrong})",
            )
        # The move did what it was declared to do — a fact the session
        # thread reads between its calls. Without it a walk whose
        # substance IS its moves (a browse or scroll route, whose agent
        # returns little) leaves the closing summary almost nothing to
        # write from. A page is not a node — it is this move's `verify`
        # — so landing IS the page's confirmation, and one clause
        # records both. Note this reaches the thread, not the handover
        # brief: the brief renders `Ledger.account`, which is the typed
        # fields only.
        walk.ledger.note(f"ran {node.macro}, now on {node.verify}")
        return walk.advance_cursor()
