"""Commands for authoring and rehearsing app packs.

`init` scaffolds a disabled pack; `install` validates a staged template before
replacing files. `list` and `check` share a catalogue, with validity and live
readiness reported separately. `run`, `step`, and `replay` wrap the rehearsal,
stepping, and replay drivers also used by the studio. `stats`, `propose`, and
`micro` turn recorded runs into authoring feedback.

Rehearsals drive the phone immediately without saving a cross-wake suspension.
Stepping keeps only its debugger position; replay uses recorded screens.
"""

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional

import typer

from physiclaw.cli._format import (
    exit_error,
    ok,
    parse_inputs,
    section,
    state_tag,
    step_fail,
    warn,
)
from physiclaw.cli.pages import pages_app
from physiclaw.common.paths import PACK_FILENAME
from physiclaw.common.ready import START_HINT
from physiclaw.conductor.bench import runs
from physiclaw.conductor.drive import rehearsal, setup
from physiclaw.conductor.spec.model import PlaybookError

if TYPE_CHECKING:
    from physiclaw.conductor.bench import decisions
    from physiclaw.conductor.load.pack import PlaybookEntry
    from physiclaw.conductor.spec.model import Playbook
    from physiclaw.conductor.spec.pack import Pack

playbooks_app = typer.Typer(no_args_is_help=True)

# The two knobs `run` and `step` share, spelled once.
VerboseOpt = Annotated[
    bool, typer.Option("--verbose", "-v", help="Print every result's listing rows.")
]
RawOpt = Annotated[
    bool,
    typer.Option(
        "--raw",
        help="Print every model round-trip: the messages as sent to the "
        "provider and the reply as received (an agent step's context, "
        "prompt, screen block, and answer).",
    ),
]
playbooks_app.add_typer(
    pages_app,
    name="pages",
    help="A pack's page fingerprints: propose anchors, match screens, "
    "extract a corpus, calibrate geometry.",
)


@playbooks_app.command()
def init(
    app: Annotated[
        str,
        typer.Argument(
            help="App name (lowercase/digits/hyphens); `channel/<im>` for a "
            "user-channel pack."
        ),
    ],
) -> None:
    """Scaffold a new app pack: APP.yml (meta + pages), a README, an
    example playbook folder, and an example pack macro — parse-clean,
    disabled. `channel/<im>` scaffolds the user channel for one IM app."""
    from physiclaw.common.paths import PACK_FILENAME
    from physiclaw.conductor.load import scaffold
    from physiclaw.conductor.spec.conventions import (
        BOOT_PLAYBOOK,
        CHANNEL_APP,
        IOS_APP,
        THREAD_PAGE,
    )
    from physiclaw.conductor.spec.specfile import SpecError

    try:
        root = scaffold.init_pack(app)
    except (SpecError, OSError, UnicodeError) as e:
        exit_error(str(e))
    typer.echo(ok(str(root)))
    typer.echo("Next:")
    if root.parent.name == CHANNEL_APP:
        typer.echo(
            f"  1. anchor the `{THREAD_PAGE}` page on YOUR chat header in {PACK_FILENAME}"
        )
        typer.echo("  2. record the send/open gesture paths in macros/*.yml")
        typer.echo("  3. rehearse both with `physiclaw macros run channel/<macro>`")
        typer.echo("     and then enable them;")
        typer.echo(
            f"     the boot ({BOOT_PLAYBOOK}/PLAYBOOK.yml) goes live with `open`"
        )
        typer.echo(
            f"  4. capture geometry: physiclaw playbooks pages calibrate {CHANNEL_APP}"
        )
    elif app == IOS_APP:
        typer.echo("  1. check the lock-screen reading matches your phone's language")
        typer.echo("     (lock it, then: physiclaw playbooks pages propose --live)")
        typer.echo(
            f"  2. capture geometry: physiclaw playbooks pages calibrate {IOS_APP} --guided"
        )
    else:
        typer.echo(
            f"  1. declare `pages:` in {PACK_FILENAME} (physiclaw playbooks pages propose --live)"
        )
        typer.echo(
            f"  2. write the route in {scaffold.EXAMPLE_PLAYBOOK}/PLAYBOOK.yml (one "
            "playbook per folder beside the manifest) and its macros, then: "
            "physiclaw playbooks check"
        )
        typer.echo("  3. capture geometry: physiclaw playbooks pages calibrate " + app)


