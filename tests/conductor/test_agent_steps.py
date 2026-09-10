"""Tests for the agent-step grammar and its walk — `agent` moves (pure
text and acting episodes), the `start` move, per-page `recover:` hands,
`landmarks`, and the anchor forms."""

from __future__ import annotations

import json

import pytest
from conductor_fakes import (
    ELSEWHERE,
    FRAME,
    make_screen,
    write_channel,
    write_pack,
    write_prompt,
)
from conductor_fakes import build_program as _program
from conductor_fakes import (
    feed as _feed,
)
from conductor_fakes import (
    finish as _finish,
)
from conductor_fakes import (
    history as _history,
)
from conductor_fakes import (
    thread_screen as _thread,
)

from physiclaw.common.bbox import BANDS
from physiclaw.common.listing import LISTING_HEADER
from physiclaw.conductor.drive import build
from physiclaw.conductor.spec import pack as pb
from physiclaw.conductor.spec import pages
from physiclaw.conductor.spec.calls import (
    ACT_SCROLL_DOWN,
    AGENT_DONE,
    TOOL_SCROLL,
    TOOL_TAP,
)
from physiclaw.conductor.spec.model import AgentNode, DoNode, NeverTap, PlaybookError
from physiclaw.conductor.walk.micro import (
    ACT_ARM,
    AGENT_ACT,
    AGENT_FIELDS,
    SUMMARIZE,
    DecisionRequest,
    Macro,
    MicroOutcome,
    Tap,
    user_content,
)
from physiclaw.conductor.walk.step_agent import refusal
from physiclaw.contract.dto import AssistantMessage, ImageBlock, TextBlock

BACK_LANDMARK = """\
back:
  label: "back"
  at: [0.0, 0.0, 0.1, 0.1]
"""

# The new-grammar walk: a pure-text agent derives a value before the
# phone is touched, `start` cold-launches unconditionally, pages declare
# their own recovery, and an acting episode carries the judgment stretch.
AGENTED = """\
description: agent-driven flow
inputs:
  user_said:
    description: verbatim ask
route:
  - agent: parse
    prompt: |
      Derive the keyword.
      Said: "{inputs.user_said}"
    returns:
      keyword: the search keyword
  - start: app
    macro:
      steps:
        - home_screen
  - page: home
    recover: force_quit
  - do: search
    macro: open-app
    with: {message: "{parse.keyword}"}
  - page: results
    recover: {tap: landmarks.back}
  - agent: pick
    prompt: |
      Add the right item to the cart, then finish on the done page.
    tools: [tap, scroll]
    give: [landmarks.back]
    returns:
      total: the audited total
    limit: {calls: 5, scrolls: 1}
  - page: done
"""

HOME = make_screen(("Files", 0.5, 0.1)).text
RESULTS = make_screen(("综合", 0.5, 0.1), ("Milk 5kg", 0.5, 0.4)).text
# The same results screen with a pay button on it — what a walk sees once
# a sheet has slid up over the list.
RESULTS_WITH_PAY = make_screen(
    ("综合", 0.5, 0.1), ("Milk 5kg", 0.5, 0.4), ("免密支付", 0.5, 0.93)
).text
DONE = make_screen(("AllDone", 0.5, 0.1)).text


GUARDED = AGENTED.replace(
    "  - agent: pick\n", '  - agent: pick\n    never_tap: ["免密支付"]\n'
)


def _write(playbook: str = AGENTED, name: str = "walk"):
    write_pack(playbooks={name: playbook}, landmarks=BACK_LANDMARK)


def _done_outcome(**payload) -> MicroOutcome:
    return MicroOutcome(out=AGENT_DONE, reason="ok", confidence=0.9, payload=payload)


# ---------- parsing ----------


def test_parse_the_agented_playbook() -> None:
    _write()
    spec, _ = build.load_spec("demo", "walk", require_live=False)

    kinds = [type(n).__name__ for n in spec.nodes]
    assert kinds == ["AgentNode", "DoNode", "DoNode", "AgentNode"]
    parse, start, search, pick = spec.nodes
    assert isinstance(parse, AgentNode) and parse.tools == ()
    assert parse.return_fields == ("keyword",)
    assert isinstance(start, DoNode) and start.enter == "" and start.verify == "home"
    assert isinstance(pick, AgentNode)
    assert pick.enter == "results" and pick.verify == "done"
    assert pick.give == ("back",) and pick.max_calls == 5 and pick.max_scrolls == 1
    assert spec.recovers["home"].elsewhere.tool == "force_quit"
    assert spec.recovers["home"].covered is spec.recovers["home"].elsewhere
    assert spec.recovers["results"].elsewhere.landmark == "back"


def _parse(text: str):
    _write(text)
    return build.load_spec("demo", "walk", require_live=False)[0]


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        # An agent with neither hands nor fields can do nothing.
        (("    returns:\n      keyword: the search keyword\n", ""), "can do nothing"),
        # An acting agent must be framed by pages.
        (("  - page: done\n", ""), "followed by the page"),
        # start must sit immediately before the first page.
        (
            (
                "  - start: app\n",
                "  - page: home\n  - start: app\n",
            ),
            "immediately before the first page",
        ),
        # A screen-touching move cannot precede the first page.
        (
            (
                "  - agent: parse\n",
                "  - do: open-app\n  - agent: parse\n",
            ),
            "precede the first page",
        ),
    ],
)
def test_route_shape_lints(mutate, fragment) -> None:
    old, new = mutate
    text = AGENTED.replace(old, new)
    with pytest.raises(PlaybookError, match=fragment):
        _parse(text)


def test_give_may_grant_a_pack_macro() -> None:
    spec = _parse(
        AGENTED.replace(
            "give: [landmarks.back]", "give: [landmarks.back, macros.add-cart]"
        )
    )
    pick = spec.nodes[3]
    assert isinstance(pick, AgentNode)
    assert pick.give == ("back",) and pick.macros == ("add-cart",)
    assert pb.disabled_macros(spec, pb.load_pack("demo")) == []


