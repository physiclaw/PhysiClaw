"""Tests for `physiclaw.conductor.bench.replay` (behind `playbooks replay`) —
the real walk over recorded screens, writing nothing."""

from __future__ import annotations

import json

from conductor_fakes import (
    DONE,
    ELSEWHERE,
    FLOW,
    HOME,
    RESULTS,
    build_program,
    make_screen,
    write_channel,
    write_pack,
)
from typer.testing import CliRunner

from physiclaw.cli.playbooks import playbooks_app
from physiclaw.common import paths
from physiclaw.conductor.bench import replay
from physiclaw.conductor.walk import suspension, walklog

AGENT_FLOW = """\
description: parse then walk
inputs:
  keyword:
    description: what to search
route:
  - agent: parse
    context:
      prompt: "Turn {keyword} into a search term"
      given: {keyword: "{inputs.keyword}"}
    returns:
      term: the search term
  - page: app.pages.home
  - do: open
    macro: app.macros.open-app
    with: {message: "{parse.term}"}
  - page: app.pages.results
"""

ACTING_FLOW = """\
description: an agent that acts on the screen
inputs:
  keyword:
    description: what to search
route:
  - page: app.pages.home
  - agent: pick
    context:
      prompt: "Pick the cheapest of {keyword}"
      given: {keyword: "{inputs.keyword}"}
    tools: [tap, scroll]
    returns:
      summary: what was picked
    limit: {calls: 4, scrolls: 2}
    think: off
  - page: app.pages.results
"""

TELLING = """\
description: tell then stop
inputs:
  keyword:
    description: what
route:
  - page: app.pages.home
  - tell: note
    message: "starting {inputs.keyword}"
  - page: app.pages.home
  - do: open
    macro: app.macros.open-app
    with: {message: "{inputs.keyword}"}
  - page: app.pages.results
"""


def _dry(name: str = "flow", **values):
    return build_program(name=name, dry=True, **values)


def test_replay_walks_the_route_to_completion_and_writes_nothing() -> None:
    write_pack(playbooks={"flow": FLOW})

    result = replay.replay(_dry(keyword="milk"), [HOME, RESULTS, DONE])

    assert result.outcome == "completed"
    assert [t.tool for t in result.turns] == [
        "peek",
        "run_macro",
        "run_macro",
        "end_session",
    ]
    assert [t.node for t in result.turns] == ["open", "open", "search", None]
    assert result.turns[1].verdict == "match demo.home"
    assert not walklog.runs_file().exists()


def test_replay_reports_where_the_walk_hands_over() -> None:
    write_pack(playbooks={"flow": FLOW})

    result = replay.replay(_dry(keyword="milk"), [HOME, ELSEWHERE])

    assert result.outcome == "handover"
    assert "did not land on 'results'" in result.detail
    assert result.turns[-1].node == "open"


def test_replay_stops_when_screens_run_out() -> None:
    write_pack(playbooks={"flow": FLOW})

    result = replay.replay(_dry(keyword="milk"), [HOME])

    assert result.outcome == "stopped" and "screens exhausted" in result.detail


def test_replay_answers_a_text_agent_from_outputs_or_stops() -> None:
    write_pack(playbooks={"flow": AGENT_FLOW})

    stopped = replay.replay(_dry(keyword="milk"), [HOME, RESULTS])
    assert stopped.outcome == "stopped" and "agent 'parse'" in stopped.detail

    answered = replay.replay(
        _dry(keyword="milk"), [HOME, RESULTS], {"parse.term": "五常大米"}
    )
    assert answered.outcome == "completed"
    assert answered.turns[1].tool == "run_macro"


def test_replay_tell_sends_then_walks_on() -> None:
    # A tell is fire-and-forget: the send lands, the next move's enter
    # check reads the thread the send left the phone on (no recover
    # declared → handover), and a dry walk writes nothing.
    write_channel()
    write_pack(playbooks={"flow": TELLING})
    thread = make_screen(("MyChat", 0.5, 0.05), ("starting milk", 0.75, 0.3)).text

    result = replay.replay(_dry(keyword="milk"), [HOME, thread, HOME])

    assert [t.tool for t in result.turns] == ["peek", "run_macro", "peek"]
    assert result.turns[1].node == "note" and result.turns[2].node == "open"
    assert result.outcome == "handover" and "expects page 'home'" in result.detail
    assert not suspension.suspended_path().exists()


def test_replay_cli_reads_listing_files(tmp_path) -> None:
    write_pack(playbooks={"flow": FLOW})
    files = []
    for i, text in enumerate((HOME, RESULTS, DONE)):
        f = tmp_path / f"s{i}.txt"
        f.write_text(text, encoding="utf-8")
        files.append(str(f))
    args = ["replay", "demo/flow", "-i", "keyword=milk"]
    for f in files:
        args += ["--listing", f]

    out = CliRunner().invoke(playbooks_app, args)

    assert out.exit_code == 0, out.output
    assert "completed:" in out.output and "run_macro" in out.output


def test_regression_session_replay_preserves_repeated_screens():
    flow = FLOW.replace("app.pages.results", "app.pages.home")
    write_pack(playbooks={"flow": flow})
    sid = "20261114-000000-abcdef"
    directory = paths.sessions_dir() / sid
    directory.mkdir(parents=True)
    records = [
        {"event": "tool_result", "text": HOME},
        {"event": "tool_result", "text": HOME},
        {"event": "tool_result", "text": DONE},
    ]
    (directory / "events.jsonl").write_text(
        "\n".join(map(json.dumps, records)),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        playbooks_app, ["replay", "demo/flow", "-i", "keyword=milk", "--session", sid]
    )

    assert result.exit_code == 0, result.output
    assert "completed:" in result.output
    assert "handover:" not in result.output


def test_a_replay_stops_at_an_acting_agent_it_cannot_answer() -> None:
    # An acting episode's move is a model decision like a text agent's:
    # only a model can answer it, so the replay must STOP and say where,
    # not fall through and report a handover. The rule is read off the
    # call's own declaration (`micro.calltable.has_fallback`).
    from physiclaw.conductor.micro.calltable import has_fallback
    from physiclaw.conductor.micro.decision import (
        AGENT_ACT,
        AGENT_FIELDS,
        PARSE_TASK,
        READ_REPLY,
        SUMMARIZE,
    )

    write_pack(playbooks={"flow": ACTING_FLOW})

    stopped = replay.replay(_dry(keyword="milk"), [HOME, RESULTS])

    assert stopped.outcome == "stopped" and "agent 'pick'" in stopped.detail
    # Only the close: it writes the walk's own recap. A `parse_task`
    # resolved with None concludes "no playbook covers the thread",
    # which would make a boot replay report success without ever asking.
    assert has_fallback(SUMMARIZE)
    assert not any(
        has_fallback(c) for c in (AGENT_ACT, AGENT_FIELDS, PARSE_TASK, READ_REPLY)
    )
