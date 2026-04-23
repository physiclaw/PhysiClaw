"""``physiclaw setup`` — typer sub-app for one-time configuration tasks.

Subcommands:
  hardware             calibrate the robotic arm + camera (interactive)
  local-vision-model   download the OmniParser icon detection model
  phone                learn the on-screen keyboard layout
"""

import typer

from physiclaw.cli.setup.hardware import hardware
from physiclaw.cli.setup.phone import phone
from physiclaw.cli.setup.vision import vision

setup_app = typer.Typer(
    help="One-time configuration: hardware, local models, phone.",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
    add_completion=False,
)

setup_app.command("hardware")(hardware)
setup_app.command("local-vision-model")(vision)
setup_app.command("phone")(phone)


__all__ = ["setup_app"]
