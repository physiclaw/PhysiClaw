"""Read-only projection of explicit task outputs and actual screen evidence.

No access to an LLM conversation, scratchpad or provider. Text is copied,
not summarized, translated or parsed into invented app/query parameters.
In particular, a clipboard receipt is an action fact, never query intent.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass

from physiclaw.common import verdict
from physiclaw.common.gesture_vocab import (
    NAV_TOOLS,
    PEEK,
    PRESS_TOOLS,
    RUN_MACRO,
    SCREENSHOT,
    SEND_TO_CLIPBOARD,
    SEQUENCE,
    SWIPE,
    UNLOCK_PHONE,
)
from physiclaw.contract.decision import (
    DecisionContext,
    DecisionElement,
    DecisionRequest,
    context_to_json,
)
from physiclaw.contract.turn import IN_PROGRESS, ActionEvent, TurnInput, TurnMetadata
from physiclaw.decision.guard import consequential_step
from physiclaw.decision.policy import QuestionProfile, question_profile

_INTERACTIONS = PRESS_TOOLS | NAV_TOOLS | {SWIPE, SEQUENCE, RUN_MACRO, UNLOCK_PHONE}
_RECEIPTS = _INTERACTIONS | {PEEK, SCREENSHOT, SEND_TO_CLIPBOARD}


def _action_failed(event: ActionEvent) -> bool:
    if event.is_error or not event.executed:
        return True
    if verdict.failure_line(event.tool, event.result_text or "") is not None:
        return True
    if event.tool == RUN_MACRO and event.result_blocks:
        metadata = event.result_blocks[0].get("_meta")
        return (
            isinstance(metadata, Mapping)
            and metadata.get(verdict.MACRO_OK_META) is False
        )
    return False


@dataclass(frozen=True, slots=True)
class ContextProjection:
    """An inspectable preparation result, never an execution acceptance."""

    context: DecisionContext | None
    blockers: tuple[str, ...]
    profile: QuestionProfile

    def to_dict(self) -> dict:
        context = self.context
        frame = context.frame if context is not None else None
        return {
            "blockers": list(self.blockers),
            "state": context_to_json(context) if context is not None else None,
            "image": {
                "observation_id": frame.id,
                "sha256": frame.sha256,
                "media_type": frame.media_type,
                "width": frame.width,
                "height": frame.height,
            }
            if frame is not None
            else None,
            "profile": self.profile.to_dict(),
        }

    def request(self, request_id: str) -> DecisionRequest | None:
        """Bind the same previewed questions to this context; blocked work yields."""
        if self.context is None or self.blockers or not self.profile.questions:
            return None
        return DecisionRequest(request_id, self.context, self.profile.questions)

    def preview(self) -> str:
        """Render the canonical projected data, without a second summary."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def project(turn: TurnInput, *, metadata: TurnMetadata) -> ContextProjection:
    """Keep missing data unknown; preserve all same-frame OCR/icon evidence."""
    progress = turn.progress
    steps = progress.steps if progress is not None else ()
    current = next(
        (step for step in steps if step.status == IN_PROGRESS),
        None,
    )
    blockers = []
    if current is None:
        blockers.append("no_active_step")
    if turn.observation_issue is not None:
        blockers.append(turn.observation_issue)
    if turn.observation is None:
        blockers.append("no_observation")
    elif turn.observation.source == "screenshot":
        # The capture gesture can change the screen after the captured frame.
        blockers.append("screenshot_requires_peek")
    publication = next(
        (event for event in reversed(turn.events) if event.tool == "update_progress"),
        None,
    )
    if publication is not None and (publication.is_error or not publication.executed):
        blockers.append("progress_update_failed")

    # Check the last interaction and any later clipboard write independently.
    # Peeks cannot clear either failure.
    previous = latest_clipboard = None
    for event in reversed(turn.events):
        if event.tool in _INTERACTIONS:
            previous = event
            break
        if latest_clipboard is None and event.tool == SEND_TO_CLIPBOARD:
            latest_clipboard = event
    if previous is not None:
        if _action_failed(previous):
            blockers.append("previous_action_failed")
        elif previous.changed is not True:
            blockers.append(
                "previous_action_unchanged"
                if previous.changed is False
                else "previous_outcome_unknown"
            )

    if latest_clipboard is not None and _action_failed(latest_clipboard):
        blockers.append("clipboard_update_failed")

    context = None
    if observation := turn.observation:
        recent = []
        for event in reversed(turn.events):
            if event.tool in _RECEIPTS:
                recent.append(event)
                if len(recent) == 3:
                    break
        recent.reverse()
        if previous is not None and previous not in recent:
            recent.insert(0, previous)
        context = DecisionContext(
            observation_id=observation.id,
            frame=observation.frame,
            ocr=tuple(
                DecisionElement(observation.element_ref(e), e.bbox, e.label)
                for e in observation.ocr
            ),
            icons=tuple(
                DecisionElement(observation.element_ref(e), e.bbox)
                for e in observation.icons
            ),
            facts={
                "turn": metadata.turn,
                "task": progress.user_said if progress is not None else None,
                "current_step": current.content if current is not None else None,
                "recent_actions": [
                    {
                        "tool": event.tool,
                        "arguments": event.arguments,
                        "executed": event.executed,
                        "is_error": event.is_error,
                        "changed": event.changed,
                    }
                    for event in recent
                ],
            },
        )
        if consequential_step(context):
            blockers.append("consequential_step")
    profile = QuestionProfile()
    if context is not None and not blockers:
        profile = question_profile(context, tools=turn.tools.keys())
    return ContextProjection(context, tuple(blockers), profile)
