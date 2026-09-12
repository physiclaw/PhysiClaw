"""Tests for `physiclaw.conductor.walk.program` and its step executors — the
walk: opening peek and locate, moves with their enter/verify checks,
declared recovery, the ask (send, hold, judge, consent), the tell
(send, move on), suspension, money, and the walk's telemetry."""

from __future__ import annotations

import json

import pytest
from conductor_fakes import (
    CHANNEL_OPEN,
    DONE,
    ELSEWHERE,
    FLOW,
    HOME,
    RESULTS,
    make_screen,
    write_channel,
    write_pack,
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

from physiclaw.common import paths as paths_mod
from physiclaw.conductor.drive import build, setup
from physiclaw.conductor.spec import channel, limits, lints
from physiclaw.conductor.spec.model import PlaybookError
from physiclaw.conductor.walk import program, suspension

# ---------- the walk ----------


def test_walk_runs_both_moves_then_completes() -> None:
    write_pack(playbooks={"flow": FLOW})
    p = _program(keyword="milk")
    h = _history()

    peek = p.advance(h)
    assert peek is not None and peek.synthesized
    assert peek.tool_names() == ["note", "peek"]

    _feed(h, peek, HOME)  # the start page — the walk begins
    move1 = p.advance(h)
    assert move1 is not None and move1.tool_names() == ["note", "run_macro"]
    assert move1.tool_calls[1].arguments == {
        "name": "demo/open-app",
        "inputs": {"message": "milk"},  # {inputs.keyword} resolved from the arm
    }

    _feed(h, move1, RESULTS)  # landed on `results` → next move (enter holds too)
    move2 = p.advance(h)
    assert move2 is not None
    assert move2.tool_calls[1].arguments["name"] == "demo/add-cart"

    _feed(h, move2, DONE)  # landed on `done` → playbook complete
    summary = _finish(p, h, p.advance(h))
    assert "demo/flow completed (2/2 nodes)" in summary


def test_walk_starts_at_the_top_whatever_the_screen_reads() -> None:
    # The screen already shows move 1's landing page — the walk still
    # begins at move 1, which expects `home`: a page match proves
    # nothing about the moves before it, and nothing fast-forwards
    # undeclared. With no recover on home the walk hands over.
    write_pack(playbooks={"flow": FLOW})
    p = _program(keyword="milk")
    h = _history()

    peek = p.advance(h)
    assert peek is not None
    _feed(h, peek, RESULTS)

    summary = _finish(p, h, p.advance(h))
    assert "move 'open' expects page 'home'" in summary


def test_verify_mismatch_hands_over_without_a_recover() -> None:
    # A wrong known page at verify: a page declaring no recover hands
    # over on the spot — what you declare is what runs, nothing hidden
    # re-peeks, waits, or taps around.
    write_pack(playbooks={"flow": FLOW})
    p = _program(keyword="milk")
    h = _history()

    _feed(h, p.advance(h), HOME)
    move1 = p.advance(h)
    assert move1 is not None
    _feed(h, move1, HOME)  # landed on the WRONG known page

    summary = _finish(p, h, p.advance(h))
    assert "did not land" in summary and "declares no recover" in summary


def test_move_verifying_a_builtin_page_is_refused_at_parse() -> None:
    # `ios` pages LOAD (the conductor matches against them itself) but a
    # playbook may not ACT on one: moves run this pack's macros and land
    # on this pack's pages — refused at the pack door, never at run time.
    write_pack(
        playbooks={
            "flow": FLOW.replace("  - page: results\n", "  - page: ios.locked\n")
        }
    )

    with pytest.raises(PlaybookError, match="reserved built-in"):
        build.load_spec("demo", "flow", require_live=False)


def test_error_result_hands_over() -> None:
    write_pack(playbooks={"flow": FLOW})
    p = _program(keyword="milk")
    h = _history()

    _feed(h, p.advance(h), HOME)
    move1 = p.advance(h)
    assert move1 is not None
    _feed(h, move1, "BLOCKED — not executed", error=True)

    summary = _finish(p, h, p.advance(h))
    assert "blocked or failed" in summary


def test_program_advance_never_raises() -> None:
    write_pack(playbooks={"flow": FLOW})
    p = _program(keyword="milk")

    # A malformed history (no pending result will ever match) must
    # degrade to a hand-over, not an exception.
    p.advance(_history())
    step = p.advance(_history())  # missing result → the handover brief
    assert step is not None and step.tool_names() == ["note", "peek"]
    assert p.advance(_history()) is None  # then permanently quiet


# A pure-text agent opens the route — the one step that brokers a
# model call in these walks.
AGENT_FLOW = """\
description: parse then walk
inputs:
  keyword:
    description: what to search
route:
  - agent: parse
    prompt: "Turn this into a search term: {inputs.keyword}"
    returns:
      term: the search term
  - page: home
  - do: open
    macro: open-app
    with: {message: "{parse.term}"}
  - page: results
"""


def test_failed_agent_call_hands_over() -> None:
    from physiclaw.conductor.walk.micro import DecisionRequest

    write_pack(playbooks={"flow": AGENT_FLOW})
    p = _program(keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)

    req = p.advance(h)
    assert isinstance(req, DecisionRequest)  # the conductor brokers it
    summary = _finish(p, h, p.resolve(None))  # a failed call hands over
    assert "call failed or under-confident" in summary


def test_a_late_page_match_never_skips_work_nodes() -> None:
    # A fresh wake whose screen matches a LATE move's verify page must
    # not skip the agent step before it: the walk starts at the top.
    flow = """\
description: agent in the middle
inputs:
  keyword:
    description: what
route:
  - page: home
  - do: open
    macro: open-app
    with: {message: "{inputs.keyword}"}
  - page: results
  - agent: choose
    prompt: "pick one"
    tools: [tap]
    returns:
      pick: the pick
  - page: done
  - do: wrap
    macro: add-cart
    with: {message: "x"}
  - page: done
"""
    write_pack(playbooks={"flow": flow})
    p = _program(keyword="milk")
    h = _history()
    _feed(h, p.advance(h), DONE)  # reads as the LAST move's landing

    # The cursor stayed at the top: the leading `do` run ended at the
    # agent, so the walk starts over at `open` (whose enter fails here)
    # instead of jumping to `wrap` off the coincidental page match.
    summary = _finish(p, h, p.advance(h))
    assert "move 'open' expects page 'home'" in summary


# ---------- the gate, suspending, activation ----------

GATED = """\
description: 买牛奶
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
    message: "已选好{inputs.keyword}，合计 ¥{ask.total}。回复 好的 确认支付，或 不用 取消。"
    yes: ["好的"]
    no: ["不用"]
    think: off
    resume: open-app
  - do: pay
    macro: add-cart
    with: {message: "pay"}
    irreversible: payment
  - page: home
"""


def _sheet(total: str = "¥45") -> str:
    return make_screen(("综合", 0.5, 0.1), (f"合计 {total}", 0.5, 0.5)).text


def _armed(playbook: str = GATED):
    """Arm the gated playbook (with a channel pack) on its start page."""
    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"pay": playbook})
    p = _program(name="pay", keyword="milk")
    assert p.channel is not None and p.channel.send == "channel/send"
    h = _history()
    _feed(h, p.advance(h), HOME)  # the start page
    return p, h


def _at_gate(total: str = "¥45", playbook: str = GATED):
    """Walk the gated playbook to the sent ask; `total` is what the
    payment sheet shows."""
    p, h = _armed(playbook)
    _feed(h, p.advance(h), _sheet(total))  # move open landed on the sheet
    send = p.advance(h)
    assert send is not None and send.tool_names() == ["note", "run_macro"]
    assert send.tool_calls[1].arguments["name"] == "channel/send"
    return p, h, send


def _reply_arrives(p, h, send, bubble: str):
    """Drive one wait+peek round ending with `bubble` as the new reply;
    returns the walk's next step."""
    ask = send.tool_calls[1].arguments["inputs"]["message"]
    _feed(h, send, _thread((ask, 0.75, 0.3)))
    _feed(h, p.advance(h), "waited")
    peek = p.advance(h)
    assert peek.tool_names() == ["note", "peek"]
    _feed(h, peek, _thread((ask, 0.75, 0.3), (bubble, 0.25, 0.5)))
    return p.advance(h)


def _suspend_via_silence(p, h, send) -> str:
    """Drive the full silent-round cycle to the suspension; returns the ask."""
    ask = send.tool_calls[1].arguments["inputs"]["message"]
    thread = _thread((ask, 0.75, 0.3))
    _feed(h, send, thread)
    step = p.advance(h)
    for _ in range(limits.DEFAULT_ASK_ROUNDS):
        assert step.tool_names() == ["note", "wait"]
        _feed(h, step, "waited")
        peek = p.advance(h)
        _feed(h, peek, thread)
        step = p.advance(h)
    assert step.tool_names() == ["note", "end_session"]
    assert step.tool_calls[1].arguments["status"] == "WAIT"
    assert p.outcome == "suspended"  # recorded BEFORE the close is minted
    return ask


