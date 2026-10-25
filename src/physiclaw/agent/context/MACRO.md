# Macros

Prefer a matching Available Macro over manual gestures; treat it as one plan step. Use `start_at` for an already-partially-completed route. Never invent macro names.

On ABORTED, inspect the completed steps and current view. If any gesture ran, continue manually; that macro is blocked for this session. A zero-gesture abort may be retried after correcting the mismatch. Macros cannot make choices or obtain approval; resolve those before executing the affected stretch.
