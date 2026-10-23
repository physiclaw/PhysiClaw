"""Weighted URL recommendations, shared selections, and automatic rotation."""

from __future__ import annotations

import io
import json
import os
import pty
import random
import select
import signal
import subprocess
import sys
import termios
import time
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import pytest

from hardware.documents import BuildError
from hardware.documents.sourcing import urls
from hardware.scheme import REPO_ROOT

NOW = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)


def bom_rows(count=8):
    return [
        {
            "part_id": f"p{i}",
            "component": {"en": f"Part {i}", "zh": f"零件{i}"},
            "spec": "5 mm bore",
            "qty": "2",
        }
        for i in range(count)
    ]


def supplier(item, vendor=0, platform="taobao"):
    return {
        "name": f"Shop {vendor}",
        "product": "smooth, 5 mm",
        "url": (
            f"https://item.taobao.com/item.htm?id={item}&skuId={vendor}&spm=tracking"
            if platform == "taobao"
            else f"https://www.aliexpress.us/item/{item}{vendor}.html"
        ),
    }


def inventory(count=8):
    return urls.collect_candidates(
        bom_rows(count),
        {
            lang: [
                {
                    "part_id": f"p{i}",
                    "suppliers": [supplier(i, j, platform) for j in range(3)],
                }
                for i in range(count)
            ]
            for lang, platform in [("zh", "taobao"), ("en", "aliexpress")]
        },
    )


def checked_state(candidates, days=1):
    timestamp = (NOW - timedelta(days=days)).isoformat()
    return {
        "version": 1,
        "round_index": 1,
        "checks": {
            candidate.url: {
                "fingerprint": candidate.fingerprint,
                "checked_at": timestamp,
                "last_round": 1,
                "shown_at": [timestamp],
            }
            for candidate in candidates
        },
    }


def test_three_per_market_bom_and_vendor_order_with_reproducible_seed(tmp_path):
    candidates = inventory()
    state = urls.load_state(tmp_path / "history.json")
    selected = urls.select_round(candidates, state, NOW, rng=random.Random(7))
    assert list(selected) == [
        *[f"T{i}" for i in range(1, 4)],
        *[f"A{i}" for i in range(1, 4)],
    ]
    assert all(item.usages[0].vendor_position == 1 for item in selected.values())
    assert [item.usages[0].bom_position for item in selected.values()] == list(
        range(1, 4)
    ) * 2
    assert (
        urls.select_round(list(reversed(candidates)), state, NOW, rng=random.Random(7))
        == selected
    )

    first_row = [item for item in candidates if item.usages[0].bom_position == 1]
    assert [
        item.usages[0].vendor_position
        for item in urls.select_round(first_row, state, NOW).values()
    ] == [1, 2, 3] * 2


def test_elapsed_time_can_outweigh_bom_and_vendor_importance():
    candidates = inventory()
    state = checked_state(candidates, days=1)
    last = candidates[23]  # Last BOM row's third Taobao vendor.
    timestamp = (NOW - timedelta(days=20)).isoformat()
    state["checks"][last.url].update(checked_at=timestamp, shown_at=[timestamp])
    assert urls.select_round(candidates, state, NOW)["T1"] == last


def test_randomness_varies_close_scores_but_preserves_large_score_differences(tmp_path):
    candidates = inventory(57)[:10]  # Neighboring BOM rows have similar scores.
    state = urls.load_state(tmp_path / "history.json")
    first_batches = {
        tuple(
            item.url
            for item in urls.select_round(
                candidates, state, NOW, rng=random.Random(seed)
            ).values()
        )
        for seed in range(30)
    }
    assert len(first_batches) > 1
    # Only vendors at the same BOM row: 6, 4, 2 weights are too far apart for
    # bounded jitter to reverse their order.
    far_apart = inventory(1)
    for seed in range(20):
        selected = urls.select_round(far_apart, state, NOW, rng=random.Random(seed))
        assert [item.usages[0].vendor_position for item in selected.values()] == [
            1,
            2,
            3,
        ] * 2


