"""Public template installation contracts, including interrupted publication.

Mutation audit: 106 installer mutants; 76 killed initially and all eight
behavioral survivors killed after adding nested-path/calibration/error tests.
The 22 accepted survivors change diagnostic decoration, private staging/backup
names, frozen dataclass bookkeeping, a falsey initial cleanup flag, an unreachable
fallback error label, or parents=True on the already-created destination parent.
Rollback and interruption tests exercise both sides of each publishing rename.
Thirteen additional omission mutants for root/values propagation through the
manifest, routes, shared/local macros, prompts and search path were all killed.
"""

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from symlink_helpers import symlink_to

from physiclaw.common import paths
from physiclaw.common.placeholders import (
    PLACEHOLDER_VALUES_FILENAME,
    placeholder_values,
    write_placeholder_values,
)
from physiclaw.conductor.load.install import install_template, read_template
from physiclaw.conductor.load.pack import load_pack
from physiclaw.conductor.spec.model import PlaybookError
from physiclaw.port.locks import FileLockBusy, exclusive_file_lock


def _values_path():
    return paths.playbooks_dir() / PLACEHOLDER_VALUES_FILENAME


def _files(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _existing(template):
    dest, _ = install_template(template, {"CONTACT": "Original"})
    (dest / "local.md").write_text("local work", encoding="utf-8")
    return dest, _files(dest), _values_path().read_bytes()


def _fail_renames(mocker, fail_at, *, after=False, interruption=OSError):
    original = os.replace
    calls = 0

    def replace(src, dst):
        nonlocal calls
        calls += 1
        if calls in fail_at and not after:
            raise interruption("rename interrupted")
        original(src, dst)
        if calls in fail_at and after:
            raise interruption("rename interrupted")

    mocker.patch("physiclaw.conductor.load.install.os.replace", replace)


@pytest.mark.parametrize("point", [1, 2, 3, 4])
@pytest.mark.parametrize("after", [False, True])
def test_replacement_failure_restores_pack_and_values(template, mocker, point, after):
    dest, original_files, original_values = _existing(template)
    _fail_renames(mocker, {point}, after=after)

    with pytest.raises(OSError, match="rename interrupted"):
        install_template(template, {"CONTACT": "Replacement"}, force=True)

    assert _files(dest) == original_files
    assert _values_path().read_bytes() == original_values
    assert list(paths.playbooks_dir().glob(".install-*")) == []


@pytest.mark.parametrize("point", [1, 2, 3, 4])
def test_interrupt_after_rename_restores_original_state(template, mocker, point):
    dest, original_files, original_values = _existing(template)
    _fail_renames(mocker, {point}, after=True, interruption=KeyboardInterrupt)

    with pytest.raises(KeyboardInterrupt, match="rename interrupted"):
        install_template(template, {"CONTACT": "Replacement"}, force=True)

    assert _files(dest) == original_files
    assert _values_path().read_bytes() == original_values


@pytest.mark.parametrize("point", [1, 2])
@pytest.mark.parametrize("after", [False, True])
def test_first_install_failure_removes_partial_new_state(
    template, mocker, point, after
):
    _fail_renames(mocker, {point}, after=after)

    with pytest.raises(OSError, match="rename interrupted"):
        install_template(template, {"CONTACT": "New"})

    assert not (paths.playbooks_dir() / "demo").exists()
    assert not _values_path().exists()
    assert list(paths.playbooks_dir().glob(".install-*")) == []


def test_failed_restore_keeps_backup_and_names_recovery_directory(template, mocker):
    dest, original_files, original_values = _existing(template)
    _fail_renames(mocker, {4, 5})

    with pytest.raises(PlaybookError, match="recovery files kept at") as error:
        install_template(template, {"CONTACT": "Replacement"}, force=True)

    (recovery,) = paths.playbooks_dir().glob(".install-*")
    assert str(recovery) in str(error.value)
    assert original_values in _files(recovery).values()
    assert _files(dest) == original_files


def test_interrupted_failed_restore_keeps_backup_without_swallowing_interrupt(
    template, mocker, caplog
):
    _, _, original_values = _existing(template)
    original = os.replace
    calls = 0

    def replace(src, dst):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise KeyboardInterrupt("stopped")
        if calls == 5:
            raise OSError("restore blocked")
        original(src, dst)

    mocker.patch("physiclaw.conductor.load.install.os.replace", replace)

    with pytest.raises(KeyboardInterrupt, match="stopped"):
        install_template(template, {"CONTACT": "Replacement"}, force=True)

    (recovery,) = paths.playbooks_dir().glob(".install-*")
    assert original_values in _files(recovery).values()
    assert str(recovery) in caplog.text


def test_install_merges_values_written_after_template_was_read(template):
    write_placeholder_values({"CITY": "Shanghai"})

    dest, pack = install_template(template, {"CONTACT": "Alice"})

    assert dest == paths.playbooks_dir() / "demo"
    assert placeholder_values() == {"CITY": "Shanghai", "CONTACT": "Alice"}
    assert pack.macros["send"].steps[0].args == {"text": "Alice"}


def test_second_interrupt_during_restore_preserves_backups(template, mocker, caplog):
    _, _, original_values = _existing(template)
    original = os.replace
    calls = 0

    def replace(src, dst):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("publication failed")
        if calls == 5:
            raise KeyboardInterrupt("restore interrupted")
        original(src, dst)

    mocker.patch("physiclaw.conductor.load.install.os.replace", replace)

    with pytest.raises(KeyboardInterrupt, match="restore interrupted"):
        install_template(template, {"CONTACT": "Replacement"}, force=True)

    (recovery,) = paths.playbooks_dir().glob(".install-*")
    assert original_values in _files(recovery).values()
    assert str(recovery) in caplog.text


def test_install_without_new_values_preserves_values_file_bytes(template):
    _values_path().parent.mkdir(parents=True)
    _values_path().write_text("# user comment\nCONTACT: Alice\n", encoding="utf-8")
    original = _values_path().read_bytes()

    install_template(template, {})

    assert _values_path().read_bytes() == original


def test_install_rejects_competing_writer_without_mutation(template):
    with exclusive_file_lock(paths.playbooks_dir() / ".install.lock"):
        with pytest.raises(FileLockBusy, match="lock is held"):
            install_template(template, {"CONTACT": "Alice"})

    assert not (paths.playbooks_dir() / "demo").exists()
    assert not _values_path().exists()


def test_install_rechecks_new_destination_after_staging(template, mocker):
    from physiclaw.conductor.load import install

    writer = install.write_placeholder_values
    marker = paths.playbooks_dir() / "demo" / "other.md"

    def write_then_create(values, *, path=None):
        writer(values, path=path)
        marker.parent.mkdir()
        marker.write_text("created concurrently", encoding="utf-8")

    mocker.patch.object(install, "write_placeholder_values", write_then_create)

    with pytest.raises(PlaybookError, match="--force"):
        install_template(template, {"CONTACT": "Alice"})

    assert marker.read_text(encoding="utf-8") == "created concurrently"
    assert not _values_path().exists()


def test_forced_symlink_replacement_does_not_modify_link_target(template, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.md").write_text("untouched", encoding="utf-8")
    paths.playbooks_dir().mkdir(parents=True)
    dest = paths.playbooks_dir() / "demo"
    symlink_to(dest, external, target_is_directory=True)

    install_template(template, {"CONTACT": "Alice"}, force=True)

    assert not dest.is_symlink()
    assert (external / "keep.md").read_text(encoding="utf-8") == "untouched"
    assert (dest / "APP.yml").is_file()


def test_staged_loader_uses_supplied_values_across_manifest_and_macro(template_root):
    from physiclaw.conductor.load.prints import save_learned
    from physiclaw.conductor.spec.pages import LearnedPage

    write_placeholder_values({"CONTACT": "Installed"})
    save_learned("demo", {"home": LearnedPage(anchors={}, observations=3)})
    original = _values_path().read_bytes()
    manifest = template_root / "APP.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + "pages:\n  home:\n    description: Home\n    anchors: ['<<CONTACT>>']\n",
        encoding="utf-8",
    )

    pack = load_pack(
        "demo", root=template_root, values={"CONTACT": "Staged"}, learned=False
    )

    assert pack.macros["send"].steps[0].args == {"text": "Staged"}
    assert pack.pages["home"].anchors[0].text == "Staged"
    assert pack.prints[0].learned is None
    assert _values_path().read_bytes() == original


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    value=st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs", "Cc", "Zl", "Zp"), blacklist_characters="{}"
        ),
        min_size=1,
        max_size=20,
    ).filter(lambda value: value.strip() == value and bool(value) and "<<" not in value)
)
def test_installed_placeholders_preserve_literal_text(template, value):
    _values_path().unlink(missing_ok=True)

    _, pack = install_template(template, {"CONTACT": value}, force=True)

    assert pack.macros["send"].steps[0].args == {"text": value}
    assert placeholder_values()["CONTACT"] == value