@playbooks_app.command()
def install(
    src: Annotated[
        Path,
        typer.Argument(
            help="A template pack directory (e.g. the repo's playbooks/taobao)."
        ),
    ],
    set_values: Annotated[
        Optional[list[str]],
        typer.Option(
            "--set",
            help="Fill a placeholder non-interactively: KEY=VALUE (repeatable).",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an already-installed pack wholesale."),
    ] = False,
) -> None:
    """Install a shared template pack into ~/.physiclaw/playbooks/<app>
    verbatim — `<<PLACEHOLDER>>` tokens stay in the files (diffable
    against the template); their values — per-installation constants
    like the IM contact name — go to playbooks/placeholders.yml, where
    every parser fills them at load. Shared templates ship disabled:
    rehearse, then enable. Each file's enabled flag is preserved."""
    from physiclaw.common.placeholders import (
        PLACEHOLDER_VALUES_FILENAME,
        placeholder_values,
    )
    from physiclaw.conductor.load.install import install_template, read_template
    from physiclaw.port.locks import FileLockBusy

    try:
        template = read_template(src)
        template.destination(force=force)
        if template.metadata.get("description"):
            typer.echo(template.metadata["description"])
        new_values = _gather_values(
            template.tokens,
            template.metadata.get("placeholders") or {},
            set_values,
            placeholder_values(),
        )
        dest, pack = install_template(template, new_values, force=force)
    except FileLockBusy:
        exit_error("another playbook install is already running")
    except (OSError, ValueError) as e:
        exit_error(str(e))
    if new_values:
        typer.echo(
            f"values recorded in playbooks/{PLACEHOLDER_VALUES_FILENAME}: "
            + ", ".join(sorted(new_values))
        )
    typer.echo(f"installed {template.app} → {dest} ({len(template.files)} file(s))")
    _check_app(template.app, pack)
    typer.echo(
        "next: rehearse (`physiclaw playbooks run`), capture pages "
        f"(`physiclaw playbooks pages calibrate {template.app}`), then set `enabled: true`."
    )


def _gather_values(
    tokens: list[str],
    manifest: dict,
    set_values: list[str] | None,
    existing: dict[str, str],
) -> dict[str, str]:
    """The values to ADD to placeholders.yml: `--set` pairs first (they
    may override an existing value), a prompt (with the manifest's
    prose) for tokens with no value yet — every value plain and
    non-empty."""
    from physiclaw.common.placeholders import find_placeholders

    values = parse_inputs(set_values or [], flag="--set")
    unknown = sorted(set(values) - set(tokens))
    if unknown:
        exit_error(
            f"--set names no placeholder in this pack: {', '.join(unknown)} "
            f"(pack has: {', '.join(tokens) or '(none)'})"
        )
    for tok in tokens:
        if tok in values or tok in existing:
            continue
        meta = manifest.get(tok) or {}
        prompt = f"{tok} — {meta.get('description', 'value')}"
        if meta.get("example"):
            prompt += f" (e.g. {meta['example']})"
        values[tok] = typer.prompt(prompt).strip()
    for tok, value in values.items():
        if not value or find_placeholders(value):
            exit_error(f"placeholder {tok} needs a plain non-empty value")
    return values


def _warn_strays() -> None:
    from physiclaw.conductor.load import pack as pb

    for stray in pb.stray_dirs():
        typer.echo(
            warn(
                f"{stray} holds playbook files but no {PACK_FILENAME} — not a pack "
                "until the manifest exists (it may be empty)"
            )
        )


def _installed_apps() -> list[str]:
    """Prepare the catalogue's README and report expected filesystem failures."""
    from physiclaw.conductor.load import pack as pb
    from physiclaw.conductor.load import scaffold

    try:
        scaffold.ensure_format_readme()
        _warn_strays()
        return pb.list_apps()
    except (OSError, UnicodeError) as e:
        exit_error(str(e))


@playbooks_app.command("list")
def list_cmd() -> None:
    """List every pack and its playbooks: enabled, disabled, or invalid."""
    from physiclaw.conductor.load import pack as pb

    apps = _installed_apps()
    if not apps:
        typer.echo(
            "No app packs found. Scaffold one (physiclaw playbooks init "
            "<app>) or install a shared template (physiclaw playbooks "
            "install <dir>) — see ~/.physiclaw/playbooks/README.md"
        )
        return
    for app in apps:
        typer.echo(f"{_pack_label(app)}/")
        try:
            entries = pb.scan_playbooks(app)
        except (pb.PlaybookError, OSError, UnicodeError) as e:
            typer.echo(f"  {step_fail(str(e))}")
            continue
        for entry in entries:
            tag = state_tag(
                valid=entry.spec is not None,
                enabled=bool(entry.spec and entry.spec.enabled),
            )
            detail = (
                (entry.error or "")
                if entry.spec is None
                else f"{entry.spec.description} ({len(entry.spec.nodes)} nodes)"
            )
            typer.echo(f"  {tag} {entry.name}  {detail}")


@playbooks_app.command()
def run(
    ref: Annotated[str, typer.Argument(help="<app>/<playbook> to rehearse.")],
    inputs: Annotated[
        list[str] | None,
        typer.Option(
            "--input",
            "-i",
            help="NAME=VALUE for a declared playbook input (repeatable).",
        ),
    ] = None,
    verbose: VerboseOpt = False,
    raw: RawOpt = False,
) -> None:
    """Rehearse one playbook against the running server — the mirror of
    `physiclaw macros run`, and the way to test a walk without waiting for
    a wake or hoping the boot picks it.

    Drives the real walk on the real phone: moves run their macros, agent
    steps spend real model calls, and an `ask` really messages you and
    waits for your reply. Works while the playbook is disabled — rehearse
    first, enable after. Nothing is persisted: a walk that suspends here
    stops instead of leaving a file for the next wake."""
    from physiclaw.conductor.spec.specfile import SpecError

    app, name = _split_ref(ref)
    try:
        outcome = asyncio.run(
            _rehearse(app, name, parse_inputs(inputs or []), verbose=verbose, raw=raw)
        )
    except SpecError as e:
        exit_error(str(e))
    except ConnectionError as e:
        # Same branch as `macros run` (`START_HINT` says why `mcp`).
        exit_error(f"{e}. {START_HINT}")
    except (RuntimeError, OSError, UnicodeError) as e:
        # `rehearsal.micro_caller` — an agent step fired with no model configured.
        exit_error(str(e))
    typer.echo(outcome)


@playbooks_app.command()
def step(
    ref: Annotated[str, typer.Argument(help="<app>/<playbook> to step through.")],
    inputs: Annotated[
        list[str] | None,
        typer.Option(
            "--input",
            "-i",
            help="NAME=VALUE for a declared playbook input (a fresh walk only).",
        ),
    ] = None,
    at: Annotated[
        Optional[str],
        typer.Option(
            "--at",
            help="Put the cursor on this node (its name) first — jump there, "
            "or re-run the node after editing it.",
        ),
    ] = None,
    to: Annotated[
        Optional[str],
        typer.Option(
            "--to",
            help="Keep stepping until the cursor has passed this node "
            "(`end`: run the route out).",
        ),
    ] = None,
    outputs: Annotated[
        list[str] | None,
        typer.Option(
            "--output",
            "-o",
            help="NODE.FIELD=VALUE — seed a pure-text agent's answer; the walk "
            "starts past it without a call.",
        ),
    ] = None,
    reply: Annotated[
        Optional[str],
        typer.Option(
            "--reply",
            help="Stage the user's reply for the next ask (the user channel "
            "is virtual while stepping).",
        ),
    ] = None,
    start_at: Annotated[
        str,
        typer.Option(
            "--start-at",
            help="Begin this node's macro at this step (its handle).",
        ),
    ] = "",
    stop_after: Annotated[
        str,
        typer.Option(
            "--stop-after",
            help="Stop this node's macro after this step (same handle).",
        ),
    ] = "",
    verbose: VerboseOpt = False,
    raw: RawOpt = False,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Print the outcome and position as JSON (for an agent's loop).",
        ),
    ] = False,
    reset: Annotated[
        bool, typer.Option("--reset", help="Forget the stored position and stop.")
    ] = False,
    status: Annotated[
        bool, typer.Option("--status", help="Show the stored position and stop.")
    ] = False,
) -> None:
    """Step one playbook through the live phone, one node per invocation
    — the playbook debugger (the studio's playbook panel drives the
    same core with a mouse).

    Each invocation re-reads the playbook (an edit applies at once),
    rebuilds the walk at its stored position (cursor, agent outputs, the
    gate's consent), runs the node there — its enter check, its macro or
    episode or ask, its verify check, its declared recovery — and pauses
    the moment the cursor moves, printing each turn's verdict, model
    answer, and macro step log. `--at` re-runs a node you just edited;
    `--start-at`/`--stop-after` narrow its macro to one gesture. The user
    channel is virtual (`--reply` stages the answer; the send still runs
    on the phone), no model ever takes over, and nothing is persisted
    beyond the position file (`debug/walk.json`). `--json` prints the
    outcome (paused / suspended / stopped / completed) and the position
    for a loop to read."""
    import json

    from physiclaw.conductor.spec.specfile import SpecError
    from physiclaw.debug import stepping

    app, name = _split_ref(ref)
    if reset and status:
        exit_error("--reset and --status cannot be used together", code=2)
    if reset:
        try:
            message = stepping.reset(app, name)
        except (SpecError, OSError, UnicodeError) as e:
            exit_error(str(e))
        typer.echo(
            json.dumps(
                stepping.StepResult("reset", message, None).to_dict(),
                ensure_ascii=False,
            )
            if as_json
            else message
        )
        return
    if status:
        try:
            pos = stepping.status(app, name)
        except (SpecError, OSError, UnicodeError) as e:
            exit_error(str(e))
        if as_json:
            typer.echo(json.dumps(pos.to_dict() if pos else None, ensure_ascii=False))
        else:
            typer.echo(
                pos.describe()
                if pos
                else f"{app}/{name}: no stored position — the next step starts the route"
            )
        return
    try:
        result = asyncio.run(
            _step(
                app,
                name,
                parse_inputs(inputs or []),
                at=at,
                to=to,
                outputs=parse_inputs(outputs or [], "--output"),
                reply=reply,
                start_at=start_at,
                stop_after=stop_after,
                verbose=verbose,
                raw=raw,
                # Under --json stdout is the JSON alone; the progress
                # lines go to stderr, where a loop can still read them.
                emit=lambda line: typer.echo(line, err=as_json),
            )
        )
    except SpecError as e:
        exit_error(str(e))
    except ConnectionError as e:
        exit_error(f"{e}. {START_HINT}")
    except (RuntimeError, OSError, UnicodeError) as e:
        exit_error(str(e))
    typer.echo(
        json.dumps(result.to_dict(), ensure_ascii=False) if as_json else result.message
    )


