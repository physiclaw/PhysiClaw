"""Generated CAD highlights, path intermediates and manual composition."""

from xml.etree import ElementTree as ET

import pytest
from build123d import Box, Compound, Location, Vector
from shapely.geometry import Point, Polygon

from hardware.assembly.annotation import (
    callouts,
    features,
    generator,
    geometry,
    specs,
    svg,
)
from hardware.assembly.annotation.specs import Figure, Highlight
from hardware.assembly.base import BaseAssembly
from hardware.assembly.projection import Camera

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-20 -20 40 40"><path id="art" d="M 0 0 L 1 1"/></svg>'
NAME = "frame_10_demo_exploded_cam0"
NS = {"svg": "http://www.w3.org/2000/svg"}


class Demo(BaseAssembly):
    camera = Camera(0, 0)
    offset = 0

    @classmethod
    def _module_stem(cls):
        return "frame_10_demo"

    def _build(self):
        base = Box(20, 20, 20)
        base.label = "Base"
        part = Box(2, 2, 4).moved(
            Location((self.offset, -15 if self.exploded else 0, 0))
        )
        part.label = "Screw_M3"
        return Compound(label="demo", children=[base, part])


@pytest.fixture
def pool(tmp_path, monkeypatch):
    svg = tmp_path / "svg"
    svg.mkdir()
    paths = tmp_path / "annotations"
    monkeypatch.setattr(generator, "SVG_DIR", svg)
    monkeypatch.setattr(generator, "ANNOTATION_DIR", paths)
    monkeypatch.setattr(
        specs,
        "FIGURES",
        specs._index_figures(
            Figure(view=NAME, name="screws", highlights=(Highlight("Screw*"),))
        ),
    )
    (svg / f"{NAME}.svg").write_text(SVG, encoding="utf-8")
    return svg, paths


def test_generated_intermediate_contains_only_paths_and_composes_over_source(pool):
    svg, paths = pool
    [figure] = generator.generate(Demo(exploded=True))
    intermediate = paths / f"{NAME}_annotated_screws.paths.svg"
    root = ET.fromstring(intermediate.read_text(encoding="utf-8"))
    assert list(root)
    assert all(el.tag == f"{{{NS['svg']}}}path" for el in root)
    assert root.find(".//*[@id='art']") is None
    final = ET.fromstring(figure.read_text(encoding="utf-8"))
    assert final.find(".//*[@id='art']") is not None
    assert final.find(".//*[@id='procedure-annotation']") is not None
    assert root.attrib["viewBox"] == final.attrib["viewBox"]
    assert (svg / f"{NAME}.svg").read_text(encoding="utf-8") == SVG


@pytest.mark.parametrize("other_view", [NAME, NAME.upper()])
def test_duplicate_output_names_fail_before_generation(other_view):
    first = Figure(view=NAME, name="screws")
    conflicting = Figure(view=other_view, name="screws")
    with pytest.raises(ValueError, match="Annotation output collision.*screws.svg"):
        specs._index_figures(first, conflicting)


def test_annotation_names_must_be_unique_even_across_different_views():
    first = Figure(view=NAME, name="screws")
    other_view = NAME.replace("exploded_cam0", "assembled_cam1")
    second = Figure(view=other_view, name="screws")
    assert first.filename != second.filename
    with pytest.raises(ValueError, match="Duplicate annotation name 'screws'") as error:
        specs._index_figures(first, second)
    assert NAME in str(error.value)
    assert other_view in str(error.value)


@pytest.mark.parametrize("name", ["", "../bad", "with spaces", "UPPER", "0"])
def test_annotation_names_are_safe_descriptive_identifiers(name):
    with pytest.raises(ValueError, match="Invalid annotation name"):
        specs._index_figures(Figure(view=NAME, name=name))


def test_named_annotations_share_a_view_and_survive_reordering_and_deletion(
    pool, monkeypatch
):
    svg, paths = pool
    screws = Figure(view=NAME, name="screws", highlights=(Highlight("Screw*"),))
    base = Figure(view=NAME, name="base", highlights=(Highlight("Base"),))
    monkeypatch.setattr(specs, "FIGURES", specs._index_figures(screws, base))
    written = generator.generate(Demo(exploded=True))
    assert {path.name for path in written} == {screws.filename, base.filename}
    baseline = {path.name: path.read_text(encoding="utf-8") for path in written}
    assert baseline[screws.filename] != baseline[base.filename]
    assert all((paths / (path.stem + ".paths.svg")).is_file() for path in written)

    monkeypatch.setattr(specs, "FIGURES", specs._index_figures(base, screws))
    generator.generate(Demo(exploded=True))
    assert {path.name: path.read_text(encoding="utf-8") for path in written} == baseline

    monkeypatch.setattr(specs, "FIGURES", specs._index_figures(base))
    [remaining] = generator.generate(Demo(exploded=True))
    assert remaining.name == base.filename
    assert remaining.read_text(encoding="utf-8") == baseline[base.filename]
    assert specs.figure_names(Demo._module_stem()) == {base.filename}
    assert (svg / f"{NAME}.svg").read_text(encoding="utf-8") == SVG


