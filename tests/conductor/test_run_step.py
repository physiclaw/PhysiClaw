"""A macro's `run` step inside a pack: the pack's shared folder, a
playbook folder's fallback to it, the liveness that reaches through
it (the readiness lint, the channel's send), and the never_tap check
that judges a callee's taps."""

from __future__ import annotations

import pytest
from conductor_fakes import PACK_MACRO, write_local_macro, write_pack

from physiclaw.conductor.load import channel, scaffold
from physiclaw.conductor.load import pack as pb
from physiclaw.conductor.route import playbook
from physiclaw.conductor.spec.live import disabled_macros
from physiclaw.conductor.spec.model import PlaybookError

SEND = """\
kind: macro
schema: 1
name: send
description: reach, then speak
inputs:
  message:
    description: text
    default: hi
steps:
  - run: open-app
  - send_to_clipboard: "{message}"
"""

VIA_SEND = """\
description: one move through send
inputs:
  keyword:
    description: what to search
route:
  - page: app.pages.home
  - do: speak
    macro: app.macros.send
    with: {message: "{inputs.keyword}"}
  - page: app.pages.home
"""


def _pack_with_send(open_enabled: bool = True):
    root = write_pack("demo", playbooks={"buy": VIA_SEND})
    (root / "macros" / "send.yml").write_text(SEND, encoding="utf-8")
    if not open_enabled:
        mp = root / "macros" / "open-app.yml"
        mp.write_text(
            mp.read_text(encoding="utf-8") + "enabled: false\n", encoding="utf-8"
        )
    return root, pb.load_pack("demo")


def test_a_pack_macro_runs_a_sibling_of_the_shared_folder() -> None:
    _, pack = _pack_with_send()

    assert not pack.macro_errors
    assert pack.macros["send"].callees()[0] is pack.macros["open-app"]


def test_the_readiness_lint_names_a_hand_whose_callee_is_disabled() -> None:
    _, pack = _pack_with_send(open_enabled=False)
    (buy,) = (e.spec for e in pb.scan_playbooks("demo", pack) if e.name == "buy")

    assert buy is not None
    assert pack.macros["send"].enabled and not pack.macros["send"].live
    assert disabled_macros(buy, pack) == ["send"]


def test_a_playbook_folder_macro_runs_the_packs_shared_hand() -> None:
    root = write_pack(
        "demo",
        playbooks={"buy": VIA_SEND.replace("macro: app.macros.send", "macro: own")},
    )
    # From a playbook's folder the pack's hand is named in full.
    own = SEND.replace("name: send", "name: own").replace(
        "run: open-app", "run: app.macros.open-app"
    )
    write_local_macro(root, "buy", "own", own)

    pack = pb.load_pack("demo")
    (buy,) = (e.spec for e in pb.scan_playbooks("demo", pack) if e.name == "buy")

    assert buy is not None and not pack.local["buy"].macros.errors
    own = buy.inline_macros["buy.own"]
    assert own.callees()[0] is pack.macros["open-app"]


def test_a_playbook_folder_macro_named_like_a_pack_hand_is_its_own() -> None:
    # A local `open-app.yml` beside `own` is no clash with the pack's:
    # `own` runs the pack's by its qualified name, and would run its
    # neighbour by the bare one.
    root = write_pack(
        "demo",
        playbooks={"buy": VIA_SEND.replace("macro: app.macros.send", "macro: own")},
    )
    own = SEND.replace("name: send", "name: own").replace(
        "run: open-app", "run: app.macros.open-app"
    )
    write_local_macro(root, "buy", "own", own)
    write_local_macro(root, "buy", "open-app", PACK_MACRO.format(name="open-app"))

    pack = pb.load_pack("demo")
    (buy,) = (e.spec for e in pb.scan_playbooks("demo", pack) if e.name == "buy")

    assert buy is not None, next(
        e.error for e in pb.scan_playbooks("demo", pack) if e.name == "buy"
    )
    own = buy.inline_macros["buy.own"]
    assert own.callees()[0] is pack.macros["open-app"]


def test_an_inline_macro_runs_a_pack_hand() -> None:
    write_pack(
        "demo",
        playbooks={
            "buy": VIA_SEND.replace(
                '    macro: app.macros.send\n    with: {message: "{inputs.keyword}"}\n',
                "    macro: {steps: [{run: app.macros.open-app}]}\n",
            )
        },
    )
    pack = pb.load_pack("demo")
    (buy,) = (e.spec for e in pb.scan_playbooks("demo", pack) if e.name == "buy")

    assert buy is not None
    assert buy.inline_macros["buy.speak"].callees()[0] is pack.macros["open-app"]


def test_never_tap_judges_a_callees_taps_too() -> None:
    # The fixture's every macro taps "t"; a granted macro that only RUNS
    # one still presses it, so the grant is refused the same way.
    from test_playbook import VALID

    root = write_pack("demo")
    (root / "macros" / "send.yml").write_text(SEND, encoding="utf-8")
    pack = pb.load_pack("demo")
    text = VALID.replace(
        "    tools: [tap, scroll]\n",
        '    tools: [scroll, app.macros.send]\n    never_tap: ["t"]\n',
    )

    with pytest.raises(PlaybookError, match="grants macro 'send', which presses"):
        playbook.parse_playbook(text, "buy", pack)


def test_the_channels_send_is_live_only_with_its_open() -> None:
    scaffold.init_pack("channel/wechat")
    from physiclaw.common import paths

    root = paths.playbooks_dir() / "channel" / "wechat"
    for name in ("open", "send"):
        mp = root / "macros" / f"{name}.yml"
        mp.write_text(
            mp.read_text(encoding="utf-8").replace("enabled: false", "enabled: true"),
            encoding="utf-8",
        )
    ch = channel.load_channel()
    assert ch is not None and ch.send == "channel/send" and ch.open == "channel/open"

    mp = root / "macros" / "open.yml"
    mp.write_text(
        mp.read_text(encoding="utf-8").replace("enabled: true", "enabled: false"),
        encoding="utf-8",
    )
    ch = channel.load_channel()
    assert ch is not None and ch.open is None
    # The scaffold's send runs open, so it is not live either.
    assert ch.send is None
