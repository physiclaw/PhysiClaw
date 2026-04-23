"""``physiclaw onboard`` — first-run wizard that chains doctor + setup steps."""

from typing import Annotated

import typer

from physiclaw import paths


def onboard(
    no_interactive: Annotated[
        bool,
        typer.Option(
            "--no-interactive",
            help="Skip confirmation prompts (for CI / scripted installs).",
        ),
    ] = False,
) -> None:
    """Interactive first-run: diagnose, download models, calibrate hardware."""
    typer.echo(typer.style("Welcome to PhysiClaw.", bold=True))
    typer.echo(
        "This one-time wizard checks your environment, downloads the "
        "local vision models, and kicks off hardware calibration."
    )
    typer.echo()

    paths.ensure_dirs()

    # Step 1: doctor
    typer.echo(typer.style("Step 1 / 3  Environment check", bold=True))
    from physiclaw.cli.doctor import doctor

    doctor(fix=True)
    typer.echo()

    # Step 2: download models
    typer.echo(typer.style("Step 2 / 3  Local vision models", bold=True))
    if paths.omniparser_onnx().exists():
        typer.echo("Already present — skipping.")
    else:
        if no_interactive or typer.confirm(
            "Download the vision models now (~100 MB)?", default=True
        ):
            from physiclaw.cli.setup.vision import vision

            vision(force=False)
        else:
            typer.echo(
                "Skipped. Run `physiclaw setup local-vision-model` later."
            )
    typer.echo()

    # Step 3: hardware (prompt only — requires a live server)
    typer.echo(typer.style("Step 3 / 3  Hardware calibration", bold=True))
    typer.echo(
        "Connect the robotic arm + camera over USB and the phone to the "
        "same network, then run:\n"
        "  physiclaw server            (leave running in one shell)\n"
        "  physiclaw setup hardware    (in another shell)\n"
    )
    typer.echo(
        "After that, `physiclaw setup phone` teaches the arm where each "
        "on-screen key sits. Optional but needed for typing."
    )
    typer.echo()
    typer.echo(typer.style("✓ Onboarding done.", fg=typer.colors.GREEN, bold=True))
