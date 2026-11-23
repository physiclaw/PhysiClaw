"""Run the built-in offline workflow scenario."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from physiclaw.cli.runtime_process import terminate_child
from physiclaw.common import paths
from physiclaw.dryrun import DAILY_ACTIONS, KEY, PHONE_ACTIONS


def dryrun(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show server and agent logs.")
    ] = False,
) -> None:
    """Test a phone request and its scheduled daily follow-up with a virtual phone and scripted model replies.

    Uses the installed local vision model. Runs offline without a camera,
    arm, or provider credentials.
    """
    model = paths.omniparser_onnx()
    if not model.is_file():
        typer.echo(
            "Dryrun needs the local vision model. Install it with "
            "`physiclaw setup local-vision-model`.",
            err=True,
        )
        raise typer.Exit(1)

    home = Path(tempfile.mkdtemp(prefix="physiclaw-dryrun-")).resolve()
    typer.echo("Running the offline workflow…", err=True)
    try:
        _stage_model(model, home)
        result = _run_worker(home, verbose=verbose)
        if result == 0:
            shutil.rmtree(home)
    except KeyboardInterrupt:
        result = 130
    except OSError as exc:
        _fail(str(exc), home)

    if result == 130:
        _fail("Dryrun interrupted", home, code=130)
    if result != 0:
        _fail(_failure_message(home, result), home)
    typer.echo(
        f"PASS  Phone request, lamp adjustment and chat reply ({len(PHONE_ACTIONS)} turns)\n"
        f"PASS  Daily wake, lamp check and schedule re-armed ({len(DAILY_ACTIONS)} turns)"
    )


def _stage_model(model: Path, home: Path) -> None:
    dest = home / model.relative_to(paths.HOME)
    dest.parent.mkdir(parents=True)
    model = model.resolve(strict=True)
    try:
        os.link(model, dest)
    except OSError:
        shutil.copyfile(model, dest)


def _run_worker(home: Path, *, verbose: bool) -> int:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("PHYSICLAW_")
        and not key.upper().endswith("_PROXY")
    }
    env.update(PHYSICLAW_HOME=str(home), OPENAI_API_KEY=KEY)
    cmd = [sys.executable, "-m", "physiclaw.dryrun", str(home)]
    if verbose:
        cmd.append("--verbose")
    console = nullcontext() if verbose else (home / "console.log").open("wb")
    with (
        console as output,
        subprocess.Popen(
            cmd,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT if output is not None else None,
        ) as worker,
    ):
        try:
            return worker.wait()
        except KeyboardInterrupt:
            # Ctrl+C reaches the worker in the same terminal group. Leave
            # its MCP listener time to close the managed runtime and trace.
            try:
                worker.wait(timeout=20)
            except (KeyboardInterrupt, subprocess.TimeoutExpired):
                terminate_child(worker)
            return 130


def _failure_message(home: Path, returncode: int) -> str:
    try:
        return json.loads((home / "failure.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"Dryrun worker exited with status {returncode}"


def _fail(message: str, home: Path, *, code: int = 1) -> NoReturn:
    typer.echo(f"FAIL  {message}\n      Trace saved to {home}", err=True)
    raise typer.Exit(code)
