# Phase 1 — Recon Audit

Source of truth for testing standards: `/TEST.md` (repo root).

## Suite state

- `tests/` is empty (cleared 2026-04-28). **Zero existing tests.**
- No prior `pytest` run results to report. Pass/fail/skip = N/A.
- No coverage data. All `line_cov` / `branch_cov` cells below are `—` (not yet measured).
- Coverage gates from TEST.md (90% line / 85% branch) apply only once tests exist.

## Stack inventory (vs TEST.md §Stack)

| Required | In `pyproject.toml` | Status |
|---|---|---|
| `pytest` | `pytest>=8` (dev group) | ✅ |
| `pytest-cov` | — | ❌ missing |
| `hypothesis` | — | ❌ missing |
| `pytest-mock` | — | ❌ missing |
| `freezegun` | — | ❌ missing |
| `mutmut` | — | ❌ missing |
| `responses`/`respx` (HTTP fakes) | — | ❌ missing — relevant for `agent/provider/*` |

`[tool.pytest.ini_options]` only sets `pythonpath = ["src"]`. No coverage config, no markers (`slow`, `integration`) registered, no `[tool.coverage.*]` block. All of these are Phase 3 (Scaffold) work.

## Source tree shape

- 105 `.py` files under `src/physiclaw/`.
- ~15 are trivial `__init__.py` (re-exports / package markers). Collapsed into one row at the bottom of each section.
- Top-level packages: `agent/`, `cli/`, `core/`, plus root files (`config.py`, `paths.py`, `runtime_state.py`).

## Risk rubric

- **HIGH** — external I/O (serial, USB camera, HTTP, subprocess), branch-heavy logic, security-sensitive paths, or modules many others depend on (single-point-of-failure).
- **MED** — pure logic with non-trivial branching, some I/O at the edge, schema/format conversion, time-dependent.
- **LOW** — dataclasses, constants, thin re-exports, dispatch wrappers under ~50 LOC.

## Module table

Columns: `module | LOC | public_symbols | existing_tests | line_cov | branch_cov | risk`. `existing_tests`, `line_cov`, `branch_cov` are uniformly empty/none across the repo; shown for the first row of each section, then `…` to keep the table readable.

### `core/hardware/` — physical device drivers

| module | LOC | public_symbols | existing_tests | line_cov | branch_cov | risk |
|---|---:|---|---|---:|---:|---|
| `core/hardware/arm.py` | 391 | `StylusArm` | none | — | — | **HIGH** |
| `core/hardware/camera.py` | 300 | `Camera`, `silenced_stderr()` | none | — | — | **HIGH** |
| `core/hardware/handler.py` | 206 | `handle_status()`, `handle_connect_arm()`, `handle_connect_camera()`, `camera_preview()`, `handle_camera_preview()` | … | — | — | **HIGH** |
| `core/hardware/iphone.py` | 148 | `AssistiveTouch` | … | — | — | **HIGH** |
| `core/hardware/grbl.py` | 94 | `candidate_ports()`, `detect_grbl()` | … | — | — | MED |
| `core/hardware/__init__.py` | 17 | (re-exports) | … | — | — | LOW |

### `core/vision/` — image processing

| module | LOC | public_symbols | risk |
|---|---:|---|---|
| `core/vision/util.py` | 468 | `encode_jpeg`, `phone_screen_crop_box`, `crop_to_phone_screen`, `laplacian_variance`, `decode_image`, `find_all_hsv_blobs`, `find_largest_hsv_blob`, `detect_bridge_corners`, `frame_similarity`, `check_phone_in_frame`, `validate_bbox`, `bbox_on_screen`, `find_numpad_digit`, `compact_json`, `format_elements` | **HIGH** |
| `core/vision/keyboard.py` | 458 | `detect_space_bottom`, `detect_row_boundaries`, `detect_keys_in_row`, `detect_key_boxes`, `draw_detected_keys`, `boxes_to_text`, `label_keyboard`, `generate_preset` | **HIGH** |
| `core/vision/ui_elements.py` | 220 | `UIElement`, `detect_ui_elements()`, `elements_to_json()` | **HIGH** |
| `core/vision/screen_match.py` | 201 | `MatchResult`, `match_screen()`, `match_best()`, `frames_differ()`, `detect_dark_overlay()` | **HIGH** |
| `core/vision/ocr.py` | 194 | `TextResult`, `OCRReader`, `results_to_elements()`, `annotate()` | MED |
| `core/vision/icon_detect.py` | 190 | `Element`, `IconDetector`, `annotate()` | MED |
| `core/vision/watchdog.py` | 155 | `Watchdog` | MED |
| `core/vision/grid_detect.py` | 140 | `detect_red_dots()`, `sort_dots_to_grid()`, `compute_affine_transforms()`, `detect_orange_dot()` | **HIGH** |
| `core/vision/render.py` | 84 | `watermark_index()`, `annotate_elements()` | LOW |
| `core/vision/__init__.py` | 32 | (re-exports) | LOW |

