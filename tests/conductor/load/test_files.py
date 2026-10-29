"""Pack reads stay within the selected pack, including symlink targets."""

from pathlib import Path

import pytest
from symlink_helpers import symlink_to

from physiclaw.conductor.load import pack, scaffold
from physiclaw.conductor.spec.model import PlaybookError


@pytest.fixture
def pack_root(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    root.mkdir()
    (root / "APP.yml").write_text("", encoding="utf-8")
    return root


def test_external_prompt_is_reported_without_reading_it(pack_root, tmp_path) -> None:
    private = tmp_path / "private.md"
    private.write_text("private data", encoding="utf-8")
    (pack_root / "prompts").mkdir()
    symlink_to(pack_root / "prompts" / "leak.md", private)

    loaded = pack.load_pack("demo", root=pack_root, values={}, learned=False)

    assert "leak" not in loaded.prompts.ok
    assert "outside" in loaded.prompts.errors["leak"]


def test_external_manifest_is_rejected(pack_root, tmp_path) -> None:
    outside = tmp_path / "manifest.yml"
    outside.write_text("", encoding="utf-8")
    (pack_root / "APP.yml").unlink()
    symlink_to(pack_root / "APP.yml", outside)

    with pytest.raises(PlaybookError, match="outside"):
        pack.load_pack("demo", root=pack_root, values={}, learned=False)


@pytest.mark.parametrize("directory", [False, True])
def test_external_playbook_is_reported(pack_root, tmp_path, directory) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "PLAYBOOK.yml").write_text(
        "description: outside\nroute: []\n", encoding="utf-8"
    )
    entry = pack_root / "task"
    if directory:
        symlink_to(entry, outside, target_is_directory=True)
    else:
        entry.mkdir()
        symlink_to(entry / "PLAYBOOK.yml", outside / "PLAYBOOK.yml")

    loaded = pack.load_pack("demo", root=pack_root, values={}, learned=False)

    assert "task" not in loaded.playbook_docs
    assert "outside" in loaded.playbook_errors["task"]


@pytest.mark.parametrize("folder", ["macros", "prompts", "task/macros", "task/prompts"])
def test_external_asset_directory_is_rejected(pack_root, tmp_path, folder) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (pack_root / "task").mkdir()
    (pack_root / "task" / "PLAYBOOK.yml").write_text(
        "description: task\nroute: []\n", encoding="utf-8"
    )
    symlink_to(pack_root / folder, outside, target_is_directory=True)

    with pytest.raises(PlaybookError, match="outside"):
        pack.load_pack("demo", root=pack_root, values={}, learned=False)


def test_pack_and_internal_prompt_symlinks_remain_usable(pack_root, tmp_path) -> None:
    shared = pack_root / "_shared"
    shared.mkdir()
    (shared / "greeting.md").write_text("Hello", encoding="utf-8")
    symlink_to(pack_root / "prompts", shared, target_is_directory=True)
    alias = tmp_path / "alias"
    symlink_to(alias, pack_root, target_is_directory=True)

    loaded = pack.load_pack("demo", root=alias, values={}, learned=False)

    assert loaded.prompts.ok == {"greeting": "Hello"}


def test_missing_channel_boot_is_not_written_through_external_symlink(
    pack_root, tmp_path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_to(pack_root / "boot", outside, target_is_directory=True)

    with pytest.raises(PlaybookError, match="outside"):
        scaffold.ensure_channel_boot(pack_root)

    assert not (outside / "PLAYBOOK.yml").exists()
