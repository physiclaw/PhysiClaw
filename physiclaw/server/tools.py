"""All MCP tools — the agent's surface for controlling the phone.

Mental model: **See → Act**. Take a photo, pick a bbox, do something there.

Each docstring is consumed twice: the first sentence becomes the inline
`## Tooling` card bullet (via AST parse in `agent/engine/mcp_inventory.py`),
and the full docstring becomes the JSONSchema `description` sent over the
native `tools=` API. So the first sentence must carry "what + when" on
its own; param semantics and return shape go in the body below.

All tools are async — blocking hardware I/O runs in a thread pool via
asyncio.to_thread() so the event loop stays free for HTTP routes.

Source order here is wire order. FastMCP registers tools in decorator-
call order (module top-to-bottom), and the MCP `list_tools()` RPC
replays them in that order. The engine concatenates MCP tools + local
tools in `tool_schemas`, so the order in this file directly determines
where each tool sits in the provider's `tools[]` array. LLMs show mild
position bias, so keep the most-reached-for tools (`peek`, `tap`) near
the top and don't shuffle without reason.
"""

import asyncio
from typing import Literal

from mcp.server.fastmcp import FastMCP, Image

from physiclaw.core import PhysiClaw
from physiclaw.logger import logged, save_tool_call
from physiclaw.server.types import Bbox, ClipboardText