@playbooks_app.command()
def replay(
    ref: Annotated[str, typer.Argument(help="<app>/<playbook> to replay.")],
    session: Annotated[
        Optional[str],
        typer.Option("--session", help="A recorded session id (suffix ok if unique)."),
    ] = None,
    listings: Annotated[
        Optional[list[Path]],
        typer.Option("--listing", help="A listing text file, one screen (repeatable)."),
    ] = None,
    inputs: Annotated[
        list[str] | None,
        typer.Option("--input", "-i", help="NAME=VALUE for a declared input."),
    ] = None,
    outputs: Annotated[
        list[str] | None,
        typer.Option(
            "--output",
            "-o",
            help="NODE.FIELD=VALUE — what a pure-text agent step answers.",
        ),
    ] = None,
) -> None:
    """Replay the real walk over recorded screens — no phone, no model.
    Each screen is the result of the walk's next action; the report
    shows the node, the verdict it acted on, the tool, and where the
    walk stopped. A session's screens come from its wire log; a
    listing file is one screen. Writes nothing."""
    from physiclaw.cli._sessions import resolve_sid
    from physiclaw.common.text import read_text
    from physiclaw.conductor.bench import corpus
    from physiclaw.conductor.bench import replay as replay_mod

    app, name = _split_ref(ref)
    if (session is None) == (not listings):
        exit_error("need exactly one of --session or --listing", code=2)
    try:
        if listings:
            screens = [read_text(p) for p in listings]
        else:
            assert session is not None  # the exactly-one check above
            screens = corpus.session_listings(resolve_sid(session))
        program, _ = setup.arm(
            app,
            name,
            parse_inputs(inputs or []),
            emit_warn=lambda s: typer.echo(warn(s)),
            dry=True,
        )
    except (OSError, UnicodeError, PlaybookError) as e:
        exit_error(str(e))
    result = replay_mod.replay(
        program, screens, parse_inputs(outputs or [], flag="--output")
    )
    for i, t in enumerate(result.turns, 1):
        typer.echo(
            f"[{i:3d}] {t.node or '(end)':16s} {t.verdict:28s} {t.tool:12s} {t.note}"
        )
    typer.echo(f"{result.outcome}: {result.detail}")


