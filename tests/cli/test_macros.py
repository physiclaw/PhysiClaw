"""Tests for `physiclaw.cli.macros` — list/check/run/stats over macro dirs
under the per-test `~/.physiclaw` (autouse `physiclaw_home`)."""

# Accepted mutation survivors: cosmetic help/advice wrappers, indentation,
# approximate display clipping budgets, unknown-value placeholders and color;
# the fallback timestamp when a directory vanishes (0 vs 1 sorts equally old);
# empty scan-error/counter defaults that their producers already normalize;
# and _run_live's defaults, always supplied by the public command. These do
# not change the exercised history, rehearsal, validation or stats behavior.

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer
from hypothesis import given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from physiclaw.cli.macros import macros_app
from physiclaw.common import paths, verdict
from physiclaw.common.config import CONFIG
from physiclaw.macros import runlog as macro_runlog
from physiclaw.macros import runner as macro_runner
from physiclaw.macros import stats as macro_stats

app = typer.Typer()
app.add_typer(macros_app, name="macros")
runner = CliRunner()

VALID = """
kind: macro
schema: 1
name: {name}
description: Demo macro
enabled: {enabled}

inputs:
  msg:
    description: text

steps:
  - send_to_clipboard: "{{msg}}"
"""


TAP_MACRO = """kind: macro
schema: 1
name: tap-demo
description: One tap, for reading a bbox back out of the run log

steps:
  - tap: t
    at: [0.12, 0.04, 0.34, 0.10]
"""

NAV_MACRO = """kind: macro
schema: 1
name: nav-demo
description: One argument-free navigation step

steps:
  - home_screen
"""


def _write(name: str, text: str | None = None, *, enabled: bool = True) -> None:
    paths.macros_dir().mkdir(parents=True, exist_ok=True)
    body = (
        text
        if text is not None
        else VALID.format(name=name, enabled=str(enabled).lower())
    )
    (paths.macros_dir() / f"{name}.yml").write_text(body, encoding="utf-8")


class FakeMcp:
    """Async-context MCP stand-in returning one changed-screen gesture."""

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def call_tool(self, name, args=None):
        return [{"type": "text", "text": verdict.attach(f"{name} ok", True)}]


def _patch_mcp(mocker, client=FakeMcp) -> None:
    mocker.patch("physiclaw.agent.engine.mcp_tool.McpClient", client)


# ---------- init ----------


def test_init_scaffolds_a_checkable_macro() -> None:
    result = runner.invoke(app, ["macros", "init", "my-macro"])

    assert result.exit_code == 0
    assert "my-macro.yml" in result.output
    assert "flip `enabled: false` to true" in result.output  # next-steps guidance
    check = runner.invoke(app, ["macros", "check"])
    assert check.exit_code == 0


def test_init_existing_macro_exits_one() -> None:
    runner.invoke(app, ["macros", "init", "my-macro"])

    result = runner.invoke(app, ["macros", "init", "my-macro"])

    assert result.exit_code == 1
    assert "already exists" in result.output


def test_init_bad_name_exits_one() -> None:
    result = runner.invoke(app, ["macros", "init", "Bad_Name"])

    assert result.exit_code == 1
    assert "lowercase" in result.output


# ---------- list ----------


def test_list_with_no_macros_hints_at_init() -> None:
    result = runner.invoke(app, ["macros", "list"])

    assert result.exit_code == 0
    assert "No macros found" in result.output
    assert "physiclaw macros init" in result.output


def test_list_writes_the_format_readme() -> None:
    runner.invoke(app, ["macros", "list"])

    assert (paths.macros_dir() / "README.md").exists()


def test_list_shows_enabled_disabled_and_invalid() -> None:
    _write("on", enabled=True)
    _write("off", enabled=False)
    _write("bad", text="steps: [not yaml")

    result = runner.invoke(app, ["macros", "list"])

    assert "enabled" in result.output and "on" in result.output
    assert "disabled" in result.output and "off" in result.output
    assert "invalid" in result.output and "bad" in result.output


# ---------- check ----------


def test_check_all_valid_exits_zero() -> None:
    _write("good")

    result = runner.invoke(app, ["macros", "check"])

    assert result.exit_code == 0
    assert "✓ good" in result.output


def test_check_invalid_macro_exits_one_with_reason() -> None:
    _write(
        "bad",
        text="kind: macro\nschema: 1\nname: other\ndescription: d\nsteps:\n  - peek:\n",
    )

    result = runner.invoke(app, ["macros", "check"])

    assert result.exit_code == 1
    assert "must equal the file name" in result.output


