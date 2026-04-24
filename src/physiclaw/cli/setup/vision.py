"""``physiclaw setup local-vision-model`` — fetch the OmniParser icon detector.

Upstream ships PyTorch weights; we convert to ONNX once and cache under
the user's cache dir. Conversion needs ``ultralytics`` + ``onnx`` + ``onnxslim``,
which are heavy (~500 MB) and not in the default install — kept opt-in so
``physiclaw`` itself stays small.
"""

import logging
import urllib.request
from typing import Annotated

import typer

from physiclaw import paths

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
        typer.echo(
            typer.style("Conversion deps missing.", fg=typer.colors.YELLOW, bold=True)
        )
        typer.echo(
            "The OmniParser upstream ships PyTorch weights; converting them "
            "to ONNX needs `ultralytics`, `onnx`, and `onnxslim`. These "
            "aren't in the default install to keep PhysiClaw small.\n\n"
            "Install into the same tool environment and re-run:\n"
            "    uv tool install physiclaw --reinstall \\\n"
            "        --with ultralytics --with onnx --with 'onnxslim>=0.1.71'\n"
            "    physiclaw setup local-vision-model\n"
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
    typer.echo(typer.style("✓ vision model ready", fg=typer.colors.GREEN))
    typer.echo()
    typer.echo(
        typer.style("Next:", bold=True)
        + " physiclaw setup hardware  (plug in the arm + USB camera first)"
    )
