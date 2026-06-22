# PhysiClaw docs — blueprint & working spec

> Internal planning doc. Leading-underscore filename → ignored by the Astro/Starlight build.
> Delete or keep as you like; it is not a published page.

This is the spec the doc rewrite is built against. The existing `*.mdx` files were
**templates written before the code existed** and are factually wrong about the system
(two cameras, a Z servo, direction/distance moves, an "untouched" phone). We keep their
*voice and rhythm* and replace their *substance*.

Audience anchor: **someone who has heard of OpenClaw but not PhysiClaw.** The intro's job is
to bridge from "agent drives software" to "agent drives a real phone."

---

## 1. Corrections ledger (template fiction → code reality)

Pin this. Do not reintroduce any left-column claim.

| Topic | Template (WRONG) | Reality (source) |
|---|---|---|
| Cameras | two: top + side @45° | **one** overhead camera; two modes (`orchestrator.peek` / `screenshot`) |
| Aiming | `move("down-right","large")` | **bboxes** `[l,t,r,b]` in 0–1, chosen from an OCR+icon listing (`core/server/tools.py`) |
| Verify | side camera checks tip | re-`peek`, compare listings |
| Z axis | `M3 S12` servo | **solenoid** hit S1000/80ms, hold S750 (`core/hardware/solenoid.py`) |
| Phone | "nothing installed, untouched" | AssistiveTouch + 3 iOS Shortcuts + bridge web page, QR-paired (`cli/setup/phone.py`, `core/bridge/`) |
| Install | `pip install …`, `config.py` | `install.sh` → `physiclaw` CLI; `doctor`, `setup local-vision-model`, `server`, `setup hardware` |
| Files | `physiclaw_server.py`/`hand.py`/`eyes.py` | `core/` + `agent/`; none of those names exist |
| Tools | 6 | **11**: peek, screenshot, tap, double_tap, long_press, swipe, home_screen, go_back, force_quit, unlock_phone, send_to_clipboard, sequence |
| Calibration | `python -m physiclaw.calibrate`, dots | **10-step** wizard (`cli/setup/hardware.py` / `core/static/setup-hardware.html`) |
| Cost | $127 (two cameras) | **~$112** one camera (README BOM) |
| Agent runtime | not mentioned | full built-in agent: 6 providers, cron/poll triggers, memory, skills, sentinel (`agent/`) |
| Z gesture g-code | `M3 S12`/`M3 S0` | solenoid duty pulses; fix the gestures page |

---

## 2. House style (the "rhythm" rules)

1. One page = one job = one question (name it in the `description` frontmatter).
2. First sentence = the whole page in miniature (pass the "skim test": read only first-sentences).
3. Show before explain: diagram / photo / real `peek` listing first.
4. Anticipate, don't backfill: each paragraph answers the question the last one raised.
5. Honesty beats hype: real trade-offs in callouts (esp. "what's on the phone").
6. Concrete numbers always (~$112, ~4s vs ~12s, 18 points, passcode 111111).
7. Define jargon inline at first use (bbox, MCP, GRBL/FluidNC, AssistiveTouch, affine, solenoid).
8. Vary the block type; never two tables in a row; every page ends with one "Next →".
9. The agent is a character: "it looks, decides, taps."

Components (per `docs/README.md`): directives `:::note/:::tip[Title]/:::caution/:::danger`;
bare tags `Card`, `CardGrid`, `LinkCard`, `Steps`, `Tabs`/`TabItem`, `FileTree`, `Badge`.
No `import`. Frontmatter = `title` + `description` only. Internal links `/en/<slug>/`.

Visuals: ASCII diagrams now; mark photo slots with `<!-- PHOTO: … -->`. Add `!docs/**/*.png`
to `.gitignore` before committing any image (repo ignores png/jpg).

---

## 3. Finalized information architecture

★ = v1 must-have, ☆ = follow-up. Agent runtime elevated to its own section.

```
Start (开始)
  ★ introduction         what it is · OpenClaw hook · honest scope
  ★ how-it-works         See→Act mental model (replaces the-loop)
  ★ what-youll-build     rig overview · cost/time/difficulty · roadmap

Build the hardware (硬件)
  ★ bill-of-materials    ~$112 parts + "why this part"
  ★ assembly             mount phone/stylus/camera/wiring (PHOTO slots)
  ☆ firmware             flashing FluidNC

Set it up (设置)
  ★ install              install.sh · doctor · vision model
  ★ prepare-the-phone    AssistiveTouch + 3 Shortcuts + bridge
  ★ calibrate            the 10-step wizard, what each step proves

Run it (运行)
  ★ first-task           connect MCP client, give a goal (payoff)
  ★ operating-modes      bring-your-own agent vs built-in brain

The agent (代理)  — PhysiClaw's built-in brain
  ★ overview             the built-in agent + OpenClaw lineage
  ★ models               6 providers, pick/switch the active model
  ★ autonomous-tasks     cron + poll triggers, the wake→session loop
  ☆ memory-and-skills    persistent memory + skill system
  ☆ inside-the-engine    native tool loop · sentinel · [note, one-other] turn shape

Concepts (原理)
  ★ architecture         full end-to-end incl. bridge + AssistiveTouch
  ★ how-it-sees          peek vs screenshot · OCR+icon detect · bbox model
  ☆ calibration-math     px→screen%→GRBL mm · affine · drift

Reference (参考)
  ★ mcp-tools            the real 12 tools
  ★ gestures             taps/swipes → solenoid + G-code
  ☆ cli                  doctor/server/setup/status/models
  ☆ configuration        providers, active model, config file

★ safety                 treat like an unlocked phone (own top-level page)
☆ troubleshooting        arm/camera/tap/screenshot/unlock failures
```

