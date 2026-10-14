"""Public playbook CLI contracts, with I/O faked at driver boundaries.

Changed-line mutation audit: 38/47 killed across CLI and loader changes. Nine
accepted survivors only decorate diagnostic/help text, choose equivalent JSON
Unicode escaping, or select an alternative YAML quoting style with the same
round-trip behavior. Separate installer mutations cover publication/rollback.
"""

import json
from dataclasses import asdict, replace

import pytest
from typer.testing import CliRunner

from physiclaw.cli import app
from physiclaw.conductor.load import scaffold
from physiclaw.conductor.spec.model import PlaybookError
from physiclaw.debug import stepping

runner = CliRunner()


@pytest.mark.parametrize("command", ["init", "list", "check"])
def test_regression_pack_commands_report_filesystem_errors(command, mocker):
    mocker.patch(
        "physiclaw.conductor.load.scaffold.ensure_format_readme",
        side_effect=PermissionError("pack directory is read-only"),
    )
    args = {"init": ["init", "demo"], "list": ["list"], "check": ["check"]}

    result = runner.invoke(app, ["playbooks", *args[command]])

    assert result.exit_code == 1
    assert "pack directory is read-only" in result.output
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("command", ["list", "check"])
def test_regression_pack_reports_unreadable_manifest(command, mocker):
    scaffold.init_pack("demo")
    mocker.patch(
        "physiclaw.conductor.load.pack.load_pack",
        side_effect=PermissionError("cannot read APP.yml"),
    )

    result = runner.invoke(app, ["playbooks", command])

    assert result.exit_code == {"list": 0, "check": 1}[command]
    assert "cannot read APP.yml" in result.output


