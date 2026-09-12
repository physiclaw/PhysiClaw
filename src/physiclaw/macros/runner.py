"""Macro runner — drive a validated `Macro`'s steps and compose the reply.

One macro run produces ONE tool result: a compact step log followed by
only the LAST step's view (image + listing). Intermediate views are
dropped here, never sent — that is the token win the whole feature
exists for. Because the result ends with a normal fused view and carries
a single verdict marker in its first text block, `engine.dispatch`
treats it exactly like any gesture result (verdict parse, observers,
screen supersede) with no special-casing.

The split, top to bottom: `model` holds the pure clause algebra, `steps`
holds one class per kind of action with its own `execute` (plus the
run-wide state, `RunContext`, kept beside the steps that mutate it), and
this module owns what neither can — the loop that walks the steps and
the composition of the one result (`_completed` / `_aborted`). So
`_run_step` reads as the macro doctrine itself (budget, idempotence,
gate, act) with no per-tool branching left in it.

Nothing branches: a decided-failed check or a tool error stops the run
and reports where, and recovery is the agent's job with the returned
screen to work from. The one forward jump a macro may hold (`if` /
`goto` / `mark`, see `parse`) skips a span, reported here exactly like
a `start_at` prefix — not executed, one event per step. `start_at`
begins at a step (by handle), reporting the skipped prefix as NOT
executed. Server-side safety (bbox validation,
AssistiveTouch guards, the hardware lock, auto-park) applies per step
unchanged; this module cannot bypass it.

Every result ends with the CURRENT screen, success or abort, so the
agent never spends a follow-up turn peeking: a fresh view rides along
as-is, and when the held one may be stale (a `wait` slept past it, or a
failed gesture may have actuated) it does ONE recovery `peek` instead.
If even that peek fails the header says so explicitly.

Takes the MCP caller as a parameter (`McpCaller` protocol) so the engine
passes its process singleton, the CLI passes its own client, and tests
pass a fake — no engine imports here.
"""

import logging
import time
from dataclasses import dataclass, replace
from typing import Any