def test_missing_path_intermediate_is_regenerated_even_when_final_exists(pool):
    _, paths = pool
    asm = Demo(exploded=True)
    [figure] = generator.generate(asm)
    assert generator.generate(asm, only_missing=True) == []
    (paths / f"{NAME}_annotated_screws.paths.svg").unlink()
    assert generator.generate(asm, only_missing=True) == [figure]


def test_highlight_follows_current_model_instead_of_saved_coordinates(
    pool, monkeypatch
):
    _, paths = pool
    generator.generate(Demo(exploded=True))
    before = (paths / f"{NAME}_annotated_screws.paths.svg").read_text(encoding="utf-8")
    monkeypatch.setattr(Demo, "offset", 5)
    generator.generate(Demo(exploded=True))
    after = (paths / f"{NAME}_annotated_screws.paths.svg").read_text(encoding="utf-8")
    assert before != after


def test_single_step_build_refreshes_paths_even_when_previous_figures_exist(
    pool, monkeypatch
):
    from hardware.assembly import build_procedures

    svg, paths = pool
    monkeypatch.setattr(Demo, "svg_path", lambda self, index=None: svg / f"{NAME}.svg")
    monkeypatch.setattr(Demo, "output_path", lambda self: svg / "unused.step")
    monkeypatch.setattr(Demo, "export", lambda self: None)
    monkeypatch.setattr(Demo, "render", lambda self, **kwargs: None)
    build_procedures._run_one(Demo, True)
    before = (paths / f"{NAME}_annotated_screws.paths.svg").read_text(encoding="utf-8")
    monkeypatch.setattr(Demo, "offset", 5)
    build_procedures._run_one(Demo, True)
    assert (paths / f"{NAME}_annotated_screws.paths.svg").read_text(
        encoding="utf-8"
    ) != before


@pytest.mark.parametrize("missing", [None, "figure", "paths"])
def test_resume_repairs_annotations_without_exporting_or_rendering_again(
    pool, monkeypatch, missing
):
    from hardware.assembly import build_procedures

    svg, paths = pool
    [figure] = generator.generate(Demo(exploded=True))
    intermediate = paths / f"{NAME}_annotated_screws.paths.svg"
    before = figure.read_bytes(), intermediate.read_bytes()
    if missing == "figure":
        figure.unlink()
    elif missing == "paths":
        intermediate.unlink()

    step = svg / "completed.step"
    step.write_text("completed geometry", encoding="utf-8")
    monkeypatch.setattr(Demo, "output_path", lambda self: step)
    monkeypatch.setattr(Demo, "svg_path", lambda self, index=None: svg / f"{NAME}.svg")

    def unexpected(*args, **kwargs):
        raise AssertionError("Completed source outputs must be reused")

    monkeypatch.setattr(Demo, "export", unexpected)
    monkeypatch.setattr(Demo, "render", unexpected)
    if missing is None:
        monkeypatch.setattr(Demo, "build", unexpected)
    assert build_procedures._run_one(Demo, True, only_missing=True) is None
    assert (figure.read_bytes(), intermediate.read_bytes()) == before


def test_assembled_destination_uses_exploded_views_camera(pool, monkeypatch):
    from hardware.assembly.projection import camera_view

    monkeypatch.setattr(Demo, "camera", Camera(45, 30))
    monkeypatch.setattr(
        specs,
        "FIGURES",
        specs._index_figures(
            Figure(
                view=NAME,
                name="screws",
                highlights=(Highlight("Screw*", assembled=True),),
            )
        ),
    )
    _, paths = pool
    asm = Demo(exploded=True)
    projection = geometry.Projection(camera_view(asm.build(), asm.cameras[0]))
    regions = []
    draw_region = svg.Drawing.region

    def capture_region(drawing, region, *args, **kwargs):
        regions.append(region)
        return draw_region(drawing, region, *args, **kwargs)

    monkeypatch.setattr(svg.Drawing, "region", capture_region)
    generator.generate(asm)
    assert (paths / f"{NAME}_annotated_screws.paths.svg").is_file()
    # Silhouette union snaps projected coordinates to a 0.002 mm grid.
    assert regions[0].centroid.coords[0] == pytest.approx(
        projection.point((0, 0, 0)), abs=0.002
    )
    assert (
        regions[0].centroid.distance(
            features.feature_region(
                geometry.select_parts(asm.build(), "Screw*")[0],
                projection,
                "silhouette",
            ).centroid
        )
        > 1
    )


