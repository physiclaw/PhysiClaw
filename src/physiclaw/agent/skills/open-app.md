---
name: open-app
description: Launch an installed app through Spotlight when its icon is absent from the current screen and dock.
---

# Open App via Spotlight

At any step, tap a visible app icon directly, including Spotlight suggestions. Otherwise:

1. Copy the exact app name with `send_to_clipboard`; retain Chinese names.
2. Go home if needed, then swipe down from mid-screen: `swipe(bbox=[0.3,0.4,0.7,0.6], direction="down", size="l")`. The top edge opens Notification Center instead.
3. Focus the search field and clear stale text with backspace. Paste appends; verify empty before pasting. Learned field/key boxes may be batched (≤5 actions).
4. With a matching learned layout, paste:

```python
sequence(actions=[
    {"tool_name": "long_press", "arg": {{search-field}}},
    {"tool_name": "tap", "arg": {{paste-button}}},
])
```

5. Tap the installed app icon matching the name in the fresh results. Skip App Store and web/Safari results. No match may mean the user named an entry within an app: try its host app, or locate the icon on home-screen pages. Ask if the destination remains unclear.

The Paste popover is above the field; AutoFill is a different action. Verify the query and app after acting.
