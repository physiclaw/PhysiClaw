"""Tests for the `run` move — a playbook of the pack walked as one move,
once or once per item (`each`), with its `returns:` read downstream."""

from __future__ import annotations

import pytest
from conductor_fakes import (
    EACH,
    ELSEWHERE,
    HOME,
    LEG,
    PAGES,
    RESULTS,
    build_program,
    feed,
    finish,
    history,
    suspend_via_silence,
    thread_screen,
    write_channel,
    write_pack,
)

from physiclaw.conductor.drive import build, setup
from physiclaw.conductor.load import pack as pb
from physiclaw.conductor.load.pack import discover, load_spec
from physiclaw.conductor.micro.decision import READ_REPLY, DecisionRequest, MicroOutcome
from physiclaw.conductor.route import lints, playbook
from physiclaw.conductor.spec.limits import MAX_MESSAGE_LINES
from physiclaw.conductor.spec.live import disabled_macros, live_gap
from physiclaw.conductor.spec.model import PlaybookError, RunNode, TellNode
from physiclaw.conductor.spec.pack import qualified_inline
from physiclaw.conductor.walk.surface import Paused

FLOW = """\
kind: entry
schema: 1
name: flow
description: runs a leg then reports
inputs:
  keyword:
    description: what
route:
  - run: leg
    with: {what: "{inputs.keyword}"}
  - page: app.pages.results
  - tell: report
    message: "done: {leg.did}"
"""


def _pack(**playbooks: str):
    """The pack on disk, written by playbook id: `flow` is the entry and
    `flow.leg` one it runs, a file beside the entry's."""
    write_pack(playbooks={"flow.leg": LEG, **playbooks})
    return pb.load_pack("demo")


def _parse(text: str, name: str = "flow", **playbooks: str):
    """One playbook parsed against a pack holding it. A keyword spells
    the id's dot with an underscore (`flow_leg=`), which Python allows."""
    others = {k.replace("_", "."): v for k, v in playbooks.items()}
    return playbook.parse_playbook(text, name, _pack(**{name: text, **others}))


def test_a_run_is_a_move_framed_like_a_do() -> None:
    spec = _parse(FLOW)

    run, tell = spec.nodes
    assert isinstance(run, RunNode) and isinstance(tell, TellNode)
    assert run.id == "leg" and run.sub.name == "flow.leg"
    assert run.sub.run_by == "flow" and spec.run_by is None
    assert run.enter == "" and run.self_starting  # the leg cold-starts itself
    assert run.verify == "results"  # …and lands on the leg's last page
    assert run.args == {"what": "{inputs.keyword}"}
    assert run.each is None and run.miss is None and run.revise is None
    assert tell.message == "done: {leg.did}"  # the leg's return, wired forward


def test_a_playbook_declares_its_returns_over_its_own_refs() -> None:
    spec = _parse(LEG, name="flow.leg")

    assert spec.returns == {"did": "searched {inputs.what}"}
    assert spec.end == "results"
    with pytest.raises(PlaybookError, match="`returns.did`"):
        _parse(
            LEG.replace('"searched {inputs.what}"', '"searched {nope.x}"'),
            name="flow.leg",
        )


@pytest.mark.parametrize(
    "old, new, fragment",
    [
        # The route must continue with the leg's last page.
        (
            "  - page: app.pages.results\n  - tell",
            "  - page: app.pages.home\n  - tell",
            "lands on 'results'",
        ),
        # A `with` key must be an input of the leg.
        ("{what: ", "{whom: ", "not inputs of playbook 'flow.leg'"),
        # A required input must be filled.
        ('    with: {what: "{inputs.keyword}"}\n', "", "requires input"),
        # `miss` goes with `each`.
        (
            "  - page: app.pages.results\n  - tell",
            "    miss: skip\n  - page: app.pages.results\n  - tell",
            "`miss: skip` goes with `each`",
        ),
        # A run names a playbook beside the entry — never the entry itself…
        ("run: leg", "run: flow", "no playbook 'flow' of 'flow'"),
        # …nor one the entry does not have.
        (
            "run: leg",
            "run: wing",
            "no playbook 'wing' of 'flow' — it would be flow/wing.yml",
        ),
    ],
)
def test_run_shape_lints(old, new, fragment) -> None:
    assert FLOW.count(old) == 1, old
    with pytest.raises(PlaybookError, match=fragment):
        _parse(FLOW.replace(old, new))


def test_a_leg_that_ends_on_a_move_cannot_be_run() -> None:
    leg = LEG + '  - tell: bye\n    message: "bye"\n'
    with pytest.raises(PlaybookError, match="ends on a move"):
        _parse(FLOW, flow_leg=leg)


def test_only_an_entry_runs_a_playbook() -> None:
    # What an entry runs is a FILE: it has no folder for playbooks of
    # its own, so a run goes one level deep by shape, said at its `run`.
    nested = LEG.replace(
        "  - page: app.pages.results\n",
        "  - page: app.pages.results\n  - run: flow\n  - page: app.pages.results\n",
    )
    with pytest.raises(PlaybookError, match="only an entry runs a playbook"):
        _parse(FLOW, flow_leg=nested)


def test_a_leg_without_its_own_start_needs_the_page_before_the_run() -> None:
    leg = LEG.replace("  - start: app\n    macro: app.macros.open-app\n", "")
    with pytest.raises(PlaybookError, match="starts on page 'home'"):
        _parse(FLOW, flow_leg=leg)
    # …and with that page in place, it frames like a do: enter = home.
    spec = _parse(
        FLOW.replace("  - run: leg\n", "  - page: app.pages.home\n  - run: leg\n"),
        flow_leg=leg,
    )
    run = spec.nodes[0]
    assert isinstance(run, RunNode) and run.enter == "home" and not run.self_starting