def test_failed_output_staging_preserves_the_previous_pair(pool, monkeypatch):
    from pathlib import Path

    _, paths = pool
    [figure] = generator.generate(Demo(exploded=True))
    intermediate = paths / f"{NAME}_annotated_screws.paths.svg"
    before = figure.read_bytes(), intermediate.read_bytes()
    monkeypatch.setattr(Demo, "offset", 5)
    write_text = Path.write_text

    def fail_staging(path, *args, **kwargs):
        if path == figure.with_suffix(".tmp"):
            raise OSError("cannot stage final figure")
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_staging)
    with pytest.raises(OSError, match="cannot stage final figure"):
        generator.generate(Demo(exploded=True))
    assert (figure.read_bytes(), intermediate.read_bytes()) == before
    assert not figure.with_suffix(".tmp").exists()
    assert not intermediate.with_suffix(".tmp").exists()


@pytest.mark.parametrize("commit", ["intermediate", "final"])
def test_interrupted_pair_commit_is_repaired_on_resume(pool, monkeypatch, commit):
    from pathlib import Path

    _, paths = pool
    [figure] = generator.generate(Demo(exploded=True))
    intermediate = paths / f"{NAME}_annotated_screws.paths.svg"
    monkeypatch.setattr(Demo, "offset", 5)
    replace = Path.replace
    interrupted = intermediate if commit == "intermediate" else figure

    def fail_commit(path, target):
        if target == interrupted:
            raise OSError("interrupted pair commit")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_commit)
    with pytest.raises(OSError, match="interrupted pair commit"):
        generator.generate(Demo(exploded=True))
    assert not figure.exists()
    assert not figure.with_suffix(".tmp").exists()
    assert not intermediate.with_suffix(".tmp").exists()
    monkeypatch.setattr(Path, "replace", replace)
    assert generator.generate(Demo(exploded=True), only_missing=True) == [figure]
    assert figure.read_text(encoding="utf-8") == svg.compose(
        SVG, intermediate.read_text(encoding="utf-8")
    )


def test_bad_selector_fails_without_replacing_valid_outputs(pool, monkeypatch):
    _, paths = pool
    [figure] = generator.generate(Demo(exploded=True))
    before = figure.read_bytes()
    intermediate = paths / f"{NAME}_annotated_screws.paths.svg"
    before_paths = intermediate.read_bytes()
    monkeypatch.setattr(
        specs,
        "FIGURES",
        specs._index_figures(
            Figure(view=NAME, name="screws", highlights=(Highlight("MissingPart"),))
        ),
    )
    with pytest.raises(ValueError, match="no part matches"):
        generator.generate(Demo(exploded=True))
    assert figure.read_bytes() == before
    assert intermediate.read_bytes() == before_paths


def test_selector_supports_repeated_labels_and_nested_parts():
    first, second = Box(1, 1, 1), Box(2, 2, 2)
    first.label = second.label = "Nut"
    group = Compound(label="Fasteners", children=[first, second])
    root = Compound(label="Step", children=[group])
    assert geometry.select_parts(root, "Fasteners/Nut") == [first, second]
    assert geometry.select_parts(root, "Fast*/Nut[1]") == [second]
    assert geometry.select_parts(root, "Fasteners/Nut[1]|Fasteners/Nut[0]") == [
        second,
        first,
    ]


@pytest.mark.parametrize(
    "camera",
    [
        ((0, -100, 0), (0, 0, 1), (0, 0, 0)),
        ((80, -100, 40), (0, 0, 1), (3, 4, 5)),
        ((0, 0, 100), (0, 1, 0), (10, -20, 0)),
        ((60, 100, -50), (1, 2, 3), (5, -10, 7)),
    ],
)
def test_projection_matches_cad_vector_math_for_tuples_and_vectors(camera):
    projection = geometry.Projection(camera)
    for point in ((0, 0, 0), (1.25, -32.5, 70.125), tuple(projection.origin)):
        relative = Vector(point) - projection.origin
        expected = relative.dot(projection.x), -relative.dot(projection.y)
        assert projection.point(point) == pytest.approx(expected, rel=0, abs=1e-12)
        assert projection.point(Vector(point)) == pytest.approx(
            expected, rel=0, abs=1e-12
        )


def test_silhouette_keeps_disconnected_parts_separate():
    shape = Compound(children=[Box(2, 2, 2), Box(2, 2, 2).moved(Location((10, 0, 0)))])
    projection = geometry.Projection(((0, -100, 0), (0, 0, 1), (0, 0, 0)))
    region = geometry.silhouette(shape, projection)
    assert len(region.geoms) == 2
    assert region.area == pytest.approx(8, abs=0.01)


def test_surface_highlight_preserves_openings_and_writes_inner_paths():
    from build123d import Cylinder

    shape = Box(20, 20, 4) - Cylinder(3, 8)
    projection = geometry.Projection(((0, 0, 100), (0, 1, 0), (0, 0, 0)))
    solid = features.feature_region(shape, projection, "silhouette")
    surface = features.feature_region(shape, projection, "surface")
    assert solid.area - surface.area == pytest.approx(3.14159 * 9, rel=0.03)
    drawing = svg.Drawing()
    drawing.region(surface)
    path = ET.fromstring(drawing.elements[0])
    assert path.attrib["fill-rule"] == "evenodd"
    assert path.attrib["d"].count("M ") == 2