@pytest.mark.parametrize(
    "grant, fragment",
    [
        ("macros.nope", "not found in this pack"),
        ("macros.done", "fixed episode answer"),
        ("gestures.back", "must look like"),
    ],
)
def test_give_grants_are_checked(grant, fragment) -> None:
    write_pack(
        playbooks={
            "walk": AGENTED.replace("give: [landmarks.back]", f"give: [{grant}]")
        },
        landmarks=BACK_LANDMARK,
        macros=("open-app", "add-cart", "done"),
    )
    with pytest.raises(PlaybookError, match=fragment):
        build.load_spec("demo", "walk", require_live=False)


def test_give_is_optional() -> None:
    _parse(AGENTED.replace("    give: [landmarks.back]\n", ""))


def test_agent_give_must_name_a_declared_landmark() -> None:
    text = AGENTED.replace("give: [landmarks.back]", "give: [landmarks.cart]")
    with pytest.raises(PlaybookError, match="not declared under\n?.*`landmarks`"):
        _parse(text)


def test_recover_tap_requires_a_landmark_target() -> None:
    text = AGENTED.replace("    recover: {tap: landmarks.back}\n", "    recover: tap\n")
    with pytest.raises(PlaybookError, match="landmarks.<name>"):
        _parse(text)


def test_recover_rejects_an_unknown_tool() -> None:
    text = AGENTED.replace("    recover: force_quit\n", "    recover: dance\n")
    with pytest.raises(PlaybookError, match="not a hand"):
        _parse(text)


def test_agent_prompt_refs_are_validated() -> None:
    text = AGENTED.replace("{inputs.user_said}", "{inputs.nope}")
    with pytest.raises(PlaybookError, match="not declared under `inputs`"):
        _parse(text)


def test_landmarks_section_is_the_open_spelling() -> None:
    root = write_pack(playbooks={"walk": AGENTED})
    doc = (root / "APP.yml").read_text(encoding="utf-8")
    (root / "APP.yml").write_text(
        doc + 'landmarks:\n  back:\n    label: "back"\n    at: [0.0, 0.0, 0.1, 0.1]\n'
        '  cart-tab:\n    label: "cart"\n    at: [0.6, 0.9, 0.8, 1.0]\n',
        encoding="utf-8",
    )
    pack = pb.load_pack("demo")
    assert sorted(pack.landmarks) == ["back", "cart-tab"]


def test_controls_is_not_a_pack_section() -> None:
    # The fixed-spot section has ONE spelling; the earlier `controls:`
    # is an unknown key, refused at the pack door.
    root = write_pack(playbooks={"walk": AGENTED}, landmarks=BACK_LANDMARK)
    doc = (root / "APP.yml").read_text(encoding="utf-8")
    (root / "APP.yml").write_text(
        doc.replace("landmarks:", "controls:"), encoding="utf-8"
    )

    with pytest.raises(PlaybookError, match="controls"):
        pb.load_pack("demo")


def test_anchor_forms_parse() -> None:
    decls = pages.parse_pages(
        """\
home:
  anchors: [{text: ["推荐", "关注"], within: top}]
results:
  anchors: ["综合", {text: ["销量", "销售"], within: top}]
paid:
  anchors: [["支付成功", "购买成功"]]
""",
        "demo",
    )
    home = decls["home"].anchors
    assert len(home) == 1 and home[0].readings == ("推荐", "关注")
    results = decls["results"].anchors
    assert len(results) == 2 and results[1].readings == ("销量", "销售")
    assert decls["paid"].anchors[0].readings == ("支付成功", "购买成功")


def test_anchor_is_a_list_of_text_within_items() -> None:
    # One shape: a list of anchors, alternates inside as `text: [..]`.
    with pytest.raises(pages.PagesError, match="unknown key.*and"):
        pages.parse_pages('p:\n  anchors: [{and: ["a"]}]', "demo")
    with pytest.raises(pages.PagesError, match="must be a list"):
        pages.parse_pages('p:\n  anchors: {or: ["a", "b"], within: top}', "demo")


# ---------- the walk: pure-text agent + start ----------


def _boot(playbook: str = AGENTED):
    """Walk AGENTED to the parse agent's request (the opening peek lands
    on an unknown screen — the text agent needs none)."""
    _write(playbook)
    p = _program(name="walk", user_said="买牛奶")
    h = _history()
    _feed(h, p.advance(h), ELSEWHERE)
    req = p.advance(h)
    assert isinstance(req, DecisionRequest) and req.call == AGENT_FIELDS
    assert "买牛奶" in req.material["prompt"]
    return p, h, req


def test_text_agent_fills_outputs_then_start_runs_unconditionally() -> None:
    p, h, req = _boot()
    assert "keyword" in req.material["fields"]

    start = p.resolve(_done_outcome(keyword="milk"))
    assert start is not None and start.tool_names() == ["note", "run_macro"]
    # The start leg has no enter: it runs from the unknown screen.
    assert start.tool_calls[1].arguments["name"] == "demo/walk.app"

    _feed(h, start, HOME)  # verified by the page that follows it
    search = p.advance(h)
    assert search is not None
    assert search.tool_calls[1].arguments == {
        "name": "demo/open-app",
        "inputs": {"message": "milk"},  # {parse.keyword} resolved
    }


def test_declared_context_rides_the_brief_and_nothing_else_does() -> None:
    from physiclaw.common import daylog, paths
    from physiclaw.common.text import write_text

    f = paths.memory_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    write_text(f, "## shopping\nprefers oat milk\n\n## other\nsecret\n")
    daylog.append_log("[11:02] demo: bought milk ¥45")
    _write(
        AGENTED.replace(
            "      keyword: the search keyword\n",
            "      keyword: the search keyword\n    context: [memory.shopping]\n",
        )
    )
    p = _program(name="walk", user_said="买牛奶")
    h = _history()
    _feed(h, p.advance(h), ELSEWHERE)

    req = p.advance(h)

    assert isinstance(req, DecisionRequest) and req.call == AGENT_FIELDS
    assert "prefers oat milk" in req.context
    assert "secret" not in req.context and "bought milk" not in req.context


