---
name: im
description: Read and send messages in WeChat or WhatsApp, including the user's primary chat. Not SMS, calls, or FaceTime.
---

# Instant Messaging

Use the user's configured IM app, otherwise the dock's IM app (WeChat for Chinese users, WhatsApp for English). Learned chat boxes apply only to the named app and current keyboard layout; otherwise ground from the view. Complete first-run screen-layout setup before sending.

## Read

Open the IM app, select Chats, and enter the intended thread. Read the latest messages in context; list previews can truncate them. Convert voice messages to text, then confirm names, amounts, addresses, and intended action with the user before acting on the transcript.

## Send

Confirm the recipient in the header and clear any stale input. For the correct 1:1 chat with clean input and matching learned layout, use the appropriate batch below. Its attached view should show the sent bubble. Then hide the keyboard and return to the chats list.

Keyboard hidden (the first tap raises it and moves the input):

```python
sequence(actions=[
    {"tool_name": "tap", "arg": {{input-hidden}}},
    {"tool_name": "send_to_clipboard", "arg": "<your text>"},
    {"tool_name": "long_press", "arg": {{input-visible}}},
    {"tool_name": "tap", "arg": {{paste-button}}},
    {"tool_name": "tap", "arg": {{send}}},
])
```

Keyboard already visible:

```python
sequence(actions=[
    {"tool_name": "send_to_clipboard", "arg": "<your text>"},
    {"tool_name": "long_press", "arg": {{input-visible}}},
    {"tool_name": "tap", "arg": {{paste-button}}},
    {"tool_name": "tap", "arg": {{send}}},
])
```

With the keyboard visible, the hidden-input bbox hits keyboard keys. Send is the learned key/button, not +, voice, or camera. Banners, quotes, another app, or a failed batch invalidate this fast path: ground and verify each action separately. In WeChat, an accidental quote shifts the input; re-ground it rather than chasing the tiny quote-dismiss button.
