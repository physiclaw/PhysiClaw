---
name: im
description: Use for any instant-messaging task — reading the user's chat, sending a reply, or finding a contact by name — in WeChat / 微信 or WhatsApp. Covers "check messages", "reply to <name>", "tell <name> ...". This is the primary channel you talk to the user through. NOT for SMS / iMessage, phone calls, or FaceTime.
---

# Instant Messaging

**Which app:** § User's if named → the dock's IM app → WeChat (微信) for Chinese users, WhatsApp for English.

Most IM apps share the same chat-page shape: bubble list, bottom input bar, Send. Input/Send/Paste bboxes come from **SYSTEM § Screen layout** — valid only for the IM app it names; in any other app, `peek` and ground live (Spotlight + keyboard keys are system-wide). First-run notice showing → complete the `screen-layout` skill before sending; reading works without it.

## Read

1. Open the app (dock icon, else `open-app`).
2. Chats tab (WeChat: **微信**; WhatsApp: **Chats**).
3. **Tap the contact's row** — the thread, not the list preview (truncated, hides earlier messages).
4. Read at the bottom; top bubble doesn't connect to your last reply → `swipe` down to the first unread.

**Voice messages:** convert-to-text, `peek` the transcript. **Never act unconfirmed** — ASR mishears names, amounts, addresses. Reply with your reading + planned action; wait for OK.

## Send

1. Confirm the contact in the chat header.
2. Keyboard hidden → `tap` `<input-hidden>`; input shifts to `<input-visible>`.
3. Stale text → `tap` `<backspace>` until empty.
4. `send_to_clipboard(text)` → `long_press` `<input-visible>` → `tap` `<paste-button>`.
5. `tap` `<send>` — NOT (+) / voice / camera. (WeChat: keyboard Send key; WhatsApp: round arrow replacing the mic.)
6. Hide the keyboard: `tap` an empty chat area, or `swipe(bbox=[0.300, 0.300, 0.700, 0.500], direction="up", size="s")` — either keeps the sent bubble on-screen.
7. `peek` — sent bubble appeared.
8. `go_back` to the chats list — clean state for the next wake.

### Fast path

On the right 1:1 chat with clean input → collapse steps 2 + 4–5 into one `sequence` (boxes pinned; CONVENTION § Sequence bundling):

```python
sequence(
    step1 = {"tool_name": "tap",               "arg": <input-hidden>},   # omit if keyboard already visible
    step2 = {"tool_name": "send_to_clipboard", "arg": "<your text>"},
    step3 = {"tool_name": "long_press",        "arg": <input-visible>},
    step4 = {"tool_name": "tap",               "arg": <paste-button>},
    step5 = {"tool_name": "tap",               "arg": <send>},
)
```

Then steps 6–8. Layout shifts (banners, unread badges) → re-`peek` and re-ground; anchors are the header name + learned boxes.
