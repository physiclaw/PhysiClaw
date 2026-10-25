"""Property tests for the conductor's pure kernels — the functions the
field bugs landed in (thousands separators, short anchors, wrapped
reply tails). Each property is an invariant a reader can state in one
line; hypothesis searches for the input that breaks it."""

from __future__ import annotations

import json
import unicodedata
from decimal import Decimal

from conductor_fakes import make_screen
from hypothesis import example, given, settings
from hypothesis import strategies as st

from physiclaw.conductor.spec import conventions, match, reply
from physiclaw.conductor.steps import select
from physiclaw.conductor.walk import money

# ---------- reply: normalization and whole-message reading ----------

_text = st.text(min_size=0, max_size=40)
_words = st.lists(st.text(min_size=1, max_size=8), min_size=1, max_size=4)


@given(_text)
def test_normalize_is_idempotent(text: str) -> None:
    # A word stored normalized at parse must read back equal to itself.
    # (Length is no invariant: NFKC expands ¼ to "1⁄4".)
    once = reply.normalize(text)
    assert reply.normalize(once) == once


@given(_text)
def test_normalize_has_no_whitespace_and_no_edge_punctuation(text: str) -> None:
    norm = reply.normalize(text)
    assert not any(ch.isspace() for ch in norm)
    if norm:
        assert not _is_punct(norm[0]) and not _is_punct(norm[-1])


def _is_punct(ch: str) -> bool:
    return unicodedata.category(ch)[0] in reply.EDGE_CATEGORIES


# Wrapping noise a reply actually carries: the punctuation and spaces
# of a chat message. (A compatibility character like ㈀ or ¨ expands
# under NFKC into letters or a combining mark that joins the word — a
# different message, which the reader is right to refuse.)
_noise = st.text(alphabet="。！!?？～~,.，、；;:：…()（）【】[] 	", max_size=3)


@given(_words, _words, _noise, _noise)
def test_a_declared_word_is_read_whatever_wraps_it(
    yes: list[str], no: list[str], lead: str, tail: str
) -> None:
    # Whole-message equality in normalize space: surrounding whitespace
    # and punctuation never defeat a declared word.
    yes_n = frozenset(reply.normalize(w) for w in yes) - {""}
    no_n = frozenset(reply.normalize(w) for w in no) - {""} - yes_n
    for word, answer in (
        (next(iter(yes_n), None), "confirm"),
        (next(iter(no_n), None), "deny"),
    ):
        if word is None:
            continue
        wrapped = f"{lead}  {word} {tail}"
        assert reply.classify(wrapped, yes_n, no_n) == answer


@given(st.lists(_text, max_size=5), _words, _words)
def test_the_newest_message_is_the_answer(
    messages: list[str], yes: list[str], no: list[str]
) -> None:
    yes_n = frozenset(reply.normalize(w) for w in yes) - {""}
    no_n = frozenset(reply.normalize(w) for w in no) - {""}
    out = reply.classify_all(messages, yes_n, no_n)
    if not messages:
        assert out is None
    else:
        assert out is reply.classify(messages[-1], yes_n, no_n)
    # The sweep's rule: a deny anywhere is a deny.
    assert reply.any_deny(messages, yes_n, no_n) is any(
        reply.classify(m, yes_n, no_n) is reply.Answer.DENY for m in messages
    )


# ---------- money: amounts off a screen ----------

_amount = st.decimals(
    min_value=0, max_value=99_999, places=2, allow_nan=False, allow_infinity=False
)


@given(_amount, st.sampled_from(["¥", "￥", "$", "€"]))
def test_the_declared_total_reads_back_whole(amount, sign: str) -> None:
    # Thousands separators included — "¥1,234.56" once quoted ¥1 — and
    # any currency sign.
    screen = make_screen((f"Total {sign}{_grouped(amount)}", 0.5, 0.9))
    read = money.declared_total(screen, ("Total",))
    assert read is not None and round(read, 2) == round(float(amount), 2)


