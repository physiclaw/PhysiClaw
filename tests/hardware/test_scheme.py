"""Tests for hardware/scheme.py — the pipeline's shared naming scheme."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hardware import scheme

# Stems drawn from the real convention's alphabet (lowercase family, digits,
# underscores) — the space the filename builders and regexes must agree on.
stems = st.from_regex(r"[a-z]+_[0-9]+_[a-z][a-z0-9_]*", fullmatch=True)


# ── constants ─────────────────────────────────────────────────────────────────


def test_family_priority_indexes_families_in_declaration_order():
    assert [scheme.FAMILIES[i] for i in scheme.FAMILY_PRIORITY.values()] == list(
        scheme.FAMILY_PRIORITY
    )


def test_variants_are_exploded_and_assembled():
    assert set(scheme.VARIANTS) == {"exploded", "assembled"}


# ── stem convention ───────────────────────────────────────────────────────────


def test_stem_re_parses_family_nn_descriptor():
    m = scheme.STEM_RE.match("belt_20_clamp")

    assert (m["family"], m["nn"], m["descriptor"]) == ("belt", "20", "clamp")


@pytest.mark.parametrize(
    "stem",
    ["Belt_20_clamp", "belt20_clamp", "belt_x_clamp", "belt_20", "20_belt_clamp"],
)
def test_stem_re_rejects_malformed_stem(stem):
    assert scheme.STEM_RE.match(stem) is None


def test_family_of_returns_leading_segment():
    assert scheme.family_of("belt_20_clamp") == "belt"


# ── filename builders ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exploded", "suffix"), [(True, "_exploded"), (False, "_assembled")]
)
def test_variant_suffix_maps_flag_to_tag(exploded, suffix):
    assert scheme.variant_suffix(exploded) == suffix


def test_step_path_for_names_variant_step_in_step_dir():
    path = scheme.step_path_for("belt_20_clamp", exploded=True)

    assert path == scheme.STEP_DIR / "belt_20_clamp_exploded.step"


def test_svg_path_for_defaults_to_cam0():
    path = scheme.svg_path_for("belt_20_clamp", exploded=False)

    assert path == scheme.SVG_DIR / "belt_20_clamp_assembled_cam0.svg"


def test_svg_path_for_with_index_names_that_camera():
    path = scheme.svg_path_for("belt_20_clamp", exploded=True, index=2)

    assert path == scheme.SVG_DIR / "belt_20_clamp_exploded_cam2.svg"


# ── classifying regexes ───────────────────────────────────────────────────────


def test_raw_svg_re_rejects_annotation_name():
    assert not scheme.raw_svg_re("belt_20_clamp").match(
        "belt_20_clamp_exploded_cam0_annotated_belt_route.svg"
    )


def test_raw_svg_re_rejects_other_stem_with_shared_prefix():
    assert not scheme.raw_svg_re("belt_20_clamp").match(
        "belt_20_clamp_show_exploded_cam0.svg"
    )


def test_annotated_svg_re_rejects_raw_render_name():
    assert not scheme.annotated_svg_re("belt_20_clamp").match(
        "belt_20_clamp_exploded_cam0.svg"
    )


def test_annotated_svg_re_matches_named_annotation():
    assert scheme.annotated_svg_re("belt_20_clamp").match(
        "belt_20_clamp_assembled_cam1_annotated_clamp_fasteners.svg"
    )


@given(stem=stems, exploded=st.booleans(), index=st.integers(0, 9))
def test_raw_svg_re_matches_every_svg_path_for_name(stem, exploded, index):
    name = scheme.svg_path_for(stem, exploded, index=index).name

    assert scheme.raw_svg_re(stem).match(name)


@given(stem=stems, exploded=st.booleans(), index=st.integers(0, 9))
def test_annotated_svg_re_matches_named_annotation_of_every_render_name(
    stem, exploded, index
):
    raw = scheme.svg_path_for(stem, exploded, index=index).name

    annotation = raw.replace(".svg", "_annotated_clamp_fasteners.svg")

    assert scheme.annotated_svg_re(stem).match(annotation)


@pytest.mark.parametrize(
    "annotation", [None, "clamp_fasteners", "detail_exploded_cam2"]
)
def test_svg_name_re_parses_the_annotation_name(annotation):
    name = (
        "belt_20_clamp_assembled_cam1"
        + (f"_annotated_{annotation}" if annotation else "")
        + ".svg"
    )
    assert scheme.SVG_NAME_RE.fullmatch(name).groupdict() == {
        "stem": "belt_20_clamp",
        "variant": "assembled",
        "cam": "1",
        "annotation": annotation,
    }


@pytest.mark.parametrize("suffix", ["aupa", "annotated", "annotated_0", "annotated_1"])
def test_svg_names_reject_random_or_numbered_annotations(suffix):
    name = f"belt_20_clamp_assembled_cam1_{suffix}.svg"
    assert not scheme.SVG_NAME_RE.fullmatch(name)
    assert not scheme.annotated_svg_re("belt_20_clamp").fullmatch(name)