@pytest.mark.parametrize(
    "kind, error, message",
    [
        ("missing", FileNotFoundError, "missing"),
        ("file", PlaybookError, "not a directory"),
        ("no-manifest", PlaybookError, "missing APP.yml"),
    ],
)
def test_template_source_requires_directory_and_manifest(
    tmp_path, kind, error, message
):
    sources = {
        "missing": tmp_path / "missing",
        "file": tmp_path / "file",
        "no-manifest": tmp_path / "empty",
    }
    sources["file"].write_text("not a directory", encoding="utf-8")
    sources["no-manifest"].mkdir()

    with pytest.raises(error, match=message):
        read_template(sources[kind])

    assert not paths.playbooks_dir().exists()


def test_template_reader_skips_drafts_and_unrelated_files(template_root):
    draft = template_root / "_draft"
    draft.mkdir()
    (draft / "bad.yml").write_text("broken", encoding="utf-8")
    (template_root / "00-notes.txt").write_text("not a pack file", encoding="utf-8")

    template = read_template(template_root)

    assert set(template.files) == {
        Path("APP.yml"),
        Path("README.md"),
        Path("macros/send.yml"),
    }


def test_template_reader_follows_internal_file_and_directory_links(template_root):
    internal = template_root / "_shared"
    internal.mkdir()
    (internal / "greeting.md").write_text("Hello", encoding="utf-8")
    symlink_to(template_root / "prompts", internal, target_is_directory=True)
    symlink_to(template_root / "alias.md", template_root / "README.md")

    template = read_template(template_root)

    assert template.files[Path("prompts/greeting.md")] == "Hello"
    assert template.files[Path("alias.md")] == "original notes\n"


