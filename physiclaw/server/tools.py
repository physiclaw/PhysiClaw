"""All MCP tools — the agent's surface for controlling the phone.

Mental model: **See → Act**. Take a photo, pick a bbox, do something there.

Each docstring is consumed twice: the first sentence becomes the inline
`## Tooling` card bullet (via AST parse in `agent/engine/mcp_inventory.py`),
and the full docstring becomes the JSONSchema `description` sent over the
native `tools=` API. So the first sentence must carry "what + when" on
its own; param semantics and return shape go in the body below.

All tools are async — blocking hardware I/O runs in a thread pool via
asyncio.to_thread() so the event loop stays free for HTTP routes.
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
    async def scan() -> str:
        """Read what's on the phone screen as a text listing — fastest view tool.

        Use whenever you need to know WHAT elements exist (icons + OCR text)
        without needing to see the screen visually. ~3s vs peek's ~4s. Use
        peek instead when planning a tap and you want pixel-level confirmation.

        Returns: plain-text element listing, one row per element:
            id [kind] "label" [x1,y1,x2,y2] conf
        where kind is "icon" or "text", bbox coords are 0-1 normalized.
        """
        listing = await asyncio.to_thread(physiclaw.scan)
        save_tool_call("scan", listing)
        return listing

    @mcp.tool()
    @logged
    async def peek() -> list:
        """Look at the phone via the overhead camera — default view tool for planning a tap.

        Call before any tap/swipe to confirm the target's bbox, and after
        to verify the screen changed. ~4s. Cheaper than `screenshot` and
        non-mutating (no side effects).

        Returns: [Image, listing] — a JPEG of the cropped camera view
        with icon bboxes drawn (so you can visually confirm what you'll
        tap), plus the same plain-text element listing `scan` returns.
        """
        jpeg, listing = await asyncio.to_thread(physiclaw.peek)
        save_tool_call("peek", listing, jpeg)
        return [Image(data=jpeg, format="jpeg"), listing]

    @mcp.tool()
    @logged
    async def screenshot() -> list:
        """Capture the phone's own screenshot (~12s, MUTATING) — only when peek/scan miss the target.

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

        After this, call `scan` or `peek` to verify the screen changed —
        scan is faster when you only need to confirm new text/icons
        appeared, peek when you also need to see them. If nothing changed,
        the stylus may have missed (retry once) or the bbox was wrong
        (re-orient with peek and pick a different element).
        """
        return await asyncio.to_thread(physiclaw.tap, bbox)

    @mcp.tool()
    @logged
    async def double_tap(bbox: Bbox) -> str:
        """Two quick taps (~150ms apart) at the center of `bbox` — for zooming or word-select.

        Use for: zooming maps / photos / web pages, or selecting a word
        in editable text. Don't use for buttons — that's `tap`.
        """
        return await asyncio.to_thread(physiclaw.double_tap, bbox)

    @mcp.tool()
    @logged
    async def long_press(bbox: Bbox) -> str:
        """Press and hold at the bbox center for ~1.2s — opens context menus, edit mode, paste menus.

        Use for: opening context menus, entering edit mode (rearrange
        icons), triggering the paste popover after `send_to_clipboard`.
        Always `scan` or `peek` after — the menu/popover that appears
        is what you tap next.
        """
        return await asyncio.to_thread(physiclaw.long_press, bbox)

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
        (swipe down from top-right). Always `scan` or `peek` after to
        see the new state.

        Common gotcha — direction is the STYLUS motion, not the page:
          - swipe UP   → page scrolls DOWN (reveals content below)
          - swipe DOWN → page scrolls UP (reveals content above / opens Notification Center)
          - swipe LEFT → page scrolls RIGHT (next photo / next page)

        Args:
            bbox: where to start the gesture (0-1 normalized).
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
        return await asyncio.to_thread(physiclaw.swipe, bbox, direction, size, speed)

    # ─── Navigate ────────────────────────────────────────────

    @mcp.tool()
    @logged
    async def home_screen() -> str:
        """Return to the iPhone home screen — exit any app from a known state.

        Issues the iPhone swipe-up-from-bottom gesture. Use to start a
        fresh task or recover from getting lost in app navigation. After
        this, `scan` or `peek` to see the home-screen icon layout.
        """
        return await asyncio.to_thread(physiclaw.home_screen)

    @mcp.tool()
    @logged
    async def go_back() -> str:
        """Pop back one screen via the iPhone swipe-from-left-edge gesture.

        Works in apps with a navigation stack (most do). If `scan` or
        `peek` after this shows the same screen, either the gesture
        didn't register (retry once) or this screen has no back action
        (modals, root tabs, lock screen) — try `home_screen` and
        re-enter, or look for an in-screen "Back" / "<" button to tap.
        """
        return await asyncio.to_thread(physiclaw.go_back)

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
        return await asyncio.to_thread(physiclaw.unlock_phone)

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
        return await asyncio.to_thread(physiclaw.send_to_clipboard, text)

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
        return await asyncio.to_thread(physiclaw.sequence, steps)
