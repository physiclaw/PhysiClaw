"""``physiclaw logs`` — list and inspect engine session logs.

The analysis entry point for `~/.physiclaw/log/sessions/`:
`physiclaw logs` tables the recent sessions from their summary.json;
`physiclaw logs <sid>` prints one session's summary plus the tail of its
narrative (rendered from that session's events.jsonl). `--json` emits
machine-readable output for scripting; `--save [DEST]` zips a session
(with its format README and the session viewer page) for backups or
bug reports. Reviewing a session in the browser is the studio's:
`physiclaw studio --review --session <sid>`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from physiclaw.agent.trace import viewer
from physiclaw.agent.trace.store import (
    load_summary,
    recent_sessions,
    resolve_session,
    stub_summary,
)
from physiclaw.cli._format import exit_error, info, section, warn
from physiclaw.common.text import iter_jsonl

# Stored artifacts may be truncated or have fields from an older schema.
# A failed human rendering must not hide the neighboring sessions/events.
_FORMAT_ERRORS = (TypeError, ValueError, AttributeError, KeyError, OverflowError)


def _echo(text: str) -> None:
    """Keep model text printable without changing valid Unicode or JSON values."""
    typer.echo(text.encode("utf-8", "backslashreplace").decode("utf-8"))


def _echo_json(value: Any) -> None:
    """Machine output must be valid JSON, even when stored data is damaged."""
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    except ValueError as e:
        exit_error(f"cannot render log data as JSON: {e}")
    _echo(text)


def logs(
    sid: Annotated[
        str | None,
        typer.Argument(
            help="Session id, or just its 6-hex-digit suffix; omit to list recent sessions.",
        ),
    ] = None,
    n: Annotated[
        int,
        typer.Option("-n", min=1, help="Sessions to list / narrative lines to show."),
    ] = 20,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Machine-readable output."),
    ] = False,
    save: Annotated[
        bool,
        typer.Option(
            "--save",
            help="Save the session as a single zip (for backups or bug reports).",
        ),
    ] = False,
    usage: Annotated[
        bool,
        typer.Option(
            "--usage",
            help="Every model call (turn, kind, model, time, input split, "
            "output) — multiply the buckets by your rates for the bill; "
            "--json for rows.",
        ),
    ] = False,
    decisions: Annotated[
        bool,
        typer.Option(
            "--decisions", help="Offline decision accuracy, execution and usage report."
        ),
    ] = False,
    label_template: Annotated[
        bool,
        typer.Option(
            "--label-template",
            help="With --decisions: export blind review evidence as JSON.",
        ),
    ] = False,
    labels: Annotated[
        Path | None,
        typer.Option(
            "--labels", help="With --decisions: completed JSON label template."
        ),
    ] = None,
    rates: Annotated[
        Path | None,
        typer.Option(
            "--rates",
            help="With --usage/--decisions: JSON rates in USD per million tokens.",
        ),
    ] = None,
    baseline: Annotated[
        str | None,
        typer.Option(
            "--baseline", help="With --decisions: compare a matched baseline session."
        ),
    ] = None,
    dest: Annotated[
        Path | None,
        typer.Argument(
            help="With --save: destination directory or .zip path (default: current dir).",
        ),
    ] = None,
) -> None:
    """List recent agent sessions, or inspect one session's log artifacts."""
    from physiclaw.common import paths

    if sum((save, usage, decisions)) > 1 or (save and as_json):
        exit_error(
            "--save, --usage and --decisions are separate modes; --save forbids --json"
        )
    if (label_template or labels is not None or baseline is not None) and not decisions:
        exit_error("--label-template, --labels and --baseline require --decisions")
    if label_template and (
        labels is not None or baseline is not None or rates is not None
    ):
        exit_error(
            "--label-template uses only original evidence; omit labels, baseline and rates"
        )
    if rates is not None and not (usage or decisions):
        exit_error("--rates requires --usage or --decisions")
    if dest is not None and not save:
        exit_error(f"a destination needs --save: physiclaw logs {sid} --save {dest}")
    sessions_dir = paths.sessions_dir()
    if sid is None:
        if save or usage or decisions:
            _echo(
                warn(
                    "that flag needs a session: physiclaw logs <sid> --save|--usage|--decisions"
                )
            )
            raise typer.Exit(1)
        _list_sessions(sessions_dir, n=n, as_json=as_json)
    elif usage:
        try:
            from physiclaw.agent.trace.accounting import read_json

            _show_usage(
                _resolve(sessions_dir, sid),
                as_json=as_json,
                rates=read_json(rates) if rates is not None else None,
            )
        except (OSError, ValueError) as exc:
            exit_error(str(exc))
    elif decisions:
        _show_decisions(
            _resolve(sessions_dir, sid),
            as_json=as_json,
            template=label_template,
            labels=labels,
            rates=rates,
            baseline=_resolve(sessions_dir, baseline) if baseline is not None else None,
        )
    elif save:
        _save_session(_resolve(sessions_dir, sid), dest)
    else:
        _show_session(_resolve(sessions_dir, sid), n=n, as_json=as_json)


