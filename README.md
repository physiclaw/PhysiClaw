# PhysiClaw

**What if an AI agent could use your phone — just like you do?**

PhysiClaw gives AI agents an eye (camera) and a finger (robotic arm) to physically operate any phone. It looks at the screen, decides what to do, and taps.

Order food delivery. Check your email. Shop for groceries. Book a hotel. Any app, any phone, iOS or Android.

No OAuth tokens. No ADB cables. No APIs. No app to install. No developer setup.
Just unlock your phone, put it on the desk, and let the agent work.

The tradeoff? PhysiClaw needs hardware: a GRBL-compatible control board (FluidNC) driving an X/Y gantry and a solenoid that taps the stylus, plus one overhead USB camera. A compact desktop rig that gives your AI agent physical presence.

## Quickstart

macOS only for now. Full docs: **[docs.physiclaw.ai](https://docs.physiclaw.ai)**. Hardware bill-of-materials [below](#bill-of-materials).

```bash
# 1. Install the CLI (uv + Python 3.12 + physiclaw, all isolated under ~/.local/bin)
curl -fsSL https://physiclaw.ai/install.sh | bash

# 2. Check your environment
physiclaw doctor

# 3. Download the local vision model (~100 MB, one-time)
physiclaw setup local-vision-model

# 4. Plug in the GRBL arm + USB camera, then start the server
physiclaw server                 # leave running in one shell

# 5. In another shell — interactive arm/camera calibration
physiclaw setup hardware
```

Then point your MCP client (Claude Desktop, etc.) at `http://localhost:8048/mcp`.

## How It Works

```text
 Camera ──→ AI Agent ──→ 3-Axis Arm ──→ Camera ──→ Aligned?
 (read screen) (decide)   (move stylus)  (check pos)   │
      ▲                                            Yes │ No
      │                                             │  │
      │       Touch Phone ◄─────────────────────────┘  │
      │            │                                    │
      └────────────┘ (next action)       adjust & retry ◄┘
```

One camera, two modes:

- **Park + screenshot** — stylus parked away, clear view of the full screen to read content
- **Screenshot** — stylus visible in frame, check its position relative to the target
- **Stylus** moves on X/Y to reach any point, up/down (Z) to touch or release

The loop is simple: **look → think → move → confirm → touch → repeat**.

### Why PhysiClaw

Today's AI agents can control your computer — but they hit walls everywhere:

- Want to order food? Need a delivery API + OAuth.
- Want to check your bank? Blocked by data walls.
- Want to book a ride? Another service integration.
- Every new skill/service = new OAuth, new API, new setup. Tedious, fragile, limited.

PhysiClaw takes a different approach: **let the AI agent physically use your phone.** A camera sees the screen. A robotic finger taps it. No OAuth to apply for. No API to integrate. No app can detect or block it — because to the phone, it's just a finger.

One setup. Every app. Just put an unlocked phone on the desk.

## Two Ways to Drive It

PhysiClaw is both an MCP server and a complete agent — you choose who's in charge:

- **Bring your own agent (MCP).** Point any MCP client (Claude Desktop, an IDE, your own) at `http://localhost:8048/mcp`. The client's model does the deciding; PhysiClaw just gives it hands.
- **Built-in agent.** PhysiClaw ships its own agent runtime — a native tool-call loop with memory and skills that runs on Anthropic, OpenAI, Google, Moonshot, or Qwen. It can wake on a schedule or a screen change and operate the phone unattended, with no external client connected.

Same robot, same tools — the difference is whose mind is in the loop.

## System Architecture

```text
┌───────────────────────────────────────┐
│           AI Agent (Brain)            │
│  Claude Desktop / OpenClaw / etc.     │
│  Sees screen → decides → calls tools  │
└──────────────────┬────────────────────┘
                   │ MCP Protocol
                   ▼
┌───────────────────────────────────────┐
│     PhysiClaw MCP Server (Python)     │
│                                       │
│  Tools (MCP):                         │
│   · peek / screenshot    (see)        │
│   · tap / swipe / etc.   (act)        │
│   · home / back / unlock (navigate)   │
└──────────┬────────────────┬───────────┘
           │                │
       USB Camera     USB Serial (GRBL)
           │                │
           ▼                ▼
    ┌────────────┐   ┌───────────────┐
    │   Camera   │   │ GRBL Board    │
    │  (above)   │   │ (embedded)    │
    │            │   │ X/Y gantry    │
    │            │   │ Z stylus      │
    └────────────┘   └──────┬────────┘
                            │ touch
                            ▼
                   ┌─────────────────┐
                   │  Phone          │
                   │  (unlocked)     │
                   └─────────────────┘
```

## Hardware

### Bill of Materials

| Component | Item | Qty | Est. Price |
| --------- | ---- | --- | ---------- |
| **GRBL Arm** | [Paixi Kuaichaobao pen plotter P25](https://e.tb.cn/h.ifgckUqg9Zmph9n?tk=cxpFUxr6Z5C) (X/Y gantry; the tip is driven by a solenoid) | 1 | ~$80 |
| **Camera** | UGREEN 1080P USB camera, fixed focus | 1 | ~$14 |
| **Stylus** | Capacitive stylus, conductive fiber tip 8-10mm | 1 | ~$1.5 |
| Camera mount | Gooseneck desk clamp, metal, 50cm | 1 | ~$2 |
| Phone mount | Anti-slip pad + L-shaped blocks | 1 set | ~$1.2 |
| USB Hub | USB 3.0 Hub (extend Mac USB ports) | 1 | ~$13 |
| **Total (excluding computer)** | | | **~$112** |

### Camera Setup

- **Camera:** straight above the screen center, ~25cm distance, reads screen content and checks stylus position

### Phone Mounting

- Phone placed face-up flat on the arm platform
- Anti-slip pad + L-shaped blocks for positioning, ensuring consistent placement

## Communication Protocol (PhysiClaw ↔ GRBL Arm)

### GRBL G-code (USB → control board)

The X/Y gantry moves with standard GRBL motion; the stylus tip is a **solenoid** fired through the spindle-PWM pin (not a Z servo). All commands used:

```gcode
G90                    # Absolute coordinate mode
G91                    # Relative coordinate mode
G0 Xxx Yyy Fxxx        # Rapid move on X/Y plane (position stylus)
G1 Xxx Yyy Fxxx        # Linear move at constant speed (swipe slide)
M3 S1000               # Solenoid strike — pull the tip onto the glass
M3 S750                # Drop to hold duty — keep it seated (long-press / swipe)
M5                     # Coil off — the return spring lifts the tip
G4 P0.08               # Dwell (gesture timing, in seconds)
?                      # Query real-time position
```

The solenoid uses a hit-and-hold profile: strike at `S1000` to pull the core in, settle ~80 ms, then drop to `S750` to hold without cooking the coil; `M5` releases. A tap is strike → ~80 ms → release; a long-press holds ~1.2 s.

Protocol: USB serial (115200 baud). Send one line at a time (LF, not CRLF), wait for `ok` before the next.

### Key GRBL Parameters

| Parameter | Meaning | Typical Value |
| --------- | ------- | ------------- |
| `$100` / `$101` | Steps per mm (X/Y) | 80 |
| `$110` / `$111` | Max speed mm/min (X/Y) | 5000 |
| `$120` / `$121` | Acceleration mm/sec² (X/Y) | 200 |
| `$22` | Homing — this plotter has no limit switches; alarms are cleared with `$X` | 0 |
| `$32` | Spindle (not laser) PWM mode — required to drive the solenoid | 0 |
| `$30` | PWM `S`-value range ceiling | 1000 |

On FluidNC these live in the `fluidnc_config.yml` you flash during setup (see the [Firmware guide](https://docs.physiclaw.ai/en/hardware/firmware/)), not live `$` writes — the firmware rejects runtime writes to config-owned settings.

### MCP Protocol (MCP Client → PhysiClaw)

The server speaks MCP over streamable HTTP at `http://localhost:8048/mcp`. MCP is a standard, language-agnostic protocol, so any compliant client works.

## Tech Stack

### Language

Python 3.12+

### Key dependencies

(All installed automatically by `install.sh` — listed here for reference.)

- `pyserial` — send G-code to the control board over USB serial
- `opencv-python` + `numpy` — USB camera capture and image handling
- `mcp` — MCP server framework
- `rapidocr` + `onnxruntime` — on-device OCR + icon detection
- `anthropic` — the built-in agent's Anthropic provider (other providers are reached over plain HTTP)
- `typer`, `croniter`, `tomlkit` — CLI, scheduling, config

Driven from an external MCP client, the LLM runs on the client side. With the **built-in agent**, PhysiClaw talks to the model provider itself — which is why the Anthropic SDK ships in the box.

### Platform Compatibility

Mac / Windows / Linux (Raspberry Pi) all supported. The only platform difference is serial device names (Mac: `/dev/tty.usbserial-xxx`, Windows: `COM3`, Linux: `/dev/ttyUSB0`).

## Code Structure

```text
src/physiclaw/
├── cli/               # `physiclaw` CLI — doctor, server, setup, models, …
├── core/              # The robot
│   ├── hardware/      #   arm (GRBL), camera, solenoid, serial-port autodetect
│   ├── vision/        #   OCR + icon detection → annotated element listing
│   ├── calibration/   #   screen ↔ camera ↔ arm affine transforms
│   ├── bridge/        #   iOS bridge — screenshots & clipboard via Shortcuts
│   ├── orchestration/ #   the PhysiClaw orchestrator (lifecycle + gestures)
│   └── server/        #   MCP server: tool definitions and HTTP routes
└── agent/             # The built-in brain (optional)
    ├── engine/        #   native tool-call loop, memory, skills
    ├── provider/      #   Anthropic, OpenAI, DeepSeek, Google, Moonshot, Qwen
    └── runtime/       #   cron + poll triggers, autonomous sessions
```

## Operation

The AI agent doesn't output coordinates. Each camera frame is run through on-device OCR and icon detection, which boxes and labels every element on screen and returns a listing:

```text
id  kind   label        bbox [left,top,right,bottom]   conf
12  icon   "Clock"      [0.41, 0.55, 0.49, 0.63]       0.97
```

A **bbox** is a rectangle around one element, given as `[left, top, right, bottom]` in `0–1` screen fractions. The agent picks a bbox and a gesture; PhysiClaw turns the bbox center into arm coordinates (via calibration) and drives the stylus there. Every step is verified by looking again.

**Full Operation Cycle:**

```text
1. peek()              → Camera frame + annotated element listing
2. AI decides          → pick a target bbox + a gesture
3. tap(bbox)           → Arm moves to the bbox center; solenoid taps
4. peek()              → Look again: did the screen change as expected?
5. Aligned / done?
   → No:  re-peek and pick a different bbox
   → Yes: continue to the next action
```

`peek` uses the overhead camera (~4s). `screenshot` triggers the phone's own pixel-perfect capture (~12s) when a target is too small for the camera to resolve.

### Gesture Implementation

The X/Y gantry positions the stylus over the bbox center, then the solenoid acts:

**Single tap:** strike (`M3 S1000`) → hold ~80 ms → release (`M5`)

**Long press:** strike → settle → drop to `S750` and hold ~1.2 s → release

**Double tap:** two strikes ~100 ms apart (a brief contact-breaking lift between), kept under the iOS ~300 ms double-tap window

**Swipe:** press and hold at `S750` → `G1` slide to the end point at the chosen feed (`F3000`–`F10000`) → release

## Use Cases

| Scenario | Status |
| -------- | ------ |
| Order food delivery (Meituan, Uber Eats) | Yes (enable password-free or give agent the password for full autonomy) |
| Hail a ride (Didi, Uber) | Yes (same as above) |
| Browse and shop (Taobao, Amazon) | Yes (same as above) |
| Check weather / news / stocks | Fully capable |
| Read and reply to messages (WeChat, WhatsApp) | Yes |
| Scroll social media (TikTok, Instagram) | Yes |
| App daily check-in / collect rewards | Fully capable |
| Set alarm / timer / reminder | Yes |
| Take a screenshot and send it | Yes |

## Security Warning

PhysiClaw has **full physical control** of your phone — it can see and tap anything on screen. Even without your passwords, it could open your password manager, read saved credentials, receive OTP codes to reset passwords, or access any app that's already logged in. If a malicious actor compromises your agent, they have the same access.

**Treat it like handing your unlocked phone to a stranger.**

- **Use a dedicated backup phone** — never your primary device
- **Separate phone number** — not linked to your main accounts
- **Fresh accounts** — don't log into your real accounts on it
- **Different passwords** — never reuse credentials from your primary phone
- **Limited funds** — only load a small amount of money, enough for the task
- **No password manager** — don't install one; only store what the agent needs

## License

MIT
