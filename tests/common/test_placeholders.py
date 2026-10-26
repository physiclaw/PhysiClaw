"""Tests for `physiclaw.common.placeholders` — the `<<TOKEN>>` grammar
and the local `playbooks/placeholders.yml` values file it fills from."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from physiclaw.common import paths
from physiclaw.common.placeholders import (
    PLACEHOLDER_VALUES_FILENAME,
    find_placeholders,
    placeholder_values,
    resolve_data_placeholders,
    resolve_placeholders,
    write_placeholder_values,
)


def test_find_placeholders_dedupes_in_order() -> None:
    found = find_placeholders("hi <<CONTACT>> and <<CITY>> and <<CONTACT>>")

    assert found == ["CONTACT", "CITY"]


def test_find_placeholders_ignores_input_braces_and_lowercase() -> None:
    # `{input}` refs and lowercase pseudo-tokens are NOT placeholders —
    # the sigil is uppercase-only so the grammars can't collide.
    assert find_placeholders("tap {keyword} <<x>> <<不>>") == []


def test_resolve_placeholders_fills_from_the_local_values_file() -> None:
    # The load-time contract: pack files keep <<TOKEN>>s on disk; the
    # values live ONLY in playbooks/placeholders.yml.
    write_placeholder_values({"CONTACT": "Alice"})

    out = resolve_placeholders('anchors: ["<<CONTACT>>"]', ValueError)

    assert out == 'anchors: ["Alice"]'


def test_resolve_placeholders_rejects_a_token_with_no_value() -> None:
    with pytest.raises(ValueError, match="placeholders.yml"):
        resolve_placeholders("x: <<UNSET>>", ValueError)


def test_resolve_placeholders_wraps_a_broken_values_file() -> None:
    # A malformed values file must fail loudly in the caller's error
    # class — silence would resurface as a baffling "unpopulated
    # placeholder" at every pack parse.
    from physiclaw.common import paths
    from physiclaw.common.text import write_text

    class Boom(Exception):
        pass

    path = paths.playbooks_dir() / PLACEHOLDER_VALUES_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, "- just\n- a list\n")

    with pytest.raises(Boom, match="mapping"):
        resolve_placeholders("plain text", Boom)


def test_write_placeholder_values_round_trips() -> None:
    write_placeholder_values({"CONTACT": "小乔", "CITY": "Hangzhou"})

    assert placeholder_values() == {"CONTACT": "小乔", "CITY": "Hangzhou"}


def test_regression_write_placeholder_question_prefix_round_trips() -> None:
    write_placeholder_values({"CONTACT": "?Alice"})

    assert placeholder_values() == {"CONTACT": "?Alice"}


# This checks a filesystem round trip, not a 200ms disk-latency guarantee.
@settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.dictionaries(st.text(), st.text(), max_size=5))
def test_write_placeholder_values_preserves_string_keys_and_values(values) -> None:
    write_placeholder_values(values)

    assert placeholder_values() == values


@pytest.mark.parametrize(
    "value", ["[Alice, Bob]", "{name: Alice}", "null", "123", "true"]
)
def test_regression_placeholder_values_reject_nonstring_values(value) -> None:
    path = paths.playbooks_dir() / PLACEHOLDER_VALUES_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text(f"CONTACT: {value}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="CONTACT.*string"):
        placeholder_values()


def test_placeholder_values_reject_nonstring_keys() -> None:
    path = paths.playbooks_dir() / PLACEHOLDER_VALUES_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text("123: Alice\n", encoding="utf-8")

    with pytest.raises(ValueError, match="token names must be strings"):
        placeholder_values()


@given(st.text().filter(lambda value: not find_placeholders(value)))
def test_resolve_data_placeholders_preserves_literal_unicode(value) -> None:
    data = {"nested": [{"<<KEY>>": "Hello <<VALUE>>", "number": 3, "empty": None}]}

    resolved = resolve_data_placeholders(
        data, ValueError, {"KEY": "contact", "VALUE": value}
    )

    assert resolved == {
        "nested": [{"contact": "Hello " + value, "number": 3, "empty": None}]
    }
    assert data["nested"][0]["<<KEY>>"] == "Hello <<VALUE>>"


def test_resolve_data_placeholders_rejects_key_collisions() -> None:
    data = {"<<NAME>>": "first", "same": "second"}

    with pytest.raises(ValueError, match="duplicate key.*same"):
        resolve_data_placeholders(data, ValueError, {"NAME": "same"})


def test_resolve_data_placeholders_rejects_unpopulated_nested_tokens() -> None:
    data = {"nested": ["<<MISSING>>"]}

    with pytest.raises(ValueError, match="unpopulated.*MISSING"):
        resolve_data_placeholders(data, ValueError, {})


def test_regression_placeholder_values_reject_aliases() -> None:
    path = paths.playbooks_dir() / PLACEHOLDER_VALUES_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text("CONTACT: &name Alice\nOTHER: *name\n", encoding="utf-8")

    with pytest.raises(ValueError, match="aliases"):
        placeholder_values()
