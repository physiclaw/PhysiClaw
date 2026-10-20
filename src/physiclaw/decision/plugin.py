"""Optional decision producer using only neutral facts and local geometry.

Shadow evaluates in the background and always yields. Active waits for its
own current request and returns a bound proposal through the normal host
path. No shadow answer is cached for execution; failures release the slot.
"""

import asyncio
import base64
import logging
import time
from uuid import uuid4

from physiclaw.common import config
from physiclaw.common.model_ref import parse_model_ref
from physiclaw.contract.decision import DecisionProvider, DecisionRequest
from physiclaw.contract.observation import Observation
from physiclaw.contract.plugin import (
    DecisionProviderFactory,
    EventSink,
    PluginRegistration,
    PluginServices,
    ProposedTurn,
    SessionSetup,
)
from physiclaw.contract.turn import TurnInput, TurnMetadata
from physiclaw.decision.context import project
from physiclaw.decision.result import ProbabilityThresholds, assess
from physiclaw.provider.provider_base import (
    ProviderConfigurationError,
    ProviderPermanentError,
    ProviderRequestError,
)

log = logging.getLogger(__name__)

# Provisional floors shared by shadow and active, recorded on every call.
# At .90 the probability floor already enforces a large winning margin.
_THRESHOLDS = ProbabilityThresholds(phase=0.90, action_type=0.90, target=0.90)
_MAX_VIEW_AGE_SECONDS = 10.0
_MAX_CONSECUTIVE_ACTIONS = 3
_FAILURE_COOLDOWN_SECONDS = 5.0


def _view_issue(observation: Observation | None) -> str | None:
    """Bound host-frame age, not sensor exposure time or UI stability."""
    if observation is None:
        return "no_observation"
    if observation.source != "camera":
        return "requires_camera_view"
    if observation.observed_after is None:
        return "unknown_view_age"
    age = time.time() - observation.observed_after
    if not 0 <= age <= _MAX_VIEW_AGE_SECONDS:
        return "stale_view"
    return None


def build() -> PluginRegistration:
    return PluginRegistration(
        id="physiclaw.decision",
        name="Decision",
        create=DecisionPlugin,
    )


