"""Data-transfer objects — the single shape the engine loop operates on.

Every provider serializes tool_calls, finish reasons, and assistant content
differently. These DTOs normalize that: `provider.py` coerces in and out,
and strips provider-specific leakage (e.g. Qwen's `reasoning_content`)
before assistant messages are echoed back into history.
"""
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FinishReason(StrEnum):
    """Provider-normalized stop cause. Principle 3: preserve the real
    cause — do not derive it from content. Values equal their string form
    so equality checks against OpenAI-style strings still work."""

    TOOL_CALLS = "tool_calls"
    STOP = "stop"
    LENGTH = "length"             # truncated — arguments may be incomplete
    CONTENT_FILTER = "content_filter"
    ERROR = "error"               # our own: parse / transport failures


@dataclass(frozen=True)
class ToolCall:
    """One call the model made. Arguments already parsed from JSON."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class AssistantMessage:
    """A single turn's assistant output, normalized across providers.

    `content` may be empty when the model only called tools. `raw` retains
    the provider's original response dict for trace replay / debugging.
    """
    content: str
    tool_calls: list[ToolCall]
    finish_reason: FinishReason
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """One result, paired to a ToolCall. Principle 6: every tool_call must
    have exactly one ToolResult with the same id in the very next message.
    """
    tool_call_id: str
    content: str | list[dict[str, Any]]   # text or multimodal content blocks
    is_error: bool = False


__all__ = ["FinishReason", "ToolCall", "AssistantMessage", "ToolResult"]