@pytest.mark.parametrize("feature", ["silhouette", "mount-seat"])
def test_nested_highlights_use_the_complete_assembly_placement(feature):
    part = Box(2, 2, 2).moved(Location((2, 0, 0)))
    part.label = "Screw_BHCS_M3x8"
    group = Compound(children=[part]).moved(Location((8, 0, 0)))
    root = Compound(children=[group])
    nested = root.children[0].children[0]
    projection = geometry.Projection(((0, -100, 100), (0, 0, 1), (0, 0, 0)))
    region = features.feature_region(nested, projection, feature)
    x0, _, x1, _ = region.bounds
    assert (x0 + x1) / 2 == pytest.approx(10, abs=0.01)


def test_crop_expands_to_include_generated_callouts():
    drawing = svg.Drawing()
    drawing.region(Polygon([(10, 10), (15, 10), (15, 15)]))
    root = ET.fromstring(svg.path_svg(SVG, drawing, crop=(0, 0, 1, 1)))
    assert root.attrib["viewBox"] == "0.0000 0.0000 15.0000 15.0000"


def test_nut_insertion_arrows_fit_the_manual_frame():
    from hardware.assembly.dispatch import load_class
    from hardware.assembly.projection import camera_view

    figure = next(f for f in specs.FIGURES.values() if f.name == "nut_insertion")
    assembly = load_class("frame_10_extrusion_tnut")(exploded=True)
    root = assembly.build()
    projection = geometry.Projection(camera_view(root, assembly.cameras[0]))
    drawing = svg.Drawing()
    callouts.draw_callouts(
        figure.callout, root=root, projection=projection, drawing=drawing
    )
    x, y, width, height = figure.crop
    assert all(
        x <= left <= right <= x + width and y <= top <= bottom <= y + height
        for left, top, right, bottom in drawing.bounds
    )


@pytest.mark.parametrize("camera_index", [0, 1])
def test_clamp_threading_keeps_blue_route_on_left_in_both_views(camera_index):
    from hardware.assembly.procedures.belt_11_clamp_show import BE11ClampShow
    from hardware.assembly.projection import camera_view

    asm = BE11ClampShow()
    root = asm.build()
    projection = geometry.Projection(camera_view(root, asm.cameras[camera_index]))
    drawing = svg.Drawing()
    callouts.draw_callouts(
        "clamp-threading", root=root, projection=projection, drawing=drawing
    )
    centres = {}
    for color in (specs.BLUE, specs.RED):
        boxes = [
            bounds
            for element, bounds in zip(drawing.elements, drawing.bounds)
            if color in element
        ]
        centres[color] = (min(b[0] for b in boxes) + max(b[2] for b in boxes)) / 2
    assert centres[specs.BLUE] < centres[specs.RED]


@pytest.mark.parametrize(
    "feature",
    [
        "mount-seat",
        "plate-bore",
        "slider-seat",
        "stack-seat",
        "joint-countersink",
        "countersink-wall",
        "exploded-countersink",
        "top-face",
        "front-pocket",
        "slant-pocket-right",
        "shaft",
        "mount-holes",
    ],
)
def test_generated_features_transform_local_points_into_world_coordinates(feature):
    part = Box(10, 10, 10).moved(Location((5, 8, 10), (0, 30, 20)))
    part.label = "Screw_BHCS_M3x8"
    projection = geometry.Projection(((80, -100, 40), (0, 0, 1), (0, 0, 0)))
    region = features.feature_region(part, projection, feature)
    assert region.is_valid and region.area > 0


@pytest.mark.parametrize(
    "stem, name, count",
    [
        ("idler_12_lu", "upper_left_idler_fasteners", 1),
        ("idler_22_ru", "upper_right_idler_fasteners", 1),
        ("idler_31_ld", "lower_left_idler_fasteners", 1),
        ("idler_41_rd", "lower_right_idler_fasteners", 1),
        ("motor_11_frame", "motor_a_bracket_mount", 2),
        ("motor_21_frame", "motor_b_bracket_mount", 2),
    ],
)
def test_frame_seat_markers_match_actual_tnut_openings(stem, name, count):
    from build123d import GeomType, Keep, Plane

    from hardware.assembly.dispatch import load_class
    from hardware.assembly.projection import camera_view

    cls = load_class(stem)
    assembly = cls(exploded=True)
    visible = assembly.build()
    assembled = cls(exploded=False).build()
    projection = geometry.Projection(camera_view(visible, assembly.cameras[1]))
    figure = next(f for f in specs.FIGURES.values() if f.name == name)
    [highlight] = [h for h in figure.highlights if h.assembled]
    screws = geometry.select_parts(assembled, highlight.part)
    nuts = [p for p in geometry.descendants(visible) if p.label == "TNut_standard_M5"]
    assert len(screws) == count
    for screw in screws:
        axis = screw.global_location.position
        # Read the receiving opening from CAD edges, independently of the rule.
        openings = [
            edge
            for nut in nuts
            for edge in geometry.world_shape(nut).edges().filter_by(GeomType.CIRCLE)
            if abs(edge.arc_center.X - axis.X) < 1e-6
            and abs(edge.arc_center.Z - axis.Z) < 1e-6
        ]
        assert len(openings) == 2  # Front and back of the through-bore.
        opening = min(openings, key=lambda edge: edge.arc_center.Y)
        center = opening.arc_center
        expected = _project_circle(opening, projection)
        # Only material in front of the CAD opening can occlude it.
        for beam in geometry.descendants(visible):
            if not beam.label.startswith("Extrusion"):
                continue
            front = geometry.world_shape(beam).split(
                Plane(origin=center, z_dir=(0, -1, 0)), Keep.TOP
            )
            if front is not None:
                front = Compound(children=front if isinstance(front, list) else [front])
                expected = expected.difference(geometry.silhouette(front, projection))
        marker = features.feature_region(screw, projection, highlight.feature)
        assert marker.symmetric_difference(expected).area < 0.04


