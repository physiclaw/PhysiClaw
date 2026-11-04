"""Agent-owned prompt and conversation policy, selected without API clients.

These are application tuning choices, not vendor protocol requirements.
Cache markers and native schema translation remain inside provider adapters.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CollapsePolicy:
    """Validated cadence for the LLM producer's conversation compaction.

    first_at is the initial complete-turn threshold; keep is the number of
    recent turns retained; interval controls subsequent collapses.
    """

    first_at: int
    keep: int
    interval: int

    def __post_init__(self) -> None:
        if any(
            type(count) is not int
            for count in (self.first_at, self.keep, self.interval)
        ):
            raise ValueError("CollapsePolicy: counts must be integers")
        if not 1 <= self.keep <= self.first_at:
            raise ValueError(
                f"CollapsePolicy: keep={self.keep} must be in "
                f"[1, first_at={self.first_at}] — the collapse cuts at "
                "turn_starts[-keep] once first_at turns accumulate"
            )
        if self.interval < 1:
            raise ValueError(f"CollapsePolicy: interval={self.interval} must be >= 1")


# Keep the existing cadence for every vendor. Whole-prefix caches make a
# collapse more expensive, but longer intervals hurt long-session quality.
DEFAULT_COLLAPSE = CollapsePolicy(first_at=30, keep=10, interval=20)


@dataclass(frozen=True)
class ContextPolicy:
    """Prompt content and conversation retention choices for the LLM producer."""

    collapse: CollapsePolicy = DEFAULT_COLLAPSE
    inline_tool_index: bool = True


DEFAULT_CONTEXT_POLICY = ContextPolicy()
_ANTHROPIC_POLICY = ContextPolicy(inline_tool_index=False)


def context_policy_for(provider_id: str) -> ContextPolicy:
    """Preserve the established defaults, including unknown-provider prompts."""
    return _ANTHROPIC_POLICY if provider_id == "anthropic" else DEFAULT_CONTEXT_POLICY