### `core/calibration/` — screen↔arm coord transforms

| module | LOC | public_symbols | risk |
|---|---:|---|---|
| `core/calibration/calibrate.py` | 967 | `grid_positions`, `measure_viewport_shift`, `calibrate_camera_frame`, `calibrate_arm`, `compute_camera_mapping`, `validate_calibration`, `trace_screen_edge`, `verify_assistive_touch` | **HIGH** |
| `core/calibration/handler.py` | 351 | 8 `handle_*` request handlers | **HIGH** |
| `core/calibration/state.py` | 174 | `Calibration` | MED |
| `core/calibration/transforms.py` | 133 | `ViewportShift`, `ScreenTransforms` | **HIGH** (pure math, depended on by everything) |
| `core/calibration/__init__.py` | 11 | (re-exports) | LOW |

### `core/orchestration/` & `core/server/` — coordination + MCP

| module | LOC | public_symbols | risk |
|---|---:|---|---|
| `core/orchestration/orchestrator.py` | 623 | `PhysiClaw` | **HIGH** |
| `core/server/tools.py` | 272 | `register()` | MED |
| `core/server/warm_start.py` | 204 | `wait_for_port()`, `try_resume()` | MED |
| `core/server/bridge.py` | 65 | `register()` | LOW |
| `core/server/calibration.py` | 57 | `register()` | LOW |
| `core/server/app.py` | 50 | `shutdown()` | LOW |
| `core/server/watch.py` | 49 | `register()` | LOW |
| `core/server/types.py` | 48 | (DTOs) | LOW |
| `core/server/hardware.py` | 32 | `register()` | LOW |
| `core/server/mcp.py` | 25 | (assembly) | LOW |
| `core/server/__init__.py` | 16 | (re-exports) | LOW |
| `core/orchestration/__init__.py` | 10 | (re-exports) | LOW |

### `core/bridge/` — phone HTTP bridge (LAN + nonce auth)

| module | LOC | public_symbols | risk |
|---|---:|---|---|
| `core/bridge/calib.py` | 184 | `CalibrationState` | MED |
| `core/bridge/state.py` | 181 | `BridgeState` | MED |
| `core/bridge/handler.py` | 149 | 9 `serve_*` / `handle_*` handlers | MED |
| `core/bridge/lan.py` | 74 | `get_lan_ip()`, `get_mdns_host()`, `bridge_base_urls()` | MED |
| `core/bridge/nonce.py` | 74 | `generate_nonce()`, `verify_nonce()` | **HIGH** (security) |
| `core/bridge/page.py` | 46 | `PageState` | LOW |
| `core/bridge/__init__.py` | 21 | (re-exports) | LOW |

### `core/logger/`

| module | LOC | public_symbols | risk |
|---|---:|---|---|
| `core/logger/logger.py` | 119 | `setup_logging()`, `logged()` | MED |
| `core/logger/dumps.py` | 63 | `save_tool_call()`, `save_snapshot()`, `save_screenshot()` | MED |
| `core/logger/__init__.py` | 10 | (re-exports) | LOW |

### Root modules

| module | LOC | public_symbols | risk |
|---|---:|---|---|
| `config.py` | 534 | `ConfigError`, `ServerConfig`, `WarmStartConfig`, `AutoPickConfig`, `EngineConfig`, `AgentConfig`, `ProviderConfig`, `CompactConfig`, `MemoryConfig`, `ClaudeConfig`, `RetentionConfig`, `SkillsConfig`, `Config`, `config_path`, `load`, `to_toml`, `write_default`, `get`, `set_dotted`, `unset_dotted`, `provider_base_url_override`, `model_ref`, `model_ref_with_source`, `parse_model_ref`, `resolve_provider_key` | **HIGH** |
| `paths.py` | 100 | 17 path helpers (`ensure_dirs`, `model_cache`, `omniparser_onnx`, `calibration_bundle`, …) | MED |
| `runtime_state.py` | 97 | `write()`, `clear()`, `read_live()` | MED |
| `__init__.py` | 21 | (version) | LOW |

### `agent/engine/` — agentic loop

