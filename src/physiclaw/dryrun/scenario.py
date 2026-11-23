"""A message from Sam asks the agent to adjust a desk lamp."""

import ast
import base64
import json
import re
from collections.abc import Callable, Iterator
from functools import lru_cache

import cv2
import numpy as np
from starlette.requests import Request
from starlette.responses import JSONResponse

from physiclaw.common.daylog import stamped
from physiclaw.common.listing import Element, Screen
from physiclaw.core.calibration.state import Calibration
from physiclaw.core.geometry.screen import ViewportShift
from physiclaw.core.vision.util import decode_image
from physiclaw.dryrun import (
    CAPTURE_SIZE,
    DAILY_ACTIONS,
    KEY,
    MODEL,
    PHONE_ACTIONS,
    DryRunError,
)

CAMERA_SIZE = CAPTURE_SIZE[::-1]  # Portrait after Camera applies its saved rotation.
SCREEN_ORIGIN = (150, 70)
SCREEN_SIZE = (420, 820)
SCREEN_MM = (68, 143)
PARKED = complex(-6.8, -7.15)

MESSAGE = ("Turn on the desk lamp.", "Make it warm and dim.", "Check it daily at noon.")
LOG_ENTRY = "Home Controls: turned on the desk lamp and set it to warm, low light."
JOB_ID = "sam-daily-lamp"
JOB_SOURCE = f"cron:{JOB_ID}"
JOB_DESCRIPTION = "Check the desk lamp settings at noon."
DAILY_RESULT = "Verified the desk lamp is on, warm and dim."
LAMP_LABELS = ("Power on", "Warm selected", "Low brightness")
REPLY = "Warm and dim."
REPLY_BUTTON = (90, 300, 330, 385)
PLAN = (
    "Open the desk lamp controls",
    "Adjust the lamp",
    "Log the outcome and reply to Sam",
)
BUTTON = (90, 455, 330, 555)
WARM = (90, 580, 330, 660)
SLIDER = (65, 690, 380, 770)
_TRANSITIONS = {
    "notification": (BUTTON, "message"),
    "message": (BUTTON, "controls"),
    "controls": (BUTTON, "powered"),
    "powered": (WARM, "warm"),
    "warm": (SLIDER, "done"),
    "done": (REPLY_BUTTON, "chat"),
    "chat": ((40, 725, 305, 798), "keyboard"),
    "keyboard": ((40, 465, 305, 535), "paste"),
    "paste": ((42, 420, 140, 475), "draft"),
    "draft": ((310, 698, 419, 770), "sent"),
    "sent": ((0, 312, 28, 510), "chats"),
    "chats": ((140, 770, 280, 819), "home"),
    "home": (BUTTON, "controls"),
}
_BUTTON_COLORS = {
    "notification": (145, 180, 245),
    "message": (160, 230, 225),
    "controls": (106, 121, 226),
    "powered": (139, 204, 145),
    "warm": (139, 204, 145),
    "done": (244, 193, 103),
    "chat": (214, 232, 208),
    "sent": (186, 217, 232),
    "chats": (203, 194, 235),
    "home": (167, 223, 224),
}


def calibration() -> Calibration:
    width, height = SCREEN_SIZE
    cam_width, cam_height = CAMERA_SIZE
    left, top = SCREEN_ORIGIN
    return Calibration(
        viewport_shift=ViewportShift(0, 0, 1, width, height),
        cam_rotation=cv2.ROTATE_90_CLOCKWISE,
        pct_to_grbl=np.array([[SCREEN_MM[0], 0, 0], [0, SCREEN_MM[1], 0]], dtype=float),
        pct_to_cam=np.array(
            [
                [width / cam_width, 0, left / cam_width],
                [0, height / cam_height, top / cam_height],
            ]
        ),
        cam_size=CAMERA_SIZE,
        cam_index=0,
        screen_dimension={
            "width": width,
            "height": height,
            "viewport_width": width,
            "viewport_height": height,
        },
    )