def test_gate_ask_quotes_the_sheet_total() -> None:
    _, _, send = _at_gate()

    ask = send.tool_calls[1].arguments["inputs"]["message"]
    assert "¥45" in ask and "好的" in ask


def test_check_warns_when_a_gate_ask_quotes_no_deny_word() -> None:
    # Advisory, never blocking: an ask whose message quotes none of its
    # own words still works — a reply in other words just hands over.
    quiet = GATED.replace("回复 好的 确认支付，或 不用 取消", "veuillez répondre")
    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"pay": quiet})

    spec, pack = build.load_spec("demo", "pay", require_live=False)
    warnings = [w for w in lints.readiness_warnings(spec, pack) if "yes/no" in w]

    (warning,) = warnings
    assert "hands the walk over" in warning


def test_check_warns_about_weak_unpinned_anchors() -> None:
    # The fixture's `results` page identifies itself by 综合 alone, two
    # characters pinned to no band: read exactly and matched anywhere,
    # so the checker says so — advisory, never a refusal. The longer
    # anchors (Files, AllDone) draw no warning.
    write_pack(playbooks={"flow": FLOW})
    spec, pack = build.load_spec("demo", "flow", require_live=False)

    warnings = [w for w in lints.readiness_warnings(spec, pack) if "anchor" in w]

    assert [w.split("'")[1] for w in warnings] == ["results"]
    assert "综合" in warnings[0] and "within" in warnings[0]


def test_gate_confirm_resumes_and_pays_under_the_predicates() -> None:
    p, h, send = _at_gate()

    back = _reply_arrives(p, h, send, "好的")  # confirmed → the resume macro
    assert back.tool_names() == ["note", "run_macro"]
    assert back.tool_calls[1].arguments["name"] == "demo/open-app"
    assert "user confirmed" in back.tool_calls[0].arguments["summary"]

    _feed(h, back, _sheet())  # back on the sheet, same total
    pay = p.advance(h)
    assert pay is not None and pay.tool_calls[1].arguments["name"] == "demo/add-cart"


def test_gate_blocks_when_the_sheet_changed_after_consent() -> None:
    p, h, send = _at_gate()
    back = _reply_arrives(p, h, send, "好的")

    _feed(h, back, _sheet("¥69"))  # the total drifted after the confirm

    summary = _finish(p, h, p.advance(h))  # staleness predicate → hand over
    assert "sheet changed after consent" in summary


def test_gate_patience_is_the_asks_own() -> None:
    # `wait:` and `rounds:` — the poll cadence and the silent rounds
    # before the session suspends are the ask's declaration, not code.
    patient = GATED.replace(
        '    yes: ["好的"]\n', '    yes: ["好的"]\n    wait: 10\n    rounds: 1\n'
    )
    p, h, send = _at_gate(playbook=patient)
    ask = send.tool_calls[1].arguments["inputs"]["message"]
    thread = _thread((ask, 0.75, 0.3))
    _feed(h, send, thread)

    wait = p.advance(h)
    assert wait.tool_names() == ["note", "wait"]
    assert wait.tool_calls[1].arguments == {"seconds": 10}
    _feed(h, wait, "waited")
    _feed(h, p.advance(h), thread)  # the one silent peek
    assert p.advance(h).tool_names() == ["note", "end_session"]


def test_gate_deny_hands_over_without_reasking() -> None:
    p, h, send = _at_gate()

    step = _reply_arrives(p, h, send, "不用")

    summary = _finish(p, h, step)
    assert "user declined" in summary and "back out" in summary


def test_gate_reply_outside_the_declared_words_is_read_in_the_thread() -> None:
    # "那就来一份吧" is a yes in spirit, but the ask declared 好的/不用: the
    # words do not decide, so the model that read the request reads the
    # reply — in the session's thread, with the ask and the replies.
    from physiclaw.conductor.walk.micro import READ_REPLY, DecisionRequest

    p, h, send = _at_gate()
    ask = send.tool_calls[1].arguments["inputs"]["message"]

    step = _reply_arrives(p, h, send, "那就来一份吧")

    assert isinstance(step, DecisionRequest) and step.call == READ_REPLY
    assert step.material["ask"] == ask and step.listing == "那就来一份吧"
    assert "sent to the user" in step.material["since"]  # the ask, from the ledger
    assert step.thinking == "off"  # the ask's own `think:`


def test_a_reply_the_model_reads_as_confirm_binds_consent_like_a_yes() -> None:
    from physiclaw.conductor.walk.micro import MicroOutcome

    p, h, send = _at_gate()
    _reply_arrives(p, h, send, "那就来一份吧")

    step = p.resolve(
        MicroOutcome(out="confirm", reason="a yes in spirit", confidence=0.9)
    )

    assert p.gate.consented == 45.0 and step is not None
    assert "read by the model" in p.ledger.events[-1]
    assert len(p.thread.turns) == 2  # the reading joined the session's thread


def test_a_reply_the_model_reads_as_deny_hands_over_as_a_no() -> None:
    from physiclaw.conductor.walk.micro import MicroOutcome

    p, h, send = _at_gate()
    _reply_arrives(p, h, send, "算了吧")

    step = p.resolve(MicroOutcome(out="deny", reason="a refusal", confidence=0.9))

    summary = _finish(p, h, step)
    assert "user declined the ask" in summary and p.gate.consented is None


def test_a_reply_the_model_cannot_read_hands_over_before_any_payment() -> None:
    from physiclaw.conductor.walk.micro import MicroOutcome

    p, h, send = _at_gate()
    _reply_arrives(p, h, send, "多少钱来着")

    step = p.resolve(MicroOutcome(out="other", reason="a question", confidence=0.9))

    summary = _finish(p, h, step)
    assert "matches none of its yes/no words" in summary and "多少钱来着" in summary
    assert p.gate.consented is None


def test_a_declared_yes_never_calls_the_model_but_lands_in_the_ledger() -> None:
    p, h, send = _at_gate()

    step = _reply_arrives(p, h, send, "好的")

    assert step is not None and not hasattr(step, "call")  # no decision request
    assert p.gate.consented == 45.0
    assert any("user confirmed" in n and "'好的'" in n for n in p.ledger.events)


def test_gate_silence_suspends_and_resumes_on_next_wake() -> None:
    p, h, send = _at_gate()
    ask = _suspend_via_silence(p, h, send)

    # Next wake: the suspended walk resumes straight into a reply check.
    resumed = setup.load_suspended()
    assert resumed is not None and setup.load_suspended() is None  # one-shot
    h2 = _history()
    peek = resumed.advance(h2)
    assert peek.tool_names() == ["note", "peek"]
    _feed(h2, peek, _thread((ask, 0.75, 0.3), ("好的", 0.25, 0.5)))
    back = resumed.advance(h2)
    assert back.tool_calls[1].arguments["name"] == "demo/open-app"


def test_gate_resume_off_thread_reopens_via_channel_open() -> None:
    p, h, send = _at_gate()
    ask = _suspend_via_silence(p, h, send)

    resumed = setup.load_suspended()
    h2 = _history()
    peek = resumed.advance(h2)
    _feed(h2, peek, ELSEWHERE)  # user left the phone on another app

    reopen = resumed.advance(h2)
    assert reopen.tool_calls[1].arguments["name"] == "channel/open"
    _feed(h2, reopen, _thread((ask, 0.75, 0.3), ("好的", 0.25, 0.5)))
    back = resumed.advance(h2)
    assert back.tool_calls[1].arguments["name"] == "demo/open-app"


TELLING = """\
description: 汇报进展
inputs:
  keyword:
    description: what
route:
  - page: home
  - do: open
    macro: open-app
    with: {message: "{inputs.keyword}"}
  - page: results
  - tell: tell
    message: "已下单{inputs.keyword}，稍后汇报进度"
  - do: wrap
    macro: add-cart
    with: {message: "done"}
  - page: done
"""


def test_tell_sends_then_the_walk_moves_on() -> None:
    # Fire-and-forget: the send lands on the thread and the cursor moves
    # to the next node in the same session — nothing suspends, nothing
    # waits for a reply. (The next move's enter check then reads the
    # thread the send left the phone on — the readiness advisory's
    # case — and hands over with no recover declared.)
    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"notify": TELLING})
    p = _program(name="notify", keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)  # the start page
    _feed(h, p.advance(h), RESULTS)  # move open landed
    send = p.advance(h)
    assert send.tool_calls[1].arguments["name"] == "channel/send"
    text = send.tool_calls[1].arguments["inputs"]["message"]
    # `message:` IS the sent text — refs filled, nothing code-appended.
    assert text == "已下单milk，稍后汇报进度"
    _feed(h, send, _thread((text, 0.75, 0.3)))

    summary = _finish(p, h, p.advance(h))

    assert "move 'wrap' expects page 'results'" in summary
    assert not suspension.suspended_path().exists()
    assert p.idx == 2  # the cursor stood on `wrap`, past the tell


