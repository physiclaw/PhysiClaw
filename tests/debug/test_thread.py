"""Tests for `physiclaw.debug.thread` — the virtual thread file, the
staged-reply timing rule, and the listing renderer. The render must
satisfy the REAL consumers: the listing grammar (`Screen.read`), the
page matcher (`match_screen` against the channel pack's own
fingerprint), and the reply reader's bubble geometry
(`reply.read_incoming`).

Mutation review accepts log wording, cosmetic row confidence, the Bubble frozen
flag (consumers do not mutate bubbles), five versus six seconds of bounded lock
waiting, and the side chosen for a centered incoming region (configured IM
regions are on one side). Serialization, reply timing, geometry, and lock
ownership mutations must be detected.
"""

from __future__ import annotations

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import cv2
import numpy as np
import pytest
from debug_fakes import write_channel_pages
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from physiclaw.common import file_lock, paths
from physiclaw.common.bbox import center_of
from physiclaw.common.file_lock import exclusive_file_lock
from physiclaw.common.listing import Screen
from physiclaw.conductor.load.prints import prints_for_app
from physiclaw.conductor.spec import reply
from physiclaw.conductor.spec.conventions import THREAD_ID
from physiclaw.conductor.spec.match import match_screen
from physiclaw.conductor.spec.pages import (
    AnchorDecl,
    LearnedAnchor,
    LearnedPage,
    PageDecl,
    PagePrint,
)
from physiclaw.conductor.spec.reply import INCOMING_LEFT
from physiclaw.debug import thread as vthread


def _render(bubbles) -> Screen:
    return Screen.read(vthread.render_listing(bubbles, *vthread.channel_view()))


# ---------- the file ----------


def test_load_is_empty_when_missing() -> None:
    assert vthread.load() == vthread.Thread()


def test_seed_and_stage_round_trip_through_the_file() -> None:
    vthread.seed("buy milk", ["ok"])
    vthread.stage(["no thanks"])

    loaded = vthread.load()

    assert loaded.bubbles == [vthread.Bubble(sender=vthread.USER, text="buy milk")]
    assert loaded.staged == ["ok", "no thanks"]


def test_load_ignores_an_unknown_schema() -> None:
    paths.debug_dir().mkdir(parents=True, exist_ok=True)
    vthread.thread_path().write_text(
        json.dumps({"schema": 99, "bubbles": [{"from": "user", "text": "x"}]}),
        encoding="utf-8",
    )

    assert vthread.load() == vthread.Thread()


def test_load_ignores_unreadable_json() -> None:
    paths.debug_dir().mkdir(parents=True, exist_ok=True)
    vthread.thread_path().write_text("not json", encoding="utf-8")

    assert vthread.load() == vthread.Thread()


def _overlap_writes(mocker, first, second):
    """Pause the first publication until the second writer tries to enter.

    Before the fix, the second publishes its stale snapshot first. With a
    transaction lock, it must wait and read the first writer's new state.
    """
    ready, competing, release = threading.Event(), threading.Event(), threading.Event()
    local = threading.local()
    write = vthread.write_json_atomic
    unlink = Path.unlink

    def publish(path, data):
        if getattr(local, "first", False):
            ready.set()
            assert release.wait(3), "first writer was not released"
        write(path, data)
        if not getattr(local, "first", False):
            competing.set()

    def acquire(*args, **kwargs):
        if not getattr(local, "first", False):
            competing.set()
        return exclusive_file_lock(*args, **kwargs)

    def remove(path, *args, **kwargs):
        unlink(path, *args, **kwargs)
        competing.set()

    def start_first():
        local.first = True
        return first()

    mocker.patch.object(vthread, "write_json_atomic", side_effect=publish)
    mocker.patch.object(
        vthread, "exclusive_file_lock", side_effect=acquire, create=True
    )
    mocker.patch.object(Path, "unlink", remove)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(start_first)
        try:
            assert ready.wait(3), "first writer never reached publication"
            b = pool.submit(second)
            assert competing.wait(3), "second writer never entered"
        finally:
            release.set()
        return a.result(timeout=3), b.result(timeout=3)