def test_text_agent_escalate_hands_over() -> None:
    p, h, _ = _boot()
    step = p.resolve(MicroOutcome(out="escalate", reason="no product", confidence=0.9))
    summary = _finish(p, h, step)
    assert "escalated" in summary


def test_text_agent_missing_return_field_hands_over() -> None:
    p, h, _ = _boot()
    step = p.resolve(_done_outcome())
    summary = _finish(p, h, step)
    assert "without return field" in summary


# ---------- the walk: the acting episode ----------


def _at_episode(playbook: str = AGENTED, screen: str = RESULTS):
    """Walk to the pick episode's first request (standing on results)."""
    p, h, _ = _boot(playbook)
    start = p.resolve(_done_outcome(keyword="milk"))
    _feed(h, start, HOME)
    search = p.advance(h)
    _feed(h, search, screen)
    req = p.advance(h)
    assert isinstance(req, DecisionRequest) and req.call == AGENT_ACT
    return p, h, req


def _spot(req: DecisionRequest, text: str, label: str | None = None) -> Tap:
    """A tap on the listed element reading `text` — the box the model
    would copy off the listing, with the label in its own words."""
    el = next(e for e in req.elements if e.label == text)
    return Tap(label=label if label is not None else text, bbox=el.bbox)


def test_episode_offers_rows_grants_and_verbs() -> None:
    _, _, req = _at_episode()

    assert req.macros == ()  # no macros granted; landmarks are not answers
    assert TOOL_TAP in req.outcomes  # taps are boxes, not names
    assert any(e.label == "Milk 5kg" for e in req.elements)  # the live screen
    assert AGENT_DONE in req.outcomes and TOOL_SCROLL in req.outcomes
    # A granted landmark shows what it reads and where it sits, not a
    # bare name the model would have to take on faith.
    assert (
        "Granted landmarks (spots the playbook knows; tap their box):"
        in (req.material["block"])
    )
    assert (
        '- back: reads "back", box [0.000,0.000,0.100,0.100]' in req.material["block"]
    )
    assert LISTING_HEADER in req.material["block"]  # the whole listing, not labels
    assert req.material["lead"].startswith("Add the right item")  # the brief leads


def test_episode_tap_grounds_and_history_is_append_only() -> None:
    p, h, req = _at_episode()
    row = _spot(req, "Milk 5kg", "the milk listing")

    tap = p.resolve(
        MicroOutcome(out=ACT_ARM, reason="fits", confidence=0.9, picked=row)
    )
    assert tap is not None and tap.tool_names() == ["note", "tap"]
    assert tap.tool_calls[1].arguments["bbox"] == list(row.bbox)

    _feed(h, tap, RESULTS)
    req2 = p.advance(h)
    assert isinstance(req2, DecisionRequest)
    # Append-only: the settled turn rides verbatim before the new block —
    # exactly the content micro sent, in the canonical reply's spelling.
    assert len(req2.history) == 2
    assert req2.history[0] == ("user", user_content(req))
    assert (
        '"action": "tap", "args": {"label": "the milk listing", "at": ['
        in req2.history[1][1]
    )
    assert req2.material["lead"] == (
        "[you tapped 'the milk listing' at [0.450,0.380,0.550,0.420]]"
    )


def test_episode_taps_a_box_off_the_listing() -> None:
    # What the detector missed the model taps from the screenshot; the
    # journal says so, box and label.
    p, h, req = _at_episode()
    own = Tap(label="the red badge", bbox=(0.1, 0.2, 0.3, 0.4))

    tap = p.resolve(
        MicroOutcome(out=ACT_ARM, reason="see it", confidence=0.9, picked=own)
    )

    assert tap is not None and tap.tool_calls[1].arguments["bbox"] == [
        0.1,
        0.2,
        0.3,
        0.4,
    ]
    _feed(h, tap, RESULTS)
    req2 = p.advance(h)
    assert isinstance(req2, DecisionRequest)
    assert req2.material["lead"] == (
        "[you tapped 'the red badge' at [0.100,0.200,0.300,0.400]]"
    )


def test_episode_frame_rides_the_request_and_settles_into_history() -> None:
    # A read that carried a frame: the request sends it beside the
    # listing, and the settled turn keeps it — uncompressed — so the
    # next request's prefix is the previous request whole.
    p, h, _ = _boot()
    _feed(h, p.resolve(_done_outcome(keyword="milk")), HOME)
    _feed(h, p.advance(h), RESULTS, frame=FRAME)
    req = p.advance(h)
    assert isinstance(req, DecisionRequest) and req.frame is FRAME
    sent = user_content(req)
    assert isinstance(sent, list) and sent[1] is FRAME

    row = _spot(req, "Milk 5kg")
    tap = p.resolve(MicroOutcome(out=ACT_ARM, reason="ok", confidence=0.9, picked=row))
    _feed(h, tap, RESULTS)  # a text-only result: no frame this time
    req2 = p.advance(h)

    assert isinstance(req2, DecisionRequest) and req2.frame is None
    assert req2.history[0] == ("user", tuple(sent))
    assert [type(b) for b in req2.history[0][1]] == [TextBlock, ImageBlock, TextBlock]
    assert isinstance(user_content(req2), str)


def test_episode_taps_an_icon_by_its_listed_box() -> None:
    # An icon has no listing label, but its box is a tap like any other,
    # and the journal keeps the label the model gave it.
    p, h, _ = _boot()
    _feed(h, p.resolve(_done_outcome(keyword="milk")), HOME)
    screen = make_screen(("综合", 0.5, 0.1), ("", 0.9, 0.9), ("Milk 5kg", 0.5, 0.5))
    _feed(h, p.advance(h), screen.text)
    req = p.advance(h)
    assert isinstance(req, DecisionRequest)
    icon = Tap(label="the cart icon", bbox=screen.rows[1].bbox)

    tap = p.resolve(
        MicroOutcome(out=ACT_ARM, reason="cart", confidence=0.9, picked=icon)
    )

    assert tap is not None and tap.tool_names() == ["note", "tap"]
    assert tap.tool_calls[1].arguments["bbox"] == list(screen.rows[1].bbox)
    _feed(h, tap, RESULTS)
    req2 = p.advance(h)
    assert isinstance(req2, DecisionRequest)
    assert (
        req2.material["lead"]
        == "[you tapped 'the cart icon' at [0.850,0.880,0.950,0.920]]"
    )


