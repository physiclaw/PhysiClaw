# Studio tests

Run the Python server/session tests with `uv run pytest tests/studio`.

Run the inline browser-script regressions with `make test-studio-ui`
(Node 22+, no npm packages). CI runs both suites on Linux and macOS.
The JavaScript suite executes the complete script from `studio.html` with
deterministic DOM, HTTP and timer boundaries. It exercises user events and
checks submitted requests, field values, cursor position and disabled controls.
Follow the behavioral-testing conventions in [TEST.md](../TEST.md).

The DOM double does not model browser layout, focus or pointer capture. Check
those in a real browser against a local mock Studio session when changing them.