def test_collects_notes_nested_details_accessories_and_optional_tools():
    rows = bom_rows(2)
    main = supplier(1)
    main["details"] = {
        "steps": [
            {
                "text": '<a href="https://detail.tmall.com/item.htm?id=2&amp;skuId=3">Order</a>'
            },
            {
                "text": '<a href="https://example.com/order?a=1&amp;b=2">Instructions</a>'
            },
        ]
    }
    entries = [
        {
            "part_id": "p0",
            "suppliers": [main],
            "note": '<a href="https://item.taobao.com/item.htm?id=77&amp;skuId=8">Note</a>',
            "accessories": [
                {
                    "name": "Adapter",
                    "url": "https://www.aliexpress.us/item/99.html",
                    "spec": "5 mm",
                }
            ],
        },
        {"part_id": "p1", "note": "Ditto"},
    ]
    tools = [
        {
            "name": {"en": "Key", "zh": "扳手"},
            "spec": "11 mm",
            "purpose": "Optional",
            "sources": {
                "zh": [supplier(88)],
                "en": [supplier(89, platform="aliexpress")],
            },
        }
    ]
    items = urls.collect_candidates(rows, {"zh": entries}, tools)
    by_url = {item.url: item for item in items}
    note = by_url["https://item.taobao.com/item.htm?id=77&skuId=8"]
    assert [usage.part_id for usage in note.usages] == ["p0", "p1"]
    assert all(usage.source == "note" for usage in note.usages)
    assert "https://example.com/order?a=1&b=2" not in by_url
    assert (
        by_url["https://detail.tmall.com/item.htm?id=2&skuId=3"].usages[0].source
        == "details"
    )
    assert (
        by_url["https://www.aliexpress.us/item/99.html"].usages[0].source == "accessory"
    )
    optional = next(item for item in items if item.usages[0].source == "optional")
    assert optional.usages[0].part_id == "扳手"
    assert optional.importance < note.importance
    assert not any("890.html" in item.url for item in items)


def test_link_extraction_handles_localization_bare_links_and_url_punctuation():
    data = {
        "note": {
            "en": '<a href="https://example.com/a?x=1&amp;y=2">Doc</a>',
            "zh": "https://example.com/chinese",
        },
        "nested": [
            "See https://example.com/item(foo).",
            "[Link](https://example.com/guide)",
            "mailto:a@example.com",
        ],
    }
    assert set(urls.extract_urls(data, "en")) == {
        "https://example.com/a?x=1&y=2",
        "https://example.com/item(foo)",
        "https://example.com/guide",
    }


def test_recommendations_record_dates_and_advance_the_round(tmp_path):
    candidates = inventory()
    path = tmp_path / "history.json"
    state = urls.load_state(path)
    before = deepcopy(state)
    original = urls.select_round(candidates, state, NOW)
    assert state == before
    state = urls.record_round(original, state, NOW)
    assert before["checks"] == {}  # Only recommended URLs acquire a timestamp.
    urls.save_state(path, state)
    restored = urls.load_state(path)
    next_round = urls.select_round(candidates, restored, NOW)
    assert {item.url for item in next_round.values()}.isdisjoint(
        item.url for item in original.values()
    )
    assert all(urls.last_checked(item, restored) == NOW for item in original.values())


def test_shared_urls_include_all_variants_and_ditto_but_keep_taobao_skus_distinct():
    rows = bom_rows(3)
    authored = [
        {"part_id": "p0", "suppliers": [supplier(1), supplier(1, 1)]},
        {"part_id": "p1", "suppliers": "Ditto"},
        {"part_id": "p2", "suppliers": ["Ditto", supplier(1, 2)]},
    ]
    candidates = urls.collect_candidates(rows, {"zh": authored})
    assert len(candidates) == 3
    assert [usage.part_id for usage in candidates[0].usages] == ["p0", "p1", "p2"]
    assert candidates[0].importance == candidates[0].usages[0].importance
    assert "spm=" not in candidates[0].url and "skuId=0" in candidates[0].url

    shared = supplier(1, platform="aliexpress")
    authored = [
        {"part_id": "p0", "suppliers": [shared]},
        {"part_id": "p1", "suppliers": [{**shared, "product": "toothed, 5 mm"}]},
        {"part_id": "p2"},
    ]
    (candidate,) = urls.collect_candidates(rows, {"en": authored})
    assert [usage.product for usage in candidate.usages] == [
        "smooth, 5 mm",
        "toothed, 5 mm",
    ]