class DecisionPlugin:
    def __init__(self) -> None:
        self._events: EventSink | None = None
        self._factory: DecisionProviderFactory | None = None
        self._model: tuple[str, str] | None = None
        self._mode = "off"
        self._timeout = 0.0
        self._provider: DecisionProvider | None = None
        self._task: asyncio.Task[ProposedTurn | None] | None = None
        self._pending: tuple[str, str] | None = None
        self._last_proposed_turn: int | None = None
        self._consecutive_actions = 0
        self._retry_after = 0.0
        self._closed = False

    async def session_setup(self, ctx: PluginServices) -> SessionSetup | None:
        self._events = ctx.events
        self._factory = ctx.decision_provider_factory
        settings = config.CONFIG.decision
        self._mode = settings.mode
        if self._mode == "off":
            return None
        self._timeout = settings.timeout_seconds
        ref = settings.model.strip()
        try:
            self._model = parse_model_ref(ref)
        except ValueError:
            self._write(
                {
                    "event": "decision_disabled",
                    "mode": self._mode,
                    "reason": "missing_model" if not ref else "invalid_model_ref",
                }
            )
            log.warning(
                "Decision mode requires decision.model as provider/model; "
                "disabled for this session"
            )
        return None

    async def advance(
        self, turn: TurnInput, *, metadata: TurnMetadata
    ) -> ProposedTurn | None:
        if self._closed or self._events is None or self._model is None:
            return None
        clock = asyncio.get_running_loop().time
        projection = project(turn, metadata=metadata)
        event = {
            "event": "decision_projection",
            "mode": self._mode,
            "turn": metadata.turn,
            "projection": projection.to_dict(),
            "limits": {
                "max_view_age_seconds": _MAX_VIEW_AGE_SECONDS,
                "max_consecutive_actions": _MAX_CONSECUTIVE_ACTIONS,
            },
        }
        deadline = clock() + self._timeout
        if metadata.deadline is not None:
            deadline = min(deadline, metadata.deadline)
        request = projection.request(uuid4().hex)
        reason = None
        if request is None:
            reason = "blocked" if projection.blockers else "no_choices"
        elif issue := _view_issue(turn.observation):
            reason = issue
        elif self._factory is None:
            reason = "provider_unavailable"
        elif self._task is not None and not self._task.done():
            reason = "busy"
        elif clock() < self._retry_after:
            reason = "provider_cooldown"
        elif deadline <= clock():
            reason = "deadline"
        if self._mode == "active":
            if self._last_proposed_turn != metadata.turn - 1:
                self._consecutive_actions = 0
            if self._consecutive_actions >= _MAX_CONSECUTIVE_ACTIONS:
                reason = "action_streak_limit"
            if self._pending is not None:
                call_id, image_hash = self._pending
                self._pending = None
                receipt = next(
                    (event for event in turn.events if event.call_id == call_id), None
                )
                if (
                    receipt is None
                    or not receipt.executed
                    or receipt.is_error
                    or receipt.changed is not True
                ):
                    reason = "unverified_previous_action"
                elif (
                    turn.observation is not None
                    and turn.observation.frame.sha256 == image_hash
                ):
                    reason = "unchanged_view"
        if reason is not None:
            event["evaluation"] = {"status": "skipped", "reason": reason}
            self._write(event)
            return None

        assert request is not None
        assert turn.observation is not None
        assert turn.observation.observed_after is not None
        # Capture the wall-clock age once as a monotonic expiry before inference
        # or trace I/O; subsequent clock adjustments cannot extend this proposal.
        expires_at = clock() + (
            turn.observation.observed_after + _MAX_VIEW_AGE_SECONDS - time.time()
        )
        if metadata.deadline is not None:
            expires_at = min(expires_at, metadata.deadline)
        event["evaluation"] = {"status": "scheduled", "request_id": request.id}
        assert request.context.frame is not None
        event["image"] = {
            "media_type": request.context.frame.media_type,
            "data_b64": base64.b64encode(request.context.frame.data).decode("ascii"),
        }
        if not self._write(event):
            return None
        self._task = asyncio.create_task(
            self._evaluate(
                request,
                turn=metadata.turn,
                observation=turn.observation,
                deadline=deadline,
                expires_at=expires_at,
            )
        )
        if self._mode == "shadow":
            return None
        proposal = await self._task
        if proposal is not None:
            self._pending = (
                proposal.message.tool_calls[0].id,
                turn.observation.frame.sha256,
            )
            self._last_proposed_turn = metadata.turn
            self._consecutive_actions += 1
        return proposal

    def _write(self, event: dict) -> bool:
        if self._events is None:
            return False
        try:
            if self._events.write(event) is True:
                return True
        except Exception:
            pass
        # Do not let diagnostics change the LLM workflow or leak server text.
        log.warning("Decision tracing failed; disabled for this session")
        self._events = None
        return False

    async def _evaluate(
        self,
        request: DecisionRequest,
        *,
        turn: int,
        observation: Observation,
        deadline: float,
        expires_at: float,
    ) -> ProposedTurn | None:
        assert self._model is not None
        provider_id, model_id = self._model
        identity = {
            "turn": turn,
            "request_id": request.id,
            "observation_id": request.context.observation_id,
            "model": f"{provider_id}/{model_id}",
            "mode": self._mode,
        }
        clock = asyncio.get_running_loop().time
        valid_until = min(deadline, expires_at)
        expired_event = {
            "event": "decision_skipped",
            **identity,
            "reason": "deadline" if deadline <= expires_at else "stale_view",
        }
        if clock() >= valid_until:
            self._write(expired_event)
            return None
        if self._provider is None:
            try:
                assert self._factory is not None
                self._provider = self._factory(
                    provider_id, model_id, require_images=True
                )
            except Exception as exc:
                # Missing credentials/configuration cannot improve during a session.
                self._factory = None
                self._write(
                    {
                        "event": "decision_skipped",
                        **identity,
                        "reason": "provider_unavailable",
                        "error": type(exc).__name__,
                    }
                )
                return None

        if clock() >= valid_until:
            self._write(expired_event)
            return None
        started = clock()
        event = {
            "event": "decision_call",
            **identity,
            "usage": {"input_tokens": None, "output_tokens": None},
        }
        proposal = None
        try:
            # Enforce the deadline for structural providers as well as our base.
            async with asyncio.timeout_at(deadline):
                result = await self._provider.evaluate(request, deadline=deadline)
            event.update(
                response_model=result.model_id,
                provider_request_id=result.provider_request_id,
                provider_elapsed_ms=(
                    result.elapsed_seconds * 1000
                    if result.elapsed_seconds is not None
                    else None
                ),
                usage={
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                },
                answers={
                    name: {
                        "selected_id": answer.selected_id,
                        "confidence": answer.confidence,
                        "probabilities": (
                            dict(answer.probabilities)
                            if answer.probabilities is not None
                            else None
                        ),
                    }
                    for name, answer in result.answers.items()
                },
            )
            # Preserve known accounting even when a provider returns too late.
            task = asyncio.current_task()
            if self._closed or (task is not None and task.cancelling()):
                raise asyncio.CancelledError
            if clock() >= deadline:
                raise TimeoutError
            if (
                result.provider_id != provider_id
                or result.requested_model_id != model_id
            ):
                raise ValueError("decision provider/model mismatch")
            assessment = assess(request, result, thresholds=_THRESHOLDS)
            action = assessment.action
            issue = _view_issue(observation)
            if issue is None and clock() >= expires_at:
                issue = "stale_view"
            event.update(status="answered", **assessment.to_dict(), view_issue=issue)
            if self._mode == "active" and action is not None and issue is None:
                proposal = ProposedTurn.from_action(
                    action, request.context.observation_id, expires_at=expires_at
                )
                event["proposal_call_id"] = proposal.message.tool_calls[0].id
        except asyncio.CancelledError:
            event.update(status="cancelled")
            raise
        except Exception as exc:
            event.update(status="error", error=type(exc).__name__)
            if isinstance(exc, ProviderRequestError):
                event["suppression"] = "request"
            elif isinstance(exc, (ProviderPermanentError, ProviderConfigurationError)):
                self._factory = None
                event["suppression"] = "session"
            else:
                self._retry_after = clock() + _FAILURE_COOLDOWN_SECONDS
                event.update(
                    suppression="cooldown",
                    cooldown_seconds=_FAILURE_COOLDOWN_SECONDS,
                )
        finally:
            event["elapsed_ms"] = round((clock() - started) * 1000)
            if not self._write(event):
                proposal = None
        return proposal

    async def aclose(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        try:
            if task is not None:
                task.cancel()
                # Child cancellation is expected; cancellation of this close propagates.
                await asyncio.gather(task, return_exceptions=True)
        finally:
            provider, self._provider = self._provider, None
            if provider is not None:
                await provider.aclose()