def test_check_reminds_that_a_disabled_macro_is_not_reachable() -> None:
    # A disabled macro passes `check` — the one thing a green checkmark
    # invites you to assume it does not.
    _write("staged", enabled=False)

    result = runner.invoke(app, ["macros", "check"])

    assert result.exit_code == 0
    assert "✓ staged  (disabled)" in result.output
    assert "the agent cannot call: staged" in result.output
    assert "enabled: true" in result.output


def test_check_says_nothing_extra_when_every_macro_is_enabled() -> None:
    _write("live")

    result = runner.invoke(app, ["macros", "check"])

    assert "disabled" not in result.output
    assert "cannot call" not in result.output


def test_check_names_every_disabled_macro() -> None:
    _write("one", enabled=False)
    _write("two", enabled=False)
    _write("three")

    result = runner.invoke(app, ["macros", "check"])

    assert "cannot call: one, two" in result.output
    assert "three" not in result.output.split("cannot call")[1]


def test_check_reports_the_fleet_gate_instead_of_the_per_file_advice(mocker) -> None:
    # The gate overrides every file's own flag, so advising `enabled: true`
    # here would send the user to edit a file that changes nothing.
    mocker.patch.object(CONFIG.macros, "enabled", False)
    _write("live")
    _write("staged", enabled=False)

    result = runner.invoke(app, ["macros", "check"])

    assert "the agent sees no macros at all" in result.output
    assert "cannot call" not in result.output


def test_check_still_exits_one_when_a_disabled_macro_sits_beside_a_broken_one() -> None:
    # The reminder must not swallow the failure it prints after.
    _write("staged", enabled=False)
    _write(
        "bad",
        text="kind: macro\nschema: 1\nname: nope\ndescription: d\nsteps:\n  - peek:\n",
    )

    result = runner.invoke(app, ["macros", "check"])

    assert result.exit_code == 1
    assert "cannot call: staged" in result.output


# ---------- run ----------


def test_run_unknown_macro_exits_one() -> None:
    result = runner.invoke(app, ["macros", "run", "ghost"])

    assert result.exit_code == 1
    assert "no macro named" in result.output


def test_run_invalid_macro_exits_one_with_reason() -> None:
    _write("bad", text="steps: [not yaml")

    result = runner.invoke(app, ["macros", "run", "bad"])

    assert result.exit_code == 1
    assert "invalid YAML" in result.output


def test_run_bad_input_pair_exits_two() -> None:
    _write("demo")

    result = runner.invoke(app, ["macros", "run", "demo", "-i", "novalue"])

    assert result.exit_code == 2
    assert "key=value" in result.output


def test_run_success_prints_log_and_records_stats(mocker) -> None:
    _write("demo")
    _patch_mcp(mocker)

    result = runner.invoke(app, ["macros", "run", "demo", "-i", "msg=hi"])

    assert result.exit_code == 0
    assert "all 1 steps completed" in result.output
    assert macro_stats.load()["demo"]["total_successes"] == 1


def test_run_works_on_disabled_macro(mocker) -> None:
    # Rehearse-before-enable: `run` must not require enabled: true.
    _write("demo", enabled=False)
    _patch_mcp(mocker)

    result = runner.invoke(app, ["macros", "run", "demo", "-i", "msg=hi"])

    assert result.exit_code == 0


def test_run_missing_required_input_records_bad_input(mocker) -> None:
    _write("demo")
    _patch_mcp(mocker)

    result = runner.invoke(app, ["macros", "run", "demo"])

    assert result.exit_code == 1
    assert "missing required input" in result.output
    assert macro_stats.load()["demo"]["last_abort"]["reason"] == "bad_input"


def test_run_abort_exits_one_and_records(mocker) -> None:
    class DownMcp(FakeMcp):
        async def call_tool(self, name, args=None):
            raise RuntimeError("arm busy")

    _write("demo")
    _patch_mcp(mocker, DownMcp)

    result = runner.invoke(app, ["macros", "run", "demo", "-i", "msg=hi"])

    assert result.exit_code == 1
    assert "ABORTED" in result.output
    assert macro_stats.load()["demo"]["last_abort"]["reason"] == "tool_error"