def test_template_reader_rejects_directory_cycle(template_root):
    symlink_to(template_root / "loop", template_root, target_is_directory=True)

    with pytest.raises(PlaybookError, match="symlink cycle"):
        read_template(template_root)

    assert not paths.playbooks_dir().exists()


def test_template_reader_rejects_nested_cycle_at_first_repeated_directory(
    template_root,
):
    nested = template_root / "notes" / "nested"
    nested.mkdir(parents=True)
    symlink_to(nested / "back", template_root, target_is_directory=True)

    with pytest.raises(PlaybookError, match="symlink cycle"):
        read_template(template_root)


def test_template_reader_reports_broken_eligible_link(template_root):
    symlink_to(template_root / "missing.md", template_root / "absent.md")

    with pytest.raises(FileNotFoundError, match="absent.md"):
        read_template(template_root)


def test_install_preserves_deeply_nested_notes(template_root):
    note = template_root / "prompts" / "nested" / "README.md"
    note.parent.mkdir(parents=True)
    note.write_text("nested notes", encoding="utf-8")

    dest, _ = install_template(read_template(template_root), {"CONTACT": "Alice"})

    assert (dest / "prompts/nested/README.md").read_text(
        encoding="utf-8"
    ) == "nested notes"


def test_install_does_not_borrow_installed_calibration(template_root):
    from physiclaw.conductor.load.prints import save_learned
    from physiclaw.conductor.spec.pages import LearnedPage

    save_learned("demo", {"home": LearnedPage(anchors={}, observations=3)})
    manifest = template_root / "APP.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + "pages:\n  home:\n    description: home\n    anchors: [Home]\n",
        encoding="utf-8",
    )

    _, pack = install_template(read_template(template_root), {"CONTACT": "Alice"})

    assert pack.prints[0].learned is None


def test_normal_pack_load_keeps_installed_calibration(template_root):
    from physiclaw.conductor.load.prints import save_learned
    from physiclaw.conductor.spec.pages import LearnedPage

    geometry = LearnedPage(anchors={}, observations=3)
    save_learned("demo", {"home": geometry})
    manifest = template_root / "APP.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + "pages:\n  home:\n    description: home\n    anchors: [Home]\n",
        encoding="utf-8",
    )
    install_template(read_template(template_root), {"CONTACT": "Alice"})

    pack = load_pack("demo")

    assert pack.prints[0].learned == geometry


def test_pack_load_uses_one_values_snapshot_when_file_changes(template_root, mocker):
    from physiclaw.conductor.load import files

    install_template(read_template(template_root), {"CONTACT": "Original"})
    read = files.read_text

    def changing_read(path):
        body = read(path)
        write_placeholder_values({"CONTACT": "Changed"})
        return body

    mocker.patch("physiclaw.conductor.load.files.read_text", side_effect=changing_read)

    pack = load_pack("demo")

    assert pack.macros["send"].steps[0].args == {"text": "Original"}
    assert placeholder_values() == {"CONTACT": "Changed"}