@pytest.mark.parametrize(
    "first, expected",
    [
        (
            partial(vthread.stage, ["first"]),
            vthread.Thread(
                [
                    vthread.Bubble(vthread.USER, "task"),
                    vthread.Bubble(vthread.AGENT, "ask"),
                ],
                ["initial", "first", "second"],
            ),
        ),
        (
            partial(vthread.record_send, "another ask"),
            vthread.Thread(
                [
                    vthread.Bubble(vthread.USER, "task"),
                    vthread.Bubble(vthread.AGENT, "ask"),
                    vthread.Bubble(vthread.AGENT, "another ask"),
                ],
                ["initial", "second"],
            ),
        ),
        (
            vthread.peek_bubbles,
            vthread.Thread(
                [
                    vthread.Bubble(vthread.USER, "task"),
                    vthread.Bubble(vthread.AGENT, "ask"),
                    vthread.Bubble(vthread.USER, "initial"),
                ],
                ["second"],
            ),
        ),
    ],
    ids=["stage", "send", "release"],
)
def test_regression_concurrent_reply_preserves_both_updates(mocker, first, expected):
    vthread.seed("task", ["initial"])
    vthread.record_send("ask")

    _overlap_writes(mocker, first, partial(vthread.stage, ["second"]))

    assert vthread.load() == expected


@pytest.mark.parametrize(
    "replace",
    [
        partial(vthread.seed, "new task", ["fresh"]),
        partial(
            vthread.save,
            vthread.Thread([vthread.Bubble(vthread.USER, "new task")], ["fresh"]),
        ),
    ],
    ids=["seed", "save"],
)
def test_regression_reset_waits_for_pending_reply_write(mocker, replace):
    vthread.seed("old task", [])

    _overlap_writes(mocker, partial(vthread.stage, ["old reply"]), replace)

    assert vthread.load() == vthread.Thread(
        [vthread.Bubble(vthread.USER, "new task")], ["fresh"]
    )


def test_regression_clear_waits_for_pending_reply_write(mocker):
    vthread.seed("old task", [])

    _overlap_writes(mocker, partial(vthread.stage, ["old reply"]), vthread.clear)

    assert not vthread.thread_path().exists()


@pytest.mark.parametrize("failure", [OSError("disk full"), KeyboardInterrupt()])
def test_failed_update_preserves_script_and_releases_lock(mocker, failure):
    vthread.seed("task", ["first"])
    write = mocker.patch.object(vthread, "write_json_atomic", side_effect=failure)

    with pytest.raises(type(failure)):
        vthread.stage(["lost"])

    assert vthread.load().staged == ["first"]
    mocker.stop(write)
    vthread.stage(["second"])
    assert vthread.load().staged == ["first", "second"]


def test_clear_leaves_stable_lock_file_and_reports_missing_script():
    vthread.seed("task", [])
    lock = vthread.thread_path().with_suffix(".lock")
    inode = lock.stat().st_ino

    vthread.clear()

    assert lock.stat().st_ino == inode
    with pytest.raises(FileNotFoundError):
        vthread.clear()


@pytest.mark.parametrize(
    "update",
    [
        partial(vthread.stage, ["reply"]),
        partial(vthread.record_send, "ask"),
        vthread.peek_bubbles,
        partial(vthread.seed, "reset", []),
        partial(vthread.save, vthread.Thread()),
        vthread.clear,
    ],
    ids=["stage", "send", "peek", "seed", "save", "clear"],
)
def test_thread_updates_wait_for_a_competing_writer(mocker, update):
    vthread.seed("task", ["reply"])
    vthread.record_send("ask")
    holder = exclusive_file_lock(vthread.thread_path().with_suffix(".lock"))
    holder.__enter__()
    sleep = mocker.patch.object(
        file_lock.time, "sleep", side_effect=lambda _: holder.__exit__(None, None, None)
    )

    try:
        update()
    finally:
        holder.__exit__(None, None, None)

    sleep.assert_called_once_with(0.01)


