"""Tests for `physiclaw.conductor.spec.reply` — the gate's deterministic
reading: whole-message matching against the ask's own words, and
new-incoming-bubble detection."""

from __future__ import annotations

import pytest
from conductor_fakes import make_screen

from physiclaw.conductor.spec import reply
from physiclaw.conductor.spec.reply import INCOMING_LEFT

YES = frozenset(map(reply.normalize, ["好的", "嗯", "ok", "go ahead", "confirm"]))
NO = frozenset(map(reply.normalize, ["不用", "不要", "算了", "no thanks", "cancel"]))


LEFT = INCOMING_LEFT  # every IM we know: the user's bubbles on the left


def _new(rows, baseline, own, **kw):
    kw.setdefault("incoming", LEFT)
    return reply.read_incoming(rows, baseline, own, **kw)[0]


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


@pytest.mark.parametrize("text", ["", "   ", "???", "👍", "👎"])
def test_empty_normalized_replies_never_decide_even_with_legacy_words(text) -> None:
    # Older suspensions may contain an empty normalized approval word.
    assert reply.classify(text, frozenset({""}), NO) is None
    assert reply.classify(text, YES, frozenset({""})) is None


def test_new_incoming_filters_baseline_and_own_ask() -> None:
    ask = "「买牛奶」已到付款页，合计 ¥45。回复 好的 确认支付，或 不用 取消。"
    screen = make_screen(
        ("MyChat", 0.5, 0.05),  # header (baseline)
        ("已发送", 0.75, 0.2),  # our own earlier bubble, above the ask
        (ask[:20], 0.75, 0.3),  # our ask bubble, wrapped line (right side)
        ("好的", 0.25, 0.5),  # the NEW user reply (left side)
    )

    new = _new(screen.rows, {"MyChat"}, ask)

    assert new == ["好的"]


def test_a_long_reply_line_counts_wherever_its_center_sits() -> None:
    # A line that fills the bubble reaches the same edges on either side
    # of the thread, so its center says nothing about who wrote it: the
    # user's 再买… line centered at x 0.467 fell outside the incoming box
    # (x < 0.45) and two polls read silence — then an OCR fragment off
    # the avatar, inside the box, read as the reply "C". Below the ask,
    # position decides; a lone letter is never thread text.
    ask = (
        "已为您选好：正宗四川浦江红心猕猴桃，【大果】12枚单果90-110g，1件，"
        "实付¥26.7（含运费¥3）。请确认是否购买？（实付以屏幕读数为准：¥26.7）"
        "回复 好的 确认支付，或 不用 取消。"
    )
    wide = 0.32  # half-width of a full bubble line, either side
    screen = make_screen(
        ("已为您选好：正宗四川浦江红心猕猴", 0.507, 0.358, 0.95, wide),
        ("桃，【大果】12枚单果90-110g，1", 0.493, 0.381, 0.95, wide),
        ("件，实付￥26.7（含运费￥3）。请确", 0.501, 0.403, 0.95, wide),
        ("认是否购买？（实付以屏幕读数为", 0.485, 0.426, 0.95, 0.29),
        ("准：￥26.7）回复好的确认支付，或", 0.507, 0.448, 0.95, wide),
        ("不用取消。", 0.285, 0.472, 0.95, 0.095),  # the wrapped tail
        ("再买巨峰葡萄500g，一袋芝士片", 0.467, 0.534, 0.95, 0.29),
        ("C", 0.056, 0.547, 0.95, 0.015),  # the avatar doodle
    )

    assert reply.ask_band(screen.rows, ask, incoming=LEFT) == (0.358, 0.472)
    assert _new(screen.rows, {"不用取消。"}, ask) == ["再买巨峰葡萄500g，一袋芝士片"]


def test_a_time_stamp_under_the_ask_is_the_threads_not_a_reply() -> None:
    # Minutes later the thread prints a centered time stamp above the
    # reply bubble, below our ask: narrow and centered, it is the
    # thread's own row, and the reply under it is the message.
    ask = "现在下单吗？回复 好的 或 不用"
    screen = make_screen(
        (ask, 0.75, 0.3),
        ("20:04", 0.5, 0.45),
        ("好的", 0.25, 0.55),
    )

    assert _new(screen.rows, set(), ask) == ["好的"]


def test_quoted_reply_words_are_never_swallowed_as_own_lines() -> None:
    # The ask quotes "confirm"/"cancel", so a verbatim reply reads as a
    # piece of the ask; on the user's side it is still the reply — a
    # swallowed yes reads as silence and the gate suspend-loops past
    # explicit consent.
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


