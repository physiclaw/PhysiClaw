"""The channel pack's home: `playbooks/channel/<im>/`, one folder per
IM app, `channel/ACTIVE.txt` naming the one in use — how the folder
resolves, what a missing or wrong word does, where `install` and
`init` put an IM folder, and the per-IM learned-geometry key."""

from __future__ import annotations

from pathlib import Path

import pytest
from conductor_fakes import channel_root, write_active, write_channel
from typer.testing import CliRunner

from physiclaw.cli import app
from physiclaw.common import paths
from physiclaw.conductor.load import pack as pb
from physiclaw.conductor.load import prints, scaffold
from physiclaw.conductor.load.channel import load_channel
from physiclaw.conductor.spec.model import PlaybookError
from physiclaw.conductor.spec.reply import INCOMING_LEFT, incoming_box

runner = CliRunner()


def test_one_im_folder_is_the_channel_with_no_active_file() -> None:
    write_channel()

    assert paths.pack_root("channel") == channel_root()
    assert load_channel().im == "wechat"  # the IM: `incoming_box` keys on it


def test_active_txt_picks_among_several_folders() -> None:
    write_channel(im="wechat")
    write_channel(im="whatsapp")
    write_active("whatsapp")

    assert paths.pack_root("channel").name == "whatsapp"
    assert prints.learned_file("channel").name == "channel-whatsapp.json"


def test_several_folders_without_active_txt_is_no_channel() -> None:
    write_channel(im="wechat")
    write_channel(im="whatsapp")

    folder, gap = paths.channel_root()
    assert folder is None and gap is not None
    assert "wechat, whatsapp" in gap and "ACTIVE.txt" in gap
    with pytest.raises(PlaybookError, match="ACTIVE.txt"):
        pb.load_pack("channel")


def test_a_word_naming_no_folder_is_no_channel() -> None:
    write_channel(im="wechat")
    write_active("telegram")

    folder, gap = paths.channel_root()
    assert folder is None and "'telegram'" in (gap or "") and "wechat" in (gap or "")


def test_the_channel_is_listed_and_the_gap_is_what_check_reports() -> None:
    write_channel(im="wechat")
    write_channel(im="whatsapp")

    assert "channel" in pb.list_apps()
    result = runner.invoke(app, ["playbooks", "check"])
    assert result.exit_code == 1
    assert "write channel/ACTIVE.txt naming one" in result.output


def test_no_pack_declares_a_thread_section() -> None:
    # The user's side of the thread is the IM's (`incoming_box`, by the
    # channel folder its manifest's `app:` names), never a manifest
    # field — a leftover `thread:` block is an unknown key, loudly, in
    # the channel pack as in any other.
    from conductor_fakes import write_pack

    write_channel()
    for root in (write_pack("demo"), channel_root()):
        mp = root / "APP.yml"
        mp.write_text(mp.read_text(encoding="utf-8") + "thread:\n  incoming: left\n")
    with pytest.raises(PlaybookError, match="unknown key.*thread"):
        pb.load_pack("demo")
    with pytest.raises(PlaybookError, match="unknown key.*thread"):
        pb.load_pack("channel")


def test_the_users_side_comes_from_the_im_name() -> None:
    assert incoming_box("wechat") == INCOMING_LEFT
    assert incoming_box("some-new-im") == INCOMING_LEFT  # left by default


def test_init_channel_takes_the_im_name() -> None:
    with pytest.raises(PlaybookError, match="channel/<im>"):
        scaffold.init_pack("channel")

    root = scaffold.init_pack("channel/telegram")

    assert root == paths.playbooks_dir() / "channel" / "telegram"
    assert (root / "APP.yml").exists() and (root / "boot" / "PLAYBOOK.yml").exists()
    assert load_channel().im == "telegram"  # the stub parses as a channel


def test_install_keeps_an_im_folder_under_channel(tmp_path: Path) -> None:
    src = tmp_path / "playbooks" / "channel" / "whatsapp"
    (src / "macros").mkdir(parents=True)
    (src / "APP.yml").write_text(
        "kind: manifest\nschema: 1\napp: whatsapp\ndescription: WhatsApp channel\n"
        "pages:\n  thread:\n    description: the thread\n    anchors: ['Alice']\n",
        encoding="utf-8",
    )
    (src / "macros" / "open.yml").write_text(
        "kind: macro\nschema: 1\nname: open\ndescription: open the channel\n"
        "enabled: false\nsteps:\n  - home_screen\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["playbooks", "install", str(src)])

    assert result.exit_code == 0, result.output
    assert (paths.playbooks_dir() / "channel" / "whatsapp" / "APP.yml").exists()


def test_the_channel_manifest_names_its_im_not_the_role() -> None:
    write_channel()
    mp = channel_root() / "APP.yml"
    mp.write_text(mp.read_text(encoding="utf-8").replace("app: wechat", "app: channel"))

    with pytest.raises(PlaybookError, match="must equal the pack directory 'wechat'"):
        pb.load_pack("channel")


def test_a_home_channel_dir_is_never_shadowed_by_the_tree(
    tmp_path: Path, monkeypatch
) -> None:
    # The tree ships a resolvable channel; a home channel dir with a gap
    # still reports its gap rather than falling through to the template.
    tree = tmp_path / "tree"
    (tree / "channel" / "wechat").mkdir(parents=True)
    (tree / "channel" / "wechat" / "APP.yml").write_text("app: wechat\n")
    (tree / "channel" / "ACTIVE.txt").write_text("wechat\n")
    home = paths.playbooks_dir()
    monkeypatch.setattr(paths, "playbooks_dirs", lambda: [home, tree])

    assert paths.channel_root() == (
        tree / "channel" / "wechat",
        None,
    )  # no home channel

    write_channel(im="wechat")
    write_channel(im="whatsapp")
    folder, gap = paths.channel_root()
    assert folder is None and gap is not None and "ACTIVE.txt" in gap
