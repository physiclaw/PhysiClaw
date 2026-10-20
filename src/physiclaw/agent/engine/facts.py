"""Host-owned observation ingress and a neutral execution journal."""

import logging
from dataclasses import dataclass, field, replace

from physiclaw.common.gesture_vocab import (
    NAV_TOOLS,
    PEEK,
    PRESS_TOOLS,
    RUN_MACRO,
    SCREENSHOT,
    SEQUENCE,
    SWIPE,
    UNLOCK_PHONE,
)
from physiclaw.contract.dto import ToolCall
from physiclaw.contract.observation import Observation, read_observation
from physiclaw.contract.turn import ActionEvent, TurnInput
from physiclaw.macros.model import MACRO_OK_META

log = logging.getLogger(__name__)

SCREEN_CALLS = (
    PRESS_TOOLS
    | NAV_TOOLS
    | {SWIPE, SEQUENCE, UNLOCK_PHONE, RUN_MACRO, PEEK, SCREENSHOT}
)
# Notes/scratchpad and model planning stay private to the producer context.
CONTEXT_CALLS = frozenset({"note", "update_progress"})


@dataclass
class FactualState:
    observation: Observation | None = None
    observation_issue: str | None = "not_observed"
    events: list[ActionEvent] = field(default_factory=list)
    _seen_observations: set[str] = field(default_factory=set)
    _sequence: int = 0

    def begin(self, tool: str) -> None:
        if tool in SCREEN_CALLS:
            self.observation = None
            self.observation_issue = "awaiting_view"

    def observe(self, blocks: list[dict], *, tool: str) -> None:
        try:
            observation = read_observation(blocks)
            if observation is None:
                return
            if observation.id in self._seen_observations:
                raise ValueError("replayed observation identity")
            self._seen_observations.add(observation.id)
            if (
                tool == RUN_MACRO
                and (blocks[0].get("_meta") or {}).get(MACRO_OK_META) is not True
            ):
                # Aborted macros can retain pre-failure views; use the typed
                # outcome rather than parsing warning prose.
                self.observation = None
                self.observation_issue = "unverified_macro_view"
                return
            self.observation = observation
            self.observation_issue = None
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            # Evidence failure must not discard the actual tool result.
            self.observation = None
            self.observation_issue = "invalid_view"
            log.warning("factual observation rejected: %s", exc)

    def record(
        self,
        call: ToolCall,
        *,
        turn: int,
        changed: bool | None,
        failed: bool,
        result_text: str | None = None,
        result_blocks: list[dict] | None = None,
        executed: bool = True,
        notices: tuple[str, ...] = (),
    ) -> None:
        private = call.name in CONTEXT_CALLS
        if private:
            # Acknowledgements pair a producer's own tool calls without sharing
            # another producer's notes, plan text or scratchpad.
            result_text = "context update failed" if failed else "context updated"
            result_blocks = None
            notices = ()
        self._sequence += 1
        try:
            event = ActionEvent(
                sequence=self._sequence,
                turn=turn,
                call_id=call.id,
                tool=call.name,
                arguments={} if private else call.arguments,
                is_error=failed,
                changed=changed,
                observation_id=self.observation.id
                if self.observation is not None
                else None,
                result_text=result_text,
                result_blocks=tuple(result_blocks)
                if result_blocks is not None
                else (
                    ({"type": "text", "text": result_text},)
                    if result_text is not None
                    else ()
                ),
                executed=executed,
                notices=notices,
            )
        except (ValueError, TypeError) as exc:
            self.observation = None
            self.observation_issue = "invalid_action_event"
            self.events.clear()
            log.warning("factual action evidence rejected: %s", exc)
            return
        if any(block.get("type") == "image" for block in event.result_blocks):
            # Keep all receipts so inactive producers can settle pending calls,
            # but retain image bytes only for the latest result.
            for index in range(len(self.events) - 1, -1, -1):
                previous = self.events[index]
                if any(
                    block.get("type") == "image" for block in previous.result_blocks
                ):
                    self.events[index] = replace(
                        previous,
                        result_blocks=tuple(
                            block
                            for block in previous.result_blocks
                            if block.get("type") != "image"
                        ),
                    )
                    break
        self.events.append(event)

    def snapshot(
        self,
        *,
        schemas: dict[str, dict],
    ) -> TurnInput:
        return TurnInput(
            observation=self.observation,
            observation_issue=self.observation_issue,
            events=tuple(self.events),
            tools={
                name: schema.get("input_schema") or {}
                for name, schema in schemas.items()
            },
        )
