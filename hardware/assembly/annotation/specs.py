"""Manual figures generated from procedure geometry.

Selectors follow direct CAD children, separated by ``/``. Shell-style wildcards
select a family of parts; ``[n]`` picks a zero-based occurrence per parent.
``**`` searches all descendants, including the current root; ``|`` joins
selections. For example, ``**/Extrusion*`` finds extrusions at any depth.
Each figure has a stable descriptive name: <view>_annotated_<name>.svg.
Several figures may share a view; their names remain stable when reordered or
when another figure is removed.

Crops are editorial frames in projected millimetres at ILLUSTRATION_TRAVEL.
Highlights are always recomputed from the current parts and the view's camera.
This module stays stdlib-only for the build cache and consistency checker.
"""

import re
from dataclasses import dataclass

BLUE = "#1e88ff"
RED = "#e53935"
GREEN = "#16845b"


@dataclass(frozen=True)
class Highlight:
    """One styled CAD selection.

    part: selector relative to the procedure root (syntax above).
    assembled: use the seated model, keeping the exploded view's camera.
    feature: named region rule in features.FEATURES.
    occluded_by: foreground selector from the visible model, even when the
        highlighted target uses seated geometry.
    fill_occluder_slots: mask extrusion slot-mouth gaps as a solid outline;
        used where feet should read behind a continuous frame edge.
    """

    part: str
    opacity: float = 0.35
    color: str = BLUE
    assembled: bool = False
    feature: str = "silhouette"
    occluded_by: str | None = None
    fill_occluder_slots: bool = False


@dataclass(frozen=True, kw_only=True)
class Figure:
    """A manual view and its drawing rules.

    view is the raw SVG filename without .svg. name is a globally unique,
    descriptive annotation identifier. crop is (x, y, width,
    height) in projected millimetres; it expands to include any callouts.
    callout names a procedure-specific recipe in callouts.CALLOUTS.
    """

    view: str
    name: str
    highlights: tuple[Highlight, ...] = ()
    crop: tuple[float, float, float, float] | None = None
    callout: str | None = None

    @property
    def filename(self) -> str:
        return f"{self.view}_annotated_{self.name}.svg"


def _index_figures(*figures: Figure) -> dict[str, Figure]:
    """Reject conflicting declarations before any generated files are written."""
    result = {}
    outputs = {}
    names = {}
    for figure in figures:
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", figure.name):
            raise ValueError(
                f"Invalid annotation name {figure.name!r} for {figure.view!r}"
            )
        filename = figure.filename
        key = filename.casefold()
        if key in outputs:
            raise ValueError(
                f"Annotation output collision: {filename!r} conflicts with "
                f"{outputs[key]!r}; choose a distinct annotation name"
            )
        if figure.name in names:
            raise ValueError(
                f"Duplicate annotation name {figure.name!r}: used by "
                f"{names[figure.name]!r} and {figure.view!r}"
            )
        outputs[key] = filename
        names[figure.name] = figure.view
        result[filename] = figure
    return result


