# Phase 2 — Test Plan

Built from `./TEST.md` (standards) + `./_AUDIT.md` (recon).

## Coupling map (one-pass survey)

Counts from `grep -lE '<pattern>' -r src/physiclaw --include=*.py`:

| Coupling | Files | Implication for tests |
| --- | ---: | --- |
| `cv2.*` | 16 | Vision/calibration/compact/dumps/keyboard. Test with synthetic `np.ndarray`; reserve real-image fixtures only for content-dependent paths (orange-dot, OCR). |
| `serial` | 3 | `arm.py`, `grbl.py`, hardware handler. Mock at `serial.Serial` import site. |
| `subprocess` | 6 | `cli/skills.py` (git), `cli/server.py`, `cli/setup/hardware.py`, `cli/config.py`, `core/bridge/lan.py`, `core/hardware/camera.py`. Mock with `pytest-mock`. |
| `httpx` | 5–6 | All providers + `cli/doctor.py`, `cli/setup/vision.py`, `agent/runtime/runtime.py`. Use `respx` (HTTPX-native) — add to dev deps. |
| `socket` | 3 | `bridge/lan.py`, `cli/setup/hardware.py`, `core/server/warm_start.py`. Mock or use `0.0.0.0`/loopback. |
| `subprocess` *async* / `asyncio.create_subprocess_*` | 11 | Engine, runtime, claude. `pytest-asyncio` + `mocker.patch.object`. |
| `time.sleep` / `asyncio.sleep` | 9 | `freezegun` doesn't help here — patch `sleep` directly to no-op in unit tests. |
| `threading` | 7 | Camera, watchdog, bridge, orchestrator. Test via shared-state assertions, not by exercising threads. |

## Module-level testability ratings

After reading the easy-win files end-to-end:

| Module | Pure? | Verdict |
| --- | --- | --- |
| `core/calibration/transforms.py` | yes (numpy math) | Round-trip + boundary tests, no fakes. |
| `core/bridge/nonce.py` | yes (numpy + random) | Property test on `generate→verify` round-trip. |
| `agent/engine/validator.py` | yes | Heavy parametrize candidate (≥10 equivalence classes). |
| `agent/engine/dto.py` | yes (dataclasses) | Minimal sanity (frozen-ness, equality, alias `ToolResult is ToolResultMessage`). |
| `agent/provider/wire.py` | yes (json + base64) | Round-trip property tests; one cv2 dependency via `compact.scale_image_bytes`. |
| `paths.py` | yes (Pathlib) | Set `PHYSICLAW_HOME` before import; otherwise pure. |
| `runtime_state.py` | mostly | `_pid_alive` calls `os.kill(pid, 0)`; mock with `mocker.patch`. |
| `core/hardware/grbl.py` | no | Mock `serial.tools.list_ports.comports()` + `serial.Serial`. |
| `agent/engine/job_store.py` | yes (logic) + filesystem | All cron/parsing logic is pure; file I/O at the edge is `tmp_path`-friendly. **freezegun required.** |
| `core/calibration/calibrate.py` | mixed | Pure: `grid_positions`, `_tilt_from_affine`, `_pick_rotation_from_markers`. Rest is hardware orchestration → **`@pytest.mark.integration`**, not unit-tested. |

## Hard-to-test callouts and proposals

Per TEST.md §Phase 2: "propose minimal refactors **only** where testing is otherwise impossible."

1. **`paths.HOME` and `agent/engine/job_store.JOBS_PATH` evaluated at import.**
   *Already testable* via `PHYSICLAW_HOME` env var set before any `import physiclaw`. Phase 3 adds an autouse `conftest.py` fixture that sets `os.environ["PHYSICLAW_HOME"] = str(tmp_path / "physiclaw_home")` and clears the module-level singletons. **No refactor.**

2. **`config.CONFIG` is a module-level singleton populated by `load()` at import.**
   Tests that need a different config value would otherwise contaminate other tests. **No refactor;** use `mocker.patch.object(physiclaw.config, "CONFIG", custom)` per test, or expose a `set_for_test(cfg)` helper inside `config.py` if monkeypatch becomes painful (defer until we hit it).

