"""Tests for optical focus search and the diagnostic freeze helper."""

from __future__ import annotations

import itertools
import math

import pytest

from physiclaw.core.hardware import focus
from physiclaw.core.hardware.focus import BLUR_THRESHOLD, CONFIRM_METERS, lock, search

SHARP = 400.0  # comfortably above BLUR_THRESHOLD (80)
SOFT = 30.0  # mid-hunt / dark-screen territory


class Rig:
    """Fake camera: scripted sharpness reads + a freeze that can refuse."""

    def __init__(self, meters, *, freeze_ok: bool = True) -> None:
        self._meters = iter(meters)
        self._freeze_ok = freeze_ok
        self.frozen = False
        self.unfreeze_calls = 0

    def meter(self) -> float | None:
        return next(self._meters)

    def freeze(self) -> bool:
        if self._freeze_ok:
            self.frozen = True
        return self._freeze_ok

    def unfreeze(self) -> None:
        self.frozen = False
        self.unfreeze_calls += 1


def test_locks_after_two_consecutive_sharp_meters() -> None:
    rig = Rig([SHARP, SHARP, SHARP])

    res = lock(rig.meter, rig.freeze, rig.unfreeze)

    assert res.locked
    assert rig.frozen is True
    assert rig.unfreeze_calls == 0


def test_locks_once_a_hunt_settles_mid_confirm() -> None:
    # First read catches the AF hunt; the next two agree sharp — that is
    # convergence, and the freeze verifies sharp.
    rig = Rig([SOFT, SHARP, SHARP, SHARP])

    res = lock(rig.meter, rig.freeze, rig.unfreeze)

    assert res.locked
    assert rig.frozen is True


def test_defers_when_scene_never_sharp() -> None:
    # Dark or blank screen: nothing to focus on — leave AF in charge and
    # let the caller retry on a sharp view.
    rig = Rig([SOFT] * CONFIRM_METERS)

    res = lock(rig.meter, rig.freeze, rig.unfreeze)

    assert not res.locked
    assert rig.frozen is False


def test_isolated_sharp_reads_do_not_count_as_settled() -> None:
    # A single sharp read can be the instant a hunt sweeps through focus
    # — only two CONSECUTIVE sharp reads mean converged.
    rig = Rig([SOFT, SHARP, SOFT, SHARP])

    res = lock(rig.meter, rig.freeze, rig.unfreeze)

    assert not res.locked
    assert rig.frozen is False


def test_no_frame_fails_open_without_deferring() -> None:
    rig = Rig([None])

    res = lock(rig.meter, rig.freeze, rig.unfreeze)

    assert not res.locked
    assert rig.frozen is False


def test_meter_loss_mid_confirm_fails_open() -> None:
    rig = Rig([SHARP, None])

    res = lock(rig.meter, rig.freeze, rig.unfreeze)

    assert not res.locked
    assert rig.frozen is False


def test_refused_freeze_fails_open_without_retry_bait() -> None:
    # A driver that rejects the AF toggle is terminal —
    # deferring would re-schedule the same doomed lock on every sharp
    # view forever.
    rig = Rig([SHARP, SHARP], freeze_ok=False)

    res = lock(rig.meter, rig.freeze, rig.unfreeze)

    assert not res.locked
    assert "refused" in res.detail
    assert rig.unfreeze_calls == 1


def test_soft_frozen_frame_reverts_to_autofocus() -> None:
    # The freeze wedged a mid-hunt lens position: the verification meter
    # is the only thing that can tell (drivers lie) — revert.
    rig = Rig([SHARP, SHARP, SOFT])

    res = lock(rig.meter, rig.freeze, rig.unfreeze)

    assert not res.locked
    assert "autofocus requested" in res.detail
    assert rig.unfreeze_calls == 1
    assert rig.frozen is False


def test_no_frame_after_freeze_reverts_to_autofocus() -> None:
    rig = Rig([SHARP, SHARP, None])

    res = lock(rig.meter, rig.freeze, rig.unfreeze)

    assert not res.locked
    assert rig.unfreeze_calls == 1


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_sharpness_does_not_start_a_focus_freeze(score):
    rig = Rig([score])

    result = lock(rig.meter, rig.freeze, rig.unfreeze)

    assert not result.locked
    assert not rig.frozen
    assert rig.unfreeze_calls == 0


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_verification_restores_autofocus(score):
    rig = Rig([SHARP, SHARP, score])

    result = lock(rig.meter, rig.freeze, rig.unfreeze)

    assert not result.locked
    assert not rig.frozen
    assert rig.unfreeze_calls == 1


