"""Tests for `drive.setup` and each wake's saved diagnostic evidence.

The roster names every playbook and its state; the decision explains what
the boot offers or why the session uses the model. Retain both at DEBUG.
"""

from __future__ import annotations

import logging

import pytest
from conductor_fakes import (
    CHANNEL_OPEN,
    FLOW,
    PACK_MACRO,
    channel_root,
    write_channel,
    write_pack,
)

from physiclaw.common import paths
from physiclaw.common.log_style import FailureState
from physiclaw.common.logger import setup_logging
from physiclaw.conductor.drive import setup
from physiclaw.conductor.load import channel
from physiclaw.conductor.load.pack import (
    discover,
    load_pack,
    playbook_gap,
    scan_playbooks,
)
from physiclaw.conductor.walk import suspension

DISABLED_MACRO = "enabled: false\n" + PACK_MACRO.format(name="open-app")


@pytest.fixture
def wake_log(caplog: pytest.LogCaptureFixture, monkeypatch) -> pytest.LogCaptureFixture:
    monkeypatch.setattr(channel, "_channel_failure", FailureState())
    monkeypatch.setattr(channel, "_boot_failure", FailureState())
    caplog.set_level(logging.DEBUG, logger="physiclaw.conductor")
    return caplog


def _lines(caplog: pytest.LogCaptureFixture, prefix: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


# ---------- the roster ----------


def test_roster_names_every_playbook_with_its_state(wake_log) -> None:
    write_channel(CHANNEL_OPEN)
    write_pack(
        playbooks={
            "flow": FLOW,
            "later": "enabled: false\n" + FLOW,
            "broken": "route: 7\n",
        }
    )

    setup.session_setup()

    (roster,) = _lines(wake_log, "Playbooks on disk:")
    assert "demo/flow (live)" in roster
    assert "demo/later (disabled)" in roster
    assert "demo/broken (invalid: " in roster


def test_roster_names_the_disabled_macro_that_holds_a_playbook_back(wake_log) -> None:
    write_channel(CHANNEL_OPEN)
    root = write_pack(playbooks={"flow": FLOW})
    (root / "macros" / "open-app.yml").write_text(DISABLED_MACRO, encoding="utf-8")

    setup.session_setup()

    (roster,) = _lines(wake_log, "Playbooks on disk:")
    assert "demo/flow (disabled macro: open-app)" in roster


def test_roster_carries_an_unusable_pack_as_a_line(wake_log) -> None:
    write_channel(CHANNEL_OPEN)
    bad = paths.playbooks_dir() / "junk"
    bad.mkdir(parents=True)
    (bad / "APP.yml").write_text("app: [\n", encoding="utf-8")

    program, _ = setup.session_setup()

    (roster,) = _lines(wake_log, "Playbooks on disk:")
    assert "junk (pack unusable: " in roster
    assert program is None


def test_playbook_gap_reads_the_one_live_rule() -> None:
    # `spec.live.live_gap` is the rule `require_live` raises off — the roster
    # and the boot's own gate can never disagree about "live".
    write_pack(
        playbooks={
            "flow": FLOW,
            "later": "enabled: false\n" + FLOW,
            "bad": "route: 7\n",
        }
    )
    pack = load_pack("demo")
    by_name = {e.name: e for e in scan_playbooks("demo", pack)}

    assert playbook_gap(by_name["flow"], pack) is None
    assert playbook_gap(by_name["later"], pack) == "disabled"
    assert playbook_gap(by_name["bad"], pack).startswith("invalid: ")
    assert discover().entries.keys() == {"demo/flow"}


# ---------- the decision ----------


def test_a_live_playbook_and_boot_log_what_the_boot_offers(wake_log) -> None:
    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"flow": FLOW})

    program, _ = setup.session_setup()

    assert program is not None
    assert _lines(wake_log, "Boot drives:") == ["Boot drives: offering demo/flow"]
    assert not _lines(wake_log, "Plain model session:")


def test_no_channel_pack_is_the_first_reason(wake_log) -> None:
    write_pack(playbooks={"flow": FLOW})

    program, _ = setup.session_setup()

    assert program is None
    (why,) = _lines(wake_log, "Plain model session:")
    assert "no usable channel pack" in why
    # The roster still prints: the user sees the pack IS there.
    (roster,) = _lines(wake_log, "Playbooks on disk:")
    assert "demo/flow (live)" in roster