3. **`core/calibration/calibrate.py` orchestration functions** (`calibrate_arm`, `compute_camera_mapping`, `validate_calibration`, `measure_viewport_shift`, `trace_screen_edge`, `verify_assistive_touch`).
   Each is 60–250 LOC, hardware-coupled, intermixes serial commands with branching. **No refactor.** Cover via:
   - Unit-test the extracted pure helpers (`grid_positions`, `_tilt_from_affine`, `_pick_rotation_from_markers`).
   - Mark each orchestration function with `@pytest.mark.integration` and write *thin* smoke tests that exercise its happy path with a fake `StylusArm` + fake `Camera`. These run in the integration suite, not the default fast suite.

4. **`agent/engine/engine.run()` — 556-LOC loop.**
   Tests must drive a fake `Provider` (return scripted `AssistantMessage`s) and a controlled `LocalTool` registry. **No refactor.** Phase 4 builds a `FakeProvider` test double and a `tool_registry` fixture; tests then assert observable engine outputs (history shape, finish_reason, tool-call dispatch order, cache markers).

5. **`agent/provider/{openai,anthropic}_compat.py`** — direct `httpx` usage (sync + async).
   Add `respx` to the `dev` group (HTTPX-native fakes; works for both sync and async clients). **No code refactor.**

6. **Threaded code (`core/hardware/camera.py`, `core/vision/watchdog.py`, `core/orchestration/orchestrator.py`).**
   Tests target the post-condition state, not the thread mechanics. Where a thread polls a queue with `time.sleep`, patch `time.sleep` to a no-op and use a `threading.Event` to signal "loop stepped once" if needed. **No refactor.**

7. **`core/hardware/arm.py` private methods (`_pen_down`, `_dwell`, `_pen_up`)** are called by `calibrate.py`.
   TEST.md §"What NOT to test" says don't test `_foo` directly. **Resolution:** test `StylusArm` public methods that wrap them; for `calibrate.py`, accept that those private methods are part of the *integration* surface, since the calibration flow is what we mark `@integration`.

No refactors are blocking. All can proceed in the scaffold + fakes layer.

## Prioritized backlog (risk × value)

Ordered for execution. **Sprint = "stop and report" boundary** per TEST.md §Phase 4 step 6. Sprints 1–4 use only fakes/fixtures; Sprint 5+ needs `respx` and async fakes added in Phase 3.

### Sprint 1 — pure logic, zero hardware fakes (≈110 tests)

1. `agent/engine/dto.py` — frozen-ness, equality, `ToolResult is ToolResultMessage` alias, `tool_names()`. **8–10 tests.** Categories: happy + boundary.
2. `core/calibration/transforms.py` — `pct_to_grbl_mm`, `pct_to_cam_pixel`, `pixel_to_pct` (round-trip), `bbox_center_pct`, `swipe_end_pct` (clamp + invalid direction), `bbox_to_pixel_rect`, `ViewportShift.css_to_pct`. **15–20 tests.** Categories: happy + boundary + error path + Hypothesis round-trip.
3. `agent/engine/validator.py` — type, enum, min/max, minLength/maxLength, array/items, nested object, **bbox special-case** (5 distinct error messages). **25–30 tests.** Heavy parametrize + 1 Hypothesis test for "well-formed input never raises".
4. `agent/provider/wire.py` — `tool_to_wire`, `assistant_to_wire` (with/without tool_calls, drops leakage), `tool_result_to_wire`, `user_content_to_openai` (str path, multipart, unknown block warning), `mcp_blocks_to_content_blocks` (single-text fast path, mixed text+image, image scale failure fallback). **20–25 tests.** Round-trip property test on text content.
5. `core/bridge/nonce.py` — `generate_nonce` length/range, `verify_nonce` exact match, partial match counter, out-of-bounds pixel (logged, not crashed), DPR scaling. **12–15 tests.** Hypothesis on synthesized image + bits round-trip.
6. `paths.py` — every helper returns child of HOME, `load_calibration_bundle` (missing/invalid JSON/wrong-type/valid). **12–15 tests.** `tmp_path` + `PHYSICLAW_HOME` env var.
7. `runtime_state.py` — `write` records correct fields, `clear` refuses other-pid file, `read_live` returns None for missing/malformed/stale, `_pid_alive` mocked. **10–12 tests.**