def test_the_registry_and_the_readiness_rule_see_the_leg_too() -> None:
    pack = _pack(flow=FLOW)
    spec = playbook.parse_playbook(FLOW, "flow", pack)

    assert disabled_macros(spec, pack) == []
    assert qualified_inline("demo", spec) == {}


# ---------- the walk ----------


def _walk(flow: str = FLOW, leg: str = LEG, pay: str | None = None, **values):
    write_channel()
    write_pack(
        playbooks={"flow.leg": leg, "flow": flow, **({"flow.pay": pay} if pay else {})}
    )
    p = build_program(name="flow", **values)
    h = history()
    feed(h, p.advance(h), ELSEWHERE)  # the opening peek
    return p, h


def test_a_run_walks_the_leg_under_its_prefix_and_hands_its_return_forward() -> None:
    p, h = _walk(keyword="milk")

    start = p.advance(h)  # the leg's own cold start — the run's first node
    assert start.tool_calls[1].arguments["name"] == "demo/open-app"
    assert p.course.label() == "leg[]/app (1/3)"  # the leg's two moves, then the tell
    feed(h, start, HOME)
    search = p.advance(h)
    assert search.tool_calls[1].arguments == {
        "name": "demo/add-cart",
        "inputs": {"message": "milk"},  # the leg's input, from the run's `with`
    }
    feed(h, search, RESULTS)

    tell = p.advance(h)

    assert tell.tool_calls[1].arguments["inputs"]["message"] == "done: searched milk"
    assert p.ledger.rounds["leg[]"]["did"] == "searched milk"  # the round's record
    assert "leg[]" not in " ".join(p.ledger.decided)  # …apart from the route's own
    assert p.ref_values()["leg.did"] == "searched milk"  # …read as the run's return
    assert any("round leg[] done" in e for e in p.ledger.events)


ASKING_LEG = LEG.replace(
    "  - page: app.pages.results\n",
    "  - page: app.pages.results\n"
    "  - ask: go\n"
    "    approve: go\n"
    '    message: "go on with {inputs.what}?"\n'
    '    yes: ["好的"]\n'
    '    no: ["不用"]\n'
    "    resume: app.macros.add-cart\n"
    "  - page: app.pages.results\n",
)


def test_a_suspension_inside_a_round_resumes_inside_it() -> None:
    # The cursor stops at the leg's ask; the file stores the run's spec
    # index plus the round it was in, and the next wake expands the run
    # again and lands on that ask's reply check.
    p, h = _walk(leg=ASKING_LEG, keyword="milk")
    feed(h, p.advance(h), HOME)
    feed(h, p.advance(h), RESULTS)
    send = p.advance(h)
    assert send.tool_calls[1].arguments["name"] == "channel/send"
    ask = send.tool_calls[1].arguments["inputs"]["message"]
    assert ask == "go on with milk?"
    suspend_via_silence(p, h, send, thread_screen((ask, 0.75, 0.3)))
    state = p.state()
    assert state["idx"] == 0 and state["round"] == {"key": "", "at": 2}
    assert state["label"] == "leg[]/go (3/4)"

    resumed = setup.load_suspended()

    assert resumed is not None and resumed.course.label() == "leg[]/go (3/4)"
    h2 = history()
    peek = resumed.advance(h2)
    assert peek.tool_names() == ["note", "peek"]
    feed(h2, peek, thread_screen((ask, 0.75, 0.3), ("好的", 0.25, 0.5)))
    back = resumed.advance(h2)
    assert back.tool_calls[1].arguments["name"] == "demo/add-cart"  # the resume
    feed(h2, back, RESULTS)
    tell = resumed.advance(h2)
    assert tell.tool_calls[1].arguments["inputs"]["message"] == "done: searched milk"


# ---------- each: one round per item ----------


def _listed(p, h, items: str):
    """Answer the parse step with `items`; returns the first round's start."""
    req = p.advance(h)
    assert isinstance(req, DecisionRequest)
    return p.resolve(
        MicroOutcome(out="done", reason="r", confidence=0.9, payload={"items": items})
    )


def _round(p, h, start, what: str, landing: str = RESULTS):
    """Drive one leg round from its start turn; returns the next turn."""
    assert start.tool_calls[1].arguments["name"] == "demo/open-app"
    feed(h, start, HOME)
    search = p.advance(h)
    assert search.tool_calls[1].arguments["inputs"] == {"message": what}
    feed(h, search, landing)
    return p.advance(h)


def test_each_walks_one_round_per_line_and_joins_the_returns() -> None:
    p, h = _walk(flow=EACH, keyword="milk and eggs")

    start = _listed(p, h, "milk\neggs\n")
    assert (
        p.course.label() == "leg[milk]/app (2/6)"
    )  # parse, two rounds of two, the tell
    second = _round(p, h, start, "milk")
    assert (
        p.course.label() == "leg[eggs]/app (2/4)"
    )  # the done round has left the route
    tell = _round(p, h, second, "eggs")

    assert tell.tool_calls[1].arguments["inputs"]["message"] == (
        "done: searched milk\nsearched eggs"
    )
    assert p.ledger.rounds["leg[milk]"]["did"] == "searched milk"
    assert p.ledger.rounds["leg[eggs]"]["did"] == "searched eggs"


def test_each_refuses_more_rounds_than_its_budget() -> None:
    p, h = _walk(flow=EACH, keyword="three things")

    step = _listed(p, h, "a\nb\nc")

    assert "3 rounds, more than its 2" in finish(p, h, step)


