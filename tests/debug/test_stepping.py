"""Tests for `physiclaw.debug.stepping` — the driver behind
`playbooks step` and the studio's playbook panel: the catalog the
panel lists, the seeded-output fast-forward, and the JSON the CLI
prints for an agent's loop. The CLI's own flag tests live beside the
conductor tests (`tests/conductor/test_step_cli.py`)."""

# Accepted mutation survivors from the stepping audit:
# - Diagnostic XX decorations/separators retain the useful error and recovery
#   text (46, 47, 62-65, 68, 76, 77, 156-158, 203, 208, 209, 211, 215, 242,
#   253, 256, 261, 262; catalogue patch mutants 7 and 15).
# - Removing frozen from the three projections does not affect their consumers,
#   which never reassign fields (27, 49, 124).
# - Reloading the staged queue instead of reusing the catalogue snapshot adds
#   one read; no atomic cross-file snapshot is promised (94).
# - The initial Program only provides state(), so its dry flag has no effect
#   before any advance (141). None and an empty string both clear a round (169).
# - Verbose result rendering sees no listing after action_text strips it (271).
#   The observation ID is an opaque label (275), and the no-result fallback is
#   unreachable with the production runner's mandatory result header (280).
# The unused logger was removed rather than retaining its equivalent mutant.

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "conductor"))
from conductor_fakes import make_screen, write_pack  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from physiclaw.cli import app  # noqa: E402
from physiclaw.common import paths  # noqa: E402
from physiclaw.conductor.spec import model  # noqa: E402
from physiclaw.debug import stepping  # noqa: E402

runner = CliRunner()
HOME = make_screen(("Files", 0.5, 0.1)).text


def _user_macro(name, steps="  - home_screen\n"):
    from physiclaw.macros import store

    path = store.init_macro(name)
    path.write_text(
        f"kind: macro\nschema: 1\nname: {name}\ndescription: test\nsteps:\n{steps}",
        encoding="utf-8",
    )
    return path


FLOW = """\
description: parse then move
inputs:
  keyword:
    description: what to search
route:
  - agent: parse
    context:
      prompt: "a keyword for {keyword}"
      given: {keyword: "{inputs.keyword}"}
    returns:
      keyword: the keyword
  - page: app.pages.home
  - do: open
    macro: app.macros.open-app
    with: {message: "{parse.keyword}"}
  - page: app.pages.home
  - do: search
    macro: app.macros.add-cart
    with: {message: "go"}
  - page: app.pages.results
"""


class _FakeMcp:
    def __init__(self, *a, **kw):
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call_tool(self, name, args=None):
        self.calls.append((name, args or {}))
        return [{"type": "image", "data": "x"}, {"type": "text", "text": HOME}]


@pytest.fixture()
def pack(mocker):
    write_pack(playbooks={"flow": FLOW}, macros=("open-app", "add-cart"))
    mocker.patch("physiclaw.agent.engine.mcp_tool.McpClient", _FakeMcp)
    mocker.patch(
        "physiclaw.macros.runner.run_and_record",
        new=mocker.AsyncMock(
            return_value=mocker.Mock(
                ok=True,
                blocks=[
                    {
                        "type": "text",
                        "text": "ran",
                        "_meta": {"physiclaw/macro_ok": True},
                    },
                    {"type": "text", "text": HOME},
                ],
            )
        ),
    )
    calls: list = []

    def no_model(rlog=None):
        calls.append(1)
        raise RuntimeError("no model configured")

    mocker.patch("physiclaw.conductor.drive.rehearsal.micro_caller", no_model)
    return calls


def test_catalog_lists_inputs_route_and_position(pack) -> None:
    (demo,) = stepping.catalog()

    assert demo["app"] == "demo"
    (flow,) = demo["playbooks"]
    assert flow["name"] == "flow" and flow["enabled"] is True
    assert [i["name"] for i in flow["inputs"]] == ["keyword"]
    assert [(n["id"], n["kind"]) for n in flow["nodes"]] == [
        ("parse", "agent"),
        ("open", "do"),
        ("search", "do"),
    ]
    assert flow["nodes"][1]["enter"] == "home" and flow["position"] is None