def test_run_unreachable_server_records_nothing(mocker) -> None:
    class NoServer(FakeMcp):
        async def __aenter__(self):
            # The shape McpClient now raises: a plain Exception naming the
            # URL, not the bare CancelledError the anyio task group used to
            # leak (which no `except Exception` could catch, so this command
            # printed a raw traceback instead of the message below).
            raise ConnectionError(
                "cannot reach the MCP server at http://127.0.0.1:8048/mcp"
            )

    _write("demo")
    _patch_mcp(mocker, NoServer)

    result = runner.invoke(app, ["macros", "run", "demo", "-i", "msg=hi"])

    assert result.exit_code == 1
    assert "cannot reach the MCP server" in result.output
    # `mcp`, not `server`: the latter also spawns the agent runtime, which
    # would drive the phone between the steps being rehearsed.
    assert "Start it first: physiclaw mcp" in result.output
    assert "Traceback" not in result.output
    assert macro_stats.load() == {}  # nothing ran, so nothing is recorded


# ---------- runs (per-step debug log) ----------


def test_run_prints_the_run_id_and_lookup_hint(mocker) -> None:
    _write("demo")
    _patch_mcp(mocker)

    result = runner.invoke(app, ["macros", "run", "demo", "-i", "msg=hi"])

    assert "run: macro-run-" in result.output
    assert "physiclaw macros runs " in result.output


def test_runs_with_no_history_reports_empty() -> None:
    result = runner.invoke(app, ["macros", "runs"])

    assert result.exit_code == 0
    assert "No macro runs logged" in result.output


def test_runs_lists_recent_runs(mocker) -> None:
    _write("demo")
    _patch_mcp(mocker)
    runner.invoke(app, ["macros", "run", "demo", "-i", "msg=hi"])

    result = runner.invoke(app, ["macros", "runs"])

    assert result.exit_code == 0
    assert "macro-run-" in result.output
    assert "demo" in result.output
    assert "cli" in result.output


def test_runs_replays_one_run_by_bare_hex(mocker) -> None:
    _write("demo")
    _patch_mcp(mocker)
    ran = runner.invoke(app, ["macros", "run", "demo", "-i", "msg=hi"])
    hex6 = re.search(r"macro-run-([0-9a-f]{6})", ran.output).group(1)

    result = runner.invoke(app, ["macros", "runs", hex6])

    assert result.exit_code == 0
    assert "✓ 1. send_to_clipboard" in result.output
    assert "[ok]" in result.output


def test_runs_shows_the_arguments_each_step_fired_with(mocker) -> None:
    # The number you edit when a tap lands wrong. It was always recorded;
    # rendering it is what makes the run log answer the rehearsal question
    # without opening the macro file.
    _write("tap-demo", TAP_MACRO)
    _patch_mcp(mocker)
    ran = runner.invoke(app, ["macros", "run", "tap-demo"])
    hex6 = re.search(r"macro-run-([0-9a-f]{6})", ran.output).group(1)

    result = runner.invoke(app, ["macros", "runs", hex6])

    assert result.exit_code == 0
    # The label renders WITH the bbox — the annotated target is what the
    # author reads when a tap lands wrong.
    assert 'label: "t"' in result.output
    assert "bbox: [0.12, 0.04, 0.34, 0.1]" in result.output


def test_runs_shows_arguments_after_substitution(mocker) -> None:
    # Post-substitution, so the log shows what actually fired rather than
    # the `{msg}` template that produced it.
    _write("demo")
    _patch_mcp(mocker)
    ran = runner.invoke(app, ["macros", "run", "demo", "-i", "msg=hello there"])
    hex6 = re.search(r"macro-run-([0-9a-f]{6})", ran.output).group(1)

    result = runner.invoke(app, ["macros", "runs", hex6])

    assert 'args: text: "hello there"' in result.output
    assert "{msg}" not in result.output


def test_runs_omits_the_args_line_for_a_step_with_no_arguments(mocker) -> None:
    # `home_screen` takes none — an empty `args: ` line would be noise on
    # every navigation step.
    _write("nav-demo", NAV_MACRO)
    _patch_mcp(mocker)
    ran = runner.invoke(app, ["macros", "run", "nav-demo"])
    hex6 = re.search(r"macro-run-([0-9a-f]{6})", ran.output).group(1)

    result = runner.invoke(app, ["macros", "runs", hex6])

    assert "✓ 1. home_screen" in result.output
    assert "args:" not in result.output