def test_episode_runs_a_granted_macro_by_name() -> None:
    from physiclaw.conductor.walk.step_agent import KIND_MACRO

    _write(AGENTED.replace("give: [landmarks.back]", "give: [macros.add-cart]"))
    p = _program(name="walk", user_said="买牛奶")
    h = _history()
    _feed(h, p.advance(h), ELSEWHERE)
    assert isinstance(p.advance(h), DecisionRequest)
    _feed(h, p.resolve(_done_outcome(keyword="milk")), HOME)
    _feed(h, p.advance(h), RESULTS)
    req = p.advance(h)
    assert isinstance(req, DecisionRequest)
    assert "Granted macros" in req.material["block"]
    assert req.macros == ("add-cart",)
    macro = Macro("add-cart")

    run = p.resolve(
        MicroOutcome(out=ACT_ARM, reason="add", confidence=0.9, picked=macro)
    )

    assert run is not None and run.tool_names() == ["note", "run_macro"]
    assert run.tool_calls[1].arguments == {"name": "demo/add-cart"}
    assert "ran macro 'add-cart'" in run.tool_calls[0].arguments["summary"]
    _feed(h, run, RESULTS)  # its result view is the next turn's screen
    req2 = p.advance(h)
    assert isinstance(req2, DecisionRequest)
    assert req2.material["lead"] == "[you ran macro 'add-cart']"
    assert KIND_MACRO in p._step.kinds


@pytest.mark.parametrize("page, offered", [("results", True), ("home", False)])
def test_page_scoped_landmark_is_offered_only_on_its_page(page, offered) -> None:
    scoped = BACK_LANDMARK.rstrip("\n") + f"\n  page: {page}\n"
    write_pack(playbooks={"walk": AGENTED}, landmarks=scoped)
    p = _program(name="walk", user_said="买牛奶")
    h = _history()
    _feed(h, p.advance(h), ELSEWHERE)
    assert isinstance(p.advance(h), DecisionRequest)
    _feed(h, p.resolve(_done_outcome(keyword="milk")), HOME)
    _feed(h, p.advance(h), RESULTS)  # the episode opens on results

    req = p.advance(h)

    assert isinstance(req, DecisionRequest)
    assert ("Granted landmarks" in req.material["block"]) is offered


def test_episode_done_is_audited_against_the_verify_page() -> None:
    p, h, req = _at_episode()

    # done while still on results → rejected, costs a call, continues
    # over the same screen: only the lead changes.
    retry = p.resolve(_done_outcome(total="45"))
    assert isinstance(retry, DecisionRequest)
    assert "done rejected" in retry.material["lead"]
    assert retry.material["block"] == req.material["block"]

    # Move to the verify page, then done sticks and records the returns.
    row = _spot(retry, "Milk 5kg")
    tap = p.resolve(MicroOutcome(out=ACT_ARM, reason="go", confidence=0.9, picked=row))
    _feed(h, tap, DONE)
    req3 = p.advance(h)
    assert isinstance(req3, DecisionRequest)
    step = p.resolve(_done_outcome(total="45"))
    summary = _finish(p, h, step)
    assert "completed" in summary


def test_episode_call_limit_hands_over() -> None:
    p, h, req = _at_episode()
    step: object = req
    for _ in range(10):
        if not isinstance(step, DecisionRequest):
            break
        step = p.resolve(_done_outcome(total="45"))  # rejected off-page each time
    summary = _finish(p, h, step)
    assert "call limit" in summary


def test_episode_scroll_limit_hands_over() -> None:
    p, h, _ = _at_episode()

    swipe = p.resolve(MicroOutcome(out=ACT_SCROLL_DOWN, reason="more", confidence=0.9))
    assert swipe is not None and swipe.tool_names() == ["note", "swipe"]
    _feed(h, swipe, RESULTS)
    assert isinstance(p.advance(h), DecisionRequest)

    step = p.resolve(MicroOutcome(out=ACT_SCROLL_DOWN, reason="more", confidence=0.9))
    summary = _finish(p, h, step)
    assert "scroll limit" in summary


# ---------- declared recovery ----------


def test_declared_recover_hand_runs_then_walk_resumes() -> None:
    p, h, _ = _boot()
    start = p.resolve(_done_outcome(keyword="milk"))
    _feed(h, start, HOME)
    search = p.advance(h)
    _feed(h, search, HOME)  # search did NOT land on results

    hand = p.advance(h)  # results' declared hand: tap landmarks.back — no re-peek first
    assert hand is not None and hand.tool_names() == ["note", "tap"]
    _feed(h, hand, RESULTS)  # the hand restored the page

    req = p.advance(h)  # VERIFY satisfied → the episode opens
    assert isinstance(req, DecisionRequest) and req.call == AGENT_ACT


def test_page_without_recover_hands_over_in_declared_mode() -> None:
    # `done` declares no recover; failing to reach it (episode fence
    # aside) → the walk hands over instead of climbing a hidden ladder.
    no_recover = AGENTED.replace(
        "  - page: results\n    recover: {tap: landmarks.back}\n",
        "  - page: results\n",
    )
    _write(no_recover)
    p = _program(name="walk", user_said="买牛奶")
    h = _history()
    _feed(h, p.advance(h), ELSEWHERE)
    assert isinstance(p.advance(h), DecisionRequest)
    start = p.resolve(_done_outcome(keyword="milk"))
    _feed(h, start, HOME)
    search = p.advance(h)
    _feed(h, search, HOME)  # wrong page; results has no recover now

    summary = _finish(p, h, p.advance(h))
    assert "declares no recover" in summary


