"""`physiclaw playbooks pages` — a pack's page fingerprints.

The `pages:` a pack declares are semantics; the geometry that makes
them robust is learned on-device. These commands do that work:
`propose` suggests anchors off screens, `match` prints the matcher's
verdict per screen, `extract` dumps a session's listings to a corpus
to label, `calibrate` learns geometry from a labeled corpus or live.
Offline-first: `extract` and `--session` inputs need no hardware;
`--live` does one `peek` against a running `physiclaw mcp`, the same
connection idiom as macro rehearsal. Nothing here touches the engine
runtime.
"""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from physiclaw.cli._format import exit_error
from physiclaw.cli._sessions import resolve_sid
from physiclaw.common.ready import START_HINT
from physiclaw.common.text import read_text

if TYPE_CHECKING:
    from physiclaw.common.listing import Screen
    from physiclaw.conductor.bench.capture import CaptureReport

pages_app = typer.Typer(no_args_is_help=True)


def _input_screens(
    session: str | None, listing: Path | None, live: bool
) -> list["Screen"]:
    """Screens from exactly one input source: a recorded session (suffix
    resolved via the shared `logs <suffix>` convention), a listing file,
    or one live peek."""
    from physiclaw.common.listing import Screen
    from physiclaw.conductor.bench import corpus

    if sum((session is not None, listing is not None, live)) != 1:
        exit_error("need exactly one of --session / --listing / --live", code=2)
    try:
        if live:
            listings = [asyncio.run(_live_listing())]
        elif listing is not None:
            listings = [read_text(listing)]
        else:
            assert session is not None
            listings = corpus.session_listings(resolve_sid(session))
    except (OSError, ValueError) as e:
        exit_error(str(e))
    return [Screen.read(t) for t in listings]


async def _live_listing() -> str:
    from mcp import MCPError

    from physiclaw.agent.engine.mcp_tool import McpClient
    from physiclaw.common import verdict

    try:
        async with McpClient() as mcp:
            blocks = await mcp.call_tool("peek", {})
    except ConnectionError as e:
        # Same arm as macro rehearsal: --live needs `physiclaw mcp` running.
        exit_error(f"{e}. {START_HINT}")
    except (OSError, RuntimeError, MCPError) as e:
        exit_error(f"could not peek: {e}")
    return verdict.screen_text(blocks)


@pages_app.command()
def extract(
    session: str = typer.Argument(help="session id (suffix ok if unique)"),
    out: Path = typer.Option(..., "--out", help="corpus JSONL to write"),
) -> None:
    """Dump a session's listings to a corpus file with '?' labels to edit."""
    from physiclaw.conductor.bench import corpus

    try:
        listings = corpus.session_listings(resolve_sid(session))
        corpus.write_corpus(
            out,
            [corpus.CorpusItem(label=corpus.UNLABELED, listing=t) for t in listings],
        )
    except (OSError, ValueError) as e:
        exit_error(str(e))
    typer.echo(f"wrote {len(listings)} listings → {out} (edit the '?' labels)")


@pages_app.command()
def match(
    app: str = typer.Argument(help="app pack whose pages to match against"),
    session: str | None = typer.Option(
        None, "--session", help="replay a recorded session"
    ),
    listing: Path | None = typer.Option(None, "--listing", help="a listing text file"),
    live: bool = typer.Option(False, "--live", help="one peek via `physiclaw mcp`"),
) -> None:
    """Print the matcher's verdict for each input screen."""
    from physiclaw.common.paths import PACK_FILENAME
    from physiclaw.conductor.load import prints
    from physiclaw.conductor.spec.match import match_screen

    try:
        found = prints.prints_for_app(app)
    except (OSError, ValueError) as e:
        exit_error(str(e))
    if not found:
        exit_error(
            f"no pages declared for app {app!r} (playbooks/{app}/{PACK_FILENAME} `pages:`)"
        )
    for i, screen in enumerate(_input_screens(session, listing, live)):
        v = match_screen(screen, found)
        typer.echo(
            f"[{i:3d}] {v.kind:8s} {v.page_id or '-':24s} "
            f"dy={v.dy:+.2f}  {v.describe()}"
        )


# Guided capture: enough observations per page for a median position and
# a real spread (capture floors the tolerance below 2, and MIN_FREQ needs
# an anchor in most of them), while staying a short chore at the desk.
GUIDED_SHOTS = 4


