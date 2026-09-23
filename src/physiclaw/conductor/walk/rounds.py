"""A run's rounds as values — what they are planned from, what they are
fed, what they return — and the ref values a step reads at the cursor,
which a run's returns are part of.

Pure over the walk's records: the course (the route and the cursor),
the ledger (decisions, round records), the walk's inputs, and the
replies an ask read. The walk (`program.py`) decides WHEN a run expands
or a round finishes and applies the result to its course and ledger;
this module computes what those moments produce.
"""

from physiclaw.conductor.spec.model import (
    Node,
    PlaybookError,
    RunNode,
    resolve_inputs,
)
from physiclaw.conductor.spec.refs import fill_args, fill_refs, own_fields
from physiclaw.conductor.walk.course import Course, Round, run_keys
from physiclaw.conductor.walk.ledger import Ledger, round_prefix


def values(
    course: Course, ledger: Ledger, inputs: dict[str, str], replies: list[str]
) -> dict[str, str]:
    """Ref-resolution values at the cursor, keyed by the dotted ref
    spellings: the walk's inputs under `inputs.<name>`, every agent
    output under `node.field` (the one read of an unrecorded one a text
    can make is a step's own: its last answer, or empty), every run's
    returns under `run.field` (its finished rounds' values, one per
    line — empty before any), and `ask.replies` (the replies read so
    far, one per line — empty before any). Inside a run's round: that
    round's inputs and its own record, nothing of the route around it.
    The roots can never collide: the parser reserves `inputs` as a move
    id."""
    rd = course.round
    if rd is not None:
        return round_values(course.node, rd, ledger, replies)
    vals = {
        **own_fields(course.node),
        **ledger.previous,
        **inputs,
        **ledger.decided,
    }
    for run in course.spec.runs:
        keys = run_keys(run, vals)
        for fld in run.sub.returns:
            vals[f"{run.id}.{fld}"] = _run_return(ledger, run, fld, keys)
    vals["ask.replies"] = "\n".join(replies)
    return vals


def round_values(
    node: Node | None, rd: Round, ledger: Ledger, replies: list[str]
) -> dict[str, str]:
    """A round's refs: its inputs, its own record, the replies."""
    return {
        **own_fields(node),
        **rd.inputs,
        **ledger.round_values(rd.prefix),
        "ask.replies": "\n".join(replies),
    }


def _run_return(ledger: Ledger, run: RunNode, fld: str, keys: list[str]) -> str:
    """A run's return: its done rounds' values, in list order, one per
    line — a missed round has no line (its reason is the ledger's), so a
    report never lists what was not done."""
    lines = (ledger.round_return(round_prefix(run.id, k), fld) for k in keys)
    return "\n".join(v for v in lines if v)


def plan(run: RunNode, vals: dict[str, str], ledger: Ledger) -> list[Round]:
    """The rounds still to do for `run`: every key of its list (or the
    one round of a plain run) whose record is not in the ledger, each
    with its resolved inputs. Raises PlaybookError when the run cannot
    expand (the guarded callers hand over on it)."""
    keys = run_keys(run, vals)
    # `rounds:` bounds the WORK, not one reading of the list: a
    # revision re-plans and the run expands again, so counting only
    # today's items would hand each re-plan a fresh budget. Rounds
    # already on record count — a finished one is work this run did.
    todo = [k for k in keys if not ledger.round_finished(round_prefix(run.id, k))]
    total = ledger.round_count(run.id) + len(todo)
    if total > run.max_rounds:
        raise PlaybookError(
            f"run {run.id!r}: {total} rounds, more than its {run.max_rounds}"
        )
    planned: list[Round] = []
    for key in todo:
        # A ref that is empty BY DESIGN — a run's returns before any
        # round ends, an `each` whose rounds all missed — is not a
        # value: left out, so the sub's declared `default:` covers it,
        # and a required input fed nothing fails closed.
        provided = {
            k: str(v)
            for k, v in fill_args(run.args, vals, f"run {run.id!r}").items()
            if str(v) != ""
        }
        if run.each is not None:
            provided[run.each[0]] = key
        try:
            resolved = resolve_inputs(run.sub, provided)
        except PlaybookError as e:
            raise PlaybookError(f"run {run.id!r}: {e}") from e
        planned.append(Round(run, key, {f"inputs.{n}": v for n, v in resolved.items()}))
    return planned


def returns(rd: Round, vals: dict[str, str]) -> dict[str, str]:
    """A finished round's returns, filled from its own values (a
    template that cannot fill raises, and the guarded caller hands
    over)."""
    return {
        f: str(fill_refs(t, vals, where=f"{rd.run.id} `returns.{f}`"))
        for f, t in rd.run.sub.returns.items()
    }
