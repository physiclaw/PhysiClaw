"""Tests for `physiclaw.debug.interceptor` — the fake-channel result
transformer: which real results are rewritten to the virtual thread,
which pass through, and how the on-thread state moves. Provenance is
the session's synthesized-turn bit, passed by dispatch.

Accepted mutation survivors affect log/action wording, equivalent falsy
state values, or the non-channel fallback for a missing macro name. Outcome
metadata, empty message text, provenance, and state transitions are asserted.
"""

from __future__ import annotations

import pytest
from debug_fakes import write_channel_pages

from physiclaw.common.verdict import SCREEN_CHANGED
from physiclaw.contract.dto import ToolCall
from physiclaw.debug import thread as vthread
from physiclaw.debug.interceptor import build
from physiclaw.macros.parse import parse_macro
from physiclaw.macros.runner import run_and_record

# What the REAL dispatch produced — the transformer receives it and
# (for channel actions) discards it for the virtual observation.
REAL_BLOCKS = [
    {
        "type": "text",
        "text": "real action | screen: changed",
        "_meta": {"physiclaw/macro_ok": True},
    },
    {"type": "image", "data": "", "mimeType": "image/jpeg"},
    {"type": "text", "text": 'id [kind] "real row" [0.1,0.1,0.2,0.2] 0.9'},
]


def _call(name: str, args: dict | None = None) -> ToolCall:
    return ToolCall(id="conductor-walk-1-act", name=name, arguments=args or {})


def _open() -> ToolCall:
    return _call("run_macro", {"name": "channel/open"})


def _send(message: str | None = "Total is $4.50 — reply ok to confirm") -> ToolCall:
    return _call("run_macro", {"name": "channel/send", "inputs": {"message": message}})


def _listing_of(blocks):
    from physiclaw.common.listing import Screen

    return Screen.read(blocks[-1]["text"])


def test_channel_send_observation_is_rewritten_as_a_gesture_reply() -> None:
    write_channel_pages()
    fc = build()

    blocks = fc.intercept(
        _send("Total is $4.50 — reply ok to confirm"), True, REAL_BLOCKS
    )

    assert blocks is not None and blocks is not REAL_BLOCKS
    assert SCREEN_CHANGED in blocks[0]["text"]  # action text carries the verdict
    assert blocks[1]["type"] == "image"
    labels = [r.label for r in _listing_of(blocks).rows]
    assert "Total is $4.50 — reply ok to confirm" in labels
    assert "real row" not in labels  # the real observation is fully replaced


def test_channel_send_persists_the_agent_bubble() -> None:
    write_channel_pages()
    build().intercept(_send("ask text"), True, REAL_BLOCKS)

    assert vthread.load().bubbles == [
        vthread.Bubble(sender=vthread.AGENT, text="ask text")
    ]


def test_peek_after_channel_open_is_rewritten_as_a_view_reply() -> None:
    write_channel_pages()
    vthread.seed("buy milk", [])
    fc = build()
    fc.intercept(_open(), True, REAL_BLOCKS)

    blocks = fc.intercept(_call("peek"), True, REAL_BLOCKS)

    assert blocks is not None
    assert blocks[0]["type"] == "image"  # view shape: no action text block
    assert "buy milk" in [r.label for r in _listing_of(blocks).rows]


def test_gate_peek_carries_the_released_staged_reply() -> None:
    # The whole point of the seam: ask → peek → the staged confirm is
    # there, below the ask, fresh against the baseline.
    write_channel_pages()
    vthread.seed("buy milk", ["ok"])
    fc = build()
    fc.intercept(_send("reply ok to confirm"), True, REAL_BLOCKS)

    blocks = fc.intercept(_call("peek"), True, REAL_BLOCKS)

    assert "ok" in [r.label for r in _listing_of(blocks).rows]


def test_peek_before_any_channel_action_keeps_the_real_result() -> None:
    assert build().intercept(_call("peek"), True, REAL_BLOCKS) is None


def test_peek_that_shows_the_real_thread_is_virtualized() -> None:
    # The previous session's wrap-up leaves the IM app on the thread, so
    # the boot's opening peek reads as `channel.thread` for real — and
    # would parse the assistant's own completion report instead of the
    # scripted task. That reading is virtualized like a post-open peek.
    write_channel_pages()
    vthread.seed("buy soy sauce", [])
    on_thread = [
        {"type": "image", "data": "", "mimeType": "image/jpeg"},
        {
            "type": "text",
            "text": 'id [kind] "label" [left,top,right,bottom] conf\n'
            '0 [text] "MyChat" [0.4,0.05,0.6,0.09] 0.99\n'
            '1 [text] "rice purchase done" [0.5,0.3,0.9,0.34] 0.99',
        },
    ]
    fc = build()

    blocks = fc.intercept(_call("peek"), True, on_thread)

    assert blocks is not None
    labels = [r.label for r in _listing_of(blocks).rows]
    assert "buy soy sauce" in labels and "rice purchase done" not in labels
    # And the state is now on-thread: the next peek stays virtual.
    assert fc.intercept(_call("peek"), True, REAL_BLOCKS) is not None


def test_an_app_macro_passes_through_and_leaves_the_thread() -> None:
    write_channel_pages()
    fc = build()
    fc.intercept(_open(), True, REAL_BLOCKS)

    leg = fc.intercept(
        _call("run_macro", {"name": "taobao/open-app"}), True, REAL_BLOCKS
    )

    assert leg is None
    assert fc.intercept(_call("peek"), True, REAL_BLOCKS) is None  # off-thread again


