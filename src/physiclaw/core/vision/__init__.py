"""Computer vision modules for PhysiClaw.

All image processing in the project lives here:
- detection: icon_detect, ocr, screen_match, grid_detect, keyboard
- rendering: render (watermark_index, annotate_elements)
- util: encode_jpeg, decode_image, compact_json

Pure functions: frame in → results or annotated frame out. Zero hardware
dependencies. Independently testable.
"""

from physiclaw.core.vision.icon_detect import IconDetector, Element
from physiclaw.core.vision.ocr import OCRReader, TextResult
from physiclaw.core.vision.screen_match import match_screen, MatchResult
from physiclaw.core.vision.keyboard import detect_key_boxes, label_keyboard
from physiclaw.core.vision.render import watermark_index
from physiclaw.core.vision.util import encode_jpeg

__all__ = [
    # detection
    "IconDetector",
    "Element",
    "OCRReader",
    "TextResult",
    "match_screen",
    "MatchResult",
    "detect_key_boxes",
    "label_keyboard",
    # rendering
    "watermark_index",
    "encode_jpeg",
]