@pages_app.command()
def calibrate(
    app: str = typer.Argument(help="app pack to calibrate"),
    corpus_file: Path | None = typer.Argument(None, help="labeled corpus JSONL"),
    guided: bool = typer.Option(
        False, "--guided", help="capture live, page by page, with prompts"
    ),
    shots: int = typer.Option(
        GUIDED_SHOTS, "--shots", min=1, help="live peeks per page (--guided)"
    ),
) -> None:
    """Capture geometry + OCR variants for an app.

    From a labeled corpus: lines labeled `<app>.<page>` are that page's
    genuine observations; any other non-'?' label is a hard negative.

    With `--guided`: put the phone on each declared page when asked and
    it peeks live — the way to calibrate pages a recorded session can't
    easily contain (the lock screen, say). Needs `physiclaw mcp` running.

    Either way, writes `learned/pages/<app>.json` and prints the per-page
    report."""
    from physiclaw.conductor.bench import capture, corpus
    from physiclaw.conductor.load import prints

    try:
        decls = prints.scan_app_decls(app)
    except (OSError, ValueError) as e:
        exit_error(str(e))
    if not decls:
        exit_error(f"no pages declared for app {app!r}")
    if guided:
        if corpus_file is not None:
            exit_error("--guided captures live; drop the corpus argument", code=2)
        negatives: list[Screen] = []
        by_page = _guided_capture(app, sorted(decls), shots)
        if not by_page:
            exit_error("no pages captured")
    else:
        if corpus_file is None:
            exit_error("need a corpus file, or --guided", code=2)
        try:
            items = corpus.read_corpus(corpus_file)
        except (OSError, ValueError) as e:
            exit_error(str(e))
        by_page, negatives = corpus.partition(items, app, set(decls))

    mined, reports, warnings = capture.capture_app(app, decls, by_page, negatives)
    for w in warnings:
        typer.echo(f"  ⚠ {w}")
    for r in reports:
        typer.echo(_report_line(r))
    if mined:
        try:
            prints.save_learned(app, mined)
        except (OSError, ValueError) as e:
            exit_error(str(e))
        typer.echo(
            f"saved learned/pages/{prints.learned_file(app).name} ({len(mined)} pages)"
        )


def _guided_capture(
    app: str, page_names: list[str], shots: int
) -> dict[str, list["Screen"]]:
    """Capture each page, omitting skipped pages and unreadable peeks."""
    by_page: dict[str, list[Screen]] = {}
    typer.echo(f"Guided capture for {app!r} — {shots} peeks per page.")
    for name in page_names:
        typer.echo(f"\n{app}.{name}")
        answer = typer.prompt(
            "  put the phone on this page, then press Enter ('s' to skip)",
            default="",
            show_default=False,
        )
        if answer.strip().lower() == "s":
            typer.echo("  skipped")
            continue
        screens = []
        for i in range(shots):
            (screen,) = _input_screens(None, None, True)
            if not screen.readable:
                typer.echo(f"  [{i + 1}/{shots}] unreadable view — skipping this peek")
                continue
            screens.append(screen)
            typer.echo(f"  [{i + 1}/{shots}] {len(screen.rows)} rows")
        if screens:
            by_page[name] = screens
    return by_page


def _report_line(r: "CaptureReport") -> str:
    impostors = "-" if r.impostor_pass is None else str(r.impostor_pass)
    flag = "" if r.separable else "  ⚠ INSEPARABLE"
    return (
        f"{r.page}: obs={r.observations} anchors={r.anchors_learned} "
        f"genuine_pass={r.genuine_pass:.0%} impostors_read={impostors}{flag}"
    )


@pages_app.command()
def propose(
    session: str | None = typer.Option(
        None, "--session", help="replay a recorded session"
    ),
    listing: Path | None = typer.Option(None, "--listing", help="a listing text file"),
    live: bool = typer.Option(False, "--live", help="one peek via `physiclaw mcp`"),
) -> None:
    """Suggest anchor declarations from screens — prune by eye into
    the pack's `pages:` section."""
    from physiclaw.conductor.bench.capture import propose_anchors

    for i, screen in enumerate(_input_screens(session, listing, live)):
        candidates = propose_anchors(screen)
        typer.echo(f"[{i:3d}] candidates: {', '.join(repr(s) for s in candidates)}")