def test_recover_relaunch_loop_is_bounded_by_the_walk_budget() -> None:
    # A hand that runs and restores its page clears its recovery State, so the
    # budget must count the WALK's spend, not the engagement's — else a
    # splash ad on every cold launch loops force_quit forever.
    from physiclaw.conductor.spec.limits import MAX_RECOVER_ACTIONS

    _write(
        AGENTED.replace(
            "    recover: force_quit\n",
            "    recover: force_quit\n    tries: 6\n",
        )
    )
    p = _program(name="walk", user_said="买牛奶")
    h = _history()
    _feed(h, p.advance(h), ELSEWHERE)
    assert isinstance(p.advance(h), DecisionRequest)
    step = p.resolve(_done_outcome(keyword="milk"))
    quits = 0
    for _ in range(60):
        assert step is not None and step.synthesized
        if step.tool_names() == ["note", "force_quit"]:
            quits += 1
        _feed(h, step, ELSEWHERE)  # home never reads
        nxt = p.advance(h)
        if nxt is None:  # the brief landed — the walk is over
            break
        step = nxt
    else:
        pytest.fail("the relaunch loop never terminated")
    assert 1 <= quits <= MAX_RECOVER_ACTIONS
    assert "budget" in step.tool_calls[0].arguments["summary"]


def test_page_recover_limit_stops_the_relaunch_before_the_walk_budget() -> None:
    # The page's own `limit:` (default 2) is spent first; the handover
    # names it, so the author sees which bound fired.
    from physiclaw.conductor.spec.limits import MAX_RECOVER_ACTIONS

    _write()
    p = _program(name="walk", user_said="买牛奶")
    h = _history()
    _feed(h, p.advance(h), ELSEWHERE)
    assert isinstance(p.advance(h), DecisionRequest)
    step = p.resolve(_done_outcome(keyword="milk"))
    quits = 0
    for _ in range(20):
        assert step is not None and step.synthesized
        if step.tool_names() == ["note", "force_quit"]:
            quits += 1
        _feed(h, step, ELSEWHERE)
        nxt = p.advance(h)
        if nxt is None:
            break
        step = nxt
    assert quits == 2 < MAX_RECOVER_ACTIONS
    assert "recover tries (2) spent" in step.tool_calls[0].arguments["summary"]


def test_recover_force_quit_then_walk_restarts_from_the_top() -> None:
    _write()
    p = _program(name="walk", user_said="买牛奶")
    h = _history()
    _feed(h, p.advance(h), ELSEWHERE)
    assert isinstance(p.advance(h), DecisionRequest)
    start = p.resolve(_done_outcome(keyword="milk"))
    _feed(h, start, ELSEWHERE)  # the launch did NOT reach home

    hand = p.advance(h)  # home's declared hand: force_quit
    assert hand is not None and hand.tool_names() == ["note", "force_quit"]
    _feed(h, hand, ELSEWHERE)  # springboard — home still does not read

    relaunch = p.advance(h)  # still off → route top → start runs again
    assert relaunch is not None
    assert relaunch.tool_calls[1].arguments["name"] == "demo/walk.app"


# ---------- the payment episode ----------

AGENT_PAY = """\
description: gated agent pay
inputs:
  keyword:
    description: what
route:
  - page: home
  - do: open
    macro: open-app
    with: {message: "{inputs.keyword}"}
  - page: results
  - ask: gate
    approve: payment
    total_label: "合计"
    message: "合计 ¥{ask.total}。回复 好的 确认支付，或 不用 取消。"
    yes: ["好的"]
    no: ["不用"]
    resume:
      macro: open-app
  - agent: pay
    irreversible: payment
    prompt: |
      Pay exactly ¥{ask.total}, then finish on the done page.
    tools: [tap]
    limit: {calls: 3}
  - page: done
"""

SHEET = make_screen(("综合", 0.5, 0.1), ("合计 ¥45", 0.5, 0.5), ("支付", 0.5, 0.8)).text
SHEET_CHANGED = make_screen(("综合", 0.5, 0.1), ("合计 ¥60", 0.5, 0.5)).text


def _at_pay_episode(resume_screen: str = SHEET):
    """Walk AGENT_PAY through the confirmed gate to the pay episode's
    first request."""
    write_channel()
    write_pack(playbooks={"pay": AGENT_PAY})
    p = _program(name="pay", keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)
    _feed(h, p.advance(h), SHEET)  # `open` landed on the sheet (results)
    send = p.advance(h)
    assert send is not None and send.tool_calls[1].arguments["name"] == "channel/send"
    ask = send.tool_calls[1].arguments["inputs"]["message"]
    assert "¥45" in ask  # the sheet total, quoted
    _feed(h, send, _thread((ask, 0.75, 0.3)))
    _feed(h, p.advance(h), "waited")
    peek = p.advance(h)
    _feed(h, peek, _thread((ask, 0.75, 0.3), ("好的", 0.25, 0.5)))
    back = p.advance(h)  # confirmed → resume macro re-enters the app
    assert back is not None and back.tool_calls[1].arguments["name"] == "demo/open-app"
    _feed(h, back, resume_screen)
    return p, h, p.advance(h)


def test_payment_episode_taps_under_consent_then_completes() -> None:
    p, h, req = _at_pay_episode()
    assert isinstance(req, DecisionRequest)
    assert "¥45" in req.material["lead"]  # {ask.total} filled into the prompt

    row = _spot(req, "支付")
    tap = p.resolve(MicroOutcome(out=ACT_ARM, reason="pay", confidence=0.9, picked=row))
    assert tap is not None and tap.tool_names() == ["note", "tap"]

    _feed(h, tap, DONE)
    req2 = p.advance(h)
    assert isinstance(req2, DecisionRequest)
    step = p.resolve(_done_outcome())
    summary = _finish(p, h, step)
    assert "completed" in summary


def test_payment_episode_second_tap_keeps_the_paid_record() -> None:
    # Consent is spent on the first tap; a later tap of the same episode
    # finds nothing to spend and must not erase the amount that fired.
    p, h, req = _at_pay_episode()
    row = _spot(req, "支付")
    tap = p.resolve(MicroOutcome(out=ACT_ARM, reason="pay", confidence=0.9, picked=row))
    _feed(h, tap, SHEET)  # the sheet still shows ¥45 — a confirm step
    req2 = p.advance(h)
    row2 = _spot(req2, "支付")
    tap2 = p.resolve(
        MicroOutcome(out=ACT_ARM, reason="ok", confidence=0.9, picked=row2)
    )
    assert tap2 is not None and tap2.tool_names() == ["note", "tap"]
    _feed(h, tap2, DONE)
    assert isinstance(p.advance(h), DecisionRequest)
    summary = _finish(p, h, p.resolve(_done_outcome()))

    assert "completed" in summary and p.ledger.paid == 45.0