def test_regression_partial_focus_freeze_restores_autofocus() -> None:
    rig = Rig([SHARP, SHARP])

    def partial_freeze() -> bool:
        rig.freeze()
        return False

    res = lock(rig.meter, partial_freeze, rig.unfreeze)

    assert not res.locked
    assert not rig.frozen


@pytest.mark.parametrize("failure_stage", ["freeze", "verify"])
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_regression_focus_exception_restores_autofocus(
    failure_stage, error_type
) -> None:
    rig = Rig([SHARP, SHARP])

    def freeze() -> bool:
        rig.freeze()
        if failure_stage == "freeze":
            raise error_type("camera disconnected")
        return True

    def meter() -> float:
        if rig.frozen:
            raise error_type("camera disconnected")
        return rig.meter()

    with pytest.raises(error_type, match="camera disconnected"):
        lock(meter, freeze, rig.unfreeze)

    assert not rig.frozen


def test_regression_focus_cleanup_failure_preserves_original_error() -> None:
    rig = Rig([SHARP, SHARP])
    attempts = []

    def freeze() -> bool:
        rig.freeze()
        raise RuntimeError("focus write failed")

    def unfreeze() -> None:
        attempts.append("unfreeze")
        raise OSError("cleanup also failed")

    with pytest.raises(RuntimeError, match="focus write failed"):
        lock(rig.meter, freeze, unfreeze)

    assert rig.frozen  # Unreachable driver: cleanup could only be attempted.
    assert attempts == ["unfreeze"]


@pytest.mark.parametrize("freeze_ok", [False, True])
def test_exhaustive_focus_meter_sequences(freeze_ok):
    """All 65,536 meter/write combinations, including exact gate boundaries.

    Use an independent decision oracle: invalid samples end acquisition;
    otherwise the first pair of consecutive sharp samples permits one freeze.
    Only a successful freeze followed by a valid sharp sample can stay frozen.
    """
    scores = [
        None,
        float("nan"),
        float("inf"),
        -float("inf"),
        0.0,
        BLUR_THRESHOLD - 0.001,
        BLUR_THRESHOLD,
        BLUR_THRESHOLD + 0.001,
    ]

    def sharp(value):
        return value is not None and math.isfinite(value) and value >= BLUR_THRESHOLD

    for sequence in itertools.product(scores, repeat=CONFIRM_METERS + 1):
        freeze_at = None
        reads = 0
        for index, value in enumerate(sequence[:CONFIRM_METERS]):
            reads += 1
            if value is None or not math.isfinite(value):
                break
            if index and sharp(sequence[index - 1]) and sharp(value):
                freeze_at = index
                break
        expected = False
        if freeze_at is not None and freeze_ok:
            reads += 1
            expected = sharp(sequence[freeze_at + 1])
        events = []
        iterator = iter(sequence)

        def meter():
            events.append("meter")
            return next(iterator)

        def freeze():
            events.append("freeze")
            return freeze_ok

        result = lock(meter, freeze, lambda: events.append("unfreeze"))

        assert result.locked is expected, sequence
        assert events.count("meter") == reads, sequence
        assert events.count("freeze") == int(freeze_at is not None), sequence
        assert events.count("unfreeze") == int(freeze_at is not None and not expected)


@pytest.mark.parametrize("failure_index", range(CONFIRM_METERS + 1))
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_interruption_at_every_meter_boundary(failure_index, error_type):
    # The first two soft samples postpone freezing until the last possible pair.
    readings = [SOFT, SOFT, SHARP, SHARP, SHARP]
    events = []

    def meter():
        index = events.count("meter")
        events.append("meter")
        if index == failure_index:
            raise error_type("injected meter failure")
        return readings[index]

    def freeze():
        events.append("freeze")
        return True

    with pytest.raises(error_type, match="injected meter failure"):
        lock(meter, freeze, lambda: events.append("unfreeze"))

    assert events.count("unfreeze") == int(failure_index == CONFIRM_METERS)


@pytest.mark.parametrize(
    "bounds,peak",
    [
        ((0, 1023, 1), 0),
        ((0, 1023, 1), 446),
        ((0, 1023, 1), 1023),
        ((10, 110, 5), 65),
        ((5, 5, 1), 5),
        ((10, 109, 5), 105),
    ],
)
def test_manual_search_finds_verified_position_within_real_step_grid(bounds, peak):
    writes = []
    unfreezes = []
    low, high, step = bounds

    def apply(value):
        assert low <= value <= high and (value - low) % step == 0
        writes.append(value)
        return True

    def meter():
        return max(0, 500 - (writes[-1] - peak) ** 2 / 10)

    value = search(bounds, meter, apply, lambda: unfreezes.append(True))
    assert value == peak
    assert writes[-1] == peak
    assert len(writes) <= focus.SEARCH_SAMPLES + 2
    assert not unfreezes


