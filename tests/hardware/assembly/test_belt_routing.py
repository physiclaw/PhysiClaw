"""Routing belongs to assembly; Belt owns only the supplied path's shape."""

import math
import subprocess
import sys

import pytest

pytest.importorskip("build123d", reason="CAD group not installed")

from build123d import Vector
from shapely.geometry import Point

from hardware.assembly.annotation import features, specs
from hardware.assembly.annotation.geometry import Projection, select_parts, world_shape
from hardware.assembly.belt_routing import motor_path
from hardware.assembly.procedures.belt_10_motor_a import (
    BELT_EXPLODE as MOTOR_A_EXPLODE,
)
from hardware.assembly.procedures.belt_10_motor_a import BE10MotorA
from hardware.assembly.procedures.belt_20_clamp import BE20Clamp
from hardware.assembly.procedures.belt_30_motor_b import BELT_EXPLODE, BE30MotorB
from hardware.assembly.projection import camera_view
from hardware.parts.standard.belt import Belt, _expand_wraps, belt_thickness
from hardware.scheme import REPO_ROOT
from tests.timing_helpers import PROCESS_TIMEOUT


def test_belt_import_does_not_load_assembly_procedures():
    script = """
import sys
from hardware.parts.standard.belt import Belt
assert not any(name.startswith('hardware.assembly') for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_routes_preserve_motor_planes_and_cache_identity():
    a, b = motor_path("A"), motor_path("B")
    assert len(a) == len(b)
    for (pa, ra), (pb, rb) in zip(a, b, strict=True):
        assert (pb[0], pb[2]) == pytest.approx((-pa[0], pa[2]))
        assert pa[1] - pb[1] == pytest.approx(9 if ra is not None else 9.55)
        assert ra == rb
    left, right = Belt(a, name="motor_a"), Belt(b, name="motor_b")
    assert left.bom_key() == right.bom_key()
    assert left.geom_key() != right.geom_key()
    assert left.output_path().name == "belt_motor_a_x1.step"
    assert right.output_path().name == "belt_motor_b_x1.step"
    a.clear()
    assert len(motor_path("A")) == len(left.path)


def test_generic_belt_build_uses_supplied_route_without_motor_transform():
    a = Belt([((0, 0, 0), None), ((10, 0, 0), None)]).build()
    b = Belt([((0, -9, 0), None), ((10, -9, 0), None)]).build()
    assert a.is_valid and b.is_valid
    assert a.volume == pytest.approx(b.volume)
    assert pytest.approx(0) == a.bounding_box().center().Y
    assert pytest.approx(-9) == b.bounding_box().center().Y


def test_empty_belt_route_has_actionable_error():
    with pytest.raises(ValueError, match="nonzero-length"):
        Belt([]).build()


def test_explicit_interior_points_preserve_a_bent_path():
    points = [(0, 0, 0), (10, 0, 0), (10, 0, 10)]
    route = [(point, None) for point in points]
    assert _expand_wraps(route) == points
    belt = Belt(route).build()
    assert belt.is_valid and len(belt.solids()) == 1
    # The outside corner must be filled to the miter. Independent butt-ended
    # extrusions leave this point empty and draw a broken-looking notch.
    assert belt.is_inside((10.5, 0, -0.5))


@pytest.mark.parametrize("motor", ["A", "B"])
def test_carriage_returns_fold_down_and_have_short_tails(motor):
    route = motor_path(motor)
    for path in (route, list(reversed(route))):
        # Explicit clamp points stop where the first idler wrap starts.
        end = path[: next(i for i, (_, radius) in enumerate(path) if radius)]
        tip, bend_bottom = end[0][0], end[1][0]
        assert tip[2] == pytest.approx(bend_bottom[2])
        assert max(p[2] for p, _ in end) - tip[2] > 4
        assert tip[2] < end[-1][0][2]
        assert abs(tip[0] - bend_bottom[0]) == pytest.approx(15.8)


@pytest.fixture(scope="module")
def carriage_parts():
    assembly = BE20Clamp(exploded=False).build()
    return [
        world_shape(select_parts(assembly, selector)[0])
        for selector in ("**/linear_32_x/MGN9H", "BeltClamp")
    ]


@pytest.mark.parametrize("motor", ["A", "B"])
def test_belt_clears_actual_carriage_and_clamp(carriage_parts, motor):
    belt = Belt(motor_path(motor)).build()
    assert belt.is_valid and len(belt.solids()) == 1
    for part in carriage_parts:
        # Check constituent solids: an intersection against the compound
        # can return an empty result despite overlap with its slider solid.
        for solid in part.solids():
            overlap = belt.intersect(solid)
            if overlap is not None:
                shapes = overlap if isinstance(overlap, list) else [overlap]
                assert sum(shape.volume for shape in shapes) < 1e-7
        assert belt.distance_to(part) > 0.05


def test_working_route_excludes_clamp_returns():
    full = motor_path("A")
    first = next(i for i, (_, radius) in enumerate(full) if radius)
    last = max(i for i, (_, radius) in enumerate(full) if radius)
    assert motor_path("A", include_clamp=False) == full[first - 1 : last + 2]


@pytest.mark.parametrize("motor", ["A", "B"])
def test_illustrated_return_has_point_two_mm_edge_gap(motor):
    route = motor_path(motor, illustrate_clamp=True)
    for path in (route, list(reversed(route))):
        end = path[: next(i for i, (_, radius) in enumerate(path) if radius)]
        tail = Belt(end[:2]).build()
        incoming = Belt(end[-2:]).build()
        assert tail.distance_to(incoming) == pytest.approx(0.2, abs=1e-6)
        assert math.dist(end[0][0], end[1][0]) == pytest.approx(15.8)


@pytest.mark.parametrize("motor", ["A", "B"])
def test_illustrated_incoming_run_has_no_crease_at_carriage_edge(motor):
    route = motor_path(motor, illustrate_clamp=True)
    for path in (route, list(reversed(route))):
        first_idler = next(i for i, (_, radius) in enumerate(path) if radius)
        before, entry = path[first_idler - 2][0], path[first_idler - 1][0]
        tangent = _expand_wraps(path[first_idler - 1 : first_idler + 2])[1]
        # Both sides of the carriage entry must be the same straight line,
        # including depth; even a tiny kink becomes a false splice in HLR.
        incoming = tuple(b - a for a, b in zip(before, entry, strict=True))
        outgoing = tuple(b - a for a, b in zip(entry, tangent, strict=True))
        scale = math.dist(entry, tangent) / math.dist(before, entry)
        assert outgoing == pytest.approx(tuple(v * scale for v in incoming), abs=1e-9)


@pytest.mark.parametrize(
    "motor,assembly_type,explode",
    [("A", BE10MotorA, MOTOR_A_EXPLODE), ("B", BE30MotorB, BELT_EXPLODE)],
)
def test_belt_illustration_and_highlight_keep_both_tail_gaps_open(
    motor, assembly_type, explode
):
    assembly = assembly_type(exploded=True)
    root = assembly.build()
    [part] = select_parts(root, "Belt")
    belt = world_shape(part)
    assert belt.is_valid and len(belt.solids()) == 1
    projection = Projection(camera_view(root, assembly.cameras[0]))
    figure = next(
        f
        for f in specs.FIGURES.values()
        if f.name == f"motor_{motor.lower()}_belt_route"
    )
    [(_, highlight)] = features.highlight_regions(
        figure.highlights,
        visible_root=root,
        assembled_root=root,
        projection=projection,
    )
    route = motor_path(motor, illustrate_clamp=True)
    for path in (route, list(reversed(route))):
        tip, bend_bottom = (Vector(point) for point, _ in path[:2])
        tail_center = (tip + bend_bottom) * 0.5 + Vector(0, -explode, 0)
        direction = (bend_bottom - tip).normalized()
        toward_incoming = Vector(-direction.Z, 0, direction.X)
        if toward_incoming.Z < 0:
            toward_incoming = -toward_incoming
        gap_center = tail_center + toward_incoming * (belt_thickness / 2 + 0.1)
        incoming_center = tail_center + toward_incoming * (belt_thickness + 0.2)
        # The actual exported assembly must retain the open illustrated gap,
        # rather than silently reverting to the installed, interlocked tails.
        assert belt.is_inside(tail_center)
        assert belt.is_inside(incoming_center)
        assert not belt.is_inside(gap_center)
        assert highlight.covers(Point(projection.point(tail_center)))
        assert highlight.covers(Point(projection.point(incoming_center)))
        assert not highlight.covers(Point(projection.point(gap_center)))
