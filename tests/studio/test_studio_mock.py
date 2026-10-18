"""Tests for `physiclaw.studio.mock` — frames paired off a session's
wire log, and the mock session's act door."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from physiclaw.common.listing import LISTING_HEADER
from physiclaw.studio import mock

JPEG = b"\xff\xd8\xff\xd9"


def _listing(label: str) -> str:
    return f'{LISTING_HEADER}\n1 [text] "{label}" [0.100,0.900,0.400,0.950] 0.95'


def _request(*tool_contents) -> dict:
    return {
        "kind": "request",
        "turn": 0,
        "messages": [{"role": "tool", "content": c} for c in tool_contents],
    }


def _openai_view(ref: str, listing: str, action: str = "") -> list:
    blocks = [{"type": "text", "text": action}] if action else []
    return blocks + [
        {"type": "image_url", "image_url": {"url": ref}},
        {"type": "text", "text": listing},
    ]


def _session_dir(tmp_path: Path, records: list[dict], images: list[str]) -> Path:
    d = tmp_path / "20260901-000000-abc123"
    (d / "images").mkdir(parents=True)
    for name in images:
        (d / "images" / name).write_bytes(JPEG)
    (d / "model.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\nnot json\n", encoding="utf-8"
    )
    return d


def test_session_frames_pairs_images_with_listings_in_order(tmp_path) -> None:
    home, results = _listing("home"), _listing("results")
    d = _session_dir(
        tmp_path,
        [
            {"kind": "session_start"},
            _request(_openai_view("images/a_t1.jpg", home)),
            # The next request carries the SAME view again (a session
            # recorded before the shared store filed a fresh copy) beside
            # the superseded stub of the old one — one frame each.
            _request(
                [{"type": "text", "text": "(superseded peek) — labels only\nhome"}],
                _openai_view("images/b_t2.jpg", home),
            ),
            _request(
                _openai_view("images/c_t3.jpg", results, "Tapped | screen: changed")
            ),
        ],
        ["a_t1.jpg", "b_t2.jpg", "c_t3.jpg"],
    )

    frames = mock.session_frames(d)

    assert [f.image.name for f in frames] == ["a_t1.jpg", "c_t3.jpg"]
    assert [f.listing for f in frames] == [home, results]


def test_session_frames_reads_anthropic_tool_result_shape(tmp_path) -> None:
    listing = _listing("home")
    d = _session_dir(
        tmp_path,
        [
            {
                "kind": "request",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "x",
                                "content": [
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "ref",
                                            "ref": "images/a.jpg",
                                        },
                                    },
                                    {"type": "text", "text": listing},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
        ["a.jpg"],
    )

    assert [f.image.name for f in mock.session_frames(d)] == ["a.jpg"]


def test_session_frames_ignores_the_system_prompt(tmp_path) -> None:
    # Doctrine quotes the listing header; a system message is never a view.
    listing = _listing("home")
    d = _session_dir(
        tmp_path,
        [
            {
                "kind": "request",
                "messages": [
                    {
                        "role": "system",
                        "content": _openai_view("images/a.jpg", listing),
                    },
                    {"role": "tool", "content": _openai_view("images/a.jpg", listing)},
                ],
            }
        ],
        ["a.jpg"],
    )

    assert len(mock.session_frames(d)) == 1


def test_session_frames_skips_missing_images_and_refuses_no_wire(tmp_path) -> None:
    d = _session_dir(
        tmp_path, [_request(_openai_view("images/gone.jpg", _listing("x")))], []
    )
    assert mock.session_frames(d) == []

    with pytest.raises(FileNotFoundError, match="model.jsonl"):
        mock.session_frames(tmp_path / "nowhere")


def _frames(tmp_path: Path, n: int) -> list[mock.Frame]:
    out = []
    for i in range(n):
        p = tmp_path / f"f{i}.jpg"
        p.write_bytes(JPEG)
        out.append(mock.Frame(image=p, listing=_listing(f"screen {i}")))
    return out


@pytest.mark.asyncio
async def test_views_show_the_current_frame_and_gestures_advance(tmp_path) -> None:
    s = mock.MockSession(_frames(tmp_path, 2), "abc123")
    assert s.state()["connected"] is True and s.state()["mcp_url"] == "mock:abc123"

    peek = await s.act("peek", {})
    assert peek["text"] == "" and peek["image"]["mime_type"] == "image/jpeg"
    assert peek["rows"][0]["label"] == "screen 0"

    tap = await s.act("tap", {"bbox": [0.1, 0.2, 0.3, 0.4]})
    assert "(mock) tap" in tap["text"] and "frame 2/2" in tap["text"]
    assert tap["rows"][0]["label"] == "screen 1"

    # A view does not advance; the next gesture wraps to the first frame.
    assert (await s.act("peek", {}))["rows"][0]["label"] == "screen 1"
    assert (await s.act("swipe", {"direction": "up"}))["rows"][0]["label"] == "screen 0"


@pytest.mark.asyncio
async def test_clipboard_is_text_only_and_unknown_tools_refuse(tmp_path) -> None:
    s = mock.MockSession(_frames(tmp_path, 1), "abc123")

    out = await s.act("send_to_clipboard", {"text": "hi"})
    assert out["image"] is None and "copied 'hi'" in out["text"]

    with pytest.raises(ValueError, match="published"):
        await s.act("calibrate", {})
    # A recording holds no phone capture, so the mock publishes no screenshot.
    assert "screenshot" not in s.state()["tools"]
    with pytest.raises(ValueError, match="published"):
        await s.act("screenshot", {})


def test_empty_session_refuses() -> None:
    with pytest.raises(ValueError, match="no screen views"):
        mock.MockSession([], "abc123")


def test_session_frames_prefer_the_tool_results_own_record(tmp_path) -> None:
    # The trace keeps every view whole on its tool_result — a walk's
    # turns included, which the wire log never carries.
    home, results = _listing("home"), _listing("results")
    d = _session_dir(
        tmp_path,
        [{"kind": "session_start"}, _request(_openai_view("images/w_t9.jpg", results))],
        ["a_t1.jpg", "c_t3.jpg", "w_t9.jpg"],
    )
    events = [
        {"event": "env"},
        {
            "event": "tool_result",
            "turn": 1,
            "name": "peek",
            "text": home,
            "images": ["images/a_t1.jpg"],
        },
        {"event": "tool_result", "turn": 2, "name": "note", "text": "noted"},
        {
            "event": "tool_result",
            "turn": 3,
            "name": "tap",
            "text": f"Tapped | screen: changed\n{results}",
            "images": ["images/c_t3.jpg"],
        },
        {
            "event": "walk_read",
            "after": "tap",
            "verdict": "match demo.results (1 anchor)",
        },
    ]
    (d / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )

    frames = mock.session_frames(d)

    assert [f.image.name for f in frames] == ["a_t1.jpg", "c_t3.jpg"]
    assert frames[1].listing.startswith("Tapped | screen: changed")


def _events(directory, records):
    (directory / "events.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )


def _event(ref, listing):
    return {"event": "tool_result", "text": listing, "images": [ref]}


@pytest.mark.parametrize(
    "refs", ["images/a.jpg", 42, {"file": "images/a.jpg"}, [None], [{}]]
)
def test_regression_malformed_image_fields_do_not_hide_later_frames(tmp_path, refs):
    directory = _session_dir(tmp_path, [], ["a.jpg"])
    broken = {"event": "tool_result", "text": _listing("bad"), "images": refs}
    _events(directory, [broken, _event("images/a.jpg", _listing("good"))])

    frames = mock.session_frames(directory)

    assert [frame.listing for frame in frames] == [_listing("good")]


@pytest.mark.parametrize("text", [None, 42, "", "ordinary tool output"])
def test_malformed_listing_text_does_not_hide_later_frames(tmp_path, text):
    directory = _session_dir(tmp_path, [], ["a.jpg"])
    _events(
        directory,
        [_event("images/a.jpg", text), _event("images/a.jpg", _listing("good"))],
    )

    frames = mock.session_frames(directory)

    assert [frame.listing for frame in frames] == [_listing("good")]


@pytest.fixture(params=["events", "wire"])
def recorded_views(tmp_path, request):
    directory = _session_dir(tmp_path, [], ["a.jpg", "b.jpg", "c.jpg"])

    def record(views):
        if request.param == "events":
            _events(directory, [_event(ref, listing) for ref, listing in views])
        else:
            records = [_request(_openai_view(ref, listing)) for ref, listing in views]
            (directory / "model.jsonl").write_text(
                "\n".join(json.dumps(rec) for rec in records), encoding="utf-8"
            )
        return directory

    return record


def test_regression_replay_preserves_a_return_to_an_earlier_screen(recorded_views):
    home, detail = _listing("home"), _listing("detail")
    directory = recorded_views(
        [
            ("images/a.jpg", home),
            ("images/a.jpg", home),
            ("images/b.jpg", detail),
            ("images/c.jpg", home),
        ]
    )

    frames = mock.session_frames(directory)

    assert [frame.listing for frame in frames] == [home, detail, home]
    assert [frame.image.name for frame in frames] == ["a.jpg", "b.jpg", "c.jpg"]


@pytest.mark.parametrize(
    "ref", ["../private.jpg", "images/../private.jpg", "images", "images/subdir"]
)
def test_regression_replay_skips_paths_outside_image_files(recorded_views, ref):
    directory = recorded_views(
        [(ref, _listing("bad")), ("images/a.jpg", _listing("good"))]
    )
    (directory.parent / "private.jpg").write_bytes(b"private")
    (directory / "private.jpg").write_bytes(b"private")
    (directory / "images" / "subdir").mkdir()

    frames = mock.session_frames(directory)

    assert [frame.image.name for frame in frames] == ["a.jpg"]


def test_regression_replay_skips_external_image_symlinks(recorded_views, tmp_path):
    directory = recorded_views(
        [("images/link.jpg", _listing("bad")), ("images/a.jpg", _listing("good"))]
    )
    external = tmp_path / "private.jpg"
    external.write_bytes(b"private")
    (directory / "images" / "link.jpg").symlink_to(external)

    frames = mock.session_frames(directory)

    assert [frame.image.name for frame in frames] == ["a.jpg"]


@pytest.mark.parametrize(
    ("suffix", "mime"),
    [(".jpg", "image/jpeg"), (".png", "image/png"), (".webp", "image/webp")],
)
async def test_regression_replay_preserves_recorded_image_mime(tmp_path, suffix, mime):
    image = tmp_path / ("frame" + suffix)
    image.write_bytes(JPEG)
    session = mock.MockSession([mock.Frame(image, _listing("screen"))], "recorded")

    response = await session.act("peek", {})

    assert response["image"]["mime_type"] == mime


async def test_regression_mock_serializes_manual_actions_with_driver(tmp_path):
    session = mock.MockSession(_frames(tmp_path, 2), "recorded")
    started, release = asyncio.Event(), asyncio.Event()

    async def driver(client):
        started.set()
        await release.wait()

    driving = asyncio.create_task(session.drive(driver))
    await started.wait()

    action = asyncio.create_task(session.act("tap", {}))
    await asyncio.sleep(0)
    completed_while_driving = action.done()
    release.set()
    await asyncio.gather(driving, action)

    assert completed_while_driving is False
    assert session.busy is False