def test_tell_landing_off_the_thread_hands_over() -> None:
    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"notify": TELLING})
    p = _program(name="notify", keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)
    _feed(h, p.advance(h), RESULTS)
    send = p.advance(h)
    _feed(h, send, HOME)  # the send macro did not reach the thread

    summary = _finish(p, h, p.advance(h))
    assert "did not land on the thread" in summary


# ---------- activation / session_setup ----------


def test_session_setup_builds_the_boot_and_hidden_registry() -> None:
    # The boot is the channel pack's own playbook, materialized beside
    # a channel that has none: a dry Program over its route, carrying
    # the activation (the menu of enabled playbooks) for its `activate`
    # step, and the whole dispatch table beside it.
    from physiclaw.common import paths
    from physiclaw.conductor.spec.model import ActivateNode

    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"flow": FLOW})

    prog, hidden = setup.session_setup()

    assert prog is not None
    assert prog.app == "channel" and prog.spec.name == "boot" and prog.dry
    assert (paths.playbooks_dir() / "channel" / "boot" / "PLAYBOOK.yml").exists()
    assert isinstance(prog.spec.nodes[-1], ActivateNode)
    activation = prog.activation
    assert activation is not None
    assert tuple(activation.entries) == ("demo/flow",)
    menu = activation._menu()
    assert "demo/flow" in menu and "keyword" in menu
    assert set(hidden) == {
        "channel/send",
        "channel/open",
        "demo/open-app",
        "demo/add-cart",
    }


def test_session_setup_builds_no_boot_without_an_open_macro() -> None:
    # The boot names `open` as its hand; a channel that cannot open the
    # thread has no live boot: a plain model session, and no pack
    # discovery paid for nothing.
    write_channel()  # send only
    write_pack(
        playbooks={
            "flow": FLOW.replace(
                "description: two moves", "description: two moves\nenabled: true"
            )
        }
    )

    prog, hidden = setup.session_setup()

    assert prog is None
    assert set(hidden) == {"channel/send"}


def test_session_setup_builds_no_boot_without_an_enabled_playbook() -> None:
    # Nothing to offer, nothing to boot for — no wake pays the boot's
    # turns to read a thread no playbook could answer.
    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"flow": FLOW.replace("enabled: true", "enabled: false")})
    (paths_mod.playbooks_dir() / "demo" / "flow" / "PLAYBOOK.yml").write_text(
        "name: flow\nenabled: false\n" + FLOW, encoding="utf-8"
    )

    prog, hidden = setup.session_setup()

    assert prog is None
    assert "channel/open" in hidden and "demo/open-app" in hidden


def test_session_setup_prefers_a_suspended_walk_over_the_boot() -> None:
    from physiclaw.common.logger import write_json_atomic

    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"flow": FLOW})
    write_json_atomic(
        suspension.suspended_path(),
        {
            "schema": suspension.SUSPENDED_SCHEMA,
            "app": "demo",
            "playbook": "flow",
            "idx": 0,
            "values": {"keyword": "milk"},
        },
    )

    prog, hidden = setup.session_setup()

    assert prog is not None and prog.channel is not None
    assert prog.spec.name == "flow"  # the suspension, not the boot


def test_activation_builds_a_request_over_the_thread_screen() -> None:
    from physiclaw.common.listing import Screen
    from physiclaw.conductor.walk.micro import PARSE_TASK, MicroOutcome

    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"flow": FLOW})
    boot, _ = setup.session_setup()
    assert boot is not None and boot.activation is not None
    activation = boot.activation

    # The caller establishes the screen IS the thread (the boot's enter
    # check read it) — this turns it into the call, opening the walk's
    # thread at the think level the boot's step declares.
    boot.screen = Screen.read(_thread(("买牛奶", 0.25, 0.4)))
    req = activation.request(boot, "parse", "off")
    assert req is not None and req.call == PARSE_TASK and req.node_id == "parse"
    assert "买牛奶" in req.listing and "demo/flow" in req.material["menu"]
    assert req.thinking == "off" and boot.thread.thinking == "off"

    prog = activation.build(
        MicroOutcome(
            out="demo/flow",
            reason="purchase task",
            confidence=0.9,
            payload={"keyword": "牛奶"},
        ),
        boot.thread,
    )
    assert prog.thread is boot.thread  # the walk extends the boot's thread
    assert prog is not None
    assert prog.values == {"keyword": "牛奶"} and prog.channel is activation.channel


def test_activation_menu_renders_the_input_example() -> None:
    # The authored `example:` is the extraction hint — it must reach the
    # parse_task menu, or a keyword input has only prose to shape its
    # value ("五常大米 5kg" beats any rule about quantity words).
    write_channel(CHANNEL_OPEN)
    spec = FLOW.replace(
        "    description: what to search\n",
        "    description: what to search\n    example: rice 5kg\n",
    )
    write_pack(playbooks={"flow": spec})
    boot, _ = setup.session_setup()
    assert boot is not None and boot.activation is not None

    menu = boot.activation._menu()

    assert "keyword (what to search; e.g. rice 5kg)" in menu


def test_activation_rejects_unresolvable_inputs_and_not_a_task() -> None:
    from physiclaw.conductor.walk.micro import MicroOutcome

    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"flow": FLOW})
    boot, _ = setup.session_setup()
    assert boot is not None and boot.activation is not None
    activation = boot.activation

    assert activation.build(None, boot.thread) is None
    assert (
        activation.build(
            MicroOutcome(out="not_a_task", reason="chat", confidence=0.9), boot.thread
        )
        is None
    )
    # keyword is required; an empty extraction cannot activate.
    assert (
        activation.build(
            MicroOutcome(out="demo/flow", reason="task", confidence=0.9, payload={}),
            boot.thread,
        )
        is None
    )


def test_scaffolded_channel_pack_parses_and_loads_disabled() -> None:
    from physiclaw.conductor.spec import scaffold

    scaffold.init_pack("channel")

    ch = channel.load_channel()
    # Pages parse and the thread page exists; the macros are scaffolded
    # DISABLED (rehearse, then enable), so the sends stay unavailable —
    # and the boot, which names `open`, is not live until it is.
    assert ch is not None
    assert ch.send is None and ch.open is None
    assert ch.boot is None and ch.pack is not None
    from physiclaw.conductor.spec.pack import load_pack

    pack = load_pack("channel")
    assert set(pack.macros) == {"send", "open"} and not pack.macro_errors


def test_gate_ask_is_the_filled_template_exactly() -> None:
    # The playbook owns every word; the conductor owns only the slots:
    # the ask is `message:` with {inputs.keyword} and {ask.total} filled —
    # nothing appended, nothing reworded.
    _, _, send = _at_gate()

    ask = send.tool_calls[1].arguments["inputs"]["message"]
    assert ask == "已选好milk，合计 ¥45。回复 好的 确认支付，或 不用 取消。"


def test_suspension_persists_consent_across_the_wake() -> None:
    # The consented total rides the projection (the Gate half of
    # `Program.state`), so a walk restored from it can never resume into
    # a refused payment.
    from physiclaw.common.logger import write_json_atomic

    p, h, send = _at_gate()
    _reply_arrives(p, h, send, "好的")
    assert p.gate.consented == 45.0
    write_json_atomic(suspension.suspended_path(), p.state())

    resumed = setup.load_suspended()

    assert resumed is not None and resumed.gate.consented == 45.0


def test_suspend_status_literal_matches_the_sentinel() -> None:
    # program.py spells WAIT literally (the conductor may not import
    # engine runtimes); this pins its constant to the sentinel's spelling.
    from physiclaw.agent.runtime.sentinel import WAIT

    assert program.SUSPEND_STATUS == WAIT


def test_done_status_literal_matches_the_sentinel() -> None:
    from physiclaw.agent.runtime.sentinel import DONE

    assert program.DONE_STATUS == DONE


# ---------- regressions: money and suspension holes ----------


def test_payment_never_fires_off_an_unverified_screen() -> None:
    # The gate's own ask bubble quotes the consented total — if the
    # resume macro fails to leave the thread, the money predicates must
    # not be satisfied by our own message.
    p, h, send = _at_gate()
    back = _reply_arrives(p, h, send, "好的")  # confirmed → resume macro

    # The resume macro FAILS to leave the messenger: the walk sees the
    # chat list (¥ amounts may sit in previews) — no verified demo page.
    _feed(
        h,
        back,
        make_screen(("Weixin", 0.5, 0.05), ("合计 ¥45 昨天", 0.3, 0.2)).text,
    )

    summary = _finish(p, h, p.advance(h))  # blind money refused, hand over
    assert "move 'pay' expects page" in summary
    # Consent was bound but never consumed — the brief must say so.
    assert "consented to ¥45" in summary and "NOT been made" in summary


def test_pay_consumes_the_consent() -> None:
    p, h, send = _at_gate()
    back = _reply_arrives(p, h, send, "好的")
    _feed(h, back, _sheet())
    pay = p.advance(h)

    assert pay.tool_calls[1].arguments["name"] == "demo/add-cart"
    assert p.gate.consented is None  # spent at fire — no leftovers


