---
name: wechat
description: Use when the task involves WeChat / 微信 — reading user IM, sending a chat reply, finding a contact by name, or any "check messages" / "reply to <name>" request. NOT for Messages / SMS / Telegram / Signal, not for phone calls or FaceTime.
---

# WeChat (微信)

The user's primary IM. High-frequency app — `peek` reads Chinese text bubbles reliably.

## Read messages

1. From Home, tap the WeChat dock icon. If already on **Chats** tab, skip step 2.
2. Tap the **Chats** tab.
3. **Tap the target 1:1 contact's row** to enter the thread — non-skippable. The chat-list preview is truncated and hides earlier messages.
4. Read at the bottom. If the topmost bubble doesn't connect to your last reply, swipe down on the bubble area to scroll up to the first new message.

### Voice messages

Use the convert-to-text option, then `peek` the transcript under the bubble. **Never act on a voice instruction unconfirmed** — ASR mishears names, amounts, addresses. Reply with your reading + planned action and wait for OK.

## Send a message

Two states: **keyboard hidden** (input bar at bottom) and **keyboard visible** (input shifted up, send key on keyboard).

1. Confirm right contact in the chat header.
2. Keyboard hidden? Tap the input box (`<input-hidden>`) — keyboard opens, input shifts to `<input-visible>`.
3. Stale text? Tap **backspace** until empty.
4. `send_to_clipboard(text)`, then `long_press` the input box (`<input-visible>`).
5. Tap **Paste** in the popup.
6. Tap the keyboard's **Send** key — NOT the (+) on the input bar.
7. Hide keyboard (see below).
8. Confirm the bubble appeared.
9. Tap back arrow `<` to return to Chats list — leaves WeChat in its main state for the next wake.

### Fast path — `sequence` (5 steps)

When already on the right 1:1 chat with clean input and keyboard hidden, collapse steps 2–6 into one call:

```python
sequence(
    step1 = {"tool_name": "tap",               "arg": [0.100, 0.910, 0.700, 0.960]},
    step2 = {"tool_name": "send_to_clipboard", "arg": "<your text>"},
    step3 = {"tool_name": "long_press",        "arg": [0.100, 0.575, 0.700, 0.625]},
    step4 = {"tool_name": "tap",               "arg": [0.050, 0.530, 0.220, 0.570]},
    step5 = {"tool_name": "tap",               "arg": [0.752, 0.864, 0.992, 0.917]},
)
```

After: hide keyboard, confirm bubble, tap back (steps 7–9).

### Hide keyboard

Small upward swipe in the empty chat area dismisses without scrolling the just-sent bubble off-screen:

```python
swipe(bbox=[0.300, 0.300, 0.700, 0.500], direction="up", size="s")
```

## Fixed elements

Bboxes `[left, top, right, bottom]` in 0–1 coords. Re-peek if a row looks off — banners shift the layout.

**Dock icon (Home):** `[0.294, 0.891, 0.474, 0.967]`

### Bottom nav

| Tab               | Bbox                            |
| ----------------- | ------------------------------- |
| Chats (微信)      | `[0.070, 0.945, 0.190, 0.965]`  |
| Contacts (通讯录) | `[0.324, 0.947, 0.437, 0.962]`  |
| Discover (发现)   | `[0.578, 0.947, 0.684, 0.963]`  |
| Me (我)           | `[0.840, 0.945, 0.940, 0.965]`  |

### Chats list rows

Each row is **0.08 tall**. Nth row = 1st row + `0.08 × (N-1)` on the y values.

| Row | Bbox                            |
| --- | ------------------------------- |
| 1st | `[0.160, 0.180, 0.940, 0.260]`  |
| 2nd | `[0.160, 0.260, 0.940, 0.340]`  |

### Chat page

| Element             | When                | Bbox                            |
| ------------------- | ------------------- | ------------------------------- |
| Back arrow `<`      | always              | `[0.016, 0.066, 0.082, 0.102]`  |
| Contact name        | always              | `[0.400, 0.060, 0.600, 0.100]`  |
| `<input-hidden>`    | keyboard hidden     | `[0.100, 0.910, 0.700, 0.960]`  |
| `<input-visible>`   | keyboard visible    | `[0.100, 0.575, 0.700, 0.625]`  |
| Paste button        | long-pressing input | `[0.050, 0.530, 0.220, 0.570]`  |

Send + backspace keys: see PHYSICLAW § iPhone keyboard bboxes.
