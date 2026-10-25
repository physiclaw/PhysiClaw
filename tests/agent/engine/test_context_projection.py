"""Request reduction must preserve facts, tool pairing, and cached prefixes."""

import pytest

from physiclaw.agent.engine import assemble, compact
from physiclaw.agent.engine.session import Session
from physiclaw.contract.dto import (
    AssistantMessage,
    CollapsePolicy,
    FinishReason,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)


def turn(number, scratchpad):
    return [
        AssistantMessage(
            content="",
            finish_reason=FinishReason.TOOL_CALLS,
            tool_calls=[
                ToolCall(
                    str(number),
                    "note",
                    {
                        "summary": f"Observed step {number}; continuing",
                        "scratchpad": scratchpad,
                    },
                ),
                ToolCall(f"a{number}", "peek", {}),
            ],
        ),
        ToolResultMessage(str(number), "noted"),
        ToolResultMessage(f"a{number}", "screen evidence"),
    ]


def test_repeated_scratchpad_is_projected_without_losing_history_or_pairing():
    original = turn(1, "item A: 20") + turn(2, "item A: 20")
    original += turn(3, "item A: 20; item B: 35")
    session = Session(scratchpad="item A: 20; item B: 35")

    request = assemble.apply_request_tails(original, session, layout_incomplete=False)

    assert request[0] is original[0]  # First copy is still available.
    assert "scratchpad" not in request[3].tool_calls[0].arguments
    assert request[6] is original[6]  # New facts survive.
    assert original[3].tool_calls[0].arguments["scratchpad"] == "item A: 20"
    assert request[3].tool_calls[0].id == original[3].tool_calls[0].id
    assert (
        request[3].tool_calls[0].arguments["summary"]
        == original[3].tool_calls[0].arguments["summary"]
    )
    assert request[4:6] == original[4:6]
    assert UserMessage("<scratchpad>\nitem A: 20; item B: 35\n</scratchpad>") in request


def test_appending_updates_preserves_projected_prefix():
    history = turn(1, "A") + turn(2, "A")
    prefix = compact.omit_repeated_scratchpads(history)
    longer = compact.omit_repeated_scratchpads(history + turn(3, "B"))
    assert longer[: len(prefix)] == prefix


@pytest.mark.parametrize("clear", ["", " ", "\n\t"])
def test_clear_and_restore_are_distinct_writes(clear):
    history = turn(1, "A") + turn(2, clear) + turn(3, clear) + turn(4, "A")
    assert compact.omit_repeated_scratchpads(history) == history


def test_non_note_scratchpad_arguments_are_never_changed():
    message = AssistantMessage(
        "", [ToolCall("x", "other", {"scratchpad": "A"})], FinishReason.TOOL_CALLS
    )
    history = turn(1, "A") + [message]
    assert compact.omit_repeated_scratchpads(history)[-1] is message


@pytest.mark.parametrize("arguments", [None, 42, [], "bad"])
def test_malformed_historical_arguments_remain_for_diagnostics(arguments):
    message = AssistantMessage(
        "", [ToolCall("n", "note", arguments)], FinishReason.TOOL_CALLS
    )
    assert compact.omit_repeated_scratchpads([message]) == [message]


def test_collapsing_history_keeps_the_first_surviving_scratchpad_copy():
    history = (
        [
            SystemMessage("System"),
            UserMessage("Wake"),
            UserMessage(compact.SUMMARY_INITIAL),
            UserMessage(compact.MEMORY_INITIAL),
            UserMessage(compact.SKILLS_INITIAL),
        ]
        + turn(1, "Keep these exact facts")
        + turn(2, "Keep these exact facts")
        + turn(3, "Keep these exact facts")
    )
    session = Session(scratchpad="Keep these exact facts")
    before = assemble.apply_request_tails(history, session, layout_incomplete=False)
    assert "scratchpad" not in before[8].tool_calls[0].arguments
    assert compact.collapse_old_turns(
        history,
        policy=CollapsePolicy(first_at=3, keep=2, interval=2),
        collapsed_once=False,
    )
    after = assemble.apply_request_tails(history, session, layout_incomplete=False)
    assert after[5].tool_calls[0].arguments["scratchpad"] == "Keep these exact facts"
    assert "scratchpad" not in after[8].tool_calls[0].arguments
    assert history[8].tool_calls[0].arguments["scratchpad"] == "Keep these exact facts"
    assert UserMessage("<scratchpad>\nKeep these exact facts\n</scratchpad>") in after


def test_provider_native_replay_blocks_are_untouched():
    from physiclaw.provider.chat.anthropic_compat import _assistant_blocks

    message = turn(2, "A")[0]
    blocks = [
        {"type": "thinking", "thinking": "opaque", "signature": "signed"},
        {
            "type": "tool_use",
            "id": "2",
            "name": "note",
            "input": message.tool_calls[0].arguments,
        },
    ]
    message.vendor_extra = {"anthropic": {"content": blocks}}
    history = turn(1, "A") + [message] + turn(3, "A")
    projected = compact.omit_repeated_scratchpads(history)
    assert projected == history
    assert projected[3] is message
    assert _assistant_blocks(projected[3]) == blocks
