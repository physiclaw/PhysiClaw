"""Tests for `physiclaw.agent.engine.compact` — turn collapse + stub.

Covers:
  - placeholder constructors
  - collapse_old_turns: bootstrap state, threshold gating, summary
    accumulation across collapses, memory/skill artifact harvesting
  - drop_stale_screens: idempotency, latest preserved, text-row filter
  - scale_image_bytes: small image passthrough, oversized scaled, decode
    failure fallback
  - small helpers: _is_screen_obs_turn, _content_to_text, _has_image,
    _filter_text_rows, _format_artifact_text, _carry_items, _render_slot
"""
from __future__ import annotations

from typing import Any

import cv2
import numpy as np
import pytest

from physiclaw.agent.engine import compact
from physiclaw.agent.engine.compact import (
    MEMORY_HEADER,
    MEMORY_INITIAL,
    SKILLS_HEADER,
    SKILLS_INITIAL,
    SUMMARY_HEADER,
    SUMMARY_INITIAL,
    _carry_items,
    _content_to_text,
    _filter_text_rows,
    _format_artifact_text,
    _has_image,
    _is_screen_obs_turn,
    _render_slot,
    collapse_old_turns,
    drop_stale_screens,
    new_memory_placeholder,
    new_skills_placeholder,
    new_summary_placeholder,
    scale_image_bytes,
)
from physiclaw.agent.engine.dto import (
    AssistantMessage,
    FinishReason,
    ImageBlock,
    Message,
    SystemMessage,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)


# ---------- placeholder constructors ----------


def test_new_summary_placeholder_initial() -> None:
    p = new_summary_placeholder()
    assert isinstance(p, UserMessage)
    assert p.content == SUMMARY_INITIAL


def test_new_skills_placeholder_initial() -> None:
    assert new_skills_placeholder().content == SKILLS_INITIAL


def test_new_memory_placeholder_falls_back_when_no_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from physiclaw.agent.engine import memory

    monkeypatch.setattr(memory, "load_recent_entries", lambda n: "")

    assert new_memory_placeholder().content == MEMORY_INITIAL


def test_new_memory_placeholder_pre_populates_when_logs_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from physiclaw.agent.engine import memory

    monkeypatch.setattr(
        memory, "load_recent_entries", lambda n: "[2026-04-28 09:00] hi",
    )
    monkeypatch.setattr(memory, "BOOTSTRAP_LOG_ENTRIES", 3)

    out = new_memory_placeholder().content

    assert isinstance(out, str)
    assert MEMORY_HEADER in out
    assert "read_logs" in out
    assert "[2026-04-28 09:00] hi" in out


# ---------- _carry_items / _render_slot ----------


def test_carry_items_returns_empty_for_non_string_input() -> None:
    assert _carry_items([], SUMMARY_HEADER, sep="\n") == []


def test_carry_items_returns_empty_when_header_missing() -> None:
    assert _carry_items("no header here", SUMMARY_HEADER, sep="\n") == []


def test_carry_items_returns_empty_for_initial_state() -> None:
    assert _carry_items(SUMMARY_INITIAL, SUMMARY_HEADER, sep="\n") == []


def test_carry_items_extracts_items_separated_by_newline() -> None:
    body = f"{SUMMARY_HEADER}\n- one\n- two\n- three"

    assert _carry_items(body, SUMMARY_HEADER, sep="\n") == ["- one", "- two", "- three"]


def test_carry_items_handles_double_newline_separator() -> None:
    body = f"{MEMORY_HEADER}\nfirst entry\n\nsecond entry"

    assert _carry_items(body, MEMORY_HEADER, sep="\n\n") == ["first entry", "second entry"]


def test_render_slot_with_items() -> None:
    out = _render_slot(SUMMARY_HEADER, ["- a", "- b"], sep="\n")

    assert out == f"{SUMMARY_HEADER}\n- a\n- b"


def test_render_slot_empty_items_uses_none_yet() -> None:
    out = _render_slot(MEMORY_HEADER, [], sep="\n\n")

    assert out == f"{MEMORY_HEADER}\n(none yet)"


# ---------- _format_artifact_text ----------


def test_format_artifact_text_renders_args_as_sorted_json() -> None:
    out = _format_artifact_text("read_logs", {"entries": 5}, "log line")

    assert out == 'read_logs({"entries": 5}) →\nlog line'


def test_format_artifact_text_handles_unicode_args() -> None:
    out = _format_artifact_text("Skill", {"name": "微信"}, "body")

    assert "微信" in out


# ---------- _is_screen_obs_turn ----------