def test_the_status_bar_clock_is_never_incoming() -> None:
    # The status bar's clock sits top-left, LEFT of center, and ticks
    # every minute: at a send's landing (the deny sweep, after_ask off)
    # it is a row the baseline never held. On a live wake it read as the
    # reply "19:16", missed the declared words and re-planned the walk.
    screen = make_screen(
        ("19:16", 0.185, 0.028),
        ("19:07", 0.5, 0.17),
        ("盒马下单：", 0.27, 0.43),
        ("合计 ¥11.7 回复 好的 确认支付，或 不用 取消。", 0.6, 0.47),
    )
    ask = "盒马下单： 合计 ¥11.7 回复 好的 确认支付，或 不用 取消。"

    assert _new(screen.rows, {"19:14"}, ask, after_ask=False) == []
    assert _new(screen.rows, {"19:14"}, ask) == []


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
    # Two lone letters are no keyboard, so nothing below them is cut
    # off — and lone letters are never thread text themselves (an
    # avatar doodle reads as one): the message is read, they are not.
    screen = make_screen(("A", 0.2, 0.9), ("B", 0.4, 0.9), ("好的", 0.2, 0.95))

    assert reply.keyboard_top(screen.rows) is None
    assert _new(screen.rows, set(), "ask") == ["好的"]


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


def test_a_garbled_tail_is_still_the_asks_until_its_last_words_are_read() -> None:
    # OCR misreads the wrapped last line ("不用取消。" as "不甪取消。"): it is
    # no longer a suffix of the ask, but the ask's last words have not
    # been read from any line above it, so the row right under the band
    # is still ours. Read as the user's it would be a deny.
    ask = "实付13.68元。回复 好的 确认支付，或 不用 取消。"
    rows = [
        ("QiaoQian", 0.5, 0.08),
        (ask[:12], 0.6, 0.40),
        ("不甪取消。", 0.3, 0.43),  # the tail, one letter wrong
    ]

    assert reply.ask_band(make_screen(*rows).rows, ask, incoming=LEFT) == (0.40, 0.43)
    assert _new(make_screen(*rows).rows, {"QiaoQian"}, ask) == []
    assert _new(make_screen(*rows, ("好的", 0.2, 0.53)).rows, {"QiaoQian"}, ask) == [
        "好的"
    ]


def test_a_one_line_ask_with_a_stray_ocr_char_is_still_complete() -> None:
    # OCR tacks a character onto the ask's only line: the row holds the
    # whole ask, so the ask is complete and the reply right under it is
    # the user's — not a tail to claim.
    ask = "现在下单吗？回复 好的 或 不用"
    screen = make_screen(
        ("QiaoQian", 0.5, 0.08),
        (ask + "x", 0.6, 0.40),
        ("不用", 0.2, 0.43),
    )

    assert _new(screen.rows, {"QiaoQian"}, ask) == ["不用"]


def test_a_misread_character_in_an_ask_line_still_anchors_it() -> None:
    # OCR misreads one character in the middle of each ask line: no
    # line is a substring of the ask any more, but five consecutive
    # characters of each still are — the ask is placed, its garbled
    # last line still reads as its end, and the reply under it counts.
    ask = (
        "已为您选好：金沙河新疆雪花粉1kg，实付9.9元。回复 好的 确认支付，或 不用 取消。"
    )
    screen = make_screen(
        ("QiaoQian", 0.5, 0.08),
        ("已为您选好：金沙河甪疆雪花粉1kg，", 0.6, 0.40, 0.95, 0.3),
        ("实付9.9元。回复 好的 确甪支付，或 不用 取消。", 0.6, 0.43, 0.95, 0.3),
        ("好的", 0.2, 0.53),
    )

    assert reply.ask_band(screen.rows, ask, incoming=LEFT) == (0.40, 0.43)
    assert _new(screen.rows, {"QiaoQian"}, ask) == ["好的"]


def test_a_reply_right_under_a_complete_ask_is_never_absorbed() -> None:
    # The ask's last words were read on its own line: the band is
    # complete, and a reply one wrap gap under it — even one repeating
    # the ask's no word — is the user's.
    ask = "实付13.68元。回复 好的 确认支付，或 不用 取消。"
    screen = make_screen(
        ("QiaoQian", 0.5, 0.08),
        (ask, 0.6, 0.40),
        ("不用", 0.2, 0.43),
    )

    assert _new(screen.rows, {"QiaoQian"}, ask) == ["不用"]