@pytest.mark.parametrize("stem", ["idler_31_ld", "idler_41_rd"])
def test_lower_idler_seats_are_hidden_by_foreground_corner_plates(stem):
    from hardware.assembly.dispatch import load_class
    from hardware.assembly.projection import camera_view

    cls = load_class(stem)
    assembly = cls(exploded=True)
    visible, assembled = assembly.build(), cls(exploded=False).build()
    projection = geometry.Projection(camera_view(visible, assembly.cameras[1]))
    figure = next(
        f for f in specs.FIGURES.values() if f.view == f"{stem}_exploded_cam1"
    )
    [highlight] = [h for h in figure.highlights if h.assembled]
    [screw] = geometry.select_parts(assembled, highlight.part)
    opening = features.feature_region(screw, projection, highlight.feature)
    plates = features.occlusion_region(visible, projection, "**/FlatBracket")
    [(_, marker)] = features.highlight_regions(
        (highlight,),
        visible_root=visible,
        assembled_root=assembled,
        projection=projection,
    )
    assert 0 < marker.area < opening.area
    assert opening.intersection(plates).area > 0.1
    assert marker.intersection(plates).area < 1e-8


def _project_circle(edge, projection):
    return Polygon([projection.point(edge.position_at(i / 80)) for i in range(80)])


def _coaxial_circles(shape, screw):
    """Read full CAD circles sharing this screw axis, in its native frame."""
    import math

    from build123d import GeomType

    inverse = screw.global_location.inverse()
    return [
        (edge, (inverse * Location(edge.arc_center)).position.Z)
        for edge in geometry.world_shape(shape).edges().filter_by(GeomType.CIRCLE)
        if (inverse * Location(edge.arc_center)).position.X ** 2
        + (inverse * Location(edge.arc_center)).position.Y ** 2
        < 1e-10
        and abs(edge.length - math.tau * edge.radius) < 1e-5
    ]


def test_planar_highlights_are_occluded_only_by_material_in_front():
    face = Box(10, 10, 1).faces().sort_by(lambda face: face.center().Z)[-1]
    projection = geometry.Projection(((0, 0, 100), (0, 1, 0), (0, 0, 0)))
    for z, expected_area in ((-3, 100), (3, 0)):
        occluder = Box(12, 12, 2).moved(Location((0, 0, z)))
        region = geometry.visible_face_region(face, occluder, Location(), projection)
        assert region.area == pytest.approx(expected_area)


def test_solenoid_attach_markers_fill_both_actual_mount_clearance_holes():
    from hardware.assembly.procedures.tapz_11_solenoid_attach import TZ11SolenoidAttach
    from hardware.assembly.projection import camera_view

    assembly = TZ11SolenoidAttach(exploded=True)
    root = assembly.build()
    projection = geometry.Projection(camera_view(root, assembly.cameras[1]))
    [mount] = geometry.select_parts(root, "SolenoidMount")
    figure = next(f for f in specs.FIGURES.values() if f.name == "solenoid_fasteners")
    [highlight] = [h for h in figure.highlights if h.feature == "mount-seat"]
    screws = geometry.select_parts(root, highlight.part)
    assert len(screws) == 2
    for screw in screws:
        opening, _ = max(_coaxial_circles(mount, screw), key=lambda item: item[1])
        marker = features.feature_region(screw, projection, highlight.feature)
        assert (
            marker.symmetric_difference(_project_circle(opening, projection)).area
            < 0.03
        )


