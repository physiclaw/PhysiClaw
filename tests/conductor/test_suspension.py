"""Tests for `physiclaw.conductor.walk.suspension` — the one cross-wake file.

Unit pins for the module's own surface: the cheap `suspended_ref` peek
(the CLI/jobs-facing read that skips walk-state validation) and the
one-shot `clear_suspended`. The WAKE path — `setup.load_suspended`'s
corrupt-file and consumed-on-load behavior — has its own tests in
test_program.py; nothing here duplicates them.
"""

from __future__ import annotations

import json

import pytest

from physiclaw.conductor.drive import setup
from physiclaw.conductor.walk import suspension


def _write(text: str) -> None:
    p = suspension.suspended_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_clear_reports_whether_anything_was_there() -> None:
    assert suspension.clear_suspended() is False  # nothing to clear
    _write("{}")
    assert suspension.clear_suspended() is True
    assert not suspension.suspended_path().exists()


def test_ref_is_none_when_nothing_is_suspended() -> None:
    assert suspension.suspended_ref() is None


def test_ref_reads_the_pair_without_validating_the_rest() -> None:
    # `suspended_ref` is the cheap peek (jobs/CLI); it must not demand
    # the full walk-state schema the loader validates.
    _write(json.dumps({"app": "demo", "playbook": "buy"}))
    assert suspension.suspended_ref() == ("demo", "buy")


def test_ref_swallows_garbage_and_half_written_files() -> None:
    # A crash mid-write (or a hand-edit) must read as "nothing
    # suspended" — the peek is used from surfaces that must never
    # crash on a bad file.
    for text in ("not json{", "", json.dumps({"app": "demo"})):
        _write(text)
        assert suspension.suspended_ref() is None, text


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        "scalar",
        1,
        {},
        {"schema": 0, "app": "demo", "playbook": "buy"},
        {"schema": 2, "app": "demo", "playbook": "buy"},
        {"schema": "1", "app": "demo", "playbook": "buy"},
    ],
    ids=["null", "list", "string", "number", "missing-schema", "old", "new", "text"],
)
def test_invalid_position_root_or_schema_is_consumed_without_building_a_walk(
    mocker, document
) -> None:
    _write(json.dumps(document))
    path = suspension.suspended_path()
    load_spec = mocker.patch.object(setup, "load_spec")
    build = mocker.patch.object(setup, "build_program")

    assert suspension.read_position(path) is None
    with pytest.raises(ValueError, match="no suspension this build can read"):
        suspension.read_suspended()
    resumed = setup.load_suspended()

    assert resumed is None
    assert not path.exists()
    load_spec.assert_not_called()
    build.assert_not_called()
