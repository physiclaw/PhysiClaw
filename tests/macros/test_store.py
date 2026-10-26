"""Tests for `physiclaw.macros.store` — discovery under
``~/.physiclaw/macros/`` and the `## Available Macros` prompt section.

All paths derive from `paths.HOME`, repointed per test by the autouse
`physiclaw_home` fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from symlink_helpers import symlink_to

from physiclaw.common import paths
from physiclaw.common.config import CONFIG
from physiclaw.macros import store as store_mod
from physiclaw.macros.model import MacroError
from physiclaw.macros.scaffold import README_CONTENT
from physiclaw.macros.store import (
    discover_enabled,
    ensure_format_readme,
    init_macro,
    list_names,
    render_section,
    scan,
)

VALID = """
kind: macro
schema: 1
name: {name}
description: Demo macro
enabled: {enabled}

inputs:
  message:
    description: The message text
    example: hello
  greeting:
    description: Opening line
    default: hi

steps:
  - home_screen
  - send_to_clipboard: "{{message}}"
"""


def _write(name: str, text: str | None = None, *, enabled: bool = True) -> Path:
    paths.macros_dir().mkdir(parents=True, exist_ok=True)
    body = (
        text
        if text is not None
        else VALID.format(name=name, enabled=str(enabled).lower())
    )
    md = paths.macros_dir() / f"{name}.yml"
    md.write_text(body, encoding="utf-8")
    return md


# ---------- scan ----------


def test_scan_missing_root_returns_empty() -> None:
    assert scan() == []


def test_scan_default_search_path_preserves_explicit_placeholder_values() -> None:
    from physiclaw.common.placeholders import write_placeholder_values

    write_placeholder_values({"CONTACT": "Installed"})
    _write(
        "demo",
        VALID.format(name="demo", enabled="false").replace("{message}", "<<CONTACT>>"),
    )

    (entry,) = scan(values={"CONTACT": "Staged"})

    assert entry.spec is not None
    assert entry.spec.steps[1].args == {"text": "Staged"}


def test_scan_valid_macro_returns_spec() -> None:
    _write("demo")

    entries = scan()

    assert len(entries) == 1
    assert entries[0].spec is not None
    assert entries[0].spec.name == "demo"
    assert entries[0].error is None


def test_scan_invalid_yaml_keeps_reason() -> None:
    _write("broken", text="steps: [unclosed")

    entries = scan()

    assert entries[0].spec is None
    assert "invalid YAML" in (entries[0].error or "")


def test_scan_invalid_utf8_excludes_only_the_unreadable_macro() -> None:
    _write("broken").write_bytes(b"\xff\xfe")
    _write("demo")

    entries = scan()

    assert entries[0].name == "broken"
    assert entries[0].spec is None
    assert "decode" in (entries[0].error or "")
    assert entries[1].spec is not None
    assert entries[1].spec.name == "demo"


def test_scan_reads_only_yml_files() -> None:
    # A macro is a leaf file: a stray folder (the old layout, a note dir)
    # and non-yml files beside the macros are not macros and not errors.
    _write("demo")
    (paths.macros_dir() / "old-shape").mkdir()
    (paths.macros_dir() / "old-shape" / "MACRO.yml").write_text(
        "name: x\n", encoding="utf-8"
    )
    (paths.macros_dir() / "notes.txt").write_text("x", encoding="utf-8")

    entries = scan()

    assert [e.name for e in entries] == ["demo"]


def test_scan_skips_hidden_and_underscore_files() -> None:
    _write("demo")
    _write("_draft")
    _write(".hidden")

    entries = scan()

    assert [e.name for e in entries] == ["demo"]


def test_scan_ignores_stats_and_readme_at_root() -> None:
    _write("demo")
    (paths.macros_dir() / "stats.json").write_text("{}", encoding="utf-8")
    (paths.macros_dir() / "README.md").write_text("# Macros", encoding="utf-8")

    entries = scan()

    assert [e.name for e in entries] == ["demo"]


def test_scan_sorted_by_name() -> None:
    _write("zeta")
    _write("alpha")

    entries = scan()

    assert [e.name for e in entries] == ["alpha", "zeta"]


def test_scan_rejects_symlink_escaping_root(tmp_path: Path) -> None:
    outside = tmp_path / "escape.yml"
    outside.write_text(VALID.format(name="escape", enabled="true"), encoding="utf-8")
    paths.macros_dir().mkdir(parents=True)
    symlink_to(paths.macros_dir() / "escape.yml", outside)

    entries = scan()

    assert entries[0].spec is None
    assert "outside" in (entries[0].error or "")


# ---------- list_names ----------


def test_list_names_missing_root_returns_empty() -> None:
    assert list_names() == set()


def test_list_names_is_identity_not_validity() -> None:
    # The stats prune set: a file counts if it exists, valid or not — no
    # read, no parse.
    _write("demo")
    _write("broken", text="steps: [not yaml")
    (paths.macros_dir() / "empty").mkdir()
    _write("_draft")

    assert list_names() == {"demo", "broken"}


# ---------- init_macro / ensure_format_readme ----------


def test_init_macro_scaffold_parses_and_is_disabled() -> None:
    path = init_macro("my-macro")

    entries = scan()

    assert path.exists()
    assert entries[0].spec is not None  # the template passes check unedited
    assert entries[0].spec.enabled is False
    assert entries[0].spec.name == "my-macro"


def test_init_macro_rejects_existing_file() -> None:
    init_macro("my-macro")

    with pytest.raises(MacroError, match="already exists"):
        init_macro("my-macro")


def test_init_macro_rejects_bad_name() -> None:
    with pytest.raises(MacroError, match="lowercase"):
        init_macro("Bad_Name")


def test_init_macro_writes_the_format_readme() -> None:
    init_macro("my-macro")

    assert (paths.macros_dir() / "README.md").read_text(
        encoding="utf-8"
    ) == README_CONTENT


def test_ensure_format_readme_rewrites_stale_content() -> None:
    ensure_format_readme()
    (paths.macros_dir() / "README.md").write_text("old spec", encoding="utf-8")

    ensure_format_readme()

    assert (paths.macros_dir() / "README.md").read_text(
        encoding="utf-8"
    ) == README_CONTENT


# ---------- discover_enabled ----------


def test_discover_enabled_filters_disabled_and_invalid() -> None:
    _write("on", enabled=True)
    _write("off", enabled=False)
    _write("bad", text="steps: [not yaml")

    macros = discover_enabled()

    assert set(macros) == {"on"}


def test_discover_enabled_empty_when_config_gate_off(mocker) -> None:
    # [macros] enabled = false hides every macro from the agent, regardless
    # of the per-file flags — the fleet-wide switch.
    _write("on", enabled=True)
    mocker.patch.object(CONFIG.macros, "enabled", False)

    assert discover_enabled() == {}


def test_scan_ignores_the_config_gate(mocker) -> None:
    # Authoring keeps working while the fleet-wide switch is off — the CLI
    # (list/check/run) reads through scan(), never through the gate.
    _write("on", enabled=True)
    mocker.patch.object(CONFIG.macros, "enabled", False)

    assert scan()[0].spec is not None


# ---------- render_section ----------


def test_render_section_empty_when_no_macros() -> None:
    assert render_section({}) == ""


def test_render_section_lists_name_description_and_step_count() -> None:
    _write("demo")

    section = render_section(discover_enabled())

    assert section.startswith("## Available Macros")
    assert "- **demo** — Demo macro" in section
    # The handle list IS the count, and every entry is a valid `start_at`.
    assert "  - steps: idx1-home_screen, idx2-send_to_clipboard-message" in section


def test_render_section_lists_inputs_with_required_and_default() -> None:
    _write("demo")

    section = render_section(discover_enabled())

    assert "- `message` (required): The message text (example: `hello`)" in section
    assert "- `greeting` (default: `hi`): Opening line" in section


def test_render_section_mentions_run_macro_tool() -> None:
    _write("demo")

    section = render_section(discover_enabled())

    assert "run_macro" in section


def test_a_file_the_loader_cannot_survive_is_excluded_not_fatal(mocker) -> None:
    # The loader does not confine itself to YAMLError — deep nesting surfaces
    # as RecursionError, which used to escape scan() -> discover_enabled() ->
    # build_prompt_bundle and STUCK every wake over ONE bad file. The
    # contract is that any unparseable file is excluded whole.
    #
    # The failure is injected rather than provoked with deeply nested YAML:
    # whether real nesting trips the limit depends on stack depth already
    # consumed, so a literal bomb passes alone and fails inside the suite.
    # What needs pinning is the handling, not the trigger.
    _write("demo")
    _write("boom")
    real = store_mod.parse_macro

    def _explode(text: str, stem: str, pages=None, macros=None, *, values=None):
        if stem == "boom":
            raise RecursionError("maximum recursion depth exceeded")
        return real(text, stem, pages, macros, values=values)

    mocker.patch.object(store_mod, "parse_macro", side_effect=_explode)

    entries = {e.name: e for e in scan()}

    assert entries["boom"].spec is None
    assert entries["boom"].error  # reason kept, so `macros check` can name it
    assert entries["demo"].spec is not None  # a healthy neighbour still loads
    assert "demo" in discover_enabled()


def test_scan_still_lets_keyboard_interrupt_through(mocker) -> None:
    # The broad `except Exception` must not swallow BaseException: a Ctrl-C
    # or a cancelled engine task has to keep propagating.
    _write("demo")
    mocker.patch.object(store_mod, "parse_macro", side_effect=KeyboardInterrupt)

    with pytest.raises(KeyboardInterrupt):
        scan()


# ---------- a folder's `run` steps ----------

OPEN_TEXT = (
    "kind: macro\nschema: 1\nname: open\ndescription: reach\nsteps:\n  - home_screen\n"
)
SEND_TEXT = "kind: macro\nschema: 1\nname: send\ndescription: speak\nsteps:\n  - run: open\n  - send_to_clipboard: hi\n"


def test_scan_resolves_a_run_step_in_its_own_folder_whatever_the_file_order() -> None:
    # `send` sorts after `open`, `a-send` before it: both bind.
    _write("open", OPEN_TEXT)
    _write("send", SEND_TEXT)
    _write("a-send", SEND_TEXT.replace("name: send", "name: a-send"))

    entries = {e.name: e for e in scan()}

    for name in ("send", "a-send"):
        spec = entries[name].spec
        assert spec is not None and spec.callees()[0].name == "open"
    # One parse per file: the two callers share the one `open`.
    assert entries["send"].spec.callees()[0] is entries["a-send"].spec.callees()[0]


def test_a_missing_or_broken_callee_is_the_callers_load_error() -> None:
    _write("send", SEND_TEXT)
    _write("open", "kind: macro\nschema: 1\nname: open\ndescription: d\nsteps: []\n")

    entries = {e.name: e for e in scan()}

    assert entries["send"].spec is None
    assert "`run`: open.yml is invalid: " in (entries["send"].error or "")


def test_macros_that_run_each_other_are_both_refused() -> None:
    _write("a", "kind: macro\nschema: 1\nname: a\ndescription: d\nsteps:\n  - run: b\n")
    _write("b", "kind: macro\nschema: 1\nname: b\ndescription: d\nsteps:\n  - run: a\n")

    entries = {e.name: e for e in scan()}

    assert entries["a"].spec is None and entries["b"].spec is None
    assert "macros that run each other" in (entries["b"].error or "")


def test_a_dotted_run_name_is_the_packs_never_a_neighbour(tmp_path) -> None:
    # `app.macros.<name>` goes to the pack's resolver; a bare name is
    # a sibling file and nothing else.
    from physiclaw.macros.parse import parse_macro

    shared = parse_macro(OPEN_TEXT, "open")
    (tmp_path / "send.yml").write_text(
        SEND_TEXT.replace("run: open", "run: app.macros.open"), encoding="utf-8"
    )

    (entry,) = scan(tmp_path, fallback=lambda name: shared)
    assert entry.spec is not None and entry.spec.callees()[0] is shared

    (alone,) = scan(tmp_path)
    assert alone.spec is None and "names a pack hand" in (alone.error or "")

    (tmp_path / "send.yml").write_text(SEND_TEXT, encoding="utf-8")
    (bare,) = scan(tmp_path, fallback=lambda name: shared)
    assert bare.spec is None and "no macro 'open' beside this one" in (bare.error or "")
