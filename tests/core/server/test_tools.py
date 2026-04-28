"""Tests for `physiclaw.core.server.tools` — MCP tool registrations.

Each tool is registered via `@mcp.tool()` and delegates to a method on
the PhysiClaw orchestrator via `asyncio.to_thread`. We feed register a
fake mcp that records and exposes each registered tool, then invoke
each one and verify it dispatches.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from physiclaw.core.server import tools as tools_mod


pytestmark = [pytest.mark.integration]


# ---------- Fake MCP ----------


class FakeMcp:
    """Records every @mcp.tool()'d coroutine."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


# ---------- helpers ----------


@pytest.fixture
def registered(mocker):
    """Build a registered mcp + a fake physiclaw with every method
    returning a string sentinel (or (jpeg, listing) for peek/screenshot)."""
    mcp = FakeMcp()
    pl = MagicMock()
    pl.peek.return_value = (b"PEEK_JPG", "peek-listing")
    pl.screenshot.return_value = (b"SS_JPG", "ss-listing")
    pl.tap.return_value = "tap result"
    pl.double_tap.return_value = "dt result"
    pl.long_press.return_value = "lp result"
    pl.swipe.return_value = "swipe result"
    pl.home_screen.return_value = "home result"
    pl.go_back.return_value = "back result"
    pl.force_quit.return_value = "fq result"
    pl.unlock_phone.return_value = "unlock result"
    pl.send_to_clipboard.return_value = "clip result"
    pl.sequence.return_value = "seq ok"

    mocker.patch.object(tools_mod, "save_tool_call")

    tools_mod.register(mcp, pl)
    return mcp, pl


def test_register_wires_all_expected_tools(registered) -> None:
    mcp, _ = registered

    expected = {
        "peek", "screenshot",
        "tap", "double_tap", "long_press", "swipe",
        "home_screen", "go_back", "force_quit", "unlock_phone",
        "send_to_clipboard", "sequence",
    }
    assert expected.issubset(set(mcp.tools.keys()))


@pytest.mark.asyncio
async def test_peek_returns_image_and_listing(registered, mocker) -> None:
    mcp, pl = registered
    save_spy = tools_mod.save_tool_call

    out = await mcp.tools["peek"]()

    assert pl.peek.called
    assert len(out) == 2
    # First item is an mcp Image; second is the listing string.
    assert out[1] == "peek-listing"
    save_spy.assert_called_once_with("peek", "peek-listing", b"PEEK_JPG")


@pytest.mark.asyncio
async def test_screenshot_returns_image_and_listing(registered) -> None:
    mcp, pl = registered

    out = await mcp.tools["screenshot"]()

    assert pl.screenshot.called
    assert out[1] == "ss-listing"


@pytest.mark.asyncio
async def test_tap_dispatches_with_bbox_and_appends_hint(registered) -> None:
    mcp, pl = registered

    out = await mcp.tools["tap"]([0.1, 0.1, 0.2, 0.2])

    pl.tap.assert_called_once_with([0.1, 0.1, 0.2, 0.2])
    assert "tap result" in out
    assert "peek" in out


@pytest.mark.asyncio
async def test_double_tap_dispatches(registered) -> None:
    mcp, pl = registered

    out = await mcp.tools["double_tap"]([0, 0, 1, 1])

    pl.double_tap.assert_called_once_with([0, 0, 1, 1])
    assert "dt result" in out


@pytest.mark.asyncio
async def test_long_press_dispatches(registered) -> None:
    mcp, pl = registered

    out = await mcp.tools["long_press"]([0, 0, 1, 1])

    pl.long_press.assert_called_once_with([0, 0, 1, 1])
    assert "lp result" in out


@pytest.mark.asyncio
async def test_swipe_dispatches_with_all_args(registered) -> None:
    mcp, pl = registered

    out = await mcp.tools["swipe"]([0, 0, 1, 1], "up", "l", "fast")

    pl.swipe.assert_called_once_with([0, 0, 1, 1], "up", "l", "fast")
    assert "swipe result" in out


@pytest.mark.asyncio
async def test_swipe_uses_default_size_and_speed(registered) -> None:
    mcp, pl = registered

    await mcp.tools["swipe"]([0, 0, 1, 1], "down")

    pl.swipe.assert_called_once_with([0, 0, 1, 1], "down", "m", "medium")


@pytest.mark.asyncio
async def test_navigation_tools_dispatch(registered) -> None:
    mcp, pl = registered

    out_home = await mcp.tools["home_screen"]()
    assert pl.home_screen.called
    assert "home result" in out_home

    out_back = await mcp.tools["go_back"]()
    assert pl.go_back.called
    assert "back result" in out_back

    out_fq = await mcp.tools["force_quit"]()
    assert pl.force_quit.called
    assert "fq result" in out_fq

    out_unlock = await mcp.tools["unlock_phone"]()
    assert pl.unlock_phone.called
    assert "unlock result" in out_unlock


@pytest.mark.asyncio
async def test_send_to_clipboard_dispatches(registered) -> None:
    mcp, pl = registered

    out = await mcp.tools["send_to_clipboard"]("hello world")

    pl.send_to_clipboard.assert_called_once_with("hello world")
    assert "clip result" in out


@pytest.mark.asyncio
async def test_sequence_filters_none_steps(registered) -> None:
    mcp, pl = registered
    s1 = {"tool_name": "tap", "arg": [0, 0, 1, 1]}
    s2 = {"tool_name": "tap", "arg": [0, 0, 1, 1]}

    await mcp.tools["sequence"](s1, s2, None, None, None)

    pl.sequence.assert_called_once_with([s1, s2])


@pytest.mark.asyncio
async def test_sequence_required_step_only(registered) -> None:
    mcp, pl = registered
    s1 = {"tool_name": "home_screen", "arg": None}

    await mcp.tools["sequence"](s1)

    pl.sequence.assert_called_once_with([s1])


@pytest.mark.asyncio
async def test_sequence_all_five_steps(registered) -> None:
    mcp, pl = registered
    steps = [{"tool_name": "tap", "arg": [0, 0, 1, 1]} for _ in range(5)]

    await mcp.tools["sequence"](*steps)

    pl.sequence.assert_called_once_with(steps)