def _grouped(a) -> str:
    whole, _, frac = f"{a:f}".partition(".")
    frac = frac.rstrip("0")
    grouped = f"{int(whole):,}"
    return f"{grouped}.{frac}" if frac else grouped


@given(st.integers(min_value=0, max_value=10**20))
@example(cents=10**16)
@example(cents=10**16 + 1)
@settings(max_examples=300)
def test_saved_consent_preserves_cents_and_cannot_authorize_a_cent_more(cents) -> None:
    text = f"{cents // 100}.{cents % 100:02d}"
    screen = make_screen((f"Total ${text}", 0.5, 0.9))
    total = money.declared_total(screen, ("Total",))
    if total is None:
        return  # Unrepresentable totals must never produce a quote.
    assert Decimal(money.plain(total)) * 100 == cents
    saved = json.loads(json.dumps(total))
    assert (
        money.fire_block(consented=saved, total_label=("Total",), screen=screen) is None
    )

    changed = cents + 1
    screen = make_screen((f"Total ${changed // 100}.{changed % 100:02d}", 0.5, 0.9))
    assert money.fire_block(consented=saved, total_label=("Total",), screen=screen)


@given(st.text(alphabet=st.characters(blacklist_categories=("Cc", "Cs")), max_size=40))
@example(label="$0")
@example(label="saved $10 Total $59.90")
def test_the_total_reader_never_raises_and_reads_only_currency_marked_numbers(
    label: str,
) -> None:
    # Arbitrary OCR text may contain ambiguous or unassociated prices.
    # The reader may decline those, but must never raise or invent an
    # amount without a currency-marked number in the parsed listing.
    screen = make_screen((f"Total {label}", 0.5, 0.15))
    amounts = set(map(money.amount, conventions.PRICE_RE.findall(screen.labels_text)))

    out = money.declared_total(screen, ("Total",))

    assert out is None or out in amounts


# ---------- match: normalization and the label tiers ----------


@given(_text)
def test_match_normalize_is_stable_under_folding(text: str) -> None:
    # The class tokens (<NUM>, <PRICE>) are uppercase markers applied
    # once, so a second full pass would lowercase them; the fold itself
    # (NFKC, casefold, whitespace) must already be stable.
    once = match.normalize(text)
    refolded = "".join(unicodedata.normalize("NFKC", once).split())
    assert unicodedata.normalize("NFKC", refolded) == once


@given(st.text(min_size=1, max_size=12))
def test_a_label_always_matches_its_own_anchor(text: str) -> None:
    norm = match.normalize(text)
    if norm:
        assert match.label_matches(norm, norm, ())


@given(
    st.integers(min_value=0, max_value=9999),
    st.integers(min_value=0, max_value=59),
    st.integers(min_value=0, max_value=23),
)
def test_volatile_numbers_normalize_to_class_tokens(
    n: int, minute: int, hour: int
) -> None:
    # "the clock is still a clock": a price, a time, a count each become one token.
    assert match.normalize(f"¥{n}") == match.normalize("¥0")
    assert match.TIME_TOKEN in match.normalize(f"{hour}:{minute:02d}")


# ---------- the boot's activate step: seaming two thread readings ----------

_labels = st.lists(
    st.text(min_size=1, max_size=4, alphabet="abcxyz"), min_size=0, max_size=8
)


@given(_labels, _labels)
@settings(max_examples=200)
def test_merge_keeps_every_newer_label_and_never_loses_the_tail(
    newer: list[str], previous: list[str]
) -> None:
    merged = select.merge_labels(newer, previous)
    assert merged[: len(newer)] == newer  # the newer reading leads, in order
    assert len(merged) <= len(newer) + len(previous)
    # Whatever seam was chosen, the previous reading's tail is preserved.
    assert previous == [] or merged[-1] == previous[-1] or merged[-1] == newer[-1]


@given(_labels)
def test_merging_a_reading_with_itself_is_the_reading(labels: list[str]) -> None:
    assert select.merge_labels(labels, labels) == labels
