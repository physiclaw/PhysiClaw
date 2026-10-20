"""Project neutral input and optionally evaluate it in the background.

Shadow answers are trace evidence only. This producer always yields, owns
at most one in-flight request, and never queues work or executes an action.
"""

import asyncio
import logging
from uuid import uuid4

from physiclaw.common import config
from physiclaw.common.model_ref import parse_model_ref
from physiclaw.contract.decision import DecisionProvider, DecisionRequest
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
from physiclaw.decision.result import ShadowThresholds, assess

log = logging.getLogger(__name__)

# Uncalibrated shadow-only floors, recorded with the actual model on each call.
# At .90 the probability floor already enforces a large winning margin.
_SHADOW_THRESHOLDS = ShadowThresholds(phase=0.90, action_type=0.90, target=0.90)


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
        self._timeout = 0.0
        self._provider: DecisionProvider | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def session_setup(self, ctx: PluginServices) -> SessionSetup | None:
        self._events = ctx.events
        self._factory = ctx.decision_provider_factory
        settings = config.CONFIG.decision
        if settings.mode != "shadow":
            return None
        self._timeout = settings.timeout_seconds
        ref = settings.model.strip()
        try:
            self._model = parse_model_ref(ref)
        except ValueError:
            self._write(
                {
                    "event": "decision_shadow_disabled",
                    "reason": "missing_model" if not ref else "invalid_model_ref",
                }
            )
            log.warning(
                "Decision shadow mode requires decision.model as provider/model; "
                "disabled for this session"
            )
        return None

    async def advance(
        self, turn: TurnInput, *, metadata: TurnMetadata
    ) -> ProposedTurn | None:
        if self._closed or self._events is None or self._model is None:
            return None
        projection = project(turn, metadata=metadata)
        event = {
            "event": "decision_projection",
            "turn": metadata.turn,
            "projection": projection.to_dict(),
        }
        deadline = asyncio.get_running_loop().time() + self._timeout
        if metadata.deadline is not None:
            deadline = min(deadline, metadata.deadline)
        request = projection.request(uuid4().hex)
        reason = None
        if request is None:
            reason = "blocked" if projection.blockers else "no_choices"
        elif self._factory is None:
            reason = "provider_unavailable"
        elif self._task is not None and not self._task.done():
            reason = "busy"
        elif deadline <= asyncio.get_running_loop().time():
            reason = "deadline"
        if reason is not None:
            event["shadow"] = {"status": "skipped", "reason": reason}
            self._write(event)
            return None

        assert request is not None
        event["shadow"] = {"status": "scheduled", "request_id": request.id}
        # A test call without a writable trace provides no benchmark evidence.
        if self._write(event):
            self._task = asyncio.create_task(
                self._evaluate(request, turn=metadata.turn, deadline=deadline)
            )
        return None

    def _write(self, event: dict) -> bool:
        if self._events is None:
            return False
        try:
            self._events.write(event)
        except Exception:
            # Do not let diagnostics change the LLM workflow or leak server text.
            log.warning("Decision shadow tracing failed; disabled for this session")
            self._events = None
            return False
        return True

    async def _evaluate(
        self, request: DecisionRequest, *, turn: int, deadline: float
    ) -> None:
        assert self._model is not None
        provider_id, model_id = self._model
        identity = {
            "turn": turn,
            "request_id": request.id,
            "observation_id": request.context.observation_id,
            "model": f"{provider_id}/{model_id}",
        }
        clock = asyncio.get_running_loop().time
        if clock() >= deadline:
            self._write(
                {"event": "decision_shadow_skipped", **identity, "reason": "deadline"}
            )
            return
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
                        "event": "decision_shadow_skipped",
                        **identity,
                        "reason": "provider_unavailable",
                        "error": type(exc).__name__,
                    }
                )
                return

        if clock() >= deadline:
            self._write(
                {"event": "decision_shadow_skipped", **identity, "reason": "deadline"}
            )
            return
        started = clock()
        event = {
            "event": "decision_call",
            "mode": "shadow",
            **identity,
            "usage": {"input_tokens": None, "output_tokens": None},
        }
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
            if self._closed:
                raise asyncio.CancelledError
            if clock() >= deadline:
                raise TimeoutError
            if (
                result.provider_id != provider_id
                or result.requested_model_id != model_id
            ):
                raise ValueError("decision provider/model mismatch")
            event.update(
                status="answered",
                **assess(request, result, thresholds=_SHADOW_THRESHOLDS),
            )
        except asyncio.CancelledError:
            event.update(status="cancelled")
            raise
        except Exception as exc:
            event.update(status="error", error=type(exc).__name__)
        finally:
            event["elapsed_ms"] = round((clock() - started) * 1000)
            self._write(event)

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
