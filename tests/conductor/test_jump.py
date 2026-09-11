"""A pack macro's one jump reads a page of its pack: the loader hands
the parser the pack's pages, the page is judged by the conductor's own
matcher, and the shipped channel and Taobao packs parse with theirs."""

from __future__ import annotations

import shutil
from pathlib import Path

from conductor_fakes import HOME, RESULTS, write_pack

from physiclaw.common import paths
from physiclaw.common.listing import Screen
from physiclaw.conductor.spec import pack as pb
from physiclaw.conductor.spec.match import PageCheck
from physiclaw.macros import store
from physiclaw.macros.steps import GotoStep, MarkStep

HOP = """\
name: hop
description: reach results unless already there
steps:
  - if: {page: results}
    goto: there
  - tap: "search"
    at: [0.1, 0.1, 0.2, 0.2]
  - mark: there
"""

REPO_PACKS = Path(__file__).resolve().parents[2] / "playbooks"


def test_a_pack_macro_reads_its_pack_page_through_the_conductors_matcher() -> None:
    root = write_pack(playbooks={})
    (root / "macros" / "hop.yml").write_text(HOP, encoding="utf-8")

    pack = pb.load_pack("demo")

    assert "hop" in pack.macros, pack.macro_errors
    goto, mark = pack.macros["hop"].steps[0], pack.macros["hop"].steps[2]
    assert isinstance(goto, GotoStep) and isinstance(mark, MarkStep)
    assert isinstance(goto.page, PageCheck) and goto.page.page_id == "demo.results"
    assert mark.guard is not None and mark.guard.require is goto.page
    assert goto.page.prints == pack.prints  # the walk's own candidate set
    assert goto.page.holds(Screen.read(RESULTS))
    assert not goto.page.holds(Screen.read(HOME))
    assert not goto.page.holds(Screen.read(""))  # unreadable never reads as a page


def test_a_jump_to_a_page_the_pack_does_not_declare_is_the_macros_error() -> None:
    root = write_pack(playbooks={})
    (root / "macros" / "hop.yml").write_text(
        HOP.replace("{page: results}", "{page: cart}"), encoding="utf-8"
    )

    pack = pb.load_pack("demo")

    assert "hop" not in pack.macros
    assert "no page 'cart' in pack 'demo'" in pack.macro_errors["hop"]


def test_an_inline_macro_may_jump_on_its_pack_page() -> None:
    flow = """\
description: one move with an inline jump
route:
  - page: home
  - do: hop
    macro:
      steps:
        - if: {page: results}
          goto: there
        - tap: "search"
          at: [0.1, 0.1, 0.2, 0.2]
        - mark: there
  - page: results
"""
    write_pack(playbooks={"flow": flow})

    (entry,) = pb.scan_playbooks("demo")

    assert entry.error is None, entry.error
    inline = entry.spec.inline_macros["flow.hop"]
    assert isinstance(inline.steps[0], GotoStep)


def test_a_user_macro_outside_a_pack_cannot_jump() -> None:
    root = paths.macros_dir()
    root.mkdir(parents=True, exist_ok=True)
    (root / "hop.yml").write_text(HOP, encoding="utf-8")

    (entry,) = [e for e in store.scan(root) if e.name == "hop"]

    assert entry.spec is None and "cannot jump" in (entry.error or "")


def test_the_shipped_packs_parse_with_their_jumps() -> None:
    # The channel's open/send and Taobao's launch are the jump's first
    # users; they must load against their own pages, placeholders filled.
    for app in ("channel", "taobao"):
        shutil.copytree(REPO_PACKS / app, paths.playbooks_dir() / app)
    (paths.playbooks_dir() / "placeholders.yml").write_text(
        "CONTACT: Alice\n", encoding="utf-8"
    )

    channel, taobao = pb.load_pack("channel"), pb.load_pack("taobao")

    assert not channel.macro_errors and not taobao.macro_errors
    for name in ("open", "send"):
        gotos = [s for s in channel.macros[name].steps if isinstance(s, GotoStep)]
        assert len(gotos) == 1 and gotos[0].page.display() == "page thread"
    launch = [s for s in taobao.macros["launch"].steps if isinstance(s, GotoStep)]
    assert len(launch) == 1 and launch[0].page.display() == "page home"
