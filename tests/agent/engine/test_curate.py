"""Tests for `physiclaw.agent.engine.curate` — the post-session LLM pass that
consolidates the learned-pitfalls list.

The autouse `physiclaw_home` fixture isolates `paths.pitfalls_dir()`.
"""

from __future__ import annotations

import json

import pytest

from physiclaw.agent.engine import curate, pitfalls
from physiclaw.agent.trace import RawLog, Trace, viewer
from physiclaw.agent.trace.store import Images
from physiclaw.common import paths
from physiclaw.contract.dto import AssistantMessage, FinishReason


class _FakeProvider:
    """Returns `replies` in order from chat(); records the histories it saw."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[list] = []

    async def chat(self, history, tools, **kw):
        self.calls.append(history)
        text = self._replies.pop(0)
        return AssistantMessage(
            content=text,
            tool_calls=[],
            finish_reason=FinishReason.STOP,
            raw={"choices": [{"message": {"content": text}}]},
        )

    def serialize_history(self, history):
        return [
            {"role": "system" if i == 0 else "user", "content": message.content}
            for i, message in enumerate(history)
        ]


class _BoomProvider:
    async def chat(self, history, tools, **kw):
        raise RuntimeError("provider down")


# ---------- _parse ----------


def test_parse_plain_fenced_and_prose_wrapped_array() -> None:
    assert curate._parse('["a", "b"]') == ["a", "b"]
    assert curate._parse('```json\n["a"]\n```') == ["a"]
    assert curate._parse('Here you go:\n["a", "b"]\nDone.') == ["a", "b"]


def test_parse_drops_non_strings_and_blanks() -> None:
    assert curate._parse('["a", 1, "", "  ", "b"]') == ["a", "b"]


def test_parse_garbage_returns_none() -> None:
    assert curate._parse("not json") is None  # no brackets
    assert curate._parse('{"a": 1}') is None  # not an array
    assert curate._parse("") is None


# ---------- curate ----------


@pytest.mark.asyncio
async def test_curate_consolidates_list_through_replace() -> None:
    pitfalls.add(["京东: Ai搜索 opens AI chat"])
    pitfalls.add(["京东: the Ai搜索 toggle opens AI chat"])  # near-dup
    provider = _FakeProvider(['["京东: Ai搜索 opens AI chat → use right-side 搜索"]'])

    assert await curate.curate(provider) is True
    assert pitfalls.read() == ["京东: Ai搜索 opens AI chat → use right-side 搜索"]
    # A curate snapshot lands in history.
    hist = (pitfalls.paths.pitfalls_dir() / "history.jsonl").read_text(encoding="utf-8")
    assert '"op": "curate"' in hist


@pytest.mark.asyncio
async def test_curate_empty_list_is_noop() -> None:
    provider = _FakeProvider([])  # never called
    assert await curate.curate(provider) is False
    assert provider.calls == []


@pytest.mark.asyncio
async def test_curate_fail_open_on_provider_error() -> None:
    pitfalls.add(["京东: a trap"])
    assert await curate.curate(_BoomProvider()) is False
    assert pitfalls.read() == ["京东: a trap"]  # untouched


@pytest.mark.asyncio
async def test_curate_skips_unparseable_without_clobbering() -> None:
    pitfalls.add(["keep me"])
    assert await curate.curate(_FakeProvider(["garbage not json"])) is False
    assert pitfalls.read() == ["keep me"]


async def test_regression_curate_unreadable_file_is_nonfatal() -> None:
    path = pitfalls.paths.pitfalls_dir() / "pitfalls.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff")
    provider = _FakeProvider([])

    result = await curate.curate(provider)

    assert result is False
    assert provider.calls == []
    assert path.read_bytes() == b"\xff"


@pytest.mark.parametrize(
    "reply, success", [('["kept lesson"]', True), ("invalid reply " * 100, False)]
)
async def test_curate_saves_complete_request_and_reply_for_session_review(
    reply, success
):
    pitfalls.add(["original lesson"])
    provider = _FakeProvider([reply])
    images = Images("curate-debug")
    trace, raw = Trace("curate-debug", images), RawLog("curate-debug", images)
    trace.write({"event": "request", "turn": 7, "message_count": 2})
    try:
        assert await curate.curate(provider, tr=trace, rlog=raw) is success
    finally:
        raw.close()
        trace.close()

    directory = paths.sessions_dir() / "curate-debug"
    request, response = [
        json.loads(line)
        for line in (directory / "model.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert request["call"] == response["call"] == "curate"
    assert request["turn"] == response["turn"] == 7
    assert request["messages"] == provider.serialize_history(provider.calls[0])
    assert response["raw"]["choices"][0]["message"]["content"] == reply
    assert response["elapsed_ms"] >= 0
    records = next(g for g in viewer.load(directory)["groups"] if g["turn"] == 7)[
        "records"
    ]
    assert any(r["rec"] == response for r in records)


async def test_failed_curate_keeps_request_and_error_in_session(caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    pitfalls.add(["original lesson"])

    class BrokenProvider(_FakeProvider):
        async def chat(self, *args, **kwargs):
            raise RuntimeError("curation failure detail")

    images = Images("failed-curate")
    trace, raw = Trace("failed-curate", images), RawLog("failed-curate", images)
    try:
        assert await curate.curate(BrokenProvider([]), tr=trace, rlog=raw) is False
    finally:
        raw.close()
        trace.close()

    directory = paths.sessions_dir() / "failed-curate"
    request = json.loads((directory / "model.jsonl").read_text(encoding="utf-8"))
    assert request["kind"] == "request" and request["call"] == "curate"
    assert request["messages"][1]["content"] == "- original lesson"
    assert "RuntimeError: curation failure detail" in (
        directory / "runtime.log"
    ).read_text(encoding="utf-8")
    assert pitfalls.read() == ["original lesson"]


async def test_curate_diagnostic_failure_cannot_prevent_consolidation(mocker):
    pitfalls.add(["original lesson"])
    raw = mocker.Mock()
    raw.write_request.side_effect = OSError("disk full")
    raw.write_response.side_effect = OSError("disk full")

    assert await curate.curate(_FakeProvider(['["kept lesson"]']), rlog=raw) is True
    assert pitfalls.read() == ["kept lesson"]
    raw.write_request.assert_called_once()
    raw.write_response.assert_called_once()