def test_the_rounds_budget_counts_work_done_not_one_reading_of_the_list() -> None:
    # A revision re-plans and the run expands again. Counting only
    # today's items would hand every re-plan a fresh budget, so a walk
    # could cold-launch, search and add far more times than the author
    # allowed. Rounds already on record are work this run did.
    p, h = _walk(
        flow=REVISING.replace(
            "    each: {what: parse.items}\n",
            "    each: {what: parse.items}\n    limit: {rounds: 2}\n",
        ),
        pay=PAY,
        keyword="milk and eggs",
    )
    send = _to_confirm(p, h, "milk\neggs")
    _reply(p, h, send, "换成 juice")
    p.resolve(MicroOutcome(out="other", reason="a change", confidence=0.9))

    step = p.resolve(
        MicroOutcome(out="done", reason="r", confidence=0.9, payload={"items": "juice"})
    )

    assert "3 rounds, more than its 2" in finish(p, h, step)


# ---------- miss: skip ----------


def test_a_skippable_round_that_fails_is_recorded_and_the_walk_goes_on() -> None:
    flow = EACH.replace("    limit: {rounds: 2}\n", "    miss: skip\n")
    p, h = _walk(flow=flow, keyword="milk and eggs")
    start = _listed(p, h, "milk\neggs")

    # The milk round's search never lands on `results` (no recover hand
    # on the leg's page): the round is missed, not the walk.
    second = _round(p, h, start, "milk", landing=ELSEWHERE)

    assert second.tool_calls[1].arguments["name"] == "demo/open-app"  # eggs starts
    assert p.ledger.rounds["leg[milk]"]["done"] == "missed"
    assert "did not land on 'results'" in p.ledger.rounds["leg[milk]"]["miss"]
    tell = _round(p, h, second, "eggs")
    message = tell.tool_calls[1].arguments["inputs"]["message"]
    assert message == "done: searched eggs"  # the missed item has no line
    assert any("round leg[milk] missed" in e for e in p.ledger.events)


def test_a_stepping_run_pauses_when_a_missed_round_leaves_the_route() -> None:
    # A finished or missed round leaves the route, so the NEXT round's
    # first node inherits the index this one had. The one-node latch has
    # to judge by position, or it opens that node — a cold launch — in a
    # run the author asked to stop after one.
    flow = EACH.replace("    limit: {rounds: 2}\n", "    miss: skip\n")
    p, h = _walk(flow=flow, keyword="milk and eggs")
    start = _listed(p, h, "milk\neggs")
    assert p.course.label() == "leg[milk]/app (2/6)"
    p.step_one = True

    feed(h, start, HOME)  # the leg's cold start landed
    search = p.advance(h)
    feed(h, search, ELSEWHERE)  # …and its search did not land on `results`

    step = p.advance(h)

    assert p.phase == "paused"  # not walking into the eggs round
    assert isinstance(step, Paused)
    assert p.course.label() == "leg[eggs]/app (2/4)"
    assert p.ledger.rounds["leg[milk]"]["done"] == "missed"


RECOVERING_LEG = """\
kind: playbook
schema: 1
name: leg
description: a leg that decides a keyword, then searches
inputs:
  what:
    description: what to search
returns:
  did: "searched {inputs.what}"
route:
  - agent: plan
    context:
      prompt: "a keyword for {what}"
      given: {what: "{inputs.what}"}
    returns:
      key: the keyword
  - start: app
    macro: app.macros.open-app
  - page: app.pages.home
  - do: search
    macro: app.macros.add-cart
    with: {message: "{plan.key}"}
  - page: app.pages.results
"""


def test_a_recover_hand_inside_a_round_never_re_derives_the_rounds_answer() -> None:
    # A hand that does not restore its page runs again in place; the
    # round's recorded answer is never asked for a second time.
    pages = PAGES.replace(
        'anchors: ["Files"]\n', 'anchors: ["Files"]\n  recover: force_quit\n'
    )
    write_channel()
    write_pack(playbooks={"flow.leg": RECOVERING_LEG, "flow": EACH}, pages=pages)
    p = build_program(name="flow", keyword="milk")
    h = history()
    feed(h, p.advance(h), ELSEWHERE)
    start = _listed(p, h, "milk")
    plan = start  # the leg's first node is the pure-text agent
    assert isinstance(plan, DecisionRequest) and plan.node_id == "plan"
    step = p.resolve(
        MicroOutcome(out="done", reason="r", confidence=0.9, payload={"key": "milk-1"})
    )

    feed(h, step, ELSEWHERE)  # the cold start did not reach `home`
    hand = p.advance(h)
    assert hand is not None and hand.tool_names() == ["note", "force_quit"]
    feed(h, hand, ELSEWHERE)  # …and the hand did not restore it either
    again = p.advance(h)

    assert again.tool_names() == ["note", "force_quit"], "re-derived a recorded answer"


# ---------- revise: an uncovered reply re-plans ----------

PAY = """\
kind: playbook
schema: 1
name: pay
description: confirm the lines
inputs:
  lines:
    description: what to confirm
route:
  - page: app.pages.results
  - ask: confirm
    approve: go
    message: "buy {inputs.lines}?"
    yes: ["好的"]
    no: ["不用"]
    resume: app.macros.add-cart
  - page: app.pages.results
"""

REVISING = """\
kind: entry
schema: 1
name: flow
description: legs, then a confirmation that may revise them
inputs:
  keyword:
    description: what
route:
  - agent: parse
    context:
      prompt: |
        List the items for {keyword}, re-read against {replies}; the list
        stood at {as_it_stood}, and {already_searched} was searched.
      given:
        keyword: "{inputs.keyword}"
        replies: "{ask.replies}"
        as_it_stood: "{parse.items}"
        already_searched: "{leg.did}"
    returns:
      items: the items, one per line
  - run: leg
    each: {what: parse.items}
  - page: app.pages.results
  - run: pay
    with: {lines: "{leg.did}"}
    revise: parse
    limit: {revisions: 1}
  - page: app.pages.results
  - tell: report
    message: "done: {leg.did}"
"""


def _to_confirm(p, h, items: str):
    """Parse, walk every round, and land on the confirmation's send."""
    step = _listed(p, h, items)
    for what in items.splitlines():
        step = _round(p, h, step, what)
    assert step.tool_calls[1].arguments["name"] == "channel/send"
    return step