def test_frame_bore_indicators_follow_short_members_and_nuts_include_exposed_ends():
    from build123d import GeomType

    from hardware.assembly.procedures.frame_20_SHCS import FR20SHCS
    from hardware.assembly.projection import camera_view
    from hardware.parts.standard.extrusion_spec import (
        bore_diameter,
        cb_shaft_d,
        end_tap_depth,
    )
    from hardware.parts.standard.t_nut import ENGAGEMENT_Y

    assembly = FR20SHCS(exploded=True)
    root = assembly.build()
    projection = geometry.Projection(camera_view(root, assembly.cameras[0]))
    captured = []
    drawing = svg.Drawing()
    drawing.region = lambda region, color=specs.BLUE, opacity=0.35: captured.append(
        (region, opacity)
    )
    callouts.draw_callouts(
        "frame-bores", root=root, projection=projection, drawing=drawing
    )
    [threads] = [region for region, opacity in captured if opacity == 0.15]
    openings = [region for region, opacity in captured if opacity == 0.65]
    assert len(openings) == 4  # Only the four short-member tap openings.
    longs = [
        part
        for part in geometry.select_parts(root, "frame_10_extrusion_tnut/Extrusion*")
        if "_cb" in part.label
    ]
    left = min(longs, key=lambda part: geometry.world_shape(part).bounding_box().min.X)
    circles = geometry.world_shape(left).edges().filter_by(GeomType.CIRCLE)
    shaft_edges = [edge for edge in circles if abs(edge.radius - cb_shaft_d / 2) < 1e-6]
    floor_x = min(edge.arc_center.X for edge in shaft_edges)
    floor_centers = {
        tuple(edge.arc_center)
        for edge in shaft_edges
        if abs(edge.arc_center.X - floor_x) < 1e-6
    }
    assert len(floor_centers) == 4
    for center in floor_centers:
        point = Point(projection.point(center))
        assert not threads.covers(point)
    # Read the end-cell bore centers from the two actual short members.
    shorts = [
        part
        for part in geometry.select_parts(root, "frame_10_extrusion_tnut/Extrusion*")
        if "_cb" not in part.label
    ]
    assert len(shorts) == 2
    centers = [
        edge.arc_center
        for short in shorts
        for edge in geometry.world_shape(short).edges().filter_by(GeomType.CIRCLE)
        if abs(edge.radius - bore_diameter / 2) < 1e-6
    ]
    assert len(centers) == 8
    for center in centers:
        inward = Vector(-1 if center.X > 0 else 1, 0, 0)
        midpoint = center + inward * (end_tap_depth / 2)
        assert threads.covers(Point(projection.point(midpoint)))

    figure = next(f for f in specs.FIGURES.values() if f.name == "frame_fasteners")
    [highlight] = [h for h in figure.highlights if h.color == specs.RED]
    nuts = geometry.select_parts(root, highlight.part)
    assert len(nuts) == 8
    for nut in nuts:
        location = nut.global_location
        native = Compound.cast(nut.wrapped.Located(Location().wrapped))
        bounds = native.bounding_box()
        direction = (
            location.inverse() * Location(location.position + projection.direction)
        ).position
        end = bounds.max.Z if direction.Z > 0 else bounds.min.Z
        # This exposed end lies below the crown/neck band used previously.
        shoulder = (
            location * Location((0, ENGAGEMENT_Y["standard"] / 2, end))
        ).position
        marker = features.feature_region(nut, projection, highlight.feature)
        assert marker.buffer(0.01).covers(Point(projection.point(shoulder)))


def test_frame_head_recesses_follow_the_slot_lip_cuts():
    from build123d import GeomType

    from hardware.assembly.procedures.frame_20_SHCS import FR20SHCS
    from hardware.assembly.projection import camera_view
    from hardware.parts.standard.extrusion_spec import (
        cb_head_d,
        leg,
        slot_lip_under_y,
    )

    assembly = FR20SHCS(exploded=True)
    root = assembly.build()
    projection = geometry.Projection(camera_view(root, assembly.cameras[0]))
    figure = next(f for f in specs.FIGURES.values() if f.name == "frame_fasteners")
    [highlight] = [h for h in figure.highlights if h.feature == "frame-head-recess"]
    assert highlight.opacity == 0.65
    beams = sorted(
        geometry.select_parts(root, highlight.part),
        key=lambda beam: geometry.world_shape(beam).bounding_box().min.X,
    )
    left, right = beams
    marker = features.feature_region(left, projection, highlight.feature)
    assert len(marker.geoms) == 4
    assert features.feature_region(right, projection, highlight.feature).is_empty

    # Independently locate the actual exposed walls in world coordinates.
    beam = geometry.world_shape(left)
    mouth_x = beam.bounding_box().min.X
    walls = [
        face
        for face in beam.faces().filter_by(GeomType.CYLINDER)
        if face.radius is not None
        and abs(face.radius - cb_head_d / 2) < 1e-6
        and face.bounding_box().max.X <= mouth_x + leg - slot_lip_under_y + 1e-6
        and face.normal_at().dot(projection.direction) > 0
    ]
    assert len(walls) == 4
    for face in walls:
        # Both curved sides and the stepped ends must follow the CAD edges.
        for edge in face.edges():
            for i in range(41):
                point = Point(projection.point(edge.position_at(i / 40)))
                assert marker.boundary.distance(point) < 0.01
        # The disc's tip lies in the slot void beyond the stepped wall end.
        rim = next(edge for edge in face.edges() if edge.geom_type == GeomType.CIRCLE)
        tip = rim.arc_center + Vector(0, 0, cb_head_d / 2 - 0.1)
        assert not marker.covers(Point(projection.point(tip)))


