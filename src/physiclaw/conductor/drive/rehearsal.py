"""Drive one playbook on the live phone — the engine's loop without the
session.

No policy gates, no compaction, no trace, no sentinel: just the
conductor's contract — ask the Program for a turn, dispatch its one
action, feed the result back — so a rehearsal exercises the real walk.
`setup.arm` loads and validates everything BEFORE any connection
exists; `walk` runs the loop over an open client and returns one of
the `WALK_*` outcomes below. `emit` receives every progress line; the
core never prints, and `exchange.py` renders what it kept.

`playbooks run` is typer around it, `playbooks replay` shares `arm`,
and `playbooks step` (`debug/stepping.py`, the studio's driver too)
runs the same loop one node at a time with `Program.step_one` set, a
`transform` hook for the virtual channel, and `macro_opts` to narrow
the first macro run to a step range.
"""

import asyncio
from typing import TYPE_CHECKING

from physiclaw.common import gesture_vocab
from physiclaw.conductor.drive.exchange import (
    ModelLog,
    args_text,
    describe_result,
    describe_verdict,
    exchanges,
    result_lines,
)
from physiclaw.conductor.drive.hooks import (
    Emit,
    McpCaller,
    Observe,
    OnExchange,
    Transform,
)
from physiclaw.conductor.micro.calltable import has_fallback
from physiclaw.conductor.micro.decision import DecisionRequest, MicroResult
from physiclaw.conductor.spec.limits import REHEARSE_MAX_TURNS
from physiclaw.conductor.walk.surface import Paused
from physiclaw.conductor.walk.walklog import Outcome
from physiclaw.contract.dto import SystemMessage, ToolResultMessage, UserMessage

if TYPE_CHECKING:
    from physiclaw.conductor.micro.channel import MicroCaller
    from physiclaw.conductor.walk.program import Program
    from physiclaw.contract.dto import ToolCall
    from physiclaw.contract.plugin import WireSink
    from physiclaw.macros.model import Macro

# What `walk` answers when the walk ends — pinned so a caller reads
# the outcome instead of parsing prose. (The stepping driver has its
# own outcome vocabulary one level up; these are the loop's.)
WALK_ENDED = "walk finished or handed over — see the notes above"

WALK_COMPLETED = "walk completed — the session would close DONE"

WALK_SUSPENDED = "walk suspended waiting on you — suspension dropped"

WALK_STOPPED = "walk stopped by its own word — see the notes above"

WALK_PAUSED = "walk paused — the node settled"