@pytest.mark.parametrize(
    "bubbles, staged",
    [
        ([], []),
        ([], ["wait"]),
        ([vthread.Bubble(vthread.AGENT, "ask")], []),
        ([vthread.Bubble(vthread.USER, "task")], ["wait"]),
    ],
)
def test_peek_without_pending_answer_does_not_publish(mocker, bubbles, staged):
    expected = vthread.Thread(bubbles, staged)
    vthread.save(expected)
    write = mocker.spy(vthread, "write_json_atomic")

    result = vthread.peek_bubbles()

    assert result == bubbles
    assert vthread.load() == expected
    write.assert_not_called()


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    bubbles=st.lists(
        st.builds(
            vthread.Bubble,
            sender=st.sampled_from([vthread.USER, vthread.AGENT]),
            text=st.text(max_size=30),
        ),
        max_size=5,
    ),
    staged=st.lists(st.text(max_size=30), max_size=5),
)
def test_save_roundtrips_conversation(bubbles, staged):
    thread = vthread.Thread(bubbles, staged)

    vthread.save(thread)

    assert vthread.load() == thread


def test_saved_script_keeps_existing_schema_and_sender_names():
    vthread.seed("task", ["reply"])
    vthread.record_send("ask")

    data = json.loads(vthread.thread_path().read_text(encoding="utf-8"))

    assert data == {
        "schema": 1,
        "bubbles": [{"from": "user", "text": "task"}, {"from": "agent", "text": "ask"}],
        "staged": ["reply"],
    }


@pytest.mark.parametrize(
    "raw", ["[]", '{"schema":1,"bubbles":[null]}', '{"schema":1,"staged":1}']
)
def test_load_malformed_structure_returns_empty(raw):
    vthread.seed("old task", [])
    vthread.thread_path().write_text(raw, encoding="utf-8")

    loaded = vthread.load()

    assert loaded == vthread.Thread()


@pytest.mark.parametrize(
    "learned, within, expected",
    [
        (
            LearnedPage({"Header": LearnedAnchor("Header", 0.8, 0.12, 0.05, 1)}, 1),
            (0, 0, 0.4, 0.2),
            (0.8, 0.12),
        ),
        (None, (0.2, 0.4, 0.8, 0.6), (0.5, 0.5)),
        (None, None, (0.5, 0.03)),
    ],
    ids=["learned", "region", "default"],
)
def test_render_anchor_position_matches_fingerprint(learned, within, expected):
    pp = PagePrint(
        "channel",
        PageDecl("thread", (AnchorDecl("Header", within=within),), "Thread"),
        learned,
    )

    screen = Screen.read(vthread.render_listing([], pp, INCOMING_LEFT))

    assert center_of(screen.rows[0].bbox) == pytest.approx(expected)


@pytest.mark.parametrize(
    "cx, cy, expected",
    [(0.01, 0.99, (0, 0.975, 0.07, 1)), (0.99, 0.01, (0.93, 0, 1, 0.025))],
)
def test_render_clips_learned_anchor_extents_to_screen(cx, cy, expected):
    learned = LearnedPage({"Header": LearnedAnchor("Header", cx, cy, 0.05, 1)}, 1)
    pp = PagePrint(
        "channel", PageDecl("thread", (AnchorDecl("Header"),), "Thread"), learned
    )

    screen = Screen.read(vthread.render_listing([], pp, INCOMING_LEFT))

    assert screen.rows[0].bbox == pytest.approx(expected)


def test_render_spreads_default_anchors_in_declared_order():
    pp = PagePrint(
        "channel",
        PageDecl(
            "thread",
            (AnchorDecl("First"), AnchorDecl("Second"), AnchorDecl("Third")),
            "Thread",
        ),
    )

    screen = Screen.read(vthread.render_listing([], pp, INCOMING_LEFT))

    assert [r.label for r in screen.rows] == ["First", "Second", "Third"]
    assert [center_of(r.bbox) for r in screen.rows] == [
        (0.5, 0.03),
        (0.5, 0.075),
        (0.5, 0.12),
    ]


