"""SVG icons and page illustrations, exposed as properties."""

from html import escape
from pathlib import Path


def load_svg(name: str, *, folder: str = "icon") -> str:
    """Read an SVG afresh so edits are visible without restarting the builder."""
    return (Path(__file__).with_name(folder) / name).read_text(encoding="utf-8")


class SvgIcons:
    """Named properties read current file contents on every access."""

    @property
    def copy_svg(self) -> str:
        return load_svg("copy.svg")

    @property
    def check_svg(self) -> str:
        return load_svg("check.svg")

    @property
    def report_svg(self) -> str:
        return load_svg("report.svg")

    def hardware_placeholder_svg(self, label: str) -> str:
        """Render a placeholder with its label safely encoded as SVG text."""
        return load_svg("hardware_placeholder.svg").format(label=escape(label))

    @property
    def cover_stripes_svg(self) -> str:
        return load_svg("cover_stripes.svg")

    @property
    def github_svg(self) -> str:
        return load_svg("github.svg")

    @property
    def back_corner_svg(self) -> str:
        return load_svg("back_corner.svg")

    @property
    def info_svg(self) -> str:
        return load_svg("info.svg")

    @property
    def crab_svg(self) -> str:
        return load_svg("crab.svg")


class SvgFigures:
    """Page illustrations read current file contents on every access."""

    @property
    def wire_splice_svg(self) -> str:
        return load_svg("wire_splice.svg", folder="figure")


icon = SvgIcons()
figure = SvgFigures()