| module | LOC | public_symbols | risk |
|---|---:|---|---|
| `agent/engine/builtin_tool.py` | 564 | `Session`, `LocalTool`, `schemas()`, `build_registry()` | **HIGH** |
| `agent/engine/engine.py` | 556 | `run()` | **HIGH** |
| `agent/engine/compact.py` | 442 | `new_summary_placeholder`, `new_memory_placeholder`, `new_skills_placeholder`, `collapse_old_turns`, `scale_image_bytes`, `drop_stale_screens` | **HIGH** |
| `agent/engine/trace.py` | 430 | `brief`, `brief_args`, `format_call_args`, `format_call_result`, `brief_content`, `Trace`, `RawLog` | MED |
| `agent/engine/job_store.py` | 397 | `Job`, `load_jobs`, `validate_schedule`, `matches_now`, `next_fire`, `format_minute`, `find_due`, `update_fields`, `purge_stale` | **HIGH** (cron/time logic) |
| `agent/engine/prompt.py` | 290 | `render_system()`, `prefix_hash()`, `dump()` | MED |
| `agent/engine/jobs.py` | 211 | `fired_job_ids`, `format_fired`, `create_job`, `upsert_auto_wait_check`, `get_job`, `finish_job` | MED |
| `agent/engine/dto.py` | 198 | `FinishReason`, `TextBlock`, `ImageBlock`, `ToolCall`, `Usage`, `SystemMessage`, `UserMessage`, `AssistantMessage`, `ToolResultMessage` | LOW |
| `agent/engine/memory.py` | 173 | `load_user`, `load_persistent`, `load_recent_entries`, `append_log`, `save_fact`, `update_fact` | MED |
| `agent/engine/validator.py` | 165 | `ValidationError`, `validate_arguments()` | MED |
| `agent/engine/skill.py` | 156 | `Skill`, `discover()`, `dispatch()`, `render_section()` | MED |
| `agent/engine/plan.py` | 150 | `Step`, `Plan`, `inject_tail()` | LOW |
| `agent/engine/mcp_tool.py` | 148 | `McpClient`, `get_mcp()`, `list_tools_cached()`, `close_mcp()` | MED |
| `agent/engine/mcp_inventory.py` | 60 | `discover_mcp_tools()` | LOW |
| `agent/engine/scratchpad.py` | 44 | `write()`, `inject_tail()` | LOW |
| `agent/engine/__init__.py` | 7 | (re-exports) | LOW |

### `agent/provider/` — multi-vendor LLM clients

| module | LOC | public_symbols | risk |
|---|---:|---|---|
| `agent/provider/anthropic_compat.py` | 306 | `AnthropicCompatibleProvider` | **HIGH** |
| `agent/provider/provider_base.py` | 303 | `ProviderError`, `ProviderTransientError`, `ProviderPermanentError`, `Provider`, `BaseProvider` | MED |
| `agent/provider/openai_compat.py` | 226 | `OpenAICompatibleProvider` | **HIGH** |
| `agent/provider/vendors/google.py` | 173 | `GoogleProvider` | MED |
| `agent/provider/wire.py` | 148 | `tool_to_wire`, `assistant_to_wire`, `tool_result_to_wire`, `user_content_to_openai`, `mcp_blocks_to_content_blocks` | **HIGH** (format conversion) |
| `agent/provider/__init__.py` | 96 | (registration) | LOW |
| `agent/provider/registry.py` | 77 | `in_process_provider_ids`, `is_known`, `provider_class`, `provider_key_status`, `make_provider` | MED |
| `agent/provider/discovered.py` | 70 | `cache_path`, `save`, `load`, `model_ids`, `is_cached` | MED |
| `agent/provider/vendors/moonshot.py` | 54 | `MoonshotProvider` | LOW |
| `agent/provider/vendors/qwen.py` | 27 | `QwenProvider` | LOW |
| `agent/provider/vendors/deepseek.py` | 22 | `DeepSeekProvider` | LOW |
| `agent/provider/vendors/anthropic.py` | 17 | `AnthropicProvider` | LOW |
| `agent/provider/vendors/openai.py` | 15 | `OpenAIProvider` | LOW |
| `agent/provider/vendors/__init__.py` | 12 | (re-exports) | LOW |

### `agent/runtime/`, `agent/hooks/`, `agent/claude/`