def test_render_caps_long_lines_at_bubble_width():
    bubbles = [vthread.Bubble(vthread.USER, "x" * 100)]

    screen = Screen.read(vthread.render_listing(bubbles, None, INCOMING_LEFT))

    assert screen.rows[0].bbox == pytest.approx((0.18, 0.185, 0.82, 0.215))


@pytest.mark.parametrize(
    "incoming, user_x, agent_x",
    [
        (INCOMING_LEFT, 0.20, 0.80),
        ((0.55, 0, 1, 1), 0.80, 0.20),
        ((0.51, 0, 0.69, 1), 0.80, 0.20),
    ],
)
def test_render_places_bubbles_on_their_configured_sides(incoming, user_x, agent_x):
    bubbles = [vthread.Bubble(vthread.USER, "u"), vthread.Bubble(vthread.AGENT, "a")]

    screen = Screen.read(vthread.render_listing(bubbles, None, incoming))

    assert [center_of(row.bbox)[0] for row in screen.rows] == pytest.approx(
        [user_x, agent_x]
    )


def test_render_keeps_newest_nonblank_lines_within_screen():
    bubbles = [vthread.Bubble(vthread.USER, "\n".join(map(str, range(15))) + "\n \n")]

    screen = Screen.read(vthread.render_listing(bubbles, None, INCOMING_LEFT))

    assert [r.label for r in screen.rows] == list(map(str, range(3, 15)))
    assert [center_of(r.bbox)[1] for r in screen.rows] == pytest.approx(
        [0.2 + 0.03 * i for i in range(12)], abs=0.001
    )


# ---------- staged-reply timing ----------


def test_staged_reply_waits_while_no_ask_is_outstanding() -> None:
    # Newest bubble is the user's own task — a released reply here would
    # answer a question nobody asked.
    vthread.seed("buy milk", ["ok"])

    bubbles = vthread.peek_bubbles()

    assert bubbles == [vthread.Bubble(sender=vthread.USER, text="buy milk")]
    assert vthread.load().staged == ["ok"]


def test_staged_reply_releases_on_the_peek_after_an_ask() -> None:
    vthread.seed("buy milk", ["ok"])
    vthread.record_send("Total is $4.50 — reply ok to confirm")

    bubbles = vthread.peek_bubbles()

    assert bubbles[-1] == vthread.Bubble(sender=vthread.USER, text="ok")
    assert vthread.load().staged == []


def test_record_send_itself_releases_nothing() -> None:
    # The send's result is the gate's baseline snapshot — a reply
    # released into it would be baselined away.
    vthread.seed("buy milk", ["ok"])

    bubbles = vthread.record_send("reply ok to confirm")

    assert bubbles[-1].sender == vthread.AGENT
    assert vthread.load().staged == ["ok"]


def test_one_release_per_ask_in_script_order() -> None:
    vthread.seed("buy milk", ["ok, but make it two boxes", "ok"])
    vthread.record_send("ask one")
    first = vthread.peek_bubbles()[-1]
    second = vthread.peek_bubbles()[-1]
    vthread.record_send("ask two")

    third = vthread.peek_bubbles()[-1]

    assert first == vthread.Bubble(
        sender=vthread.USER, text="ok, but make it two boxes"
    )
    assert second == first  # spent ask releases nothing more
    assert third == vthread.Bubble(sender=vthread.USER, text="ok")


# ---------- the render vs its real consumers ----------


def test_render_parses_as_listing_rows() -> None:
    write_channel_pages(("MyChat", "InputBox"))

    screen = _render([vthread.Bubble(vthread.USER, "buy milk")])

    labels = [r.label for r in screen.rows]
    assert "buy milk" in labels and "MyChat" in labels and "InputBox" in labels


