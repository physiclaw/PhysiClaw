"""Reading tool results out of the transcript — the conductor's eyes.

The conductor never actuates and never holds an MCP client; everything
it knows about the world arrives as ordinary ``ToolResultMessage``s in
the session history. These three readers are the whole vocabulary: the
result of one specific synthesized call (the walk's view, via
`Turnsmith.settle`), and a result's text/screen extraction. One home,
so every reader of the transcript reads it identically.
"""

from physiclaw.common.listing import Screen
from physiclaw.contract.dto import ImageBlock, Message, TextBlock, ToolResultMessage


def result_for(history: list[Message], call_id: str) -> ToolResultMessage | None:
    for msg in reversed(history):
        if isinstance(msg, ToolResultMessage) and msg.tool_call_id == call_id:
            return msg
    return None


def screen_of(result: ToolResultMessage) -> Screen:
    """The screen a tool result carries — its text blocks parsed as a
    listing (macro results and peeks both end with the current view)."""
    return Screen.read(text_of(result))


def text_of(result: ToolResultMessage) -> str:
    """All text of a tool result, joined."""
    if isinstance(result.content, str):
        return result.content
    return "\n".join(b.text for b in result.content if isinstance(b, TextBlock))


def frame_of(result: ToolResultMessage) -> ImageBlock | None:
    """The frame a tool result carries — the view's image, the same
    bytes the model's own turn would see beside the listing. None for a
    text-only result (a failed read, a replayed or rehearsed screen)."""
    if isinstance(result.content, str):
        return None
    return next(
        (b for b in reversed(result.content) if isinstance(b, ImageBlock)), None
    )