def test_suspended_idx_outside_the_spec_drops_the_suspension() -> None:
    p, h, send = _at_gate()
    _suspend_via_silence(p, h, send)

    susp_file = suspension.suspended_path()
    data = json.loads(susp_file.read_text(encoding="utf-8"))
    data["idx"] = 99  # spec edited shorter between wakes
    susp_file.write_text(json.dumps(data), encoding="utf-8")

    assert setup.load_suspended() is None  # dropped, not a fake completion


def test_blocked_suspend_end_session_drops_the_suspension() -> None:
    p, h, send = _at_gate()
    ask = send.tool_calls[1].arguments["inputs"]["message"]
    thread = _thread((ask, 0.75, 0.3))
    _feed(h, send, thread)
    step = p.advance(h)
    for _ in range(limits.DEFAULT_ASK_ROUNDS):  # silent rounds → suspend turn
        _feed(h, step, "waited")
        peek = p.advance(h)
        _feed(h, peek, thread)
        step = p.advance(h)
    assert step.tool_names() == ["note", "end_session"]

    _feed(h, step, "BLOCKED", error=True)  # end_session refused

    summary = _finish(p, h, p.advance(h))
    assert "suspension dropped" in summary
    assert setup.load_suspended() is None  # stale suspension not resurrected


def _suspended_ask():
    """Walk GATED to its suspension turn (the ask ran out of patience);
    returns (program, history, the end_session turn — not yet fed)."""
    p, h, send = _at_gate()
    ask = send.tool_calls[1].arguments["inputs"]["message"]
    thread = _thread((ask, 0.75, 0.3))
    _feed(h, send, thread)
    step = p.advance(h)
    for _ in range(limits.DEFAULT_ASK_ROUNDS):
        _feed(h, step, "waited")
        peek = p.advance(h)
        _feed(h, peek, thread)
        step = p.advance(h)
    assert step.tool_names() == ["note", "end_session"]
    return p, h, step


def test_missing_suspend_result_drops_the_suspension_file() -> None:
    # The suspension file is written before end_session; if that result
    # never lands, the session may run on — a dead walk must not
    # resurrect.
    p, h, susp = _suspended_ask()
    assert suspension.suspended_path().exists()
    h.append(susp)  # the end_session result never arrives

    summary = _finish(p, h, p.advance(h))
    assert "suspension dropped" in summary
    assert not suspension.suspended_path().exists()


def test_clear_suspended_drops_a_suspension_file() -> None:
    _suspended_ask()

    assert suspension.clear_suspended() is True
    assert not suspension.suspended_path().exists()


def _write_suspended(playbook: str, idx: int, **over) -> None:
    """suspended.json with the boilerplate defaulted — tests pass only
    their deltas. The production shape is `Program.state`; a
    schema change is edited here once beside the tests that fake it."""
    from physiclaw.common.logger import write_json_atomic

    data = {
        "schema": suspension.SUSPENDED_SCHEMA,
        "app": "demo",
        "playbook": playbook,
        "idx": idx,
        "values": {},
        "outputs": {},
        "ask_text": "",
        "baseline": [],
        "quoted": None,
        "consented": None,
        "seen": [],
        "awaiting": False,
    }
    data.update(over)
    write_json_atomic(suspension.suspended_path(), data)


def test_payment_gate_total_is_quoted_only_off_a_verified_page() -> None:
    # A suspended resume straight onto the gate with an unknown screen
    # must hand over, not quote max(¥) off whatever the camera saw.
    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"pay": GATED})
    _write_suspended("pay", 1, values={"keyword": "milk"})  # idx 1 = the ask
    p = setup.load_suspended(channel.load_channel())
    assert p is not None
    h = _history()
    _feed(h, p.advance(h), ELSEWHERE)  # unknown screen at the gate

    summary = _finish(p, h, p.advance(h))  # handover — no ask was sent
    assert "refusing to ask blind" in summary


TWO_ASKS = """\
description: two asks
inputs:
  keyword:
    description: what
route:
  - page: home
  - do: open
    macro: open-app
    with: {message: "{inputs.keyword}"}
  - page: results
  - ask: address
    approve: address
    message: "地址没变吧？回复 好的 或 不用"
    yes: ["好的"]
    no: ["不用", "cancel"]
    resume: open-app
  - ask: handoff
    approve: handoff
    message: "现在下单吗？回复 好的 或 不用"
    yes: ["好的"]
    no: ["不用", "cancel"]
"""


def test_second_ask_reads_a_deny_sent_meanwhile() -> None:
    # The user cancels while the walk is off in the app between two
    # asks; the "cancel" sits on the thread when the second send lands.
    # Overwriting the baseline would swallow it forever — the send must
    # read it first.
    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"two": TWO_ASKS})
    p = _program(name="two", keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)
    _feed(h, p.advance(h), RESULTS)
    send = p.advance(h)
    back = _reply_arrives(p, h, send, "好的")  # the first ask confirmed
    assert back.tool_calls[1].arguments["name"] == "demo/open-app"
    _feed(h, back, RESULTS)
    resend = p.advance(h)
    ask1 = send.tool_calls[1].arguments["inputs"]["message"]
    ask2 = resend.tool_calls[1].arguments["inputs"]["message"]
    _feed(
        h, resend, _thread((ask1, 0.75, 0.2), ("cancel", 0.25, 0.4), (ask2, 0.75, 0.6))
    )

    summary = _finish(p, h, p.advance(h))
    assert "user declined" in summary


def test_a_deny_swept_at_the_asks_landing_is_answered_with_its_line() -> None:
    # Same sweep, but the second ask declares `denied:` — the ask IS the
    # node in scope at its own landing, so the walk answers the swept
    # "cancel" with that line before the entry's word ends the walk.
    write_channel(CHANNEL_OPEN)
    answering = TWO_ASKS.replace(
        "    approve: handoff\n", '    approve: handoff\n    denied: "已取消。"\n'
    )
    write_pack(playbooks={"two": answering})
    p = _program(name="two", keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)
    _feed(h, p.advance(h), RESULTS)
    send = p.advance(h)
    back = _reply_arrives(p, h, send, "好的")
    _feed(h, back, RESULTS)
    resend = p.advance(h)
    ask1 = send.tool_calls[1].arguments["inputs"]["message"]
    ask2 = resend.tool_calls[1].arguments["inputs"]["message"]
    _feed(
        h, resend, _thread((ask1, 0.75, 0.2), ("cancel", 0.25, 0.4), (ask2, 0.75, 0.6))
    )

    ack = p.advance(h)

    assert ack.tool_names() == ["note", "run_macro"]
    assert ack.tool_calls[1].arguments["inputs"]["message"] == "已取消。"
    _feed(h, ack, _thread((ask2, 0.75, 0.2), ("已取消。", 0.75, 0.4)))
    summary = _finish(p, h, p.advance(h))
    assert "user declined the ask, answered" in summary


def test_second_ask_reads_a_yes_that_repeats_the_first() -> None:
    # Both asks take "好的"; the first reply is still on screen (and in
    # the second ask's baseline) when the user says "好的" again.
    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"two": TWO_ASKS})
    p = _program(name="two", keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)
    _feed(h, p.advance(h), RESULTS)
    send = p.advance(h)
    back = _reply_arrives(p, h, send, "好的")
    _feed(h, back, RESULTS)
    resend = p.advance(h)
    ask1 = send.tool_calls[1].arguments["inputs"]["message"]
    ask2 = resend.tool_calls[1].arguments["inputs"]["message"]
    thread = [(ask1, 0.75, 0.2), ("好的", 0.25, 0.35), (ask2, 0.75, 0.5)]
    _feed(h, resend, _thread(*thread))
    _feed(h, p.advance(h), "waited")
    _feed(h, p.advance(h), _thread(*thread, ("好的", 0.25, 0.7)))

    summary = _finish(p, h, p.advance(h))
    assert "completed" in summary


def test_gate_seen_amounts_survive_the_suspension() -> None:
    gate = program.Gate(quoted=45.0, consented=45.0, seen=(45.0, 79.0))

    restored = program.Gate.from_suspended(gate.to_suspended())

    assert restored.seen == (45.0, 79.0) and restored.consented == 45.0


# ---------- payment gate: total edges ----------


def test_consent_binds_to_the_quoted_total() -> None:
    # Whatever the sheet says is what the user is asked about and what
    # they consent to — there is no other bound.
    p, h, send = _at_gate("¥145")
    assert "¥145" in send.tool_calls[1].arguments["inputs"]["message"]

    _reply_arrives(p, h, send, "好的")

    assert p.gate.consented == 145.0


def test_gate_hands_over_when_no_total_is_readable() -> None:
    # The sheet page verified but shows no ¥ amount: the ask IS the
    # consent record, so with nothing to quote the gate refuses to ask.
    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"pay": GATED})
    p = _program(name="pay", keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)  # the start page
    _feed(h, p.advance(h), RESULTS)  # the verified results page — no ¥ on it

    step = p.advance(h)  # handover, no ask sent
    assert "no amount readable beside 合计" in _finish(p, h, step)


