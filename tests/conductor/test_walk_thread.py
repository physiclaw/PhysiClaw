"""Tests for `physiclaw.conductor.walk.thread` — the session's one
conversation with the model: append-only turns, one system prompt for
every call, the ledger's delta between calls, the legend at the tail."""

from __future__ import annotations

from conductor_fakes import FRAME

from physiclaw.conductor.walk.ledger import Ledger
from physiclaw.conductor.walk.micro import (
    _SPECS,
    ASK,
    MENU,
    PARSE_TASK,
    READ_REPLY,
    SUMMARIZE,
    MicroOutcome,
    _messages,
    _system,
    user_content,
)
from physiclaw.conductor.walk.thread import Thread
from physiclaw.contract.dto import ImageBlock, TextBlock


def _ledger() -> Ledger:
    return Ledger(ref="taobao/buy", nodes=7, task={"user_said": "buy milk"})


def test_every_thread_call_shares_one_system_prompt() -> None:
    ledger = _ledger()
    thread = Thread()
    parse = thread.request(
        PARSE_TASK, "parse", ("taobao/buy",), {MENU: "m"}, ledger=ledger
    )
    read = thread.request(READ_REPLY, "confirm", (), {ASK: "pay?"}, ledger=ledger)
    close = thread.request(SUMMARIZE, "close", (), {}, ledger=ledger)

    prompts = {_system(req) for req in (parse, read, close)}

    assert len(prompts) == 1  # the whole thread is one cached prefix
    (system,) = prompts
    assert "OUTSTANDING" in system and '"answer"' in system
    # The call's legend is the user block's tail, never the system prompt.
    assert "playbook EXACTLY as listed" not in system
    assert str(user_content(parse)).endswith(
        "those belong only in an input that asks for them."
    )


def test_a_later_call_extends_the_earlier_one_byte_for_byte() -> None:
    ledger = _ledger()
    thread = Thread()
    parse = thread.request(
        PARSE_TASK,
        "parse",
        ("taobao/buy",),
        {MENU: "m"},
        ledger=ledger,
        listing="buy milk",
        frame=FRAME,
    )
    first = _messages(parse)
    thread.settle(
        parse,
        MicroOutcome(
            out="taobao/buy",
            reason="r",
            confidence=1.0,
            payload={"user_said": "buy milk"},
        ),
        ledger,
    )

    read = thread.request(READ_REPLY, "confirm", (), {ASK: "pay ¥45?"}, ledger=ledger)
    second = _messages(read)

    # The prefix is the previous request whole, then its canonical reply.
    assert second[: len(first)] == first
    assert second[len(first)].content == (
        '{"reason": "r", "answer": "taobao/buy", "confidence": 1.0, '
        '"inputs": {"user_said": "buy milk"}}'
    )
    assert isinstance(first[-1].content, list) and first[-1].content[1] is FRAME


def test_the_ledgers_delta_rides_the_next_call_once() -> None:
    boot = Ledger(ref="channel/boot", nodes=3, task={})
    thread = Thread()
    parse = thread.request(
        PARSE_TASK, "parse", ("taobao/buy",), {MENU: "m"}, ledger=boot
    )
    assert "since the last call" not in str(user_content(parse))  # nothing yet
    thread.settle(
        parse, MicroOutcome(out="taobao/buy", reason="r", confidence=1.0), boot
    )

    # The activated walk keeps its own ledger (the thread outlives the
    # boot's); the playbook acts on its own: decisions, a message, a
    # payment.
    ledger = _ledger()
    ledger.decide("pick.summary", "Milk 5kg ¥45")
    ledger.say("buy Milk 5kg for ¥45?")
    ledger.note("user confirmed pay ('好的')")
    ledger.pay(45.0)
    read = thread.request(SUMMARIZE, "close", (), {}, ledger=ledger)
    block = str(user_content(read))

    assert "What the playbook did since the last call" in block
    assert "asked user_said='buy milk'" in block
    assert "decided pick.summary='Milk 5kg ¥45'" in block
    assert "sent to the user: 'buy Milk 5kg for ¥45?'" in block
    assert "user confirmed pay ('好的')" in block and "paid ¥45" in block
    thread.settle(read, MicroOutcome(out="done", reason="r", confidence=0.9), ledger)
    again = thread.request(SUMMARIZE, "close", (), {}, ledger=ledger)
    assert "since the last call" not in str(user_content(again))  # reported already


