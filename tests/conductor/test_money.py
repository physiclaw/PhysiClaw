"""Tests for `physiclaw.conductor.walk.money` — the declared total, the
fire-time predicates, and the one payment guard, pure arithmetic over a
screen."""

from __future__ import annotations

from conductor_fakes import make_screen

from physiclaw.conductor.spec.match import Reading, Verdict
from physiclaw.conductor.walk import money

_ON_SHEET = Verdict(Reading.MATCH, "shop.sheet", 0.0, "3/3 anchors")


def test_declared_total_reads_the_label_row_itself() -> None:
    screen = make_screen(("原价 ¥79", 0.5, 0.4), ("合计: ¥59.9", 0.5, 0.9))

    assert money.declared_total(screen, ("合计",)) == 59.9


def test_declared_total_joins_a_split_row_on_the_same_line() -> None:
    # OCR split the footer: "合计" and its amount sit side by side.
    screen = make_screen(("合计", 0.2, 0.9), ("¥59.9", 0.3, 0.9), ("¥79", 0.5, 0.2))

    assert money.declared_total(screen, ("合计",)) == 59.9


def test_declared_total_ignores_a_far_amount() -> None:
    screen = make_screen(("合计", 0.2, 0.9), ("¥79", 0.8, 0.2))

    assert money.declared_total(screen, ("合计",)) is None


def test_declared_total_never_reads_a_row_above_the_bare_label() -> None:
    # The rig's Taobao order sheet, 2026-09-03: the pay button reads the
    # exact label 免密支付 with no price on its line, and a 顺手买 add-on
    # (¥15.80) sits a hand above it — closer than the 实付 row at the
    # top. The quoted total is the 实付 row's, never the add-on's.
    sheet = make_screen(
        ("实付￥24.75 优惠前￥45", 0.55, 0.245),
        ("五常大米5kg真空锁鲜 每千克￥4.95", 0.42, 0.42),
        ("顺手买·天猫维达集团旗舰店", 0.27, 0.655),
        ("￥15.80￥49.90", 0.44, 0.77),
        ("免密支付", 0.50, 0.935),
    )

    assert money.declared_total(sheet, ("实付", "免密支付", "优惠后")) == 24.75


def test_declared_total_reads_at_most_two_decimals() -> None:
    # OCR glues a digit onto a price now and then ("￥24.751"): the
    # amount reads to the fen, never a third decimal.
    screen = make_screen(("实付 ￥24.751", 0.5, 0.9))

    assert money.declared_total(screen, ("实付",)) == 24.75


def test_declared_total_takes_the_nearest_amount_on_the_line() -> None:
    # OCR split the label from its amount, and the struck-through
    # original price shares the line further right: the amount beside
    # the label is the nearest one, not the first in listing order.
    sheet = make_screen(
        ("优惠前￥62.5", 0.62, 0.27),
        ("实付", 0.20, 0.27),
        ("￥50", 0.33, 0.27),
    )

    assert money.declared_total(sheet, ("实付",)) == 50


def test_declared_total_needs_the_label() -> None:
    screen = make_screen(("¥45", 0.5, 0.5))

    assert money.declared_total(screen, ("合计", "总计")) is None
    assert (
        money.declared_total(make_screen(("总计 ¥45", 0.5, 0.5)), ("合计", "总计"))
        == 45
    )


def test_fire_block_requires_consent_then_the_quoted_total() -> None:
    sheet = make_screen(("合计 ¥45", 0.5, 0.9))
    label = ("合计",)

    assert "without a confirmed total" in (
        money.fire_block(consented=None, total_label=label, screen=sheet) or ""
    )
    assert money.fire_block(consented=45.0, total_label=label, screen=sheet) is None
    changed = make_screen(("合计 ¥60", 0.5, 0.9))
    assert "now 60 beside 合计" in (
        money.fire_block(consented=45.0, total_label=label, screen=changed) or ""
    )
    gone = make_screen(("提交订单", 0.5, 0.9))
    assert "no amount beside 合计" in (
        money.fire_block(consented=45.0, total_label=label, screen=gone) or ""
    )


def test_fire_block_reads_only_the_declared_total() -> None:
    # A promo card mid-page read ￥45 when the ask quoted the sheet and
    # ￥46 at the tap — one OCR digit on a card that is not the order. The
    # walk knows money only where the pack says the total is read.
    label = ("合计",)
    flickered = make_screen(("￥46¥0.99", 0.5, 0.63), ("合计：￥11.7", 0.6, 0.92))

    assert money.fire_block(consented=11.7, total_label=label, screen=flickered) is None
    repriced = make_screen(("￥45¥0.99", 0.5, 0.63), ("合计：￥46", 0.6, 0.92))
    assert "now 46" in (
        money.fire_block(consented=11.7, total_label=label, screen=repriced) or ""
    )


def test_declared_total_reads_thousands_separators() -> None:
    sheet = make_screen(("合计 ¥1,234.56", 0.5, 0.9), ("¥12", 0.5, 0.3))

    assert money.declared_total(sheet, ("合计",)) == 1234.56


def test_declared_total_prefers_the_exact_label_and_the_footer() -> None:
    # "商品合计" is a subtotal wearing the total's characters; the payable
    # total is the bare label, and among bare labels the lowest row.
    sheet = make_screen(
        ("商品合计 ¥79", 0.5, 0.3), ("合计: ¥59.9", 0.5, 0.9), ("合计 ¥10", 0.5, 0.2)
    )

    assert money.declared_total(sheet, ("合计",)) == 59.9


def test_payment_block_on_the_consented_total_is_none() -> None:
    sheet = make_screen(("合计 ¥45", 0.5, 0.9))

    blocked = money.payment_block(
        _ON_SHEET, sheet, "shop", "move 'pay'", consented=45.0, total_label=("合计",)
    )

    assert blocked is None


def test_payment_block_off_a_verified_page_names_the_page_once() -> None:
    elsewhere = Verdict(Reading.UNKNOWN, None, 0.0, "no page")
    sheet = make_screen(("合计 ¥45", 0.5, 0.9))

    blocked = money.payment_block(
        elsewhere, sheet, "shop", "move 'pay'", consented=45.0, total_label=("合计",)
    )

    assert blocked == (
        "move 'pay': current screen is not a verified shop page"
        " — money never reads or fires blind"
    )


def test_payment_block_on_a_changed_total_prefixes_the_fire_reason() -> None:
    changed = make_screen(("合计 ¥60", 0.5, 0.9))

    blocked = money.payment_block(
        _ON_SHEET, changed, "shop", "move 'pay'", consented=45.0, total_label=("合计",)
    )

    assert blocked == (
        "move 'pay': sheet changed after consent: confirmed 45, now 60 beside 合计"
    )


def test_payment_block_without_consent_blocks() -> None:
    sheet = make_screen(("合计 ¥45", 0.5, 0.9))

    blocked = money.payment_block(
        _ON_SHEET, sheet, "shop", "move 'pay'", consented=None, total_label=("合计",)
    )

    assert blocked == "move 'pay': reached without a confirmed total"