def _bubbles(ask: str) -> list[tuple]:
    """A multi-line ask as the thread shows it: one row per line, ours
    (right of centre), stacked — the locator reads each as a fragment."""
    return [(line, 0.75, 0.2 + i * 0.05) for i, line in enumerate(ask.splitlines())]


def _reply(p, h, send, text: str):
    """One wait+peek round ending with `text` as the new reply."""
    ask = send.tool_calls[1].arguments["inputs"]["message"]
    feed(h, send, thread_screen(*_bubbles(ask)))
    feed(h, p.advance(h), "waited")
    feed(h, p.advance(h), thread_screen(*_bubbles(ask), (text, 0.25, 0.6)))
    return p.advance(h)


def test_an_uncovered_reply_revises_from_the_named_agent_and_reuses_finished_rounds() -> (
    None
):
    p, h = _walk(flow=REVISING, pay=PAY, keyword="milk and eggs")
    send = _to_confirm(p, h, "milk\neggs")
    assert send.tool_calls[1].arguments["inputs"]["message"] == (
        "buy searched milk\nsearched eggs?"
    )

    read = _reply(p, h, send, "再加 juice，不要 milk")
    assert isinstance(read, DecisionRequest) and read.call == READ_REPLY
    replan = p.resolve(MicroOutcome(out="other", reason="a change", confidence=0.9))

    # Back at parse, whose prompt now reads the reply and what was
    # searched where it wrote their names.
    assert isinstance(replan, DecisionRequest) and replan.node_id == "parse"
    brief = replan.material["prompt"]
    assert "against 再加 juice，不要 milk;" in brief
    # Its own last answer, and the rounds' returns — a list one per line.
    assert "stood at milk\neggs, and" in brief
    assert "searched milk\nsearched eggs was searched" in brief
    assert p.gate.revisions == 1 and not p.gate.awaiting
    assert set(p.ledger.rounds) == {"leg[milk]", "leg[eggs]"}  # finished rounds only
    # The old list is its last answer, not a decision: a walk opening
    # here (a stepping rebuild) opens AT parse.
    assert "parse.items" not in p.ledger.decided
    assert p.ledger.previous == {"parse.items": "milk\neggs"}
    assert p.spec.first_unsettled(p.outputs) == 0
    start = p.resolve(
        MicroOutcome(
            out="done", reason="r", confidence=0.9, payload={"items": "eggs\njuice"}
        )
    )
    # Only the juice round runs: eggs kept its record, milk is off the list.
    assert p.course.label() == "leg[juice]/app (2/5)"
    assert p.outputs["parse.items"] == "eggs\njuice" and not p.ledger.previous
    resend = _round(p, h, start, "juice")
    assert resend.tool_calls[1].arguments["inputs"]["message"] == (
        "buy searched eggs\nsearched juice?"
    )
    assert "revising from 'parse' (1/1)" in " ".join(p.ledger.events)

    # A second uncovered reply is past the limit: the ask hands over.
    _reply(p, h, resend, "算了，换个牌子")
    step = p.resolve(MicroOutcome(out="other", reason="again", confidence=0.9))

    assert "matches none of its yes/no words" in finish(p, h, step)


def test_a_no_typed_while_the_walk_re_plans_is_read_at_the_next_landing() -> None:
    # The sweep at a send's landing exists so a deny typed while the walk
    # was off in the app can never be baselined away unread. A revision
    # leaves the ask but NOT the conversation, so it must keep the thread
    # snapshot the sweep diffs against.
    p, h = _walk(flow=REVISING, pay=PAY, keyword="milk and eggs")
    send = _to_confirm(p, h, "milk\neggs")
    ask = send.tool_calls[1].arguments["inputs"]["message"]
    _reply(p, h, send, "再加 juice，不要 milk")
    p.resolve(MicroOutcome(out="other", reason="a change", confidence=0.9))
    start = p.resolve(
        MicroOutcome(
            out="done", reason="r", confidence=0.9, payload={"items": "eggs\njuice"}
        )
    )
    assert p.gate.baseline  # the thread is still there to diff against

    resend = _round(p, h, start, "juice")
    # The user changed their mind again while the juice round walked.
    feed(
        h,
        resend,
        thread_screen(
            *_bubbles(ask), ("再加 juice，不要 milk", 0.25, 0.6), ("不用", 0.25, 0.7)
        ),
    )

    assert "user declined" in finish(p, h, p.advance(h))


def test_a_stepping_rebuild_after_a_revision_opens_at_the_revised_agent() -> None:
    # The rig, 2026-09-11: the stepper rebuilds the walk from its stored
    # position after every node, and the rebuild at `parse` after a
    # revision walked past it (a pure-text agent with its output on
    # record) — the tell resent the old list and the new item never ran.
    p, h = _walk(flow=REVISING, pay=PAY, keyword="milk and eggs")
    send = _to_confirm(p, h, "milk\neggs")
    _reply(p, h, send, "再加 juice，不要 milk")
    replan = p.resolve(MicroOutcome(out="other", reason="a change", confidence=0.9))
    assert isinstance(replan, DecisionRequest) and replan.node_id == "parse"

    spec, pack = load_spec("demo", "flow")
    stepped = build.build_program(
        spec, pack, {"keyword": "milk and eggs"}, None, position=p.state(), dry=True
    )
    h2 = history()
    feed(h2, stepped.advance(h2), ELSEWHERE)
    again = stepped.advance(h2)
    assert isinstance(again, DecisionRequest) and again.node_id == "parse"
    assert "stood at milk\neggs, and" in again.material["prompt"]


def test_a_step_reads_its_own_returns_empty_the_first_time() -> None:
    p, h = _walk(flow=REVISING, pay=PAY, keyword="milk and eggs")
    first = p.advance(h)
    assert isinstance(first, DecisionRequest) and first.node_id == "parse"
    assert "stood at , and  was searched" in first.material["prompt"]


