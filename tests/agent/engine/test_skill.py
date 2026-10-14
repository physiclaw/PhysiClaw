"""Tests for `physiclaw.agent.engine.skill` — discovery + dispatch.

Discovery scans three roots: the built-in package root (`BUILTIN_SKILLS_DIR`,
flat `.md`), the official pack root (`OFFICIAL_SKILLS_DIR` =
paths.official_skills_dir()), and the user home root (`HOME_SKILLS_DIR` =
paths.skills_dir()), both folder `SKILL.md`. The autouse `_skill_roots`
fixture re-points all three at per-test tmp dirs. The cwd is NOT a discovery
root; the fixture still chdirs to an empty tmp for hygiene.

Accepted mutation survivors decorate diagnostic/prose strings, change the
default falsey flat flag, or are equivalent for the documented newline-delimited
frontmatter. Assertions cover dispatch, source grouping, path boundaries, and
workflow bodies rather than freezing explanatory prose.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from physiclaw.agent.engine import skill as skill_mod
from physiclaw.agent.engine.skill import (
    Skill,
    _load_reference,
    _split_frontmatter,
    discover,
    discover_builtin_skills,
    discover_user_skills,
    dispatch,
    render_builtin,
    render_section,
)


@pytest.fixture
def discovery_defaults() -> tuple[Path, Path, Path]:
    return (
        skill_mod.BUILTIN_SKILLS_DIR,
        skill_mod.OFFICIAL_SKILLS_DIR,
        skill_mod.HOME_SKILLS_DIR,
    )


@pytest.fixture(autouse=True)
def _skill_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, discovery_defaults
) -> tuple[Path, Path]:
    """Per-test builtin + official + home + cwd skill roots.

    Re-points all three real roots at empty tmp dirs so the shipped
    flat-.md skills and any synced official pack don't leak into
    discovery assertions. Returns (home, cwd); the official root is
    reached via ``skill_mod.OFFICIAL_SKILLS_DIR``.
    """
    home = tmp_path / "home_skills"
    official = tmp_path / "official_skills"
    cwd = tmp_path / "cwd_root"
    cwd.mkdir()
    builtin = tmp_path / "builtin_skills"
    builtin.mkdir()
    monkeypatch.setattr(skill_mod, "BUILTIN_SKILLS_DIR", builtin)
    monkeypatch.setattr(skill_mod, "OFFICIAL_SKILLS_DIR", official)
    monkeypatch.setattr(skill_mod, "HOME_SKILLS_DIR", home)
    monkeypatch.chdir(cwd)
    return home, cwd


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str = "do something",
    body: str = "Workflow steps here.",
    extra_files: dict[str, str] | None = None,
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n"
    )
    if extra_files:
        for relpath, content in extra_files.items():
            target = skill_dir / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    return skill_dir


# ---------- discover ----------


def test_discover_defaults_load_packaged_skills_and_empty_isolated_home(
    discovery_defaults, monkeypatch
) -> None:
    builtin, official, home = discovery_defaults
    monkeypatch.setattr(skill_mod, "BUILTIN_SKILLS_DIR", builtin)
    monkeypatch.setattr(skill_mod, "OFFICIAL_SKILLS_DIR", official)
    monkeypatch.setattr(skill_mod, "HOME_SKILLS_DIR", home)

    builtins = discover_builtin_skills()
    on_demand = discover_user_skills()

    assert builtins
    assert {sk.source for sk in builtins.values()} == {"built-in"}
    assert all(sk.flat for sk in builtins.values())
    assert on_demand == {}


def test_discover_alias_uses_first_available_numeric_suffix(_skill_roots) -> None:
    home, _ = _skill_roots
    _write_skill(skill_mod.OFFICIAL_SKILLS_DIR, "foo")
    _write_skill(skill_mod.OFFICIAL_SKILLS_DIR, "foo-user")
    _write_skill(home, "foo", body="user body")

    found = discover_user_skills()

    assert dispatch(found, {"name": "foo-user-2"}) == "user body"


def test_regression_generated_alias_preserves_existing_skill(_skill_roots) -> None:
    home, _ = _skill_roots
    _write_skill(skill_mod.OFFICIAL_SKILLS_DIR, "foo", body="official base")
    _write_skill(skill_mod.OFFICIAL_SKILLS_DIR, "foo-user", body="original suffix")
    _write_skill(home, "foo", body="user base")
    _write_skill(home, "foo-user-2", body="original numbered suffix")

    found = discover_user_skills()

    assert list(found) == ["foo-official", "foo-user", "foo-user-3", "foo-user-2"]
    assert {name: dispatch(found, {"name": name}) for name in found} == {
        "foo-official": "official base",
        "foo-user": "original suffix",
        "foo-user-3": "user base",
        "foo-user-2": "original numbered suffix",
    }
    assert {name: sk.name for name, sk in found.items()} == {
        name: name for name in found
    }


_COLLIDING_NAMES = st.lists(
    st.sampled_from(
        ["foo", "foo-user", "foo-official", "foo-user-2", "foo-official-2", "bar"]
    ),
    unique=True,
    max_size=6,
)


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(official_names=_COLLIDING_NAMES, user_names=_COLLIDING_NAMES)
def test_discover_preserves_each_origin_and_stable_names(
    tmp_path, monkeypatch, official_names, user_names
) -> None:
    with TemporaryDirectory(dir=tmp_path) as workspace:
        official = Path(workspace) / "official"
        user = Path(workspace) / "user"
        monkeypatch.setattr(skill_mod, "OFFICIAL_SKILLS_DIR", official)
        monkeypatch.setattr(skill_mod, "HOME_SKILLS_DIR", user)
        official_dirs = {_write_skill(official, name) for name in official_names}
        user_dirs = {_write_skill(user, name) for name in user_names}

        found = discover_user_skills()

        assert len(found) == len(official_names) + len(user_names)
        assert {sk.dir for sk in found.values()} == official_dirs | user_dirs
        assert {(sk.dir, sk.source) for sk in found.values()} == {
            (directory, "official") for directory in official_dirs
        } | {(directory, "user") for directory in user_dirs}
        assert {name: sk.name for name, sk in found.items()} == {
            name: name for name in found
        }
        assert set(official_names) ^ set(user_names) <= found.keys()
        assert found == discover_user_skills()


@pytest.mark.parametrize("flat", [False, True])
def test_regression_invalid_utf8_does_not_hide_healthy_skills(
    _skill_roots, flat
) -> None:
    home, _ = _skill_roots
    bad_dir = _write_skill(home, "bad")
    _write_skill(home, "healthy", body="healthy body")
    (skill_mod.BUILTIN_SKILLS_DIR / "bad.md").write_bytes(b"\xff")
    (bad_dir / "SKILL.md").write_bytes(b"\xff")
    (skill_mod.BUILTIN_SKILLS_DIR / "healthy.md").write_text("healthy body")
    discover_skills = {False: discover_user_skills, True: discover_builtin_skills}[flat]

    found = discover_skills()

    assert list(found) == ["healthy"]
    assert found["healthy"].body == "healthy body"


@pytest.mark.parametrize("flat", [False, True])
def test_regression_skill_file_symlink_cannot_escape_root(
    tmp_path, _skill_roots, flat
) -> None:
    home, _ = _skill_roots
    folder = _write_skill(home, "escape")
    outside = tmp_path / "outside.md"
    outside.write_text("outside contents")
    (folder / "SKILL.md").unlink()
    (folder / "SKILL.md").symlink_to(outside)
    (skill_mod.BUILTIN_SKILLS_DIR / "escape.md").symlink_to(outside)
    discover_skills = {False: discover_user_skills, True: discover_builtin_skills}[flat]

    found = discover_skills()

    assert found == {}


@pytest.mark.parametrize("flat", [False, True])
def test_discover_accepts_skill_file_symlink_inside_root(_skill_roots, flat) -> None:
    home, _ = _skill_roots
    folder = _write_skill(home, "linked")
    (folder / "SKILL.md").rename(folder / "body.txt")
    (folder / "SKILL.md").symlink_to(folder / "body.txt")
    (skill_mod.BUILTIN_SKILLS_DIR / "body.txt").write_text("built-in body")
    (skill_mod.BUILTIN_SKILLS_DIR / "linked.md").symlink_to(
        skill_mod.BUILTIN_SKILLS_DIR / "body.txt"
    )
    discover_skills = {False: discover_user_skills, True: discover_builtin_skills}[flat]

    found = discover_skills()

    assert list(found) == ["linked"]
    assert (
        found["linked"].body
        == {False: "Workflow steps here.", True: "built-in body"}[flat]
    )


@pytest.mark.parametrize(
    "name", ["/tmp/outside", "../escape", "a/b", r"a\b", "C:drive", ".", ".."]
)
def test_discover_rejects_unsafe_frontmatter_name(_skill_roots, name) -> None:
    home, _ = _skill_roots
    folder = _write_skill(home, "normal")
    (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody")
    (skill_mod.BUILTIN_SKILLS_DIR / "normal.md").write_text(
        f"---\nname: {name}\n---\nbody"
    )

    found = discover()

    assert found == {}


def test_discover_accepts_installer_name_characters(_skill_roots) -> None:
    home, _ = _skill_roots
    _write_skill(home, "A0-b_c.d")

    found = discover_user_skills()

    assert list(found) == ["A0-b_c.d"]


def test_discover_ignores_private_paths_even_with_public_frontmatter(
    _skill_roots,
) -> None:
    home, _ = _skill_roots
    hidden = _write_skill(home, "_hidden")
    (hidden / "SKILL.md").write_text("---\nname: public\n---\nhidden body")
    (skill_mod.BUILTIN_SKILLS_DIR / "_hidden.md").write_text(
        "---\nname: public\n---\nhidden body"
    )
    _write_skill(home, "healthy")
    (skill_mod.BUILTIN_SKILLS_DIR / "healthy-flat.md").write_text("body")

    found = discover()

    assert list(found) == ["healthy-flat", "healthy"]


@pytest.mark.parametrize(
    "text", ["X body", "---\n\n---\nX body", "---\nname: plain\n---\nX body"]
)
def test_discover_optional_metadata_preserves_body(_skill_roots, text) -> None:
    home, _ = _skill_roots
    folder = _write_skill(home, "plain")
    (folder / "SKILL.md").write_text(text)

    found = discover_user_skills()

    assert found["plain"].description == ""
    assert dispatch(found, {"name": "plain"}) == "X body"


def test_render_section_orders_sources_and_keeps_one_bullet_per_skill() -> None:
    skills = {
        "shared": Skill(
            name="shared", description="", body="", dir=Path("."), source="community"
        ),
        "alpha": Skill(name="alpha", description="first", body="", dir=Path(".")),
        "beta": Skill(name="beta", description="second", body="", dir=Path(".")),
    }

    rendered = render_section(skills)

    assert rendered.split("\n\n") == [
        "## Available skills",
        "### user skills",
        "- **alpha** — first\n- **beta** — second",
        "### community skills",
        "- **shared** — (no description)",
    ]


def test_render_builtin_separates_header_and_workflow_body() -> None:
    skills = {
        "workflow": Skill(
            name="workflow", description="", body="Exact steps", dir=Path(".")
        )
    }

    rendered = render_builtin(skills)

    assert rendered.startswith("## Built-in Skills\n\n")
    assert rendered.endswith("\n\n### workflow\n\nExact steps")


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("read failed")])
def test_discover_read_error_does_not_hide_healthy_skills(
    _skill_roots, monkeypatch, error
) -> None:
    home, _ = _skill_roots
    _write_skill(home, "bad")
    _write_skill(home, "healthy")
    read_text = skill_mod.read_text

    def read_or_fail(path):
        if path.parent.name == "bad":
            raise error
        return read_text(path)

    monkeypatch.setattr(skill_mod, "read_text", read_or_fail)

    found = discover_user_skills()

    assert list(found) == ["healthy"]


def test_discover_stat_error_does_not_hide_healthy_skills(
    _skill_roots, monkeypatch
) -> None:
    home, _ = _skill_roots
    bad = _write_skill(home, "bad") / "SKILL.md"
    _write_skill(home, "healthy")
    bad_flat = skill_mod.BUILTIN_SKILLS_DIR / "bad.md"
    bad_flat.write_text("body")
    (skill_mod.BUILTIN_SKILLS_DIR / "healthy-flat.md").write_text("body")
    is_file = Path.is_file

    def is_file_or_deny(path):
        if path in {bad, bad_flat}:
            raise PermissionError("cannot stat skill file")
        return is_file(path)

    monkeypatch.setattr(Path, "is_file", is_file_or_deny)

    found = discover()

    assert list(found) == ["healthy-flat", "healthy"]


def test_discover_rejects_outside_directory_even_with_inside_skill_file(
    tmp_path, _skill_roots
) -> None:
    home, _ = _skill_roots
    home.mkdir()
    (home / "body.txt").write_text("body")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").symlink_to(home / "body.txt")
    (home / "escape").symlink_to(outside, target_is_directory=True)

    found = discover_user_skills()

    assert found == {}


def test_discover_inside_directory_symlink_keeps_references_scoped(
    _skill_roots,
) -> None:
    home, _ = _skill_roots
    target = _write_skill(
        home, "_storage", extra_files={"references/guide.txt": "guide"}
    )
    (target / "SKILL.md").write_text("body without frontmatter")
    (home / "linked").symlink_to(target, target_is_directory=True)

    found = discover_user_skills()
    guide = dispatch(found, {"name": "linked", "reference": "guide.txt"})

    assert list(found) == ["linked"]
    assert found["linked"].dir == target.resolve()
    assert guide == "guide"


def test_discover_ignores_non_directory_roots(_skill_roots) -> None:
    home, _ = _skill_roots
    home.write_text("not a directory")
    skill_mod.OFFICIAL_SKILLS_DIR.write_text("not a directory")
    skill_mod.BUILTIN_SKILLS_DIR.rmdir()
    skill_mod.BUILTIN_SKILLS_DIR.write_text("not a directory")

    found = discover()

    assert found == {}


def test_discover_skips_directory_named_like_flat_file() -> None:
    (skill_mod.BUILTIN_SKILLS_DIR / "directory.md").mkdir()

    found = discover_builtin_skills()

    assert found == {}


def test_render_builtin_retains_empty_skill_heading() -> None:
    skills = {"empty": Skill(name="empty", description="", body="", dir=Path("."))}

    rendered = render_builtin(skills)

    assert rendered.endswith("### empty")


def test_discover_empty_when_no_roots_exist() -> None:
    assert discover() == {}


def test_discover_ignores_cwd_skills_dir(_skill_roots) -> None:
    # The cwd is not a discovery root — a ./skills there must be ignored.
    home, cwd = _skill_roots
    _write_skill(cwd / "skills", "foo")

    assert discover() == {}


def test_discover_finds_skill_in_home_skills_root(_skill_roots) -> None:
    home, _ = _skill_roots
    _write_skill(home, "bar")

    out = discover()

    assert "bar" in out


def test_discover_skips_directories_starting_with_underscore(
    _skill_roots,
) -> None:
    home, _ = _skill_roots
    _write_skill(home, "_internal")

    assert discover() == {}


def test_discover_skips_skills_without_skill_md(_skill_roots) -> None:
    home, _ = _skill_roots
    skill_dir = home / "no-md"
    skill_dir.mkdir(parents=True)
    (skill_dir / "README.md").write_text("not a skill")

    assert discover() == {}


def test_discover_skips_non_directory_entries(_skill_roots) -> None:
    home, _ = _skill_roots
    home.mkdir()
    (home / "stray.txt").write_text("not a dir")

    assert discover() == {}


def test_discover_uses_directory_name_when_frontmatter_lacks_name(
    _skill_roots,
) -> None:
    home, _ = _skill_roots
    skill_dir = home / "no-name"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\ndescription: only desc\n---\nbody\n")

    out = discover()

    assert "no-name" in out


def test_discover_rejects_symlink_resolving_outside_root(
    tmp_path: Path,
    _skill_roots,
) -> None:
    home, _ = _skill_roots
    home.mkdir()
    # Create a real skill outside the home root, then symlink into it.
    outside = tmp_path / "outside"
    _write_skill(outside, "evil")
    symlink_path = home / "evil-link"
    symlink_path.symlink_to(outside / "evil")

    out = discover()

    assert "evil" not in out


def test_discover_strips_frontmatter_whitespace_from_description_and_body(
    _skill_roots,
) -> None:
    home, _ = _skill_roots
    skill_dir = home / "trimmed"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: trimmed\ndescription: padded around\n---\n\n  body\n"
    )

    out = discover()

    assert out["trimmed"].description == "padded around"
    assert out["trimmed"].body == "body"


# ---------- built-in flat-.md discovery ----------


def _write_flat(
    name: str, *, description: str = "flat desc", body: str = "flat body"
) -> None:
    """Write a built-in-style flat `<name>.md` into the (monkeypatched) root."""
    root = skill_mod.BUILTIN_SKILLS_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n"
    )


def test_discover_finds_builtin_flat_md_skill() -> None:
    _write_flat("im", description="messaging", body="im workflow")

    out = discover()

    assert out["im"].description == "messaging"
    assert out["im"].body == "im workflow"
    assert out["im"].flat is True


def test_load_reference_rejects_flat_skill() -> None:
    sk = Skill(name="im", description="d", body="b", dir=Path("/tmp"), flat=True)

    with pytest.raises(ValueError, match=r"built-in and self-contained"):
        _load_reference(sk, "spec.md")


def test_discover_flat_uses_filename_stem_when_name_missing() -> None:
    (skill_mod.BUILTIN_SKILLS_DIR / "open-app.md").write_text(
        "---\ndescription: launch apps\n---\nbody\n"
    )

    assert "open-app" in discover()


def test_discover_flat_skips_underscore_prefixed_files() -> None:
    (skill_mod.BUILTIN_SKILLS_DIR / "_draft.md").write_text(
        "---\nname: _draft\ndescription: wip\n---\nbody\n"
    )

    assert discover() == {}


def test_discover_home_skill_overrides_builtin(_skill_roots) -> None:
    # Built-in is the lowest-precedence root; a home skill of the same name wins.
    home, _ = _skill_roots
    _write_flat("im", description="built-in", body="built-in-body")
    _write_skill(home, "im", description="user", body="user-body")

    out = discover()

    assert out["im"].description == "user"
    assert out["im"].body == "user-body"


# ---------- discover_builtin_skills / discover_user_skills ----------


def test_discover_builtin_only_scans_builtin_root(_skill_roots) -> None:
    home, _ = _skill_roots
    _write_flat("im", description="messaging")
    _write_skill(home, "taobao")

    out = discover_builtin_skills()

    assert set(out) == {"im"}
    assert out["im"].flat is True


def test_discover_user_only_scans_home_root(_skill_roots) -> None:
    home, _ = _skill_roots
    _write_flat("im")
    _write_skill(home, "taobao")

    out = discover_user_skills()

    assert set(out) == {"taobao"}
    assert out["taobao"].flat is False


# ---------- official skills: on-demand set, no shadowing ----------


def test_discover_user_includes_official(_skill_roots) -> None:
    home, _ = _skill_roots
    _write_skill(skill_mod.OFFICIAL_SKILLS_DIR, "jd", description="official jd")
    _write_skill(home, "taobao", description="user taobao")

    out = discover_user_skills()

    assert set(out) == {"jd", "taobao"}
    assert out["jd"].description == "official jd"


def test_official_and_user_clash_disambiguated(_skill_roots) -> None:
    # Same name in both roots → both kept, suffixed; neither shadows the other.
    home, _ = _skill_roots
    _write_skill(
        skill_mod.OFFICIAL_SKILLS_DIR, "jd", description="official", body="off-body"
    )
    _write_skill(home, "jd", description="user", body="user-body")

    out = discover_user_skills()

    assert set(out) == {"jd-official", "jd-user"}
    # Key AND Skill.name are both suffixed, so index bullet + dispatch agree.
    assert out["jd-official"].name == "jd-official"
    assert out["jd-official"].body == "off-body"
    assert out["jd-user"].name == "jd-user"
    assert out["jd-user"].body == "user-body"
    # Dispatch routes by the disambiguated key.
    assert dispatch(out, {"name": "jd-official"}) == "off-body"


def test_no_clash_keeps_plain_names(_skill_roots) -> None:
    home, _ = _skill_roots
    _write_skill(skill_mod.OFFICIAL_SKILLS_DIR, "jd")
    _write_skill(home, "taobao")

    out = discover_user_skills()

    assert set(out) == {"jd", "taobao"}
    assert out["jd"].name == "jd"  # no clash → plain name, no suffix


def test_builtin_always_on_alongside_clashing_on_demand(_skill_roots) -> None:
    # A built-in is inlined independently; official/user disambiguation never
    # touches it. discover() (merged Claude view) still surfaces all three.
    home, _ = _skill_roots
    _write_flat("im", description="built-in im")
    _write_skill(skill_mod.OFFICIAL_SKILLS_DIR, "jd", description="official jd")
    _write_skill(home, "jd", description="user jd")

    assert set(discover_builtin_skills()) == {"im"}  # built-in unaffected
    merged = discover()
    assert set(merged) == {"im", "jd-official", "jd-user"}


# ---------- render_builtin ----------


def test_render_builtin_empty_when_no_skills() -> None:
    assert render_builtin({}) == ""


def test_render_builtin_inlines_full_bodies() -> None:
    skills = {
        "im": Skill(
            name="im", description="d", body="IM WORKFLOW", dir=Path("/"), flat=True
        ),
    }

    out = render_builtin(skills)

    assert out.startswith("## Built-in Skills")
    assert "### im" in out
    assert "IM WORKFLOW" in out  # full body, not just metadata


# ---------- Skill dataclass ----------


def test_skill_dataclass_holds_required_fields() -> None:
    s = Skill(name="x", description="y", body="z", dir=Path("/tmp"))

    assert s.name == "x"
    assert s.description == "y"
    assert s.body == "z"
    assert s.dir == Path("/tmp")


# ---------- dispatch ----------


def test_dispatch_returns_body_for_known_skill(_skill_roots) -> None:
    home, _ = _skill_roots
    _write_skill(home, "foo", body="step 1\nstep 2")

    out = dispatch(discover(), {"name": "foo"})

    assert out == "step 1\nstep 2"


def test_dispatch_raises_on_missing_name_arg() -> None:
    with pytest.raises(ValueError, match=r"^Skill call requires a 'name' argument$"):
        dispatch({}, {})


def test_dispatch_raises_on_empty_name_arg() -> None:
    with pytest.raises(ValueError, match=r"^Skill call requires a 'name'"):
        dispatch({}, {"name": "   "})


def test_dispatch_raises_on_unknown_skill_name(_skill_roots) -> None:
    home, _ = _skill_roots
    _write_skill(home, "alpha")
    _write_skill(home, "beta")

    with pytest.raises(
        ValueError,
        match=r"^unknown skill 'mystery'\. Available: alpha, beta$",
    ):
        dispatch(discover(), {"name": "mystery"})


def test_dispatch_lists_none_when_no_skills_available() -> None:
    with pytest.raises(ValueError, match=r"^unknown skill 'x'\. Available: \(none\)$"):
        dispatch({}, {"name": "x"})


def test_dispatch_redirects_builtin_name_to_inline_section() -> None:
    """Wake triggers name built-in skills (e.g. `screen-layout`), and
    models take the bait and Skill()-load them. The error must redirect
    to the inline `## Built-in Skills` section instead of the generic
    unknown-skill message — the generic one derailed a real first run
    (two burned turns + a corrective before the model recovered)."""
    _write_flat("screen-layout", description="first-run capture")

    with pytest.raises(
        ValueError,
        match=r"built-in skill.*## Built-in Skills",
    ):
        dispatch({}, {"name": "screen-layout"})


def test_dispatch_folder_skill_shadows_builtin_redirect(_skill_roots) -> None:
    """A folder skill that happens to share a built-in's name still
    serves its body — the redirect only fires on a registry miss."""
    home, _ = _skill_roots
    _write_flat("screen-layout", description="first-run capture")
    _write_skill(home, "screen-layout", body="folder body")

    out = dispatch(discover_user_skills(), {"name": "screen-layout"})

    assert out == "folder body"


def test_dispatch_loads_reference_when_reference_arg_present(
    _skill_roots,
) -> None:
    home, _ = _skill_roots
    _write_skill(
        home,
        "foo",
        extra_files={"references/spec.md": "spec contents here"},
    )

    out = dispatch(discover(), {"name": "foo", "reference": "spec.md"})

    assert out == "spec contents here"


# ---------- _load_reference ----------


def test_load_reference_returns_file_contents(_skill_roots) -> None:
    home, _ = _skill_roots
    _write_skill(
        home,
        "foo",
        extra_files={"references/note.md": "hi"},
    )
    skills = discover()

    assert _load_reference(skills["foo"], "note.md") == "hi"


def test_load_reference_raises_on_path_traversal(_skill_roots, tmp_path: Path) -> None:
    home, _ = _skill_roots
    _write_skill(home, "foo")
    # Place an "escape" file outside the references dir.
    (home / "secret.md").write_text("don't read me")
    skills = discover()

    with pytest.raises(
        ValueError,
        match=r"^reference path '\.\./secret\.md' escapes skill directory$",
    ):
        _load_reference(skills["foo"], "../secret.md")


def test_load_reference_raises_when_target_missing(_skill_roots) -> None:
    home, _ = _skill_roots
    _write_skill(home, "foo")
    skills = discover()

    with pytest.raises(
        FileNotFoundError,
        match=r"^reference 'gone\.md' not found in skill 'foo'$",
    ):
        _load_reference(skills["foo"], "gone.md")


# ---------- _split_frontmatter ----------


def test_split_frontmatter_returns_empty_when_no_leading_dashes() -> None:
    fm, body = _split_frontmatter("hello world\n")

    assert fm == {}
    assert body == "hello world\n"


def test_split_frontmatter_returns_empty_when_end_marker_missing() -> None:
    fm, body = _split_frontmatter("---\nname: foo\nno end here\n")

    assert fm == {}


def test_split_frontmatter_parses_scalar_keys() -> None:
    fm, body = _split_frontmatter(
        "---\nname: foo\ndescription: do thing\n---\nbody text\n"
    )

    assert fm == {"name": "foo", "description": "do thing"}
    assert body == "body text\n"


def test_split_frontmatter_skips_lines_without_colon() -> None:
    fm, _ = _split_frontmatter(
        "---\nname: foo\nrandom-no-colon\ndescription: x\n---\nbody\n"
    )

    assert fm == {"name": "foo", "description": "x"}


def test_split_frontmatter_handles_value_containing_colons() -> None:
    fm, _ = _split_frontmatter("---\nurl: https://example.com:443\n---\nbody\n")

    assert fm == {"url": "https://example.com:443"}


def test_split_frontmatter_strips_leading_newlines_from_body() -> None:
    fm, body = _split_frontmatter("---\nname: foo\n---\n\n\nbody\n")

    assert body == "body\n"


# ---------- render_section ----------


def test_render_section_empty_when_no_skills() -> None:
    assert render_section({}) == ""


def test_render_section_lists_each_skill_as_bullet() -> None:
    skills = {
        "foo": Skill(
            name="foo", description="do foo", body="SECRET STEPS", dir=Path("/")
        ),
        "bar": Skill(name="bar", description="do bar", body="", dir=Path("/")),
    }

    out = render_section(skills)

    assert out.startswith("## Available skills\n")
    assert "- **foo** — do foo" in out
    assert "- **bar** — do bar" in out
    assert "SECRET STEPS" not in out  # index only — body loads on demand


def test_render_section_uses_no_description_placeholder_when_empty() -> None:
    skills = {
        "foo": Skill(name="foo", description="", body="", dir=Path("/")),
    }

    assert "- **foo** — (no description)" in render_section(skills)


def test_render_section_splits_official_and_user_subsections() -> None:
    skills = {
        "jd": Skill(
            name="jd", description="off", body="", dir=Path("/"), source="official"
        ),
        "taobao": Skill(
            name="taobao", description="usr", body="", dir=Path("/"), source="user"
        ),
    }

    out = render_section(skills)

    assert "## Available skills" in out
    assert "### official skills" in out and "### user skills" in out
    # official subsection precedes user, and each skill sits under its heading.
    assert out.index("### official skills") < out.index("### user skills")
    assert "- **jd** — off" in out
    assert "- **taobao** — usr" in out


def test_render_section_orders_builtin_official_user() -> None:
    # The Claude merged view can carry all three sources at once.
    skills = {
        "im": Skill(
            name="im", description="b", body="", dir=Path("/"), source="built-in"
        ),
        "jd": Skill(
            name="jd", description="o", body="", dir=Path("/"), source="official"
        ),
        "taobao": Skill(
            name="taobao", description="u", body="", dir=Path("/"), source="user"
        ),
    }

    out = render_section(skills)

    assert (
        out.index("### built-in skills")
        < out.index("### official skills")
        < out.index("### user skills")
    )


# ---------- dispatch (user skills only) ----------


def _sk(name: str, body: str = "body") -> Skill:
    return Skill(name=name, description=f"{name} desc", body=body, dir=Path("/x"))


def test_dispatch_resolves_by_exact_name() -> None:
    m = {"jd": _sk("jd", "BODY")}
    assert dispatch(m, {"name": "jd"}) == "BODY"
    with pytest.raises(ValueError, match="unknown skill"):
        dispatch(m, {"name": "nope"})