@pytest.mark.parametrize("field", ["spec", "qty", "product", "url"])
def test_source_changes_become_new_selections(field):
    rows = bom_rows(1)
    authored = [{"part_id": "p0", "suppliers": [supplier(1)]}]
    original = urls.collect_candidates(rows, {"zh": authored})
    state = checked_state(original)
    if field in {"spec", "qty"}:
        rows[0][field] = "changed"
    else:
        authored[0]["suppliers"][0][field] = (
            "https://item.taobao.com/item.htm?id=2" if field == "url" else "toothed"
        )
    changed = urls.collect_candidates(rows, {"zh": authored})
    assert urls.last_checked(changed[0], state) is None
    selected = urls.select_round(changed, state, NOW)
    state = urls.record_round(selected, state, NOW)
    assert urls.last_checked(changed[0], state) == NOW


def test_reordering_changes_priority_without_erasing_verification():
    rows = bom_rows(2)
    authored = [
        {"part_id": "p0", "suppliers": [supplier(1), supplier(2)]},
        {"part_id": "p1"},
    ]
    original = urls.collect_candidates(rows, {"zh": authored})
    state = checked_state(original)
    authored[0]["suppliers"].reverse()
    changed = urls.collect_candidates(list(reversed(rows)), {"zh": authored})
    assert all(
        urls.last_checked(item, state) == NOW - timedelta(days=1) for item in changed
    )
    assert changed[0].importance != original[1].importance


def test_initial_sweep_reaches_bottom_rows_and_third_vendors(tmp_path):
    candidates = inventory()
    state = urls.load_state(tmp_path / "history.json")
    seen = set()
    for round_index in range(8):
        now = NOW + timedelta(seconds=round_index)
        selected = urls.select_round(candidates, state, now)
        this_round = {item.url for item in selected.values()}
        assert this_round.isdisjoint(seen)
        seen.update(this_round)
        state = urls.record_round(selected, state, now)
    assert seen == {candidate.url for candidate in candidates}


@pytest.mark.parametrize("smaller_pool", ["taobao", "aliexpress"])
def test_rolling_rounds_are_complete_and_independent(tmp_path, smaller_pool):
    candidates = [
        item
        for item in inventory(3)
        if item.platform != smaller_pool
        or item.usages[0].bom_position == 1
        or (item.usages[0].bom_position, item.usages[0].vendor_position) == (2, 1)
    ]
    # One pool has nine URLs, the other four and a partial final sweep batch.
    path = tmp_path / "history.json"
    state = urls.load_state(path)
    expected = {
        platform: {item.url for item in candidates if item.platform == platform}
        for platform in urls.PLATFORMS
    }
    seen = {platform: set() for platform in urls.PLATFORMS}
    completed = Counter()
    for index in range(6):
        now = NOW + timedelta(hours=index)
        selected = urls.select_round(candidates, state, now, rng=random.Random(index))
        assert Counter(item.platform for item in selected.values()) == {
            platform: 1 if platform == smaller_pool and index % 2 else 3
            for platform in urls.PLATFORMS
        }
        for platform in urls.PLATFORMS:
            batch = {
                item.url for item in selected.values() if item.platform == platform
            }
            assert batch.isdisjoint(seen[platform])
            seen[platform].update(batch)
            if seen[platform] == expected[platform]:
                completed[platform] += 1
                seen[platform].clear()
        state = urls.record_round(selected, state, now)
        urls.save_state(path, state)
        state = urls.load_state(path)
    assert completed == {
        platform: 3 if platform == smaller_pool else 2 for platform in urls.PLATFORMS
    }
    assert all(not batch for batch in seen.values())


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_rolling_30_day_cutoff_is_recomputed_each_run(offset):
    candidates = inventory(2)
    state = checked_state(candidates)
    last = candidates[5]  # Last Taobao row's third vendor.
    timestamp = (NOW - timedelta(days=30) + timedelta(seconds=offset)).isoformat()
    state["checks"][last.url].update(checked_at=timestamp, shown_at=[timestamp])
    selected = urls.select_round(candidates, state, NOW)
    taobao = [item for item in selected.values() if item.platform == "taobao"]
    assert len(taobao) == (1 if offset <= 0 else 3)
    if offset <= 0:
        assert taobao == [last]


