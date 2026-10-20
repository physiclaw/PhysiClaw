"""Bounded decisions over host-owned actions, independent of chat history.

These DTOs carry facts, never a session or another model's messages. Nested
JSON is copied and frozen on ingress. A returned ID is a selection only;
eligibility, provenance, authorization and execution belong to the consumer.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType
from typing import Literal, Protocol

from physiclaw.common.bbox import Bbox, parse_box

type DecisionValue = (
    str
    | int
    | float
    | bool
    | None
    | Mapping[str, DecisionValue]
    | Sequence[DecisionValue]
)


def _text(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "decision identifiers and descriptions must be nonempty strings"
        )


def _finite(value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError("decision numbers must be finite")


def _freeze(value: DecisionValue, *, depth: int = 0) -> DecisionValue:
    if depth > 32:
        raise ValueError("decision facts exceed maximum nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        _finite(value)
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("decision object keys must be strings")
        return MappingProxyType(
            {key: _freeze(item, depth=depth + 1) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, depth=depth + 1) for item in value)
    raise ValueError("decision facts must be JSON values")


def freeze_mapping(value: Mapping[str, DecisionValue]) -> Mapping[str, DecisionValue]:
    """Copy and deeply freeze JSON facts or arguments at a contract boundary."""
    if not isinstance(value, Mapping):
        raise ValueError("decision facts and arguments must be objects")
    frozen = _freeze(value)
    assert isinstance(frozen, Mapping)
    return frozen


def decision_json(value: DecisionValue) -> object:
    """Fresh JSON containers for an adapter; no shared mutable references."""
    if isinstance(value, Mapping):
        return {key: decision_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [decision_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class DecisionFrame:
    """One encoded frame in its declared coordinate space; no file I/O."""

    id: str
    data: bytes = field(repr=False)
    media_type: Literal["image/jpeg", "image/png"]
    width: int
    height: int

    def __post_init__(self) -> None:
        _text(self.id)
        if not isinstance(self.data, bytes) or not self.data:
            raise ValueError("decision frame data must be nonempty immutable bytes")
        if self.media_type not in ("image/jpeg", "image/png"):
            raise ValueError("unsupported decision image media type")
        if any(type(n) is not int or n <= 0 for n in (self.width, self.height)):
            raise ValueError("decision frame dimensions must be positive integers")

    @property
    def sha256(self) -> str:
        return sha256(self.data).hexdigest()


@dataclass(frozen=True, slots=True)
class DecisionElement:
    """Observed text or icon box, normalized against the context's frame."""

    ref: str
    bbox: Bbox
    text: str | None = None

    def __post_init__(self) -> None:
        _text(self.ref)
        object.__setattr__(self, "bbox", parse_box(self.bbox))
        if self.text is not None and not isinstance(self.text, str):
            raise ValueError("decision element text must be a string or unknown")


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """A current observation and canonical facts, projected by the host.

    ``observation_id`` also supports explicitly evaluated text-only inputs.
    Frame identity and all element references must refer to this observation.
    Capture freshness/transforms and provenance are later host responsibilities.
    """

    observation_id: str
    facts: Mapping[str, DecisionValue] = field(default_factory=dict)
    frame: DecisionFrame | None = None
    ocr: tuple[DecisionElement, ...] = ()
    icons: tuple[DecisionElement, ...] = ()

    def __post_init__(self) -> None:
        _text(self.observation_id)
        object.__setattr__(self, "facts", freeze_mapping(self.facts))
        object.__setattr__(self, "ocr", tuple(self.ocr))
        object.__setattr__(self, "icons", tuple(self.icons))
        if self.frame is not None and self.frame.id != self.observation_id:
            raise ValueError("decision frame and observation identity must match")
        elements = (*self.ocr, *self.icons)
        if any(not e.ref.startswith(f"{self.observation_id}:") for e in elements):
            raise ValueError("decision elements must reference the current observation")
        if len({e.ref for e in elements}) != len(elements):
            raise ValueError("duplicate decision element reference")


