"""
OCR module — read text from phone screen regions.

Uses RapidOCR (PaddleOCR models on ONNX Runtime) for lightweight,
torch-free text recognition. Supports Chinese + English out of the box.

Usage:
    from physiclaw.core.vision.ocr import OCRReader

    reader = OCRReader()
    results = reader.read(screen_image)
    for r in results:
        print(f"{r.text}  conf={r.confidence:.2f}  bbox={r.bbox}")

    # Or read text within a specific region:
    text = reader.read_crop(screen_image, x1, y1, x2, y2)
"""

import dataclasses
import logging
from pathlib import Path

import cv2
import numpy as np

from physiclaw.core.vision.preprocess import resize_to_max_edge

log = logging.getLogger(__name__)


@dataclasses.dataclass
class TextResult:
    """A detected text region."""

    text: str
    bbox: tuple[int, int, int, int]  # (x1, y1, x2, y2) in original image pixels
    confidence: float


class OCRReader:
    """Read text from phone screen images using RapidOCR."""

    def __init__(self):
        try:
            from rapidocr import RapidOCR
        except ImportError:
            raise ImportError(
                "rapidocr is required but couldn't be imported — it ships as a "
                "physiclaw dependency, so reinstall physiclaw to restore it."
            ) from None
        self._ocr = RapidOCR(params={"Global.log_level": "warning"})

    def read(
        self,
        frame: np.ndarray,
        crop_box: tuple[int, int, int, int] | None = None,
        max_edge: int | None = None,
    ) -> list[TextResult]:
        """Detect and read all text in an image.

        When ``crop_box`` is given, OCR only inside that rectangle but
        report bboxes in the original frame's coordinate space — lets
        callers skip off-screen pixels without rewriting coordinates.

        ``max_edge`` downscales the (cropped) image so its long edge is at
        most that many pixels before OCR — recognition cost scales with
        pixels, so a 4K crop OCRs many times slower than a screen-sized
        one. Reported bboxes are scaled back to the original frame's
        coordinates, so callers are unaffected.

        Returns TextResults sorted top-to-bottom, left-to-right.
        """
        if crop_box is None:
            image = frame
            dx, dy = 0, 0
        else:
            left, top, right, bottom = crop_box
            h, w = frame.shape[:2]
            left, top = max(0, left), max(0, top)
            right, bottom = max(0, min(w, right)), max(0, min(h, bottom))
            image = frame[top:bottom, left:right]
            dx, dy = left, top

        if image.size == 0:
            return []
        orig_h, orig_w = image.shape[:2]
        if max_edge is not None:
            image = resize_to_max_edge(image, max_edge)
        work_h, work_w = image.shape[:2]

        result = self._ocr(image)

        if result.boxes is None or result.txts is None:
            return []

        texts = []
        for points, text, score in zip(result.boxes, result.txts, result.scores):
            if not np.isfinite(points).all() or not np.isfinite(score) or score < 0:
                continue
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            x1 = int(float(min(xs)) * orig_w / work_w) + dx
            y1 = int(float(min(ys)) * orig_h / work_h) + dy
            x2 = int(float(max(xs)) * orig_w / work_w) + dx
            y2 = int(float(max(ys)) * orig_h / work_h) + dy

            texts.append(
                TextResult(
                    text=text,
                    bbox=(x1, y1, x2, y2),
                    confidence=float(score),
                )
            )

        texts.sort(key=lambda t: (t.bbox[1], t.bbox[0]))
        log.debug(f"OCR found {len(texts)} text regions")
        return texts

    def read_crop(self, frame: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> str:
        """Read text within a specific bounding box region.

        Args:
            frame: BGR image (numpy array).
            x1, y1, x2, y2: bounding box in pixel coordinates.

        Returns:
            Concatenated text found in the region, or empty string.
        """
        results = self.read(frame, crop_box=(x1, y1, x2, y2))
        return " ".join(r.text for r in results)


def results_to_elements(results: list[TextResult], transforms) -> list[dict]:
    """Convert OCR results to element dicts with screen 0-1 bboxes.

    Uses transforms.pixel_to_pct() to map camera pixel bboxes to
    phone screen coordinates. Output matches the screenshot() JSON
    schema: {id, kind, label, bbox, conf}.
    """
    elements = []
    for i, r in enumerate(results):
        x1, y1, x2, y2 = r.bbox
        corners = [
            transforms.pixel_to_pct(x, y)
            for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2))
        ]
        xs, ys = zip(*corners)
        left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
        elements.append(
            {
                "id": i,
                "kind": "text",
                "label": r.text,
                "bbox": [
                    round(left, 3),
                    round(top, 3),
                    round(right, 3),
                    round(bottom, 3),
                ],
                "conf": round(r.confidence, 2),
            }
        )
    return elements


def annotate(frame: np.ndarray, results: list[TextResult]) -> np.ndarray:
    """Draw text bounding boxes and labels on a frame. Returns a copy."""
    out = frame.copy()
    for r in results:
        x1, y1, x2, y2 = r.bbox
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 0, 255), 2)

        label = f"{r.text} ({r.confidence:.2f})"
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.4
        thickness = 1
        (tw, th), _ = cv2.getTextSize(label, font, font_scale, thickness)

        cv2.rectangle(out, (x1, y1 - th - 4), (x1 + tw + 4, y1), (0, 0, 255), -1)
        cv2.putText(
            out, label, (x1 + 2, y1 - 2), font, font_scale, (255, 255, 255), thickness
        )

    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="OCR on a phone screenshot")
    parser.add_argument("image", help="Path to image")
    parser.add_argument("-o", "--output", help="Save annotated image to this path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG, format="%(message)s")

    img = cv2.imread(args.image)
    if img is None:
        raise SystemExit(f"Cannot read image: {args.image}")

    reader = OCRReader()
    results = reader.read(img)

    print(f"Found {len(results)} text regions:")
    for r in results:
        print(f"  {r.text:30s}  conf={r.confidence:.2f}  bbox={r.bbox}")

    output_path = args.output or str(Path(args.image).stem) + "_ocr.jpg"
    annotated = annotate(img, results)
    cv2.imwrite(output_path, annotated)
    print(f"Saved: {output_path}")
