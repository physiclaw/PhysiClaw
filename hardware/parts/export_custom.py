"""Export every custom part — one .step file per part written to
hardware/output/step/. Run: `uv run python -m hardware.parts.export_custom`."""

from hardware.parts.base import export_all as export_all
from hardware.parts.base import export_main
from hardware.parts.catalog import CUSTOM_PARTS

ALL_PARTS = [part(qty=qty) for part, qty, _, _ in CUSTOM_PARTS]


if __name__ == "__main__":
    export_main(ALL_PARTS, __doc__)
