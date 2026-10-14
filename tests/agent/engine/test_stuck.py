"""Tests for `physiclaw.agent.engine.stuck` — the engine's loop detectors.

Accepted mutation survivors: 42 presentation-only changes to warning prose
or separators; 15 equivalents within the engine lifecycle: bounded history
sizes still sufficient for period-3 blocking (20/22/23), unchanged zero or
invalid-coordinate signatures (34/36), cycle counts below both thresholds
and redundant lookback skips (39/43-46/51), the initially empty step reset
(76), absent-count defaults still below blocking (95/110), and break versus
continue after a standalone press's only center (152). Warning counts,
blocking thresholds, jitter, exemptions, sequence identity and resets are
asserted through the public guard API rather than exact prose snapshots.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.agent.engine import stuck
from physiclaw.agent.engine.stuck import BLOCK_AT, WARN_AT, StuckGuard

BOX = [0.908, 0.526, 0.983, 0.562]  # the log's dead + stepper
NEAR_BOX = [0.911, 0.527, 0.983, 0.562]  # re-transcription jitter, same target
FAR_BOX = [0.100, 0.100, 0.200, 0.200]


def _guard() -> StuckGuard:
    return StuckGuard(_exempt=[])


def _press(guard: StuckGuard, bbox, *, tool: str = "tap", changed: bool | None = False):
    """One dispatched press with its verdict fed back. Returns the warning."""
    return guard.record(tool, {"bbox": bbox}, changed)


# ---------- counting & tiers ----------


def test_warns_on_third_fruitless_press() -> None:
    g = _guard()
    warnings = [_press(g, BOX) for _ in range(WARN_AT)]
    assert warnings[:-1] == [None, None]
    assert "press #3" in warnings[-1]
    assert "CONVENTION § Stuck" in warnings[-1]


def test_blocks_fifth_press_on_exhausted_target() -> None:
    g = _guard()
    for _ in range(BLOCK_AT - 1):
        _press(g, BOX)
    msg = g.should_block("tap", {"bbox": BOX})
    assert msg is not None and msg.startswith("BLOCKED")
    # Not exhausted one press earlier:
    g2 = _guard()
    for _ in range(BLOCK_AT - 2):
        _press(g2, BOX)
    assert g2.should_block("tap", {"bbox": BOX}) is None


def test_jittered_bbox_is_the_same_target() -> None:
    # The log's failure mode: tap → double_tap → nudged-coordinate tap on
    # one element. All of it is one target to the guard.
    g = _guard()
    _press(g, BOX, tool="tap")
    _press(g, NEAR_BOX, tool="double_tap")
    warning = _press(g, BOX, tool="long_press")
    assert warning is not None


def test_different_target_counts_separately() -> None:
    g = _guard()
    for _ in range(WARN_AT):
        _press(g, BOX)
    assert _press(g, FAR_BOX) is None
    assert g.should_block("tap", {"bbox": FAR_BOX}) is None


# ---------- resets & fail-open ----------


def test_screen_change_resets_the_target() -> None:
    # A qty stepper that actually increments must never MISS-trip or
    # block (the warn-only orbit advisory at press 5 is the designed
    # exception — it explicitly says "carry on if progressing").
    g = _guard()
    for _ in range(20):
        w = _press(g, BOX, changed=True)
        assert w is None or "same spot" in w
    assert g.should_block("tap", {"bbox": BOX}) is None
    # ...and a change after misses wipes the accumulated count.
    _press(g, BOX)
    _press(g, BOX)
    _press(g, BOX, changed=True)
    w = _press(g, BOX)
    assert w is None or "same spot" in w  # no miss warning: count restarted


def test_no_verdict_fails_open() -> None:
    # No verdict → no miss counting, no block. (The press still executed,
    # so the warn-only orbit advisory legitimately fires once.)
    g = _guard()
    for _ in range(20):
        w = _press(g, BOX, changed=None)
        assert w is None or "same spot" in w
    assert g.should_block("tap", {"bbox": BOX}) is None


def test_step_change_resets_all_targets() -> None:
    g = _guard()
    g.observe_step("update quantity to 4")
    for _ in range(BLOCK_AT - 1):
        _press(g, BOX)
    assert g.should_block("tap", {"bbox": BOX}) is not None
    g.observe_step("pay for the order")
    assert g.should_block("tap", {"bbox": BOX}) is None


def test_same_step_observation_keeps_counters() -> None:
    g = _guard()
    g.observe_step("step A")
    for _ in range(WARN_AT - 1):
        _press(g, BOX)
    g.observe_step("step A")
    assert _press(g, BOX) is not None  # warn fires — nothing was reset


# ---------- exemptions & non-press tools ----------


def test_swipes_never_counted() -> None:
    g = _guard()
    args = {"bbox": BOX, "direction": "up"}
    for _ in range(20):
        assert g.record("swipe", args, False) is None
    assert g.should_block("swipe", args) is None


@pytest.mark.parametrize("tool", ["peek", "screenshot", "note", "wait"])
def test_non_gesture_tools_ignored(tool: str) -> None:
    g = _guard()
    assert g.record(tool, {}, False) is None
    assert g.should_block(tool, {}) is None


def test_learned_keyboard_keys_exempt(mocker) -> None:
    backspace = [0.864, 0.804, 0.986, 0.856]
    mocker.patch.object(
        stuck.layout,
        "repeatable_key_boxes",
        return_value=[backspace],
    )
    g = StuckGuard()
    for _ in range(20):
        assert g.record("tap", {"bbox": backspace}, False) is None
    assert g.should_block("tap", {"bbox": backspace}) is None


# ---------- nav-gesture misses ----------


def test_nav_miss_warns_then_blocks() -> None:
    # The silent go_back miss: edge swipe never registers, screen never
    # changes. Same tiers as a dead press target, keyed per tool name.
    g = _guard()
    warnings = [g.record("go_back", {}, False) for _ in range(WARN_AT)]
    assert warnings[:-1] == [None, None]
    assert "go_back" in warnings[-1]
    assert "CONVENTION § Stuck" in warnings[-1]

    for _ in range(BLOCK_AT - 1 - WARN_AT):
        g.record("go_back", {}, False)
    msg = g.should_block("go_back", {})
    assert msg is not None and msg.startswith("BLOCKED")
    # Not exhausted one miss earlier:
    g2 = _guard()
    for _ in range(BLOCK_AT - 2):
        g2.record("go_back", {}, False)
    assert g2.should_block("go_back", {}) is None


def test_nav_pop_success_resets_the_count() -> None:
    g = _guard()
    for _ in range(BLOCK_AT - 1):
        g.record("go_back", {}, False)
    g.record("go_back", {}, True)  # a pop finally landed
    assert g.should_block("go_back", {}) is None


def test_nav_no_verdict_fails_open() -> None:
    g = _guard()
    for _ in range(20):
        assert g.record("go_back", {}, None) is None
    assert g.should_block("go_back", {}) is None


def test_nav_tools_counted_separately() -> None:
    g = _guard()
    for _ in range(BLOCK_AT - 1):
        g.record("go_back", {}, False)
    assert g.should_block("go_back", {}) is not None
    assert g.should_block("home_screen", {}) is None


def test_nav_miss_step_change_resets() -> None:
    g = _guard()
    g.observe_step("step A")
    for _ in range(BLOCK_AT - 1):
        g.record("go_back", {}, False)
    assert g.should_block("go_back", {}) is not None
    g.observe_step("step B")
    assert g.should_block("go_back", {}) is None


def test_malformed_bbox_ignored() -> None:
    g = _guard()
    assert g.record("tap", {"bbox": ["x", 1, 2]}, False) is None
    assert g.record("tap", {}, False) is None
    assert g.should_block("tap", {"bbox": None}) is None


# ---------- sequence steps ----------


def test_sequence_cannot_hide_a_target_exhausted_by_standalone_presses() -> None:
    g = _guard()
    args = {
        "actions": [
            {"tool_name": "tap", "arg": BOX},
            {"tool_name": "send_to_clipboard", "arg": "hello"},
        ]
    }
    [_press(g, BOX) for _ in range(BLOCK_AT - 1)]

    blocked = g.should_block("sequence", args)

    # The exhausted target blocks the whole batch that includes it...
    assert blocked is not None
    # ...and blocks the bare press too.
    assert g.should_block("tap", {"bbox": NEAR_BOX}) is not None


def test_sequence_with_clean_targets_not_blocked() -> None:
    g = _guard()
    args = {"actions": [{"tool_name": "tap", "arg": FAR_BOX}]}
    assert g.should_block("sequence", args) is None


def test_sequence_malformed_actions_ignored() -> None:
    g = _guard()
    assert g.record("sequence", {"actions": "not-a-list"}, False) is None
    assert g.record("sequence", {}, False) is None


# ---------- ping-pong ----------


BOX_A = [0.100, 0.100, 0.200, 0.200]
BOX_B = [0.700, 0.700, 0.800, 0.800]


def _pp(guard: StuckGuard, bbox, *, tool: str = "tap") -> None:
    """One executed, screen-CHANGING press — the verdict that same-target
    counting ignores but ping-pong must still see."""
    guard.record(tool, {"bbox": bbox}, True)


def test_pingpong_warns_then_blocks() -> None:
    g = _guard()
    for _ in range(WARN_AT - 1):
        _pp(g, BOX_A)
        _pp(g, BOX_B)
    _pp(g, BOX_A)
    # The WARN_AT-th pair completes on this press:
    warning = g.record("tap", {"bbox": BOX_B}, True)
    assert warning is not None and "repeated the same action cycle" in warning

    for _ in range(BLOCK_AT - WARN_AT - 1):
        _pp(g, BOX_A)
        _pp(g, BOX_B)
    _pp(g, BOX_A)
    # Continuing the alternation would be the BLOCK_AT-th pair:
    msg = g.should_block("tap", {"bbox": BOX_B})
    assert msg is not None and msg.startswith("BLOCKED")
    assert "action cycle" in msg


def test_pingpong_fires_despite_changed_verdicts() -> None:
    # The whole point: screen-changing loops evade same-target counting.
    g = _guard()
    for _ in range(BLOCK_AT):
        _pp(g, BOX_A)
        _pp(g, BOX_B)
    assert g.should_block("tap", {"bbox": BOX_A}) is not None
    # ...but a genuinely third target breaks the pattern and is free —
    # same-target never tripped either (all verdicts were `changed`):
    third = [0.400, 0.400, 0.500, 0.500]
    assert g.should_block("tap", {"bbox": third}) is None


def test_pingpong_nav_and_press_alternation() -> None:
    # Real-log shape: tap product row ↔ go_back, forever.
    g = _guard()
    for _ in range(BLOCK_AT):
        _pp(g, BOX_A)
        g.record("go_back", {}, True)
    assert g.should_block("tap", {"bbox": BOX_A}) is not None


def test_pingpong_browsing_pattern_not_flagged() -> None:
    # tap(item1) → back → tap(item2) → back → … : the press targets
    # differ each round, so no two-signature alternation forms.
    g = _guard()
    for i in range(10):
        bbox = [0.1, 0.05 * i, 0.2, 0.05 * i + 0.05]
        _pp(g, bbox)
        g.record("go_back", {}, True)
    assert g.should_block("go_back", {}) is None


def test_pingpong_same_action_repetition_not_flagged() -> None:
    # Feed scrolling: swipe up ×10 is A,A,A… — not an alternation.
    g = _guard()
    for _ in range(10):
        g.record("swipe", {"bbox": BOX_A, "direction": "up"}, True)
    assert g.should_block("swipe", {"bbox": BOX_A, "direction": "up"}) is None


def test_pingpong_swipe_direction_hunting_flagged() -> None:
    # up/down/up/down scroll hunting is the swipe-shaped loop.
    g = _guard()
    for _ in range(BLOCK_AT):
        g.record("swipe", {"bbox": BOX_A, "direction": "up"}, True)
        g.record("swipe", {"bbox": BOX_A, "direction": "down"}, True)
    assert g.should_block("swipe", {"bbox": BOX_A, "direction": "up"}) is not None


def test_pingpong_broken_by_a_third_action() -> None:
    g = _guard()
    for _ in range(BLOCK_AT):
        _pp(g, BOX_A)
        _pp(g, BOX_B)
    _pp(g, FAR_BOX)  # C breaks the A,B tail
    assert g.should_block("tap", {"bbox": BOX_A}) is None


def test_pingpong_step_change_resets_history() -> None:
    g = _guard()
    g.observe_step("step A")
    for _ in range(BLOCK_AT):
        _pp(g, BOX_A)
        _pp(g, BOX_B)
    g.observe_step("step B")
    assert g.should_block("tap", {"bbox": BOX_A}) is None


def test_pingpong_exempt_keyboard_keys_never_tracked(mocker) -> None:
    # Typing "ababab…" on two keyboard keys must not read as ping-pong.
    key_a = [0.100, 0.800, 0.160, 0.860]
    key_b = [0.300, 0.800, 0.360, 0.860]
    mocker.patch.object(
        stuck.layout,
        "repeatable_key_boxes",
        return_value=[key_a, key_b],
    )
    g = StuckGuard()
    for _ in range(10):
        g.record("tap", {"bbox": key_a}, True)
        g.record("tap", {"bbox": key_b}, True)
    assert g.should_block("tap", {"bbox": key_a}) is None


def test_pingpong_views_do_not_break_pattern() -> None:
    # A peek between the alternating actions has no signature — the
    # pattern continues through it.
    g = _guard()
    for _ in range(BLOCK_AT):
        _pp(g, BOX_A)
        g.record("peek", {}, None)
        _pp(g, BOX_B)
    assert g.should_block("tap", {"bbox": BOX_A}) is not None


def test_pingpong_history_bounded() -> None:
    g = _guard()
    for i in range(100):
        bbox = [0.01 * (i % 40), 0.5, 0.01 * (i % 40) + 0.05, 0.6]
        _pp(g, bbox)
    assert len(g._history) <= stuck._HISTORY_MAX


# ---------- period-3 cycles & error loops ----------


def test_period3_cycle_flagged() -> None:
    # cart → product row → checkout → cart → … (the log's nav loop).
    g = _guard()
    box_c = [0.400, 0.400, 0.500, 0.500]
    for _ in range(BLOCK_AT):
        _pp(g, BOX_A)
        _pp(g, BOX_B)
        _pp(g, box_c)
    assert g.should_block("tap", {"bbox": BOX_A}) is not None


def test_period3_distinct_content_not_flagged() -> None:
    # search → add → back with DIFFERENT items each round is a legit
    # workflow — differing tap targets break the cycle identity.
    g = _guard()
    for i in range(8):
        item = [0.1, 0.06 * i, 0.2, 0.06 * i + 0.05]
        _pp(g, item)
        _pp(g, BOX_B)  # "add to cart" button, same every round
        g.record("go_back", {}, True)
    assert g.should_block("go_back", {}) is None


def test_error_repeat_warns_then_blocks() -> None:
    # The AssistiveTouch-overlap trap: identical call raises every time.
    g = _guard()
    warnings = [g.record_error("tap", {"bbox": BOX}) for _ in range(WARN_AT)]
    assert warnings[:-1] == [None, None]
    assert "failed" in warnings[-1]

    for _ in range(BLOCK_AT - 1 - WARN_AT):
        g.record_error("tap", {"bbox": BOX})
    msg = g.should_block("tap", {"bbox": BOX})
    assert msg is not None and "failed" in msg


def test_error_count_cleared_by_success() -> None:
    g = _guard()
    for _ in range(BLOCK_AT - 1):
        g.record_error("tap", {"bbox": BOX})
    _press(g, BOX, changed=True)  # the call finally worked
    assert g.should_block("tap", {"bbox": BOX}) is None


def test_error_repeat_different_args_counted_separately() -> None:
    g = _guard()
    for _ in range(10):
        g.record_error("tap", {"bbox": BOX})
    assert g.should_block("tap", {"bbox": FAR_BOX}) is None


def test_pressless_sequences_with_different_actions_not_conflated() -> None:
    # Two DIFFERENT all-swipe sequences must not share the (SEQUENCE, None)
    # signature — else a corrective's instructed method change gets
    # pre-blocked as "this exact call already failed".
    up = {"actions": [{"tool_name": "swipe", "arg": {"bbox": BOX, "direction": "up"}}]}
    down = {
        "actions": [{"tool_name": "swipe", "arg": {"bbox": BOX, "direction": "down"}}]
    }
    g = _guard()
    for _ in range(BLOCK_AT - 1):
        g.record_error("sequence", up)
    # The distinct batch is a different call — not blocked.
    assert g.should_block("sequence", down) is None
    # The identical batch still self-matches and blocks.
    assert g.should_block("sequence", up) is not None


def test_error_repeat_step_change_resets() -> None:
    g = _guard()
    g.observe_step("step A")
    for _ in range(BLOCK_AT):
        g.record_error("tap", {"bbox": BOX})
    g.observe_step("step B")
    assert g.should_block("tap", {"bbox": BOX}) is None


# ---------- position orbit (detector 4, warn-only) ----------


def test_orbit_warns_on_fifth_same_pos_press_mixed_everything() -> None:
    # 5 of the last 10 presses on one spot — across gesture types,
    # verdicts, errors, and interleaved other targets — must warn.
    g = _guard()
    g.record("tap", {"bbox": BOX}, True)  # 1: changed
    g.record("tap", {"bbox": FAR_BOX}, True)  # other spot
    g.record("double_tap", {"bbox": BOX}, False)  # 2: no change
    g.record_error("long_press", {"bbox": BOX})  # 3: raised
    g.record("tap", {"bbox": FAR_BOX}, True)  # other spot
    g.record("tap", {"bbox": BOX}, True)  # 4: changed
    warning = g.record("tap", {"bbox": NEAR_BOX}, True)  # 5: jittered

    assert warning is not None and "same spot" in warning
    # Advisory only — nothing blocks:
    assert g.should_block("tap", {"bbox": BOX}) is None


def test_orbit_warns_once_per_position() -> None:
    g = _guard()
    for _ in range(4):
        g.record("tap", {"bbox": BOX}, True)
    first = g.record("tap", {"bbox": BOX}, True)
    later = [g.record("tap", {"bbox": BOX}, True) for _ in range(4)]

    assert first is not None and "same spot" in first
    assert all(w is None or "same spot" not in w for w in later)


def test_orbit_silent_below_threshold() -> None:
    g = _guard()
    for _ in range(4):
        assert g.record("tap", {"bbox": BOX}, True) is None


def test_orbit_window_slides() -> None:
    # 4 presses on BOX, then 10 elsewhere pushes them out of the window —
    # the next BOX press is 1-of-10, not 5-of-anything.
    g = _guard()
    for _ in range(4):
        g.record("tap", {"bbox": BOX}, True)
    for i in range(10):
        other = [0.05, 0.05 * i + 0.01, 0.10, 0.05 * i + 0.05]
        g.record("tap", {"bbox": other}, True)
    assert g.record("tap", {"bbox": BOX}, True) is None


def test_orbit_step_change_resets() -> None:
    g = _guard()
    g.observe_step("a")
    for _ in range(4):
        g.record("tap", {"bbox": BOX}, True)
    g.observe_step("b")
    assert g.record("tap", {"bbox": BOX}, True) is None


def test_orbit_exempt_keys_never_tracked(mocker) -> None:
    # Typing runs — backspace, space, return — must never enter the
    # orbit window, whatever the mix of keys and verdicts.
    backspace = [0.864, 0.804, 0.986, 0.856]
    ret = [0.749, 0.864, 0.987, 0.916]
    space = [0.256, 0.865, 0.742, 0.916]
    mocker.patch.object(
        stuck.layout,
        "repeatable_key_boxes",
        return_value=[backspace, ret, space],
    )
    g = StuckGuard()
    for _ in range(10):
        assert g.record("tap", {"bbox": backspace}, True) is None
        assert g.record("tap", {"bbox": space}, False) is None
        assert g.record_error("tap", {"bbox": ret}) is None
    assert g._presses == []  # nothing entered the window at all


# ---------- batch verdict attribution ----------


def test_batch_changed_verdict_does_not_reset_target() -> None:
    # A sequence's "changed" can come from ANY step (a keyboard rising),
    # so it must not launder evidence built against one press target —
    # a paste-flow loop survives on exactly this laundering.
    g = _guard()
    for _ in range(BLOCK_AT - 1):
        _press(g, BOX)  # camera-verified no-ops
    g.record("sequence", {"actions": [{"tool_name": "long_press", "arg": BOX}]}, True)

    assert g.should_block("tap", {"bbox": BOX}) is not None


def test_batch_changed_verdict_does_not_count_misses_either() -> None:
    g = _guard()
    args = {"actions": [{"tool_name": "long_press", "arg": BOX}]}
    for _ in range(BLOCK_AT + 1):
        g.record("sequence", args, True)  # unattributable — no evidence

    assert g.should_block("tap", {"bbox": BOX}) is None


def test_standalone_changed_press_still_resets_target() -> None:
    g = _guard()
    for _ in range(BLOCK_AT - 1):
        _press(g, BOX)
    _press(g, BOX, changed=True)  # single press owns its verdict

    assert g.should_block("tap", {"bbox": BOX}) is None


# ---------- sequence identity and evidence regressions ----------


@pytest.fixture
def public_guard(mocker) -> StuckGuard:
    mocker.patch.object(stuck.layout, "repeatable_key_boxes", return_value=[])
    return StuckGuard()


def _batch(*steps: tuple[str, object]) -> dict:
    return {"actions": [{"tool_name": name, "arg": arg} for name, arg in steps]}


@pytest.mark.parametrize(
    ("old_step", "new_step"),
    [
        (("tap", BOX_A), ("tap", BOX_B)),
        (("tap", BOX_A), ("long_press", BOX_A)),
        (("send_to_clipboard", "old"), ("send_to_clipboard", "corrected")),
        (
            ("swipe", {"direction": "up", "bbox": BOX_A}),
            ("swipe", {"direction": "down", "bbox": BOX_A}),
        ),
    ],
)
def test_regression_sequence_error_allows_corrected_later_step(
    public_guard, old_step, new_step
) -> None:
    old = _batch(("tap", BOX), old_step)
    corrected = _batch(("tap", BOX), new_step)
    [public_guard.record_error("sequence", old) for _ in range(BLOCK_AT - 1)]

    retry = public_guard.should_block("sequence", old)
    correction = public_guard.should_block("sequence", corrected)

    assert retry is not None and "failed" in retry
    assert correction is None


def test_sequence_errors_distinguish_action_order(public_guard) -> None:
    old = _batch(("tap", BOX), ("tap", BOX_A), ("tap", BOX_B))
    reordered = _batch(("tap", BOX), ("tap", BOX_B), ("tap", BOX_A))
    [public_guard.record_error("sequence", old) for _ in range(BLOCK_AT - 1)]

    blocked = public_guard.should_block("sequence", reordered)

    assert blocked is None


@pytest.mark.parametrize("changed", [False, True, None])
def test_regression_batch_verdict_never_exhausts_individual_targets(
    public_guard, changed
) -> None:
    # Two taps can toggle a control on then off: the batch's final screen
    # matches its starting frame, even though both presses worked.
    args = _batch(("tap", BOX), ("tap", BOX))
    [public_guard.record("sequence", args, changed) for _ in range(BLOCK_AT)]

    blocked = public_guard.should_block("tap", {"bbox": BOX})

    assert blocked is None


@pytest.mark.parametrize("changed", [False, True, None])
def test_batch_verdict_preserves_existing_standalone_misses(
    public_guard, changed
) -> None:
    [_press(public_guard, BOX) for _ in range(BLOCK_AT - 2)]
    public_guard.record("sequence", _batch(("tap", BOX), ("tap", BOX_A)), changed)

    before = public_guard.should_block("tap", {"bbox": BOX})
    _press(public_guard, BOX)
    after = public_guard.should_block("tap", {"bbox": BOX})

    assert before is None
    assert after is not None and "camera-verified" in after


_BACKSPACE = [0.864, 0.804, 0.986, 0.856]
_SPACE = [0.256, 0.865, 0.742, 0.916]


def _feed_calls(guard, calls, *, failed=False, changed=True):
    """Exercise the public pre-dispatch/record lifecycle in order."""
    results = []
    for name, arguments in calls:
        blocked = guard.should_block(name, arguments)
        warning = None
        if blocked is None:
            warning = (
                guard.record_error(name, arguments)
                if failed
                else guard.record(name, arguments, changed)
            )
        results.append((blocked, warning))
    return results


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("changed", [False, True, None])
def test_regression_repeatable_key_batches_are_exempt_everywhere(
    mocker, failed, changed
):
    mocker.patch.object(
        stuck.layout, "repeatable_key_boxes", return_value=[_BACKSPACE, _SPACE]
    )
    guard = StuckGuard()
    batches = [
        _batch(("tap", _BACKSPACE), ("tap", _SPACE)),
        _batch(("tap", _SPACE), ("tap", _BACKSPACE)),
    ]
    calls = [("sequence", args) for args in batches * BLOCK_AT]

    results = _feed_calls(guard, calls, failed=failed, changed=changed)

    assert results == [(None, None)] * len(calls)


def test_mixed_key_batches_cannot_hide_repeated_nonexempt_errors(mocker):
    mocker.patch.object(
        stuck.layout, "repeatable_key_boxes", return_value=[_BACKSPACE, _SPACE]
    )
    guard = StuckGuard()
    old = _batch(("tap", _BACKSPACE), ("tap", BOX_A))
    same_target = _batch(("tap", _SPACE), ("tap", BOX_A))
    [guard.record_error("sequence", old) for _ in range(BLOCK_AT - 1)]

    blocked = guard.should_block("sequence", same_target)

    assert blocked is not None and "failed" in blocked


def test_sequence_cycles_detect_different_tails_with_shared_first_target(public_guard):
    batches = [
        _batch(("tap", BOX), ("tap", BOX_A)),
        _batch(("tap", BOX), ("tap", BOX_B)),
    ]
    calls = [("sequence", args) for args in batches * BLOCK_AT]

    results = _feed_calls(public_guard, calls)

    assert [blocked for blocked, _ in results[:-1]] == [None] * (len(calls) - 1)
    assert results[-1][0] is not None and "action cycle" in results[-1][0]
    assert any(warning and "action cycle" in warning for _, warning in results)


def test_sequence_error_identity_keeps_press_jitter_tolerance(public_guard):
    args = _batch(("long_press", BOX), ("tap", BOX_A))
    jittered = _batch(("long_press", NEAR_BOX), ("tap", BOX_A))
    [public_guard.record_error("sequence", args) for _ in range(BLOCK_AT - 1)]

    blocked = public_guard.should_block("sequence", jittered)

    assert blocked is not None and "failed" in blocked


def test_sequence_skips_unusable_entries_without_losing_valid_presses(public_guard):
    args = {
        "actions": [
            None,
            {"tool_name": "tap", "arg": []},
            {"tool_name": "tap", "arg": BOX_A},
        ]
    }
    [public_guard.record_error("sequence", args) for _ in range(BLOCK_AT - 1)]

    blocked = public_guard.should_block("sequence", args)

    assert blocked is not None and "failed" in blocked


@pytest.mark.parametrize(
    "other_box",
    [[0.7, 0.1, 0.8, 0.2], [0.1, 0.7, 0.2, 0.8]],
)
def test_error_identity_distinguishes_each_coordinate_axis(public_guard, other_box):
    [public_guard.record_error("tap", {"bbox": BOX_A}) for _ in range(BLOCK_AT - 1)]

    blocked = public_guard.should_block("tap", {"bbox": other_box})

    assert blocked is None


def test_sequence_swipe_errors_distinguish_regions_with_the_same_direction(
    public_guard,
):
    fixed = ("swipe", {"bbox": BOX, "direction": "down"})
    old = _batch(fixed, ("swipe", {"bbox": BOX_A, "direction": "up"}))
    corrected = _batch(fixed, ("swipe", {"bbox": BOX_B, "direction": "up"}))
    [public_guard.record_error("sequence", old) for _ in range(BLOCK_AT - 1)]

    blocked = public_guard.should_block("sequence", corrected)

    assert blocked is None


def test_sequence_checks_exhausted_targets_after_nonpress_steps(public_guard):
    [_press(public_guard, BOX_A) for _ in range(BLOCK_AT - 1)]
    args = _batch(("send_to_clipboard", "message"), ("tap", BOX_A))

    blocked = public_guard.should_block("sequence", args)

    assert blocked is not None and f"press #{BLOCK_AT}" in blocked


def test_sequence_skips_nondict_steps_before_an_exhausted_target(public_guard):
    [_press(public_guard, BOX_A) for _ in range(BLOCK_AT - 1)]
    args = {"actions": [None, {"tool_name": "tap", "arg": BOX_A}]}

    blocked = public_guard.should_block("sequence", args)

    assert blocked is not None and "camera-verified" in blocked


@pytest.mark.parametrize("changed", [True, False, None])
def test_sequence_tracks_orbit_for_later_presses(public_guard, changed):
    [_press(public_guard, BOX_B, changed=True) for _ in range(BLOCK_AT - 1)]

    warning = public_guard.record(
        "sequence", _batch(("tap", BOX_A), ("tap", BOX_B)), changed
    )

    assert warning is not None and f"{BLOCK_AT} of your last" in warning


def test_error_repeat_allows_attempts_before_block_and_reports_actual_failures(
    public_guard,
):
    calls = [("tap", {"bbox": BOX_A})] * BLOCK_AT

    results = _feed_calls(public_guard, calls, failed=True)

    assert [blocked for blocked, _ in results[:-1]] == [None] * (BLOCK_AT - 1)
    assert f"failed {BLOCK_AT - 1}×" in results[-1][0]


def test_nav_block_reports_the_attempt_that_would_execute(public_guard):
    [public_guard.record("go_back", {}, False) for _ in range(BLOCK_AT - 1)]

    blocked = public_guard.should_block("go_back", {})

    assert f"`go_back` #{BLOCK_AT}" in blocked


@pytest.mark.parametrize("bad_name", [[], {}, ["tap"]])
def test_regression_malformed_sequence_tool_names_do_not_crash_guard(
    public_guard, bad_name
):
    args = _batch((bad_name, BOX_A), ("tap", BOX_B))

    results = _feed_calls(public_guard, [("sequence", args)])

    assert results == [(None, None)]


@pytest.mark.parametrize("bad_box", [["nan", 0, 1, 1], ["Infinity", 0, 1, 1]])
def test_regression_nonfinite_string_sequence_boxes_do_not_crash_guard(
    public_guard, bad_box
):
    args = _batch(("tap", bad_box), ("tap", BOX_B))

    results = _feed_calls(public_guard, [("sequence", args)])

    assert results == [(None, None)]


def test_regression_sequence_swipe_identity_preserves_equivalent_numeric_boxes(
    public_guard,
):
    old = _batch(
        ("swipe", {"bbox": [0, 0, 1, 1], "direction": "up"}),
        ("swipe", {"bbox": BOX_A, "direction": "down"}),
    )
    same = _batch(
        ("swipe", {"bbox": [0.0, 0.0, 1.0, 1.0], "direction": "up"}),
        ("swipe", {"bbox": BOX_A, "direction": "down"}),
    )
    [public_guard.record_error("sequence", old) for _ in range(BLOCK_AT - 1)]

    blocked = public_guard.should_block("sequence", same)

    assert blocked is not None and "failed" in blocked


@pytest.mark.parametrize(
    ("original_box", "jittered_box"),
    [
        ([0.1, 0.1, 0.2, 0.2], [0.101, 0.101, 0.201, 0.201]),
        ([0, 0, 1, 1], [0.001, 0.001, 0.999, 0.999]),
    ],
)
def test_sequence_swipe_error_identity_preserves_coordinate_jitter(
    public_guard, original_box, jittered_box
):
    fixed = ("tap", BOX_B)
    old = _batch(("swipe", {"bbox": original_box, "direction": "up"}), fixed)
    same = _batch(("swipe", {"bbox": jittered_box, "direction": "up"}), fixed)
    [public_guard.record_error("sequence", old) for _ in range(BLOCK_AT - 1)]

    blocked = public_guard.should_block("sequence", same)

    assert blocked is not None and "failed" in blocked


@pytest.mark.parametrize("invalid_coord", [1e308, 10**400])
def test_regression_oversized_sequence_swipe_coordinate_does_not_crash_guard(
    public_guard, invalid_coord
):
    args = _batch(
        ("swipe", {"bbox": [0, 0, invalid_coord, 1], "direction": "up"}), ("tap", BOX_A)
    )

    results = _feed_calls(public_guard, [("sequence", args)])

    assert results == [(None, None)]


@given(
    tails=st.lists(
        st.text(min_size=1, max_size=20), min_size=2, max_size=5, unique=True
    )
)
def test_sequence_error_identity_preserves_distinct_clipboard_content(tails):
    guard = _guard()
    original = _batch(("tap", BOX), ("send_to_clipboard", tails[0]))
    [guard.record_error("sequence", original) for _ in range(BLOCK_AT - 1)]

    blocks = [
        guard.should_block(
            "sequence", _batch(("tap", BOX), ("send_to_clipboard", tail))
        )
        for tail in tails[1:]
    ]

    assert blocks == [None] * (len(tails) - 1)