@pytest.mark.parametrize("variation", [0.0, 0.1])
def test_search_rejects_controls_without_optical_focus_response(variation):
    position = 0
    restored = []

    def apply(value):
        nonlocal position
        position = value
        return True

    value = search(
        (0, 100, 1),
        lambda: 300 * (1 + variation * position / 100),
        apply,
        lambda: restored.append(True),
    )

    assert value is None
    assert restored == [True]


def test_search_remeasures_peak_when_exposure_settles_during_refinement():
    position = 0
    readings = itertools.count()
    restored = []

    def apply(value):
        nonlocal position
        position = value
        return True

    def meter():
        # A bright coarse pass used to keep its obsolete 576 peak forever,
        # defeating both finer measurements and verification at normal gain.
        gain = 2 if next(readings) < 17 else 1
        return gain * max(1, 500 - (position - 552) ** 2 / 10)

    assert search((0, 1023, 1), meter, apply, lambda: restored.append(True)) == 552
    assert not restored


def test_search_counts_repeated_positions_against_sample_budget(monkeypatch):
    monkeypatch.setattr(focus, "SEARCH_SAMPLES", 18)
    positions = []
    restored = []

    def apply(value):
        positions.append(value)
        return True

    assert (
        search((0, 1023, 1), lambda: 500, apply, lambda: restored.append(True)) is None
    )
    assert len(positions) == 18
    assert positions.count(0) == 2
    assert restored == [True]