def register(mcp: FastMCP, physiclaw: PhysiClaw):
    """Register every MCP tool on the given FastMCP instance."""

    # ─── See ─────────────────────────────────────────────────

    @mcp.tool()
    @logged
    async def peek() -> list:
        """Look at the phone via the overhead camera — default view tool.

        Call before any tap/swipe to confirm the target's bbox, and after
        to verify the screen changed. ~4s. Non-mutating (no side effects).

        Returns: [Image, listing] — a JPEG of the cropped camera view
        with icon bboxes drawn (so you can visually confirm what you'll
        tap), plus a plain-text element listing, one row per element:
            id [kind] "label" [left,top,right,bottom] conf
        where kind is "icon" or "text", bbox coords are 0-1 decimals.
        """
        jpeg, listing = await asyncio.to_thread(physiclaw.peek)
        save_tool_call("peek", listing, jpeg)
        return [Image(data=jpeg, format="jpeg"), listing]

    @mcp.tool()
    @logged
    async def screenshot() -> list:
        """Capture the phone's own screenshot (~12s, MUTATING) — only when peek misses the target.

        Triggers the iOS screenshot gesture, which apps observe and react
        to (share sheet pops up, shopping apps show similar-items panel,
        some apps watermark the captured frame). Treat as a mutating
        call — always `peek` AFTER one before tapping anything, since the
        screen state may have changed.

        Use only when:
          - The target is too small for the camera to detect (`peek` listing
            doesn't include it).
          - Camera glare or motion blur makes peek unreadable.
          - You need to read fine print the camera can't resolve.

        Returns: [Image, listing] — same shape as peek, but the JPEG is
        the phone's pixel-perfect screen capture, not a camera frame.
        """
        jpeg, listing = await asyncio.to_thread(physiclaw.screenshot)
        save_tool_call("screenshot", listing, jpeg)
        return [Image(data=jpeg, format="jpeg"), listing]

    # ─── Act ─────────────────────────────────────────────────

    @mcp.tool()
    @logged
    async def tap(bbox: Bbox) -> str:
        """Tap once at the center of `bbox` — for buttons, links, list items, dismissing dialogs.

        After this, `peek` to verify the screen changed and get a fresh
        bbox for your next move. If the listing is identical to before,
        the tap missed (retry once) or the bbox was wrong (pick a
        different element from the new peek).
        """
        result = await asyncio.to_thread(physiclaw.tap, bbox)
        return f"{result} — `peek` to verify and plan the next move"

    @mcp.tool()
    @logged
    async def double_tap(bbox: Bbox) -> str:
        """Two quick taps (~150ms apart) at the center of `bbox` — for zooming or word-select.

        Use for: zooming maps / photos / web pages, or selecting a word
        in editable text. Don't use for buttons — that's `tap`.
        """
        result = await asyncio.to_thread(physiclaw.double_tap, bbox)
        return f"{result} — `peek` to verify the zoom / selection landed"

    @mcp.tool()
    @logged
    async def long_press(bbox: Bbox) -> str:
        """Press and hold at the bbox center for ~1.2s — opens context menus, edit mode, paste menus.

        Use for: opening context menus, entering edit mode (rearrange
        icons), triggering the paste popover after `send_to_clipboard`.
        Always `peek` next — you need a fresh bbox from the just-revealed
        popover to tap (Paste / Copy / etc.).
        """
        result = await asyncio.to_thread(physiclaw.long_press, bbox)
        return f"{result} — `peek` next: you'll need a fresh bbox from the popover to tap (Paste / Copy / etc.)"

    # ─── Swipe ───────────────────────────────────────────────

    @mcp.tool()
    @logged
    async def swipe(
        bbox: Bbox,
        direction: Literal["up", "down", "left", "right"],
        size: Literal["s", "m", "l", "xl", "xxl"] = "m",
        speed: Literal["slow", "medium", "fast"] = "medium",
    ) -> str:
        """Slide the stylus across `bbox` in `direction` — STYLUS motion, not page motion.

        Use for: scrolling content, dismissing cards, paging carousels,
        revealing swipe-actions on list items, opening Control Center
        (swipe down from top-right). After this, `peek` to verify the
        page scrolled and get fresh bboxes from the new view.

        Common gotcha — direction is the STYLUS motion, not the page:
          - swipe UP   → page scrolls DOWN (reveals content below)
          - swipe DOWN → page scrolls UP (reveals content above / opens Notification Center)
          - swipe LEFT → page scrolls RIGHT (next photo / next page)

        Args:
            bbox: where to start the gesture (0-1 decimals).
            direction: which way the stylus moves — "up", "down", "left", "right".
            size: stroke length (default "m"):
                "s"   ≈ 1cm  — small nudge
                "m"   ≈ 2cm  — default, fits most scrolls
                "l"   ≈ 4cm  — long scroll
                "xl"  ≈ 6cm  — page-sized scroll
                "xxl" ≈ 8cm  — full-screen swipe (Control Center, Notification Center)
            speed: stylus velocity (default "medium"). Faster strokes build
                more momentum on iOS scroll lists (swipes keep coasting).
        """
        result = await asyncio.to_thread(physiclaw.swipe, bbox, direction, size, speed)
        return f"{result} — `peek` to verify the page scrolled and plan the next move"

    # ─── Navigate ────────────────────────────────────────────

    @mcp.tool()
    @logged
    async def home_screen() -> str:
        """Return to the iPhone home screen — exit any app from a known state.

        Issues the iPhone swipe-up-from-bottom gesture. Use to start a
        fresh task or recover from getting lost in app navigation. After
        this, `peek` to plan your next tap on the home-screen icons.
        """
        result = await asyncio.to_thread(physiclaw.home_screen)
        return f"{result} — `peek` to plan your next tap on the home-screen icons"

    @mcp.tool()
    @logged
    async def go_back() -> str:
        """Pop back one screen via the iPhone swipe-from-left-edge gesture.

        Works in apps with a navigation stack (most do). If `peek`
        after this shows the same screen, either the gesture didn't
        register (retry once) or this screen has no back action
        (modals, root tabs, lock screen) — try `home_screen` and
        re-enter, or look for an in-screen "Back" / "<" button to tap.
        """
        result = await asyncio.to_thread(physiclaw.go_back)
        return f"{result} — `peek` to verify navigation landed and plan the next move"

    @mcp.tool()
    @logged
    async def unlock_phone() -> str:
        """Unlock the phone by entering passcode 111111 (~12s).

        Wakes the screen, swipes up, waits for Face ID to fail, OCRs the
        keypad, taps each digit. Hardcoded to 111111 — a throwaway
        tool-phone code so a real password never leaks via git or logs.

        If unlock fails there's nothing else you can do — every other
        tool needs the phone unlocked, and you can't even reach IM to
        message the owner. Don't retry. Just:
          1. `append_log("[HH:MM] tried unlock_phone — failed")`
          2. `end_session("STUCK", "phone unlock failed — owner needs to
             set passcode to 111111 or disable auto-lock")`

        The owner can fix it later: set phone passcode to 111111, or
        disable auto-lock (Settings → Display & Brightness → Auto-Lock
        → Never; trade-off is faster display wear).
        """
        result = await asyncio.to_thread(physiclaw.unlock_phone)
        return f"{result} — `peek` to confirm you're on the home screen and plan the next tap"

    # ─── Text ────────────────────────────────────────────────

    @mcp.tool()
    @logged
    async def send_to_clipboard(text: ClipboardText) -> str:
        """Copy `text` to the phone's clipboard — use whenever you need to type into a field (much faster than the on-screen keyboard).

        Standard flow:
          1. Call this with the text.
          2. `long_press(field_bbox)` to open the paste popover.
          3. `tap` the "Paste" button (or "粘贴" in zh) that appears.

        Fall back to the keyboard only if the field rejects paste (rare
        — passcode fields, some search bars).
        """
        result = await asyncio.to_thread(physiclaw.send_to_clipboard, text)
        return f"{result} — next: `long_press` the target field, then `tap` the Paste button that appears"

    # ─── Sequence ────────────────────────────────────────────

    @mcp.tool()
    @logged
    async def sequence(
        step1: dict,
        step2: dict | None = None,
        step3: dict | None = None,
        step4: dict | None = None,
        step5: dict | None = None,
    ) -> str:
        """Run up to 5 actions in one call — saves turns on deterministic flows.

        Use when intermediate observations would add nothing (opening an
        app via tap → tap → tap, or pasting + sending an IM message).
        Stops at the first failure; earlier steps are NOT rolled back —
        `peek` after a failure to see where it left off before retrying.

        DON'T use sequence when:
          - You'd want to confirm the screen changed between steps
            (e.g. dialogs that may or may not appear).
          - The target's bbox is uncertain — verify with peek first.

        Each step is a dict with two fields:
            tool_name: one of "tap", "double_tap", "long_press",
                       "swipe", "send_to_clipboard".
            arg:       that tool's argument (see its docstring) — for
                       tap/double_tap/long_press a Bbox; for swipe a dict
                       of {bbox, direction, size?, speed?}; for
                       send_to_clipboard a string.

        Args:
            step1: first action (required).
            step2-5: additional actions (optional, executed in order).
        """
        steps = [s for s in (step1, step2, step3, step4, step5) if s is not None]
        result = await asyncio.to_thread(physiclaw.sequence, steps)
        return f"{result}\n— `peek` to verify the final state and plan the next move"
