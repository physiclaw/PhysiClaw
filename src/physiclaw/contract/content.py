"""Shared message content: MCP observations and host turn metadata.

Convert incoming content before it enters history; providers receive DTOs
and only translate them to their API format.
"""

import base64
import json
import logging
from dataclasses import asdict

from physiclaw.common import image
from physiclaw.contract.dto import ContentBlock, ImageBlock, TextBlock, UserMessage
from physiclaw.contract.turn import TurnMetadata

log = logging.getLogger(__name__)


def turn_metadata_message(metadata: TurnMetadata) -> UserMessage:
    """Render host metadata as a fresh model-visible tail, outside its history."""
    return UserMessage(
        content="<turn_metadata>"
        + json.dumps(asdict(metadata), separators=(",", ":"))
        + "</turn_metadata>"
    )


def mcp_blocks_to_content_blocks(blocks: list[dict]) -> str | list[ContentBlock]:
    """MCP tool-result content blocks → engine `ContentBlock`s. Image bytes
    are scaled (JPEG + long-edge cap) on the way through; smaller payload
    means smaller history footprint thereafter.

    Returns a bare string when no images are present so common text-only
    tool results stay lightweight (providers fast-path string content).
    """
    # Fast path: single text block
    if len(blocks) == 1 and blocks[0].get("type") == "text":
        return blocks[0].get("text") or ""

    texts: list[str] = []
    images: list[ImageBlock] = []
    for b in blocks:
        kind = b.get("type")
        if kind == "text":
            texts.append(b.get("text") or "")
        elif kind == "image":
            try:
                raw = base64.b64decode(b["data"])
                scaled, mime = image.scale_image_bytes(raw)
                if mime == "application/octet-stream":
                    # The scaler kept the original on a codec failure;
                    # retain MCP's image type for the provider envelope.
                    mime = b.get("mime_type") or "image/jpeg"
                # Steady state returns `raw` itself (byte-identical
                # pass-through) — the b64 we already hold is the answer.
                new_b64 = (
                    b["data"] if scaled is raw else base64.b64encode(scaled).decode()
                )
            except image.ImageSizeError as exc:
                # The tool already ran. Preserve its outcome without forwarding
                # rejected pixels or turning an image failure into an action failure.
                texts.append(f"[Image omitted: {exc}]")
                continue
            except Exception:
                log.info("scale failed; forwarding original image", exc_info=True)
                new_b64 = b["data"]
                mime = b.get("mime_type") or "image/jpeg"
            images.append(ImageBlock(media_type=mime, data_b64=new_b64))

    if images:
        out: list[ContentBlock] = []
        if texts:
            out.append(TextBlock(text="\n".join(texts)))
        out.extend(images)
        return out
    return "\n".join(texts) if texts else "(empty tool result)"