def test_read_reply_carries_the_ask_the_replies_and_the_frame() -> None:
    ledger = _ledger()
    req = Thread().request(
        READ_REPLY,
        "confirm",
        (),
        {ASK: "buy it for ¥45?"},
        ledger=ledger,
        listing="嗯 那就来一份吧",
        frame=FRAME,
    )

    blocks = user_content(req)

    assert isinstance(blocks, list)
    assert [type(b) for b in blocks] == [TextBlock, ImageBlock, TextBlock]
    assert "The ask, as sent to the user" in blocks[0].text and "¥45" in blocks[0].text
    assert "嗯 那就来一份吧" in blocks[2].text
    assert (
        blocks[2]
        .text.rstrip()
        .endswith(
            'When unsure, "other": money moves on confirm, and a wrong confirm cannot be undone.'
        )
    )
    assert _SPECS[READ_REPLY].answer_space(req) == ("confirm", "deny", "other")


def test_later_thread_calls_inherit_the_first_calls_think_level() -> None:
    # The boot's `select` declares how much its parse may think; the
    # thread's later calls (a reply reading, the close) think the same
    # unless told otherwise — never the vendor's default, which on a
    # thinking model is minutes of rumination over a record.
    ledger = _ledger()
    thread = Thread()
    parse = thread.request(
        PARSE_TASK, "parse", ("taobao/buy",), {MENU: "m"}, ledger=ledger, thinking="off"
    )
    close = thread.request(SUMMARIZE, "close", (), {}, ledger=ledger)
    told = thread.request(SUMMARIZE, "close", (), {}, ledger=ledger, thinking="low")

    assert (
        parse.thinking == "off" and close.thinking == "off" and told.thinking == "low"
    )


def test_a_fact_landing_during_a_call_is_carried_by_the_next_one() -> None:
    # The settle marks the ledger reported up to the request, not up to
    # the answer: a note written while the model was answering was not
    # in the block it saw, so the next call carries it.
    ledger = _ledger()
    thread = Thread()
    first = thread.request(SUMMARIZE, "close", (), {}, ledger=ledger)
    ledger.note("landed mid-call")
    thread.settle(first, MicroOutcome(out="done", reason="r", confidence=0.9), ledger)

    second = thread.request(SUMMARIZE, "close", (), {}, ledger=ledger)

    assert "landed mid-call" in str(user_content(second))


def test_a_suspension_round_trips_the_events_and_the_payment() -> None:
    ledger = _ledger()
    ledger.decide("pick.total", "45")
    ledger.say("buy?")
    ledger.pay(45.0)
    ledger.reported = 2  # a thread call carried the first two

    fresh = _ledger()
    fresh.restore(ledger.to_suspended())

    assert fresh.paid == 45.0 and fresh.events == ledger.events
    # A new wake opens a fresh thread with no history: everything is
    # unreported again.
    assert fresh.reported == 0


def test_a_record_without_events_rebuilds_them_from_the_fields() -> None:
    # A record from before the event log was kept carries `outputs`
    # only, so the account is rebuilt through the writers that own the
    # wording — grouped, not in landing order.
    ledger = _ledger()
    ledger.restore({"outputs": {"pick.total": "45"}})

    assert ledger.events == [
        "asked user_said='buy milk'",
        "decided pick.total='45'",
    ]
    assert ledger.paid is None