def test_regression_step_reset_honors_json():
    result = runner.invoke(app, ["playbooks", "step", "demo/flow", "--reset", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "outcome": "reset",
        "message": "demo/flow: no stored position",
        "position": None,
    }


def test_regression_step_refuses_reset_with_status_before_changing_state():
    path = stepping.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stored position", encoding="utf-8")

    result = runner.invoke(
        app, ["playbooks", "step", "demo/flow", "--reset", "--status"]
    )

    assert result.exit_code == 2
    assert "--reset and --status" in result.output
    assert path.read_text(encoding="utf-8") == "stored position"


@pytest.mark.parametrize("mode", ["reset", "status"])
def test_regression_step_local_modes_report_io_errors(mode, mocker):
    mocker.patch(
        f"physiclaw.debug.stepping.{mode}",
        side_effect=PermissionError("cannot access stored position"),
    )

    result = runner.invoke(app, ["playbooks", "step", "demo/flow", f"--{mode}"])

    assert result.exit_code == 1
    assert "cannot access stored position" in result.output
    assert isinstance(result.exception, SystemExit)


def test_regression_replay_reports_invalid_utf8_listing(tmp_path):
    listing = tmp_path / "listing.txt"
    listing.write_bytes(b"\xff")

    result = runner.invoke(
        app, ["playbooks", "replay", "demo/flow", "--listing", str(listing)]
    )

    assert result.exit_code == 1
    assert "decode" in result.output
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize(
    "args", [["stats", "--last", "-1"], ["propose", "--top", "-1"]]
)
def test_regression_negative_report_limits_are_rejected(args):
    result = runner.invoke(app, ["playbooks", *args])

    assert result.exit_code == 2
    assert "Invalid value" in result.output


@pytest.mark.parametrize(
    "name, advice",
    [
        ("demo", "write the route"),
        ("channel/wechat", "channel/<macro>"),
        ("ios", "--guided"),
    ],
)
def test_init_scaffolds_pack_with_relevant_next_steps(name, advice):
    result = runner.invoke(app, ["playbooks", "init", name])

    assert result.exit_code == 0, result.output
    assert advice in result.output
    assert "Next:" in result.output


def test_init_rejects_invalid_app_name():
    result = runner.invoke(app, ["playbooks", "init", "../../outside"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("command", ["list", "check"])
def test_empty_pack_catalogue_reports_no_packs(command):
    result = runner.invoke(app, ["playbooks", command])

    assert result.exit_code == 0
    assert "No app packs found" in result.output


def test_list_shows_invalid_routes_beside_valid_disabled_routes():
    root = scaffold.init_pack("demo")
    broken = root / "broken"
    broken.mkdir()
    (broken / "PLAYBOOK.yml").write_text("route: invalid\n", encoding="utf-8")

    result = runner.invoke(app, ["playbooks", "list"])

    assert result.exit_code == 0, result.output
    assert "demo/" in result.output
    assert "broken" in result.output
    assert "disabled" in result.output


def test_check_valid_pack_reports_disabled_routes_without_reloading(mocker):
    from physiclaw.conductor.load import pack

    scaffold.init_pack("demo")
    load = mocker.spy(pack, "load_pack")

    result = runner.invoke(app, ["playbooks", "check"])

    assert result.exit_code == 0, result.output
    assert "demo/" in result.output and "disabled" in result.output
    load.assert_called_once_with("demo")


def test_run_forwards_inputs_flags_and_closes_connection(playbook_mcp, mocker):
    arm = mocker.patch(
        "physiclaw.conductor.drive.setup.arm", return_value=("program", "registry")
    )
    walk = mocker.patch(
        "physiclaw.conductor.drive.rehearsal.walk",
        new=mocker.AsyncMock(return_value="completed"),
    )

    result = runner.invoke(
        app, ["playbooks", "run", "demo/flow", "-i", "term=milk", "--verbose", "--raw"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "completed"
    assert arm.call_args.args == ("demo", "flow", {"term": "milk"})
    assert walk.call_args.args == ("program", "registry", playbook_mcp)
    assert walk.call_args.kwargs["verbose"] is True
    assert walk.call_args.kwargs["raw"] is True
    playbook_mcp.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("command", ["run", "step"])
@pytest.mark.parametrize(
    "error",
    [
        PlaybookError("invalid route"),
        PermissionError("pack unreadable"),
        RuntimeError("no model"),
        ConnectionError("offline"),
    ],
)
def test_live_commands_report_driver_errors_and_close(
    command, error, playbook_mcp, mocker
):
    mocker.patch(
        "physiclaw.conductor.drive.setup.arm", return_value=("program", "registry")
    )
    drivers = {
        "run": "physiclaw.conductor.drive.rehearsal.walk",
        "step": "physiclaw.debug.stepping.step",
    }
    mocker.patch(drivers[command], new=mocker.AsyncMock(side_effect=error))

    result = runner.invoke(app, ["playbooks", command, "demo/flow"])

    assert result.exit_code == 1
    assert str(error) in result.output
    assert isinstance(result.exception, SystemExit)
    playbook_mcp.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("command", ["run", "step"])
def test_connection_errors_retain_server_start_hint(command, playbook_mcp, mocker):
    from physiclaw.common.ready import START_HINT

    playbook_mcp.__aenter__.side_effect = ConnectionError("offline")
    mocker.patch(
        "physiclaw.conductor.drive.setup.arm", return_value=("program", "registry")
    )

    result = runner.invoke(app, ["playbooks", command, "demo/flow"])

    assert result.exit_code == 1
    assert START_HINT in result.output


@pytest.mark.parametrize(
    "args, expected", [([], "no stored position"), (["--json"], "null")]
)
def test_step_status_without_state_is_readable(args, expected):
    result = runner.invoke(app, ["playbooks", "step", "demo/flow", "--status", *args])

    assert result.exit_code == 0
    assert expected in result.stdout


def test_step_status_json_preserves_position(mocker):
    position = stepping.Position(
        "demo", "flow", 1, "choose", {"term": "茶"}, {}, False, None, None
    )
    mocker.patch("physiclaw.debug.stepping.status", return_value=position)

    result = runner.invoke(
        app, ["playbooks", "step", "demo/flow", "--status", "--json"]
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == position.to_dict()


def test_step_json_keeps_progress_off_stdout_and_forwards_flags(playbook_mcp, mocker):
    outcome = stepping.StepResult("paused", "choose next", None)

    async def step(*args, **kwargs):
        kwargs["emit"]("progress")
        kwargs["emit_warn"]("warning")
        return outcome

    driver = mocker.patch(
        "physiclaw.debug.stepping.step", new=mocker.AsyncMock(side_effect=step)
    )

    result = runner.invoke(
        app,
        [
            "playbooks",
            "step",
            "demo/flow",
            "--json",
            "--at",
            "one",
            "--to",
            "two",
            "-i",
            "term=茶",
            "-o",
            "agent.answer=yes",
            "--reply",
            "ok",
            "--start-at",
            "tap",
            "--stop-after",
            "paste",
            "--verbose",
            "--raw",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == outcome.to_dict()
    assert "progress" in result.stderr and "warning" in result.stderr
    driver.assert_awaited_once_with(
        "demo",
        "flow",
        playbook_mcp,
        values={"term": "茶"},
        at="one",
        to="two",
        outputs={"agent.answer": "yes"},
        reply="ok",
        start_at="tap",
        stop_after="paste",
        verbose=True,
        raw=True,
        emit=mocker.ANY,
        emit_warn=mocker.ANY,
    )
    playbook_mcp.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("args", [[], ["--session", "demo", "--listing", "screen.txt"]])
def test_replay_requires_exactly_one_source(args):
    result = runner.invoke(app, ["playbooks", "replay", "demo/flow", *args])

    assert result.exit_code == 2
    assert "exactly one" in result.output


@pytest.mark.parametrize("command", ["run", "step", "replay"])
def test_route_commands_reject_unqualified_references(command):
    result = runner.invoke(app, ["playbooks", command, "flow"])

    assert result.exit_code == 1
    assert "<app>/<playbook>" in result.output


def test_replay_resolves_session_before_loading_screens(mocker):
    from physiclaw.conductor.bench.replay import Replay

    resolve = mocker.patch(
        "physiclaw.cli._sessions.resolve_sid", return_value="full-session"
    )
    screens = mocker.patch(
        "physiclaw.conductor.bench.corpus.session_listings", return_value=["screen"]
    )
    mocker.patch(
        "physiclaw.conductor.drive.setup.arm", return_value=("program", "registry")
    )
    replay = mocker.patch(
        "physiclaw.conductor.bench.replay.replay",
        return_value=Replay((), "stopped", "no more screens"),
    )

    result = runner.invoke(
        app, ["playbooks", "replay", "demo/flow", "--session", "suffix"]
    )

    assert result.exit_code == 0
    assert "stopped: no more screens" in result.output
    resolve.assert_called_once_with("suffix")
    screens.assert_called_once_with("full-session")
    replay.assert_called_once_with("program", ["screen"], {})


def test_replay_lists_turns_and_reports_terminal_outcome(tmp_path, mocker):
    from physiclaw.conductor.bench.replay import Replay, ReplayTurn

    listing = tmp_path / "screen.txt"
    listing.write_text("screen", encoding="utf-8")
    arm = mocker.patch(
        "physiclaw.conductor.drive.setup.arm", return_value=("program", "registry")
    )
    replay = mocker.patch(
        "physiclaw.conductor.bench.replay.replay",
        return_value=Replay(
            (ReplayTurn(None, "match", "observe", "ready"),), "completed", "done"
        ),
    )

    result = runner.invoke(
        app,
        [
            "playbooks",
            "replay",
            "demo/flow",
            "--listing",
            str(listing),
            "-i",
            "term=milk",
            "-o",
            "pick.answer=yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (
        "(end)" in result.output
        and "match" in result.output
        and "ready" in result.output
    )
    assert "completed: done" in result.output
    assert arm.call_args.args == ("demo", "flow", {"term": "milk"})
    assert arm.call_args.kwargs["dry"] is True
    replay.assert_called_once_with("program", ["screen"], {"pick.answer": "yes"})


def test_micro_json_forwards_options_and_closes_provider(micro_session):
    result = runner.invoke(
        app,
        [
            "playbooks",
            "micro",
            "session",
            "--model",
            "openai/test",
            "--only",
            "choose",
            "--think",
            "off",
            "--reps",
            "2",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {
            "call": "move",
            "node": "choose",
            "recorded": "done",
            "recorded_args": None,
            "replies": [asdict(micro_session.reply)],
        }
    ]
    assert micro_session.load.call_args.kwargs == {"only": "choose"}
    micro_session.factory.assert_called_once_with("openai", "test")
    micro_session.ask.assert_awaited_once_with(
        micro_session.provider, micro_session.recorded, thinking="off", reps=2
    )
    micro_session.provider.aclose.assert_awaited_once()


@pytest.mark.parametrize("kind", ["same", "differs", "invalid", "error", "unjudged"])
def test_micro_text_reports_reply_judgement(micro_session, kind):
    from physiclaw.conductor.bench.decisions import Row

    replies = {
        "same": micro_session.reply,
        "differs": replace(micro_session.reply, answer="escalate"),
        "invalid": replace(
            micro_session.reply, answer=None, confidence=None, valid=False
        ),
        "error": replace(
            micro_session.reply, error="provider unavailable", valid=False
        ),
        "unjudged": micro_session.reply,
    }
    recorded = {
        "unjudged": replace(
            micro_session.recorded, allowed=(), answer=None, node="", confidence=None
        )
    }.get(kind, micro_session.recorded)
    micro_session.ask.return_value = Row(recorded, (replies[kind],))

    result = runner.invoke(
        app,
        ["playbooks", "micro", "session", "--model", "openai/test", "--think", "low"],
    )

    assert result.exit_code == 0, result.output
    assert kind in result.output
    assert "1 ask(s):" in result.output
    micro_session.provider.aclose.assert_awaited_once()


def test_micro_failure_still_closes_provider(micro_session):
    micro_session.ask.side_effect = RuntimeError("driver failed")

    result = runner.invoke(
        app, ["playbooks", "micro", "session", "--model", "openai/test"]
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    micro_session.provider.aclose.assert_awaited_once()


@pytest.mark.parametrize("failure", ["empty", "unreadable", "provider"])
def test_micro_reports_setup_failures(micro_session, failure):
    micro_session.load.return_value = {"empty": []}.get(
        failure, [micro_session.recorded]
    )
    micro_session.load.side_effect = {"unreadable": OSError("cannot read records")}.get(
        failure
    )
    micro_session.factory.side_effect = {"provider": ValueError("missing key")}.get(
        failure
    )
    expected = {
        "empty": "no recorded decision calls",
        "unreadable": "cannot read records",
        "provider": "cannot build provider",
    }

    result = runner.invoke(
        app, ["playbooks", "micro", "session", "--model", "openai/test"]
    )

    assert result.exit_code == 1
    assert expected[failure] in result.output
    micro_session.ask.assert_not_awaited()


@pytest.mark.parametrize("args", [["--reps", "0"], ["--think", "unknown"]])
def test_micro_rejects_invalid_options_before_loading(micro_session, args):
    result = runner.invoke(app, ["playbooks", "micro", "session", *args])

    assert result.exit_code == 2
    micro_session.load.assert_not_called()