Spine logic: **Understand → Build → Set up → Run → (Agent) → depth/reference/safety.**
A hardware+software project cannot front-load Quickstart; the honest path builds first.

---

## 4. Per-page briefs (condensed; full beats in chat history)

- **introduction** — hook off OpenClaw; 3-wall problem (APIs/accessibility/jailbreak); See→decide→move→touch; honesty box on what's really on the phone; who it's for + cost teaser.
- **how-it-works** — one-camera loop diagram; See (annotated bbox listing — show a real one); Decide (bbox+gesture); Act (solenoid drop, re-peek); peek vs screenshot; robustness via re-observe.
- **what-youll-build** — annotated rig photo slot; expectations table (cost/time/skill/OS); roadmap cards onward.
- **bill-of-materials** — one-camera ~$112 table + "why/what matters" column; substitution note.
- **assembly** — Steps w/ photo slots: phone corner-registered in holder, stylus in Z holder, camera ~25cm overhead, wiring, 12V; rigidity=drift callout.
- **firmware** — FluidNC flash + config (`firmware/`).
- **install** — install.sh → doctor → setup local-vision-model (why local) → server → MCP endpoint; OS tabs for serial ports.
- **prepare-the-phone** — AssistiveTouch + 3 Shortcuts + bridge + QR; passcode 111111 + why; link safety.
- **calibrate** — the 10 steps conceptually + "what good looks like"; re-calibrate-when box.
- **first-task** — claude_desktop_config.json; plain goal; REAL trace (peek→tap(bbox)→peek).
- **operating-modes** — BYO MCP client vs built-in agent; close the OpenClaw loop here.
- **agent/overview** — the built-in brain, what it adds over a plain MCP server, OpenClaw lineage.
- **agent/models** — anthropic/openai/deepseek/google/moonshot/qwen; `physiclaw models`.
- **agent/autonomous-tasks** — cron/poll triggers, wake→session, jobs.md, WAIT auto-follow-up.
- **agent/memory-and-skills** — persistent memory, skill discovery.
- **agent/inside-the-engine** — the 7 principles, sentinel (DONE/FAIL/WAIT/STUCK/IDLE), [note,one-other], compaction.
- **architecture** — redraw incl. bridge/AssistiveTouch/Shortcuts; core/ vs agent/ split.
- **how-it-sees** — OCR + ONNX icon detection, listing format, coordinate system.
- **calibration-math** — affine, park geometry, drift.
- **mcp-tools** — transcribe the (already-excellent) `core/server/tools.py` docstrings.
- **gestures** — solenoid duty + G-code; down/dwell/move/up table.
- **cli / configuration** — from `cli/`.
- **safety** — promote README security section.
- **troubleshooting** — mine real error strings from `cli/setup/hardware.py` + tool docstrings.

---

## 5. Wave plan & checklist

Write English first; translate each `.zh.mdx` only after its English sibling is locked.

- [x] **Wave 1 — skeptic's path:** introduction · how-it-works · what-youll-build · update docs.json + index.mdx
- [x] **Wave 2 — builder's path:** bill-of-materials · assembly · install · prepare-the-phone · calibrate · first-task
- [x] **Wave 3 — agent + trust:** operating-modes · agent/overview · agent/models · agent/autonomous-tasks · architecture · mcp-tools · gestures · safety
- [x] **Wave 4 — depth:** firmware · agent/memory-and-skills · agent/inside-the-engine · how-it-sees · calibration-math · cli · configuration · troubleshooting
- [x] **Wave 5 — Chinese:** translate all `.zh.mdx` (native zh, supplier-style part terms per repo convention)

ALL WAVES COMPLETE — 26 EN + 26 ZH pages; docs.json consolidated to the full 8-section IA;
all internal links resolve in both locales; stale templates removed. (Wave 1 nav uses
`set-up/`, `build/`, `run/`, `agent/` slugs.)

Each page must pass the skim test before moving on.