def test_payment_move_without_consent_hands_over() -> None:
    # A resume landing directly ON the payment move with no consent
    # recorded (the gate never confirmed): money never fires. This is
    # `money.fire_block`'s first predicate — the last line of defense
    # if every earlier guard were somehow skipped.
    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"pay": GATED})
    spec, _ = build.load_spec("demo", "pay")
    pay_idx = next(
        i for i, n in enumerate(spec.nodes) if getattr(n, "irreversible", None)
    )
    _write_suspended(
        "pay",
        pay_idx,
        values={"keyword": "milk"},
        quoted=45.0,
        # consented stays None — the gate never opened.
    )
    p = setup.load_suspended(channel.load_channel())
    assert p is not None
    h = _history()
    _feed(h, p.advance(h), _sheet())  # verified sheet at the pay move

    step = p.advance(h)  # money never fires blind
    assert "without a confirmed total" in _finish(p, h, step)


# ---------- declared recovery ----------


LOCKED_MID = make_screen(("Enter Passcode", 0.5, 0.5)).text

LANDMARKS = """\
back:
  label: "back chevron"
  at: [0.02, 0.05, 0.10, 0.10]
"""

# FLOW with declared hands: home force-quits, results pops back with
# the OS gesture.
RECOVERING = FLOW.replace(
    "  - page: home\n",
    "  - page: home\n    recover: force_quit\n",
).replace(
    "  - page: results\n",
    "  - page: results\n    recover: go_back\n",
)


def _recovering_walk(flow: str = RECOVERING):
    """A walk with declared hands, driven past the opening peek to move 1."""
    write_pack(playbooks={"flow": flow}, landmarks=LANDMARKS)
    p = _program(keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)  # the start page
    move1 = p.advance(h)
    assert move1 is not None and move1.tool_names() == ["note", "run_macro"]
    return p, h, move1


def test_wrong_page_on_resume_runs_the_hand_then_the_move() -> None:
    # A resumed walk stands on `home` while its move expects `results`:
    # the page's declared hand (go_back) runs, the enter check re-runs
    # on the restored page, and the move fires — the cursor never moved.
    write_pack(playbooks={"flow": RECOVERING}, landmarks=LANDMARKS)
    _write_suspended("flow", 1, values={"keyword": "milk"})
    p = setup.load_suspended(channel.load_channel())
    assert p is not None
    h = _history()
    _feed(h, p.advance(h), HOME)  # `search` enters at results — wrong page
    back = p.advance(h)
    assert back is not None and back.tool_names() == ["note", "go_back"]
    assert "declared hand" in back.tool_calls[0].arguments["summary"]

    _feed(h, back, RESULTS)
    move2 = p.advance(h)
    assert move2 is not None
    assert move2.tool_calls[1].arguments["name"] == "demo/add-cart"
    assert "recovered demo.results" in move2.tool_calls[0].arguments["summary"]


def test_a_stepping_position_recovers_in_place() -> None:
    # A hand that does not restore its page runs again in place: the
    # cursor never moves, so the prefix the author stepped past never
    # re-runs (a resumed suspension: `test_resumed_walk_never_re_runs…`).
    write_pack(playbooks={"flow": RECOVERING}, landmarks=LANDMARKS)
    spec, pack = build.load_spec("demo", "flow", require_live=False)
    at_search = {**_program(keyword="milk").state(), "idx": 1}

    stepped = build.build_program(
        spec, pack, {"keyword": "milk"}, None, position=at_search, dry=True
    )
    h = _history()
    _feed(h, stepped.advance(h), ELSEWHERE)
    back = stepped.advance(h)
    assert back is not None and back.tool_names() == ["note", "go_back"]
    _feed(h, back, ELSEWHERE)  # the hand did not restore results
    again = stepped.advance(h)
    assert again is not None and again.tool_names() == ["note", "go_back"]
    assert stepped.idx == 1 and "still off" in again.tool_calls[0].arguments["summary"]


def test_declared_unlock_hand_wakes_the_phone_then_continues() -> None:
    # Nothing unlocks in the background: the page declares the
    # `unlock_phone` hand, and only then does a locked phone get woken.
    flow = FLOW.replace(
        "  - page: results\n", "  - page: results\n    recover: unlock_phone\n"
    )
    p, h, move1 = _recovering_walk(flow)
    _feed(h, move1, LOCKED_MID)

    unlock = p.advance(h)
    assert unlock is not None and unlock.tool_names() == ["note", "unlock_phone"]

    _feed(h, unlock, RESULTS)
    move2 = p.advance(h)
    assert move2 is not None
    assert move2.tool_calls[1].arguments["name"] == "demo/add-cart"


def test_blocked_move_hands_over() -> None:
    # A blocked tool result hands over — nothing re-runs in the
    # background, whatever the failure text says.
    p, h, move1 = _recovering_walk()
    _feed(h, move1, "tool run_macro failed: bridge unreachable", error=True)

    summary = _finish(p, h, p.advance(h))
    assert "blocked or failed" in summary


def test_aborted_move_is_judged_by_its_landing_page() -> None:
    # A macro that ABORTS mid-run is a result, not an error (the engine
    # and the rehearsal driver return it the same way): its landing page
    # does not read, so the page's declared hand runs — never a handover
    # for the abort itself.
    p, h, move1 = _recovering_walk()
    _feed(
        h,
        move1,
        "macro demo/open-app: ABORTED at step 2/3 (guard_failed) — "
        "steps 1-1 already executed. Do NOT re-run.\n" + ELSEWHERE,
    )

    back = p.advance(h)
    assert back is not None and back.tool_names() == ["note", "go_back"]
    assert "declared hand" in back.tool_calls[0].arguments["summary"]


def test_recover_tap_hand_falls_back_to_the_declared_bbox() -> None:
    # A tap hand whose label is not on screen presses the declared spot.
    flow = FLOW.replace(
        "  - page: results\n",
        "  - page: results\n    recover: {tap: landmarks.back}\n",
    )
    p, h, move1 = _recovering_walk(flow)
    _feed(h, move1, ELSEWHERE)  # unknown landing

    back = p.advance(h)

    assert back is not None and back.tool_names() == ["note", "tap"]
    assert back.tool_calls[1].arguments == {"bbox": [0.02, 0.05, 0.10, 0.10]}
    assert "declared hand" in back.tool_calls[0].arguments["summary"]


def test_hand_that_does_not_restore_runs_again_then_hands_over() -> None:
    # After the hand the page still does not read: the same hand runs
    # again within the page's tries, and the walk never goes back to an
    # earlier move to reach the page another way.
    p, h, move1 = _recovering_walk()
    _feed(h, move1, ELSEWHERE)
    back = p.advance(h)
    assert back.tool_names() == ["note", "go_back"]

    _feed(h, back, HOME)  # popped all the way to home

    again = p.advance(h)
    assert again is not None and again.tool_names() == ["note", "go_back"]
    assert "still off" in again.tool_calls[0].arguments["summary"]
    _feed(h, again, HOME)
    summary = _finish(p, h, p.advance(h))
    assert "recover tries (2) spent" in summary


OCCLUDABLE_PAGES = """\
home:
  anchors: ["Files"]
results:
  anchors: ["综合", "销量"]
done:
  anchors: ["AllDone"]
"""

KEYED = FLOW.replace(
    "  - page: results\n",
    "  - page: results\n    recover:\n"
    "      covered: {tap: landmarks.back}\n"
    "      elsewhere: go_back\n",
)


def _learn_results() -> None:
    """Calibrated geometry for `results` — an overlay verdict needs it."""
    from conductor_fakes import make_learned

    from physiclaw.conductor.spec import pages

    def anchor(text, cy):
        return make_learned(text, 0.5, cy, pos_tol=0.03)

    pages.save_learned(
        "demo",
        {
            "results": pages.LearnedPage(
                anchors={"综合": anchor("综合", 0.1), "销量": anchor("销量", 0.2)},
                observations=6,
            )
        },
    )


# `results` with its second anchor hidden under a coupon sheet: three
# unexpected labels in the band where 销量 should be.
COVERED_RESULTS = make_screen(
    ("综合", 0.5, 0.1),
    ("领券", 0.5, 0.18),
    ("消费券", 0.5, 0.2),
    ("马上去用", 0.5, 0.22),
).text