def test_runs_clips_an_overlong_argument_value(mocker) -> None:
    _write("long-demo")
    _patch_mcp(mocker)
    ran = runner.invoke(app, ["macros", "run", "long-demo", "-i", f"msg={'x' * 400}"])
    hex6 = re.search(r"macro-run-([0-9a-f]{6})", ran.output).group(1)

    result = runner.invoke(app, ["macros", "runs", hex6])

    args_line = next(ln for ln in result.output.splitlines() if "args:" in ln)

    assert "…" in args_line
    assert len(args_line) < 400


def test_runs_hangs_a_multiline_screen_under_its_own_label() -> None:
    # An element listing carries embedded newlines. Left as-is those rows
    # restart at column 0 and the block stops reading as one field of the
    # step above it.
    rl = macro_runlog.RunLogger("demo", "cli")
    rl.step(1, "tap", "tap-1", "guard_failed", screen_text="row one\nrow two")

    result = runner.invoke(app, ["macros", "runs", rl.run_id[-6:]])

    assert result.exit_code == 0
    assert "      screen: row one" in result.output
    assert "              row two" in result.output


def test_runs_unknown_id_exits_one(mocker) -> None:
    _write("demo")
    _patch_mcp(mocker)
    runner.invoke(app, ["macros", "run", "demo", "-i", "msg=hi"])

    result = runner.invoke(app, ["macros", "runs", "ffffff"])

    assert result.exit_code == 1
    assert "no run matching" in result.output


# ---------- stats ----------


def test_stats_empty_reports_no_runs() -> None:
    result = runner.invoke(app, ["macros", "stats"])

    assert result.exit_code == 0
    assert "No macro runs recorded" in result.output


def test_stats_shows_counters_and_last_abort() -> None:
    macro_stats.record("demo", ok=True, known_names={"demo"})
    macro_stats.record(
        "demo",
        ok=False,
        known_names={"demo"},
        step=2,
        reason="guard_failed",
        detail="'WeChat' not found",
    )

    result = runner.invoke(app, ["macros", "stats"])

    assert "demo: 1/2 ok, 1 abort(s), streak 1" in result.output
    assert "step 2 [guard_failed] 'WeChat' not found" in result.output


def test_run_start_at_is_forwarded_to_the_runner(mocker) -> None:
    # The agent can resume a macro partway in, so the rehearsal command must
    # be able to exercise that same path — otherwise the one thing you are
    # told to test before enabling is untestable.
    _write("demo")
    seen: dict = {}

    async def fake(
        spec, values, mcp, caller="engine", start_at="", stop_after="", record_as=""
    ):
        seen["start_at"] = start_at
        seen["stop_after"] = stop_after
        return macro_runner.MacroRunResult(blocks=[], ok=True)

    _patch_mcp(mocker, FakeMcp)
    mocker.patch.object(macro_runner, "run_and_record", side_effect=fake)

    result = runner.invoke(
        app,
        [
            "macros",
            "run",
            "demo",
            "-i",
            "msg=hi",
            "--start-at",
            "paste",
            "--stop-after",
            "paste",
        ],
    )

    assert result.exit_code == 0
    assert seen["start_at"] == "paste"
    assert seen["stop_after"] == "paste"


def test_run_resolves_a_pack_macro_by_qualified_name(mocker, monkeypatch) -> None:
    # `macros run demo/open-app` — the same name a walk dispatches, so a
    # pack's hand is rehearsed gesture by gesture without a playbook.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "conductor"))
    from conductor_fakes import write_pack

    write_pack(macros=("open-app",))
    seen: dict = {}

    async def fake(
        spec, values, mcp, caller="engine", start_at="", stop_after="", record_as=""
    ):
        seen["name"] = spec.name
        seen["start_at"] = start_at
        return macro_runner.MacroRunResult(blocks=[], ok=True)

    _patch_mcp(mocker, FakeMcp)
    mocker.patch.object(macro_runner, "run_and_record", side_effect=fake)

    result = runner.invoke(
        app, ["macros", "run", "demo/open-app", "-i", "message=hi", "--start-at", "go"]
    )

    assert result.exit_code == 0, result.output
    assert seen == {"name": "open-app", "start_at": "go"}


def test_run_names_the_missing_pack_macro() -> None:
    result = runner.invoke(app, ["macros", "run", "ghost/x"])

    assert result.exit_code == 1 and "pack 'ghost'" in result.output


