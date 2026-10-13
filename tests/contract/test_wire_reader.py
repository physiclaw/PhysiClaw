"""Tests for `physiclaw.contract.wire` — the reader half of the codec:
request messages as leaf blocks, texts, and persisted image refs."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from physiclaw.contract import wire


def _wire(tmp_path, records):
    p = tmp_path / "wire.jsonl"
    p.write_text(
        "\n".join(json.dumps(r) for r in records) + "\nnot json\n", encoding="utf-8"
    )
    return p


def test_messages_flatten_tool_results_and_skip_non_requests(tmp_path) -> None:
    p = _wire(
        tmp_path,
        [
            {"kind": "session_start"},
            {
                "kind": "request",
                "messages": [
                    {"role": "system", "content": "doctrine"},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "content": [
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "ref",
                                            "ref": "images/a.jpg",
                                        },
                                    },
                                    {"type": "text", "text": "listing"},
                                ],
                            },
                            "stray string",
                        ],
                    },
                ],
            },
            "a string record",
        ],
    )

    out = list(wire.iter_request_messages(p))

    assert out == [
        ("system", [{"type": "text", "text": "doctrine"}]),
        (
            "user",
            [
                {"type": "image", "source": {"type": "ref", "ref": "images/a.jpg"}},
                {"type": "text", "text": "listing"},
            ],
        ),
    ]
    assert list(wire.iter_request_texts(p)) == [
        ("system", "doctrine"),
        ("user", "listing"),
    ]


def test_image_ref_reads_both_scrubbed_shapes_only() -> None:
    assert (
        wire.image_ref({"type": "image_url", "image_url": {"url": "images/a.jpg"}})
        == "images/a.jpg"
    )
    assert (
        wire.image_ref(
            {"type": "image", "source": {"type": "ref", "ref": "images/b.jpg"}}
        )
        == "images/b.jpg"
    )
    # Unscrubbed data, a byte-count stub, and text are not refs.
    assert (
        wire.image_ref(
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,xx"}}
        )
        is None
    )
    assert (
        wire.image_ref({"type": "image", "source": {"type": "base64", "byte_count": 3}})
        is None
    )
    assert wire.image_ref({"type": "text", "text": "images/a.jpg"}) is None


def test_scrub_and_image_ref_round_trip() -> None:
    persisted = lambda mime, data: "images/x.jpg"  # noqa: E731
    for block in (
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,aGk="}},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "aGk="},
        },
    ):
        assert wire.image_ref(wire.scrub_block(block, persisted)) == "images/x.jpg"


@pytest.mark.parametrize(
    "messages",
    [None, 7, "not a list", {}, [None], [3], [{"role": [], "content": "bad"}]],
)
def test_request_reader_skips_malformed_messages_without_losing_later_records(
    tmp_path, messages
):
    path = _wire(
        tmp_path,
        [
            {"kind": "request", "messages": messages},
            {"kind": "request", "messages": [{"role": "user", "content": "kept"}]},
        ],
    )

    texts = list(wire.iter_request_texts(path))

    assert texts == [("user", "kept")]


def test_request_reader_recovers_after_invalid_utf8_line(tmp_path):
    path = tmp_path / "wire.jsonl"
    valid = json.dumps(
        {"kind": "request", "messages": [{"role": "user", "content": "牛奶"}]}
    ).encode()
    path.write_bytes(valid + b'\n{"kind":"request","bad":"\xff"}\n' + valid + b"\n")

    texts = list(wire.iter_request_texts(path))

    assert texts == [("user", "牛奶"), ("user", "牛奶")]


@pytest.mark.parametrize("bad_text", [None, [], {}, 42, False])
def test_leaf_blocks_excludes_malformed_text_from_replay(bad_text):
    content = [{"type": "text", "text": bad_text}, {"type": "text", "text": "kept"}]

    blocks = list(wire.leaf_blocks(content))

    assert blocks == [{"type": "text", "text": "kept"}]


@pytest.mark.parametrize(
    "block",
    [
        {"type": "image_url", "image_url": []},
        {"type": "image_url", "image_url": "bad"},
        {"type": "image_url", "image_url": {"url": 7}},
        {"type": "image", "source": "bad"},
        {"type": "image", "source": {"type": "ref", "ref": []}},
        {"type": "image", "source": {"type": "ref", "ref": 7}},
    ],
)
def test_image_ref_returns_none_for_malformed_reference(block):
    ref = wire.image_ref(block)

    assert ref is None


def test_scrub_preserves_openai_image_detail_and_block_metadata():
    block = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,aGk=", "detail": "high"},
        "cache_control": {"type": "ephemeral"},
    }

    result = wire.scrub_block(block, lambda mime, data: "images/x.png")

    assert result == {
        "type": "image_url",
        "image_url": {"url": "images/x.png", "detail": "high"},
        "cache_control": {"type": "ephemeral"},
    }
    assert block["image_url"]["url"] == "data:image/png;base64,aGk="


def test_scrub_preserves_anthropic_block_metadata():
    block = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "aGk="},
        "cache_control": {"type": "ephemeral"},
    }

    result = wire.scrub_block(block, lambda mime, data: "images/x.png")

    assert result == {
        "type": "image",
        "source": {"type": "ref", "ref": "images/x.png"},
        "cache_control": {"type": "ephemeral"},
    }
    assert block["source"]["data"] == "aGk="


_nested_text = st.recursive(
    st.text().map(lambda text: ({"type": "text", "text": text}, [text])),
    lambda children: st.lists(children, max_size=4).map(
        lambda items: (
            {"type": "tool_result", "content": [item[0] for item in items]},
            [text for item in items for text in item[1]],
        )
    ),
    max_leaves=20,
)


@settings(max_examples=500, derandomize=True)
@given(_nested_text)
def test_nested_text_survives_scrubbing_and_json_roundtrip(case):
    block, expected = case
    messages = [{"role": "tool", "content": [block]}]
    original = deepcopy(messages)

    scrubbed = wire.scrub_messages(messages, lambda mime, data: "images/x.png")
    restored = json.loads(json.dumps(scrubbed))
    blocks = list(wire.leaf_blocks(restored[0]["content"]))

    assert list(map(lambda b: b["text"], blocks)) == expected
    assert messages == original


@pytest.mark.parametrize(
    "raw, expected",
    json.loads(
        (
            Path(__file__).resolve().parents[1] / "data/contract/reply_gist_cases.json"
        ).read_text(encoding="utf-8")
    ),
)
def test_reply_gist_normalizes_known_wire_shapes_and_leaves_unknown_raw(raw, expected):
    gist = wire.reply_gist(raw)

    assert gist == expected