def test_keyed_recover_runs_the_hand_for_the_reading() -> None:
    # The page itself under an overlay → its `covered` hand (the scrim
    # tap); any other screen → its `elsewhere` hand (go_back).
    write_pack(playbooks={"flow": KEYED}, pages=OCCLUDABLE_PAGES, landmarks=LANDMARKS)
    _learn_results()
    p = _program(keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)
    move1 = p.advance(h)
    _feed(h, move1, COVERED_RESULTS)

    dismiss = p.advance(h)
    assert dismiss is not None and dismiss.tool_names() == ["note", "tap"]
    assert "(covered)" in dismiss.tool_calls[0].arguments["summary"]
    assert dismiss.tool_calls[1].arguments == {"bbox": [0.02, 0.05, 0.10, 0.10]}

    # The sheet is gone: both anchors read where they were learned.
    _feed(h, dismiss, make_screen(("综合", 0.5, 0.1), ("销量", 0.5, 0.2)).text)
    move2 = p.advance(h)
    assert move2 is not None and move2.tool_names() == ["note", "run_macro"]
    assert "recovered demo.results" in move2.tool_calls[0].arguments["summary"]

    # A fresh walk landing on an unknown screen takes the other hand.
    p2 = _program(keyword="milk")
    h2 = _history()
    _feed(h2, p2.advance(h2), HOME)
    _feed(h2, p2.advance(h2), ELSEWHERE)
    back = p2.advance(h2)
    assert back is not None and back.tool_names() == ["note", "go_back"]
    assert "(elsewhere)" in back.tool_calls[0].arguments["summary"]


def test_a_locked_phone_takes_the_locked_hand_and_no_other() -> None:
    # The matcher reads the lock screen by shape, so a page's hands key
    # it as `locked`: with none declared for it, the walk hands over
    # saying so (a go_back hand would land nowhere), and the reading
    # names the OS page rather than "no known page".
    keyed = FLOW.replace(
        "  - page: results\n",
        "  - page: results\n    recover:\n      elsewhere: go_back\n",
    )
    p, h, move1 = _recovering_walk(keyed)
    _feed(h, move1, LOCKED_MID)

    summary = _finish(p, h, p.advance(h))
    assert "declares no `locked` recover hand" in summary
    assert "ios.locked" in summary

    with_lock = keyed.replace(
        "      elsewhere: go_back\n",
        "      elsewhere: go_back\n      locked: unlock_phone\n",
    )
    p2, h2, m1 = _recovering_walk(with_lock)
    _feed(h2, m1, LOCKED_MID)
    unlock = p2.advance(h2)
    assert unlock is not None and unlock.tool_names() == ["note", "unlock_phone"]
    assert "(locked)" in unlock.tool_calls[0].arguments["summary"]


def test_keyed_recover_without_a_hand_for_the_reading_hands_over() -> None:
    only_occluded = FLOW.replace(
        "  - page: results\n",
        "  - page: results\n    recover:\n      covered: {tap: landmarks.back}\n",
    )
    p, h, move1 = _recovering_walk(only_occluded)
    _feed(h, move1, ELSEWHERE)

    summary = _finish(p, h, p.advance(h))
    assert "declares no `elsewhere` recover hand" in summary


def test_failed_payment_call_logs_the_purchase_and_briefs_it() -> None:
    # Consent is spent when the pay macro is dispatched; an error result
    # means money MAY have moved: the purchase line lands and the brief
    # says the payment fired with an unverified result.
    from physiclaw.common import daylog

    p, h, send = _at_gate()
    back = _reply_arrives(p, h, send, "好的")
    _feed(h, back, _sheet())
    pay = p.advance(h)
    assert pay.tool_calls[1].arguments["name"] == "demo/add-cart"
    _feed(h, pay, "macro demo/add-cart: ABORTED at step 1/1", error=True)

    summary = _finish(p, h, p.advance(h))

    assert "A payment of ¥45 was FIRED" in summary
    assert "payment ¥45 fired" in daylog.load_recent_entries(5)


def test_recovery_never_restarts_once_a_payment_fired() -> None:
    # A hand beside a fired payment could walk back into the ask and pay again:
    # after the payment move, a deviation is the model's even where the
    # page declares a hand.
    gated = GATED.replace(
        "  - page: home\n  - do: open",
        "  - page: home\n    recover: go_back\n  - do: open",
    )
    p, h, send = _at_gate(playbook=gated)
    back = _reply_arrives(p, h, send, "好的")
    _feed(h, back, _sheet())
    pay = p.advance(h)
    _feed(h, pay, ELSEWHERE)  # the pay macro did not land on home

    summary = _finish(p, h, p.advance(h))
    assert "did not land on 'home'" in summary


def test_resumed_walk_never_re_runs_the_nodes_before_its_cursor() -> None:
    # The nodes before the stored cursor ran on an earlier wake; a hand
    # that does not restore runs again in place, never `open` — so the
    # page's limit is what ends it, never a cross-wake loop.
    write_pack(playbooks={"flow": RECOVERING}, landmarks=LANDMARKS)
    _write_suspended("flow", 1, values={"keyword": "milk"})
    p = setup.load_suspended(channel.load_channel())
    assert p is not None
    h = _history()
    _feed(h, p.advance(h), HOME)  # `search` expects results
    back = p.advance(h)
    assert back.tool_names() == ["note", "go_back"]
    _feed(h, back, HOME)  # not restored

    again = p.advance(h)
    assert again.tool_names() == ["note", "go_back"]  # NOT `open` re-run
    _feed(h, again, HOME)

    summary = _finish(p, h, p.advance(h))
    assert "recover tries (2) spent" in summary


def test_resumed_walk_unlocks_a_locked_phone_before_reading() -> None:
    # A wake minutes later meets the cover: one unlock, then the opening
    # exactly as before — no walk-level hand can run before it reads.
    write_pack(playbooks={"flow": FLOW})
    _write_suspended("flow", 1, values={"keyword": "milk"})
    p = setup.load_suspended(channel.load_channel())
    assert p is not None
    h = _history()
    _feed(h, p.advance(h), LOCKED_MID)

    unlock = p.advance(h)
    assert unlock.tool_names() == ["note", "unlock_phone"]
    _feed(h, unlock, RESULTS)
    peek = p.advance(h)
    assert peek.tool_names() == ["note", "peek"]
    _feed(h, peek, RESULTS)

    move2 = p.advance(h)
    assert move2.tool_calls[1].arguments["name"] == "demo/add-cart"


def test_trailing_tell_completes_the_walk() -> None:
    write_channel()
    write_pack(playbooks={"flow": FLOW + '  - tell: finish\n    message: "all done"\n'})
    p = _program(keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)
    _feed(h, p.advance(h), RESULTS)
    _feed(h, p.advance(h), DONE)
    send = p.advance(h)
    assert send.tool_calls[1].arguments["name"] == "channel/send"
    _feed(h, send, _thread(("all done", 0.75, 0.3)))

    summary = _finish(p, h, p.advance(h))
    assert "completed" in summary
    assert not suspension.suspended_path().exists()


def test_check_warns_when_an_ask_without_resume_precedes_a_screen_move() -> None:
    flow = TWO_ASKS.replace("    resume: open-app\n", "") + (
        "  - page: results\n  - do: wrap\n    macro: add-cart\n"
        '    with: {message: "x"}\n  - page: done\n'
    )
    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"two": flow})
    spec, pack = build.load_spec("demo", "two", require_live=False)

    warnings = [w for w in lints.readiness_warnings(spec, pack) if "resume" in w]

    assert len(warnings) == 1 and "ask 'handoff'" in warnings[0]


def test_recovery_never_runs_with_consent_bound() -> None:
    # Money keeps the hard handover: a deviation after the user consented
    # is the model's, never a hand's.
    gated = GATED.replace(
        "  - page: results\n", "  - page: results\n    recover: go_back\n"
    )
    p, h, send = _at_gate(playbook=gated)
    back = _reply_arrives(p, h, send, "好的")
    _feed(h, back, HOME)  # the resume macro landed on the wrong page

    summary = _finish(p, h, p.advance(h))
    assert "move 'pay' expects page" in summary


# ---------- walk telemetry (runs.jsonl) ----------


def test_completed_walk_records_one_completed_run_line() -> None:
    from physiclaw.conductor.walk import walklog

    write_pack(playbooks={"flow": FLOW})
    p = _program(keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)
    _feed(h, p.advance(h), RESULTS)
    _feed(h, p.advance(h), DONE)

    _finish(p, h, p.advance(h))

    (row,) = walklog.load()
    assert row["outcome"] == "completed"
    assert (row["app"], row["playbook"]) == ("demo", "flow")
    assert row["node"] is None  # cursor past the last node
    assert row["micros"] == 1  # the close's record call, answered by nobody


def test_handover_records_run_line_at_the_failing_node() -> None:
    from physiclaw.conductor.walk import walklog

    p, h, move1 = _recovering_walk()
    _feed(h, move1, HOME)  # move 1 landed on the WRONG page
    _feed(h, p.advance(h), HOME)  # the declared hand — still wrong
    _feed(h, p.advance(h), HOME)  # the hand again — still wrong, tries spent
    _finish(p, h, p.advance(h))

    (row,) = walklog.load()
    assert row["outcome"] == "handover"
    assert row["node"] == "open"
    assert row["rescues"] >= 2