def test_a_reply_sent_before_the_ask_landed_revises_at_the_landing() -> None:
    p, h = _walk(flow=REVISING, pay=PAY, keyword="milk")
    send = _to_confirm(p, h, "milk")
    ask = send.tool_calls[1].arguments["inputs"]["message"]

    # The user typed while the walk was off in the app; the ask's send
    # lands with that message already on the thread — no baseline from
    # a previous send here, so it is read as a revision straight away.
    p.gate.baseline = {"MyChat"}  # what the thread showed before
    feed(h, send, thread_screen(("再加 eggs", 0.25, 0.2), (ask, 0.75, 0.4)))
    replan = p.advance(h)

    assert isinstance(replan, DecisionRequest) and replan.node_id == "parse"
    assert p.gate.replies == ["再加 eggs"]


# ---------- a playbook the entry runs: the file beside it ----------


def test_a_run_only_playbook_is_off_the_boot_menu_but_its_entry_walks_it() -> None:
    pack = _pack(flow=FLOW)
    spec = playbook.parse_playbook(LEG, "flow.leg", pack)
    flow = playbook.parse_playbook(FLOW, "flow", pack)
    assert spec.run_by == "flow" and flow.run_by is None

    found = discover()

    assert "demo/flow" in found.entries and "demo/flow.leg" not in found.entries
    assert "demo/flow.leg (run by flow)" in found.roster
    assert isinstance(flow.nodes[0], RunNode) and flow.nodes[0].sub.run_by == "flow"
    assert lints.unrun_playbooks([spec, flow]) == []
    assert lints.unrun_playbooks([spec]) == [
        "flow.leg is run by no `run:` of flow — the boot offers only an entry, "
        "so nothing walks it"
    ]


def test_a_run_only_playbooks_name_is_its_file_stem_and_scope_is_no_key() -> None:
    write_pack(
        playbooks={"flow.leg": LEG.replace("name: leg", "name: wing"), "flow": FLOW}
    )
    entries = {e.name: e for e in pb.scan_playbooks("demo")}
    assert entries["flow.leg"].error == (
        "name 'wing' must equal the file name 'leg' (flow/leg.yml)"
    )

    with pytest.raises(PlaybookError, match="unknown key.*scope"):
        _parse(
            FLOW.replace(
                "description: runs a leg then reports\n",
                "description: runs a leg then reports\nscope: local\n",
            )
        )


def test_a_run_only_playbook_reads_its_entrys_leaf_folders() -> None:
    # It is a file in the entry's folder, so the files beside it are the
    # ENTRY's: a bare `macro:` and `prompt:` reach them, and the hand
    # dispatches under the entry.
    from conductor_fakes import write_local_macro, write_prompt

    leg = LEG.replace("macro: app.macros.add-cart", "macro: hand").replace(
        "  - page: app.pages.results\n",
        "  - page: app.pages.results\n"
        "  - agent: read\n    context:\n      prompt: prompts.note\n"
        "    returns:\n      what: a word\n  - page: app.pages.results\n",
    )
    root = write_pack(playbooks={"flow.leg": leg, "flow": FLOW})
    write_local_macro(root, "flow", "hand")
    write_prompt(root, "flow", "note", "read the screen")

    spec = playbook.parse_playbook(leg, "flow.leg", pb.load_pack("demo"))

    assert spec.inline_macros["flow.hand"].name == "flow.hand"
    assert spec.prompts_used == frozenset({"flow/prompts/note.md"})


def test_naming_no_file_of_its_entry_says_the_entrys_folder() -> None:
    leg = LEG.replace("macro: app.macros.add-cart", "macro: hand")
    write_pack(playbooks={"flow.leg": leg, "flow": FLOW})

    entries = {e.name: e for e in pb.scan_playbooks("demo")}

    assert "no macro 'hand' in flow/macros/" in (entries["flow.leg"].error or "")


CLAIMING_ENTRY = """\
kind: entry
schema: 1
name: flow
description: an entry with a page named for the file beside it
route:
  - start: app
    macro: app.macros.open-app
  - page: app.pages.leg
    recover: {macro: {steps: [{peek: null}]}}
  - do: search
    macro: app.macros.add-cart
    with: {message: "x"}
  - page: app.pages.results
"""

CLAIMING_LEG = """\
kind: playbook
schema: 1
name: leg
description: one leg with a move named for a hand's role
route:
  - start: app
    macro: app.macros.open-app
  - page: app.pages.home
  - do: recover
    macro: {steps: [{peek: null}]}
  - page: app.pages.results
"""


def test_two_files_cannot_claim_one_inline_dispatch_name() -> None:
    # An inline body is `<playbook id>.<move>[.<role>]`, and a run-only
    # playbook's id carries a dot: the entry's page `leg` writes its
    # hand inline as `flow.leg.recover`, and `leg.yml`'s own move
    # `recover` spells the same. The walk dispatches by name, so the
    # second file is refused rather than silently merged over the first.
    write_pack(
        playbooks={"flow.leg": CLAIMING_LEG, "flow": CLAIMING_ENTRY},
        pages=PAGES + 'leg:\n  description: a leg\n  anchors: ["Leg"]\n',
    )

    entries = {e.name: e for e in pb.scan_playbooks("demo")}

    assert entries["flow"].spec is not None, entries["flow"].error
    assert "inline macro 'flow.leg.recover' is already 'flow'" in (
        entries["flow.leg"].error or ""
    )


# ---------- `kind:`, checked against where the file sits ----------


