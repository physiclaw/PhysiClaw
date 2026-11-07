"""Board highlights must stop at the visible foreground mounting plates."""

import pytest
from shapely.geometry import Point

from hardware.assembly.annotation import features, geometry, specs
from hardware.assembly.dispatch import load_class
from hardware.assembly.projection import camera_view


@pytest.mark.parametrize(
    "stem,name,plates,visible_point",
    [
        (
            "board_20_frame",
            "pcb_holder_mount_cam0",
            "**/MotorBracket",
            (-30, -180),
        ),
        ("board_30_pcb", "pcb_face", "**/MotorBracket", (-20, -185)),
        (
            "board_32_tube_holder",
            "tube_holder_mount",
            "**/FlatBracket|**/MotorBracket",
            (-65, -169),
        ),
    ],
)
def test_board_highlights_stop_at_foreground_plates(stem, name, plates, visible_point):
    assembly = load_class(stem)(exploded=False)
    root = assembly.build()
    projection = geometry.Projection(camera_view(root, assembly.cameras[0]))
    figure = next(f for f in specs.FIGURES.values() if f.name == name)
    [highlight] = figure.highlights
    [(_, region)] = features.highlight_regions(
        (highlight,),
        visible_root=root,
        assembled_root=root,
        projection=projection,
    )
    foreground = features.occlusion_region(root, projection, plates)
    # The visible upper body remains colored; its hidden tips do not paint
    # over the rounded motor plates and corner bracket at the frame edge.
    assert region.covers(Point(visible_point))
    assert region.intersection(foreground).area < 1e-8
