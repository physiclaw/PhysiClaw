"""One same-frame view across MCP; metadata never becomes chat content.

The existing listing is the sole element codec. Images span the full phone
screen, so proportional image resizing preserves normalized tap coordinates.
No crop, rotation or other image-to-screen transform is supported here.
"""

import base64
import math
from dataclasses import dataclass
from hashlib import sha256
from uuid import uuid4

from physiclaw.common.bbox import parse_box
from physiclaw.common.image import image_dimensions
from physiclaw.common.listing import Element, decode_elements
from physiclaw.contract.decision import DecisionFrame

OBSERVATION_META = "physiclaw/observation"
COORDINATE_SPACE = "phone_screen_normalized"
IDENTITY_TRANSFORM = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


def view_metadata(
    jpeg: bytes, listing: str, *, source: str, observed_after: float | None = None
) -> dict:
    """Bind the atomic image/listing pair at the server's view boundary.

    The packaging ID identifies this observation, not a hardware frame
    counter. Camera views carry the reader's host publication time;
    sensor capture time remains unknown.
    """
    width, height = image_dimensions(jpeg)
    return {
        "id": uuid4().hex,
        "image_sha256": sha256(jpeg).hexdigest(),
        "listing_sha256": sha256(listing.encode()).hexdigest(),
        "width": width,
        "height": height,
        "source": source,
        "captured_at": None,
        "observed_after": observed_after,
        "coordinate_space": COORDINATE_SPACE,
        "image_to_screen": list(IDENTITY_TRANSFORM),
    }


@dataclass(frozen=True, slots=True)
class Observation:
    """Detected elements, not a claim of complete OCR/icon coverage.

    An empty detection channel does not prove that no target is visible.
    Capture time stays unknown when the source does not expose it.
    ``observed_after`` is a conservative Unix-time lower bound on when
    the host camera reader published this frame, not its sensor exposure.
    It includes acquisition and detection latency in subsequent age checks.
    """

    frame: DecisionFrame
    elements: tuple[Element, ...]
    source: str
    captured_at: float | None
    observed_after: float | None
    coordinate_space: str
    image_to_screen: tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "elements", tuple(self.elements))
        object.__setattr__(self, "image_to_screen", tuple(self.image_to_screen))
        if not isinstance(self.frame, DecisionFrame) or any(
            not isinstance(element, Element) for element in self.elements
        ):
            raise ValueError("observation requires immutable frame and elements")
        if (
            self.coordinate_space != COORDINATE_SPACE
            or self.image_to_screen != IDENTITY_TRANSFORM
            or any(type(value) not in (int, float) for value in self.image_to_screen)
        ):
            raise ValueError("unsupported image-to-screen coordinates")
        if self.source not in {"camera", "screenshot", "debug"}:
            raise ValueError("unknown observation source")
        for timestamp in (self.captured_at, self.observed_after):
            if timestamp is not None and (
                type(timestamp) not in (int, float)
                or not math.isfinite(timestamp)
                or timestamp < 0
            ):
                raise ValueError("invalid observation time")
        if len({e.id for e in self.elements}) != len(self.elements):
            raise ValueError("duplicate observation element ID")
        for element in self.elements:
            parse_box(element.bbox)
            if element.conf > 1:
                raise ValueError("invalid detection confidence")

    @property
    def id(self) -> str:
        return self.frame.id

    def element_ref(self, element: Element) -> str:
        return f"{self.id}:{element.id}"

    @property
    def ocr(self) -> tuple[Element, ...]:
        return tuple(e for e in self.elements if e.kind == "text")

    @property
    def icons(self) -> tuple[Element, ...]:
        return tuple(e for e in self.elements if e.kind == "icon")


def read_observation(blocks: list[dict]) -> Observation | None:
    """Read exactly one bound view; absent metadata means unknown evidence.

    The image must be followed by its original listing. Hashes prevent a
    macro, interceptor or transport from silently pairing different views.
    """
    images = [i for i, b in enumerate(blocks) if b.get("type") == "image"]
    if not images:
        return None
    if len(images) != 1:
        raise ValueError("ambiguous observation images")
    index = images[0]
    block = blocks[index]
    metadata = (block.get("_meta") or {}).get(OBSERVATION_META)
    if metadata is None:
        return None
    if index + 1 >= len(blocks) or blocks[index + 1].get("type") != "text":
        raise ValueError("observation listing is missing")
    listing = blocks[index + 1]["text"]
    raw = base64.b64decode(block["data"], validate=True)
    if (
        sha256(raw).hexdigest() != metadata["image_sha256"]
        or sha256(listing.encode()).hexdigest() != metadata["listing_sha256"]
    ):
        raise ValueError("image/listing capture mismatch")
    if image_dimensions(raw) != (metadata["width"], metadata["height"]):
        raise ValueError("observation dimensions do not match the image")
    return Observation(
        frame=DecisionFrame(
            id=metadata["id"],
            data=raw,
            media_type=block["mime_type"],
            width=metadata["width"],
            height=metadata["height"],
        ),
        elements=tuple(decode_elements(listing)),
        source=metadata["source"],
        captured_at=metadata["captured_at"],
        observed_after=metadata["observed_after"],
        coordinate_space=metadata["coordinate_space"],
        image_to_screen=metadata["image_to_screen"],
    )