def test_a_channel_without_a_live_boot_says_so(wake_log) -> None:
    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"flow": FLOW})
    (paths.playbooks_dir() / "channel" / "wechat" / "boot" / "PLAYBOOK.yml").write_text(
        "kind: entry\nschema: 1\nname: boot\ndescription: off\nenabled: false\nroute:\n"
        "  - page: app.pages.thread\n  - select: parse\n",
        encoding="utf-8",
    )

    program, _ = setup.session_setup()

    assert program is None
    (why,) = _lines(wake_log, "Plain model session:")
    assert "no live boot" in why
    # …and the channel logged the specific gap above it.
    assert any("channel/boot: disabled" in m for m in _lines(wake_log, "conductor:"))
    assert not any(r.levelno >= logging.WARNING for r in wake_log.records)


def test_no_task_pack_on_disk_says_so(wake_log) -> None:
    write_channel(CHANNEL_OPEN)

    program, _ = setup.session_setup()

    assert program is None
    assert _lines(wake_log, "Playbooks on disk:") == ["Playbooks on disk: none"]
    (why,) = _lines(wake_log, "Plain model session:")
    assert "no task pack on disk" in why


def test_only_disabled_playbooks_point_at_the_roster(wake_log) -> None:
    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"flow": "enabled: false\n" + FLOW})

    program, _ = setup.session_setup()

    assert program is None
    (why,) = _lines(wake_log, "Plain model session:")
    assert "no live playbook" in why


# ---------- optional setup and actionable production notices ----------


@pytest.fixture(params=["missing", "placeholder", "thread", "boot"])
def channel_gap(request) -> str:
    if request.param == "missing":
        return "no pack 'channel' on disk"
    write_channel(CHANNEL_OPEN)
    manifest = channel_root() / "APP.yml"
    if request.param == "placeholder":
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace("MyChat", "<<CONTACT>>"),
            encoding="utf-8",
        )
        return "unpopulated template placeholder(s) CONTACT"
    if request.param == "thread":
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace("thread:", "other:"),
            encoding="utf-8",
        )
        return "declares no 'thread' page"
    (channel_root() / "boot" / "PLAYBOOK.yml").write_text(
        "route: 7\n", encoding="utf-8"
    )
    return "channel boot is invalid"


@pytest.mark.parametrize("playbooks", ["absent", "disabled"])
def test_unused_channel_setup_is_silent_but_saved(
    wake_log, channel_gap, playbooks, tmp_path, capsys
) -> None:
    if playbooks == "disabled":
        write_pack(playbooks={"flow": "enabled: false\n" + FLOW})
    setup_logging("runtime", file_dir=tmp_path, file_level=logging.DEBUG)

    program, _ = setup.session_setup()

    assert program is None
    assert capsys.readouterr().err == ""
    saved = (tmp_path / "runtime.log").read_text(encoding="utf-8")
    assert channel_gap in saved
    assert (
        "no task pack on disk" if playbooks == "absent" else "no live playbook"
    ) in saved


def test_live_playbooks_warn_once_with_fallback_and_action(
    wake_log, channel_gap, tmp_path, capsys
) -> None:
    write_pack(playbooks={"flow": FLOW})
    setup_logging("runtime", file_dir=tmp_path, file_level=logging.DEBUG)

    assert setup.session_setup()[0] is None
    notice = capsys.readouterr().err
    assert "WARN" in notice
    assert "Playbook" in notice and "using the task model" in notice
    action = (
        "physiclaw playbooks init channel/<im>"
        if channel_gap == "no pack 'channel' on disk"
        else "physiclaw playbooks check"
    )
    assert action in notice
    assert "Phone messaging" not in notice

    assert setup.session_setup()[0] is None
    assert capsys.readouterr().err == ""
    # Every wake retains the full cause even while the notice is suppressed.
    assert (tmp_path / "runtime.log").read_text(encoding="utf-8").count(
        channel_gap
    ) == 2