async def walk(
    program: "Program",
    registry: "dict[str, Macro]",
    mcp: McpCaller,
    emit: Emit,
    caller: str = "cli",
    *,
    transform: Transform | None = None,
    macro_opts: dict | None = None,
    verbose: bool = False,
    observe: Observe | None = None,
    raw: bool = False,
    on_exchange: OnExchange | None = None,
    unlock: bool = True,
) -> str:
    """One armed walk over an already-open MCP client, one turn at a
    time, until it finishes, hands over, suspends, pauses (a stepping
    program's cursor moved), or hits the turn cap. Emits each turn's
    note, the verdict the next turn acted on, and (`verbose`) the
    result text — a macro's step log and the listing.
    `transform(call, blocks) -> blocks | None` rewrites a result before
    the program reads it (the debug fake-channel); `macro_opts` are
    `run_and_record` keywords for the first macro run only;
    `observe(call, blocks)` sees every real result before any rewrite
    (the studio renders the phone off it). Every model round-trip is
    captured (`ModelLog`): `raw` emits each one — the messages as sent
    and the reply as received — and `on_exchange(record)` receives it
    (the studio's expandable log entry). `unlock` is the one lock-screen
    preamble; a caller re-entering per node pays it once. Raises
    RuntimeError when a model call fires with no model configured."""
    history: list = [
        SystemMessage(content="rehearsal"),
        UserMessage(content="rehearse the armed walk"),
    ]
    decider = _Decider(emit, raw=raw, on_exchange=on_exchange)
    opts = dict(macro_opts or {})
    shown = None  # the last verdict printed — one line per reading
    try:
        # A rehearsal drives the phone NOW — wake it first if it locked
        # between runs (the runtime's boot does this at every real wake
        # through its `locked:` hand; a rehearsal owes the walk the
        # same floor) — unless the route's own start page declares that
        # hand, in which case the walk wakes the phone itself.
        if unlock and not _declares_locked_hand(program):
            await unlock_if_covered(mcp, emit)
        for _ in range(REHEARSE_MAX_TURNS):
            step = program.advance(history)
            if program.verdict is not None and program.verdict is not shown:
                shown = program.verdict
                emit(f"  {describe_verdict(shown)}")
            while isinstance(step, DecisionRequest):
                result = await decider.decide(step)
                step = program.resolve(result.outcome)
            if isinstance(step, Paused):
                return WALK_PAUSED
            if step is None:
                if program.baton is not None:
                    # The boot decided: the rehearsal ends where the
                    # wake would go on — say which walk, and how to
                    # rehearse it on its own.
                    baton = program.baton
                    emit(
                        f"  boot hands over to {baton.app}/{baton.spec.name} "
                        f"{baton.values} — rehearse it: physiclaw playbooks run "
                        f"{baton.app}/{baton.spec.name}"
                    )
                return WALK_ENDED
            note, act = step.tool_calls
            emit(f"  {note.arguments['summary']}")
            if act.name == "end_session":
                return _closed(program)
            emit(f"    → {act.name}({args_text(act.arguments)})")
            run_opts: dict = {}
            if opts and act.name == gesture_vocab.RUN_MACRO:
                run_opts, opts = opts, {}  # the first macro run only
            text, is_error = await dispatch(
                mcp,
                act,
                registry,
                caller=caller,
                transform=transform,
                observe=observe,
                **run_opts,
            )
            if verbose or is_error:
                for line in result_lines(text, verbose):
                    emit(f"      {line}")
            history.append(step)
            history.append(
                ToolResultMessage(tool_call_id=act.id, content=text, is_error=is_error)
            )
        return f"stopped after {REHEARSE_MAX_TURNS} turns"
    finally:
        # A rehearsal cut short (Ctrl-C, the turn cap) records its
        # abandoned row like a real wake's teardown would — latched, so
        # a walk that closed properly is a no-op.
        program.abandon()
        await decider.aclose()


class _Decider:
    """The rehearsal's side of a decision request: the model caller,
    built on FIRST use and after the connection — so "start the server
    first" is what a user without one hears, and a walk that never
    calls a model never pays a model-config error either — the one
    rule for a call with nobody wired, and the wire log every round
    trip lands in (`raw` emits each; `on_exchange` receives each)."""

    def __init__(
        self, emit: Emit, *, raw: bool, on_exchange: OnExchange | None
    ) -> None:
        self._emit = emit
        self._raw = raw
        self._on_exchange = on_exchange
        self._micro: "MicroCaller | None" = None
        self._unwired: str | None = None  # why no model can be called, once known
        self._wire = ModelLog()

    async def decide(self, step: DecisionRequest) -> MicroResult:
        if self._micro is None and self._unwired is None:
            try:
                self._micro = micro_caller(rlog=self._wire)
            except Exception as e:
                # A call that declares its own answer for "nobody is
                # wired" (the close writes the recap from the ledger)
                # gets it, as in `replay.py` and `Conductor._drive`; any
                # other call is the config error the user must hear.
                if not has_fallback(step.call):
                    raise
                self._unwired = f"nobody wired ({e})"
        if self._micro is not None:
            result = await self._micro.run(step)
        else:
            assert self._unwired is not None  # set the moment the build failed
            result = MicroResult(None, self._unwired, attempts=0, elapsed_ms=0)
        decision = describe_result(result)
        self._emit(f"  model {step.call} ({step.node_id}): {decision}")
        for record in exchanges(self._wire.drain(), step, decision):
            if self._raw:
                for line in record["lines"]:
                    self._emit(f"      {line}")
            if self._on_exchange is not None:
                self._on_exchange(record)
        return result

    async def aclose(self) -> None:
        if self._micro is not None:
            await self._micro.aclose()


def _closed(program: "Program") -> str:
    """The walk closed the session by its own hand, having recorded how
    it ended: a completion, a suspension for a later wake, or a stop
    (recorded as a handover — a brief never mints end_session). Only a
    suspension wrote a file, and a rehearsal has no later wake, so only
    then is it dropped — a real wake's pending suspension survives a
    rehearsal that merely completed."""
    if program.outcome is Outcome.SUSPENDED:
        program.drop_suspension()
        return WALK_SUSPENDED
    if program.outcome is Outcome.COMPLETED:
        return WALK_COMPLETED
    return WALK_STOPPED