from physiclaw.common import gesture_vocab, verdict
from physiclaw.macros import runlog, stats, store
from physiclaw.macros.inputs import resolve_inputs
from physiclaw.macros.model import (
    MAX_RUN_SECONDS,
    NO_GESTURES_NOTE,
    REASON_BAD_INPUT,
    REASON_TIMEOUT,
    REASON_TOOL_ERROR,
    Macro,
    MacroError,
)
from physiclaw.macros.steps import (
    McpCaller,
    RunContext,
    Step,
    StepOutcome,
    guard_outcome,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MacroRunResult:
    """Outcome of one replay. `blocks` are raw MCP-style blocks
    (`{"type": "text"|"image", ...}`) ready to hand back through the
    dispatch block path; the remaining fields feed `run_and_record`'s
    stats fold."""

    blocks: list[dict]
    ok: bool
    aborted_step: int | None = None  # 1-based; None on success
    reason: str | None = None
    detail: str = ""
    # How many steps actually ACTUATED a gesture (wait steps and
    # pre-actuation failures excluded). Zero on an abort means the phone
    # did not move — "failed before acting" is not "burned", so the
    # engine's one-strike rule stands down (`builtin_tool`).
    gestures: int = 0
    # `macro-run-<hex6>`, set by `run_and_record`. Returned rather than left
    # for the caller to scrape back out of the composed header — that would
    # make the header's wording a wire format.
    run_id: str = ""


async def run_and_record(
    spec: Macro,
    provided: dict[str, Any],
    mcp: McpCaller,
    caller: str = "engine",
    start_at: str = "",
    record_as: str = "",
    stop_after: str = "",
) -> MacroRunResult:
    """The one entry point callers use: replay + fold the outcome into
    stats and the per-step run log. The engine handler and the CLI
    rehearsal both go through here so recording (what counts as a run,
    how bad input is recorded, which keys survive pruning, what gets
    logged) cannot drift between layers. Mints the run's
    ``macro-run-<hex6>`` id — it rides in every log line, the result
    header, and stats, so one id references the whole run. The prune set
    is derived here from the dirs on disk — a disabled macro keeps its
    stats. Raises MacroError — recorded as bad_input — before any
    gesture fires.

    ``record_as`` overrides the name the run is recorded under (default:
    the spec's own). Pack-private macros record under their qualified
    ``app/name``, so their decay signal (`consecutive_aborts`) accrues in
    its own namespace instead of shadowing — or being pruned as — a user
    macro's."""
    name = record_as or spec.name
    known = store.list_names()
    rlog = runlog.RunLogger(name, caller)

    def record(ok: bool, step: int | None, reason: str | None, detail: str) -> None:
        # The one fold both endings share — run log and stats must agree.
        rlog.end(ok=ok, aborted_step=step, reason=reason, detail=detail)
        stats.record(
            name,
            ok=ok,
            known_names=known,
            step=step,
            reason=reason,
            detail=detail,
            run_id=rlog.run_id,
        )

    try:
        result = await run(
            spec, provided, mcp, rlog=rlog, start_at=start_at, stop_after=stop_after
        )
    except MacroError as e:
        record(ok=False, step=0, reason=REASON_BAD_INPUT, detail=str(e))
        raise
    record(
        ok=result.ok,
        step=result.aborted_step,
        reason=result.reason,
        detail=result.detail,
    )
    return replace(result, run_id=rlog.run_id)


async def run(
    spec: Macro,
    provided: dict[str, Any],
    mcp: McpCaller,
    rlog: "runlog.RunLogger | None" = None,
    start_at: str = "",
    stop_after: str = "",
) -> MacroRunResult:
    """Replay the steps in order. Raises MacroError (from input resolution
    or an unresolvable `start_at`) before any gesture fires; after the first
    gesture it always returns a result — a mid-run failure is a substantive
    outcome the agent must read, not an error to retry. `rlog` (threaded in
    by `run_and_record`) receives one event per step; None keeps the replay
    silent for unit tests.

    `start_at` is the handle of the step to begin at (see `_step_index`),
    for when the caller already did the leading steps by hand. The skipped prefix is NOT executed and is
    reported as such. `stop_after` names the last step to run — a
    rehearsal inspecting one gesture stops there and the rest is
    reported as not run, the same way."""
    values = resolve_inputs(spec, provided)
    start_at, stop_after = start_at.strip(), stop_after.strip()
    start = _step_index(spec, start_at, "start_at") if start_at else 1
    stop = (
        _step_index(spec, stop_after, "stop_after") if stop_after else len(spec.steps)
    )
    if stop < start:
        raise MacroError(
            f"stop_after {stop_after!r} (step {stop}) precedes the start step {start}"
        )
    ctx = RunContext(mcp=mcp, deadline=time.monotonic() + MAX_RUN_SECONDS)
    if rlog:
        rlog.start(values, start_at=start_at)
    ident = f" [{rlog.run_id}]" if rlog else ""
    log_lines = _skipped_prefix(spec, start, start_at, rlog)

    i = start
    while i <= stop:
        # Checks are templated exactly like step arguments: a macro that
        # pastes to `{contact}` wants to VERIFY it landed in `{contact}`'s
        # chat. See `model.TextClause.substituted`.
        step = spec.steps[i - 1].substituted(values)
        ctx.reads = 0
        t_step = time.monotonic()
        outcome = await _run_step(step, ctx)
        ctx.ran += 1
        if outcome.outcome in _ACTUATED and step.actuates:
            ctx.gestures += 1
        log_lines.append(_numbered(outcome.log_line, i))
        if rlog:
            rlog.step(
                i,
                step.tool,
                step.name,
                outcome.outcome,
                args=step.log_args,
                verdict=outcome.verdict,
                guard_polls=ctx.reads,
                ms=int((time.monotonic() - t_step) * 1000),
                detail=outcome.detail,
                screen_text=outcome.screen_text,
                view=outcome.view,
            )
        if outcome.stop:
            return await _aborted(ctx, spec, ident, log_lines, i, outcome, start)
        if outcome.verdict is not None:
            ctx.last_verdict = outcome.verdict
        if outcome.jump_to is not None:
            # The span up to the mark is not executed — reported like a
            # `start_at` prefix, one event per step — and the loop lands
            # PAST the mark: the read that took the jump is the mark's
            # own check. A `stop_after` inside the span ends the run at
            # the jump; the suffix report then covers the rest.
            log_lines += _jumped_span(
                spec, i, outcome.jump_to, stop, outcome.detail, rlog
            )
            i = outcome.jump_to + 1
            continue
        i += 1

    log_lines += _unrun_suffix(spec, stop, stop_after, rlog)
    return await _completed(ctx, spec, ident, log_lines, start, stop)


# Step outcomes that mean the tool actually fired (or attempted to):
# `ok`, a tool error mid-actuation, a timeout of the call itself. A
# guard/skip/expect miss never reached the phone.
_ACTUATED = ("ok", "tool_error", "timeout")


def _jumped_span(
    spec: Macro,
    at: int,
    target: int,
    stop: int,
    why: str,
    rlog: "runlog.RunLogger | None",
) -> list[str]:
    """The log lines for a taken jump at step `at`: the span it skipped
    — up to the mark at `target`, or the run's stop when that comes
    first — and the mark it landed on."""
    lines = _unrun(spec, at + 1, min(target - 1, stop), f"skipped — {why}", why, rlog)
    if target <= stop:
        mark = spec.steps[target - 1]
        lines.append(f"· {target}. {mark.display()} — landed")
        if rlog:
            rlog.step(target, mark.tool, mark.name, "ok", detail="landed by the jump")
    return lines


def _unrun(
    spec: Macro,
    first: int,
    last: int,
    line: str,
    detail: str,
    rlog: "runlog.RunLogger | None",
) -> list[str]:
    """The report of steps `first`-`last` (1-based, inclusive) this run
    did not execute: one log line, and — the forensic trail must show
    every step's fate, or `macros runs` jumps from step 1 to step 3
    with no explanation of the gap — one run-log event per step. []
    for an empty range."""
    if last < first:
        return []
    if rlog:
        for j in range(first, last + 1):
            unrun = spec.steps[j - 1]
            rlog.step(j, unrun.tool, unrun.name, "skipped", detail=detail)
    return [f"↷ {first}-{last}. {line}"]


async def _run_step(step: Step, ctx: RunContext) -> StepOutcome:
    """One step's whole life: budget, idempotence, gate, act.

    The order is doctrine, not convenience. The budget is checked BETWEEN
    steps, never mid-gesture, so the phone is in a known state when a run
    stops. `when` / `skip_when` come before the guard so a guard cannot
    abort a run for a step that is not needed. Everything tool-specific lives behind
    `step.execute`, which is why nothing here names a tool."""
    if ctx.out_of_time:
        detail = (
            f"macro exceeded its {MAX_RUN_SECONDS}s budget — the app is "
            "responding far slower than when this was rehearsed"
        )
        return StepOutcome(
            log_line=f"✗ {step.display()} — {detail}",
            outcome=REASON_TIMEOUT,
            detail=detail,
        )

    if step.when is not None or step.skip_when is not None:
        # One rule for both conditions: an unreadable screen satisfies no
        # check. A `skip_when` unsatisfied means the step runs (skipping
        # is an optimisation, and `{not: X}` is satisfied by a blank
        # screen — a camera hiccup must not silently drop a gesture); a
        # `when` unsatisfied means it does not (running is what `when`
        # withholds, and the steps written that way fire at pay buttons).
        screen = await ctx.read_screen()
        if step.when is not None and not (screen.readable and step.when.holds(screen)):
            why = (
                "its `when` does not hold"
                if screen.readable
                else "the screen could not be read"
            )
            return StepOutcome(
                log_line=f"↷ {step.display()} — skipped ({why})",
                outcome="skipped",
                view=ctx.last_view or None,
            )
        if (
            step.skip_when is not None
            and screen.readable
            and step.skip_when.holds(screen)
        ):
            return StepOutcome(
                log_line=f"↷ {step.display()} — skipped (already satisfied)",
                outcome="skipped",
                view=ctx.last_view or None,
            )

    if step.guard is not None:
        screen = await ctx.read_screen(retry=True)
        if not screen.readable:
            # An unreadable screen is not a satisfied guard. `require`
            # already fails closed (nothing matches ""), but a forbid-only
            # tripwire would PASS — the popup it exists to catch is merely
            # invisible, not absent — and the step would fire blind.
            return guard_outcome(step, "could not read the screen (`peek` failed)", "")
        detail = step.guard.check(screen)
        if detail:
            return guard_outcome(step, detail, screen.text)

    return await step.execute(ctx)


def _numbered(line: str, i: int) -> str:
    """Step lines carry their number after the status glyph (`✓ 3. tap …`).
    The glyph is chosen by the step, the number only known by the loop."""
    glyph, _, rest = line.partition(" ")
    return f"{glyph} {i}. {rest}"


def _skipped_prefix(
    spec: Macro, start: int, start_at: str, rlog: "runlog.RunLogger | None"
) -> list[str]:
    """The log lines for a `start_at` prefix this run did not execute."""
    if start <= 1:
        return []
    lines = _unrun(
        spec,
        1,
        start - 1,
        f"skipped (start_at {start_at!r} — done by you, not by this run)",
        f"start_at {start_at!r} — done by the caller",
        rlog,
    )
    if spec.steps[start - 1].guard is None:
        # Resuming lands a rehearsed bbox on a screen this macro did not
        # produce. A guard on the entry step is what makes that safe; say
        # plainly when there is none rather than implying it was checked.
        lines.append(f"  ⚠ step {start} has no guard — entry state was NOT verified")
    return lines


def _unrun_suffix(
    spec: Macro, stop: int, stop_after: str, rlog: "runlog.RunLogger | None"
) -> list[str]:
    """The log lines for a `stop_after` suffix this run did not execute —
    `_skipped_prefix`'s twin, one event per unrun step."""
    return _unrun(
        spec,
        stop + 1,
        len(spec.steps),
        f"not run (stop_after {stop_after!r})",
        f"stop_after {stop_after!r} — not run",
        rlog,
    )


def _step_index(spec: Macro, want: str, field: str) -> int:
    """A step handle (`idx3-tap-paste`, as the agent's `steps:` list and
    the run log show it) → its 1-based index. The handle carries the
    position AND the verb line, so one written down before the macro
    was edited fails loudly here rather than landing on a different
    gesture."""
    names = [s.name for s in spec.steps]
    if want in names:
        return names.index(want) + 1
    raise MacroError(
        f"{field} {want!r} names no step of macro {spec.name!r}. Steps: "
        f"{', '.join(names)}"
    )


async def _completed(
    ctx: RunContext,
    spec: Macro,
    ident: str,
    log_lines: list[str],
    start: int,
    total: int,
) -> MacroRunResult:
    """The success result — `_aborted`'s twin, so the two ways a run ends
    compose their reply the same way (header, step log, current view).
    "All steps" only when the loop judged every one: a run that jumped a
    span did not, and saying so would contradict the step log below."""
    view, view_note = await _current_view(ctx)
    count = len(spec.steps)
    jumped = (total - start + 1) - ctx.ran
    if start == 1 and total == count and not jumped:
        ran = f"all {total} steps completed"
    else:
        skipped = f" (1-{start - 1} skipped by start_at)" if start > 1 else ""
        unrun = f" ({total + 1}-{count} not run, stop_after)" if total < count else ""
        over = f" ({jumped} skipped by a jump)" if jumped else ""
        ran = f"steps {start}-{total} completed{skipped}{unrun}{over}"
    header = f"macro {spec.name}{ident}: {ran} — {view_note}."
    return MacroRunResult(
        blocks=_compose(header, log_lines, view, ctx.last_verdict),
        ok=True,
        gestures=ctx.gestures,
    )


async def _aborted(
    ctx: RunContext,
    spec: Macro,
    ident: str,
    log_lines: list[str],
    step_no: int,
    outcome: StepOutcome,
    start: int,
) -> MacroRunResult:
    reason = outcome.reason or ""
    # Steer the recovery: completed steps already moved the phone, so a
    # whole-macro re-run would replay them — say so explicitly instead of
    # trusting the model to infer it from "aborted". Counted from `start`,
    # not from 1: a start_at prefix was never executed by this run, and
    # telling the agent otherwise sends it to recover from a state it is
    # not actually in.
    before = ctx.ran - 1  # the loop judged the aborting step too
    if before == 0:
        completed = "no steps executed by this run"
    elif before == step_no - start:
        completed = f"steps {start}-{step_no - 1} already executed"
    else:
        completed = (
            f"{before} of steps {start}-{step_no - 1} already executed "
            "(the rest skipped by a jump)"
        )
    # A failed gesture may have actuated before erroring, so the retained
    # view can be stale; a failed check fired nothing, so it stays current.
    view, view_note = await _current_view(ctx, stale=reason == REASON_TOOL_ERROR)
    if ctx.gestures == 0:
        # "Failed before acting" is not "burned": nothing moved, so the
        # steering flips — clearing the blocker makes a retry safe. The
        # marker is one spelling (`model.NO_GESTURES_NOTE`); the
        # conductor's move-retry reads it off this very text.
        steer = (
            f"{NO_GESTURES_NOTE} — the phone did not move; clear the "
            "blocker and a re-run is safe."
        )
    else:
        steer = "Do NOT re-run the macro; continue manually from here."
    header = (
        f"macro {spec.name}{ident}: ABORTED at step {step_no}/{len(spec.steps)} "
        f"({reason}) — {completed}; {view_note}. {steer}"
    )
    return MacroRunResult(
        blocks=_compose(header, log_lines, view, ctx.last_verdict),
        ok=False,
        aborted_step=step_no,
        reason=reason,
        detail=outcome.detail,
        gestures=ctx.gestures,
    )


async def _current_view(
    ctx: RunContext, *, stale: bool = False
) -> tuple[list[dict], str]:
    """The view to ship with the result, guaranteed-current when possible.
    The last step's own fused view (image present, not stale) rides free;
    otherwise ONE recovery `peek` fetches the current screen — so the
    agent never needs a peek turn after a macro. Returns the blocks plus
    the header phrase describing them."""
    stale = stale or ctx.view_stale
    if ctx.has_view and not stale:
        return ctx.last_view, "the view below is the current screen"
    try:
        fresh = await ctx.mcp.call_tool(gesture_vocab.PEEK, {})
    except Exception as e:
        log.warning("macro recovery peek failed: %s", e)
        fresh = None
    if fresh is not None and verdict.has_image(fresh):
        return fresh, "the view below is a fresh `peek` of the current screen"
    # Recovery peek failed — say exactly what the agent is looking at.
    if stale and ctx.last_view:
        return ctx.last_view, (
            "the view below may be STALE (pre-failure; recovery `peek` also failed)"
        )
    if ctx.last_view:
        return ctx.last_view, (
            "no screen image available (recovery `peek` failed) — `peek` to see it"
        )
    return [], "no view available (recovery `peek` failed) — try `peek` yourself"


def _compose(
    header: str,
    log_lines: list[str],
    last_view: list[dict],
    last_verdict: bool | None,
) -> list[dict]:
    """[step-log text, *last view]. Exactly one verdict marker in the whole
    result: `verdict.attach` stamps the log (first block) with the last
    gesture's verdict, and the retained view's own texts are
    `verdict.defang`ed so the marker dispatch parses is always ours."""
    text = "\n".join([header, *log_lines])
    out: list[dict] = [{"type": "text", "text": verdict.attach(text, last_verdict)}]
    for b in last_view:
        if b.get("type") == "text":
            out.append({"type": "text", "text": verdict.defang(b.get("text") or "")})
        else:
            out.append(b)
    return out