FIGURES: dict[str, Figure] = _index_figures(
    Figure(
        view="belt_10_motor_a_exploded_cam0",
        name="motor_a_belt_route",
        highlights=(Highlight("Belt", 0.65, feature="surface"),),
    ),
    Figure(
        view="belt_11_clamp_show_assembled_cam0",
        name="clamp_threading_cam0",
        callout="clamp-threading",
    ),
    Figure(
        view="belt_11_clamp_show_assembled_cam1",
        name="clamp_threading_cam1",
        callout="clamp-threading",
    ),
    Figure(
        view="belt_20_clamp_assembled_cam0",
        name="belt_clamp",
        highlights=(Highlight("BeltClamp", 0.65),),
    ),
    Figure(
        view="belt_20_clamp_exploded_cam1",
        name="clamp_fasteners",
        highlights=(
            Highlight("BeltClamp|Screw*"),
            Highlight(
                "Screw*",
                0.65,
                assembled=True,
                feature="slider-seat",
                occluded_by="belt_10_motor_a/Belt",
            ),
        ),
        crop=(-54.3398, -110.3465, 98.7473, 128.6283),
    ),
    Figure(
        view="belt_30_motor_b_assembled_cam0",
        name="belt_tension",
        highlights=(Highlight("belt_20_clamp/BeltClamp"),),
        callout="belt-tension",
    ),
    Figure(
        view="belt_30_motor_b_exploded_cam0",
        name="motor_b_belt_route",
        highlights=(Highlight("Belt", 0.65, feature="surface"),),
    ),
    Figure(
        view="board_10_holder_tnut_exploded_cam1",
        name="holder_fasteners",
        highlights=(
            Highlight("Screw*"),
            Highlight("TNut*"),
            Highlight("Screw*", 0.65, assembled=True, feature="countersink-wall"),
        ),
    ),
    Figure(
        view="board_20_frame_assembled_cam0",
        name="pcb_holder_mount_cam0",
        highlights=(
            Highlight(
                "board_10_holder_tnut",
                0.65,
                occluded_by="**/Extrusion*|**/MotorBracket",
            ),
        ),
    ),
    Figure(
        view="board_20_frame_assembled_cam1",
        name="pcb_holder_mount_cam1",
        highlights=(Highlight("board_10_holder_tnut"),),
        crop=(-16.3321, -38.028, 126.7416, 152.9138),
    ),
    Figure(
        view="board_30_pcb_assembled_cam0",
        name="pcb_face",
        highlights=(
            Highlight(
                "MksBoard",
                0.65,
                feature="board-front",
                occluded_by="**/PcbHolder|**/Extrusion*|**/MotorBracket",
            ),
        ),
    ),
    Figure(
        view="board_30_pcb_exploded_cam1",
        name="pcb_fasteners",
        highlights=(
            Highlight("Nut[0]|Nut[1]"),
            Highlight("MksBoard", 0.15),
            Highlight("Screw_BHCS_M3*"),
        ),
        crop=(30.56, -38.111, 135.1481, 158.2284),
    ),
    Figure(
        view="board_32_tube_holder_assembled_cam0",
        name="tube_holder_mount",
        highlights=(
            Highlight(
                "board_31_tube_tnut",
                0.65,
                occluded_by="**/Extrusion*|**/FlatBracket|**/MotorBracket",
            ),
        ),
    ),
    Figure(
        view="board_32_tube_holder_assembled_cam1",
        name="tube_holder",
        highlights=(Highlight("board_31_tube_tnut/TubeHolder"),),
        crop=(-157.5743, -70.4172, 113.5504, 185.4466),
    ),
    Figure(
        view="board_40_teflon_assembled_cam0",
        name="teflon_tube",
        highlights=(Highlight("Teflon", 0.65),),
    ),
    Figure(
        view="camera_40_frame_assembled_cam0",
        name="wiring_connections",
        highlights=(
            Highlight("**/motor_10_bracket", feature="surface"),
            Highlight("**/motor_20_bracket", feature="surface"),
            Highlight("**/Solenoid"),
        ),
    ),
    Figure(
        view="camera_40_frame_assembled_cam1",
        name="camera_mount",
        highlights=(Highlight("camera_30_mount", feature="camera-mount"),),
    ),
    Figure(
        view="frame_10_extrusion_tnut_exploded_cam0",
        name="nut_insertion",
        highlights=(
            Highlight("_layer_ghost/TNut_standard_M5", 0.15),
            Highlight("_layer_solid/TNut_standard_M5"),
        ),
        crop=(-204.1946, -101.4535, 409.3763, 202.907),
    ),
    Figure(
        view="frame_20_SHCS_exploded_cam0",
        name="frame_fasteners",
        highlights=(
            Highlight("Screw_SHCS_M6*"),
            Highlight("frame_10_extrusion_tnut/TNut*", 0.35, RED, feature="frame-tnut"),
            Highlight(
                "frame_10_extrusion_tnut/Extrusion*_cb",
                0.65,
                feature="frame-head-recess",
            ),
        ),
        callout="frame-bores",
    ),
    Figure(
        view="frame_30_bracket_tnut_exploded_cam0",
        name="bracket_fasteners",
        highlights=(Highlight("Screw_BHCS_M5*"), Highlight("TNut_hammer_M5")),
    ),
    Figure(
        view="frame_31_bracket_assembled_cam0",
        name="frame_bracket",
        highlights=(Highlight("frame_30_bracket_tnut"),),
    ),
    Figure(
        view="frame_40_bumper_tnut_exploded_cam0",
        name="bumper_fasteners",
        highlights=(Highlight("TNut_hammer_M5"), Highlight("Screw_BHCS_M5*")),
    ),
    Figure(
        view="frame_41_bumper_assembled_cam0",
        name="bumper",
        highlights=(
            Highlight(
                "frame_40_bumper_tnut/Bumper",
                0.65,
                occluded_by="frame_31_bracket",
                fill_occluder_slots=True,
            ),
        ),
    ),
    Figure(
        view="idler_12_lu_assembled_cam0",
        name="upper_left_idler_mount",
        highlights=(Highlight("idler_1[01]_lu", 0.65),),
    ),
    Figure(
        view="idler_12_lu_exploded_cam1",
        name="upper_left_idler_fasteners",
        highlights=(
            Highlight("idler_10_lu"),
            Highlight("idler_11_lu"),
            Highlight(
                "idler_10_lu/Screw_BHCS_M5*", 0.65, assembled=True, feature="frame-seat"
            ),
        ),
        crop=(-172.8992, -91.5858, 87.7895, 123.3614),
    ),
    Figure(
        view="idler_22_ru_assembled_cam0",
        name="upper_right_idler_mount",
        highlights=(Highlight("idler_2[01]_ru", 0.65),),
    ),
    Figure(
        view="idler_22_ru_exploded_cam1",
        name="upper_right_idler_fasteners",
        highlights=(
            Highlight("idler_20_ru"),
            Highlight("idler_21_ru"),
            Highlight(
                "idler_20_ru/Screw_BHCS_M5*", 0.65, assembled=True, feature="frame-seat"
            ),
        ),
        crop=(95.7649, -91.7786, 92.9353, 130.3197),
    ),
    Figure(
        view="idler_31_ld_assembled_cam0",
        name="lower_left_idler_mount",
        highlights=(Highlight("idler_30_ld", 0.65),),
    ),
    Figure(
        view="idler_31_ld_exploded_cam1",
        name="lower_left_idler_fasteners",
        highlights=(
            Highlight("idler_30_ld"),
            Highlight(
                "idler_30_ld/Screw_BHCS_M5*",
                0.65,
                assembled=True,
                feature="frame-seat",
                occluded_by="**/FlatBracket",
            ),
        ),
        crop=(-205.6947, -44.3062, 99.8882, 130.8134),
    ),
    Figure(
        view="idler_41_rd_assembled_cam0",
        name="lower_right_idler_mount",
        highlights=(Highlight("idler_40_rd", 0.65),),
    ),
    Figure(
        view="idler_41_rd_exploded_cam1",
        name="lower_right_idler_fasteners",
        highlights=(
            Highlight("idler_40_rd"),
            Highlight(
                "idler_40_rd/Screw_BHCS_M5*",
                0.65,
                assembled=True,
                feature="frame-seat",
                occluded_by="**/FlatBracket",
            ),
        ),
        crop=(-17.8064, -0.7243, 85.8887, 135.26),
    ),
    Figure(
        view="linear_10_y_exploded_cam0",
        name="y_rail_fasteners",
        highlights=(Highlight("Screw_BHCS_M3*"), Highlight("TNut_hammer_M3")),
    ),
    Figure(
        view="linear_11_y_exploded_cam1",
        name="y_rail",
        highlights=(Highlight("linear_10_y"),),
    ),
    Figure(
        view="linear_20_joint_assembled_cam0",
        name="xy_joints",
        highlights=(
            Highlight("xy_joint_right", 0.65),
            Highlight("xy_joint_left", 0.65),
        ),
    ),
    Figure(
        view="linear_20_joint_exploded_cam1",
        name="joint_positions",
        highlights=(Highlight("xy_joint_*"),),
        crop=(-113.9475, -100.3111, 158.3648, 184.5984),
    ),
    Figure(
        view="linear_30_x_assembled_cam1",
        name="tnut_position",
        highlights=(Highlight("TNut_standard_M5", feature="beam-tnut"),),
    ),
    Figure(
        view="linear_30_x_exploded_cam1",
        name="rail_tnuts",
        highlights=(Highlight("TNut_standard_M5"),),
    ),
    Figure(
        view="linear_31_x_exploded_cam1",
        name="rail_mount",
        highlights=(
            Highlight("Screw*"),
            Highlight("linear_30_x"),
            Highlight("Screw*", 0.65, assembled=True, feature="joint-countersink"),
        ),
    ),
    Figure(
        view="linear_32_x_exploded_cam0",
        name="x_rail_fasteners",
        highlights=(Highlight("Screw_BHCS_M3*"), Highlight("TNut_hammer_M3")),
    ),
    Figure(
        view="linear_33_x_exploded_cam1",
        name="x_rail",
        highlights=(Highlight("linear_32_x"),),
    ),
    Figure(
        view="linear_41_idler_lj1_assembled_cam0",
        name="idler_lj1_stack",
        highlights=(Highlight("linear_40_idler_lj1", 0.65),),
    ),
    Figure(
        view="linear_41_idler_lj1_exploded_cam1",
        name="idler_lj1_seat",
        highlights=(
            Highlight("linear_40_idler_lj1"),
            Highlight("Nut"),
            Highlight(
                "linear_40_idler_lj1", 0.65, assembled=True, feature="stack-seat"
            ),
            Highlight("**/xy_joint_right", 0.65, feature="front-pocket-right"),
        ),
        crop=(-15.6508, -85.8283, 108.8771, 162.7095),
    ),
    Figure(
        view="linear_43_idler_lj2_assembled_cam0",
        name="idler_lj2_stack",
        highlights=(Highlight("linear_42_idler_lj2", 0.65),),
    ),
    Figure(
        view="linear_43_idler_lj2_exploded_cam1",
        name="idler_lj2_seat",
        highlights=(
            Highlight("linear_42_idler_lj2"),
            Highlight("Nut"),
            Highlight(
                "linear_42_idler_lj2", 0.65, assembled=True, feature="stack-seat"
            ),
            Highlight("**/xy_joint_right", 0.65, feature="slant-pocket-right"),
        ),
        crop=(-104.0665, -91.3037, 86.994, 109.7284),
    ),
    Figure(
        view="linear_45_idler_rj1_assembled_cam0",
        name="idler_rj1_stack",
        highlights=(Highlight("linear_44_idler_rj1", 0.65),),
    ),
    Figure(
        view="linear_45_idler_rj1_exploded_cam1",
        name="idler_rj1_seat",
        highlights=(
            Highlight("linear_44_idler_rj1"),
            Highlight("Nut"),
            Highlight(
                "linear_44_idler_rj1", 0.65, assembled=True, feature="stack-seat"
            ),
            Highlight("**/xy_joint_left", 0.65, feature="front-pocket"),
        ),
        crop=(-78.2327, -86.1946, 89.3284, 146.1519),
    ),
    Figure(
        view="linear_47_idler_rj2_assembled_cam0",
        name="idler_rj2_stack",
        highlights=(Highlight("linear_46_idler_rj2", 0.65),),
    ),
    Figure(
        view="linear_47_idler_rj2_exploded_cam1",
        name="idler_rj2_seat",
        highlights=(
            Highlight("linear_46_idler_rj2"),
            Highlight("Nut"),
            Highlight(
                "linear_46_idler_rj2", 0.65, assembled=True, feature="stack-seat"
            ),
            Highlight("**/xy_joint_left", 0.65, feature="slant-pocket"),
        ),
        crop=(27.5776, -83.344, 81.1298, 114.2989),
    ),
    Figure(
        view="motor_11_frame_assembled_cam0",
        name="motor_a_bracket",
        highlights=(Highlight("motor_10_bracket", 0.65),),
    ),
    Figure(
        view="motor_11_frame_exploded_cam1",
        name="motor_a_bracket_mount",
        highlights=(
            Highlight("motor_10_bracket"),
            Highlight(
                "motor_10_bracket/Screw_BHCS_M5*",
                0.65,
                assembled=True,
                feature="frame-seat",
            ),
        ),
        crop=(-65.2693, 4.2459, 110.1314, 136.4075),
    ),
    Figure(
        view="motor_21_frame_assembled_cam0",
        name="motor_b_bracket",
        highlights=(Highlight("motor_20_bracket", 0.65),),
    ),
    Figure(
        view="motor_21_frame_exploded_cam1",
        name="motor_b_bracket_mount",
        highlights=(
            Highlight("motor_20_bracket"),
            Highlight(
                "motor_20_bracket/Screw_BHCS_M5*",
                0.65,
                assembled=True,
                feature="frame-seat",
            ),
        ),
        crop=(-152.3696, -44.3261, 97.6319, 147.481),
    ),
    Figure(
        view="motor_30_pulley_assembled_cam0",
        name="pulley",
        highlights=(Highlight("pulley", 0.65),),
    ),
    Figure(
        view="motor_30_pulley_exploded_cam1",
        name="pulley_and_shaft",
        highlights=(
            Highlight("pulley"),
            Highlight(
                "motor_21_frame/motor_20_bracket/Nema17Motor",
                0.35,
                RED,
                feature="shaft",
            ),
        ),
        crop=(-145.962, -65.4471, 102.0888, 154.0559),
    ),
    Figure(
        view="tapz_10_solenoid_tip_assembled_cam1",
        name="solenoid_wiring",
        crop=(-20.4167, -40.0012, 35.301, 79.1986),
        callout="solenoid-wiring",
    ),
    Figure(
        view="tapz_10_solenoid_tip_exploded_cam1",
        name="solenoid_tip",
        highlights=(Highlight("_layer_ghost", 0.15), Highlight("_layer_solid/Tip")),
    ),
    Figure(
        view="tapz_11_solenoid_attach_exploded_cam1",
        name="solenoid_fasteners",
        highlights=(
            Highlight("Screw*"),
            Highlight("Screw*", 0.65, feature="mount-seat"),
            Highlight("tapz_10_solenoid_tip/Solenoid", 0.65, feature="mount-holes"),
        ),
        callout="mount-wiring",
    ),
    Figure(
        view="tapz_20_solenoid_mount_assembled_cam0",
        name="solenoid_mount",
        highlights=(Highlight("tapz_11_solenoid_attach", 0.65),),
    ),
    Figure(
        view="tapz_20_solenoid_mount_exploded_cam1",
        name="mount_fasteners",
        highlights=(
            Highlight("Screw*"),
            Highlight("Nut", occluded_by="tapz_11_solenoid_attach/SolenoidMount"),
            Highlight("Screw_BHCS*", 0.65, feature="plate-bore"),
            Highlight("Screw_FHCS*", 0.65, feature="exploded-countersink"),
        ),
        crop=(-69.0927, -108.1148, 115.408, 156.5674),
        callout="clamp-pockets",
    ),
    Figure(
        view="wire_10_tmc2209_assembled_cam0",
        name="driver_carrier",
        highlights=(Highlight("Tmc2209/carrier", 0.65),),
    ),
    Figure(
        view="wire_10_tmc2209_assembled_cam1",
        name="board_connections",
        highlights=(
            Highlight("MksBoard/spindle"),
            Highlight("MksBoard/X-Motor", occluded_by="Tmc2209|MksBoard/driver_slot"),
            Highlight("MksBoard/Y1-Motor", occluded_by="Tmc2209|MksBoard/driver_slot"),
            Highlight("MksBoard/dc_input"),
            Highlight("MksBoard/usb_pc"),
        ),
    ),
    Figure(
        view="wire_10_tmc2209_exploded_cam1",
        name="driver_slots",
        highlights=(
            Highlight("MksBoard/driver_slot[0]", 0.35, RED, feature="top-face"),
            Highlight("MksBoard/driver_slot[2]", 0.35, RED, feature="top-face"),
            Highlight("MksBoard/driver_slot[1]", feature="top-face"),
            Highlight("MksBoard/driver_slot[3]", feature="top-face"),
            Highlight("Tmc2209"),
        ),
    ),
)


def figures_for(stem: str, exploded: bool | None = None) -> dict[str, Figure]:
    """Figure declarations for one procedure, optionally limited to one variant."""
    variants = (
        ("exploded", "assembled")
        if exploded is None
        else ("exploded" if exploded else "assembled",)
    )
    prefixes = tuple(f"{stem}_{variant}_cam" for variant in variants)
    return {
        filename: figure
        for filename, figure in FIGURES.items()
        if figure.view.startswith(prefixes)
    }


def figure_names(stem: str) -> set[str]:
    """Final annotated SVG filenames for one procedure."""
    return set(figures_for(stem))