@dataclass(frozen=True, slots=True)
class ActionCandidate:
    """An offered action, or a control outcome when ``tool`` is absent.

    This is a transport contract, not a tool schema/permission validator.
    The same immutable object must later bind the local executable action.
    """

    id: str
    description: str
    tool: str | None = None
    arguments: Mapping[str, DecisionValue] = field(default_factory=dict)
    target_ref: str | None = None

    def __post_init__(self) -> None:
        _text(self.id)
        _text(self.description)
        object.__setattr__(self, "arguments", freeze_mapping(self.arguments))
        if self.tool is not None:
            _text(self.tool)
        elif self.arguments or self.target_ref is not None:
            raise ValueError("a control outcome cannot carry an executable action")
        if self.target_ref is not None:
            _text(self.target_ref)


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    id: str
    context: DecisionContext
    candidates: tuple[ActionCandidate, ...]
    instructions: str

    def __post_init__(self) -> None:
        _text(self.id)
        _text(self.instructions)
        object.__setattr__(self, "candidates", tuple(self.candidates))
        ids = {candidate.id for candidate in self.candidates}
        if len(ids) < 2 or len(ids) != len(self.candidates):
            raise ValueError(
                "a decision requires at least two uniquely identified choices"
            )
        refs = {e.ref for e in (*self.context.ocr, *self.context.icons)}
        if any(
            c.target_ref is not None and c.target_ref not in refs
            for c in self.candidates
        ):
            raise ValueError(
                "decision target must be an element in the current observation"
            )


@dataclass(frozen=True, slots=True)
class DecisionCapabilities:
    images: bool = False
    probabilities: bool = False
    confidence: bool = False
    max_choices: int | None = None

    def __post_init__(self) -> None:
        if self.max_choices is not None and (
            type(self.max_choices) is not int or self.max_choices < 2
        ):
            raise ValueError("max_choices must be at least two or unknown")


@dataclass(frozen=True, slots=True)
class DecisionUsage:
    """Unknown accounting stays unknown; zero means a reported zero."""

    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        if any(
            n is not None and (type(n) is not int or n < 0)
            for n in (self.input_tokens, self.output_tokens)
        ):
            raise ValueError(
                "decision token counts must be nonnegative integers or unknown"
            )


@dataclass(frozen=True, slots=True)
class DecisionResult:
    request_id: str
    selected_id: str
    provider_id: str
    model_id: str
    requested_model_id: str
    confidence: float | None = None
    probabilities: Mapping[str, float] | None = None
    usage: DecisionUsage = field(default_factory=DecisionUsage)
    elapsed_seconds: float | None = None
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        for identifier in (
            self.request_id,
            self.selected_id,
            self.provider_id,
            self.model_id,
            self.requested_model_id,
        ):
            _text(identifier)
        if self.confidence is not None:
            _finite(self.confidence)
            if not 0 <= self.confidence <= 1:
                raise ValueError("decision confidence must be in [0, 1]")
        if self.probabilities is not None:
            probabilities = dict(self.probabilities)
            for key, value in probabilities.items():
                _text(key)
                _finite(value)
                if not 0 <= value <= 1:
                    raise ValueError("decision probabilities must be in [0, 1]")
            # Absolute tolerance for rounding, not a policy acceptance threshold.
            if not math.isclose(
                sum(probabilities.values()), 1.0, rel_tol=0, abs_tol=1e-6
            ):
                raise ValueError("decision probabilities must sum to one within 1e-6")
            object.__setattr__(self, "probabilities", MappingProxyType(probabilities))
        if self.elapsed_seconds is not None:
            _finite(self.elapsed_seconds)
            if self.elapsed_seconds < 0:
                raise ValueError("decision elapsed time cannot be negative")
        if self.provider_request_id is not None:
            _text(self.provider_request_id)

    def validate_for(self, request: DecisionRequest) -> None:
        """Bind a reply to exactly this request's offered choice set."""
        ids = {candidate.id for candidate in request.candidates}
        if self.request_id != request.id or self.selected_id not in ids:
            raise ValueError(
                "decision reply does not match the request or offered choices"
            )
        if self.probabilities is not None:
            if set(self.probabilities) != ids:
                raise ValueError(
                    "decision probability keys must match the offered choices"
                )
            if self.probabilities[self.selected_id] < max(self.probabilities.values()):
                raise ValueError("selected decision must have maximal probability")


class DecisionProvider(Protocol):
    @property
    def api_kind(self) -> Literal["decision"]: ...

    @property
    def capabilities(self) -> DecisionCapabilities: ...

    async def evaluate(
        self, request: DecisionRequest, *, deadline: float
    ) -> DecisionResult:
        """One attempt within an absolute asyncio loop.time() deadline."""
        ...

    async def aclose(self) -> None: ...
