"""Interpret a shadow response without authorizing or executing an action.

An offered gesture and target can form a valid tuple and still be the wrong
thing to do. These results are benchmark evidence, never live acceptance.
"""

from dataclasses import dataclass

from physiclaw.contract.decision import (
    ActionCandidate,
    DecisionRequest,
    DecisionResult,
    candidate_to_json,
)
from physiclaw.decision.guard import blocked_controls
from physiclaw.decision.policy import REVIEW_TOOLS


@dataclass(frozen=True, slots=True)
class ShadowInterpretation:
    reason: str
    action: ActionCandidate | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "action": (
                {"id": self.action.id, **candidate_to_json(self.action)}
                if self.action is not None
                else None
            ),
        }


def interpret(request: DecisionRequest, result: DecisionResult) -> ShadowInterpretation:
    """Bind answers to the original request and resolve only local geometry."""
    try:
        result.validate_for(request)
    except ValueError:
        return ShadowInterpretation("invalid_result")

    choices = {
        question.id: {choice.id: choice for choice in question.candidates}
        for question in request.questions
    }
    if (
        request.context.frame is None
        or set(choices) != {"phase", "action_type", "target"}
        or set(choices["phase"]) != {"ROUTINE", "SEMANTIC", "COMPLETE", "UNCLEAR"}
        or "YIELD" not in choices["action_type"]
        or not set(choices["action_type"]) <= {"YIELD", *REVIEW_TOOLS}
        or "NONE" not in choices["target"]
        or any(
            choice.tool is not None
            or (
                choice.target_ref is not None
                and (question.id != "target" or choice.id == "NONE")
            )
            for question in request.questions
            for choice in question.candidates
        )
    ):
        return ShadowInterpretation("invalid_profile")

    for target_id, choice in choices["target"].items():
        if target_id == "NONE":
            continue
        index = target_id.removeprefix("b")
        if (
            not target_id.startswith("b")
            or not index.isascii()
            or not index.isdecimal()
            or str(int(index)) != index
            or choice.target_ref != f"{request.context.observation_id}:{index}"
        ):
            return ShadowInterpretation("invalid_profile")

    if result.answers["phase"].selected_id != "ROUTINE":
        return ShadowInterpretation("non_routine")
    gesture = result.answers["action_type"].selected_id
    if gesture == "YIELD":
        return ShadowInterpretation("model_yield")
    target_id = result.answers["target"].selected_id
    if target_id == "NONE":
        return ShadowInterpretation("no_target")

    target = choices["target"][target_id]
    if target.target_ref in blocked_controls(request.context):
        return ShadowInterpretation("consequential_control")
    # DecisionRequest already binds target refs to the original observation.
    element = next(
        element
        for element in (*request.context.ocr, *request.context.icons)
        if element.ref == target.target_ref
    )
    return ShadowInterpretation(
        "hypothetical_action",
        ActionCandidate(
            id=target_id,
            description=f"Hypothetical {gesture} on observed element {target_id[1:]}.",
            tool=gesture,
            arguments={"bbox": element.bbox},
            target_ref=element.ref,
        ),
    )
