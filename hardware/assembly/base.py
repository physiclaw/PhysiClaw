"""Base class for assembly-step drawings.

``BaseAssembly`` inherits from ``BasePart`` — assemblies share the
build/export/output_path machinery (so ``.export()`` writes a STEP of
the composed assembly, handy for CAD inspection) and add ``.render()``
for the SVG drawing used in the manual.

Default filenames:
  * ``hardware/output/step/<module_name>_<variant>.step`` (inherited)
  * ``hardware/output/svg/<module_name>_<variant>_cam<i>.svg`` (this class)
"""

from pathlib import Path

from build123d import MM, Compound, ExportSVG, LineType, ShapeList, Unit

from hardware.assembly.projection import ISO, Camera, camera_view
from hardware.assembly.svg_utils import inject_non_scaling_strokes, strip_root_dims
from hardware.parts.base import REPO_ROOT, BasePart

SVG_DIR = REPO_ROOT / "hardware" / "output" / "svg"

# Top-level Compound labels recognised by BaseAssembly.render() for the
# two-layer split. The ``layer_`` prefix namespaces them as render-routing
# tags so they can't collide with a normal part label like "solid" or
# "ghost" that a procedure might choose for an unrelated reason.
SOLID_LABEL = "_layer_solid"
GHOST_LABEL = "_layer_ghost"


def variant_suffix(exploded: bool) -> str:
    return "_exploded" if exploded else "_assembled"


def svg_path_for(stem: str, exploded: bool, index: int | None = None) -> Path:
    """Output path for a rendered SVG. Single-camera assemblies use
    ``_cam0`` so the filename scheme is uniform with multi-camera ones."""
    return SVG_DIR / f"{stem}{variant_suffix(exploded)}_cam{index or 0}.svg"


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
    both on disk side by side, plus ``_cam<i>`` (always present, ``cam0``
    for single-camera assemblies) so the scheme is uniform whether
    ``camera`` is a single Camera or a list.
    """

    # One camera → one SVG per variant; a list of cameras → one SVG per
    # camera per variant, filenames suffixed ``_cam0``, ``_cam1``, ….
    camera: "Camera | list[Camera]" = ISO
    # With `vector-effect: non-scaling-stroke` baked into every render,
    # these values are interpreted as ~device pixels (not millimetres) —
    # sub-pixel widths still render via anti-aliasing. 0.8 / 0.4 chosen
    # by eye: heavy enough to read at full zoom, light enough not to
    # crowd the drawing.
    line_weight: float = 0.8
    page_margin: float = 5 * MM
    ghost_line_weight: float = 0.4
    ghost_line_type: LineType = LineType.PHANTOM

    def __init__(self, *, exploded: bool = False):
        super().__init__()
        self.exploded = exploded

    def name_suffix(self) -> str:
        # Assemblies are one-offs; drop the inherited "_x{qty}" suffix and
        # use the variant tag instead, so the STEP filename is e.g.
        # solenoid_tip_exploded.step / solenoid_tip_assembled.step.
        return variant_suffix(self.exploded)

    def bom_key(self):
        return None  # assemblies are structural; their parts register themselves

    def svg_path(self, index: int | None = None) -> Path:
        return svg_path_for(self._module_stem(), self.exploded, index=index)

    def geom_key(self):
        """Cache key = class + every ctor-set hashable instance attr.

        Sweeps __dict__ so subclass-specific kwargs (e.g.
        FR10ExtrusionTnut's `separation`) are captured automatically.
        Opts out (returns None) if any attr is unhashable — can't
        safely de-duplicate without value equality.
        """
        items = []
        for k, v in sorted(self.__dict__.items()):
            # ``camera`` controls rendering, not geometry — including it
            # would needlessly fragment the cache and breaks hashing when
            # a procedure sets ``self.camera`` to a list.
            if k == "camera" or k.startswith("_") or callable(v):
                continue
            try:
                hash(v)
            except TypeError:
                return None
            items.append((k, v))
        return (type(self).__name__, tuple(items))

    # Cache post-build state (LI20Joint exposes attrs like
    # big_csk_world_centers that LI31X reads off the instance, so a
    # shape-only cache would leave those undefined on cache hits).
    def _snapshot_state(self):
        return set(self.__dict__.keys())

    def _diff_state(self, pre_keys):
        return {
            k: v for k, v in self.__dict__.items()
            if k not in pre_keys and not k.startswith("_")
        }

    def _restore_state(self, state):
        for k, v in state.items():
            setattr(self, k, v)

    def _build(self) -> Compound:
        raise NotImplementedError

    def render(self) -> None:
        assembly = self.build()
        solid, ghost = _split_solid_ghost(assembly)

        cameras = self.camera if isinstance(self.camera, list) else [self.camera]

        for i, cam in enumerate(cameras):
            # Camera + look_at derived from the FULL assembly bbox so
            # solid and ghost layers align pixel-for-pixel. Without a
            # shared look_at, project_to_viewport defaults to each
            # subset's own center, which warps the projection direction
            # per layer.
            cam_pos, up, look_at = camera_view(assembly, cam)

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

            path = self.svg_path(index=i)
            path.parent.mkdir(parents=True, exist_ok=True)
            exporter.write(str(path))
            path.write_text(inject_non_scaling_strokes(strip_root_dims(path.read_text())))


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