def _show_decisions(
    directory: Path,
    *,
    as_json: bool,
    template: bool,
    labels: Path | None,
    rates: Path | None,
    baseline: Path | None,
) -> None:
    from physiclaw.agent.trace.accounting import read_json
    from physiclaw.agent.trace.decisions import label_template, report

    try:
        if template:
            _echo_json(label_template(directory))
            return
        rate_table = read_json(rates) if rates is not None else None
        result = report(
            directory,
            labels=read_json(labels) if labels is not None else None,
            rates=rate_table,
        )
        if baseline is not None:
            if baseline.resolve() == directory.resolve():
                raise ValueError("baseline must be a different matched session")
            reference = report(baseline, rates=rate_table)
            result["comparison"] = {
                "baseline": {
                    key: reference[key]
                    for key in (
                        "session_id",
                        "events_sha256",
                        "malformed_lines",
                        "session",
                        "usage",
                        "cost",
                    )
                },
                "meaning": (
                    "Side-by-side observed totals, not causal savings. Confirm matched "
                    "tasks and outcomes; shadow overlaps LLM work. Missing cost stays unknown."
                ),
            }
    except (OSError, ValueError) as exc:
        exit_error(str(exc))
    if as_json:
        _echo_json(result)
        return
    _echo(section(f"Decision report — {directory.name}"))
    for key in (
        "opportunities",
        "accuracy",
        "latency",
        "session",
        "usage",
        "cost",
        "comparison",
    ):
        if key in result:
            _echo(f"{key}:")
            _echo_json(result[key])
    if result["malformed_lines"]:
        _echo(warn(f"{result['malformed_lines']} malformed event line(s) excluded"))
    for limitation in result["limitations"]:
        _echo(info(limitation))
    _echo(
        info(
            "--json includes each request and execution outcome; --label-template exports blind evidence."
        )
    )


def _resolve(sessions_dir: Path, query: str) -> Path:
    """Resolve a session by full id, or by any unique trailing fragment
    (`trace.store.resolve_session` owns the convention and the wording)."""
    try:
        return resolve_session(sessions_dir, query)
    except LookupError as e:
        exit_error(str(e))


# ---------- list mode ----------


def _list_sessions(sessions_dir: Path, *, n: int, as_json: bool) -> None:
    summaries = recent_sessions(sessions_dir, n)
    if as_json:
        _echo_json(summaries)
        return
    if not summaries:
        _echo(
            info(
                "no sessions yet — logs appear under "
                f"{sessions_dir} after the first agent wake"
            )
        )
        return
    _echo(section(f"Sessions ({len(summaries)} most recent)"))
    _echo(
        f"  {'SID':<22} {'OUTCOME':<7} {'TURNS':>5} {'DUR':>6} "
        f"{'TOKENS':>12} {'CACHE':>5}  RECAP"
    )
    for s in summaries:
        try:
            row = _row(s)
        except _FORMAT_ERRORS:
            row = f"{s['sid']} (unformatted summary; use --json to inspect)"
        _echo("  " + row)