def test_completed_payment_walk_records_history_fields() -> None:
    # The completed line carries the structured fields: inputs and the
    # fired total (consent is consumed at fire — this is where it
    # survives).
    from physiclaw.conductor.walk import walklog

    p, h, send = _at_gate()
    back = _reply_arrives(p, h, send, "好的")
    _feed(h, back, _sheet())
    pay = p.advance(h)
    assert pay.tool_calls[1].arguments["name"] == "demo/add-cart"
    _feed(h, pay, HOME)  # the pay move's verify page

    _finish(p, h, p.advance(h))

    (row,) = walklog.load()
    assert row["outcome"] == "completed"
    assert row["total"] == 45.0
    assert row["values"] == {"keyword": "milk"}


def test_payment_fire_writes_the_doctrine_purchase_log_line() -> None:
    # The conductor is the one doing the purchasing, so it writes the
    # daily-log line itself, the moment the payment move's result lands.
    from physiclaw.common import daylog

    p, h, send = _at_gate()
    back = _reply_arrives(p, h, send, "好的")
    _feed(h, back, _sheet())
    pay = p.advance(h)
    _feed(h, pay, HOME)
    p.advance(h)  # the landing that judges the pay move — and logs

    entries = daylog.load_recent_entries(5)

    assert "conductor: demo: payment ¥45 fired (playbook demo/pay)" in entries


TRAILING_ASK = (
    GATED
    + """\
  - ask: rate
    approve: rating
    message: "满意吗？回复 好的 或 不用。"
    yes: ["好的"]
    no: ["不用"]
"""
)


def test_a_resumed_walk_does_not_log_last_wakes_payment_again() -> None:
    # The amount survives a suspension (the next wake must know money
    # moved); the line saying so was written by the wake that fired it.
    # A resumed walk must not write it a second time under today's date
    # — that record is what the doctrine reads to decide whether to buy
    # again.
    from physiclaw.common import daylog

    p, h, send = _at_gate(playbook=TRAILING_ASK)
    back = _reply_arrives(p, h, send, "好的")
    _feed(h, back, _sheet())
    pay = p.advance(h)
    _feed(h, pay, HOME)
    rate = p.advance(h)  # the landing logs the purchase; then the trailing ask
    assert rate.tool_calls[1].arguments["name"] == "channel/send"
    _suspend_via_silence(p, h, rate)
    line = "conductor: demo: payment ¥45 fired (playbook demo/pay)"
    assert daylog.load_recent_entries(20).count(line) == 1

    resumed = setup.load_suspended()
    assert resumed is not None and resumed.ledger.paid == 45.0
    h2 = _history()
    resumed.advance(h2)  # the wake opens on the reply check
    resumed.abandon()  # the session's teardown — one of the line's writers

    assert daylog.load_recent_entries(20).count(line) == 1


def test_suspend_writes_the_close_routine_log_line() -> None:
    # A conductor-suspended wake never runs the model, and the walk has
    # no per-step logs — without this line the suspension is invisible
    # to the next wake's memory window.
    from physiclaw.common import daylog

    p, h, send = _at_gate()
    _suspend_via_silence(p, h, send)

    entries = daylog.load_recent_entries(5)

    assert "conductor: demo/pay suspended" in entries
    assert "any wake resumes it" in entries


def test_session_setup_assembles_the_activation_context() -> None:
    # The agent's OWN memory convention feeds parse_task: the recent
    # daily-log window — the same record the model reads at wake, never
    # a conductor-private store (runs.jsonl stays telemetry).
    from physiclaw.common import daylog

    write_channel(CHANNEL_OPEN)
    daylog.append_log("[11:02] demo: bought milk ¥45 — reported to the user")
    write_pack(playbooks={"flow": FLOW})

    boot, _ = setup.session_setup()

    assert boot is not None and boot.activation is not None
    ctx = boot.activation.context
    assert "Recent daily-log entries" in ctx
    assert "bought milk ¥45" in ctx


def test_abandon_records_a_mid_flight_walk_and_breadcrumbs_it() -> None:
    # The killed-session path: the plugin's teardown abandons a walk cut
    # short — one telemetry row plus the daily-log breadcrumb, since
    # this walk had acted.
    from physiclaw.common import daylog
    from physiclaw.conductor.walk import walklog

    write_pack(playbooks={"flow": FLOW})
    p = _program(keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)
    p.advance(h)  # move 1 synthesized — the walk acted, then the session dies

    p.abandon()

    (row,) = walklog.load()
    assert row["outcome"] == "abandoned"
    assert row["node"] == "open"
    assert "cut short mid-walk at node open" in daylog.load_recent_entries(5)


def test_abandon_is_a_no_op_for_unstarted_and_closed_walks() -> None:
    from physiclaw.conductor.walk import walklog

    write_pack(playbooks={"flow": FLOW})
    fresh = _program(keyword="milk")
    fresh.abandon()  # never advanced — not a run
    assert walklog.load() == []

    p = _program(keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)
    _feed(h, p.advance(h), RESULTS)
    _feed(h, p.advance(h), DONE)
    _finish(p, h, p.advance(h))  # completed — latched

    p.abandon()

    (row,) = walklog.load()
    assert row["outcome"] == "completed"  # still exactly one row


def test_failed_agent_call_records_handover_with_micro_count() -> None:
    from physiclaw.conductor.walk import walklog
    from physiclaw.conductor.walk.micro import DecisionRequest

    write_pack(playbooks={"flow": AGENT_FLOW})
    p = _program(keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)
    step = p.advance(h)
    assert isinstance(step, DecisionRequest)

    _finish(p, h, p.resolve(None))  # the brokered call failed → handover

    (row,) = walklog.load()
    assert row["outcome"] == "handover"
    assert row["node"] == "parse"
    assert row["micros"] == 1


# ---------- inline macros (a move's embedded body) ----------


# FLOW's first move with the body embedded in place of `macro: open-app`.
INLINE_FLOW = FLOW.replace(
    "    macro: open-app\n",
    "    macro:\n"
    "      inputs:\n"
    "        message: {description: the text}\n"
    "      steps:\n"
    "        - home_screen\n",
)


def test_inline_move_dispatches_under_its_synthesized_name() -> None:
    # The whole wiring in one walk: parse synthesizes `flow.open`,
    # build_program merges it into the dispatch registry, and the
    # run_macro turn is name-keyed like any pack macro — refs filled
    # through the node's `with:` exactly as on the directory path.
    write_pack(playbooks={"flow": INLINE_FLOW})
    p = _program(keyword="milk")
    h = _history()
    _feed(h, p.advance(h), HOME)  # the start page

    move = p.advance(h)

    assert move is not None and move.tool_calls[1].arguments == {
        "name": "demo/flow.open",
        "inputs": {"message": "milk"},
    }
    assert "demo/flow.open" in p.pack_macros


def test_session_setup_hidden_registry_carries_inline_macros() -> None:
    # The registry is handed to the engine ONCE at wake — an activation
    # mid-session dispatches inline moves out of `hidden`, so they must
    # ride it beside the directory macros.
    write_channel(CHANNEL_OPEN)
    write_pack(playbooks={"flow": INLINE_FLOW})

    boot, hidden = setup.session_setup()

    assert boot is not None
    assert "demo/flow.open" in hidden


def test_suspended_walk_with_a_broken_spec_is_dropped() -> None:
    # load_suspended is fail-open: a spec that no longer parses drops the
    # suspension instead of taking the wake down.
    _suspended_ask()
    write_pack(playbooks={"pay": "description: broken\nroute: []"})

    assert setup.load_suspended() is None
    with pytest.raises(PlaybookError):
        build.load_spec("demo", "pay")


# ---------- on_fail: stop — the model never inherits the pay hand ----------

STOPPING = GATED.replace(
    "    resume: open-app\n", "    resume: open-app\n    on_fail: stop\n"
)


ANSWERING = STOPPING.replace(
    '    no: ["不用"]\n',
    '    no: ["不用"]\n    denied: "已取消，未付款 ¥{ask.total}。"\n',
)


def _deny_answered(p, h, send, playbook_reply: str = "不用"):
    """Drive a deny to the ask's `denied:` send and land it on the thread;
    returns the walk's next step (the exit the entry's word decides)."""
    ack = _reply_arrives(p, h, send, playbook_reply)
    assert ack.tool_names() == ["note", "run_macro"]
    args = ack.tool_calls[1].arguments
    assert args["name"] == "channel/send"
    assert args["inputs"]["message"] == "已取消，未付款 ¥45。"
    ask = send.tool_calls[1].arguments["inputs"]["message"]
    _feed(
        h,
        ack,
        _thread(
            (ask, 0.75, 0.3),
            (playbook_reply, 0.25, 0.5),
            (args["inputs"]["message"], 0.75, 0.7),
        ),
    )
    return p.advance(h)