class Phone:
    def __init__(self, fetch_clipboard: Callable[[], str] | None = None) -> None:
        self.fetch_clipboard = fetch_clipboard
        self.clipboard: str | None = None
        self.draft: str | None = None
        self.lamp_screen = "controls"
        self.sent_reply: str | None = None
        self.screen = "idle"
        self.failure: str | None = None
        self._render = lru_cache(maxsize=1)(_frame)

    def notify(self) -> None:
        if self.screen == "idle":
            self.screen = "notification"

    def frame(self) -> np.ndarray:
        screen = self.screen
        if screen == "draft":
            return self._render(screen, self.draft)
        if screen == "sent":
            return self._render(screen, self.sent_reply)
        return self._render(screen)

    def touch(self, start: complex, end: complex, duration: float) -> None:
        if self.failure is None:
            self.failure = self._apply_touch(start, end, duration)

    def _apply_touch(self, start: complex, end: complex, duration: float) -> str | None:
        transition = _TRANSITIONS.get(self.screen)
        if transition is None:
            return f"Unexpected touch on the {self.screen} screen"

        (left, top, right, bottom), next_screen = transition
        # Physical fixture coordinates are independent of the loaded calibration.
        x = start.real * SCREEN_SIZE[0] / SCREEN_MM[0]
        y = start.imag * SCREEN_SIZE[1] / SCREEN_MM[1]
        dx = (end.real - start.real) * SCREEN_SIZE[0] / SCREEN_MM[0]
        dy = (end.imag - start.imag) * SCREEN_SIZE[1] / SCREEN_MM[1]
        # The iOS Shortcut fetches queued text only on a stationary AT long-press.
        if self.screen == "keyboard" and abs(x - 39) < 12 and abs(y - 260) < 12:
            if duration >= 0.5 and abs(end - start) < 0.1 and self.fetch_clipboard:
                self.clipboard = self.fetch_clipboard()
            if not self.clipboard:
                return "The clipboard shortcut did not fetch the queued reply"
            return None
        if self.screen == "done" and 770 <= y <= 820:
            left, top, right, bottom = (140, 770, 280, 820)
            next_screen = "home"
        if not (left <= x <= right and top <= y <= bottom):
            return (
                f"Virtual touch at ({start.real:.1f}, {start.imag:.1f}) mm "
                f"missed the control on the {self.screen} screen"
            )
        if duration < 0.05:
            return f"Virtual touch ended after {duration:.3f}s"
        if self.screen == "warm":
            if not (-150 <= dx <= -100 and abs(dy) < 2 and left <= x + dx <= 210):
                return "Brightness slider did not reach the low setting"
        elif next_screen == "home":
            if dy > -300 or abs(dx) > 10:
                return "Expected a bottom-edge swipe to the home screen"
        elif next_screen == "chats":
            if dx < 200 or abs(dy) > 10:
                return "Expected a back swipe to the chats list"
        elif self.screen == "keyboard" and duration < 0.5:
            return "Paste menu requires a long press in the raised input"
        elif self.screen == "draft" and not self.draft:
            return "The virtual chat composer was empty"
        elif abs(end - start) > 0.1:
            return "A virtual tap moved while the stylus was down"

        if self.screen in {"controls", "powered", "warm"}:
            self.lamp_screen = next_screen
        elif self.screen == "home":
            next_screen = self.lamp_screen
        elif next_screen == "draft":
            self.draft = self.clipboard
        elif next_screen == "sent":
            self.sent_reply = self.draft
        self.screen = next_screen
        return None


