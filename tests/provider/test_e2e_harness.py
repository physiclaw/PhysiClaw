"""Replay fidelity and cache diagnostics without private fixtures or API calls."""

from copy import deepcopy

import pytest

from physiclaw.agent.engine.output import ActionCatalog
from physiclaw.contract.dto import Usage, UserMessage
from scripts import provider_e2e as harness


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("original_native", [False, True])
def test_replay_removes_source_instruction_and_keeps_selected_retry(
    monkeypatch, tmp_path, native, original_native
):
    catalog = ActionCatalog.compile(
        [
            {
                "name": "peek",
                "description": "Look",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            }
        ],
        [],
        max_scratchpad=100,
    )
    spec = catalog.spec()
    instruction = (
        f"Return one {spec.name} JSON object matching the response schema."
        if original_native
        else f"Submit one {spec.name} through the {spec.function_name} function."
    )
    first = {
        "kind": "request",
        "turn": 7,
        "messages": [
            {
                "role": "system",
                "content": "Host prompt\n\n"
                + instruction
                + " Do not include standalone prose.",
            },
            {"role": "user", "content": "Original task"},
        ],
    }
    retry = deepcopy(first)
    retry["messages"].append({"role": "user", "content": "Correct missing scratchpad"})
    records = [{"kind": "session_start", "output_schema": catalog.schema}, first, retry]
    monkeypatch.setattr(harness, "session_records", lambda _: records)
    original = deepcopy(records)
    history, rebuilt = harness.load_snapshot(tmp_path, 7, native=native, record=retry)
    assert history[0].content == "Host prompt"
    assert history[-1] == UserMessage("Correct missing scratchpad")
    assert rebuilt.schema == spec.schema
    assert records == original


def test_cache_fingerprints_detect_prefix_and_schema_changes_without_mutation():
    payload = {
        "model": "test",
        "tools": [{"name": "peek"}],
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": "stable"}]},
            {"role": "user", "content": "variable"},
        ],
    }
    before = deepcopy(payload)
    facts = harness.request_cache_facts(payload)
    assert harness.request_cache_facts(deepcopy(payload)) == facts
    marked = deepcopy(payload)
    marked["messages"][0]["content"][0]["cache_control"] = {"type": "ephemeral"}
    changed = harness.request_cache_facts(marked)
    assert changed["request_hash"] != facts["request_hash"]
    assert [p["hash"] for p in changed["prefixes"]] == [
        p["hash"] for p in facts["prefixes"]
    ]
    assert changed["prefixes"][1]["marked"]
    tail = deepcopy(payload)
    tail["messages"][-1]["content"] = "new tail"
    assert harness.request_cache_facts(tail)["prefixes"][:-1] == facts["prefixes"][:-1]
    schema = deepcopy(payload)
    schema["tools"][0]["name"] = "different"
    assert (
        harness.request_cache_facts(schema)["prefixes"][0]["hash"]
        != facts["prefixes"][0]["hash"]
    )
    assert payload == before


def test_replay_rejects_image_paths_outside_session(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        harness.restore_images(
            {"type": "image_url", "image_url": {"url": "../private.png"}}, tmp_path
        )


def test_restored_image_results_do_not_share_mutable_state(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    (images / "screen.png").write_bytes(b"image fixture")
    source = {"type": "image_url", "image_url": {"url": "images/screen.png"}}
    first = harness.restore_images(source, tmp_path)
    expected = deepcopy(first)
    first["image_url"]["url"] = "changed by caller"
    assert harness.restore_images(source, tmp_path) == expected
    assert source["image_url"]["url"] == "images/screen.png"


@pytest.mark.parametrize("field", ["cache_control", "prompt_cache_breakpoint"])
def test_cache_diagnostics_preserve_tool_schema_properties_named_like_markers(field):
    payload = {
        "model": "test",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "configure",
                    "parameters": {
                        "type": "object",
                        "properties": {field: {"type": "string"}},
                    },
                },
            }
        ],
        "messages": [{"role": "user", "content": "task"}],
    }
    before = harness.request_cache_facts(payload)
    changed = deepcopy(payload)
    changed["tools"][0]["function"]["parameters"]["properties"][field]["type"] = (
        "integer"
    )
    after = harness.request_cache_facts(changed)
    assert all(
        a["hash"] != b["hash"] for a, b in zip(before["prefixes"], after["prefixes"])
    )


@pytest.mark.parametrize(
    "field", ["prompt_tokens", "completion_tokens", "total_tokens"]
)
@pytest.mark.parametrize(
    "invalid", [None, True, False, -1, "-1", 1.5, "invalid", [], {}]
)
def test_independent_oracle_preserves_unknown_google_counters(field, invalid):
    counters = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 150}
    counters[field] = invalid
    expected = Usage(
        0 if field == "prompt_tokens" else 100,
        0 if field == "completion_tokens" else 20,
    )
    assert harness.raw_usage({"usage": counters}, "google") == expected


@pytest.mark.parametrize(
    "provider,counters,expected",
    [
        (
            "google",
            {"prompt_tokens": "100", "completion_tokens": "20", "total_tokens": "150"},
            Usage(100, 50, reasoning_tokens=30),
        ),
        (
            "google",
            {
                "prompt_tokens": 100,
                "completion_tokens": 70,
                "total_tokens": 170,
                "completion_tokens_details": {"reasoning_tokens": 50},
            },
            Usage(100, 70, reasoning_tokens=50),
        ),
        (
            "google",
            {"prompt_tokens": 100, "total_tokens": 150},
            Usage(100),
        ),
        (
            "openai",
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 0},
                "cached_tokens": 99,
            },
            Usage(100, 20),
        ),
        (
            "deepseek",
            {
                "input_tokens": 999,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_cache_hit_tokens": 60,
            },
            Usage(100, 20, cached_tokens=60),
        ),
        (
            "anthropic",
            {
                "input_tokens": "10",
                "output_tokens": "20",
                "cache_read_input_tokens": "100",
                "cache_creation_input_tokens": "5",
            },
            Usage(115, 20, cached_tokens=100, cache_creation_tokens=5),
        ),
        ("anthropic", [], Usage()),
        (
            "qwen",
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": [],
            },
            Usage(100, 20),
        ),
        (
            "qwen",
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cache_creation_input_tokens": 50},
            },
            Usage(100, 20, cache_creation_tokens=50),
        ),
    ],
)
def test_independent_usage_oracle_obeys_each_wire_protocol(
    provider, counters, expected
):
    assert harness.raw_usage({"usage": counters}, provider) == expected


@pytest.mark.parametrize("canonical", [0, None])
def test_canonical_zero_cache_writes_do_not_fall_back_to_legacy_count(canonical):
    raw = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {
                "cache_write_tokens": canonical,
                "cache_creation_input_tokens": 50,
            },
        }
    }
    assert harness.raw_usage(raw, "qwen") == Usage(100, 20)
