"""The wire.jsonl text codec — scrubber and reader, one home.

`RawLog` (agent.trace.rawlog) owns the sink mechanics: session dirs,
image files, fail-open writes. The *format* knowledge lives here: which
content-block shapes carry text or images on the wire. Writer and
reader encode the same two shapes (top-level blocks vs Anthropic
`tool_result` blocks whose `content` nests its own block list), so a
provider shape added to one must be added to the other in the same
edit — which is why they share this module instead of splitting across
the packages that call them.
"""

import json
from typing import Any, Callable, Iterable, Iterator

# persist(mime, b64_data) -> path relative to the session dir, or "" on
# decode failure (the scrub falls back to a byte-count stub so the raw
# log still distinguishes an image from a tap result).
PersistImage = Callable[[str, str], str]


def scrub_messages(messages: list[dict], persist: PersistImage) -> list[dict]:
    """Copy of `messages` with inline base64 image data replaced by a
    reference `persist` returns. `persist` is asked once per occurrence;
    whether a screen still in context maps to one file or a fresh one
    each request is the callback's policy (the session store keeps one
    file per frame).

    Handles two wire shapes (recognized at the block level, not the
    provider level):

      - OpenAI: `{"type": "image_url", "image_url": {"url": "data:..."}}`
      - Anthropic: `{"type": "image", "source": {"type": "base64",
        "media_type": "...", "data": "..."}}`
    """
    out: list[dict] = []
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list):
            out.append(m)
            continue
        new_c: list[dict] = []
        for b in c:
            if isinstance(b, dict):
                new_c.append(scrub_block(b, persist))
            else:
                new_c.append(b)
        out.append({**m, "content": new_c})
    return out


def scrub_block(b: dict, persist: PersistImage) -> dict:
    """Scrub one content block; handles OpenAI `image_url`, Anthropic
    `image`, and Anthropic `tool_result` (whose nested `content` may
    itself contain image blocks). Pass-through for everything else
    (text, tool_use, …)."""
    bt = b.get("type")
    if bt == "image_url":
        url = (b.get("image_url") or {}).get("url", "")
        if not url.startswith("data:"):
            return b
        head, _, data = url.partition(",")
        mime = head[5:].partition(";")[0]
        rel = persist(mime, data) if data else ""
        scrubbed = rel or f"{head},<{len(data)}b unreadable>"
        return {"type": "image_url", "image_url": {"url": scrubbed}}
    if bt == "image":
        src = b.get("source") or {}
        if src.get("type") != "base64":
            return b
        data = src.get("data") or ""
        mime = src.get("media_type") or "image/jpeg"
        rel = persist(mime, data) if data else ""
        scrubbed_src = (
            {"type": "ref", "ref": rel}
            if rel
            else {"type": "base64", "byte_count": len(data)}
        )
        return {"type": "image", "source": scrubbed_src}
    if bt == "tool_result":
        inner = b.get("content")
        if isinstance(inner, list):
            scrubbed_inner = [
                scrub_block(x, persist) if isinstance(x, dict) else x for x in inner
            ]
            return {**b, "content": scrubbed_inner}
        return b
    return b


def iter_request_messages(path) -> Iterator[tuple[str, list[dict]]]:
    """Every ``(message_role, leaf blocks)`` in a wire.jsonl's request
    records — the reader beside the writer above. A string content is
    one text block; a ``tool_result``'s nested content is flattened so
    a consumer sees the image and listing blocks it carries in order.
    Streams the file; skips unparseable lines."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("kind") != "request":
                continue
            for msg in rec.get("messages", ()):
                yield msg.get("role", ""), list(leaf_blocks(msg.get("content")))


def iter_request_texts(path) -> Iterator[tuple[str, str]]:
    """Every ``(message_role, text)`` in a wire.jsonl's request records."""
    for role, blocks in iter_request_messages(path):
        for b in blocks:
            if b.get("type") == "text":
                yield role, b.get("text", "")


def image_ref(block: dict) -> str | None:
    """The persisted image path a scrubbed block carries (relative to
    the session dir), for either wire shape — the reader half of
    `scrub_block`. None for any other block, or an unscrubbed data URL."""
    bt = block.get("type")
    if bt == "image_url":
        url = (block.get("image_url") or {}).get("url", "")
        return url if url and not url.startswith("data:") else None
    if bt == "image":
        src = block.get("source") or {}
        return src.get("ref") or None if src.get("type") == "ref" else None
    return None


def leaf_blocks(content: Any) -> Iterable[dict]:
    """A message's content as a flat run of typed blocks, whichever wire
    shape: a bare string is one text block, a `tool_result` opens into
    its own blocks. The one flattening every reader of the wire uses."""
    if isinstance(content, str):
        yield {"type": "text", "text": content}
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            yield from leaf_blocks(block.get("content"))
        else:
            yield block


def reply_gist(raw: dict) -> dict[str, Any] | None:
    """What a raw provider reply said, in one shape whichever wire it
    came over: `text`, `calls` ([{name, args}]), `thinking`. None when
    the dict is neither the OpenAI nor the Anthropic shape — the caller
    shows it verbatim."""
    try:
        choices = raw.get("choices")
        if choices:  # the OpenAI shape
            msg = choices[0]["message"]
            content = msg.get("content")
            if not content:
                text = ""
            elif isinstance(content, str):
                text = content
            else:
                text = json.dumps(content, ensure_ascii=False)
            calls = [tc.get("function") or {} for tc in msg.get("tool_calls") or []]
            return {
                "text": text,
                "calls": [
                    {"name": fn.get("name", "?"), "args": fn.get("arguments", "")}
                    for fn in calls
                ],
                "thinking": str(msg.get("reasoning_content") or ""),
            }
        content = raw.get("content")
        if isinstance(content, list):  # the Anthropic shape
            blocks = [b for b in content if isinstance(b, dict)]
            return {
                "text": "\n".join(
                    str(b.get("text", "")) for b in blocks if b.get("type") == "text"
                ),
                "calls": [
                    {"name": b.get("name", "?"), "args": b.get("input")}
                    for b in blocks
                    if b.get("type") == "tool_use"
                ],
                "thinking": "\n".join(
                    str(b.get("thinking", ""))
                    for b in blocks
                    if b.get("type") == "thinking"
                ),
            }
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    return None