def test_individual_recommendations_expire_without_resetting_recent_ones():
    candidates = inventory(2)
    state = checked_state(candidates)
    for check in state["checks"].values():
        check["shown_at"] = [
            (NOW - timedelta(days=29)).isoformat(),
            check["checked_at"],
        ]
    last = candidates[5]
    state["checks"][last.url]["shown_at"][0] = (NOW - timedelta(days=31)).isoformat()
    selected = urls.select_round(candidates, state, NOW)
    assert [item for item in selected.values() if item.platform == "taobao"] == [last]
    state = urls.record_round(selected, state, NOW)
    assert state["checks"][last.url]["shown_at"] == [
        (NOW - timedelta(days=1)).isoformat(),
        NOW.isoformat(),
    ]
    # Tomorrow the other Taobao links each lose their 29-day-old recommendation;
    # this link's two recent recommendations stay in the rolling count.
    selected = urls.select_round(candidates, state, NOW + timedelta(days=1))
    assert last.url not in {item.url for item in selected.values()}


def test_future_dates_do_not_count_as_recent_checks_or_erase_history():
    candidates = inventory(2)
    state = checked_state(candidates)
    future = (NOW + timedelta(days=1)).isoformat()
    future_urls = {candidates[5].url, candidates[11].url}
    for url in future_urls:
        state["checks"][url].update(checked_at=future, shown_at=[future] * 3)
    selected = urls.select_round(candidates, state, NOW)
    assert {item.url for item in selected.values()} == future_urls
    state = urls.record_round(selected, state, NOW)
    assert all(
        state["checks"][url]["shown_at"] == [future] * 3 + [NOW.isoformat()]
        for url in future_urls
    )
    selected = urls.select_round(candidates, state, NOW)
    assert future_urls.isdisjoint(item.url for item in selected.values())


def test_rolling_counts_with_irregular_runs_and_catalog_changes(tmp_path):
    rng = random.Random(2026)
    candidates = inventory(5)
    state = urls.load_state(tmp_path / "checks.json")
    events = []  # Independent, unpruned log of actual recommendations.
    now = NOW
    for turn in range(160):
        now += rng.choice(
            [
                timedelta(0),
                timedelta(minutes=10),
                timedelta(hours=6),
                timedelta(days=1),
                timedelta(days=29),
                timedelta(days=31),
            ]
        )
        if turn == 40:
            candidates = inventory(6)
        elif turn == 90:
            candidates = [
                item for item in candidates if item.usages[0].bom_position > 1
            ]
        elif turn == 130:
            item = candidates[0]
            item.usages[0] = replace(item.usages[0], spec="updated specification")
        fingerprints = {item.url: item.fingerprint for item in candidates}
        counts = Counter(
            url
            for url, fingerprint, timestamp in events
            if fingerprint == fingerprints.get(url)
            and now - timedelta(days=30) < timestamp <= now
        )
        before = deepcopy(state)
        selected = urls.select_round(candidates, state, now, rng=rng)
        assert state == before
        assert len({item.url for item in selected.values()}) == len(selected)
        for platform in urls.PLATFORMS:
            pool = [item for item in candidates if item.platform == platform]
            minimum = min(counts[item.url] for item in pool)
            batch = [item for item in selected.values() if item.platform == platform]
            assert len(batch) == min(
                3, sum(counts[item.url] == minimum for item in pool)
            )
            assert all(counts[item.url] == minimum for item in batch)
        events.extend(
            (item.url, fingerprints[item.url], now) for item in selected.values()
        )
        state = urls.record_round(selected, state, now)
        urls.save_state(tmp_path / "checks.json", state)
        state = urls.load_state(tmp_path / "checks.json")


def test_long_gaps_and_clock_reversal_preserve_coverage(tmp_path):
    candidates = inventory(2)
    state = urls.load_state(tmp_path / "history.json")
    state = urls.record_round(urls.select_round(candidates, state, NOW), state, NOW)
    later = NOW + timedelta(days=95)
    first = urls.select_round(candidates, state, later)
    state = urls.record_round(first, state, later)
    earlier = NOW + timedelta(days=89)
    remaining = urls.select_round(candidates, state, earlier)
    assert {item.url for item in first.values()}.isdisjoint(
        item.url for item in remaining.values()
    )
    assert {item.url for item in [*first.values(), *remaining.values()]} == {
        item.url for item in candidates
    }