@playbooks_app.command()
def stats(
    last: Annotated[
        int,
        typer.Option("--last", min=0, help="How many recent runs to list (0 = none)."),
    ] = 10,
) -> None:
    """Per-playbook walk outcomes from playbooks/runs.jsonl — the
    escalation-rate KPI. A playbook that keeps handing over at one node
    is a rehearsal bug this table points at."""

    rows = runs.load()
    if not rows:
        typer.echo(
            "No walks recorded yet — runs land in playbooks/runs.jsonl "
            "as playbooks execute (wakes and rehearsals alike)."
        )
        return
    for key, st in sorted(runs.summarize(rows).items()):
        typer.echo(
            f"{key}: runs={st.runs} completed={st.completed} "
            f"suspended={st.suspended} handover={st.handover} "
            f"crashed={st.crashed} abandoned={st.abandoned} "
            f"escalation={st.escalation_rate:.0%} "
            f"micros={st.micros} rescues={st.rescues}"
        )
        for node, count, reason in st.hot_nodes():
            typer.echo(f"  ✗ {node} ×{count}  {reason}")
    if last > 0:
        typer.echo("\nrecent:")
        for rec in rows[-last:]:
            where = rec.get("node") or "(end)"
            reason = str(rec.get("reason") or "")
            typer.echo(
                f"  [{rec.get('ts', '?')}] {rec.get('app', '?')}/"
                f"{rec.get('playbook', '?')} {rec.get('outcome', '?')} "
                f"at {where}" + (f" — {reason}" if reason else "")
            )
    _decisions_section(rows)


