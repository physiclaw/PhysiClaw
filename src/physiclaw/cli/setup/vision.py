"""``physiclaw setup local-vision-model`` — fetch the OmniParser icon detector.

Upstream ships PyTorch weights; we convert to ONNX once and cache under
the user's data dir. Conversion needs ``ultralytics`` + ``onnx`` + ``onnxslim``
(~500 MB), which we deliberately keep OUT of physiclaw's own dependencies.
Instead, the conversion runs in a throwaway scratch directory under
``uv run --with``: uv resolves the heavy deps into its own cache, runs the
export once, and we ``rmtree`` the scratch dir on success. The physiclaw
install never sees ultralytics — runtime inference only needs ``onnxruntime``,
which is already a core dep.
"""

import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Annotated

import typer

from physiclaw import paths
from physiclaw.cli._format import next_hint, ok

_PT_URL = (
    "https://huggingface.co/microsoft/OmniParser-v2.0/"
    "resolve/main/icon_detect/model.pt"
)

# Bumping these is a deliberate decision — the ONNX export contract has
# shifted between ultralytics minor versions before.
_CONVERT_DEPS = (
    "ultralytics>=8.4",
    "onnx>=1.18",
    "onnxslim>=0.1.71",
)

# Real file (not -c) so it stays in convert/ for debugging on failure.
_CONVERT_SCRIPT = """\
from ultralytics import YOLO
YOLO("model.pt").export(format="onnx", imgsz=1280)
"""


def _abort_kept_scratch(convert_dir: Path, reason: str) -> None:
    typer.echo(typer.style(
        f"{reason} Scratch dir kept for debugging: {convert_dir}",
        fg=typer.colors.RED, bold=True,
    ))
    raise typer.Abort()


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

    if shutil.which("uv") is None:
        typer.echo(typer.style(
            "`uv` is required to convert the vision model.",
            fg=typer.colors.YELLOW, bold=True,
        ))
        typer.echo(
            "The conversion runs the heavy deps (ultralytics + onnx + onnxslim, "
            "~500 MB) in an ephemeral uv environment so they never enter the "
            "physiclaw install. Install uv first, then re-run:\n\n"
            "    curl -fsSL https://astral.sh/uv/install.sh | sh   # macOS / Linux\n"
            "    irm https://astral.sh/uv/install.ps1 | iex        # Windows\n"
            "    physiclaw setup local-vision-model\n"
        )
        raise typer.Abort()

    convert_dir = onnx.parent / "convert"
    if force and convert_dir.exists():
        # --force means re-fetch from upstream — drop any stale .pt cached
        # from a partial prior run.
        shutil.rmtree(convert_dir)
    convert_dir.mkdir(parents=True, exist_ok=True)
    pt_path = convert_dir / "model.pt"
    script_path = convert_dir / "convert.py"
    onnx_in_scratch = convert_dir / "model.onnx"

    if not pt_path.exists():
        typer.echo(f"Downloading {_PT_URL} …")
        urllib.request.urlretrieve(_PT_URL, pt_path)
        typer.echo(f"  {pt_path.stat().st_size / 1024 / 1024:.1f} MB saved.")

    script_path.write_text(_CONVERT_SCRIPT)

    typer.echo("Converting to ONNX in ephemeral uv env …")
    cmd = [
        "uv", "run",
        "--python", "3.12",
        "--no-project",
        *(arg for dep in _CONVERT_DEPS for arg in ("--with", dep)),
        "python", "convert.py",
    ]
    result = subprocess.run(cmd, cwd=convert_dir)
    if result.returncode != 0:
        _abort_kept_scratch(
            convert_dir, f"Conversion failed (uv exit {result.returncode}).",
        )
    if not onnx_in_scratch.exists():
        _abort_kept_scratch(
            convert_dir, f"Conversion finished but {onnx_in_scratch} not found.",
        )

    shutil.move(onnx_in_scratch, onnx)
    shutil.rmtree(convert_dir)

    typer.echo(f"  {onnx.stat().st_size / 1024 / 1024:.1f} MB at {onnx}")
    typer.echo(ok("vision model ready"))
    typer.echo()
    typer.echo(next_hint(
        "physiclaw setup hardware  (plug in the arm + USB camera first)"
    ))