def _asst(*tool_names: str) -> AssistantMessage:
    return AssistantMessage(
        content="",
        tool_calls=[
            ToolCall(id=f"t{i}", name=name, arguments={})
            for i, name in enumerate(tool_names)
        ],
        finish_reason=FinishReason.TOOL_CALLS,
    )


def test_is_screen_obs_turn_true_for_note_plus_peek() -> None:
    assert _is_screen_obs_turn(_asst("note", "peek")) is True


def test_is_screen_obs_turn_true_for_note_plus_screenshot() -> None:
    assert _is_screen_obs_turn(_asst("note", "screenshot")) is True


def test_is_screen_obs_turn_false_when_no_tool_calls() -> None:
    assert _is_screen_obs_turn(_asst()) is False


def test_is_screen_obs_turn_false_when_other_tool_present() -> None:
    assert _is_screen_obs_turn(_asst("note", "tap")) is False


def test_is_screen_obs_turn_false_when_only_note() -> None:
    # Has note but no screen tool — still a turn boundary, not an obs.
    assert _is_screen_obs_turn(_asst("note")) is False


# ---------- _content_to_text / _has_image ----------


def test_content_to_text_string_passthrough() -> None:
    assert _content_to_text("hi") == "hi"


def test_content_to_text_first_text_block_in_multipart() -> None:
    out = _content_to_text(
        [ImageBlock(media_type="image/jpeg", data_b64="aGk="), TextBlock(text="cap")]
    )

    assert out == "cap"


def test_content_to_text_returns_empty_when_no_text_block_in_list() -> None:
    out = _content_to_text(
        [ImageBlock(media_type="image/jpeg", data_b64="aGk=")]
    )

    assert out == ""


def test_has_image_false_for_string() -> None:
    assert _has_image("text") is False


def test_has_image_true_when_image_block_present() -> None:
    assert _has_image(
        [TextBlock(text="x"), ImageBlock(media_type="image/jpeg", data_b64="a")]
    ) is True


def test_has_image_false_when_only_text_blocks() -> None:
    assert _has_image([TextBlock(text="x")]) is False


# ---------- _filter_text_rows ----------


def test_filter_text_rows_keeps_text_rows_drops_icon_rows() -> None:
    listing = (
        'id [kind] "label" [left,top,right,bottom] conf\n'
        '1 [icon] "" [0.1,0.1,0.2,0.2] 0.95\n'
        '2 [text] "Send" [0.5,0.8,0.6,0.9] 0.99\n'
        '3 [icon] "" [0.7,0.1,0.8,0.2] 0.90\n'
    )

    out = _filter_text_rows(listing)

    lines = out.splitlines()
    assert len(lines) == 2  # header + 1 text row
    assert "[text]" in lines[1]
    assert "[icon]" not in out


def test_filter_text_rows_returns_empty_when_no_text_rows() -> None:
    listing = (
        'id [kind] "label" [left,top,right,bottom] conf\n'
        '1 [icon] "" [0.1,0.1,0.2,0.2] 0.95\n'
    )

    assert _filter_text_rows(listing) == ""


def test_filter_text_rows_returns_empty_for_empty_listing() -> None:
    assert _filter_text_rows("") == ""


def test_filter_text_rows_skips_malformed_rows() -> None:
    listing = (
        'id [kind] "label"\n'
        'not a valid row\n'
        '1 [text] "ok" [0.1,0.1,0.2,0.2] 0.9\n'
    )

    out = _filter_text_rows(listing)

    assert "not a valid row" not in out
    assert "[text]" in out


# ---------- scale_image_bytes ----------