def _row(s: dict[str, Any]) -> str:
    from physiclaw.agent.trace import brief, fmt_tokens

    outcome = s.get("outcome") or {}
    sentinel = outcome.get("sentinel") or "?"
    if outcome.get("crashed"):
        sentinel = "CRASH"
    u = s.get("usage") or {}
    tokens = (
        f"{fmt_tokens(u['input_tokens'])}/{fmt_tokens(u['output_tokens'])}"
        if u
        else "-"
    )
    cache = f"{u['cache_hit_pct']:.0f}%" if u else "-"
    dur = f"{s['duration_s']:.0f}s" if "duration_s" in s else "-"
    turns = s.get("turns", "-")
    recap = brief(outcome.get("recap") or "", 48)
    return (
        f"{s.get('sid', '?'):<22} {sentinel:<7} {turns:>5} {dur:>6} "
        f"{tokens:>12} {cache:>5}  {recap}"
    )


# ---------- save mode ----------


def _save_session(d: Path, dest: Path | None) -> None:
    """Zip the session dir to `dest` (dir or .zip path; default cwd).
    The zip stays local — whether it leaves the machine is the user's
    call."""
    import tempfile
    import zipfile

    from physiclaw.agent.trace import SESSIONS_README

    if not d.is_dir():
        _echo(warn(f"no such session: {d.name} (looked in {d.parent})"))
        raise typer.Exit(1)

    default_name = f"physiclaw-session-{d.name}.zip"
    out = (dest or Path.cwd()).expanduser()
    if out.is_dir() or not out.suffix:
        out = out / default_name
    if out.suffix.lower() != ".zip":
        exit_error("the destination must be a directory or a .zip file")
    try:
        viewer.write(d)  # the archive reviews itself, beside its frames
        out.parent.mkdir(parents=True, exist_ok=True)
        resolved_out = out.resolve()
        files = sorted(
            p
            for p in d.rglob("*")
            if p.is_file() and p.resolve() != resolved_out and p != d / "README.md"
        )
        # Snapshot inputs before creating the temporary archive. Re-exporting
        # inside the session must not include the archive in itself, and a
        # failed write must leave an existing backup intact.
        with tempfile.TemporaryDirectory(
            prefix=".physiclaw-session-", dir=out.parent
        ) as tmp:
            archive = Path(tmp) / "session.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
                for p in files:
                    # Explicit forward slashes keep Windows exports portable.
                    z.write(p, arcname=f"{d.name}/{p.relative_to(d).as_posix()}")
                z.writestr(f"{d.name}/README.md", SESSIONS_README)
            archive.replace(out)
        size_kb = out.stat().st_size / 1024
    except OSError as e:
        exit_error(f"could not save session: {e}")
    images = sum(1 for p in files if p.parent.name == "images")

    from physiclaw.cli._format import ok

    _echo(
        ok(
            f"saved {len(files)} file(s) ({images} screenshots, "
            f"{size_kb:.0f} KB) → {out}"
        )
    )
    _echo(
        warn("PRIVATE: phone screenshots + full prompts inside — review before sharing")
    )


# ---------- detail mode ----------


