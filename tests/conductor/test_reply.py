"""Tests for `physiclaw.conductor.spec.reply` — the gate's deterministic
reading: whole-message matching against the ask's own words, and
new-incoming-bubble detection."""

from __future__ import annotations

import pytest
from conductor_fakes import make_screen

from physiclaw.conductor.spec import reply

YES = frozenset(map(reply.normalize, ["好的", "嗯", "ok", "go ahead", "confirm"]))
NO = frozenset(map(reply.normalize, ["不用", "不要", "算了", "no thanks", "cancel"]))


def _new(rows, baseline, own, **kw):
    return reply.new_incoming(rows, baseline, own, **kw)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("好的", "confirm"),
        ("好的！", "confirm"),  # trailing punctuation stripped
        ("OK", "confirm"),  # casefold
        ("ｏｋ", "confirm"),  # full-width folds via NFKC
        ("go ahead", "confirm"),  # inner whitespace removed on both sides
        ("不用", "deny"),
        ("算了。", "deny"),
        ("No thanks", "deny"),
        ("等等", None),  # a hold is neither — undeclared
        ("好的，但是买两盒", None),  # qualifier → whole-message rule defers
        ("what's the price?", None),
    ],
)
def test_classify_whole_message_only(text: str, expected: str | None) -> None:
    assert reply.classify(text, YES, NO) == expected


def test_classify_all_follows_the_newest_message() -> None:
    assert reply.classify_all(["好的", "不要"], YES, NO) == "deny"
    assert reply.classify_all(["不要", "好的"], YES, NO) == "confirm"  # a changed mind
    assert reply.classify_all(["顺便查下天气", "嗯"], YES, NO) == "confirm"
    # An undeclared newest message defers — the model reads the thread.
    assert reply.classify_all(["好的", "顺便查下天气"], YES, NO) is None
    assert reply.classify_all([], YES, NO) is None


def test_only_the_declared_words_count() -> None:
    # The conductor holds no word list of its own: an undeclared "yes"
    # spelling is unclassified, a declared one classifies.
    assert reply.classify("sure", YES, NO) is None
    assert reply.classify("sure", frozenset({"sure"}), NO) == "confirm"


def test_new_incoming_filters_side_baseline_and_own_ask() -> None:
    ask = "「买牛奶」已到付款页，合计 ¥45。回复 好的 确认支付，或 不用 取消。"
    screen = make_screen(
        ("MyChat", 0.5, 0.05),  # header (baseline)
        (ask[:20], 0.75, 0.3),  # our ask bubble, wrapped line (right side)
        ("好的", 0.25, 0.5),  # the NEW user reply (left side)
        ("已发送", 0.75, 0.6),  # our own new bubble — right side, excluded
    )

    new = _new(screen.rows, {"MyChat"}, ask)

    assert new == ["好的"]


def test_quoted_reply_words_are_never_swallowed_as_own_lines() -> None:
    # The ask quotes "confirm"/"cancel" (≥ _OWN_FRAGMENT_MIN chars); a
    # verbatim reply must still register — a swallowed yes reads as
    # silence and the gate suspend-loops past explicit consent.
    ask = 'Total ¥45. Reply "confirm" to pay, or "cancel" to stop.'
    screen = make_screen(
        ("MyChat", 0.5, 0.05),
        (ask[:20], 0.75, 0.3),
        ("confirm", 0.25, 0.5),
    )

    assert _new(screen.rows, {"MyChat"}, ask) == ["confirm"]


def test_bubbles_above_the_visible_ask_never_count() -> None:
    # A keyboard hides bubbles without touching the thread's anchors, so
    # the baseline can miss old history; when it dismisses, a stale "ok"
    # from another conversation resurfaces as "not in baseline". A real
    # reply is chronologically after the ask — below it on screen.
    ask = 'Total ¥45. Reply "ok" to pay, or "no" to cancel.'
    screen = make_screen(
        ("MyChat", 0.5, 0.05),
        ("ok", 0.25, 0.2),  # STALE — an old confirm above the ask
        (ask[:20], 0.75, 0.5),  # our ask bubble
        ("ok", 0.25, 0.8),  # the real reply, below the ask
    )

    new = _new(screen.rows, {"MyChat"}, ask)

    assert len(new) == 1  # only the reply below the ask

    # The re-ask deny sweep runs with the filter OFF — it must see what
    # arrived above a just-sent ask.
    swept = _new(screen.rows, {"MyChat"}, ask, after_ask=False)
    assert len(swept) == 2


def test_centered_timestamp_rows_are_not_incoming() -> None:
    # System rows (timestamps) sit centered ~0.5 — outside the incoming
    # band, or every fresh timestamp after a suspension burns an LLM check.
    screen = make_screen(
        ("昨天 14:32", 0.5, 0.2),
        ("好的", 0.25, 0.4),
    )

    assert _new(screen.rows, set(), "ask text") == ["好的"]


def test_reply_repeating_a_visible_word_below_the_ask_counts() -> None:
    # The user's earlier "好的" (to a previous ask) is still on screen and
    # in the baseline; their new "好的" below this ask is a reply all the
    # same — position, not the label set, decides below a visible ask.
    ask = "现在下单吗？回复 好的 或 不用"
    screen = make_screen(
        ("MyChat", 0.5, 0.05),
        ("好的", 0.25, 0.2),  # the old reply, above
        (ask, 0.75, 0.4),
        ("好的", 0.25, 0.6),  # the new reply, below
    )

    assert _new(screen.rows, {"MyChat", "好的"}, ask) == ["好的"]


