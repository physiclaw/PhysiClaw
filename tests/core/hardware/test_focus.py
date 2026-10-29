"""Tests for `physiclaw.core.hardware.focus` — the lock flow."""

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
    assert len(writes) <= focus.SEARCH_SAMPLES + 1
    assert not unfreezes


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
    search_rig.meter.side_effect = [500, final_score]
    value = search((0, 0, 1), search_rig.meter, search_rig.apply, search_rig.unfreeze)
    assert value is None
    search_rig.unfreeze.assert_called_once_with()


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
def test_search_unusable_bounds_do_not_move_the_lens(search_rig, bounds):
    value = search(bounds, search_rig.meter, search_rig.apply, search_rig.unfreeze)
    assert value is None
    search_rig.apply.assert_not_called()


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