def test_a_file_declares_what_it_is_and_the_kind_must_match_its_place() -> None:
    write_pack(playbooks={"flow.leg": LEG, "flow": FLOW})
    entries = {e.name: e for e in pb.scan_playbooks("demo")}
    assert entries["flow"].spec is not None and entries["flow.leg"].spec is not None

    # An entry's file claiming to be one of the playbooks it runs.
    write_pack(
        playbooks={
            "flow.leg": LEG,
            "flow": FLOW.replace("kind: entry", "kind: playbook"),
        }
    )
    assert {e.name: e for e in pb.scan_playbooks("demo")}["flow"].error == (
        "flow/PLAYBOOK.yml: `kind: playbook` sits where `kind: entry` belongs "
        "— `kind: playbook` lives in <entry>/<name>.yml, beside the entry that "
        "runs it"
    )


def test_a_macro_in_a_playbooks_place_is_told_where_it_belongs() -> None:
    # The whole point of the field: a misplaced file names its own folder
    # instead of failing on a key the other grammar does not know.
    root = write_pack(playbooks={"flow.leg": LEG, "flow": FLOW})
    (root / "flow" / "hand.yml").write_text(
        "kind: macro\nschema: 1\nname: hand\ndescription: d\nsteps:\n  - peek\n",
        encoding="utf-8",
    )

    assert {e.name: e for e in pb.scan_playbooks("demo")}["flow.hand"].error == (
        "flow/hand.yml: `kind: macro` sits where `kind: playbook` belongs — "
        "`kind: macro` lives in macros/<name>.yml"
    )


def test_a_kind_that_is_no_kind_at_all_is_refused() -> None:
    write_pack(
        playbooks={
            "flow.leg": LEG,
            "flow": FLOW.replace("kind: entry", "kind: route"),
        }
    )
    assert "is not one of manifest, entry, playbook, macro" in (
        {e.name: e for e in pb.scan_playbooks("demo")}["flow"].error or ""
    )


def test_a_folder_inside_an_entry_is_not_a_playbook() -> None:
    root = write_pack(playbooks={"flow.leg": LEG, "flow": FLOW})
    (root / "flow" / "wing").mkdir()
    (root / "flow" / "wing" / "PLAYBOOK.yml").write_text(LEG, encoding="utf-8")

    entries = {e.name: e for e in pb.scan_playbooks("demo")}

    assert entries["flow/wing/"].error == (
        "demo/flow/wing/: what an entry runs is a file beside its PLAYBOOK.yml "
        "— flow/wing.yml"
    )


# ---------- the invariants the cleanup leaned on ----------


def test_a_suspension_inside_the_second_round_resumes_there_without_the_first() -> None:
    flow = EACH.replace("    limit: {rounds: 2}\n", "")
    p, h = _walk(flow=flow, leg=ASKING_LEG, keyword="milk and eggs")
    start = _listed(p, h, "milk\neggs")
    # Round one: search, the ask, a yes, the resume back to results.
    feed(h, start, HOME)
    feed(h, p.advance(h), RESULTS)
    send = p.advance(h)
    back = _reply(p, h, send, "好的")
    assert back.tool_calls[1].arguments["name"] == "demo/add-cart"
    feed(h, back, RESULTS)
    # Round two up to its ask, then silence.
    start2 = p.advance(h)
    assert p.course.label() == "leg[eggs]/app (2/5)"
    feed(h, start2, HOME)
    feed(h, p.advance(h), RESULTS)
    send2 = p.advance(h)
    ask2 = send2.tool_calls[1].arguments["inputs"]["message"]
    suspend_via_silence(p, h, send2, thread_screen((ask2, 0.75, 0.3)))
    assert p.state()["round"] == {"key": "eggs", "at": 2}

    resumed = setup.load_suspended()

    assert resumed is not None and resumed.course.label() == "leg[eggs]/go (4/5)"
    assert resumed.ledger.round_finished("leg[milk]")  # round one is not walked again
    h2 = history()
    peek = resumed.advance(h2)
    feed(h2, peek, thread_screen((ask2, 0.75, 0.3), ("好的", 0.25, 0.5)))
    back2 = resumed.advance(h2)
    feed(h2, back2, RESULTS)
    tell = resumed.advance(h2)
    assert tell.tool_calls[1].arguments["inputs"]["message"] == (
        "done: searched milk\nsearched eggs"
    )


def test_a_recover_hand_inside_a_round_runs_again_never_the_rounds_start() -> None:
    leg = LEG.replace(
        "  - page: app.pages.results\n",
        "  - page: app.pages.results\n    recover: go_back\n",
    )
    p, h = _walk(flow=EACH, leg=leg, keyword="milk and eggs")
    start = _listed(p, h, "milk\neggs")
    feed(h, start, HOME)
    search = p.advance(h)
    feed(h, search, ELSEWHERE)  # the search did not reach results

    hand = p.advance(h)  # the page's declared hand
    assert hand.tool_calls[1].name == "go_back"
    feed(h, hand, HOME)  # …which did not restore the page either
    again = p.advance(h)

    # The same hand again, in place — never the milk round's cold
    # launch, never the eggs round, never the parse.
    assert again.tool_calls[1].name == "go_back"
    assert p.course.label() == "leg[milk]/search (3/6)"
    assert "parse.items" in p.ledger.decided


TOP_ASK = EACH.replace(
    "  - page: app.pages.results\n  - tell: report\n",
    "  - page: app.pages.results\n"
    "  - ask: go\n"
    "    approve: go\n"
    '    message: "all in?"\n'
    '    yes: ["好的"]\n'
    '    no: ["不用"]\n'
    "    resume: app.macros.add-cart\n"
    "  - page: app.pages.results\n"
    "  - tell: report\n",
)


