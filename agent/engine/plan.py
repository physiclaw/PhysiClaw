"""The session's working plan — pinned at the tail of every request.

The plan lives on `Session`, never in `messages[]`. Before each
`provider.chat(...)` call the engine appends `plan.render()` as a final
user-role message so the model sees its current plan on every turn
without being lost in the scroll of tool_results. After the model
responds, the plan message is discarded; only the assistant reply lands
in history. This keeps the prefix byte-stable across turns — prefix
cache still hits everything above; only the short tail recomputes.

Mutation funnels through the `update_plan` tool (handler in
`builtin_tool.py`). Default seed on wake says "owner's IM hasn't been
checked yet" so the first turn has a clear starting task.
"""
from dataclasses import dataclass, field


DEFAULT_OWNER_SAID = "(not yet observed — open IM and read)"

DEFAULT_UNDERSTANDING = (
    "Owner's IM hasn't been checked this wake. First job: open the IM, "
    "read the most recent message, then call update_plan with the real goal."
)

DEFAULT_STEPS: tuple[str, ...] = (
    "1. Open the IM app and read the latest message.",
    "2. Call update_plan with owner_said + understanding + concrete steps.",
    "3. Execute the plan, verifying each step via scan/peek.",
    "4. Reply in IM, then append_log, then end_session.",
)


@dataclass
class Plan:
    owner_said: str = DEFAULT_OWNER_SAID
    understanding: str = DEFAULT_UNDERSTANDING
    steps: list[str] = field(default_factory=lambda: list(DEFAULT_STEPS))

    def update(
        self,
        *,
        owner_said: str | None = None,
        understanding: str | None = None,
        steps: list[str] | None = None,
    ) -> None:
        if owner_said is None and understanding is None and steps is None:
            raise ValueError(
                "update needs at least one of owner_said / understanding / steps"
            )
        if owner_said is not None:
            self.owner_said = owner_said.strip()
        if understanding is not None:
            self.understanding = understanding.strip()
        if steps is not None:
            self.steps = [s.strip() for s in steps if s and s.strip()]

    def render(self) -> str:
        step_lines = [f"  {s}" for s in self.steps] or ["  (none)"]
        lines = [
            "<plan>",
            f"Owner said: {self.owner_said}",
            f"My understanding: {self.understanding}",
            "Steps:",
            *step_lines,
            "</plan>",
        ]
        return "\n".join(lines)


def inject_tail(messages: list[dict], plan: Plan) -> list[dict]:
    """Return `messages + [plan-tail user message]`. Original list untouched."""
    return messages + [{"role": "user", "content": plan.render()}]
