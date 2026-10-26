"""Tests for `physiclaw.core.hardware.focus` — the lock flow."""

from __future__ import annotations

import pytest

from physiclaw.core.hardware.focus import CONFIRM_METERS, lock

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