def test_a_suspension_after_a_run_keeps_the_runs_returns_for_the_rest() -> None:
    p, h = _walk(flow=TOP_ASK, keyword="milk and eggs")
    start = _listed(p, h, "milk\neggs")
    second = _round(p, h, start, "milk")
    send = _round(p, h, second, "eggs")
    assert send.tool_calls[1].arguments["name"] == "channel/send"
    ask = send.tool_calls[1].arguments["inputs"]["message"]
    suspend_via_silence(p, h, send, thread_screen((ask, 0.75, 0.3)))
    assert p.state()["idx"] == 2 and p.state()["round"] is None  # the ask, spec index

    resumed = setup.load_suspended()

    h2 = history()
    peek = resumed.advance(h2)
    feed(h2, peek, thread_screen((ask, 0.75, 0.3), ("好的", 0.25, 0.5)))
    back = resumed.advance(h2)
    feed(h2, back, RESULTS)
    tell = resumed.advance(h2)
    assert tell.tool_calls[1].arguments["inputs"]["message"] == (
        "done: searched milk\nsearched eggs"
    )


def test_a_revision_that_keeps_the_list_re_runs_no_round() -> None:
    p, h = _walk(flow=REVISING, pay=PAY, keyword="milk")
    send = _to_confirm(p, h, "milk")
    _reply(p, h, send, "就这样，不用改")
    replan = p.resolve(MicroOutcome(out="other", reason="no change", confidence=0.9))
    assert isinstance(replan, DecisionRequest) and replan.node_id == "parse"

    resend = p.resolve(
        MicroOutcome(out="done", reason="r", confidence=0.9, payload={"items": "milk"})
    )

    assert (
        resend.tool_calls[1].arguments["name"] == "channel/send"
    )  # straight to the ask
    assert resend.tool_calls[1].arguments["inputs"]["message"] == "buy searched milk?"


# ---------- the second review's findings, pinned ----------

PAY_LONG = PAY.replace(
    "  - page: app.pages.results\n  - ask: confirm\n",
    '  - page: app.pages.results\n  - do: hop\n    macro: app.macros.add-cart\n    with: {message: "x"}\n'
    "  - page: app.pages.results\n  - ask: confirm\n",
)


def test_a_revision_on_a_resumed_walk_recovers_in_place_inside_the_new_round() -> None:
    # Resume at the confirmation inside `pay` (a route index deep in the
    # expanded layout), revise, and let the new round's page hand fail:
    # the hand runs again where the round stands, never past the end.
    leg = LEG.replace(
        "  - page: app.pages.results\n",
        "  - page: app.pages.results\n    recover: go_back\n",
    )
    write_channel()
    write_pack(playbooks={"flow.leg": leg, "flow.pay": PAY_LONG, "flow": REVISING})
    p = build_program(name="flow", keyword="milk")
    h = history()
    feed(h, p.advance(h), ELSEWHERE)
    start = _listed(p, h, "milk")
    hop = _round(p, h, start, "milk")
    feed(h, hop, RESULTS)
    send = p.advance(h)
    ask = send.tool_calls[1].arguments["inputs"]["message"]
    suspend_via_silence(p, h, send, thread_screen(*_bubbles(ask)))
    resumed = setup.load_suspended()
    assert resumed is not None
    h2 = history()
    peek = resumed.advance(h2)
    feed(h2, peek, thread_screen(*_bubbles(ask), ("再加 eggs", 0.25, 0.6)))
    read = resumed.advance(h2)
    assert isinstance(read, DecisionRequest) and read.call == READ_REPLY
    replan = resumed.resolve(MicroOutcome(out="other", reason="more", confidence=0.9))
    assert isinstance(replan, DecisionRequest) and replan.node_id == "parse"
    start2 = resumed.resolve(
        MicroOutcome(
            out="done", reason="r", confidence=0.9, payload={"items": "milk\neggs"}
        )
    )
    assert resumed.course.label() == "leg[eggs]/app (2/5)"
    feed(h2, start2, HOME)
    search = resumed.advance(h2)
    feed(h2, search, ELSEWHERE)
    hand = resumed.advance(h2)
    assert hand.tool_calls[1].name == "go_back"

    feed(h2, hand, HOME)  # the hand did not restore the page
    again = resumed.advance(h2)

    assert again.tool_calls[1].name == "go_back"  # the hand again, in place
    assert resumed.course.label() == "leg[eggs]/search (3/5)"


def test_a_runs_on_fail_word_covers_failures_inside_its_rounds() -> None:
    flow = FLOW.replace("  - run: leg\n", "  - run: leg\n    on_fail: stop\n")
    p, h = _walk(flow=flow, keyword="milk")
    feed(h, p.advance(h), HOME)
    search = p.advance(h)
    feed(h, search, ELSEWHERE)  # the round's move did not land

    stop = p.advance(h)

    assert stop.tool_names() == ["note", "end_session"]
    assert "stopped at leg[]/search" in stop.tool_calls[1].arguments["recap"]


def test_the_runs_miss_word_wins_over_a_sub_pages_own_stop() -> None:
    leg = LEG.replace(
        "  - page: app.pages.results\n",
        "  - page: app.pages.results\n    on_fail: stop\n",
    )
    flow = EACH.replace("    limit: {rounds: 2}\n", "    miss: skip\n")
    p, h = _walk(flow=flow, leg=leg, keyword="milk and eggs")
    start = _listed(p, h, "milk\neggs")

    second = _round(p, h, start, "milk", landing=ELSEWHERE)

    assert second.tool_calls[1].arguments["name"] == "demo/open-app"  # eggs goes on
    assert p.ledger.rounds["leg[milk]"]["done"] == "missed"