| module | LOC | public_symbols | risk |
|---|---:|---|---|
| `agent/claude/spawn.py` | 500 | `spawn_claude()` | **HIGH** (subprocess + IO) |
| `agent/hooks/cron.py` | 215 | `cron()` | **HIGH** (time/cron) |
| `agent/claude/preview.py` | 168 | `claude_preview()` | MED |
| `agent/claude/skills/jobs/jobs.py` | 148 | `main()` | MED |
| `agent/claude/plugin.py` | 142 | `prepare_plugin_dir()` | MED |
| `agent/runtime/launcher.py` | 132 | `engine_label()`, `resolve()`, `launch()` | MED |
| `agent/runtime/runtime.py` | 128 | `Runtime` | MED |
| `agent/runtime/hook.py` | 117 | `Trigger`, `register`, `check_hooks`, `clear`, `load_hooks` | MED |
| `agent/hooks/poll.py` | 48 | `phone_watch()` | LOW |
| `agent/runtime/sentinel.py` | 32 | `parse_sentinel()` | LOW |
| `agent/claude/__init__.py` | 20 | (re-exports) | LOW |
| `agent/hooks/__init__.py` | 7 | (re-exports) | LOW |
| `agent/runtime/__init__.py` | 4 | (re-exports) | LOW |
| `agent/runtime/__main__.py` | 5 | (entry) | LOW |
| `agent/__init__.py` | 1 | — | LOW |

### `cli/` — typer CLI

| module | LOC | public_symbols | risk |
|---|---:|---|---|
| `cli/doctor.py` | 488 | `doctor()` | MED |
| `cli/skills.py` | 410 | `installed_skill_dirs`, `read_provenance` | **HIGH** (git subprocess) |
| `cli/models.py` | 365 | (typer cmds) | MED |
| `cli/setup/hardware.py` | 364 | `api`, `ok`, `lan_ip`, `wait`, `ask`, `calibrate`, `calibrate_retry`, `run`, `hardware` | MED |
| `cli/server.py` | 205 | `server()` | MED |
| `cli/config.py` | 154 | (typer cmds) | MED |
| `cli/setup/phone.py` | 116 | `phone()` | MED |
| `cli/__init__.py` | 95 | (typer wiring) | LOW |
| `cli/setup/vision.py` | 78 | `vision()` | MED |
| `cli/status.py` | 54 | `status()` | LOW |
| `cli/_format.py` | 36 | `ok`, `warn`, `next_hint`, `info`, `section` | LOW |
| `cli/setup/__init__.py` | 27 | (typer subapp) | LOW |

## Risk roll-up

| risk | module count | total LOC |
|---|---:|---:|
| HIGH | 21 | ~7,820 |
| MED | 38 | ~6,790 |
| LOW | 46 | ~1,210 |

(LOC totals approximate; small `__init__.py` files counted as 1 each within LOW.)

## Notable observations for Phase 2 (Plan)

1. **Hardware-coupled modules dominate the HIGH bucket.** `core/hardware/*` and `core/calibration/calibrate.py` are not testable without fakes for serial (`pyserial`), camera (`cv2.VideoCapture`), and the GRBL protocol. Phase 3 needs a fakes layer before Phase 4 can start.
2. **Pure-logic islands worth targeting first** (high value, low setup cost):
   - `core/calibration/transforms.py` — pure math, used by everything.
   - `core/bridge/nonce.py` — security, ~74 LOC, pure crypto.
   - `core/vision/util.py` — many pure helpers (`validate_bbox`, `bbox_on_screen`, `compact_json`, etc.) testable without images.
   - `agent/engine/job_store.py` — cron/schedule logic; `freezegun` makes it deterministic.
   - `agent/engine/validator.py` — schema validation, parser-shaped, ideal for Hypothesis.
   - `agent/engine/dto.py` — dataclasses, sanity checks only.
   - `agent/provider/wire.py` — message format conversion, round-trip testable.
   - `paths.py` / `runtime_state.py` — filesystem helpers, `tmp_path` fixture.
3. **Provider modules** (`anthropic_compat`, `openai_compat`, vendor adapters) need an HTTP-fake layer (`responses` or `respx`) — flag as Phase 3 dependency.
4. **`core/calibration/calibrate.py` at 967 LOC is the largest single file.** Likely candidate for the Phase 2 "hard to test — propose minimal refactor" callout if its functions aren't separable from hardware I/O.
5. **No `conftest.py` exists yet.** Phase 3 will create the top-level one; module-scoped ones can be added during Phase 4 as needed.
6. **Two `TEST.md` files exist:** one at repo root (the standards doc, source of truth) and `src/physiclaw/TEST.md` (untracked, content unknown — flag for the user before Phase 2).

## Phase 1 status: complete

Awaiting confirmation to proceed to Phase 2 (Plan).
