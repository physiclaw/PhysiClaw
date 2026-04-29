"""``physiclaw setup local-vision-model`` — fetch the OmniParser icon detector.

Upstream ships PyTorch weights; we convert to ONNX once and cache under
the user's data dir. Conversion needs ``ultralytics`` + ``onnx`` + ``onnxslim``,
exposed as the ``[vision]`` extra. install.sh / install.ps1 install with
``physiclaw[vision]``, run this command, then reinstall physiclaw without
the extra to free the ~500 MB. Manual users can do the same dance —
the message printed below shows the commands.
"""

import logging
import urllib.request
from typing import Annotated

import typer

from physiclaw import paths
from physiclaw.cli._format import next_hint, ok

log = logging.getLogger(__name__)

_PT_URL = (
    "https://huggingface.co/microsoft/OmniParser-v2.0/"
    "resolve/main/icon_detect/model.pt"
)


def vision(
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="Re-download even if the ONNX already exists."
        ),
    ] = False,
) -> None:
    """Download and convert the local vision model."""
    onnx = paths.omniparser_onnx()
    if onnx.exists() and not force:
        typer.echo(f"Already present: {onnx}")
        return

    try:
        from ultralytics import YOLO  # type: ignore[import-not-found]
    except ImportError:
        typer.echo(typer.style(
            "The local vision model needs a few extra packages first.",
            fg=typer.colors.YELLOW, bold=True,
        ))
        typer.echo(
            "Converting the upstream PyTorch weights to ONNX requires "
            "`ultralytics`, `onnx`, and `onnxslim` (about 500 MB). They're "
            "not in the default install so `physiclaw` itself stays small.\n\n"
            "Add them, run the conversion, then drop them again:\n\n"
            "    uv tool install 'physiclaw[vision]' --reinstall\n"
            "    physiclaw setup local-vision-model\n"
            "    uv tool install physiclaw --reinstall   # frees the ~500 MB\n"
        )
        raise typer.Abort()

    paths.ensure_dirs()
    onnx.parent.mkdir(parents=True, exist_ok=True)
    pt_path = onnx.with_suffix(".pt")

    if not pt_path.exists():
        typer.echo(f"Downloading {_PT_URL} …")
        urllib.request.urlretrieve(_PT_URL, pt_path)
        typer.echo(f"  {pt_path.stat().st_size / 1024 / 1024:.1f} MB saved.")

    typer.echo("Converting to ONNX …")
    YOLO(str(pt_path)).export(format="onnx", imgsz=1280)
    exported = pt_path.with_suffix(".onnx")
    if exported != onnx:
        exported.rename(onnx)
    typer.echo(f"  {onnx.stat().st_size / 1024 / 1024:.1f} MB at {onnx}")

    pt_path.unlink(missing_ok=True)
    typer.echo(ok("vision model ready"))
    typer.echo()
    typer.echo(next_hint(
        "physiclaw setup hardware  (plug in the arm + USB camera first)"
    ))