def test_regression_pack_rehearsal_keeps_qualified_stats_and_history(
    mocker, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "conductor"))
    from conductor_fakes import write_pack

    write_pack(macros=("open-app",))
    _write("open-app")
    _patch_mcp(mocker)
    macro_stats.record(
        "open-app", ok=False, known_names={"open-app"}, reason="tool_error"
    )
    original = macro_stats.load()["open-app"]

    ran = runner.invoke(app, ["macros", "run", "demo/open-app", "-i", "message=hi"])
    history = runner.invoke(app, ["macros", "runs"])
    stats = macro_stats.load()

    assert ran.exit_code == 0, ran.output
    assert stats["open-app"] == original
    assert stats["demo/open-app"]["total_successes"] == 1
    assert "demo/open-app" in history.output


# ---------- init --app: a hand scaffolded into a pack or a route ----------


def test_init_app_scaffolds_into_the_pack_or_the_playbook(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "conductor"))
    from conductor_fakes import FLOW, write_pack

    root = write_pack(playbooks={"flow": FLOW})

    pack_level = runner.invoke(app, ["macros", "init", "shared", "--app", "demo"])
    route_level = runner.invoke(app, ["macros", "init", "mine", "--app", "demo/flow"])

    assert pack_level.exit_code == 0, pack_level.output
    assert route_level.exit_code == 0, route_level.output
    assert (root / "macros" / "shared.yml").is_file()
    assert (root / "flow" / "macros" / "mine.yml").is_file()
    assert "playbooks check" in route_level.output  # the pack's check, not the user one
    # Neither joins the user macros dir.
    assert not (paths.macros_dir() / "shared.yml").exists()


def test_init_app_refuses_an_unknown_pack_or_playbook(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "conductor"))
    from conductor_fakes import FLOW, write_pack

    write_pack(playbooks={"flow": FLOW})

    assert runner.invoke(app, ["macros", "init", "x", "--app", "ghost"]).exit_code == 1
    assert (
        runner.invoke(app, ["macros", "init", "x", "--app", "demo/none"]).exit_code == 1
    )


# History fixtures use the same envelope as RunLogger without running hardware.
RUN_ID = "macro-run-abcdef"


def _event(kind="start", **fields):
    return {
        "event": kind,
        "run": RUN_ID,
        "macro": "demo",
        "ts": "2026-10-15T12:34:56",
        **fields,
    }


def _step(**fields):
    return _event("step", **{"i": 1, "tool": "tap", "outcome": "ok", **fields})


def _write_history(records, run_id=RUN_ID):
    import json

    directory = paths.macros_log_dir() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "events.jsonl"
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_regression_unicode_jsonl_preserves_recorded_step(separator):
    _write_history(
        [_event(), _step(screen_text=f"alpha{separator}beta"), _event("end", ok=True)]
    )

    result = runner.invoke(app, ["macros", "runs", "abcdef"])

    assert result.exit_code == 0
    assert "1 step(s)" in result.output
    assert "1. tap" in result.output
    assert f"alpha{separator}beta" in result.output


@pytest.mark.parametrize(
    "record",
    [
        None,
        [],
        1,
        True,
        "bad",
        {},
        {"event": "step"},
        _step(i="one"),
        _step(i=True),
        _step(tool=[]),
        _step(outcome={}),
        _step(args=[]),
        _step(screen_text=123),
        _step(at=1),
        _event(ts=123),
        _step(run="../../outside"),
        _event("future"),
    ],
)
def test_regression_malformed_history_record_does_not_hide_valid_steps(record):
    _write_history([_event(), record, _step(), _event("end", ok=True)])

    result = runner.invoke(app, ["macros", "runs", "abcdef"])

    assert result.exit_code == 0
    assert "1 step(s)" in result.output
    assert "1. tap" in result.output


@pytest.mark.parametrize("ok", ["false", "true", 1, [], None])
def test_invalid_end_outcome_is_not_reported_as_success(ok):
    _write_history([_event(), _step(), _event("end", ok=ok)])

    result = runner.invoke(app, ["macros", "runs"])

    assert result.exit_code == 0
    assert "incomplete" in result.output


@pytest.mark.parametrize("n", ["0", "-1"])
def test_history_count_must_be_positive(n):
    result = runner.invoke(app, ["macros", "runs", "-n", n])

    assert result.exit_code == 2
    assert "Invalid value" in result.output


def test_history_limit_counts_readable_runs_instead_of_empty_directories():
    import os

    old = _write_history([_event(), _step(), _event("end", ok=True)])
    newer = _write_history([], run_id="macro-run-ffffff")
    os.utime(old.parent, (10, 10))
    os.utime(newer.parent, (20, 20))

    result = runner.invoke(app, ["macros", "runs", "-n", "1"])

    assert result.exit_code == 0
    assert RUN_ID in result.output