@pytest.mark.parametrize(
    "old, new, fragment",
    [
        # A granted name can never be spelled like a fixed answer.
        ("give: [landmarks.back]", "give: [landmarks.done]", "fixed episode answer"),
        # A return field cannot reuse the reply contract's own fields.
        ("      keyword: the search keyword\n", "      answer: the pick\n", "contract"),
        # scroll granted with no scroll budget would hand over at once.
        ("limit: {calls: 5, scrolls: 1}", "limit: {calls: 5, scrolls: 0}", "scrolls"),
        # Landmarks are pressed; without tap there is nothing to press with.
        ("    tools: [tap, scroll]\n", "    tools: [scroll]\n", "without `tap`"),
    ],
)
def test_episode_grammar_lints(old, new, fragment) -> None:
    write_pack(
        playbooks={"walk": AGENTED.replace(old, new)},
        landmarks=BACK_LANDMARK
        + 'done:\n  label: "done"\n  at: [0.5, 0.5, 0.6, 0.6]\n',
    )
    with pytest.raises(PlaybookError, match=fragment):
        build.load_spec("demo", "walk", require_live=False)


def test_give_refuses_one_name_as_both_landmark_and_macro() -> None:
    write_pack(
        playbooks={
            "walk": AGENTED.replace(
                "give: [landmarks.back]", "give: [landmarks.back, macros.back]"
            )
        },
        landmarks=BACK_LANDMARK,
        macros=("open-app", "add-cart", "back"),
    )
    with pytest.raises(PlaybookError, match="both a landmark and a macro"):
        build.load_spec("demo", "walk", require_live=False)


def test_payment_ask_before_a_screen_move_needs_resume() -> None:
    text = AGENT_PAY.replace("    resume:\n      macro: open-app\n", "")
    write_channel()
    write_pack(playbooks={"pay": text})
    with pytest.raises(PlaybookError, match="declare `resume:`"):
        build.load_spec("demo", "pay", require_live=False)


def test_payment_ask_reads_the_page_before_it() -> None:
    # A reserved built-in cannot be the sheet a payment ask reads.
    text = AGENT_PAY.replace(
        "  - page: results\n  - ask: gate\n",
        "  - page: results\n  - page: ios.locked\n  - ask: gate\n",
    )
    write_channel()
    write_pack(playbooks={"pay": text})
    with pytest.raises(PlaybookError, match="reads its total off the page before"):
        build.load_spec("demo", "pay", require_live=False)


def test_payment_episode_blocks_a_tap_when_the_sheet_changed() -> None:
    p, h, req = _at_pay_episode(resume_screen=SHEET_CHANGED)
    assert isinstance(req, DecisionRequest)

    row = Tap(label="anything", bbox=req.elements[0].bbox)
    step = p.resolve(
        MicroOutcome(out=ACT_ARM, reason="pay", confidence=0.9, picked=row)
    )
    summary = _finish(p, h, step)
    assert "sheet changed after consent" in summary


# ---------- prompts as files: `prompt: prompts.<name>` ----------

FILE_PROMPT = AGENTED.replace(
    '    prompt: |\n      Derive the keyword.\n      Said: "{inputs.user_said}"\n',
    "    prompt: prompts.parse\n",
)


def test_a_prompt_file_is_the_step_prompt_verbatim_with_refs_filled_later() -> None:
    from physiclaw.common import paths

    _write(FILE_PROMPT)
    root = paths.playbooks_dir() / "demo"
    write_prompt(
        root, "walk", "parse", '# Keyword\n\nDerive it.\nSaid: "{inputs.user_said}"\n\n'
    )

    spec, _ = build.load_spec("demo", "walk", require_live=False)

    parse = spec.nodes[0]
    assert isinstance(parse, AgentNode)
    # Body only, trailing whitespace trimmed, headings are the author's prose.
    assert parse.prompt == '# Keyword\n\nDerive it.\nSaid: "{inputs.user_said}"'
    assert spec.prompts_used == frozenset({"parse"})


def test_a_pack_level_prompt_is_shared_by_every_route() -> None:
    from physiclaw.common import paths

    _write(FILE_PROMPT)
    root = paths.playbooks_dir() / "demo"
    write_prompt(root, None, "parse", "Shared brief.")

    spec, _ = build.load_spec("demo", "walk", require_live=False)

    assert spec.nodes[0].prompt == "Shared brief."


def test_a_prompt_file_ref_is_validated_like_inline_prose() -> None:
    from physiclaw.common import paths

    _write(FILE_PROMPT)
    write_prompt(paths.playbooks_dir() / "demo", "walk", "parse", "Said: {inputs.nope}")

    with pytest.raises(PlaybookError, match="`prompt`"):
        build.load_spec("demo", "walk", require_live=False)


def test_a_missing_prompt_file_names_what_exists() -> None:
    from physiclaw.common import paths

    _write(FILE_PROMPT)
    write_prompt(paths.playbooks_dir() / "demo", "walk", "other", "x")

    with pytest.raises(
        PlaybookError, match="no parse.md sits in.*Available: prompts.other"
    ):
        build.load_spec("demo", "walk", require_live=False)


def test_an_empty_prompt_file_fails_the_step_with_the_cause() -> None:
    from physiclaw.common import paths

    _write(FILE_PROMPT)
    root = paths.playbooks_dir() / "demo"
    write_prompt(root, "walk", "parse", "   \n")

    pack = pb.load_pack("demo")
    assert pack.local["walk"].prompts.errors["parse"] == "the prompt file is empty"
    with pytest.raises(
        PlaybookError, match="parse.md is invalid: the prompt file is empty"
    ):
        build.load_spec("demo", "walk", require_live=False)