def _declares_locked_hand(program: "Program") -> bool:
    recovery = program.spec.recovers.get(program.spec.start)
    return recovery is not None and recovery.locked is not None


async def unlock_if_covered(mcp: McpCaller, emit: Emit) -> None:
    """One peek; a lock-screen reading (the cover's hero clock, or the
    unlock hint text) gets one `unlock_phone`. Fail-open — a camera blip
    just lets the walk meet the world as it is."""
    from physiclaw.common import verdict
    from physiclaw.common.listing import Screen
    from physiclaw.conductor.spec.match import reads_as_locked

    try:
        screen = Screen.read(
            verdict.screen_text(await mcp.call_tool(gesture_vocab.PEEK, {}))
        )
        if reads_as_locked(screen):
            emit("  phone is locked — unlocking first")
            await mcp.call_tool(gesture_vocab.UNLOCK_PHONE, {})
    except Exception as e:
        emit(f"unlock preamble skipped ({e})")


async def dispatch(
    mcp: McpCaller,
    call: "ToolCall",
    registry: "dict[str, Macro]",
    caller: str = "cli",
    *,
    transform: Transform | None = None,
    observe: Observe | None = None,
    start_at: str = "",
    stop_after: str = "",
) -> tuple[str, bool]:
    """One synthesized action → the text its result carries.

    Routes the way the engine does: `run_macro` is the LOCAL tool (it
    runs a macro through the macro runner, which drives the same MCP
    connection step by step), everything else is a plain MCP call.
    `registry` holds qualified `app/name` macros — the pack's plus the
    channel's, like the engine's hidden registry. The reply text is
    EVERY text block of the result (`verdict.all_text`), exactly what
    the engine's tool result carries — a gesture's action line, a
    macro's header and step log — ahead of the listing the Program
    reads its screen out of, so an aborted macro's cause reaches the
    handover reason and the eye. `transform(call, blocks)` may replace a
    successful result's blocks (None keeps them) — the engine's
    debug-intercept seam, reproduced; `observe(call, blocks)` is told
    the real blocks first, rewritten or not; `start_at`/`stop_after`
    narrow a macro run to a step range (`run_and_record`'s own knobs)."""
    from physiclaw.common import verdict
    from physiclaw.macros import runner as macro_runner

    try:
        if call.name == "wait":
            # An engine-LOCAL tool, not an MCP one — the gate's reply
            # polling rides it, so the rehearsal sleeps in place exactly
            # like the engine's handler (which also requires `seconds`).
            seconds = float(call.arguments["seconds"])
            await asyncio.sleep(seconds)
            return f"waited {seconds:g}s", False
        if call.name == gesture_vocab.RUN_MACRO:
            qualified = call.arguments.get("name", "")
            spec = registry.get(qualified)
            if spec is None:
                return f"unknown pack macro {qualified!r}", True
            result = await macro_runner.run_and_record(
                spec,
                call.arguments.get("inputs") or {},
                mcp,
                caller=caller,
                start_at=start_at,
                stop_after=stop_after,
            )
            # A mid-run abort is a substantive result, exactly as the live
            # engine returns it: the step log and the current screen, for
            # the page check to judge. Only an unknown macro is an error.
            blocks, is_error = result.blocks, False
        else:
            blocks, is_error = await mcp.call_tool(call.name, call.arguments), False
        if observe is not None:
            observe(call, blocks)
        if transform is not None and not is_error:
            faked = transform(call, blocks)
            if faked is not None:
                blocks = faked
        return verdict.all_text(blocks), is_error
    except Exception as e:  # a rehearsal reports, it does not crash
        return f"{call.name} failed: {e}", True


def micro_caller(rlog: "WireSink | None" = None) -> "MicroCaller":
    """The decision channel a rehearsal needs — same resolution as the
    engine's (`[conductor] micro_model`, else the session model), so a
    rehearsal spends the model a real wake would. `rlog` is the wire
    sink every round-trip goes to (`ModelLog`). Raises RuntimeError
    when no model is configured."""
    from physiclaw.common.config import CONFIG, model_ref, parse_model_ref
    from physiclaw.conductor.micro.channel import MicroCaller
    from physiclaw.provider import make_provider

    ref = CONFIG.conductor.micro_model or model_ref()
    pid, mid = parse_model_ref(ref)
    return MicroCaller(
        make_provider(pid, mid),
        confidence_floor=CONFIG.conductor.micro_confidence,
        rlog=rlog,
    )