def test_history_directory_permission_error_is_clean_cli_error(mocker):
    from pathlib import Path

    paths.macros_log_dir().mkdir(parents=True)
    mocker.patch.object(Path, "iterdir", side_effect=PermissionError("history blocked"))

    result = runner.invoke(app, ["macros", "runs"])

    assert result.exit_code == 1
    assert "history blocked" in result.output
    assert isinstance(result.exception, SystemExit)


def test_torn_utf8_record_does_not_hide_later_history():
    path = _write_history([_event(), _step(), _event("end", ok=True)])
    path.write_bytes(b"\xff\n" + path.read_bytes())

    result = runner.invoke(app, ["macros", "runs", "abcdef"])

    assert result.exit_code == 0
    assert "1. tap" in result.output


def test_absent_failure_reason_is_rendered_as_aborted():
    _write_history([_event(), _event("end", ok=False, reason=None)])

    result = runner.invoke(app, ["macros", "runs"])

    assert result.exit_code == 0
    assert "aborted" in result.output
    assert "None" not in result.output


def test_legacy_null_optional_text_fields_do_not_crash_history():
    _write_history(
        [
            _event(ts=None, caller=None),
            _step(at=None, screen_text=None, args=None),
            _event("end", ok=True),
        ]
    )

    result = runner.invoke(app, ["macros", "runs", "abcdef"])

    assert result.exit_code == 0
    assert "1. tap" in result.output


def test_macro_init_filesystem_error_is_clean_cli_error(mocker):
    from pathlib import Path

    mocker.patch.object(Path, "mkdir", side_effect=PermissionError("scaffold blocked"))

    result = runner.invoke(app, ["macros", "init", "demo"])

    assert result.exit_code == 1
    assert "scaffold blocked" in result.output
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("target", ["/flow", "demo/flow/extra"])
def test_init_rejects_malformed_pack_target(target):
    result = runner.invoke(app, ["macros", "init", "demo", "--app", target])

    assert result.exit_code == 1
    assert "--app takes <app> or <app>/<playbook>" in result.output


def test_check_empty_directory_reports_no_macros():
    result = runner.invoke(app, ["macros", "check"])

    assert result.exit_code == 0
    assert "No macros found" in result.output


def test_check_reports_disabled_dependency_without_disabling_parent():
    _write(
        "child",
        "kind: macro\nschema: 1\nname: child\ndescription: d\nenabled: false\nsteps:\n  - home_screen\n",
    )
    _write(
        "parent",
        "kind: macro\nschema: 1\nname: parent\ndescription: d\nsteps:\n  - run: child\n",
    )

    result = runner.invoke(app, ["macros", "check"])

    assert result.exit_code == 0
    assert "parent" in result.output
    assert "enable that first" in result.output


@pytest.mark.parametrize("run_id", ["abcdef", RUN_ID])
def test_history_detail_preserves_nested_step_and_abort_evidence(run_id):
    _write_history(
        [
            _event(start_at="resume"),
            _step(
                at="3.2",
                name="target",
                outcome="guard_failed",
                verdict="unchanged",
                detail="target absent",
                image="2.jpg",
            ),
            _event("end", ok=False, reason="guard_failed", detail="stopped safely"),
        ]
    )

    result = runner.invoke(app, ["macros", "runs", run_id])

    assert result.exit_code == 0
    assert "from 'resume'" in result.output
    assert (
        "    ✗ 3.2. tap 'target' [guard_failed] 0ms — unchanged — target absent — images/2.jpg"
        in result.output
    )
    assert "end: guard_failed — stopped safely" in result.output


@pytest.mark.parametrize(("ok", "label"), [(True, "ok"), (False, "aborted")])
def test_history_end_detail_without_reason_uses_outcome_label(ok, label):
    _write_history([_event(), _event("end", ok=ok, detail="saved evidence")])

    result = runner.invoke(app, ["macros", "runs", "abcdef"])

    assert result.exit_code == 0
    assert f"end: {label} — saved evidence" in result.output


def _write_ordered_runs(count=4):
    import os

    for index in range(count):
        run_id = f"macro-run-abc{index:03x}"
        path = _write_history([_event(run=run_id)], run_id=run_id)
        os.utime(path.parent, (index + 1, index + 1))


