"""Interpret responses and gate proposals without executing actions.

Raw proposals remain separate from probability gates so a local rejection
cannot hide a model mistake. Probability floors are not proof of safety.
"""

from copy import deepcopy
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
class DecisionInterpretation:
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


@dataclass(frozen=True, slots=True)
class ProbabilityThresholds:
    """Independent probability floors; these are not calibrated guarantees."""

    phase: float
    action_type: float
    target: float

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 1
            for value in self.to_dict().values()
        ):
            raise ValueError("probability thresholds must be within [0, 1]")

    def to_dict(self) -> dict[str, float]:
        return {
            "phase": self.phase,
            "action_type": self.action_type,
            "target": self.target,
        }


@dataclass(frozen=True, slots=True)
class DecisionAssessment:
    interpretation: DecisionInterpretation
    threshold_gate: dict

    @property
    def action(self) -> ActionCandidate | None:
        if self.threshold_gate["status"] == "passed":
            return self.interpretation.action
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "interpretation": self.interpretation.to_dict(),
            "threshold_gate": deepcopy(self.threshold_gate),
        }


def assess(
    request: DecisionRequest,
    result: DecisionResult,
    *,
    thresholds: ProbabilityThresholds,
) -> DecisionAssessment:
    """Retain raw interpretation and apply separate probability gates.

    All questions must meet their own floor and have a unique winner.
    Missing distributions fail the gate; vendor confidence is not a
    substitute. Scores are neither averaged nor multiplied into a supposed
    probability that the complete action is correct.
    """
    interpretation = interpret(request, result)
    floors = thresholds.to_dict()
    gate: dict = {
        "calibrated": False,
        "status": "not_applicable",
        "thresholds": floors,
        "scores": {},
        "failed_checks": [],
    }
    if interpretation.action is not None:
        missing = False
        for name, floor in floors.items():
            answer = result.answers[name]
            probabilities = answer.probabilities
            if probabilities is None:
                missing = True
                gate["failed_checks"].append(name)
                continue
            selected = probabilities[answer.selected_id]
            runner_up = max(
                value
                for choice, value in probabilities.items()
                if choice != answer.selected_id
            )
            gate["scores"][name] = {
                "selected_probability": selected,
                "runner_up_probability": runner_up,
                "margin": selected - runner_up,
            }
            if selected < floor or selected <= runner_up:
                gate["failed_checks"].append(name)
        if missing:
            gate["status"] = "missing_scores"
        elif gate["failed_checks"]:
            gate["status"] = "failed"
        else:
            gate["status"] = "passed"
    return DecisionAssessment(interpretation, gate)


def interpret(
    request: DecisionRequest, result: DecisionResult
) -> DecisionInterpretation:
    """Bind answers to the original request and resolve only local geometry."""
    try:
        result.validate_for(request)
    except ValueError:
        return DecisionInterpretation("invalid_result")

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
        return DecisionInterpretation("invalid_profile")

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
            return DecisionInterpretation("invalid_profile")

    if result.answers["phase"].selected_id != "ROUTINE":
        return DecisionInterpretation("non_routine")
    gesture = result.answers["action_type"].selected_id
    if gesture == "YIELD":
        return DecisionInterpretation("model_yield")
    target_id = result.answers["target"].selected_id
    if target_id == "NONE":
        return DecisionInterpretation("no_target")

    target = choices["target"][target_id]
    if target.target_ref in blocked_controls(request.context):
        return DecisionInterpretation("consequential_control")
    # DecisionRequest already binds target refs to the original observation.
    element = next(
        element
        for element in (*request.context.ocr, *request.context.icons)
        if element.ref == target.target_ref
    )
    return DecisionInterpretation(
        "hypothetical_action",
        ActionCandidate(
            id=target_id,
            description=f"Hypothetical {gesture} on observed element {target_id[1:]}.",
            tool=gesture,
            arguments={"bbox": element.bbox},
            target_ref=element.ref,
        ),
    )