def _decisions_section(rows: list[dict]) -> None:
    """What the recorded walks' decision calls cost, per call kind, off
    the sessions the runs name — the second KPI beside escalation: a
    decision that takes minutes or escalates is a `think:` level, a
    prompt, or a model to change."""
    from physiclaw.conductor.bench import decisions

    dirs = runs.session_dirs(rows)
    stats = decisions.decision_stats(dirs)
    if not stats:
        return
    typer.echo(f"\ndecisions ({len(dirs)} session(s) on disk):")
    for call, st in sorted(stats.items()):
        nodes = ", ".join(f"{n} ×{c}" for n, c in sorted(st.nodes.items()))
        typer.echo(
            f"  {call}: calls={st.calls} escalated={st.escalated} "
            f"repaired={st.repaired} mean={st.mean_ms / 1000:.1f}s "
            f"max={st.ms_max / 1000:.1f}s reasoning={st.mean_reasoning:.0f}/call "
            f"output={st.mean_output:.0f}/call rows≤{st.rows_max}  [{nodes}]"
        )


def _mark(r: "decisions.Reply", rec: "decisions.Recorded") -> str:
    """One word on a reply, beside the recorded move — the same
    agreement `decisions.summarize` counts."""
    from physiclaw.conductor.bench import decisions

    if r.error:
        return "error"
    if not rec.allowed:
        return "unjudged"
    if not r.valid:
        return "invalid"
    return "same" if decisions.agreed(r, rec) else "differs"