def test_lower_rolling_count_outweighs_bom_vendor_and_time_scores():
    candidates = inventory()
    state = checked_state(candidates, days=10)
    for check in state["checks"].values():
        check["shown_at"] = [
            (NOW - timedelta(days=20)).isoformat(),
            check["checked_at"],
        ]
    last = candidates[23]  # Lowest BOM/vendor importance on Taobao.
    state["checks"][last.url].update(
        checked_at=(NOW - timedelta(days=1)).isoformat(),
        shown_at=[(NOW - timedelta(days=1)).isoformat()],
    )
    selected = urls.select_round(candidates, state, NOW)
    assert [item for item in selected.values() if item.platform == "taobao"] == [last]


@pytest.mark.parametrize(
    "step", [timedelta(0), timedelta(hours=1), -timedelta(hours=1)]
)
def test_repeated_sweeps_cover_every_url_with_stopped_or_reversing_clock(
    tmp_path, step
):
    candidates = inventory()
    state = checked_state(candidates, days=0)
    path = tmp_path / "history.json"
    expected = {item.url for item in candidates}
    bound = max(Counter(item.platform for item in candidates).values())
    # Start after the initial sweep, where the old scheduler could repeat only
    # the same six URLs forever when all timestamps matched the current clock.
    for sweep in range(3):
        seen = set()
        for round_index in range(bound):
            now = NOW + step * (sweep * bound + round_index)
            selected = urls.select_round(candidates, state, now)
            seen.update(item.url for item in selected.values())
            state = urls.record_round(selected, state, now)
            urls.save_state(path, state)
            state = urls.load_state(path)
            if seen == expected:
                break
        assert seen == expected


def test_real_inventory_is_complete_and_rolling_rounds_reach_every_url(tmp_path):
    rows = urls.load_bom_rows()
    vendors = {
        lang: urls.load_sourcing_data(path) for lang, path in urls.VENDOR_FILES.items()
    }
    candidates = urls.collect_candidates(rows, vendors, urls.load_optional_tools())
    expected = set()

    class GuideLinks(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "a" and "report-link" not in attrs.get("class", "").split():
                href = attrs.get("href", "")
                if urls.platform_for(href) in urls.PLATFORMS:
                    expected.add(urls.clean_supplier_url(href))

    # Independently audit the actual rendered guides, including notes, ordering
    # dialogs and tools, rather than checking just the supplier JSON columns.
    from hardware.documents.sourcing import build

    for lang, entries in vendors.items():
        GuideLinks().feed(build.render_document(rows, entries, "", lang))
    assert expected <= {item.url for item in candidates}
    all_urls = {item.url for item in candidates}
    counts = Counter(item.platform for item in candidates)
    state = {"version": 1, "round_index": 0, "checks": {}}
    path = tmp_path / "checks.json"
    runs_per_sweep = max((count + 2) // 3 for count in counts.values())
    # Audit three full rounds against the catalog, with persisted progress and
    # individual recommendations aging out between rounds 31 days apart.
    for window in range(3):
        started = NOW + timedelta(days=31 * window)
        seen = {platform: set() for platform in counts}
        for index in range(runs_per_sweep):
            now = started + timedelta(minutes=index)
            selected = urls.select_round(candidates, state, now)
            for platform, count in counts.items():
                batch = {
                    item.url for item in selected.values() if item.platform == platform
                }
                remaining = count - len(seen[platform])
                assert len(batch) == min(3, remaining or count)
                if remaining:
                    assert batch.isdisjoint(seen[platform])
                seen[platform].update(batch)
            state = urls.record_round(selected, state, now)
            urls.save_state(path, state)
            state = urls.load_state(path)
        assert set.union(*seen.values()) == all_urls
        now = started + timedelta(hours=2)
        selected = urls.select_round(candidates, state, now)
        assert len(selected) == sum(min(3, count) for count in counts.values())
        state = urls.record_round(selected, state, now)


@pytest.mark.parametrize("field", ["round_index", "last_round", "shown_at"])
def test_history_requires_current_fields(tmp_path, field):
    state = checked_state(inventory(1))
    record = state if field == "round_index" else next(iter(state["checks"].values()))
    del record[field]
    path = tmp_path / "checks.json"
    path.write_text(json.dumps(state))
    with pytest.raises(BuildError, match="Cannot read URL review history"):
        urls.load_state(path)


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        '{"version": 2}',
        '{"version": 1, "round_index": -1, "checks": {}}',
        '{"version": 1, "round_index": true, "checks": {}}',
    ],
)
def test_malformed_history_is_not_silently_discarded(tmp_path, content):
    path = tmp_path / "history.json"
    path.write_text(content)
    with pytest.raises(BuildError, match="Cannot read URL review history"):
        urls.load_state(path)
    assert path.read_text() == content


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("last_round", 2),
        ("checked_at", "2026-10-22"),
        ("shown_at", None),
        ("shown_at", "invalid"),
        ("shown_at", ["2026-10-22"]),
    ],
)
def test_invalid_check_record_preserves_history(tmp_path, field, value):
    state = checked_state(inventory(1))
    next(iter(state["checks"].values()))[field] = value
    path = tmp_path / "history.json"
    content = json.dumps(state)
    path.write_text(content)
    with pytest.raises(BuildError, match="Cannot read URL review history"):
        urls.load_state(path)
    assert path.read_text() == content