def test_belt_clamp_marks_all_four_slider_openings_behind_belt():
    from hardware.assembly.procedures.belt_20_clamp import BE20Clamp
    from hardware.assembly.projection import camera_view

    assembly = BE20Clamp(exploded=True)
    visible = assembly.build()
    assembled = BE20Clamp(exploded=False).build()
    projection = geometry.Projection(camera_view(visible, assembly.cameras[1]))
    sliders = geometry.select_parts(assembled, "**/MGN9H")
    figure = next(f for f in specs.FIGURES.values() if f.name == "clamp_fasteners")
    [highlight] = [h for h in figure.highlights if h.assembled]
    screws = geometry.select_parts(assembled, highlight.part)
    assert len(screws) == 4
    [(_, visible_markers)] = features.highlight_regions(
        (highlight,),
        visible_root=visible,
        assembled_root=assembled,
        projection=projection,
    )
    belt = features.occlusion_region(visible, projection, "belt_10_motor_a/Belt")
    assert visible_markers.intersection(belt).area < 1e-8
    occluded_count = 0
    for screw in screws:
        circles = [
            circle for slider in sliders for circle in _coaxial_circles(slider, screw)
        ]
        # The chamfer mouth is larger than the nominal screw shank.
        opening, _ = max(circles, key=lambda item: item[0].radius)
        marker = features.feature_region(screw, projection, highlight.feature)
        assert (
            marker.symmetric_difference(_project_circle(opening, projection)).area
            < 0.03
        )
        exposed = marker.difference(belt)
        assert exposed.area > 0
        assert exposed.difference(visible_markers).area < 1e-8
        if marker.intersection(belt).area > 0.01:
            occluded_count += 1
    assert occluded_count == 2


def test_x_beam_screw_receivers_follow_both_joint_countersinks():
    from hardware.assembly.procedures.linear_31_x import LI31X
    from hardware.assembly.projection import camera_view

    assembly = LI31X(exploded=True)
    visible = assembly.build()
    assembled = LI31X(exploded=False).build()
    projection = geometry.Projection(camera_view(visible, assembly.cameras[1]))
    figure = next(f for f in specs.FIGURES.values() if f.name == "rail_mount")
    [highlight] = [h for h in figure.highlights if h.assembled]
    screws = geometry.select_parts(assembled, highlight.part)
    joints = geometry.select_parts(assembled, "**/XyJoint*")
    assert len(screws) == len(joints) == 2
    for screw in screws:
        circles = [
            circle for joint in joints for circle in _coaxial_circles(joint, screw)
        ]
        entry, entry_z = max(circles, key=lambda item: item[1])
        throat, _ = max(
            (item for item in circles if item[1] < entry_z - 1e-5),
            key=lambda item: item[1],
        )
        expected = _project_circle(entry, projection).difference(
            _project_circle(throat, projection)
        )
        marker = features.feature_region(screw, projection, highlight.feature)
        assert marker.symmetric_difference(expected).area < 0.03
        assert marker.intersection(_project_circle(throat, projection)).area < 0.01


@pytest.mark.parametrize(
    "feature,count", [("plate-bore", 4), ("exploded-countersink", 2)]
)
def test_solenoid_mount_highlights_follow_actual_inner_walls(feature, count):
    from hardware.assembly.procedures.tapz_20_solenoid_mount import TZ20SolenoidMount
    from hardware.assembly.projection import camera_view

    assembly = TZ20SolenoidMount(exploded=True)
    root = assembly.build()
    projection = geometry.Projection(camera_view(root, assembly.cameras[1]))
    [mount] = geometry.select_parts(root, "**/SolenoidMount")
    figure = next(f for f in specs.FIGURES.values() if f.name == "mount_fasteners")
    [highlight] = [h for h in figure.highlights if h.feature == feature]
    screws = geometry.select_parts(root, highlight.part)
    assert len(screws) == count
    for screw in screws:
        circles = _coaxial_circles(mount, screw)
        entry, entry_z = max(circles, key=lambda item: item[1])
        # For a countersink use its throat, before the straight through-bore.
        exit, _ = max(
            (item for item in circles if item[1] < entry_z - 1e-5),
            key=lambda item: item[1],
        )
        expected = _project_circle(entry, projection).difference(
            _project_circle(exit, projection)
        )
        marker = features.feature_region(screw, projection, feature)
        assert marker.symmetric_difference(expected).area < 0.03