### Sprint 2 — time-dependent + filesystem (≈90 tests)

8. `agent/engine/job_store.py` — parser (header regex, field regex, missing required, duplicate id, invalid kind/status/schedule, Next-fire mismatch, ISO parse error), `validate_schedule`, `matches_now`, `next_fire`, `format_minute`, `find_due`, `update_fields` (single + multi-job + missing field), `purge_stale` (terminal+old vs young vs non-terminal vs no-timestamp). **35–45 tests.** `freezegun` for cron, `tmp_path` for files.
9. `agent/engine/scratchpad.py` — `write` appends/replaces, `inject_tail` formats. **5–7 tests.**
10. `agent/engine/plan.py` — `Step`/`Plan` dataclasses, `inject_tail`. **8–10 tests.**
11. `agent/engine/memory.py` — `load_user`, `load_persistent`, `load_recent_entries`, `append_log` (date-rolling), `save_fact`/`update_fact`. **15–20 tests.** `tmp_path` + `freezegun`.
12. `core/calibration/state.py` — `Calibration` dataclass (serialization, defaults). **10–15 tests.**

### Sprint 3 — vision pure helpers (synthetic arrays, ≈60 tests)

13. `core/vision/util.py` — testable purely:
    - `validate_bbox` (mirror of validator's bbox; same message family) — 6 cases
    - `bbox_on_screen` — 4 cases
    - `compact_json` — 3 cases
    - `format_elements` — 3 cases
    - `phone_screen_crop_box` — 4 cases
    - `frame_similarity` — 3 cases (identical, shifted, different)
    - `laplacian_variance` — 3 cases (uniform low, edge high)
    - `encode_jpeg` / `decode_image` round-trip — 2 cases
    - `find_largest_hsv_blob` / `find_all_hsv_blobs` — 4 cases (synthetic colored squares)
    - `detect_bridge_corners` — 2 cases (with markers / without)
    - `check_phone_in_frame` — 3 cases
    - **38–45 tests total.** Mix of synthetic numpy + small fixture images committed under `tests/data/img/`.
14. `core/vision/render.py` — `watermark_index`, `annotate_elements`. **5–8 tests.**
15. `core/calibration/calibrate.py` (pure helpers only) — `grid_positions`, `_tilt_from_affine`, `_pick_rotation_from_markers`. **8–12 tests.**

### Sprint 4 — config + small bridge state (≈70 tests)

16. `config.py` — load default, TOML round-trip, `set_dotted`/`unset_dotted`/`get`, `parse_model_ref` (3 forms), `model_ref` resolution (config > env > built-in > error), `provider_base_url_override`, validation errors per dataclass. **40–50 tests.** `tmp_path` + isolated `Config()` instances.
17. `core/bridge/page.py` — `PageState` (10 tests).
18. `core/bridge/state.py` — `BridgeState` thread-safe set/get assertions (15 tests, patch `time.sleep`).
19. `core/bridge/calib.py` — `CalibrationState` (10 tests).

### Sprint 5 — provider stack with HTTP fakes (≈110 tests)

Requires Phase 3 to add `respx` to dev group.

20. `agent/provider/registry.py` — id resolution, key-status logic. **10–15 tests.**
21. `agent/provider/discovered.py` — `cache_path`/save/load/`is_cached`. **8–10 tests.**
22. `agent/provider/provider_base.py` — error hierarchy + base behavior. **10–15 tests.**
23. `agent/provider/anthropic_compat.py` (+ `vendors/anthropic.py`) — request shape, response parse (text + tool_use + thinking-stripped), retry on transient, permanent error mapping, `Usage` extraction. **20–25 tests** with `respx`.
24. `agent/provider/openai_compat.py` (+ `vendors/{openai,deepseek,moonshot,qwen}.py`) — same shape per vendor. **25–30 tests.**
25. `agent/provider/vendors/google.py` — distinct API shape (Gemini), `vendor_extra.thought_signature` round-trip. **15–20 tests.**

### Sprint 6 — engine glue (≈170 tests, hardest cluster)

26. `agent/engine/jobs.py` — `fired_job_ids`, `format_fired`, `create_job`, `upsert_auto_wait_check`, `get_job`, `finish_job`. **20–25 tests.** `tmp_path` + `freezegun`.
27. `agent/engine/skill.py` — `Skill.discover`/`dispatch`/`render_section`. **15–20 tests.**
28. `agent/engine/mcp_inventory.py` — `discover_mcp_tools` against fake MCP client. **8–10 tests.**
29. `agent/engine/mcp_tool.py` — `McpClient` lifecycle, list/cache. **15–20 tests.**
30. `agent/engine/trace.py` — `brief`, `brief_args`, `format_call_args`, `format_call_result`, `brief_content`, `Trace`/`RawLog`. **20–30 tests.**
31. `agent/engine/prompt.py` — `render_system`, `prefix_hash` (deterministic, sensitive to input), `dump`. **15–20 tests.**
32. `agent/engine/compact.py` — `scale_image_bytes` (limits, preserved aspect), placeholder shape, `collapse_old_turns` (keeps boundary intact), `drop_stale_screens` (rewrites superseded only). **25–35 tests.**
33. `agent/engine/builtin_tool.py` — `Session`, `LocalTool` (one test per built-in), `schemas()`, `build_registry()`. **30–40 tests.** Mocked `PhysiClaw` orchestrator.
34. `agent/engine/engine.py` — `run()` integration via `FakeProvider` returning scripted turns. Cover: tool-call dispatch, validation-error correction, loop termination on `STOP`, max-turns guard, cache marker placement, exception handling per turn. **30–50 tests.** Heavy fixture work.

### Sprint 7 — bridge HTTP + server wiring (≈80 tests)

35. `core/bridge/lan.py` — mock socket / subprocess. **8–12 tests.**
36. `core/bridge/handler.py` — fake state + content. **15–20 tests.**
37. `core/bridge/nonce.py` covered in Sprint 1.
38. `core/server/warm_start.py` — `wait_for_port` (success/timeout), `try_resume` (every branch). **12–15 tests.**
39. `core/server/{tools,bridge,calibration,hardware,watch,app}.py` — `register()` mounts expected routes against a fake `McpServer`. **20–25 tests.**
40. `core/server/types.py`, `mcp.py` — small (5–8 tests).

### Sprint 8 — hardware + orchestrator (≈140 tests, most fakes)

41. `core/hardware/grbl.py` — `_probe_port`, `candidate_ports`, `detect_grbl`. **8–12 tests.** Mock `serial.tools.list_ports`, `serial.Serial`.
42. `core/hardware/arm.py` — `StylusArm.connect`, `move_*`, `_pen_*`, `wait_idle`, error paths, safety bounds. **20–30 tests.**
43. `core/hardware/camera.py` — `Camera` open/read/release, threading, frame-conversion. **15–20 tests.** Mock `cv2.VideoCapture`.
44. `core/hardware/iphone.py` — `AssistiveTouch` builds correct shortcut payloads, subprocess invocation. **8–12 tests.**
45. `core/hardware/handler.py` — handler delegates correctly. **10–15 tests.**
46. `core/calibration/handler.py` — 8 handlers; verify they call `calibrate.*` with correct args (the underlying functions stay `@integration`). **15–20 tests.**
47. `core/orchestration/orchestrator.py` — `PhysiClaw` lifecycle, gesture dispatch, error propagation. **30–50 tests.** Fake hardware composite fixture.
48. `core/vision/{ocr,icon_detect,screen_match,watchdog,grid_detect,keyboard,ui_elements}.py` — model-loading paths mocked, branch coverage on detection logic. **40–60 tests** total. Some tests gated on the OmniParser model file presence — skip with reason if absent.

### Sprint 9 — CLI (typer, ≈90 tests)

49. `cli/_format.py` — output funcs (5 tests).
50. `cli/__init__.py`, `cli/status.py` — wiring + status (10 tests).
51. `cli/config.py` — typer `CliRunner`, every subcommand. **15–20 tests.**
52. `cli/skills.py` — git operations mocked. **15–20 tests.**
53. `cli/server.py`, `cli/setup/{hardware,phone,vision}.py`, `cli/doctor.py`, `cli/models.py` — typer `CliRunner` per subcommand, mock everything below. **30–40 tests.**

### Sprint 10 — agent runtime + claude + hooks (≈90 tests)

54. `agent/runtime/sentinel.py` — `parse_sentinel`. **5–7 tests.**
55. `agent/runtime/runtime.py` — `Runtime` class. **10–15 tests.**
56. `agent/runtime/launcher.py` — `engine_label`, `resolve`, `launch`. **15–20 tests.**
57. `agent/runtime/hook.py` — `Trigger`, `register`, `check_hooks`, `clear`, `load_hooks`. **15–20 tests.**
58. `agent/hooks/cron.py` — `cron()` end-to-end with `freezegun`. **15–20 tests.**
59. `agent/hooks/poll.py` — `phone_watch`. **5–8 tests.**
60. `agent/claude/spawn.py` — `spawn_claude` (mock asyncio subprocess). **20–30 tests.**
61. `agent/claude/preview.py` — `claude_preview`. **10–15 tests.**
62. `agent/claude/plugin.py` — `prepare_plugin_dir` (`tmp_path`). **8–12 tests.**

## Roll-up estimates

| sprint | risk-focus | tests (est.) | new fixtures/fakes |
| --- | --- | ---: | --- |
| 1 | pure logic | 110 | none |
| 2 | time + fs | 90 | `freezegun`, `tmp_path` |
| 3 | vision pure | 60 | synthetic numpy + small fixture images |
| 4 | config + bridge state | 70 | isolated `Config()` |
| 5 | providers | 110 | `respx` |
| 6 | engine | 170 | `FakeProvider`, `FakeMcpClient`, `FakePhysiClaw` |
| 7 | bridge http + server | 80 | `FakeMcpServer`, `FakeRequest` |
| 8 | hardware + orchestrator | 140 | `FakeSerial`, `FakeVideoCapture`, `FakeAssistiveTouch` |
| 9 | CLI | 90 | typer `CliRunner` + per-cmd mocks |
| 10 | runtime + claude + hooks | 90 | async-subprocess fakes |
| **total** | | **~1010** | |

After Sprint 6 the suite should already exceed 90% line-coverage on the critical agentic loop; the remaining sprints raise structural coverage and make the @integration tier executable.

## Phase 3 (Scaffold) requirements derived from this plan

- Add to `pyproject.toml [dependency-groups] dev`: `pytest-cov`, `pytest-mock`, `freezegun`, `hypothesis`, `respx`, `mutmut`.
- Add `[tool.pytest.ini_options] markers = ["slow", "integration"]` and `addopts = "-m 'not slow and not integration'"` for default fast runs.
- Add `[tool.coverage.*]` block with line + branch enabled, source = `src/`, exclude `*/cli/__init__.py` style trivial wirings, gate at 90% line / 85% branch (TEST.md §Coverage gates).
- Top-level `tests/conftest.py`: autouse fixture sets `PHYSICLAW_HOME=tmp_path` before any package import; provides `silenced_log` to suppress noisy module loggers; provides `freeze_time` re-export sugar; provides `fake_orchestrator` factory.
- Mirror directory tree under `tests/` for every source package (15 dirs).
- `tests/data/img/` for committed fixture frames (target ≤500 KB total — small synthetic gradients, not real screenshots; phone-screen crops kept under 200 px on long edge).
- `Makefile` (or `justfile`) recipes for the commands in TEST.md §Commands.

## Phase 1 → Phase 2 scope traceability

- All 21 HIGH-risk modules from `_AUDIT.md` appear in Sprints 1–8 (all by Sprint 8).
- All 38 MED-risk modules appear by Sprint 9.
- LOW-risk `__init__.py` re-exports get one smoke test per package in their owning sprint, not a dedicated entry above.

## Phase 2 status: complete

Awaiting approval to proceed to Phase 3 (Scaffold). No tests written yet, no source modified.