def test_search_rejects_brightness_settling_mistaken_for_lens_response(search_rig):
    # Ignored lens writes can still appear to improve while exposure settles.
    search_rig.meter.side_effect = itertools.chain([50], itertools.repeat(SHARP))

    value = search((0, 4, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze)

    assert value is None
    search_rig.unfreeze.assert_called_once_with()


@pytest.mark.parametrize("failure", [None, float("nan"), float("inf")])
def test_search_missing_or_invalid_worst_position_recheck_restores_af(
    search_rig, failure
):
    search_rig.meter.side_effect = [50, SHARP, failure]

    assert (
        search((0, 1, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze)
        is None
    )

    search_rig.unfreeze.assert_called_once_with()


@pytest.mark.parametrize("stage", ["apply", "meter"])
def test_search_expired_recheck_does_not_reapply_candidate(
    monkeypatch, search_rig, stage
):
    now = 0.0
    monkeypatch.setattr(focus.time, "monotonic", lambda: now)

    def apply(value):
        nonlocal now
        if stage == "apply" and search_rig.apply.call_count == 3:
            now = focus.SEARCH_SECONDS + 1
        return True

    def meter():
        nonlocal now
        if stage == "meter" and search_rig.meter.call_count == 3:
            now = focus.SEARCH_SECONDS + 1
        return SHARP if search_rig.meter.call_count == 2 else 50

    search_rig.apply.side_effect = apply
    search_rig.meter.side_effect = meter

    assert (
        search((0, 1, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze)
        is None
    )

    assert search_rig.apply.call_count == 3
    search_rig.unfreeze.assert_called_once_with()


@pytest.fixture
def search_rig(mocker):
    rig = mocker.Mock()
    rig.meter.return_value = SHARP
    rig.apply.return_value = True
    return rig


@pytest.mark.parametrize("failure", [None, float("nan"), float("inf"), 0])
def test_search_missing_invalid_or_soft_frames_restore_af(search_rig, failure):
    search_rig.meter.return_value = failure
    value = search(
        (0, 1023, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze
    )
    assert value is None
    search_rig.unfreeze.assert_called_once_with()


@pytest.mark.parametrize("stage", ["apply", "meter", "verify"])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_search_partial_writes_and_interruption_restore_af(
    search_rig, stage, error_type
):
    error = error_type("disconnected")
    if stage == "apply":
        search_rig.apply.side_effect = error
    else:
        search_rig.meter.side_effect = error if stage == "meter" else [SHARP, error]
    with pytest.raises(error_type, match="disconnected"):
        search((0, 0, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze)
    search_rig.unfreeze.assert_called_once_with()


@pytest.mark.parametrize("final_score", [None, float("nan"), 79, 100])
def test_search_rejects_final_scene_degradation_even_above_absolute_gate(
    search_rig, final_score
):
    search_rig.meter.side_effect = itertools.chain([500], itertools.repeat(final_score))
    value = search((0, 0, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze)
    assert value is None
    search_rig.unfreeze.assert_called_once_with()


@pytest.mark.parametrize(
    "verification,expected",
    [([30, 400, 400], 0.0), ([400, 30, 400, 400], 0.0), ([400, 30, 400, 30], None)],
)
def test_search_allows_lens_settling_but_requires_two_consecutive_sharp_frames(
    search_rig, verification, expected
):
    search_rig.meter.side_effect = itertools.chain(
        [500, *verification], itertools.repeat(SOFT)
    )

    assert (
        search((0, 0, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze)
        == expected
    )

    assert search_rig.unfreeze.call_count == int(expected is None)


@pytest.mark.parametrize(
    "peak,settling",
    [
        (3973, [0, 84, 2784, 3122, 3299, 3510]),
        (977, [139, 373, 586, 763, 879, 932]),
        (4571, [1801, 2331, 3034, 3622, 4006, 4261]),
    ],
)
def test_search_verifies_slow_full_range_moves_measured_on_ugreen(
    search_rig, peak, settling
):
    # Real settling traces: the old four-read window ended before the pair
    # of acceptable frames. Never relax sharpness for a moving lens.
    search_rig.meter.side_effect = [1, peak, 1, *settling]

    assert (
        search((0, 1023, 1023), search_rig.meter, search_rig.apply, search_rig.unfreeze)
        == 1023
    )
    search_rig.unfreeze.assert_not_called()


def test_search_full_range_settling_is_bounded_and_requires_consecutive_reads(
    search_rig,
):
    search_rig.meter.side_effect = itertools.chain(
        [1, 500, 1], itertools.cycle([500, 30])
    )

    assert (
        search((0, 1023, 1023), search_rig.meter, search_rig.apply, search_rig.unfreeze)
        is None
    )
    assert search_rig.meter.call_count == 3 + focus.SEARCH_CONFIRM_METERS
    search_rig.unfreeze.assert_called_once_with()


def test_search_allows_settling_after_each_large_verification_move(search_rig):
    search_rig.meter.side_effect = [50, 500, 500, 50, 30, 500, 500]

    assert (
        search((0, 1, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze)
        == 1.0
    )

    assert [call.args[0] for call in search_rig.apply.call_args_list] == [0, 1, 0, 1]
    search_rig.unfreeze.assert_not_called()


@pytest.mark.parametrize("accepted", [[False], [True, False]], ids=["initial", "final"])
def test_search_refused_write_restores_af(search_rig, accepted):
    search_rig.apply.side_effect = accepted
    value = search((0, 0, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze)
    assert value is None
    search_rig.unfreeze.assert_called_once_with()


@pytest.mark.parametrize("budget,limit", [("SEARCH_SECONDS", 0), ("SEARCH_SAMPLES", 1)])
def test_search_has_time_and_sample_budgets(monkeypatch, search_rig, budget, limit):
    monkeypatch.setattr(focus, budget, limit)
    value = search(
        (0, 1023, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze
    )
    assert value is None
    search_rig.unfreeze.assert_called_once_with()


@pytest.mark.parametrize("write", [1, 2], ids=["candidate", "final-reapply"])
def test_search_does_not_start_meter_after_focus_write_exhausts_budget(
    monkeypatch, search_rig, write
):
    now = 0.0
    monkeypatch.setattr(focus.time, "monotonic", lambda: now)

    def delayed_apply(value):
        nonlocal now
        if search_rig.apply.call_count == write:
            now = focus.SEARCH_SECONDS + 1
        return True

    search_rig.apply.side_effect = delayed_apply
    value = search((0, 0, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze)

    assert value is None
    assert search_rig.meter.call_count == write - 1
    search_rig.unfreeze.assert_called_once_with()


def test_search_cleanup_failure_preserves_original_error(search_rig):
    search_rig.apply.side_effect = KeyboardInterrupt("cancelled")
    search_rig.unfreeze.side_effect = OSError("also disconnected")
    with pytest.raises(KeyboardInterrupt, match="cancelled"):
        search((0, 1, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze)


@pytest.mark.parametrize("bounds", [(-1, 10, 1), (10, 0, 1), (0, 10, 0)])
def test_search_unusable_bounds_do_not_move_the_lens(search_rig, bounds, focus_events):
    value = search(bounds, search_rig.meter, search_rig.apply, search_rig.unfreeze)
    assert value is None
    search_rig.apply.assert_not_called()
    assert focus_events[-1]["reason"] == "invalid_focus_bounds"


@pytest.mark.parametrize(
    "late_read", [2, 3], ids=["first-verification", "last-verification"]
)
def test_search_late_final_frame_is_not_saved(monkeypatch, search_rig, late_read):
    now = 0.0
    monkeypatch.setattr(focus.time, "monotonic", lambda: now)

    def meter():
        nonlocal now
        if search_rig.meter.call_count == late_read:
            now = focus.SEARCH_SECONDS + 1
        return SHARP

    search_rig.meter.side_effect = meter
    value = search((0, 0, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze)
    assert value is None
    search_rig.unfreeze.assert_called_once_with()


@pytest.fixture
def focus_events(mocker, search_rig):
    events = []

    def capture(event, **fields):
        frame = f"frame-{search_rig.meter.call_count}"
        reference = {
            "seq": len(events) + 1,
            "frame_id": fields.get("frame_id", frame),
            "input_artifact_id": fields.get("input_artifact_id", frame),
        }
        events.append({"event": event, **fields, **reference})
        return reference

    mocker.patch.object(focus, "record_event", side_effect=capture)
    return events


def test_search_decision_links_peak_and_consecutive_verification_frames(
    search_rig, focus_events
):
    search_rig.meter.side_effect = [50, 500, 50, 100, 450, 460]

    result = search((0, 1, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze)

    decision = focus_events[-1]
    assert result == 1.0
    assert decision["reason"] == "verified_peak"
    assert decision["peak_sample"]["frame_id"] == "frame-2"
    assert decision["peak_sample"]["sharpness"] == 500
    assert decision["thresholds"]["verification_minimum_sharpness"] == 400
    assert [s["frame_id"] for s in decision["verification_samples"]] == [
        "frame-4",
        "frame-5",
        "frame-6",
    ]
    assert [s["consecutive"] for s in decision["verification_samples"]] == [0, 1, 2]
    assert decision["frame_id"] == "frame-6"
    assert decision["autofocus_restore"] == {"requested": False}


@pytest.mark.parametrize(
    "readings,bounds,reason,phase",
    [
        ([None], (0, 0, 1), "measurement_unavailable", "coarse"),
        ([float("nan")], (0, 0, 1), "invalid_sharpness", "coarse"),
        ([30], (0, 0, 1), "peak_below_minimum_sharpness", "coarse"),
        ([400, 400], (0, 1, 1), "insufficient_lens_response", "lens_response"),
        (
            [50, 400, *([400] * 8)],
            (0, 1, 1),
            "lens_response_not_reproduced",
            "lens_response",
        ),
        ([400, *([30] * 8)], (0, 0, 1), "verification_not_stable", "verification"),
    ],
)
def test_search_failure_explains_rejected_gate_in_summary_event(
    search_rig, focus_events, readings, bounds, reason, phase
):
    search_rig.meter.side_effect = readings

    result = search(bounds, search_rig.meter, search_rig.apply, search_rig.unfreeze)

    decision = focus_events[-1]
    assert result is None
    assert decision["status"] == "failed"
    assert decision["reason"] == reason
    assert decision["phase"] == phase
    assert decision["summarize"] is True
    assert decision["autofocus_restore"] == {"requested": True, "call_succeeded": True}


@pytest.mark.parametrize(
    "accepted,phase", [([False], "coarse"), ([True, False], "verification")]
)
def test_search_driver_rejection_identifies_phase_and_attempted_position(
    search_rig, focus_events, accepted, phase
):
    search_rig.apply.side_effect = accepted

    result = search(
        (42, 42, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze
    )

    decision = focus_events[-1]
    assert result is None
    assert decision["reason"] == "driver_rejected_focus"
    assert decision["phase"] == phase
    assert decision["attempted_position"] == 42


@pytest.mark.parametrize(
    "budget,limit,reason",
    [
        ("SEARCH_SECONDS", 0, "deadline_exceeded"),
        ("SEARCH_SAMPLES", 1, "sample_budget_exhausted"),
    ],
)
def test_search_budget_failure_is_explicit(
    search_rig, focus_events, monkeypatch, budget, limit, reason
):
    monkeypatch.setattr(focus, budget, limit)

    result = search((0, 1, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze)

    assert result is None
    assert focus_events[-1]["reason"] == reason


def test_search_exception_and_failed_autofocus_restore_keep_both_errors(
    search_rig, focus_events
):
    search_rig.apply.side_effect = OSError("focus write failed")
    search_rig.unfreeze.side_effect = OSError("autofocus write failed")

    with pytest.raises(OSError, match="focus write failed"):
        search((0, 1, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze)

    decision = focus_events[-1]
    assert decision["reason"] == "exception"
    assert decision["message"] == "focus write failed"
    assert decision["autofocus_restore"] == {
        "requested": True,
        "call_succeeded": False,
        "message": "autofocus write failed",
    }