def test_a_hand_after_a_done_round_runs_in_place_never_the_round() -> None:
    flow = EACH.replace(
        "  - page: app.pages.results\n  - tell: report\n",
        "  - page: app.pages.results\n    recover: go_back\n"
        '  - do: after\n    macro: app.macros.add-cart\n    with: {message: "x"}\n'
        "  - page: app.pages.results\n  - tell: report\n",
    )
    p, h = _walk(flow=flow, keyword="milk and eggs")
    start = _listed(p, h, "milk\neggs")
    second = _round(p, h, start, "milk")
    after = _round(p, h, second, "eggs")
    assert after.tool_calls[1].arguments["inputs"] == {"message": "x"}
    feed(h, after, ELSEWHERE)  # the move after the run misses its page
    hand = p.advance(h)
    assert hand.tool_calls[1].name == "go_back"
    feed(h, hand, HOME)  # …and the hand did not restore it

    again = p.advance(h)

    # The hand runs a second time where the walk stands: the move after
    # the run — both rounds are done and gone, never their cold starts.
    assert p.course.label() == "after (2/3)"
    assert again.tool_calls[1].name == "go_back"


ADDING_LEG = """\
kind: playbook
schema: 1
name: leg
description: a leg whose move has an effect the phone keeps
inputs:
  what:
    description: what to add
returns:
  did: "added {inputs.what}"
route:
  - start: app
    macro: app.macros.open-app
  - page: app.pages.home
  - do: add
    macro: app.macros.add-cart
    with: {message: "{inputs.what}"}
  - page: app.pages.results
  - do: back
    macro: app.macros.open-app
  - page: app.pages.home
    recover: go_back
"""


def test_a_hand_after_a_landed_move_never_runs_that_move_again() -> None:
    # A hand runs in place: a move that landed (an add to a cart) is
    # never crossed again by a recovery, so the round misses with the
    # add done exactly once rather than searching and adding twice.
    flow = EACH.replace("    limit: {rounds: 2}\n", "    miss: skip\n").replace(
        "  - page: app.pages.results\n  - tell", "  - page: app.pages.home\n  - tell"
    )
    p, h = _walk(flow=flow, leg=ADDING_LEG, keyword="milk")
    start = _listed(p, h, "milk")
    feed(h, start, HOME)
    add = p.advance(h)
    assert add.tool_calls[1].arguments == {
        "name": "demo/add-cart",
        "inputs": {"message": "milk"},
    }
    feed(h, add, RESULTS)  # the add landed
    back = p.advance(h)
    assert back.tool_calls[1].arguments["name"] == "demo/open-app"
    feed(h, back, ELSEWHERE)  # …but the return did not reach home

    again = p.advance(h)
    assert again.tool_names() == ["note", "go_back"]  # the hand, in place
    feed(h, again, ELSEWHERE)
    again = p.advance(h)
    assert again.tool_names() == ["note", "go_back"]  # again — never the add
    feed(h, again, ELSEWHERE)
    p.advance(h)  # tries spent → the run's `miss: skip`

    assert p.ledger.rounds["leg[milk]"]["done"] == "missed"


def test_a_broken_playbook_is_named_in_its_entrys_error() -> None:
    leg = LEG.replace(
        "description: one leg — open, then search\n",
        "description: one leg — open, then search\nenabled: maybe\n",
    )
    write_pack(playbooks={"flow.leg": leg, "flow": FLOW})

    entries = {e.name: e for e in pb.scan_playbooks("demo")}

    assert entries["flow.leg"].error == "`enabled` must be true or false"
    assert entries["flow"].error == (
        "playbook 'flow.leg' (run by 'flow'): `enabled` must be true or false"
    )


def test_a_run_of_a_disabled_playbook_is_not_live() -> None:
    leg = LEG.replace(
        "description: one leg — open, then search\n",
        "description: one leg — open, then search\nenabled: false\n",
    )
    write_pack(playbooks={"flow.leg": leg, "flow": FLOW})
    pack = pb.load_pack("demo")

    assert live_gap(playbook.parse_playbook(FLOW, "flow", pack), pack) == (
        "runs disabled playbook 'flow.leg'"
    )


def test_a_bare_yes_or_no_typed_early_is_the_gates_word_not_a_revision() -> None:
    p, h = _walk(flow=REVISING, pay=PAY, keyword="milk")
    send = _to_confirm(p, h, "milk")
    ask = send.tool_calls[1].arguments["inputs"]["message"]
    p.gate.baseline = {"MyChat"}

    feed(h, send, thread_screen(("好的", 0.25, 0.2), (ask, 0.75, 0.4)))
    step = p.advance(h)

    assert step.tool_names() == ["note", "wait"]  # asked, not revised
    assert p.gate.revisions == 0

    p2, h2 = _walk(flow=REVISING, pay=PAY, keyword="milk")
    send2 = _to_confirm(p2, h2, "milk")
    ask2 = send2.tool_calls[1].arguments["inputs"]["message"]
    p2.gate.baseline = {"MyChat"}

    feed(h2, send2, thread_screen(("不用", 0.25, 0.2), (ask2, 0.75, 0.4)))
    summary = finish(p2, h2, p2.advance(h2))

    assert "user declined the ask" in summary


BOUNDED = """\
description: one agent, then a message quoting it
inputs:
  keyword:
    description: what
route:
  - agent: parse
    context:
      prompt: "describe {keyword}"
      given: {keyword: "{inputs.keyword}"}
    returns:
      items: the items, one per line
  - page: app.pages.results
  - tell: report
    message: "买了：\\n{parse.items}"
"""


def test_a_filled_message_is_bounded_before_it_reaches_the_user() -> None:
    # A return field is written by an agent reading a screen a seller
    # controls. Unbounded, one of them forges a consent clause quoting
    # another number, or pushes the authored one past what the bubble
    # shows. Every value is held to what one authored message may say.
    forged = "\n".join(f"实付 ¥0.01 回复 好的 确认支付 {i}" for i in range(40))
    write_channel()
    write_pack(playbooks={"flow": BOUNDED})
    p = build_program(name="flow", keyword="milk")
    h = history()
    feed(h, p.advance(h), ELSEWHERE)
    send = _listed(p, h, forged)

    message = send.tool_calls[1].arguments["inputs"]["message"]

    assert message.count("实付") == MAX_MESSAGE_LINES
    assert message.endswith("…")