def test_history_ambiguous_prefix_displays_three_newest_matches():
    _write_ordered_runs()

    result = runner.invoke(app, ["macros", "runs", "abc"])

    assert result.exit_code == 0
    assert "4 runs match" in result.output
    assert (
        result.output.index("macro-run-abc003")
        < result.output.index("macro-run-abc002")
        < result.output.index("macro-run-abc001")
    )
    assert "macro-run-abc000" not in result.output


def test_history_limit_reads_exactly_requested_number_of_valid_runs():
    _write_ordered_runs()

    result = runner.invoke(app, ["macros", "runs", "-n", "2"])

    assert result.exit_code == 0
    assert "macro-run-abc003" in result.output
    assert "macro-run-abc002" in result.output
    assert "macro-run-abc001" not in result.output


def test_history_ignores_nonrun_directories_and_unreadable_event_paths():
    path = _write_history([_event(), _step()])
    (path.parent.parent / "other").mkdir()
    bad = path.parent.parent / "macro-run-fffffe" / "events.jsonl"
    bad.mkdir(parents=True)

    result = runner.invoke(app, ["macros", "runs"])

    assert result.exit_code == 0
    assert RUN_ID in result.output
    assert "fffffe" not in result.output


def test_history_survives_directory_stat_race(mocker):
    from pathlib import Path

    path = _write_history([_event(), _step()])
    original = Path.stat

    def stat(p, *args, **kwargs):
        if p == path.parent:
            raise FileNotFoundError("purged")
        return original(p, *args, **kwargs)

    mocker.patch.object(Path, "stat", stat)

    result = runner.invoke(app, ["macros", "runs"])

    assert result.exit_code == 0
    assert RUN_ID in result.output


@pytest.mark.parametrize("field", ["macro", "tool", "outcome"])
def test_history_rejects_empty_required_names(field):
    _write_history([_event(), _step(**{field: ""}), _step()])

    result = runner.invoke(app, ["macros", "runs", "abcdef"])

    assert result.exit_code == 0
    assert "1 step(s)" in result.output


def test_stats_success_without_abort_shows_no_abort_detail():
    macro_stats.record("demo", ok=True, known_names={"demo"})

    result = runner.invoke(app, ["macros", "stats"])

    assert result.exit_code == 0
    assert "demo: 1/1 ok, 0 abort(s), streak 0" in result.output
    assert "last abort" not in result.output


def test_stats_abort_preserves_run_reference():
    macro_stats.record("demo", ok=False, known_names={"demo"}, run_id=RUN_ID)

    result = runner.invoke(app, ["macros", "stats"])

    assert result.exit_code == 0
    assert RUN_ID in result.output


def test_run_reports_returned_image_count(mocker):
    class ImageMcp(FakeMcp):
        async def call_tool(self, name, args=None):
            return [
                *await super().call_tool(name, args),
                {"type": "image", "data": "", "mime_type": "image/png"},
            ]

    _write("demo")
    _patch_mcp(mocker, ImageMcp)

    result = runner.invoke(app, ["macros", "run", "demo", "-i", "msg=hi"])

    assert result.exit_code == 0
    assert "(1 image(s) in the result" in result.output


_json_text = st.text(st.characters(blacklist_categories=("Cs",)), max_size=30)
_json_value = st.recursive(
    st.none() | st.booleans() | st.integers() | _json_text,
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(_json_text, inner, max_size=3)
    ),
    max_leaves=10,
)


@settings(max_examples=60, deadline=None)
@given(
    fields=st.dictionaries(
        st.sampled_from(
            [
                "event",
                "run",
                "macro",
                "ts",
                "caller",
                "start_at",
                "i",
                "tool",
                "outcome",
                "args",
                "at",
                "name",
                "verdict",
                "detail",
                "image",
                "screen_text",
                "reason",
                "ok",
            ]
        ),
        _json_value,
        max_size=6,
    )
)
def test_history_arbitrary_record_fields_cannot_hide_later_valid_step(fields):
    _write_history([_event(), {**_step(), **fields}, _step(tool="home_screen")])

    result = runner.invoke(app, ["macros", "runs", "abcdef"])

    assert result.exit_code == 0
    assert "1. home_screen" in result.output


def test_macros_without_subcommand_displays_help():
    result = runner.invoke(app, ["macros"])

    assert result.exit_code == 2
    assert "Commands" in result.output
    assert "Scaffold a new macro" in result.output