def _move(answer: "str | None", args: "dict | None") -> str:
    """A recorded or replayed move in words (`describe_move`); an
    invalid reply reads as None."""
    from physiclaw.conductor.micro.moves import describe_move

    return describe_move(answer, args or {}) if answer is not None else "None"


@playbooks_app.command()
def micro(
    session: Annotated[
        str, typer.Argument(help="A recorded session id (suffix ok if unique).")
    ],
    model: Annotated[
        Optional[str],
        typer.Option(
            "--model",
            help="provider/model to ask (default: [conductor] micro_model, "
            "else the session model).",
        ),
    ] = None,
    think: Annotated[
        Optional[str],
        typer.Option(
            "--think", help="off|low|medium|high — override the recorded level."
        ),
    ] = None,
    reps: Annotated[
        int, typer.Option("--reps", help="How many times to ask each call.")
    ] = 1,
    only: Annotated[
        Optional[str],
        typer.Option("--only", help="Only this call kind (parse_task…) or node id."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Rows as JSON.")] = False,
) -> None:
    """Re-ask a session's recorded decision calls against a live provider
    and measure them: valid replies, agreement with what the wake read,
    time, hidden tokens. The request goes exactly as the wake sent it —
    a prompt or pack edit is measured by a new walk, a model or think
    level by this. Spends tokens; writes nothing."""
    from dataclasses import asdict

    from physiclaw.cli._sessions import resolve_sid
    from physiclaw.common import paths
    from physiclaw.common.config import CONFIG
    from physiclaw.common.model_ref import parse_model_ref
    from physiclaw.conductor.bench import decisions
    from physiclaw.contract.dto import THINKING_LEVELS, Thinking
    from physiclaw.provider import make_chat_provider

    level: Thinking | None = None
    if think is not None:
        if think not in THINKING_LEVELS:
            exit_error(f"--think must be one of {', '.join(THINKING_LEVELS)}", code=2)
        level = think
    if reps < 1:
        exit_error("--reps must be at least 1", code=2)
    d = paths.sessions_dir() / resolve_sid(session)
    try:
        records = decisions.load(d, only=only or None)
    except OSError as e:
        exit_error(str(e))
    if not records:
        exit_error("no recorded decision calls match")
    ref = model or CONFIG.conductor.micro_model or CONFIG.agent.model
    try:
        pid, mid = parse_model_ref(ref)
        provider = make_chat_provider(pid, mid)
    except Exception as e:
        exit_error(f"cannot build provider for {ref!r}: {e}")
    if not as_json:
        typer.echo(
            section(
                f"{len(records)} decision(s) × {reps} on {ref}"
                + (f" think={think}" if think else " (recorded think levels)")
            )
        )

    async def _run() -> list[decisions.Row]:
        try:
            return [
                await decisions.ask(provider, r, thinking=level, reps=reps)
                for r in records
            ]
        finally:
            await provider.aclose()

    rows = asyncio.run(_run())
    if as_json:
        typer.echo(
            json.dumps(
                [
                    {
                        "call": row.recorded.call,
                        "node": row.recorded.node,
                        "recorded": row.recorded.answer,
                        "recorded_args": row.recorded.args,
                        "replies": [asdict(r) for r in row.replies],
                    }
                    for row in rows
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    unjudged = sum(1 for row in rows if not row.recorded.allowed)
    if unjudged:
        typer.echo(
            warn(
                f"{unjudged} record(s) predate the caller's reading (no allowed "
                "answers kept) — their replies are shown, not judged"
            )
        )
    for row in rows:
        rec = row.recorded
        typer.echo(
            f"{rec.node or '?'} ({rec.call}) recorded: {_move(rec.answer, rec.args)}"
            + (f" {rec.confidence:.2f}" if rec.confidence is not None else "")
        )
        for r in row.replies:
            mark = _mark(r, rec)
            typer.echo(
                f"    {mark:<8} {_move(r.answer, r.args)}"
                + (f" {r.confidence:.2f}" if r.confidence is not None else "")
                + f"  {r.ms / 1000:.1f}s out={r.output} reasoning={r.reasoning}"
                + (f"  {r.error}" if r.error else "")
            )
    s = decisions.summarize(rows)
    typer.echo(
        f"\n{s.asks} ask(s): valid={s.valid_rate:.0%} agree={s.agreement:.0%} "
        f"mean={s.mean_ms / 1000:.1f}s max={s.ms_max / 1000:.1f}s "
        f"reasoning={s.mean_reasoning:.0f}/ask output={s.mean_output:.0f}/ask"
        + (f" errors={s.errors}" if s.errors else "")
    )


@playbooks_app.command()
def propose(
    top: Annotated[
        int,
        typer.Option("--top", min=1, help="How many escalation sites to triage."),
    ] = 3,
) -> None:
    """Turn the hottest escalation sites into concrete authoring work:
    where walks keep handing over, why, and the exact mining commands
    that make a pack patch out of the recorded evidence. Nothing
    self-applies — every escalation the author fixes here prevents the
    next one."""

    sites = runs.escalation_sites(runs.load(), top=top)
    if not sites:
        typer.echo("No escalations recorded — nothing to propose.")
        return
    for s in sites:
        typer.echo(f"{s.app}/{s.playbook} escalates at {s.node} ×{s.count}")
        typer.echo(f"  last reason: {s.reason}")
        sid = s.sessions[-1] if s.sessions else "<session-id>"
        if s.sessions:
            typer.echo(f"  sessions to mine: {', '.join(s.sessions)}")
        typer.echo("  work it:")
        typer.echo(f"    physiclaw playbooks pages match {s.app} --session {sid}")
        typer.echo(
            f"    physiclaw playbooks pages extract {sid} --out {s.app}-corpus.jsonl"
        )
        typer.echo(
            f"    physiclaw playbooks pages calibrate {s.app} {s.app}-corpus.jsonl"
            "  (after labeling)"
        )


@playbooks_app.command()
def check() -> None:
    """Validate every pack: pages, pack macros, playbooks. Exit 1 if any
    is invalid."""
    from physiclaw.conductor.route import lints

    apps = _installed_apps()
    if not apps:
        typer.echo("No app packs found.")
        return
    specs: dict[str, "Playbook"] = {}
    bad = False
    for app in apps:
        app_bad, app_specs = _check_app(app)
        bad = bad or app_bad
        specs.update(app_specs)
    for line in lints.menu_warnings(specs):
        typer.echo(warn(line))
    if bad:
        raise typer.Exit(1)


def _pack_label(app: str) -> str:
    """The pack's name, with the folder it loads from when that is
    named differently: `channel (wechat)`."""
    from physiclaw.common import paths

    folder = paths.pack_root(app).name
    return app if folder == app else f"{app} ({folder})"


def _check_app(
    app: str, pack: "Pack | None" = None
) -> "tuple[bool, dict[str, Playbook]]":
    """Report one pack: (anything invalid, the valid playbooks by ref —
    what the cross-pack advisory reads, so nothing loads twice)."""
    from physiclaw.conductor.load import pack as pb
    from physiclaw.conductor.route import lints

    try:
        if pack is None:
            pack = pb.load_pack(app)
        entries = pb.scan_playbooks(app, pack)
    except (pb.PlaybookError, OSError, UnicodeError) as e:
        typer.echo(step_fail(str(e)))
        return True, {}
    bad = False
    for rel, err in pack.file_errors():
        typer.echo(step_fail(f"{app}/{rel}: {err}"))
        bad = True
    for line in lints.pack_warnings(pack, [e.spec for e in entries if e.spec]):
        typer.echo(warn(f"{app}/{line}"))
    for entry in entries:
        if entry.spec is None:
            typer.echo(step_fail(f"{app}/{entry.name}: {entry.error or ''}"))
            bad = True
            continue
        tags = "" if entry.spec.enabled else "  (disabled)"
        typer.echo(ok(f"{app}/{entry.name}{tags}"))
    _report_not_live(app, pack, entries)
    for line in lints.unrun_playbooks([e.spec for e in entries if e.spec is not None]):
        typer.echo(warn(f"{app}: {line}"))
    for entry in entries:
        if entry.spec is None:
            continue
        # Advisories: an ask quoting none of its reply words, a route
        # page too thinly anchored to survive one OCR miss.
        for line in lints.readiness_warnings(entry.spec, pack):
            typer.echo(warn(f"{app}/{entry.name}: {line}"))
    return bad, {f"{app}/{e.name}": e.spec for e in entries if e.spec is not None}


def _report_not_live(app: str, pack: "Pack", entries: "list[PlaybookEntry]") -> None:
    """Valid is not live: a green check invites the wrong assumption. Say
    which playbooks the boot will not offer, and why — the reason from
    `spec.live.live_gap`, the one rule the wake roster and `require_live`
    also read. Rehearse them (`playbooks run`), then enable."""
    from physiclaw.conductor.spec.live import live_gap

    for e in entries:
        if e.spec is None or not e.spec.offered:
            continue  # never offered; its entry's gap says if it is off
        if (gap := live_gap(e.spec, pack)) is not None:
            typer.echo(warn(f"{app}/{e.name}: the boot will not offer it — {gap}."))


def _split_ref(ref: str) -> tuple[str, str]:
    """`<app>/<playbook>` — the pack's own parse, exiting on a bad one."""
    from physiclaw.conductor.spec.pack import split_ref
    from physiclaw.conductor.spec.specfile import SpecError

    try:
        return split_ref(ref)
    except SpecError as e:
        exit_error(str(e))


# ---------- rehearsal (`playbooks run`) ----------
# The core lives in `conductor.rehearsal` (`replay` shares its `arm`);
# this layer adds the connection and typer skin.


# ---------- stepping (`playbooks step`) ----------
# One call into the driver (`debug/stepping.py`); the CLI adds the MCP
# connection and typer.


async def _step(app: str, name: str, values: dict[str, str], *, emit, **opts):
    # Local import: the mcp SDK only loads when actually stepping.
    from physiclaw.agent.engine.mcp_tool import McpClient
    from physiclaw.debug import stepping

    async with McpClient() as mcp:
        return await stepping.step(
            app,
            name,
            mcp,
            values=values,
            emit=emit,
            emit_warn=lambda line: emit(warn(line)),
            **opts,
        )


async def _rehearse(
    app: str,
    name: str,
    values: dict[str, str],
    *,
    verbose: bool = False,
    raw: bool = False,
) -> str:
    """Arm first (bad inputs fail before any connection), then drive the
    walk over one client, printing each synthesized turn as it goes."""
    # Local import: the mcp SDK only loads when actually rehearsing.
    from physiclaw.agent.engine.mcp_tool import McpClient

    program, registry = setup.arm(
        app, name, values, emit_warn=lambda s: typer.echo(warn(s))
    )
    async with McpClient() as mcp:
        return await rehearsal.walk(
            program,
            registry,
            mcp,
            emit=typer.echo,
            verbose=verbose,
            raw=raw,
        )