def test_seeded_output_settles_the_pure_text_node_without_a_call(pack) -> None:
    result = runner.invoke(
        app,
        [
            "playbooks",
            "step",
            "demo/flow",
            "-i",
            "keyword=milk",
            "-o",
            "parse.keyword=milk",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert pack == []  # the model was never asked
    out = json.loads(result.stdout)  # stdout is the JSON alone under --json
    assert out["outcome"] == "paused"
    assert out["position"]["idx"] == 2 and out["position"]["outputs"] == {
        "parse.keyword": "milk"
    }
    assert stepping.catalog()[0]["playbooks"][0]["position"]["idx"] == 2


def test_an_explicit_at_reruns_a_settled_node(pack) -> None:
    runner.invoke(
        app,
        [
            "playbooks",
            "step",
            "demo/flow",
            "-i",
            "keyword=milk",
            "-o",
            "parse.keyword=milk",
        ],
    )

    result = runner.invoke(app, ["playbooks", "step", "demo/flow", "--at", "parse"])

    assert result.exit_code == 1 and "no model configured" in result.output
    assert pack == [1]


def test_status_json_reports_none_before_the_first_step(pack) -> None:
    result = runner.invoke(
        app, ["playbooks", "step", "demo/flow", "--status", "--json"]
    )

    assert result.exit_code == 0 and json.loads(result.output) is None


# ---------- macros: the catalog, the resolver, one gesture ----------


def test_macro_catalog_lists_user_and_pack_macros_with_steps(pack) -> None:
    from physiclaw.macros import store as macro_store

    macro_store.init_macro("mine")
    items = {m["name"]: m for m in stepping.macro_catalog()}

    assert items["mine"]["source"] == "user" and items["mine"]["enabled"] is False
    assert items["demo/open-app"]["source"] == "demo"
    assert [(s["name"], s["tool"]) for s in items["demo/open-app"]["steps"]] == [
        ("idx1-tap-t", "tap")
    ]
    assert items["demo/open-app"]["steps"][0]["detail"] == "t"
    assert [i["name"] for i in items["demo/open-app"]["inputs"]] == ["message"]


def test_find_macro_resolves_pack_and_user_names(pack) -> None:
    from physiclaw.macros.model import MacroError

    assert stepping.find_macro("demo/open-app").name == "open-app"
    with pytest.raises(MacroError, match="no macro 'demo/nope'") as missing:
        stepping.find_macro("demo/nope")
    assert "it has: demo/add-cart, demo/open-app" in str(missing.value)
    with pytest.raises(MacroError, match="pack 'ghost'"):
        stepping.find_macro("ghost/x")
    with pytest.raises(MacroError, match="no macro named 'mine'"):
        stepping.find_macro("mine")


def test_catalog_reports_bad_pack_and_skips_empty_pack():
    bad = write_pack(app="broken")
    (bad / "APP.yml").write_text("[]", encoding="utf-8")
    write_pack(app="empty")
    write_pack(app="zgood", playbooks={"flow": FLOW}, macros=("open-app", "add-cart"))

    items = stepping.catalog()

    assert [item["app"] for item in items] == ["broken", "zgood"]
    assert items[0]["error"] and items[0]["playbooks"] == []
    assert items[1]["playbooks"][0]["description"] == "parse then move"


def test_catalog_keeps_invalid_playbook_error_visible():
    write_pack(playbooks={"broken": "description: invalid\nroute: []\n"})

    (pack,) = stepping.catalog()

    (entry,) = pack["playbooks"]
    assert entry["name"] == "broken" and entry["enabled"] is False
    assert entry["error"] and "nodes" not in entry


@pytest.mark.parametrize(
    "saved",
    [
        {},
        {"idx": "bad"},
        {"idx": float("inf")},
        {"idx": -1},
        {"idx": 4},
        {"idx": 0, "values": 3},
        {"idx": 0, "outputs": 3},
    ],
)
def test_regression_catalog_isolates_invalid_saved_position(pack, saved):
    write_pack(app="other", playbooks={"flow": FLOW}, macros=("open-app", "add-cart"))
    paths.debug_dir().mkdir(parents=True, exist_ok=True)
    state = {"schema": 1, "app": "demo", "playbook": "flow", **saved}
    stepping.save_state(state)
    before = stepping.state_path().read_bytes()

    demo, other = stepping.catalog()

    entry = demo["playbooks"][0]
    assert entry["error"] is None and entry["enabled"] is True
    assert entry["position"] is None
    assert "Saved position is invalid" in entry["position_error"]
    assert "Reset" in entry["position_error"]
    assert other["app"] == "other" and other["playbooks"][0]["position"] is None
    assert "position_error" not in other["playbooks"][0]
    assert stepping.state_path().read_bytes() == before


@pytest.mark.parametrize("idx", [0, 3])
def test_catalog_accepts_route_boundary_positions(pack, idx):
    paths.debug_dir().mkdir(parents=True, exist_ok=True)
    stepping.save_state({"schema": 1, "app": "demo", "playbook": "flow", "idx": idx})

    (demo,) = stepping.catalog()

    entry = demo["playbooks"][0]
    assert entry["position"]["idx"] == idx and "position_error" not in entry


def test_macro_catalog_reports_bad_pack_and_macro():
    bad = write_pack(app="broken")
    (bad / "APP.yml").write_text("[]", encoding="utf-8")
    _user_macro("invalid").write_text("[]", encoding="utf-8")
    write_pack(app="zgood", macros=("open-app",))

    items = {item["name"]: item for item in stepping.macro_catalog()}

    assert set(items) == {"broken", "invalid", "zgood/open-app"}
    assert items["broken"]["error"] and items["invalid"]["error"]
    assert items["broken"]["enabled"] is False and items["invalid"]["enabled"] is False


def test_macro_catalog_preserves_input_and_macro_descriptions():
    text = (Path(__file__).parents[1] / "data/debug/catalog-macro.yml").read_text()
    _user_macro("example").write_text(text, encoding="utf-8")

    (item,) = stepping.macro_catalog()

    assert item["description"] == "catalogue example"
    assert item["inputs"] == [
        {
            "name": "message",
            "description": "text to send",
            "default": "default message",
            "example": "example message",
        }
    ]


def test_find_user_macro_skips_other_entries_and_returns_valid_spec():
    _user_macro("a-first")
    _user_macro("z-target")

    spec = stepping.find_macro("z-target")

    assert spec.name == "z-target"


@pytest.mark.parametrize("scope", ["user", "pack"])
def test_find_invalid_macro_reports_its_parse_error(scope):
    from physiclaw.macros.model import MacroError

    root = write_pack(macros=("bad",))
    user = _user_macro("bad")
    target = {"user": user, "pack": root / "macros" / "bad.yml"}[scope]
    target.write_text("[]", encoding="utf-8")
    name = {"user": "bad", "pack": "demo/bad"}[scope]

    with pytest.raises(MacroError, match=name):
        stepping.find_macro(name)


@pytest.mark.parametrize(
    "steps, expected",
    [
        ("  - tap: [First, Second]\n    at: [0.1, 0.1, 0.2, 0.2]\n", "First / Second"),
        ("  - wait: 2\n", "2s"),
        ("  - send_to_clipboard: hello\n", "hello"),
    ],
)
def test_macro_catalog_renders_meaningful_step_details(steps, expected):
    _user_macro("example", steps)

    (item,) = stepping.macro_catalog()

    assert item["steps"][0]["detail"] == expected


@pytest.mark.parametrize(
    "node, kind, macro, enter, verify",
    [
        (
            model.DoNode("open", "open-app", {}, "", "home"),
            "start",
            "demo/open-app",
            "",
            "home",
        ),
        (
            model.DoNode("move", "pay", {}, "home", "done", "payment"),
            "do",
            "demo/pay",
            "home",
            "done",
        ),
        (
            model.AskNode(
                "approve",
                "pay",
                "ask?",
                ("yes",),
                ("no",),
                resume="open-app",
                enter="home",
            ),
            "ask",
            "demo/open-app",
            "home",
            "",
        ),
        (
            model.AskNode("approve", "pay", "ask?", ("yes",), ("no",)),
            "ask",
            None,
            "",
            "",
        ),
        (model.SelectNode("choose", "home", 2), "select", None, "home", ""),
        (model.TellNode("report", "done"), "tell", None, "", ""),
        (
            model.RunNode(
                "round",
                {},
                model.Playbook(
                    "demo", "leg", "leg", True, (), (model.TellNode("done", "done"),)
                ),
                "home",
                "done",
            ),
            "run",
            None,
            "home",
            "done",
        ),
    ],
)
def test_node_info_preserves_node_kind_pages_and_macro_namespace(
    node, kind, macro, enter, verify
):
    info = stepping.node_info("demo", node)

    assert info.to_dict() == {
        "id": node.id,
        "kind": kind,
        "enter": enter,
        "verify": verify,
        "irreversible": getattr(node, "irreversible", None),
        "macro": macro,
    }


def test_position_derives_missing_label_and_copies_state(pack):
    from physiclaw.conductor.load.pack import load_spec

    spec, _ = load_spec("demo", "flow")
    state = {
        "idx": 1,
        "values": {"keyword": "milk"},
        "outputs": {"parse.keyword": "milk"},
        "awaiting": True,
        "quoted": 4.5,
        "consented": 4.5,
    }
    staged = ["yes"]

    position = stepping.position(spec, state, staged)
    wire = position.to_dict()
    wire["values"]["keyword"] = "changed"
    wire["outputs"].clear()
    wire["staged"].clear()

    assert position.node == "open (2/3)" and position.idx == 1
    assert position.values == state["values"] == {"keyword": "milk"}
    assert position.outputs == state["outputs"] == {"parse.keyword": "milk"}
    assert position.staged == staged == ["yes"]
    assert "awaiting=True quoted=4.5 consented=4.5" in position.describe()


def test_position_wire_contract_includes_empty_default_queue():
    position = stepping.Position(
        "demo",
        "flow",
        2,
        "finish (3/3)",
        {"input": "value"},
        {"output": "value"},
        True,
        4.5,
        4.0,
    )

    wire = position.to_dict()

    assert wire == {
        "app": "demo",
        "playbook": "flow",
        "idx": 2,
        "node": "finish (3/3)",
        "values": {"input": "value"},
        "outputs": {"output": "value"},
        "awaiting": True,
        "quoted": 4.5,
        "consented": 4.0,
        "staged": [],
    }


def test_status_reads_existing_walk_filename_and_keeps_round_label(pack):
    state = {
        "schema": 1,
        "app": "demo",
        "playbook": "flow",
        "idx": 1,
        "label": "leg: pay (2/2)",
        "values": {"keyword": "milk"},
    }
    paths.debug_dir().mkdir(parents=True, exist_ok=True)
    (paths.debug_dir() / "walk.json").write_text(json.dumps(state), encoding="utf-8")

    position = stepping.status("demo", "flow")

    assert position.node == "leg: pay (2/2)" and position.idx == 1


async def test_run_user_macro_defaults_to_its_own_recording_name():
    from physiclaw.macros import runlog, stats

    _user_macro("mine")
    seen = []

    result = await stepping.run_macro(
        stepping.find_macro("mine"),
        {},
        _FakeMcp(),
        emit=lambda _: None,
        observe=lambda call, blocks: seen.append(call.arguments["name"]),
    )

    assert result["ok"] is True and result["run_id"]
    assert set(stats.load()) == {"mine"} and seen == ["mine"]
    assert (
        json.loads(
            (runlog.run_dir(result["run_id"]) / "events.jsonl")
            .read_text()
            .splitlines()[0]
        )["caller"]
        == "cli"
    )


async def test_run_macro_bad_input_records_under_qualified_name_before_any_gesture():
    from physiclaw.macros import stats
    from physiclaw.macros.model import MacroError

    write_pack(app="alpha", macros=("open-app",))
    mcp = _FakeMcp()

    with pytest.raises(MacroError, match="step"):
        await stepping.run_macro(
            stepping.find_macro("alpha/open-app"),
            {},
            mcp,
            record_as="alpha/open-app",
            start_at="missing",
            emit=lambda _: None,
        )

    assert mcp.calls == []
    assert stats.load()["alpha/open-app"]["last_abort"]["reason"] == "bad_input"


async def test_run_macro_reports_abort_without_observer_and_keeps_qualified_history(
    mocker,
):
    from physiclaw.macros import stats

    write_pack(app="alpha", macros=("open-app",))
    mcp = _FakeMcp()
    mocker.patch.object(mcp, "call_tool", side_effect=RuntimeError("gesture failed"))

    result = await stepping.run_macro(
        stepping.find_macro("alpha/open-app"),
        {},
        mcp,
        record_as="alpha/open-app",
        emit=lambda _: None,
    )

    assert result["ok"] is False and "ABORTED" in result["message"]
    assert set(stats.load()) == {"alpha/open-app"}
    assert stats.load()["alpha/open-app"]["consecutive_aborts"] == 1


async def test_step_to_end_continues_until_a_node_needs_repair(pack):
    lines = []

    result = await stepping.step(
        "demo",
        "flow",
        _FakeMcp(),
        values={"keyword": "milk"},
        outputs={"parse.keyword": "milk"},
        to="end",
        emit=lines.append,
    )

    assert result.to_dict()["outcome"] == "stopped" and result.position.idx == 2
    assert result.to_dict()["message"] == result.message
    assert [line for line in lines if line.startswith("node ")] == [
        "node parse (1/3)",
        "node search (3/3)",
    ]


@pytest.mark.parametrize("target", [{"at": "missing"}, {"to": "missing"}])
@pytest.mark.parametrize("existing", [False, True])
async def test_regression_invalid_step_target_preserves_script_and_position(
    pack, target, existing
):
    from physiclaw.debug import thread as vthread

    vthread.seed("keep task", ["keep reply"])
    state = {
        "schema": 1,
        "app": "demo",
        "playbook": "flow",
        "idx": 0,
        "values": {"keyword": "milk"},
    }
    setups = {False: lambda: None, True: lambda: stepping.save_state(state)}
    setups[existing]()
    before = stepping.read_state()
    values = {False: {"keyword": "milk"}, True: {}}[existing]
    mcp = _FakeMcp()

    with pytest.raises(model.PlaybookError, match="no node 'missing'"):
        await stepping.step(
            "demo",
            "flow",
            mcp,
            values=values,
            reply="must not be staged",
            emit=lambda _: None,
            **target,
        )

    assert mcp.calls == []
    assert stepping.read_state() == before
    assert vthread.load() == vthread.Thread(
        [vthread.Bubble(vthread.USER, "keep task")], ["keep reply"]
    )


async def test_step_applies_gesture_range_and_unlock_preamble_once_per_invocation(
    pack, mocker
):
    from physiclaw.conductor.drive import rehearsal

    walk = mocker.spy(rehearsal, "walk")

    await stepping.step(
        "demo",
        "flow",
        _FakeMcp(),
        values={"keyword": "milk"},
        outputs={"parse.keyword": "milk"},
        to="end",
        start_at="one",
        stop_after="one",
        emit=lambda _: None,
    )

    assert [c.kwargs["macro_opts"] for c in walk.call_args_list] == [
        {"start_at": "one", "stop_after": "one"},
        {},
    ]
    assert [c.kwargs["unlock"] for c in walk.call_args_list] == [True, False]
    assert [(c.kwargs["raw"], c.kwargs["verbose"]) for c in walk.call_args_list] == [
        (False, False),
        (False, False),
    ]


async def test_step_default_range_runs_the_real_macro_runner():
    write_pack(playbooks={"flow": FLOW}, macros=("open-app", "add-cart"))
    mcp = _FakeMcp()

    result = await stepping.step(
        "demo",
        "flow",
        mcp,
        values={"keyword": "milk"},
        outputs={"parse.keyword": "milk"},
        emit=lambda _: None,
    )

    assert result.to_dict()["outcome"] == "paused" and result.position.idx == 2
    assert "tap" in [name for name, args in mcp.calls]


async def test_step_with_empty_input_keeps_reply_staged(pack):
    from physiclaw.debug import thread as vthread

    await stepping.step(
        "demo",
        "flow",
        _FakeMcp(),
        values={"keyword": ""},
        outputs={"parse.keyword": "milk"},
        reply="yes",
        emit=lambda _: None,
    )

    assert vthread.load() == vthread.Thread(
        [vthread.Bubble(vthread.USER, "(stepping demo/flow)")], ["yes"]
    )


async def test_jump_to_run_discards_previous_round_offset(pack):
    from conductor_fakes import EACH, LEG

    write_pack(
        playbooks={"flow": EACH, "flow.leg": LEG}, macros=("open-app", "add-cart")
    )
    paths.debug_dir().mkdir(parents=True, exist_ok=True)
    stepping.save_state(
        {
            "schema": 1,
            "app": "demo",
            "playbook": "flow",
            "idx": 1,
            "round": {"key": "milk", "at": 2},
            "values": {"keyword": "milk"},
            "outputs": {"parse.items": "milk"},
        }
    )

    result = await stepping.step(
        "demo", "flow", _FakeMcp(), at="leg", emit=lambda _: None
    )

    assert result.outcome == "paused"
    assert stepping.load_state("demo", "flow")["round"] == {"key": "milk", "at": 1}


async def test_step_to_named_node_includes_that_node(pack):
    result = await stepping.step(
        "demo",
        "flow",
        _FakeMcp(),
        values={"keyword": "milk"},
        outputs={"parse.keyword": "milk"},
        to="search",
        emit=lambda _: None,
    )

    assert result.outcome == "stopped" and result.position.idx == 2


async def test_step_suspends_an_unanswered_ask_and_persists_gate(pack, mocker):
    from conductor_fakes import write_channel

    from physiclaw.conductor.walk.suspension import suspended_path

    write_channel()
    write_pack(
        playbooks={
            "flow": (Path(__file__).parents[1] / "data/debug/asking.yml").read_text()
        },
        macros=("open-app",),
    )
    mocker.patch(
        "physiclaw.conductor.drive.rehearsal.asyncio.sleep", new=mocker.AsyncMock()
    )
    suspended_path().write_text("real wake pending", encoding="utf-8")

    result = await stepping.step(
        "demo", "flow", _FakeMcp(), to="end", emit=lambda _: None
    )

    assert result.to_dict()["outcome"] == "suspended"
    assert result.position.awaiting is True and result.position.idx == 1
    assert stepping.load_state("demo", "flow")["awaiting"] is True
    assert suspended_path().read_text() == "real wake pending"


async def test_run_macro_emits_the_step_log_and_shows_the_view(pack, mocker) -> None:
    from physiclaw.macros import runner as macro_runner

    lines: list[str] = []
    seen: list = []
    mcp = _FakeMcp()

    out = await stepping.run_macro(
        stepping.find_macro("demo/open-app"),
        {"message": "hi"},
        mcp,
        start_at="go",
        stop_after="go",
        emit=lines.append,
        observe=lambda call, blocks: seen.append((call.name, len(blocks))),
    )

    kwargs = macro_runner.run_and_record.await_args.kwargs
    assert kwargs["start_at"] == "go" and kwargs["stop_after"] == "go"
    assert out["ok"] is True and out["message"] == "ran"
    assert lines == ["ran", "(0 rows)"] or lines[0] == "ran"
    assert seen == [("run_macro", 2)]


async def test_regression_macro_history_and_observer_keep_qualified_names():
    from physiclaw.macros import runlog, stats

    write_pack(app="alpha", macros=("open-app",))
    write_pack(app="beta", macros=("open-app",))
    seen = []
    options = {
        "emit": lambda _: None,
        "observe": lambda call, blocks: seen.append(call.arguments["name"]),
        "caller": "studio",
    }

    first = await stepping.run_macro(
        stepping.find_macro("alpha/open-app"),
        {},
        _FakeMcp(),
        record_as="alpha/open-app",
        **options,
    )
    second = await stepping.run_macro(
        stepping.find_macro("beta/open-app"),
        {},
        _FakeMcp(),
        record_as="beta/open-app",
        **options,
    )

    assert first["ok"] and second["ok"]
    assert {name: row["total_runs"] for name, row in stats.load().items()} == {
        "alpha/open-app": 1,
        "beta/open-app": 1,
    }
    assert seen == ["alpha/open-app", "beta/open-app"]
    assert {
        json.loads(line)["macro"]
        for line in (runlog.run_dir(first["run_id"]) / "events.jsonl")
        .read_text()
        .splitlines()
    } == {"alpha/open-app"}
    assert {
        json.loads(line)["macro"]
        for line in (runlog.run_dir(second["run_id"]) / "events.jsonl")
        .read_text()
        .splitlines()
    } == {"beta/open-app"}


def test_at_abandons_a_pending_ask(pack) -> None:
    # A position left awaiting a reply, jumped away from: the gate must
    # not resume as a reply check on the wrong node.
    runner.invoke(
        app,
        [
            "playbooks",
            "step",
            "demo/flow",
            "-i",
            "keyword=milk",
            "-o",
            "parse.keyword=milk",
        ],
    )
    state = stepping.load_state("demo", "flow")
    stepping.save_state({**state, "awaiting": True, "yes": ["ok"], "no": ["no"]})

    result = runner.invoke(
        app, ["playbooks", "step", "demo/flow", "--at", "open", "--json"]
    )

    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)  # stdout is the JSON alone under --json
    assert out["outcome"] == "paused" and out["position"]["awaiting"] is False
    left = stepping.load_state("demo", "flow")
    assert left["yes"] == [] and left["no"] == [] and left["ask_text"] == ""


# ---------- the boot ----------


def test_catalog_lists_the_channel_boot_with_its_activate_step(pack) -> None:
    from conductor_fakes import write_channel

    write_channel(
        "kind: macro\nschema: 1\nname: open\ndescription: d\nsteps:\n  - home_screen\n"
    )
    from physiclaw.conductor.load import scaffold

    scaffold.ensure_channel_boot(paths.playbooks_dir() / "channel" / "wechat")

    (channel,) = [p for p in stepping.catalog() if p["app"] == "channel"]
    (boot,) = channel["playbooks"]
    assert boot["name"] == "boot"
    assert [(n["id"], n["kind"], n["enter"]) for n in boot["nodes"]] == [
        ("parse", "select", "thread")
    ]


def test_stepping_the_boot_reads_the_staged_reply_as_the_request(pack, mocker) -> None:
    # The boot has no inputs and reads the thread first: `--reply` is the
    # user's message there, seeded before the opening read — and the
    # activate step's parse_task runs over the virtual thread.
    from conductor_fakes import write_channel

    from physiclaw.conductor.load import scaffold
    from physiclaw.conductor.micro.decision import MicroOutcome, MicroResult
    from physiclaw.debug import thread as vthread

    write_channel(
        "kind: macro\nschema: 1\nname: open\ndescription: d\nsteps:\n  - home_screen\n"
    )
    scaffold.ensure_channel_boot(paths.playbooks_dir() / "channel" / "wechat")
    seen: list = []

    class _Micro:
        async def run(self, req):
            seen.append(req)
            outcome = MicroOutcome(out="not_a_task", reason="chat", confidence=0.9)
            return MicroResult(outcome, "chat", 1, 1)

        async def aclose(self):
            return None

    mocker.patch(
        "physiclaw.conductor.drive.rehearsal.micro_caller", lambda rlog=None: _Micro()
    )

    result = runner.invoke(
        app, ["playbooks", "step", "channel/boot", "--reply", "买牛奶", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert vthread.load().bubbles[0].text == "买牛奶" and vthread.load().staged == []
    assert (
        len(seen) == 1 and seen[0].call == "parse_task" and "买牛奶" in seen[0].listing
    )
    assert json.loads(result.stdout)["outcome"] == "completed"