def test_a_prompt_name_in_both_the_pack_and_the_route_is_refused() -> None:
    from physiclaw.common import paths

    _write(FILE_PROMPT)
    root = paths.playbooks_dir() / "demo"
    write_prompt(root, "walk", "parse", "mine")
    write_prompt(root, None, "parse", "ours")

    with pytest.raises(
        PlaybookError, match="declared both in walk/prompts/ and the pack's"
    ):
        build.load_spec("demo", "walk", require_live=False)


def test_placeholders_fill_in_a_prompt_file_too() -> None:
    from physiclaw.common import paths
    from physiclaw.common.placeholders import write_placeholder_values

    write_placeholder_values({"CONTACT": "Alice"})
    _write(FILE_PROMPT)
    write_prompt(
        paths.playbooks_dir() / "demo", "walk", "parse", "Buy for <<CONTACT>>."
    )

    spec, _ = build.load_spec("demo", "walk", require_live=False)

    assert spec.nodes[0].prompt == "Buy for Alice."


def test_inline_prose_that_merely_mentions_prompts_stays_prose() -> None:
    inline = AGENTED.replace(
        "      Derive the keyword.\n",
        "      Derive the keyword, see prompts.parse in the docs.\n",
    )
    _write(inline)

    spec, _ = build.load_spec("demo", "walk", require_live=False)

    assert "see prompts.parse in the docs" in spec.nodes[0].prompt
    assert spec.prompts_used == frozenset()


# ---------- the whole loop: conductor → micro → program, over tool calls ----------


@pytest.mark.asyncio
async def test_conductor_drives_a_full_episode_over_tool_call_replies() -> None:
    # End to end through the real broker: the model answers tool calls
    # (done with args, scroll, a tap by a listed box, a tap at a granted
    # landmark, done), the walk grounds each, the frames ride and replay,
    # and the returns land in the walk's outputs.
    from conductor_fakes import ScriptedProvider, Sink

    from physiclaw.conductor.drive.conductor import Conductor
    from physiclaw.conductor.walk.micro import MicroCaller

    milk = make_screen(("综合", 0.5, 0.1), ("Milk 5kg", 0.5, 0.4)).rows[1]
    replies = [
        '{"reason": "r", "action": "done", "args": {"keyword": "milk"}, '
        '"confidence": 0.9}',
        '{"reason": "r", "action": "scroll", "args": {"direction": "down"}, '
        '"confidence": 0.9}',
        json.dumps(
            {
                "reason": "r",
                "action": "tap",
                "args": {"label": "the milk listing", "at": list(milk.bbox)},
                "confidence": 0.9,
            }
        ),
        '{"reason": "r", "action": "tap", "args": {"label": "the back chevron", '
        '"at": [0.0, 0.0, 0.1, 0.1]}, "confidence": 0.9}',
        '{"reason": "r", "action": "done", "args": {"total": "45"}, "confidence": 0.9}',
        # The close's record, written in the session thread.
        '{"reason": "r", "answer": "done", "confidence": 0.9, '
        '"recap": "bought 5kg milk for 45", "memory": "user buys Milk 5kg, ¥45"}',
    ]
    _write()
    p = _program(name="walk", user_said="买牛奶")
    provider = ScriptedProvider(replies)
    conductor = Conductor(
        program=p, micro=MicroCaller(provider, confidence_floor=0.6, tr=Sink())
    )
    h = _history()

    peek = await conductor.advance(h)
    _feed(h, peek, ELSEWHERE)
    start = await conductor.advance(h)  # the parse call brokered inside
    assert start is not None and start.tool_names() == ["note", "run_macro"]
    _feed(h, start, HOME)
    search = await conductor.advance(h)
    _feed(h, search, RESULTS, frame=FRAME)

    scroll = await conductor.advance(h)
    assert scroll is not None and scroll.tool_names() == ["note", "swipe"]
    _feed(h, scroll, RESULTS, frame=FRAME)
    tap = await conductor.advance(h)
    assert tap is not None and tap.tool_names() == ["note", "tap"]
    assert tap.tool_calls[1].arguments["bbox"] == list(milk.bbox)
    _feed(h, tap, RESULTS)
    mark = await conductor.advance(h)
    assert mark is not None and mark.tool_names() == ["note", "tap"]
    assert (
        "tapped 'the back chevron' at [0.000,0.000,0.100,0.100]"
        in (mark.tool_calls[0].arguments["summary"])
    )
    _feed(h, mark, DONE)
    end = await conductor.advance(h)
    assert end is not None and end.tool_names() == ["note", "end_session"]
    # The close asked the session thread for the record and closed on it.
    assert end.tool_calls[1].arguments == {
        "status": "DONE",
        "recap": "bought 5kg milk for 45",
    }
    assert p.outputs["pick.total"] == "45"
    _feed(h, end, "ended")
    assert await conductor.advance(h) is None

    # The last episode call replayed every earlier turn: both frames,
    # and each settled reply as the tool call it was. (The very last
    # call is the close's, in the session thread.)
    last = provider.calls[-2]
    frames = [
        b
        for m in last
        if isinstance(m.content, list)
        for b in m.content
        if isinstance(b, ImageBlock)
    ]
    assert len(frames) == 2
    settled = [m.content for m in last if isinstance(m, AssistantMessage)]
    assert settled == [
        '{"reason": "r", "action": "scroll", "args": {"direction": "down"}, '
        '"confidence": 0.9}',
        '{"reason": "r", "action": "tap", "args": {"label": "the milk listing", '
        f'"at": {json.dumps([round(v, 3) for v in milk.bbox])}}}, "confidence": 0.9}}',
        '{"reason": "r", "action": "tap", "args": {"label": "the back chevron", '
        '"at": [0.0, 0.0, 0.1, 0.1]}, "confidence": 0.9}',
    ]