def _show_session(d: Path, *, n: int, as_json: bool) -> None:
    if not d.is_dir():
        _echo(warn(f"no such session: {d.name} (looked in {d.parent})"))
        raise typer.Exit(1)
    summary = load_summary(d)
    if as_json:
        _echo_json(summary or stub_summary(d))
        return
    if summary is not None:
        _echo(section(f"Session {d.name}"))
        _echo(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _echo(warn("no summary.json — session was killed or is still running"))
    _echo_narrative(d / "events.jsonl", n)
    _echo("")
    _echo(info(f"session:     {d}"))
    for name in ("runtime.log", "server.log"):
        if (d / name).is_file():
            _echo(info(f"{name + ':':<13}{d / name}"))
    _echo(info(f"model log:   {d / 'model.jsonl'}"))
    _echo(info(f"images:      {d / 'images'}"))
    # Suffix handle = the trailing 6 hex chars — true regardless of the
    # id's separator style, and all `_resolve` matches via `endswith`.
    _echo(info(f"review it whole: physiclaw studio --review --session {d.name[-6:]}"))
    _echo(info(f"save a copy: physiclaw logs {d.name[-6:]} --save"))


def _events(events_path: Path) -> list[dict[str, Any]] | None:
    """The session's events.jsonl as dicts, skipping torn lines; None
    when the file is unreadable."""
    try:
        return list(iter_jsonl(events_path)) if events_path.exists() else None
    except OSError:
        return None


def _echo_narrative(events_path: Path, n: int) -> None:
    """Render the last `n` readable events from this session's journal."""
    from physiclaw.agent.trace import brief, summarize_event

    events = _events(events_path)
    if events is None:
        _echo(warn("no events.jsonl"))
        return
    _echo("")
    _echo(section(f"Last {min(n, len(events))} of {len(events)} events"))
    for event in events[-n:]:
        stamp = str(event.pop("t", ""))[11:19]  # popped: the line carries it,
        try:
            msg = summarize_event(event)  # and fallback repr would echo it
        except _FORMAT_ERRORS:
            msg = f"unformatted event: {brief(event, 200)}"
        if msg is not None:
            _echo(f"  [{stamp}] {msg}")


# ---------- usage mode ----------


def _show_usage(d: Path, *, as_json: bool, rates: dict | None = None) -> None:
    """Call accounting with explicit unknowns and optional supplied prices."""
    from physiclaw.agent.trace.accounting import (
        prepare_accounting,
        read_events,
        session_cost,
        summarize_usage,
        token_count,
        usage_error,
    )

    # Missing journals have no rows, but cannot establish a complete zero bill.
    if (d / "events.jsonl").exists():
        events, malformed, _ = read_events(d)
    else:
        events, malformed = [], 1
    accounting = prepare_accounting(events, malformed_lines=malformed)
    rows = accounting["rows"]
    cost = session_cost(accounting, rates) if rates is not None else None
    if as_json:
        _echo_json(rows if cost is None else {"rows": rows, "cost": cost})
        return
    if not rows:
        _echo(warn("no usage events — the provider reported no token counts"))
        if cost is None:
            return
    if accounting["incomplete_trace"]:
        _echo(warn("incomplete event trace — total cost remains unknown"))
    if malformed:
        _echo(warn(f"{malformed} malformed event line(s) excluded from totals"))

    from physiclaw.agent.trace import brief, fmt_tokens
    from physiclaw.contract.dto import USAGE_BUCKETS

    _echo(section(f"Model calls — {d.name}"))
    _echo(
        f"{'turn':>4} {'api':<8} {'call':<8} {'model':<28}{'time':>7}"
        + "".join(f"{c:>12}" for c in USAGE_BUCKETS)
    )
    for r in rows:
        if usage_error(r) is not None:
            _echo(warn(f"unformatted usage (excluded from totals): {brief(r, 200)}"))
            continue
        turn = r.get("turn")
        elapsed = r.get("elapsed_ms")
        time = "?" if elapsed is None else f"{elapsed / 1000:.1f}s"
        counts = [token_count(r, bucket) for bucket in USAGE_BUCKETS]
        line = (
            f"{'-' if turn is None else str(turn):>4} {r['api_kind']:<8} "
            f"{str(r.get('call') or '?'):<8} {str(r.get('model') or '?'):<28}"
            f"{time:>7}"
            + "".join(f"{'?' if n is None else fmt_tokens(n):>12}" for n in counts)
            + (f"  error={r['error']}" if r.get("error") else "")
        )
        _echo(line)
    _echo("")
    _echo(section("Known subtotals per API and model (exact counts)"))
    summary = summarize_usage(rows)
    for group in summary["groups"]:
        _echo(
            f"  {group['api_kind']} {group['model']}  "
            f"({group['calls']} calls, {group['failed']} failed)"
        )
        for bucket, count in group["tokens"].items():
            _echo(
                f"    {bucket:<12}{count['known']:>14,} known"
                f"  ({count['missing']} missing)"
            )
    if summary["excluded_rows"]:
        _echo(warn(f"{summary['excluded_rows']} damaged row(s) excluded from totals"))
    if cost is not None:
        _echo("")
        if cost["total_usd"] is None:
            _echo(
                warn(
                    f"estimated known cost: ${cost['known_usd']:.8f} USD; total unknown "
                    f"({cost['missing_calls']} incomplete calls)"
                )
            )
        else:
            _echo(info(f"estimated cost: ${cost['total_usd']:.8f} USD"))
    _echo("")
    _echo(
        info(
            "? = unreported; native decision cache/reasoning splits are not reported. "
            "Chat cost uses input_new/cache_read/cache_write + output; decision uses "
            "input + output. Do not also bill input totals or reasoning."
        )
    )