def _frame(screen: str, chat_text: str | None = None) -> np.ndarray:
    width, height = CAMERA_SIZE
    left, top = SCREEN_ORIGIN
    screen_width, screen_height = SCREEN_SIZE
    frame = np.full((height, width, 3), 58, dtype=np.uint8)
    view = frame[top : top + screen_height, left : left + screen_width]
    view[:] = (48, 48, 48) if screen == "idle" else (225, 227, 232)
    if screen == "notification":
        cv2.rectangle(view, (0, 0), (screen_width, 88), (238, 238, 238), -1)
        _label(view, "New message", 52)
        _label(view, "Sam", 165)
        _button(view, BUTTON, _BUTTON_COLORS[screen], "Open message")
    elif screen == "message":
        _label(view, "Sam", 67)
        _label(view, MESSAGE[0], 177, scale=1.02)
        _label(view, MESSAGE[1], 231, scale=1.02)
        _label(view, MESSAGE[2], 293, scale=1.02)
        _button(view, BUTTON, _BUTTON_COLORS[screen], "Open controls")
    elif screen == "home":
        _label(view, "Home", 82)
        _button(view, BUTTON, _BUTTON_COLORS[screen], "Desk lamp")
    elif screen in {"chat", "keyboard", "paste", "draft", "sent", "chats"}:
        _label(view, "Chats" if screen == "chats" else "Sam", 82)
        cv2.rectangle(
            view,
            BUTTON[:2],
            BUTTON[2:],
            _BUTTON_COLORS.get(screen, (220, 215, 210)),
            -1,
        )
        if screen == "sent" and chat_text:
            _label(view, chat_text, 216)
        elif screen == "chat":
            _label(view, "Message", 773)
        elif screen == "chats":
            _label(view, "Sam", 216)
        elif screen in {"keyboard", "paste", "draft"}:
            _label(view, chat_text or "Message", 518, x=165)
            _label(view, "Send", 751, x=360, scale=0.85)
            if screen == "paste":
                _label(view, "Paste", 451, x=90, scale=0.85)
    elif screen != "idle":
        _label(view, "Desk lamp", 82)
        if screen == "done":
            _label(view, "Low brightness", 215)
            _button(view, REPLY_BUTTON, (190, 220, 193), "Reply to Sam")
        _button(
            view,
            BUTTON,
            _BUTTON_COLORS[screen],
            "Power off" if screen == "controls" else "Power on",
        )
        is_warm = screen in {"warm", "done"}
        _button(
            view,
            WARM,
            (118, 192, 235) if is_warm else (191, 191, 191),
            "Warm selected" if is_warm else "Warm",
        )
        cv2.line(view, (68, 743), (375, 743), (100, 108, 122), 6)
        knob = 155 if screen == "done" else 282
        cv2.circle(view, (knob, 743), 13, (65, 105, 165), -1)
        _label(view, "Brightness", 721, x=282, scale=1.0)
    return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)