def test_a_completed_walk_asks_the_thread_for_its_record_then_ends_done() -> None:
    # The task was the playbook's; when it is done the walk asks the
    # session thread for the record (the model that read the request
    # writes the recap and the memory line), then closes the session
    # DONE by its own hand.
    p, h, req = _at_episode()
    row = _spot(req, "Milk 5kg")
    tap = p.resolve(MicroOutcome(out=ACT_ARM, reason="go", confidence=0.9, picked=row))
    _feed(h, tap, DONE)
    assert isinstance(p.advance(h), DecisionRequest)

    close = p.resolve(_done_outcome(total="45"))

    assert isinstance(close, DecisionRequest) and close.call == SUMMARIZE
    # The ledger's events the call carries tell the whole walk, task to total.
    block = str(user_content(close))
    assert "asked user_said='买牛奶'" in block and "decided pick.total='45'" in block
    end = p.resolve(
        MicroOutcome(
            out="done",
            reason="r",
            confidence=0.9,
            payload={"recap": "bought milk, ¥45", "memory": "user buys Milk 5kg"},
        )
    )
    assert end is not None and end.tool_names() == ["note", "end_session"]
    assert end.tool_calls[1].arguments == {
        "status": "DONE",
        "recap": "bought milk, ¥45",
    }
    _feed(h, end, "ended")
    assert p.advance(h) is None


def test_a_completed_walk_closes_on_its_own_recap_when_nobody_answers() -> None:
    p, h, req = _at_episode()
    row = _spot(req, "Milk 5kg")
    _feed(
        h,
        p.resolve(MicroOutcome(out=ACT_ARM, reason="go", confidence=0.9, picked=row)),
        DONE,
    )
    assert isinstance(p.advance(h), DecisionRequest)
    close = p.resolve(_done_outcome(total="45"))
    assert isinstance(close, DecisionRequest) and close.call == SUMMARIZE

    end = p.resolve(None)

    assert end is not None and end.tool_names() == ["note", "end_session"]
    args = end.tool_calls[1].arguments
    assert args["status"] == "DONE"
    assert args["recap"].startswith("demo/walk completed (4/4 nodes)")
    assert "pick.total='45'" in args["recap"]


# ---------- never_tap: the taps the walker will not fire ----------

_PAY = NeverTap(label=("免密支付", "立即支付"))
_FOOTER_PAY = NeverTap(label=("免密支付",), within=BANDS["bottom"])


def _sheet():
    """The recorded shape of a Taobao order sheet: the pay word reads
    BOTH as the caption beside the total (mid-sheet) and as the button at
    the foot, with the walk's own button beside it."""
    screen = make_screen(
        ("免密支付", 0.30, 0.57),  # caption beside the total
        ("加购物车", 0.25, 0.93),  # the walk's own button…
        ("免密支付", 0.85, 0.93),  # …beside the pay button
    )
    caption, cart, button = (r.bbox for r in screen.rows)
    return screen, caption, cart, button


def test_a_tap_that_presses_a_listed_target_is_refused() -> None:
    screen, caption, cart, button = _sheet()

    said = refusal((_PAY,), screen.rows, Tap(label="the orange button", bbox=button))
    assert said is not None and "免密支付" in said
    # A box aimed a little past the text still presses the button under it.
    nudged = (button[0] + 0.02, button[1] + 0.015, button[2] + 0.02, button[3] + 0.015)
    assert refusal((_PAY,), screen.rows, Tap(label="x", bbox=nudged)) is not None
    # Unbanded, the mid-sheet caption counts as the target too.
    assert refusal((_PAY,), screen.rows, Tap(label="x", bbox=caption)) is not None
    # Declaring nothing refuses nothing.
    assert refusal((), screen.rows, Tap(label="x", bbox=button)) is None


def test_the_press_lands_at_the_centre_so_that_is_the_whole_question() -> None:
    # A tap fires at its box's CENTRE (`core.server.tools.tap`). A box
    # drawn over the whole screen presses the middle of the screen, not
    # whatever it happens to span — and naming a target while pressing
    # elsewhere is narration, since the box is what fires.
    screen, caption, cart, button = _sheet()

    assert (
        refusal((_PAY,), screen.rows, Tap(label="x", bbox=(0.0, 0.0, 1.0, 1.0))) is None
    )
    assert refusal((_PAY,), screen.rows, Tap(label="免密支付", bbox=cart)) is None
    # …and the walk's own button, beside the pay button, goes through.
    assert refusal((_PAY,), screen.rows, Tap(label="加购物车", bbox=cart)) is None


def test_a_within_band_says_where_the_target_sits() -> None:
    # The band gates the tap and the row alike, as `within:` does on a
    # page anchor. It is what keeps the pay word's mid-sheet caption from
    # standing in for the button — this playbook declares that same word
    # as a `total_label:`.
    screen, caption, cart, button = _sheet()

    assert refusal((_FOOTER_PAY,), screen.rows, Tap(label="x", bbox=button)) is not None
    assert refusal((_FOOTER_PAY,), screen.rows, Tap(label="x", bbox=caption)) is None


def test_a_band_is_a_sketch_and_must_be_drawn_generously() -> None:
    # A band drawn tight around its target can miss: here the row sits ON
    # the edge, half above it, and a tap centred on that half is outside.
    # That is the trade a band buys — declare one only to keep a word that
    # also appears elsewhere from standing in, and draw it with room.
    edge = make_screen(("免密支付", 0.5, 0.750))
    row = edge.rows[0].bbox

    assert refusal((_FOOTER_PAY,), edge.rows, Tap(label="x", bbox=row)) is not None
    above = (row[0], row[1], row[2], 0.7480)
    assert refusal((_FOOTER_PAY,), edge.rows, Tap(label="x", bbox=above)) is None


def test_a_refused_tap_is_journaled_and_the_episode_goes_on() -> None:
    # The move never becomes a turn, so without the journal line nothing
    # would record that the model reached for the pay button at all.
    p, h, req = _at_episode(GUARDED, RESULTS_WITH_PAY)
    row = _spot(req, "免密支付")

    again = p.resolve(
        MicroOutcome(out=ACT_ARM, reason="pay", confidence=0.9, picked=row)
    )

    assert isinstance(again, DecisionRequest)  # re-asked, NOT handed over
    assert "refused a tap" in p.ledger.events[-1]
    assert "免密支付" in p.ledger.events[-1]
    assert "免密支付" in str(again.material["lead"])
    assert again.material["lead"] != req.material["lead"]