def _encode_jpg(arr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", arr)
    assert ok
    return buf.tobytes()


def test_scale_image_bytes_passthrough_when_within_max_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compact, "MAX_IMAGE_EDGE", 1000)
    img = np.full((300, 200, 3), 128, dtype=np.uint8)
    raw = _encode_jpg(img)

    out_bytes, mime = scale_image_bytes(raw)

    assert mime == "image/jpeg"
    decoded = cv2.imdecode(np.frombuffer(out_bytes, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == (300, 200, 3)


def test_scale_image_bytes_scales_when_over_max_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compact, "MAX_IMAGE_EDGE", 100)
    img = np.full((300, 600, 3), 128, dtype=np.uint8)
    raw = _encode_jpg(img)

    out_bytes, mime = scale_image_bytes(raw)

    assert mime == "image/jpeg"
    decoded = cv2.imdecode(np.frombuffer(out_bytes, np.uint8), cv2.IMREAD_COLOR)
    # Long edge 600 → 100; aspect 2 preserved.
    assert max(decoded.shape[:2]) == 100


def test_scale_image_bytes_returns_input_on_decode_failure() -> None:
    raw = b"definitely not an image"

    out_bytes, mime = scale_image_bytes(raw)

    assert out_bytes == raw
    assert mime == "application/octet-stream"


# ---------- drop_stale_screens ----------


def _peek_pair(listing: str = 'id [kind] "label" [left,top,right,bottom] conf') -> list[Message]:
    """Build [asst-with-peek-call, tool_result-with-image]."""
    return [
        AssistantMessage(
            content="",
            tool_calls=[
                ToolCall(id="t1", name="note", arguments={"summary": "x"}),
                ToolCall(id="t2", name="peek", arguments={}),
            ],
            finish_reason=FinishReason.TOOL_CALLS,
        ),
        ToolResultMessage(
            tool_call_id="t1", content="noted: x"
        ),
        ToolResultMessage(
            tool_call_id="t2",
            content=[
                TextBlock(text=listing),
                ImageBlock(media_type="image/jpeg", data_b64="aGk="),
            ],
        ),
    ]


def test_drop_stale_screens_no_op_when_one_or_zero_obs_turns() -> None:
    msgs = [SystemMessage(content="x"), UserMessage(content="hi")]
    snapshot = list(msgs)

    drop_stale_screens(msgs)

    assert msgs == snapshot


def test_drop_stale_screens_stubs_earlier_obs_keeps_latest() -> None:
    listing = (
        'id [kind] "label" [left,top,right,bottom] conf\n'
        '1 [text] "Send" [0.5,0.8,0.6,0.9] 0.99\n'
    )
    msgs: list[Message] = [
        SystemMessage(content="sys"),
        *_peek_pair(listing),
        *_peek_pair(),
    ]

    drop_stale_screens(msgs)

    # Earlier peek's tool_result (index 3) is now superseded.
    earlier = msgs[3]
    assert isinstance(earlier, ToolResultMessage)
    assert earlier.is_superseded is True
    assert isinstance(earlier.content, str)
    assert "(superseded peek)" in earlier.content
    # Later peek's tool_result still has the image.
    latest = msgs[6]
    assert isinstance(latest, ToolResultMessage)
    assert latest.is_superseded is False
    assert _has_image(latest.content)


def test_drop_stale_screens_idempotent_on_second_pass() -> None:
    msgs: list[Message] = [SystemMessage(content=""), *_peek_pair(), *_peek_pair()]

    drop_stale_screens(msgs)
    snapshot = [(m.__class__, getattr(m, "content", None)) for m in msgs]
    drop_stale_screens(msgs)

    assert [(m.__class__, getattr(m, "content", None)) for m in msgs] == snapshot


# ---------- collapse_old_turns ----------


def test_collapse_old_turns_warns_when_slots_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    msgs: list[Message] = [SystemMessage(content="s"), UserMessage(content="u")]

    with caplog.at_level(logging.WARNING, logger="physiclaw.agent.engine.compact"):
        collapse_old_turns(msgs, first_at=10, interval=10, keep=5)

    assert any(
        "missing summary/memory/skill slots" in r.getMessage()
        for r in caplog.records
    )


def _scaffold_with_slots() -> list[Message]:
    return [
        SystemMessage(content="sys"),
        UserMessage(content="trigger"),
        new_summary_placeholder(),
        UserMessage(content=MEMORY_INITIAL),
        new_skills_placeholder(),
    ]


def test_collapse_no_op_when_below_first_at_threshold() -> None:
    msgs = _scaffold_with_slots()
    snapshot = list(msgs)

    collapse_old_turns(msgs, first_at=10, interval=5, keep=3)

    assert msgs == snapshot


def _note_turn(summary: str) -> list[Message]:
    """Synthetic [asst with `note(summary=...)`, tool_result] pair."""
    return [
        AssistantMessage(
            content="",
            tool_calls=[
                ToolCall(id=f"t-{summary}", name="note", arguments={"summary": summary}),
            ],
            finish_reason=FinishReason.TOOL_CALLS,
        ),
        ToolResultMessage(tool_call_id=f"t-{summary}", content=f"noted: {summary}"),
    ]


def test_collapse_first_collapse_harvests_note_summaries_into_slot() -> None:
    msgs: list[Message] = _scaffold_with_slots()
    # Add 4 turns — first_at=3, keep=1 → 3 turns harvested, 1 kept.
    for s in ("step-a", "step-b", "step-c", "step-d"):
        msgs.extend(_note_turn(s))

    collapse_old_turns(msgs, first_at=3, interval=10, keep=1)

    # Slot at index 2 now contains the summaries.
    summary = msgs[2]
    assert isinstance(summary, UserMessage)
    assert isinstance(summary.content, str)
    assert SUMMARY_HEADER in summary.content
    assert "- step-a" in summary.content
    assert "- step-b" in summary.content
    assert "- step-c" in summary.content
    # Most recent step kept intact, NOT in the summary.
    assert "- step-d" not in summary.content


def test_collapse_no_op_when_no_salvageable_content() -> None:
    """Many turns but none have note/memory/skill calls → no-op."""
    msgs = _scaffold_with_slots()
    for i in range(5):
        msgs.append(AssistantMessage(
            content="",
            tool_calls=[ToolCall(id=f"t{i}", name="tap", arguments={})],
            finish_reason=FinishReason.TOOL_CALLS,
        ))
        msgs.append(ToolResultMessage(tool_call_id=f"t{i}", content="ok"))

    snapshot = [(m.__class__, getattr(m, "content", None)) for m in msgs]

    collapse_old_turns(msgs, first_at=3, interval=10, keep=1)

    assert [(m.__class__, getattr(m, "content", None)) for m in msgs] == snapshot


def test_collapse_harvests_memory_tool_results() -> None:
    msgs = _scaffold_with_slots()
    for i in range(3):
        msgs.append(AssistantMessage(
            content="",
            tool_calls=[ToolCall(id=f"r{i}", name="read_memory", arguments={"key": f"k{i}"})],
            finish_reason=FinishReason.TOOL_CALLS,
        ))
        msgs.append(ToolResultMessage(tool_call_id=f"r{i}", content=f"value-{i}"))
    msgs.extend(_note_turn("step-keep"))  # latest kept turn

    collapse_old_turns(msgs, first_at=3, interval=10, keep=1)

    memory_slot = msgs[3]
    assert isinstance(memory_slot, UserMessage)
    text = memory_slot.content
    assert isinstance(text, str)
    assert MEMORY_HEADER in text
    for i in range(3):
        assert f"value-{i}" in text


def test_collapse_harvests_skill_tool_results() -> None:
    msgs = _scaffold_with_slots()
    msgs.append(AssistantMessage(
        content="",
        tool_calls=[ToolCall(id="s1", name="Skill", arguments={"name": "wechat"})],
        finish_reason=FinishReason.TOOL_CALLS,
    ))
    msgs.append(ToolResultMessage(tool_call_id="s1", content="WeChat workflow body"))
    for s in ("a", "b", "c"):
        msgs.extend(_note_turn(s))

    collapse_old_turns(msgs, first_at=3, interval=10, keep=1)

    skill_slot = msgs[4]
    assert isinstance(skill_slot, UserMessage)
    text = skill_slot.content
    assert isinstance(text, str)
    assert SKILLS_HEADER in text
    assert "WeChat workflow body" in text


def test_collapse_subsequent_collapse_uses_keep_plus_interval_threshold() -> None:
    msgs = _scaffold_with_slots()
    # Pre-set the summary slot to indicate first collapse already happened.
    msgs[2] = UserMessage(content=f"{SUMMARY_HEADER}\n- pre-existing")
    for s in [f"s{i}" for i in range(8)]:
        msgs.extend(_note_turn(s))

    # keep+interval = 1+5 = 6 turns needed before subsequent fires.
    collapse_old_turns(msgs, first_at=100, interval=5, keep=1)

    summary = msgs[2]
    assert isinstance(summary, UserMessage)
    text = summary.content
    assert isinstance(text, str)
    assert "- pre-existing" in text  # carried forward
    # Older steps harvested; latest kept.
    assert "- s0" in text


def test_collapse_skips_when_artifact_result_is_error() -> None:
    msgs = _scaffold_with_slots()
    msgs.append(AssistantMessage(
        content="",
        tool_calls=[ToolCall(id="s1", name="Skill", arguments={"name": "x"})],
        finish_reason=FinishReason.TOOL_CALLS,
    ))
    msgs.append(ToolResultMessage(
        tool_call_id="s1", content="oops", is_error=True,
    ))
    for s in ("a", "b", "c"):
        msgs.extend(_note_turn(s))

    collapse_old_turns(msgs, first_at=3, interval=10, keep=1)

    skill_slot = msgs[4]
    assert isinstance(skill_slot, UserMessage)
    # Error skipped — no skill artifact in slot.
    assert "oops" not in str(skill_slot.content)