@pytest.mark.parametrize("failure", ["render", "flush"])
def test_output_failure_does_not_advance_history(tmp_path, monkeypatch, failure):
    path = tmp_path / "checks.json"
    urls.save_state(path, {"version": 1, "round_index": 0, "checks": {}})
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise BrokenPipeError("output unavailable")

    class Output(io.StringIO):
        flush = fail

    with monkeypatch.context() as patch:
        if failure == "render":
            patch.setattr(urls, "print_round", fail)
        else:
            patch.setattr(sys, "stdout", Output())
        assert urls.main.__wrapped__(["--state", str(path)]) == 1
    assert path.read_bytes() == before


def test_failed_atomic_save_preserves_history(tmp_path, monkeypatch):
    path = tmp_path / "checks.json"
    urls.save_state(path, {"version": 1, "round_index": 0, "checks": {}})
    before = path.read_bytes()
    entries = set(tmp_path.iterdir())

    def fail(*args):
        raise OSError("write unavailable")

    monkeypatch.setattr(urls.Path, "replace", fail)
    with pytest.raises(OSError, match="write unavailable"):
        urls.save_state(path, {"version": 1, "round_index": 1, "checks": {}})
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) == entries


def test_shared_history_is_locked_across_output_directories(tmp_path):
    path = tmp_path / "checks.json"
    command = [sys.executable, "-m", "hardware", "url", "--state", str(path)]
    env = {**os.environ, "PHYSICLAW_HW_OUTPUT": str(tmp_path / "other-output")}
    with urls.history_lock(path):
        rejected = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=20
        )
        assert rejected.returncode != 0
        assert "URL history already in use" in rejected.stderr
        assert not rejected.stdout
        assert not path.exists()
    accepted = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=20
    )
    assert accepted.returncode == 0, accepted.stderr
    assert urls.load_state(path)["round_index"] == 1