def test_channel_warning_returns_after_recovery_or_reenable(wake_log) -> None:
    root = write_pack(playbooks={"flow": FLOW})
    setup.session_setup()
    write_channel(CHANNEL_OPEN)
    assert setup.session_setup()[0] is not None

    manifest = channel_root() / "APP.yml"
    original = manifest.read_text(encoding="utf-8")
    manifest.write_text(original.replace("MyChat", "<<CONTACT>>"), encoding="utf-8")
    setup.session_setup()
    manifest.write_text(original, encoding="utf-8")
    assert setup.session_setup()[0] is not None
    manifest.write_text(original.replace("MyChat", "<<CONTACT>>"), encoding="utf-8")
    setup.session_setup()

    playbook = root / "flow" / "PLAYBOOK.yml"
    original = playbook.read_text(encoding="utf-8")
    playbook.write_text(original + "enabled: false\n", encoding="utf-8")
    setup.session_setup()
    playbook.write_text(original, encoding="utf-8")
    setup.session_setup()

    warnings = [r for r in wake_log.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 4


@pytest.mark.parametrize("channel_status", ["missing", "broken_boot"])
def test_resuming_warns_only_about_setup_it_needs_without_loading_other_packs(
    wake_log, channel_status
) -> None:
    write_pack(playbooks={"flow": FLOW})
    if channel_status == "broken_boot":
        write_channel(CHANNEL_OPEN)
        (channel_root() / "boot" / "PLAYBOOK.yml").write_text(
            "route: 7\n", encoding="utf-8"
        )
    unrelated = paths.playbooks_dir() / "unrelated"
    unrelated.mkdir()
    (unrelated / "APP.yml").write_text("app: [\n", encoding="utf-8")
    suspension.write_suspended(
        {
            "schema": suspension.SUSPENDED_SCHEMA,
            "app": "demo",
            "playbook": "flow",
            "idx": 0,
            "values": {"keyword": "milk"},
        }
    )

    program, hidden = setup.session_setup()

    assert program is not None and program.spec.name == "flow"
    assert "demo/open-app" in hidden
    assert not suspension.suspended_path().exists()
    warnings = [r for r in wake_log.records if r.levelno >= logging.WARNING]
    if channel_status == "missing":
        assert program.channel is None
        assert (
            len(warnings) == 1 and "channel pack unusable" in warnings[0].getMessage()
        )
    else:
        assert program.channel is not None and program.channel.open == "channel/open"
        assert {"channel/open", "channel/send"} <= hidden.keys()
        assert not warnings
        # A later fresh wake does need boot and must still report its failure.
        (unrelated / "APP.yml").unlink()
        assert setup.session_setup()[0] is None
        warnings = [r for r in wake_log.records if r.levelno >= logging.WARNING]
        assert (
            len(warnings) == 1 and "channel boot is invalid" in warnings[0].getMessage()
        )


def test_disabled_task_macro_keeps_unused_channel_quiet(wake_log, capsys) -> None:
    root = write_pack(playbooks={"flow": FLOW})
    (root / "macros" / "open-app.yml").write_text(DISABLED_MACRO, encoding="utf-8")
    setup_logging("runtime", force=False)

    program, _ = setup.session_setup()

    assert program is None and capsys.readouterr().err == ""
    assert "demo/flow (disabled macro: open-app)" in wake_log.text
    assert "no live playbook" in wake_log.text


@pytest.mark.parametrize("reset", ["recovery", "disable"])
def test_boot_warning_tracks_changed_causes_and_resets(wake_log, reset) -> None:
    write_channel(CHANNEL_OPEN)
    root = write_pack(playbooks={"flow": FLOW})
    boot = channel_root() / "boot" / "PLAYBOOK.yml"
    assert setup.session_setup()[0] is not None
    valid_boot = boot.read_text(encoding="utf-8")
    first_error = "route: 7\n"
    second_error = valid_boot.replace("route:", "route: 7\ninvalid_route:")

    for text in (first_error, first_error, second_error, second_error):
        boot.write_text(text, encoding="utf-8")
        assert setup.session_setup()[0] is None
    assert len([r for r in wake_log.records if r.levelno >= logging.WARNING]) == 2

    playbook = root / "flow" / "PLAYBOOK.yml"
    valid_playbook = playbook.read_text(encoding="utf-8")
    if reset == "recovery":
        boot.write_text(valid_boot, encoding="utf-8")
        assert setup.session_setup()[0] is not None
    else:
        playbook.write_text(valid_playbook + "enabled: false\n", encoding="utf-8")
        assert setup.session_setup()[0] is None
        playbook.write_text(valid_playbook, encoding="utf-8")
    boot.write_text(second_error, encoding="utf-8")
    assert setup.session_setup()[0] is None
    assert len([r for r in wake_log.records if r.levelno >= logging.WARNING]) == 3
