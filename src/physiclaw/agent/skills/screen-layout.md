---
name: screen-layout
description: First-run capture of Spotlight, chat input, Paste, and keyboard boxes when the setup notice is present.
---

# Learn the screen layout

Use the system keyboard. Capture each page below with `screenshot`; copy element bboxes verbatim into `report_screen_layout`, one field per call. Use gesture views / `peek` to verify navigation and screenshot side effects. Re-read a rejected box rather than adjusting its coordinates.

If an input has no detected box, report other detected fields or recapture; never infer its edges from neighboring elements.

| Page | Fields | How to expose them |
| --- | --- | --- |
| `spotlight` | `spotlight_input`, `space`, `backspace`, `return` | Go home if needed, swipe down mid-screen, focus the search field, capture the keyboard. |
| `spotlight` | `spotlight_paste` | Copy text, long-press the empty search field, capture Paste. |
| `chat-no-keyboard` | `chat_input_kb_hidden` | Open the IM app and a real thread with keyboard hidden. |
| `chat-keyboard` | `chat_input_kb_visible`, `send` | Tap the chat input, capture the fully raised keyboard. If Send is hidden, enter one character, then clear it. |
| `chat-keyboard` | `chat_paste` | With empty input and keyboard raised, copy text, long-press the input, capture Paste. |

```text
report_screen_layout(page="spotlight", field="spotlight_input", bbox=[left, top, right, bottom])
report_screen_layout(page="chat-keyboard", field="send", bbox=[left, top, right, bottom], app="wechat")
```

Pass the same actual IM app name for both chat pages. The tool reports remaining fields; captured boxes are saved automatically. Keyboard geometry is system-wide, chat boxes are app-specific. Home-screen gestures are state-sensitive: skip `home_screen` when already home (it opens the App Switcher there).

After every field is captured, the session restarts with the layout loaded. Continue the original task, or close IDLE if setup was the only task.
