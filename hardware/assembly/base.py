"""Base class for assembly-step drawings.

``BaseAssembly`` inherits from ``BasePart`` — assemblies share the
build/export/output_path machinery (so ``.export()`` writes a STEP of
the composed assembly, handy for CAD inspection) and add ``.render()``
for the SVG drawing used in the manual.

Default filenames:
  * ``hardware/output/step/<module_name>.step`` (inherited)
  * ``hardware/output/svg/<module_name>.svg``  (this class)
"""

from pathlib import Path

from build123d import MM, Compound, ExportSVG, LineType, ShapeList, Unit

from hardware.assembly.render import ISO, Camera, camera_view
from hardware.parts.base import REPO_ROOT, BasePart

SVG_DIR = REPO_ROOT / "hardware" / "output" / "svg"

# Top-level Compound labels recognised by BaseAssembly.render() for the
# two-layer split. The ``layer_`` prefix namespaces them as render-routing
# tags so they can't collide with a normal part label like "solid" or
# "ghost" that a procedure might choose for an unrelated reason.
SOLID_LABEL = "_layer_solid"
GHOST_LABEL = "_layer_ghost"


class BaseAssembly(BasePart):
    """Buildable assembly — STEP via .export() (inherited), SVG via .render().

    Two-layer SVG: if ``_build`` returns a Compound with top-level children
    labeled ``SOLID_LABEL`` and ``GHOST_LABEL``, they are projected to
    separate SVG layers (ghost = lighter + phantom dashes) — exploded-view
    illustration of prep state vs result. Otherwise the whole assembly is
    one layer.

    Every assembly has two variants: ``exploded=True`` shows the install
    motion (gaps + ghosts), ``exploded=False`` shows the finished state.
    The flag is exposed as a ctor kwarg so callers can ``export()`` both
    from one ``__main__``, and so a downstream assembly can embed an
    upstream one in its assembled form (e.g. ``FR20SHCS(exploded=False)``).
    Output filenames are suffixed ``_exploded`` / ``_assembled`` to keep
    both on disk side by side.
    """

    camera: Camera = ISO
    line_weight: float = 0.25   # mm — heavier than build123d's 0.09 default
    page_margin: float = 5 * MM
    ghost_line_weight: float = 0.12
    ghost_line_type: LineType = LineType.PHANTOM

    def __init__(self, *, exploded: bool = False):
        super().__init__()
        self.exploded = exploded

    @property
    def _variant(self) -> str:
        return "_exploded" if self.exploded else "_assembled"

    def name_suffix(self) -> str:
        # Assemblies are one-offs; drop the inherited "_x{qty}" suffix and
        # use the variant tag instead, so the STEP filename is e.g.
        # solenoid_tip_exploded.step / solenoid_tip_assembled.step.
        return self._variant

    def bom_key(self):
        return None  # assemblies are structural; their parts register themselves

    def svg_path(self) -> Path:
        return SVG_DIR / f"{self._module_stem()}{self._variant}.svg"

    def _build(self) -> Compound:
        raise NotImplementedError

    def render(self) -> None:
        assembly = self.build()
        solid, ghost = _split_solid_ghost(assembly)

        # Camera + look_at derived from the FULL assembly bbox so solid and
        # ghost layers align pixel-for-pixel. Without a shared look_at,
        # project_to_viewport defaults to each subset's own center, which
        # warps the projection direction per layer.
        cam_pos, up, look_at = camera_view(assembly, self.camera)

        exporter = ExportSVG(unit=Unit.MM, margin=self.page_margin)
        exporter.add_layer(SOLID_LABEL, line_weight=self.line_weight)
        if ghost is not None:
            exporter.add_layer(
                GHOST_LABEL,
                line_weight=self.ghost_line_weight,
                line_type=self.ghost_line_type,
            )

        solid_visible, _ = solid.project_to_viewport(cam_pos, up, look_at=look_at)
        exporter.add_shape(ShapeList(solid_visible), layer=SOLID_LABEL)
        if ghost is not None:
            ghost_visible, _ = ghost.project_to_viewport(cam_pos, up, look_at=look_at)
            exporter.add_shape(ShapeList(ghost_visible), layer=GHOST_LABEL)

        path = self.svg_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        exporter.write(str(path))
        print(f"  wrote {path}")


def _split_solid_ghost(assembly):
    """Return (solid, ghost) Compounds. Looks for top-level children
    labeled SOLID_LABEL / GHOST_LABEL; if no SOLID_LABEL child is
    present, the whole assembly is treated as solid so existing
    single-layer assemblies render unchanged.

    Uses ``dict.get`` (not a truthiness fallback) so an empty Compound
    is still recognised as the solid layer rather than slipping through
    to the assembly-root fallback — empty Compounds are falsy in
    build123d but a labeled-but-empty solid layer is still meaningful.

    Raises ValueError if either label appears more than once — last-wins
    silent overwrite is a footgun; merge the shapes under one Compound
    explicitly instead."""
    found: dict[str, Compound] = {}
    for child in getattr(assembly, "children", []):
        label = getattr(child, "label", None)
        if label not in (SOLID_LABEL, GHOST_LABEL):
            continue
        if label in found:
            raise ValueError(
                f"duplicate top-level {label!r} child in assembly "
                f"{assembly.label!r}; merge into one Compound"
            )
        found[label] = child
    solid = found.get(SOLID_LABEL, assembly)
    return solid, found.get(GHOST_LABEL)


def render_all(assemblies):
    for a in assemblies:
        a.render()