def test_seated_beam_nuts_have_no_missing_shoulder_or_hidden_wing_overfill():
    from hardware.assembly.procedures.linear_30_x import LI30X
    from hardware.assembly.projection import camera_view
    from hardware.parts.standard.t_nut import ENGAGEMENT_Y, HALF_PROFILES, LENGTHS

    assembly = LI30X(exploded=False)
    root = assembly.build()
    projection = geometry.Projection(camera_view(root, assembly.cameras[1]))
    figure = next(f for f in specs.FIGURES.values() if f.name == "tnut_position")
    [highlight] = figure.highlights
    nuts = geometry.select_parts(root, highlight.part)
    assert len(nuts) == 2
    for nut in nuts:
        marker = features.feature_region(nut, projection, highlight.feature)
        # Crown and exposed end meet without the former white wedge.
        assert marker.buffer(0.01).geom_type == "Polygon"
        location = nut.global_location
        profile = HALF_PROFILES["standard"]
        shoulder = projection.point(
            (
                location
                * Location(
                    (
                        -profile[-2][0],
                        (ENGAGEMENT_Y["standard"] + profile[-1][1]) / 2,
                        LENGTHS["standard"],
                    )
                )
            ).position
        )
        hidden_wing = projection.point(
            (location * Location((-profile[2][0], profile[2][1], 0))).position
        )

        assert marker.buffer(0.01).covers(Point(shoulder))
        assert not marker.buffer(0.01).covers(Point(hidden_wing))


def test_tube_holder_highlight_fills_seated_fastener_openings():
    from hardware.assembly.procedures.board_32_tube_holder import BO32TubeHolder
    from hardware.assembly.projection import camera_view

    assembly = BO32TubeHolder(exploded=False)
    root = assembly.build()
    projection = geometry.Projection(camera_view(root, assembly.cameras[1]))
    figure = next(f for f in specs.FIGURES.values() if f.name == "tube_holder")
    [highlight] = figure.highlights
    [holder] = geometry.select_parts(root, highlight.part)
    marker = features.feature_region(holder, projection, highlight.feature)
    assert marker.geom_type == "Polygon"
    assert not marker.interiors


def test_camera_mount_excludes_hidden_fasteners_but_keeps_visible_screw_head():
    from hardware.assembly.procedures.camera_40_frame import Camera40Frame
    from hardware.assembly.projection import camera_view

    assembly = Camera40Frame(exploded=False)
    root = assembly.build()
    projection = geometry.Projection(camera_view(root, assembly.cameras[1]))
    figure = next(f for f in specs.FIGURES.values() if f.name == "camera_mount")
    [highlight] = figure.highlights
    [mount] = geometry.select_parts(root, highlight.part)
    marker = features.feature_region(mount, projection, highlight.feature)
    [bracket] = geometry.select_parts(mount, "**/CornerBracket")
    foot = geometry.silhouette(bracket, projection)
    leaves = list(geometry.descendants(mount))
    [nut] = [leaf for leaf in leaves if leaf.label == "TNut_hammer_M5"]
    [screw] = [leaf for leaf in leaves if leaf.label == "Screw_BHCS_M5x10"]
    # The complete nut and screw previously protruded beyond the bracket.
    hidden = (
        geometry.silhouette(nut, projection)
        .union(geometry.silhouette(screw, projection))
        .difference(foot)
    )
    head_center = projection.point(
        (screw.global_location * Location((0, 0, 1))).position
    )
    assert marker.buffer(0.01).covers(Point(head_center))
    # Check the lobe furthest outside the foot, where the hidden shank was.
    outside = hidden.difference(foot.buffer(0.2))
    assert outside.area > 1
    assert marker.intersection(outside).area < 0.05


@pytest.mark.parametrize(
    "stem,side",
    [
        ("linear_41_idler_lj1", -1),
        ("linear_43_idler_lj2", -1),
        ("linear_45_idler_rj1", 1),
        ("linear_47_idler_rj2", 1),
    ],
)
def test_receiving_pocket_marks_the_joint_on_the_installation_side(stem, side):
    from hardware.assembly.dispatch import load_class

    root = load_class(stem)(exploded=True).build()
    [highlight] = [
        h
        for h in next(
            f for f in specs.FIGURES.values() if f.view == f"{stem}_exploded_cam1"
        ).highlights
        if "pocket" in h.feature
    ]
    [part] = geometry.select_parts(root, highlight.part)
    # Joint names describe the manufactured handedness. The stage installs
    # XyJointRight on its left, so names alone cannot choose the target.
    assert part.global_location.position.X * side > 0


def test_occluding_parts_clip_highlights_before_composition(pool, monkeypatch):
    from shapely.geometry import box

    _, paths = pool
    monkeypatch.setattr(
        specs,
        "FIGURES",
        specs._index_figures(
            Figure(
                view=NAME,
                name="screws",
                highlights=(Highlight("Screw*", occluded_by="Base"),),
            )
        ),
    )
    monkeypatch.setattr(
        features,
        "silhouette",
        lambda part, projection: (
            box(0, 0, 3, 2) if part.label.startswith("Screw") else box(1, -1, 2, 3)
        ),
    )
    monkeypatch.setitem(features.FEATURES, "silhouette", features.silhouette)
    generator.generate(Demo(exploded=True))
    root = ET.fromstring(
        (paths / f"{NAME}_annotated_screws.paths.svg").read_text(encoding="utf-8")
    )
    # The front component splits the rear highlight into two visible islands.
    assert root[0].attrib["d"].count("M ") == 2