def test_staged_values_reach_entry_macros_routes_and_prompts(template_root):
    write_placeholder_values({"CONTACT": "Installed"})
    entry = template_root / "flow"
    (entry / "macros").mkdir(parents=True)
    (entry / "prompts").mkdir()
    (template_root / "prompts").mkdir()
    (entry / "PLAYBOOK.yml").write_text(
        "kind: entry\nschema: 1\nname: flow\ndescription: Talk to <<CONTACT>>\nroute:\n  - page: home\n    description: Home page\n    anchors: [Home]\n  - do: send\n    macro: local\n  - page: home\n",
        encoding="utf-8",
    )
    (entry / "macros/local.yml").write_text(
        (template_root / "macros/send.yml")
        .read_text(encoding="utf-8")
        .replace("name: send", "name: local"),
        encoding="utf-8",
    )
    (entry / "prompts/own.md").write_text("Ask <<CONTACT>>", encoding="utf-8")
    (template_root / "prompts/shared.md").write_text(
        "Tell <<CONTACT>>", encoding="utf-8"
    )

    _, pack = install_template(read_template(template_root), {"CONTACT": "Staged"})

    assert pack.playbook_docs["flow"]["description"] == "Talk to Staged"
    assert pack.local["flow"].macros.ok["local"].steps[0].args == {"text": "Staged"}
    assert pack.local["flow"].prompts.ok == {"own": "Ask Staged"}
    assert pack.prompts.ok == {"shared": "Tell Staged"}


def test_install_reports_specific_playbook_error_before_publishing(template_root):
    route = template_root / "flow" / "PLAYBOOK.yml"
    route.parent.mkdir()
    route.write_text(
        "kind: entry\nschema: 1\nname: flow\ndescription: invalid\nroute: not-a-route\n",
        encoding="utf-8",
    )

    with pytest.raises(PlaybookError, match="flow.*route"):
        install_template(read_template(template_root), {"CONTACT": "Alice"})

    assert not (paths.playbooks_dir() / "demo").exists()
    assert not _values_path().exists()


def test_successful_install_survives_temporary_directory_cleanup_error(
    template, mocker
):
    rmdir = os.rmdir

    def remove(path, *args, **kwargs):
        if Path(path).name.startswith(".install-"):
            raise PermissionError("cleanup denied")
        return rmdir(path, *args, **kwargs)

    mocker.patch("os.rmdir", side_effect=remove)

    dest, pack = install_template(template, {"CONTACT": "Alice"})

    assert (dest / "APP.yml").exists()
    assert pack.macros["send"].steps[0].args == {"text": "Alice"}
    assert placeholder_values() == {"CONTACT": "Alice"}


def test_template_reader_rejects_file_symlink_cycle(template_root):
    cycle = template_root / "loop.md"
    symlink_to(cycle, cycle)

    with pytest.raises(PlaybookError, match="symlink cycle"):
        read_template(template_root)

    assert not paths.playbooks_dir().exists()


def test_template_reader_rejects_cyclic_source_root(tmp_path):
    cycle = tmp_path / "loop"
    symlink_to(cycle, cycle)

    with pytest.raises(PlaybookError, match="invalid template path"):
        read_template(cycle)

    assert not paths.playbooks_dir().exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes require POSIX")
def test_template_reader_refuses_special_files_without_reading_them(template_root):
    os.mkfifo(template_root / "pipe.md")

    with pytest.raises(PlaybookError, match="unsupported template file"):
        read_template(template_root)

    assert not paths.playbooks_dir().exists()


def test_install_rejects_escaping_channel_parent(template_root, tmp_path):
    import shutil

    source = tmp_path / "channel" / "demo"
    shutil.copytree(template_root, source)
    template = read_template(source)
    external = tmp_path / "external"
    external.mkdir()
    paths.playbooks_dir().mkdir(parents=True)
    symlink_to(paths.playbooks_dir() / "channel", external, target_is_directory=True)

    with pytest.raises(PlaybookError, match="parent resolves outside"):
        install_template(template, {"CONTACT": "Alice"})

    assert list(external.iterdir()) == []


def test_explicit_empty_placeholder_values_do_not_fall_back_to_installed_values(
    template_root,
):
    write_placeholder_values({"CONTACT": "Installed"})

    pack = load_pack("demo", root=template_root, values={}, learned=False)

    assert "unpopulated template placeholder" in pack.macro_errors["send"]


def test_empty_manifest_is_a_valid_template(template_root):
    (template_root / "APP.yml").write_text("", encoding="utf-8")

    template = read_template(template_root)
    dest, pack = install_template(template, {"CONTACT": "Alice"})

    assert (dest / "APP.yml").read_text(encoding="utf-8") == ""
    assert pack.macros["send"].steps[0].args == {"text": "Alice"}