def test_wait_keeps_the_thread_state() -> None:
    # The gate's ask → wait → peek cycle: the wait between polls must
    # not drop the conductor off the virtual thread.
    write_channel_pages()
    fc = build()
    fc.intercept(_send(), True, REAL_BLOCKS)

    assert fc.intercept(_call("wait", {"seconds": 45}), True, REAL_BLOCKS) is None
    assert fc.intercept(_call("peek"), True, REAL_BLOCKS) is not None


def test_model_turns_are_never_rewritten() -> None:
    write_channel_pages()

    blocks = build().intercept(_send(), False, REAL_BLOCKS)

    assert blocks is None


def test_a_harness_crash_fails_open_to_the_real_result(monkeypatch) -> None:
    write_channel_pages()
    fc = build()
    fc.intercept(_open(), True, REAL_BLOCKS)
    monkeypatch.setattr(
        vthread, "record_send", lambda message: (_ for _ in ()).throw(RuntimeError())
    )

    assert fc.intercept(_send(), True, REAL_BLOCKS) is None
    assert fc.intercept(_call("peek"), True, REAL_BLOCKS) is None


class MacroPhone:
    """A failed gesture followed by a real recovery view, or a successful one."""

    def __init__(self, failed):
        self.failed = failed
        self.calls = []

    async def call_tool(self, name, args=None):
        self.calls.append(name)
        if self.failed and name != "peek":
            raise RuntimeError("fixture gesture failed")
        return REAL_BLOCKS


def channel_send_spec():
    return parse_macro(
        "kind: macro\nschema: 1\nname: send\ndescription: fixture\n"
        "inputs:\n  message:\n    description: text\nsteps:\n  - home_screen\n",
        "send",
    )


async def test_regression_aborted_send_keeps_real_result_and_staged_reply():
    write_channel_pages()
    vthread.seed("buy milk", ["ok"])
    fc = build()
    fc.intercept(_open(), True, REAL_BLOCKS)
    result = await run_and_record(
        channel_send_spec(), {"message": "Confirm now"}, MacroPhone(failed=True)
    )

    replacement = fc.intercept(_send("Confirm now"), True, result.blocks)

    assert result.ok is False
    assert replacement is None
    assert vthread.load() == vthread.Thread(
        bubbles=[vthread.Bubble(vthread.USER, "buy milk")], staged=["ok"]
    )
    assert fc.intercept(_call("peek"), True, REAL_BLOCKS) is None


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        True,
        [True],
        {"physiclaw/macro_ok": "false"},
        {"physiclaw/macro_ok": 1},
    ],
)
def test_regression_unconfirmed_send_never_adds_a_virtual_bubble(metadata):
    write_channel_pages()
    vthread.seed("buy milk", [])
    fc = build()
    fc.intercept(_open(), True, REAL_BLOCKS)
    blocks = [{**REAL_BLOCKS[0], "_meta": metadata}, *REAL_BLOCKS[1:]]

    replacement = fc.intercept(_send(), True, blocks)

    assert replacement is None
    assert vthread.load().bubbles == [vthread.Bubble(vthread.USER, "buy milk")]
    assert fc.intercept(_call("peek"), True, REAL_BLOCKS) is None


def test_unconfirmed_open_with_empty_result_cannot_enter_virtual_thread():
    fc = build()

    replacement = fc.intercept(_open(), True, [])

    assert replacement is None
    assert fc.intercept(_call("peek"), True, REAL_BLOCKS) is None


async def test_completed_send_uses_outcome_even_when_text_mentions_abort():
    write_channel_pages()
    result = await run_and_record(
        channel_send_spec(), {"message": "ABORTED at step 1"}, MacroPhone(failed=False)
    )

    replacement = build().intercept(_send("ABORTED at step 1"), True, result.blocks)

    assert result.ok is True
    assert replacement is not None
    assert vthread.load().bubbles == [
        vthread.Bubble(vthread.AGENT, "ABORTED at step 1")
    ]


async def test_regression_rehearsal_preserves_aborted_channel_send():
    from physiclaw.conductor.drive import rehearsal

    write_channel_pages()
    vthread.seed("buy milk", ["ok"])
    fc = build()
    phone = MacroPhone(failed=True)

    text, is_error = await rehearsal.dispatch(
        phone,
        _send("Confirm now"),
        {"channel/send": channel_send_spec()},
        transform=lambda call, blocks: fc.intercept(call, True, blocks),
    )

    assert is_error is False  # A substantive abort retains its recovery view.
    assert "ABORTED" in text and "fixture gesture failed" in text
    assert "debug:" not in text
    assert vthread.load() == vthread.Thread(
        bubbles=[vthread.Bubble(vthread.USER, "buy milk")], staged=["ok"]
    )
    assert phone.calls == ["home_screen", "peek"]


@pytest.mark.parametrize("message", [None, ""])
def test_empty_send_does_not_invent_message_text(message):
    write_channel_pages()

    blocks = build().intercept(_send(message), True, REAL_BLOCKS)

    assert blocks is not None
    assert vthread.load().bubbles == [vthread.Bubble(vthread.AGENT, "")]


def test_session_caches_fingerprint_but_reads_current_thread():
    write_channel_pages()
    fc = build()
    fc.intercept(_open(), True, REAL_BLOCKS)
    write_channel_pages(("NewChat",))
    vthread.seed("new task", [])

    blocks = fc.intercept(_call("peek"), True, REAL_BLOCKS)
    fresh = build().intercept(_open(), True, REAL_BLOCKS)

    assert [row.label for row in _listing_of(blocks).rows] == ["MyChat", "new task"]
    assert [row.label for row in _listing_of(fresh).rows] == ["NewChat", "new task"]