@pytest.mark.parametrize(
    ("keys", "browser_result"),
    [
        (" x\rabc", True),
        (("\x1b[A", "\x1b[15~", "é", "中", "q"), True),
        ("xq", True),
        ("q", True),
        ("\x1b", True),
        ("Q", True),
        ("\x04", True),
        ("xq", False),
        (None, True),
    ],
)
def test_terminal_opens_one_link_per_key_and_restores_input(
    tmp_path, keys, browser_result
):
    state_path = tmp_path / "checks.json"
    master, slave = pty.openpty()
    settings = termios.tcgetattr(slave)
    script = """
import sys
from hardware.documents.sourcing import urls

def open_url(url, **kwargs):
    print("BROWSER " + url, flush=True)
    return sys.argv[2] == "True"

urls.webbrowser.open = open_url
try:
    result = urls.main(["--state", sys.argv[1]])
except KeyboardInterrupt:
    result = 130
print("REVIEW_EXIT", flush=True)
sys.exit(result)
"""
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(state_path), str(browser_result)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PHYSICLAW_HW_OUTPUT": str(tmp_path / "output"),
            "NO_COLOR": "1",
        },
    )
    output = b""

    def wait_for(text):
        nonlocal output
        deadline = time.monotonic() + 10
        while text.encode() not in output:
            remaining = max(0, deadline - time.monotonic())
            assert select.select([master], [], [], remaining)[0], output.decode()
            output += os.read(master, 65536)

    try:
        wait_for("Open 1/6")
        # Recording is complete before waiting, even if the user quits immediately.
        recorded = state_path.read_bytes()
        state = json.loads(recorded)
        listed = [
            line for line in output.decode().splitlines() if line.startswith("https://")
        ]
        assert len(listed) == 6
        assert set(state["checks"]) == set(listed)
        assert state["round_index"] == 1
        count = 0
        if keys is None:
            child.send_signal(signal.SIGINT)
        else:
            for key in keys:
                os.write(master, key.encode())
                if key in {"q", "Q", "\x1b", "\x04"}:
                    break
                wait_for("BROWSER " + listed[count])
                count += 1
                if count < 6:
                    wait_for(f"Open {count + 1}/6")
        wait_for("REVIEW_EXIT")
        assert child.wait(timeout=10) == (130 if keys is None else 0)
        while select.select([master], [], [], 0)[0]:
            output += os.read(master, 65536)
        opened = [
            line.split("BROWSER ", 1)[1]
            for line in output.decode().splitlines()
            if "BROWSER " in line
        ]
        assert opened == listed[:count]
        restored = termios.tcgetattr(slave)
        # macOS sets PENDIN when canonical input is restored.
        restored[3] &= ~termios.PENDIN
        settings[3] &= ~termios.PENDIN
        assert restored == settings
        assert state_path.read_bytes() == recorded
        if not browser_result:
            assert "failed; use the URL above" in output.decode()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://detail.tmall.com/item.htm?id=1", "taobao"),
        ("https://www.aliexpress.com/item/1.html", "aliexpress"),
        ("https://taobao.com.evil.test/item.htm", "other"),
        ("https://notaliexpress.us/item/1.html", "other"),
        ("https://us.misumi-ec.com/", "other"),
        ("mailto:a@example.com", None),
    ],
)
def test_platform_grouping_uses_real_domain_boundaries(url, platform):
    assert urls.platform_for(url) == platform


def test_real_make_target_automatically_records_marketplaces_and_rotates(tmp_path):
    state_path = tmp_path / "checks.json"

    def invoke(*arguments):
        return subprocess.run(
            [
                "make",
                "--no-print-directory",
                "hw-url",
                f"HW_DOC={sys.executable} -m hardware",
                f"ARGS=--state {state_path} {' '.join(arguments)}",
            ],
            cwd=REPO_ROOT,
            env={**os.environ, "PHYSICLAW_HW_OUTPUT": str(tmp_path / "output")},
            capture_output=True,
            text=True,
            timeout=20,
        )

    result = invoke()
    assert result.returncode == 0, result.stderr
    assert "Taobao (3)" in result.stdout
    assert "AliExpress (3)" in result.stdout
    assert "Other" not in result.stdout
    assert str(state_path) not in result.stdout
    assert "Need:" not in result.stdout
    assert "URLs recorded as checked" not in result.stdout
    assert "\033[" not in result.stdout
    state = json.loads(state_path.read_text())
    assert len(state["checks"]) == 6
    assert all(urls.platform_for(url) in urls.PLATFORMS for url in state["checks"])
    assert "--checked" not in result.stdout
    assert "any key" not in result.stdout
    assert sum(line.startswith("https://") for line in result.stdout.splitlines()) == 6
    detailed = invoke("--verbose")
    assert detailed.returncode == 0, detailed.stderr
    assert "Need:" in detailed.stdout
    assert str(state_path) in detailed.stdout
    updated = json.loads(state_path.read_text())
    assert len(updated["checks"]) == 12
    assert all(
        updated["checks"][url] == check for url, check in state["checks"].items()
    )