def test_render_matches_the_channel_thread_fingerprint() -> None:
    write_channel_pages(("MyChat", "InputBox"))
    screen = _render([vthread.Bubble(vthread.USER, "buy milk")])

    verdict = match_screen(screen, prints_for_app("channel"))

    assert verdict.matches(THREAD_ID)


def test_user_bubble_reads_as_incoming_and_agent_ask_is_excluded() -> None:
    # The gate's exact read: baseline snapshot taken at ask time, then a
    # fresh user reply below the ask — new_incoming must return exactly it.
    write_channel_pages()
    ask = "Total is $4.50. Reply ok to confirm the payment."
    at_ask = _render(
        [
            vthread.Bubble(vthread.USER, "buy milk"),
            vthread.Bubble(vthread.AGENT, ask),
        ]
    )
    baseline = {r.label.strip() for r in at_ask.rows if r.label.strip()}
    after_reply = _render(
        [
            vthread.Bubble(vthread.USER, "buy milk"),
            vthread.Bubble(vthread.AGENT, ask),
            vthread.Bubble(vthread.USER, "ok"),
        ]
    )

    yes, no = frozenset({"ok", "好的"}), frozenset({"不用"})
    new = reply.read_incoming(after_reply.rows, baseline, ask, incoming=INCOMING_LEFT)[
        0
    ]

    assert new == ["ok"]
    assert reply.classify_all(new, yes, no) == "confirm"


def test_multiline_bubble_renders_as_wrapped_rows() -> None:
    write_channel_pages()

    screen = _render([vthread.Bubble(vthread.USER, "buy milk\ntwo boxes")])

    labels = [r.label for r in screen.rows]
    assert "buy milk" in labels and "two boxes" in labels


def test_a_long_reply_line_reads_as_the_reply() -> None:
    # A line that fills the bubble is rendered as wide as a real one, so
    # its center drifts past the incoming box — and the reader must still
    # read it as the reply, by its place under the ask.
    write_channel_pages()
    ask = "已为您选好红心猕猴桃12枚，实付¥26.7。回复 好的 确认支付，或 不用 取消。"
    long_reply = "再买巨峰葡萄500g，一袋芝士片，然后一起结算"
    screen = _render(
        [
            vthread.Bubble(vthread.USER, "buy fruit"),
            vthread.Bubble(vthread.AGENT, ask),
            vthread.Bubble(vthread.USER, long_reply),
        ]
    )
    row = next(r for r in screen.rows if r.label == long_reply)
    assert center_of(row.bbox)[0] > 0.45  # past the box's edge

    new = reply.read_incoming(screen.rows, set(), ask, incoming=INCOMING_LEFT)
    assert new == ([long_reply], True)


def test_render_survives_a_missing_channel_pack() -> None:
    screen = _render([vthread.Bubble(vthread.USER, "buy milk")])

    assert [r.label for r in screen.rows] == ["buy milk"]


# ---------- wire shapes ----------


def test_view_blocks_lead_with_an_image() -> None:
    # Block 0 must NOT be text, or `verdict.action_text` would misread
    # the listing as composed action text (and `screen_text` drop it).
    blocks = vthread.view_blocks("listing")

    assert blocks[0]["type"] == "image"
    assert blocks[1] == {"type": "text", "text": "listing"}


@pytest.mark.parametrize(
    "blocks",
    [vthread.view_blocks("listing"), vthread.gesture_blocks("action", "listing")],
)
def test_virtual_image_is_a_decodable_jpeg(blocks):
    image = next(b for b in blocks if b["type"] == "image")
    encoded = np.frombuffer(
        base64.b64decode(image["data"], validate=True), dtype=np.uint8
    )

    decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)

    assert image["mimeType"] == "image/jpeg"
    assert decoded.shape == (1, 1, 3)


def test_gesture_blocks_lead_with_the_action_text() -> None:
    blocks = vthread.gesture_blocks("did it | screen: changed", "listing")

    assert blocks[0] == {"type": "text", "text": "did it | screen: changed"}
    assert blocks[1]["type"] == "image"
    assert blocks[2] == {"type": "text", "text": "listing"}