def test_user_echo_of_the_ask_counts() -> None:
    # "好的 确认支付" is a substring of the ask; it is still the user's
    # bubble (left side, below the ask), never one of our own lines.
    ask = "合计 ¥45。回复 好的 确认支付，或 不用 取消。"
    screen = make_screen(
        ("MyChat", 0.5, 0.05), (ask, 0.75, 0.3), ("好的 确认支付", 0.25, 0.5)
    )

    assert _new(screen.rows, {"MyChat"}, ask) == ["好的 确认支付"]


def test_sweep_skips_the_just_sent_asks_own_band() -> None:
    # A short wrapped tail of OUR new ask can OCR left of center; the
    # deny sweep reads above the ask and must not read the ask itself.
    ask = "现在下单吗？回复 好的 或 不用"
    screen = make_screen(
        ("MyChat", 0.5, 0.05),
        ("cancel", 0.25, 0.2),  # sent while the walk was in the app
        (ask[:8], 0.75, 0.5),
        ("不用", 0.3, 0.52),  # the ask's own last line, left-aligned
    )

    assert _new(screen.rows, {"MyChat"}, ask, after_ask=False) == ["cancel"]


def test_a_raised_keyboard_is_not_the_thread() -> None:
    # A send leaves the keyboard up: its keys and the predictive bar
    # above them sit left of center below the ask, exactly where a reply
    # would — recognized by shape, they never read as messages.
    ask = "已为您选好太古小粒优级黄冰糖454g，实付13.68元。回复 好的 确认支付，或 不用 取消。"
    keys = [(k, 0.05 + i * 0.1, 0.71) for i, k in enumerate("QWERTYUIOP")]
    screen = make_screen(
        ("QiaoQian", 0.5, 0.08),
        (ask[:22], 0.6, 0.38),
        ("好的", 0.2, 0.53),  # the reply
        ("I", 0.15, 0.656),
        ("The", 0.5, 0.656),
        ("I'm", 0.83, 0.656),  # predictive bar
        *keys,
        ("123", 0.12, 0.885),
        ("space", 0.5, 0.885),
    )

    # The floor hangs off the key ROW, one line above its top edge and
    # the input bar: the predictive bar's lone "I" is not a row.
    assert reply.keyboard_top(screen.rows) == pytest.approx(0.59)
    assert _new(screen.rows, {"QiaoQian"}, ask) == ["好的"]
    assert (
        reply.classify_all(_new(screen.rows, {"QiaoQian"}, ask), YES, NO) == "confirm"
    )


def test_a_bubbles_rows_are_one_message() -> None:
    # OCR reads a two-line bubble as two rows a line apart; the whole
    # message carries the qualifier, so it must not confirm off its last
    # line — while two bubbles, further apart, stay two messages.
    ask = "现在下单吗？回复 好的 或 不用"
    screen = make_screen(
        (ask, 0.75, 0.3),
        ("买两袋，", 0.25, 0.500),
        ("好的", 0.2, 0.523),  # the same bubble's second line
    )

    new = _new(screen.rows, set(), ask)
    assert new == ["买两袋， 好的"]
    assert reply.classify_all(new, YES, NO) is None

    two = make_screen((ask, 0.75, 0.3), ("不用", 0.2, 0.5), ("好的", 0.2, 0.56))
    assert _new(two.rows, set(), ask) == ["不用", "好的"]


def test_no_keyboard_without_a_spread_of_keys() -> None:
    # Two lone letters are no keyboard; they read as bubbles like any
    # other, in screen order whatever order the listing had — and at
    # one height they are one line, so one message.
    screen = make_screen(("A", 0.2, 0.9), ("B", 0.4, 0.9), ("好的", 0.2, 0.5))

    assert reply.keyboard_top(screen.rows) is None
    assert _new(screen.rows, set(), "ask") == ["好的", "A B"]


def test_the_asks_own_wrapped_tail_is_not_a_reply() -> None:
    # The ask's last line wraps short and OCRs left of center just under
    # its recognized lines; spacing differs from the sent text (OCR drops
    # it). It is ours — without it the round would hand over before the
    # user has said anything.
    ask = "实付13.68元。回复 好的 确认支付，或 不用 取消。"
    rows = [
        ("QiaoQian", 0.5, 0.08),
        (ask[:12], 0.6, 0.40),
        ("付，或不用取消。", 0.3, 0.43),  # the wrapped tail, one line under
    ]

    assert _new(make_screen(*rows).rows, {"QiaoQian"}, ask) == []
    assert _new(make_screen(*rows, ("好的", 0.2, 0.53)).rows, {"QiaoQian"}, ask) == [
        "好的"
    ]

    # A tail as short as the ask's own no word is still the ask's.
    short = make_screen(*rows[:2], ("取消。", 0.3, 0.43))
    assert _new(short.rows, {"QiaoQian"}, ask) == []


def test_any_deny_is_the_sweeps_rule() -> None:
    # A deny said while the walk was in the app stops it whatever
    # followed — the sweep does not follow the newest message.
    assert reply.any_deny(["不要", "顺便查下天气"], YES, NO) is True
    assert reply.any_deny(["好的", "嗯"], YES, NO) is False
    assert reply.any_deny([], YES, NO) is False