def test_the_same_ask_sent_twice_is_read_at_its_latest_send() -> None:
    # An earlier run asked the same words and they are still on screen
    # above: two runs of anchors, and the ask is the lower one — the
    # latest send. Read at the upper, the new ask's own lines would be
    # the "reply" (three live sessions of 2026-09-13 showed exactly this).
    ask = "收到，正在盒马为你挑选：\n鲜牛奶 950ml ×1 ¥7.8\n合计 ¥16.6。回复 好的 确认支付，或 不用 取消。"
    screen = make_screen(
        (
            "合计 ¥16.6。回复 好的 确认支付，或 不用 取消。",
            0.6,
            0.20,
        ),  # the earlier ask
        ("不用", 0.25, 0.30),  # its reply, then
        ("收到，正在盒马为你挑选：", 0.3, 0.50),
        ("鲜牛奶 950ml ×1 ¥7.8", 0.3, 0.53),
        ("合计 ¥16.6。回复 好的 确认支付，或 不用 取消。", 0.6, 0.56),  # the new ask
        ("好的", 0.25, 0.66),
    )

    assert reply.ask_band(screen.rows, ask, incoming=LEFT) == pytest.approx(
        (0.50, 0.56)
    )
    assert _new(screen.rows, set(), ask) == ["好的"]


def test_a_narrow_row_on_our_side_under_the_ask_is_not_a_reply() -> None:
    # With no key row read, the keyboard floor is unset and the word
    # bar's "I'm" sits under the ask on the right: narrow, it shows its
    # side, and that side is ours (a live poll of 2026-09-13 read it).
    ask = "现在下单吗？回复 好的 或 不用"
    screen = make_screen(
        (ask, 0.75, 0.3),
        ("好的", 0.25, 0.5),
        ("The", 0.5, 0.65),
        ("I'm", 0.83, 0.65),
    )

    assert _new(screen.rows, set(), ask) == ["好的"]


def test_any_deny_is_the_sweeps_rule() -> None:
    # A deny said while the walk was in the app stops it whatever
    # followed — the sweep does not follow the newest message.
    assert reply.any_deny(["不要", "顺便查下天气"], YES, NO) is True
    assert reply.any_deny(["好的", "嗯"], YES, NO) is False
    assert reply.any_deny([], YES, NO) is False


# ---------- the ask's own band: what counts as OUR bubble ----------


ASK = "一单下单：\n牛奶 1L ×2\n实付 ¥40.8。回复 好的 确认支付，或 不用 取消。"


def test_short_leading_lines_of_our_own_bubble_are_not_a_reply() -> None:
    # A multi-line bubble is wide; its short leading lines sit inside it
    # and OCR LEFT of center, above the lines that anchor the band. Read
    # as incoming they become a reply the ask's words do not cover — and
    # a run that declares `revise:` then re-plans the errand from the
    # text the walk just sent itself.
    screen = make_screen(
        ("一单下单：", 0.25, 0.31),
        ("牛奶 1L ×2", 0.25, 0.34),
        ("实付 ¥40.8。回复 好的 确认支付，或 不用 取消。", 0.70, 0.37),
    )

    assert reply.ask_band(screen.rows, ASK, incoming=LEFT) == (0.31, 0.37)
    assert _new(screen.rows, {"earlier"}, ASK, after_ask=False) == []


def test_a_reply_repeating_one_of_the_asks_own_words_still_counts() -> None:
    # The band grows UPWARD only: "不用" is in the ask's own text, so
    # growing downward over it would swallow the cancellation.
    screen = make_screen(
        ("一单下单：", 0.25, 0.30),
        ("实付 ¥40.8。回复 好的 确认支付，或 不用 取消。", 0.70, 0.34),
        ("不用", 0.25, 0.37),
    )

    assert _new(screen.rows, set(), ASK) == ["不用"]


def test_the_band_is_none_when_our_message_is_not_on_the_thread() -> None:
    screen = make_screen(("好的", 0.25, 0.40))

    assert reply.ask_band(screen.rows, ASK, incoming=LEFT) is None
    assert _new(screen.rows, set(), ASK) == ["好的"]  # the baseline still decides


def test_the_incoming_box_is_the_ims() -> None:
    # The IM says where the user's bubbles sit (`incoming_box`). The
    # default reads a left-incoming thread; a right-incoming thread is
    # the same rows mirrored, read with that IM's box.
    left = make_screen(("our ask text", 0.75, 0.3), ("好的", 0.25, 0.4))
    assert _new(left.rows, set(), "our ask text") == ["好的"]

    mirrored = make_screen(("our ask text", 0.25, 0.3), ("好的", 0.75, 0.4))
    right = (0.55, 0.0, 1.0, 1.0)
    assert _new(mirrored.rows, set(), "our ask text", incoming=right) == ["好的"]
