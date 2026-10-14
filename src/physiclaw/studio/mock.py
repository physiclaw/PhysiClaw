"""A recorded session standing in for the phone — `physiclaw studio
--session <id>`.

The page is checked against real frames without a rig or an MCP server:
every screen view a session logged (the annotated JPEG beside its full
listing — off the tool results the trace kept, a walk's turns included;
off the wire log for a session recorded before it kept them) becomes
one frame, and `MockSession`
serves them behind the same `Session` door `StudioSession` has. A peek
shows the current frame; a gesture advances to the next one, so
tapping through the page walks the recorded session in order.
"""

import asyncio
import base64
import json
from dataclasses import dataclass
from pathlib import Path

from physiclaw.common import gesture_vocab
from physiclaw.common.listing import is_screen
from physiclaw.common.logger.session_artifacts import mime_for, resolve_image
from physiclaw.common.text import iter_jsonl
from physiclaw.contract.wire import image_ref, iter_request_messages
from physiclaw.studio.session import unpublished, view_reply

# What the mock publishes: the gesture vocabulary, the camera view, and
# the clipboard. Not `screenshot` — a recording holds no phone capture,
# and the page greys out what a session does not publish.
MOCK_TOOLS = frozenset(
    gesture_vocab.PRESS_TOOLS
    | gesture_vocab.NAV_TOOLS
    | {
        gesture_vocab.PEEK,
        gesture_vocab.SWIPE,
        gesture_vocab.UNLOCK_PHONE,
        gesture_vocab.SEND_TO_CLIPBOARD,
    }
)


@dataclass(frozen=True)
class Frame:
    image: Path
    listing: str


def session_frames(session_dir: Path) -> list[Frame]:
    """The screen views in a recorded session, in order: the
    image a tool reply attached and the full listing beside it — the
    tool results' own record when the session kept it, else the wire
    log's requests (older views there are superseded to labels-only
    stubs, so each request contributes its latest view; the same view
    carried into the next request is not a second frame). Consecutive
    copies of a listing collapse; returning to a previous screen is kept."""
    frames = _event_frames(session_dir)
    if frames:
        return frames
    wire = session_dir / "wire.jsonl"
    if not wire.exists():
        raise FileNotFoundError(f"no events.jsonl views or wire.jsonl in {session_dir}")
    for role, blocks in iter_request_messages(wire):
        if role == "system":  # doctrine quotes the header; never a view
            continue
        # One view per tool reply: its image and its listing travel in
        # the same message (either order, by provider shape).
        refs = [r for b in blocks if (r := image_ref(b)) is not None]
        screens = [
            b["text"]
            for b in blocks
            if b.get("type") == "text" and is_screen(b["text"])
        ]
        for ref, listing in zip(refs, screens):
            _append_frame(frames, session_dir, ref, listing)
    return frames


def _event_frames(session_dir: Path) -> list[Frame]:
    """The views the session's `tool_result` events carry: the result's
    text whole (a screen) beside the frame it filed."""
    frames: list[Frame] = []
    for rec in iter_jsonl(session_dir / "events.jsonl", '"tool_result"'):
        text, refs = rec.get("text"), rec.get("images")
        if rec.get("event") != "tool_result" or not isinstance(refs, list) or not refs:
            continue
        if not isinstance(text, str) or not is_screen(text):
            continue
        _append_frame(frames, session_dir, refs[0], text)
    return frames


def _append_frame(
    frames: list[Frame], session_dir: Path, ref: object, listing: str
) -> None:
    if frames and frames[-1].listing == listing:
        return
    image = resolve_image(session_dir, ref)
    if image is not None:
        frames.append(Frame(image=image, listing=listing))


class MockSession:
    """`Session` over recorded frames. Never dials anything. `call_tool`
    is the MCP-shaped door (blocks out), so a driver lent the session
    via `drive` — a stepped playbook node — walks the frames the way a
    session's actions once did."""

    def __init__(self, frames: list[Frame], label: str):
        if not frames:
            raise ValueError(f"{label}: no screen views recorded")
        self.frames = frames
        self.label = label
        self.idx = 0
        # Lent to a driver: manual gestures answer busy meanwhile, the
        # one-rig rule the real session keeps with its lock.
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def mcp_url(self) -> str:
        return f"mock:{self.label}"

    def state(self) -> dict:
        return {"connected": True, "mcp_url": self.mcp_url, "tools": sorted(MOCK_TOOLS)}

    async def act(self, tool: str, args: dict) -> dict:
        async with self._lock:
            return view_reply(await self.call_tool(tool, args))

    async def drive(self, job):
        async with self._lock:
            return await job(self)

    async def call_tool(self, tool: str, args: dict | None = None) -> list[dict]:
        args = args or {}
        if tool not in MOCK_TOOLS:
            raise unpublished(tool)
        if tool == gesture_vocab.SEND_TO_CLIPBOARD:
            return [{"type": "text", "text": f"(mock) copied {args.get('text', '')!r}"}]
        if tool == gesture_vocab.PEEK:
            return self._view()
        self.idx = (self.idx + 1) % len(self.frames)
        text = (
            f"(mock) {tool} {json.dumps(args, ensure_ascii=False)} | "
            f"frame {self.idx + 1}/{len(self.frames)}"
        )
        return [{"type": "text", "text": text}, *self._view()]

    def _view(self) -> list[dict]:
        frame = self.frames[self.idx]
        data = base64.b64encode(frame.image.read_bytes()).decode()
        return [
            {"type": "image", "mime_type": mime_for(frame.image.name), "data": data},
            {"type": "text", "text": frame.listing},
        ]

    async def close(self) -> None:
        return None