def _button(
    view: np.ndarray,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
    text: str,
) -> None:
    x1, y1, x2, y2 = box
    cv2.rectangle(view, (x1, y1), (x2, y2), color, -1)
    _label(view, text, (y1 + y2) // 2 + 13, scale=1.02)


def _label(
    view: np.ndarray, text: str, y: int, *, x: int = 210, scale: float = 1.25
) -> None:
    font = cv2.FONT_HERSHEY_SIMPLEX
    (width, _), _ = cv2.getTextSize(text, font, scale, 2)
    cv2.putText(
        view, text, (x - width // 2, y), font, scale, (25, 32, 43), 2, cv2.LINE_AA
    )


class ModelStub:
    """A fixed script using the current production OpenAI Responses wire format."""

    def __init__(self) -> None:
        self.actions: tuple[str, ...] = PHONE_ACTIONS
        self.turns = 0
        self.failure: str | None = None

    async def respond(self, request: Request) -> JSONResponse:
        try:
            if request.headers.get("authorization") != f"Bearer {KEY}":
                raise DryRunError("Model request did not use the dryrun credential")
            body = await request.json()
            if (
                body.get("model") != MODEL
                or body["text"]["format"]["name"] != "turn_output"
            ):
                raise DryRunError("Expected a structured agent turn request")
            summary, action = self._next(body)
        except (
            DryRunError,
            KeyError,
            TypeError,
            ValueError,
            AttributeError,
            cv2.error,
        ) as exc:
            if self.failure is None:
                self.failure = f"Model request {self.turns + 1}: {exc}"
            return JSONResponse({"error": self.failure}, status_code=422)
        self.turns += 1
        text = json.dumps(
            {
                "summary": summary,
                "scratchpad": None,
                "action": {self.actions[self.turns - 1]: action},
            }
        )
        return JSONResponse(
            {
                "id": f"resp-dryrun-{self.turns}",
                "model": MODEL,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": f"msg-dryrun-{self.turns}",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": text, "annotations": []}
                        ],
                    }
                ],
            }
        )

    def _next(self, body: dict) -> tuple[str, dict]:
        if self.turns >= len(self.actions):
            raise DryRunError(
                f"Received an extra request after {len(self.actions)} scripted turns"
            )
        previous = self.actions[self.turns - 1] if self.turns else None
        receipt = _tool_receipt(body, previous) if previous else []
        action = self.actions[self.turns]
        if self.actions == DAILY_ACTIONS:
            return self._daily_turn(body, action)
        match action, previous:
            case "peek", _:
                _require_wake(body, "phone")
                return "Look at the notification.", {}
            case "tap", "peek":
                (message,) = _view(body, "notification", "Open message")
                return "Open Sam's message.", {"bbox": message}
            case "update_progress", "tap":
                _view(body, "message", *MESSAGE)
                return "Plan the lamp changes requested by Sam.", {
                    "user_said": " ".join(MESSAGE),
                    "understanding": "Set the desk lamp to warm, low light.",
                    "steps": [
                        {
                            "content": step,
                            "status": "in_progress" if i == 0 else "pending",
                        }
                        for i, step in enumerate(PLAN)
                    ],
                }
            case "create_job", _:
                return "Schedule Sam's daily noon lamp check.", {
                    "id": JOB_ID,
                    "kind": "periodic",
                    "schedule": "0 12 * * *",
                    "description": JOB_DESCRIPTION,
                    "context": "Verify the lamp is on, warm and dim for Sam.",
                }
            case "tap", "update_progress":
                (controls,) = _view(body, "message", "Open controls")
                return "Open the desk lamp controls.", {"bbox": controls}
            case "sequence", "create_job":
                power, warm, brightness = _view(
                    body, "controls", "Power off", "Warm", "Brightness"
                )
                return "Turn the lamp on, select warm light, and dim it.", {
                    "actions": [
                        {"tool_name": "tap", "arg": power},
                        {"tool_name": "tap", "arg": warm},
                        {
                            "tool_name": "swipe",
                            "arg": {
                                "bbox": brightness,
                                "direction": "left",
                                "size": "m",
                                "speed": "slow",
                            },
                        },
                    ]
                }
            case "append_log", _:
                _view(body, "done", *LAMP_LABELS)
                return "The lamp is on, warm, and dim. Record the outcome.", {
                    "entry": stamped(LOG_ENTRY)
                }
            case "read_logs", _:
                return "Read back the daily log entry.", {"entries": 1}
            case "tap", "read_logs":
                if not any(LOG_ENTRY in text for text in receipt):
                    raise DryRunError("The daily log did not contain the lamp outcome")
                (chat,) = _view(body, "done", "Reply to Sam")
                return "Return to Sam's chat to confirm the change.", {"bbox": chat}
            case "sequence", _:
                _view(body, "chat", "Sam", "Message")
                return "Send the verified result to Sam.", {
                    "actions": _reply_actions(body)
                }
            case "go_back", _:
                _view(body, "sent", "Sam", REPLY)
                return "The reply is visible. Return to the chats list.", {}
            case "home_screen", _:
                _view(body, "chats", "Chats")
                return "Leave the phone on its home screen.", {}
            case "update_progress", "home_screen":
                _view(body, "home", "Desk lamp")
                return "Mark the verified task complete.", {
                    "steps": [{"content": step, "status": "completed"} for step in PLAN]
                }
            case "end_session", _:
                return "Finish the verified lamp task.", {
                    "status": "DONE",
                    "recap": "The desk lamp is on with warm, low light.",
                }
            case _:
                raise DryRunError(f"No scripted reply for turn {self.turns + 1}")

    def _daily_turn(self, body: dict, action: str) -> tuple[str, dict]:
        match action:
            case "peek":
                _require_wake(body, JOB_SOURCE, JOB_DESCRIPTION)
                return "Inspect the phone for the scheduled lamp check.", {}
            case "tap":
                (lamp,) = _view(body, "home", "Desk lamp")
                return "Open the lamp controls.", {"bbox": lamp}
            case "append_log":
                _view(body, "done", *LAMP_LABELS)
                return "Record the observed settings.", {"entry": stamped(DAILY_RESULT)}
            case "home_screen":
                return "Return home after the scheduled check.", {}
            case "finish_job":
                _view(body, "home", "Desk lamp")
                return "Complete this occurrence of the daily job.", {
                    "id": JOB_ID,
                    "status": "done",
                    "recap": DAILY_RESULT,
                }
            case "end_session":
                return "Finish the scheduled session.", {
                    "status": "DONE",
                    "recap": DAILY_RESULT,
                }
            case _:
                raise DryRunError(f"Unexpected scheduled action {action}")