def test_list_includes_macro_description_step_count_and_parse_error():
    _write("good")
    _write(
        "bad",
        "kind: macro\nschema: 1\nname: other\ndescription: d\nsteps:\n  - home_screen\n",
    )

    result = runner.invoke(app, ["macros", "list"])

    assert result.exit_code == 0
    assert "Demo macro (1 steps)" in result.output
    assert "must equal the file name" in result.output


def test_run_lookup_hint_resolves_the_same_run(mocker):
    _write("demo")
    _patch_mcp(mocker)

    ran = runner.invoke(app, ["macros", "run", "demo", "-i", "msg=hi"])
    hint = re.search(r"physiclaw macros runs (\S+)", ran.output).group(1)
    replayed = runner.invoke(app, ["macros", "runs", hint])

    assert ran.exit_code == replayed.exit_code == 0
    assert re.fullmatch(r"[a-f0-9]{6}", hint)
    assert "1. send_to_clipboard" in replayed.output


def test_default_history_count_is_ten():
    _write_ordered_runs(11)

    result = runner.invoke(app, ["macros", "runs"])

    assert result.exit_code == 0
    assert len(result.output.splitlines()) == 10
    assert "macro-run-abc00a" in result.output
    assert "macro-run-abc000" not in result.output


@pytest.mark.parametrize(("count", "ambiguous"), [(1, False), (2, True)])
def test_history_ambiguity_requires_more_than_one_match(count, ambiguous):
    _write_ordered_runs(count)

    result = runner.invoke(app, ["macros", "runs", "abc"])

    assert result.exit_code == 0
    assert ("ambiguous" in result.output) == ambiguous


def test_history_skipped_steps_keep_marker_and_duration():
    _write_history([_event(), _step(outcome="skipped", ms=37)])

    result = runner.invoke(app, ["macros", "runs", "abcdef"])

    assert result.exit_code == 0
    assert "  ↷ 1. tap [skipped] 37ms" in result.output


def test_history_arguments_remain_readable_unicode_flow_values():
    _write_history(
        [_event(), _step(args={"label": "综合", "bbox": [0.1, 0.2, 0.3, 0.4]})]
    )

    result = runner.invoke(app, ["macros", "runs", "abcdef"])

    assert result.exit_code == 0
    assert 'args: label: "综合", bbox: [0.1, 0.2, 0.3, 0.4]' in result.output


@pytest.mark.parametrize(
    "field",
    ["ts", "caller", "start_at", "name", "verdict", "detail", "image", "reason"],
)
def test_history_wrong_optional_text_type_is_skipped(field):
    _write_history([_event(), _step(**{field: 23}), _step()])

    result = runner.invoke(app, ["macros", "runs", "abcdef"])

    assert result.exit_code == 0
    assert "1 step(s)" in result.output


def test_history_rejects_zero_step_index():
    _write_history([_event(), _step(i=0), _step()])

    result = runner.invoke(app, ["macros", "runs", "abcdef"])

    assert result.exit_code == 0
    assert "1 step(s)" in result.output


@pytest.mark.parametrize(
    ("ok", "reason", "state"),
    [
        (True, None, "✓ ok"),
        (False, "tool_error", "✗ tool_error"),
        (False, None, "✗ aborted"),
    ],
)
def test_history_summary_preserves_timestamp_and_final_outcome(ok, reason, state):
    _write_history([_event(caller="cli"), _event("end", ok=ok, reason=reason)])

    result = runner.invoke(app, ["macros", "runs"])

    assert result.exit_code == 0
    assert result.output.split() == [
        RUN_ID,
        "2026-10-15T12:34",
        "demo",
        "cli",
        *state.split(),
        "0",
        "step(s)",
    ]


def test_run_records_cli_caller_for_history_consumers(mocker):
    import json

    _write("demo")
    _patch_mcp(mocker)

    result = runner.invoke(app, ["macros", "run", "demo", "-i", "msg=hi"])
    run_id = re.search(r"macro-run-[a-f0-9]{6}", result.output).group()
    start = json.loads(
        (paths.macros_log_dir() / run_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )

    assert result.exit_code == 0
    assert start["caller"] == "cli"


def test_stats_last_abort_preserves_recorded_timestamp():
    from freezegun import freeze_time

    with freeze_time("2026-10-15 12:34:56"):
        macro_stats.record("demo", ok=False, known_names={"demo"})

    result = runner.invoke(app, ["macros", "stats"])

    assert result.exit_code == 0
    assert "(2026-10-15T12:34:56)" in result.output
