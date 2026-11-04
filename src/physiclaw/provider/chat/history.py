"""Pure DTO-to-wire history traversal and cache-marker placement.

Callers own conversation retention. Encoders and marker strategies own
native representation; this traversal preserves ordering without mutating
messages or performing I/O.
"""

from collections.abc import Callable

from physiclaw.contract.content import turn_metadata_message
from physiclaw.contract.dto import (
    ImageBlock,
    Message,
    OutputResultMessage,
    SystemMessage,
    ToolResultMessage,
)
from physiclaw.contract.turn import TurnMetadata

# Shared native cache marker; treat as immutable.
EPHEMERAL_CACHE_CONTROL = {"type": "ephemeral"}


class CacheMarkers:
    """Provider-owned marker shapes and retained stub count.

    History traversal supplies the system, superseded results and tail
    anchors. Providers mark only the placements their cache rewards.
    Hooks return shallow copies without mutating caller-owned entries;
    identity defaults disable marking for automatic-cache providers.
    """

    stub_count: int = 1

    def mark_system(self, entry: dict) -> dict:
        return entry

    def mark_stub(self, entry: dict) -> dict:
        return entry

    def mark_tail(self, entry: dict) -> dict:
        return entry


NO_CACHE_MARKERS = CacheMarkers()


def _has_image(content) -> bool:
    """True iff a tool result's DTO content carries an `ImageBlock` —
    i.e. it is (or was, until superseded) a live screen capture."""
    return isinstance(content, list) and any(isinstance(b, ImageBlock) for b in content)


def _tail_anchor_indexes(results: list[tuple[int, bool]]) -> list[int]:
    """The moving tail anchors for one request, from `serialize_history`'s
    (out-index, carries-image) record of every tool result: the last
    result BEFORE the live screen (survives the next supersede rewrite)
    and the last result overall (advances every turn, dies on the next
    supersede). Deduped; one entry when they coincide or when no live
    screen exists."""
    anchors: list[int] = []
    images = [i for i, has in results if has]
    if images:
        before = [i for i, _ in results if i < images[-1]]
        if before:
            anchors.append(before[-1])
    if results:
        anchors.append(results[-1][0])
    return list(dict.fromkeys(anchors))


def serialize_history(
    history: list[Message],
    *,
    encode_message: Callable[[Message], dict | list[dict] | None],
    markers: CacheMarkers,
    metadata: TurnMetadata | None = None,
) -> list[dict]:
    """Serialize history and apply the provider's cache policy.

    Candidates are the leading system message, latest `stub_count`
    superseded results, and tail anchors around the live screen.
    Provider hooks choose which candidates to mark; volatile user
    tails and host metadata follow the anchors.

    Encoders may return one entry, several entries, or None to skip
    a message. Split results keep all receipts ahead of their image
    followups so parallel calls stay paired. Markers attach to the
    first entry; superseded results contain no images.
    """
    if metadata is not None:
        # A fresh tail keeps producer metadata visible to the model without
        # mutating its conversation or importing another producer's context.
        history = [*history, turn_metadata_message(metadata)]
    out: list[dict] = []
    tool_followups: list[dict] = []
    stub_indexes: list[int] = []
    results: list[tuple[int, bool]] = []  # (out index, carries image)
    for i, msg in enumerate(history):
        is_result = isinstance(msg, (ToolResultMessage, OutputResultMessage))
        if not is_result and tool_followups:
            out.extend(tool_followups)
            tool_followups.clear()
        entries = encode_message(msg)
        if entries is None:
            continue
        if isinstance(entries, dict):
            entries = [entries]
        if not entries:
            continue
        if i == 0 and isinstance(msg, SystemMessage):
            entries[0] = markers.mark_system(entries[0])
        elif isinstance(msg, (ToolResultMessage, OutputResultMessage)):
            results.append((len(out), _has_image(msg.content)))
            if msg.is_superseded:
                stub_indexes.append(len(out))
            tool_followups.extend(entries[1:])
            entries = entries[:1]
        out.extend(entries)
    out.extend(tool_followups)
    marked_stubs = stub_indexes[-markers.stub_count :]
    for idx in marked_stubs:
        out[idx] = markers.mark_stub(out[idx])
    for idx in _tail_anchor_indexes(results):
        if idx not in marked_stubs:
            out[idx] = markers.mark_tail(out[idx])
    return out