def _request_texts(body: dict, role: str) -> Iterator[str]:
    for item in body["input"]:
        if item.get("role") == role:
            yield from (part.get("text", "") for part in item["content"])


def _require_wake(body: dict, source: str, description: str = "") -> None:
    for text in _request_texts(body, "user"):
        if (
            "[Current wake — act on this]" in text
            and f"] {source}: " in text
            and description in text
        ):
            return
    raise DryRunError(f"Missing {source} wake context in the agent request")


def _reply_actions(body: dict) -> list[dict]:
    # Use the hidden-keyboard send sequence from the IM skill that the model
    # received. This exercises both the shipped recipe and learned-box filling.
    prompt = "\n".join(_request_texts(body, "system"))
    im_skill = prompt.partition("\n### im\n")[2].split("\n### ", 1)[0]
    batch = re.search(
        r"^sequence\(actions=(\[.*?\])\)", im_skill, re.MULTILINE | re.DOTALL
    )
    if batch is None:
        raise DryRunError("Missing the IM send sequence in the agent's system prompt")
    try:
        actions = ast.literal_eval(batch[1])
    except (ValueError, SyntaxError) as exc:
        raise DryRunError(
            "IM send sequence must contain the learned phone coordinates"
        ) from exc
    for step in actions:
        if step["arg"] == "<your text>":
            step["arg"] = REPLY
    return actions


def _tool_receipt(body: dict, tool: str) -> list[str]:
    """Read the latest action receipt and require that its tool ran successfully."""
    success = [f"{tool}: ok", "executed: True"]
    for item in reversed(body["input"]):
        if item.get("role") != "user":
            continue
        texts = [
            part.get("text", "")
            for part in item["content"]
            if part["type"] == "input_text"
        ]
        if not texts or texts[0] not in {
            "Structured output result:",
            "Structured output result (error):",
        }:
            continue
        if texts[0] == "Structured output result:":
            for index in range(1, len(texts) - 1):
                if texts[index : index + 2] == success:
                    return texts[index + 2 :]
        break
    raise DryRunError(
        f"Expected a successful {tool} receipt in the latest model request"
    )


def _view(body: dict, screen: str, *expected: str) -> list[list[float]]:
    """Verify the newest image and its accompanying OCR listing on the wire."""
    for item in reversed(body["input"]):
        parts = item.get("content")
        if not isinstance(parts, list):
            continue
        for index in range(len(parts) - 1, -1, -1):
            part = parts[index]
            if part["type"] != "input_image":
                continue
            if index == 0 or parts[index - 1]["type"] != "input_text":
                raise DryRunError("Image arrived without its adjacent screen listing")
            _verify_image(part["image_url"], screen)
            return _bboxes(parts[index - 1]["text"], expected)
    raise DryRunError(f"Expected the {screen} image and listing")


def _label_key(label: str) -> str:
    return re.sub(r"[^a-z]", "", label.lower())


def _bboxes(listing: str, expected: tuple[str, ...]) -> list[list[float]]:
    by_label: dict[str, Element] = {}
    for row in Screen.read(listing).rows:
        by_label.setdefault(_label_key(row.label), row)
    boxes = []
    for label in expected:
        element = by_label.get(_label_key(label))
        if element is None:
            raise DryRunError(f"Expected {label!r} in the latest camera listing")
        boxes.append(list(element.bbox))
    return boxes


def _verify_image(url: str, screen: str) -> None:
    prefix, _, encoded = url.partition(",")
    if prefix != "data:image/jpeg;base64":
        raise DryRunError("Expected a JPEG from MCP")
    data = base64.b64decode(encoded, validate=True)
    try:
        image = decode_image(data)
    except RuntimeError as exc:
        raise DryRunError("The model request contained an invalid JPEG") from exc
    height, width = image.shape[:2]
    # Sample near the top of the button, away from text and its OCR box.
    patch = image[
        height * 57 // 100 : height * 59 // 100, width * 25 // 100 : width * 32 // 100
    ]
    if (
        patch.size == 0
        or np.abs(patch.mean(axis=(0, 1)) - _BUTTON_COLORS[screen]).max() > 26
    ):
        raise DryRunError(f"Camera image did not show the {screen} screen")
