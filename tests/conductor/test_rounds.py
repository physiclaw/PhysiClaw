"""Tests for `physiclaw.conductor.walk.rounds` — a run's rounds as values:
the plan a run expands into, the returns a finished round records, and
the ref values a step reads at the cursor."""

from __future__ import annotations

import dataclasses

import pytest
from conductor_fakes import EACH, LEG, write_pack

from physiclaw.conductor.load.pack import load_spec
from physiclaw.conductor.spec.model import PlaybookError, RunNode
from physiclaw.conductor.walk import rounds
from physiclaw.conductor.walk.course import Course, Round
from physiclaw.conductor.walk.ledger import Ledger


def _spec():
    write_pack(playbooks={"flow": EACH, "flow.leg": LEG})
    spec, _ = load_spec("demo", "flow")
    return spec


def _run(spec) -> RunNode:
    run = spec.runs[0]
    assert isinstance(run, RunNode)
    return run


def test_plan_makes_one_round_per_listed_item() -> None:
    spec = _spec()
    ledger = Ledger(ref="demo/flow", task={})

    planned = rounds.plan(_run(spec), {"parse.items": "milk\neggs"}, ledger)

    assert [(r.key, r.inputs) for r in planned] == [
        ("milk", {"inputs.what": "milk"}),
        ("eggs", {"inputs.what": "eggs"}),
    ]


def test_plan_leaves_out_a_finished_round() -> None:
    spec = _spec()
    run = _run(spec)
    ledger = Ledger(ref="demo/flow", task={})
    ledger.round_done(Round(run, "milk", {}).prefix, {"did": "searched milk"})

    planned = rounds.plan(run, {"parse.items": "milk\neggs"}, ledger)

    assert [r.key for r in planned] == ["eggs"]


def test_plan_past_the_round_budget_raises() -> None:
    spec = _spec()

    with pytest.raises(PlaybookError, match="3 rounds, more than its 2"):
        rounds.plan(
            _run(spec), {"parse.items": "a\nb\nc"}, Ledger(ref="demo/flow", task={})
        )


def test_returns_fill_from_the_rounds_values() -> None:
    spec = _spec()
    rd = Round(_run(spec), "milk", {"inputs.what": "milk"})

    got = rounds.returns(rd, {"inputs.what": "milk"})

    assert got == {"did": "searched milk"}


def test_values_read_a_runs_done_rounds_one_per_line() -> None:
    spec = _spec()
    run = _run(spec)
    ledger = Ledger(ref="demo/flow", task={})
    ledger.decide("parse.items", "milk\neggs\ntea")
    ledger.round_done(Round(run, "milk", {}).prefix, {"did": "searched milk"})
    ledger.round_done(Round(run, "tea", {}).prefix, {"did": "searched tea"})

    vals = rounds.values(Course(spec), ledger, {"inputs.keyword": "x"}, ["ok"])

    assert (vals["leg.did"], vals["inputs.keyword"], vals["ask.replies"]) == (
        "searched milk\nsearched tea",
        "x",
        "ok",
    )


def test_values_inside_a_round_read_only_the_round() -> None:
    spec = _spec()
    run = _run(spec)
    ledger = Ledger(ref="demo/flow", task={})
    ledger.decide("parse.items", "milk")
    course = Course(spec)
    course.skip_to(1)
    course.expand(rounds.plan(run, {"parse.items": "milk"}, ledger))

    vals = rounds.values(course, ledger, {"inputs.keyword": "x"}, [])

    assert vals["inputs.what"] == "milk" and "inputs.keyword" not in vals


def test_plan_with_a_required_input_unfed_names_the_run() -> None:
    spec = _spec()
    bare = dataclasses.replace(_run(spec), each=None)  # feeds `what` nothing

    with pytest.raises(PlaybookError, match="^run 'leg': "):
        rounds.plan(bare, {}, Ledger(ref="demo/flow", task={}))