def test_a_deny_is_answered_by_the_ask_and_then_the_stop_takes_it() -> None:
    # The user said no: the gate worked. The ask sends its own `denied:`
    # line (the total it quoted still fills), and only then does the
    # entry's `on_fail: stop` end the session — recorded as a fact, no
    # imperative for a model that never runs, no suspension.
    p, h, send = _at_gate(playbook=ANSWERING)

    stop = _deny_answered(p, h, send)

    assert stop.synthesized and stop.tool_names() == ["note", "end_session"]
    args = stop.tool_calls[1].arguments
    assert args["status"] == "WAIT"
    assert "user declined the ask, answered" in args["recap"]
    assert "back out" not in args["recap"] and "acknowledge" not in args["recap"]
    assert "nothing paid" in args["recap"]
    assert "已取消" in args["recap"]  # the answer is in the walk's account
    assert p.gate.consented is None and not p.gate.awaiting
    assert not suspension.suspended_path().exists()
    _feed(h, stop, "session ended")
    assert p.advance(h) is None


def test_a_deny_is_answered_by_the_ask_before_the_brief() -> None:
    # Same answer, the default word: the brief no longer asks the model
    # to acknowledge (that is done), only to back out and wrap up.
    p, h, send = _at_gate(playbook=ANSWERING.replace("    on_fail: stop\n", ""))

    summary = _finish(p, h, _deny_answered(p, h, send))

    assert "user declined the ask, answered" in summary
    assert "Back out" in summary and "acknowledge" not in summary


def test_a_deny_the_model_read_is_answered_too() -> None:
    from physiclaw.conductor.walk.micro import MicroOutcome

    p, h, send = _at_gate(playbook=ANSWERING)
    _reply_arrives(p, h, send, "算了吧")

    ack = p.resolve(MicroOutcome(out="deny", reason="a refusal", confidence=0.9))

    assert ack.tool_names() == ["note", "run_macro"]
    assert ack.tool_calls[1].arguments["inputs"]["message"] == "已取消，未付款 ¥45。"


def test_a_deny_without_a_declared_answer_takes_the_word_at_once() -> None:
    # No `denied:` — the stop ends the session with the fact alone; the
    # cost (silence to the user) is what `playbooks check` warns about.
    p, h, send = _at_gate(playbook=STOPPING)

    stop = _reply_arrives(p, h, send, "不用")

    assert stop.tool_names() == ["note", "end_session"]
    recap = stop.tool_calls[1].arguments["recap"]
    assert "user declined the ask" in recap and "acknowledge" not in recap


def test_the_ask_that_says_stop_ends_the_session_with_nothing_paid() -> None:
    # The ask's send fails (Saturday's case): instead of the brief that
    # would leave the model standing next to the pay hand, the walk
    # closes the session WAIT by its own end_session — recorded as a
    # handover, no suspension file, the next wake retries from the top.
    p, h, send = _at_gate(playbook=STOPPING)
    _feed(h, send, "macro send ABORTED at step 13/17 (guard_failed)", error=True)

    stop = p.advance(h)

    assert stop.synthesized and stop.tool_names() == ["note", "end_session"]
    args = stop.tool_calls[1].arguments
    assert args["status"] == "WAIT"
    assert "stopped at gate" in args["recap"] and "nothing paid" in args["recap"]
    assert p.outcome == "handover"
    assert not suspension.suspended_path().exists()
    _feed(h, stop, "session ended")
    assert p.advance(h) is None


@pytest.mark.parametrize(
    "anchor, result, fragment",
    [
        # a page waypoint that says stop: the move landed elsewhere and
        # the page declares no recover hand
        ("  - page: results\n", ELSEWHERE, "did not land on 'results'"),
        # a move that says stop: its macro was blocked
        (
            '    with: {message: "{inputs.keyword}"}\n',
            "BLOCKED — not executed",
            "stopped at open",
        ),
    ],
)
def test_an_entry_that_says_stop_ends_the_session_when_it_fails(
    anchor, result, fragment
) -> None:
    p, h = _armed(GATED.replace(anchor, f"{anchor}    on_fail: stop\n", 1))
    move = p.advance(h)
    _feed(h, move, result, error=result.startswith("BLOCKED"))

    stop = p.advance(h)

    assert stop.synthesized and stop.tool_names() == ["note", "end_session"]
    assert fragment in stop.tool_calls[1].arguments["recap"]
    assert p.outcome == "handover"


def test_a_page_that_says_handover_is_briefed_even_under_a_node_that_says_stop() -> (
    None
):
    # The page answers for itself in BOTH spellings: the author put
    # `handover` on the page the move must reach (the model verifies
    # what the move did) and `stop` on the move itself; an unreached
    # page briefs the model, it does not fall to the node's stop.
    p, h = _armed(
        GATED.replace("  - do: open\n", "  - do: open\n    on_fail: stop\n", 1).replace(
            "  - page: results\n", "  - page: results\n    on_fail: handover\n", 1
        )
    )
    move = p.advance(h)
    _feed(h, move, ELSEWHERE)

    summary = _finish(p, h, p.advance(h))

    assert "conductor handing over" in summary
    assert "did not land on 'results'" in summary
    assert p.outcome == "handover"


def test_before_the_gate_a_handover_still_briefs_the_model() -> None:
    p, h = _armed(STOPPING)
    move = p.advance(h)
    _feed(h, move, "BLOCKED — not executed", error=True)

    summary = _finish(p, h, p.advance(h))

    assert "conductor handing over" in summary


def test_after_a_fired_payment_stop_still_stops_and_says_so() -> None:
    # The playbook decides, even here: a stop after money moved reports
    # the fired amount as unverified rather than claiming nothing paid.
    text = STOPPING.replace(
        "    irreversible: payment\n", "    irreversible: payment\n    on_fail: stop\n"
    )
    p, h, send = _at_gate(playbook=text)
    pay = _reply_arrives(p, h, send, "好的")
    _feed(h, pay, _sheet("¥45"))
    fire = p.advance(h)
    assert fire.tool_calls[1].arguments["name"] == "demo/add-cart"
    _feed(h, fire, "BLOCKED — not executed", error=True)

    stop = p.advance(h)

    assert stop.tool_names() == ["note", "end_session"]
    assert "a payment of ¥45 fired, unverified" in stop.tool_calls[1].arguments["recap"]


def test_a_suspension_carries_the_threads_think_level() -> None:
    # The reachable path: wake 1 suspends at the ask, wake 2 resumes and
    # the declared yes/no words decide it (no thread call at all), the
    # walk completes, and the close's `summarize` is that wake's FIRST
    # model call. Without the level travelling it goes out at the
    # vendor's default — on a thinking model, minutes over one line.
    from physiclaw.conductor.walk.micro import SUMMARIZE

    write_pack(playbooks={"flow": FLOW})
    spec, pack = build.load_spec("demo", "flow", require_live=False)
    first = _program(keyword="milk")
    first.thread.thinking = "off"  # as the boot's `select` declared it

    carried = first.state()
    assert carried["think"] == "off"

    resumed = build.build_program(
        spec, pack, {"keyword": "milk"}, None, suspended=carried, dry=True
    )
    assert resumed.thread.thinking == "off"
    close = resumed.thread.request(SUMMARIZE, "close", (), {}, ledger=resumed.ledger)
    assert close.thinking == "off"

    # A record from before the field was kept, or one hand-edited to
    # nonsense, reads as unset rather than reaching the provider.
    for bad in (
        {k: v for k, v in carried.items() if k != "think"},
        {**carried, "think": "banana"},
    ):
        plain = build.build_program(
            spec, pack, {"keyword": "milk"}, None, suspended=bad, dry=True
        )
        assert plain.thread.thinking is None


def test_a_landed_move_leaves_its_trace_in_the_ledger() -> None:
    # A page is not a node — it is a move's `verify` — so the moment a
    # move lands is the moment its page is confirmed, and one clause
    # records both. It rides the session thread's since-block, which is
    # how the closing summary learns the route actually ran.
    p, h, _ = _at_gate()

    ran = [e for e in p.ledger.events if e.startswith("ran ")]

    assert ran == ["ran open-app, now on results"]
    # The whole account still reads in order, task first.
    assert p.ledger.events[0].startswith("asked ")
    assert p.ledger.events.index(ran[0]) < len(p.ledger.events) - 1


def test_the_asks_verdict_reaches_every_exit_as_an_answer() -> None:
    # A payment ask leaves a consent amount, so the brief can warn about
    # it. Any OTHER ask left only a journal note, and notes reach no
    # reader outside the session thread — so a walk that got a yes and
    # handed over later for some other reason never told the model the
    # user had already agreed. It rides its own slot, NOT `decided`,
    # which is the ref namespace an agent step's returns own.
    from physiclaw.conductor.walk import brief

    p, h, send = _at_gate()
    _feed(h, send, _thread(("buy it?", 0.75, 0.3)))
    _reply_arrives(p, h, send, "好的")

    assert p.ledger.answers == {"gate": "yes"}
    assert "gate.answer" not in p.ledger.decided  # not an agent output
    # It reaches the account, so the recap and the handover brief carry it.
    assert any("answered gate='yes'" in c for c in p.ledger.account())
    assert "answered gate='yes'" in brief.walk_brief(
        "something else broke",
        ledger=p.ledger,
        node="pay",
        idx=4,
        consented=None,
    )
    # The journal line still keeps the reply verbatim and who read it.
    assert any("user confirmed" in e and "'好的'" in e for e in p.ledger.events)
