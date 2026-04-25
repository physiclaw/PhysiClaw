"""The session's working plan — pinned at the tail of every request.

The plan lives on `Session`, never in `messages[]`. Before each
`provider.chat(...)` call the engine appends `plan.render()` as a final
`UserMessage` so the model sees its current plan on every turn without
being lost in the scroll of tool_results. After the model responds, the
plan message is discarded; only the assistant reply lands in history.
This keeps the prefix byte-stable across turns — prefix cache still
hits everything above; only the short tail recomputes.

Mutation funnels through the `update_progress` tool (handler in
`builtin_tool.py`). Each step has a typed status (pending / in_progress
/ completed); Plan.update() enforces the invariant that at most ONE
step is `in_progress` at a time — matching Claude Code's TodoWrite rule.
"""
from dataclasses import dataclass, field

from physiclaw.agent.engine.dto import Message, UserMessage
from physiclaw.config import CONFIG


PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
STATUSES: frozenset[str] = frozenset({PENDING, IN_PROGRESS, COMPLETED})

STATUS_ICON = {PENDING: "- ", IN_PROGRESS: "▸ ", COMPLETED: "✓ "}

DEFAULT_OWNER_SAID = "(not yet read)"

DEFAULT_UNDERSTANDING = (
    "Unknown — open IM, read the latest message, then call update_progress."
)

DEFAULT_SEED_STEP = (
    "(no plan yet — after reading the owner's IM, call update_progress "
    "with the full step list through end_session; see CONVENTION § "
    "'The plan' for rules, update_progress docstring for the worked "
    "example)"
)


@dataclass
class Step:
    content: str
    status: str = PENDING


# Stay-silent window after a successful update_progress call. A legit
# multi-tap step (e.g. add-to-cart) can run 10-15 tap+peek turns, so a
# tip at turn 8 may fire mid-step — intentional: the tip is advisory,
# not rejecting, and reminding the model to re-check beats missing a
# real forgot-to-flip.
STALE_TICK_AFTER = CONFIG.engine.stale_tick_threshold
# When the plan is still in its default state this long into a wake, the
# model almost certainly forgot to call update_progress after reading the
# IM. Turn 0-1 is usually peek + (optional) skill-load, so 2 is the right
# cutoff — earlier would false-positive, later would let drift compound.
DEFAULT_STATE_AFTER = CONFIG.engine.state_decay_turns


@dataclass
class Plan:
    owner_said: str = DEFAULT_OWNER_SAID
    understanding: str = DEFAULT_UNDERSTANDING
    steps: list[Step] = field(
        default_factory=lambda: [Step(DEFAULT_SEED_STEP)]
    )
    turns_since_update: int = 0

    def tick_turn(self) -> None:
        """Engine calls this once per turn (before `inject_tail`) so
        `render()` can surface a staleness tip when the model forgets to
        call update_progress."""
        self.turns_since_update += 1

    def update(
        self,
        *,
        owner_said: str | None = None,
        understanding: str | None = None,
        steps: list[dict] | None = None,
    ) -> None:
        if owner_said is None and understanding is None and steps is None:
            raise ValueError(
                "update needs at least one of owner_said / understanding / steps"
            )
        # Validate everything before mutating — partial updates on failure
        # leave the plan in a confusing mixed state.
        parsed_steps: list[Step] | None = None
        if steps is not None:
            parsed_steps = [Step(content=s["content"], status=s["status"]) for s in steps]
            active = [s for s in parsed_steps if s.status == IN_PROGRESS]
            if len(active) > 1:
                names = ", ".join(repr(s.content) for s in active)
                raise ValueError(
                    f"{len(active)} steps in_progress ({names}); "
                    "exactly one step may be in_progress at a time — "
                    "mark the others pending or completed"
                )
        if owner_said is not None:
            self.owner_said = owner_said.strip()
        if understanding is not None:
            self.understanding = understanding.strip()
        if parsed_steps is not None:
            self.steps = parsed_steps
        self.turns_since_update = 0

    def render(self) -> str:
        done = sum(1 for s in self.steps if s.status == COMPLETED)
        total = len(self.steps)
        step_lines = [f"  {STATUS_ICON[s.status]}{s.content}" for s in self.steps]
        if not step_lines:
            step_lines = ["  (none)"]
        lines = [
            "<plan>",
            f"Owner said: {self.owner_said}",
            f"My understanding: {self.understanding}",
            f"Progress: {done}/{total}",
            "Steps:",
            *step_lines,
        ]
        tip = self._tip()
        if tip:
            lines.append(tip)
        lines.append("</plan>")
        return "\n".join(lines)

    def _tip(self) -> str | None:
        """Contextual reminder line appended to render() when a signal
        fires. Silent when the plan is fresh — the model learns to ignore
        messages that always appear."""
        is_default = self.owner_said == DEFAULT_OWNER_SAID
        if is_default and self.turns_since_update >= DEFAULT_STATE_AFTER:
            return (
                f"⚠ Plan still default after {self.turns_since_update} "
                "turns — read the owner's IM and call update_progress."
            )
        if not is_default and self.turns_since_update >= STALE_TICK_AFTER:
            return (
                f"⚠ {self.turns_since_update} turns since last "
                "update_progress. If the current step's intent is "
                "achieved, flip its status now; if you're stuck, re-plan."
            )
        return None


def inject_tail(messages: list[Message], plan: Plan) -> list[Message]:
    """Return `messages + [plan-tail UserMessage]`. Original list untouched."""
    return messages + [UserMessage(content=plan.render())]
