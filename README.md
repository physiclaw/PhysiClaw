# PhysiClaw

**What if an AI agent could use your phone — just like you do?**

PhysiClaw gives AI agents an eye (camera) and a finger (robotic arm) to physically operate any phone. It looks at the screen, decides what to do, and taps.

Order food delivery. Check your email. Shop for groceries. Book a hotel. Any app, any phone, iOS or Android.

No OAuth tokens. No ADB cables. No APIs. No app to install. No developer setup.
Just unlock your phone, put it on the desk, and let the agent work.

The tradeoff? PhysiClaw needs hardware: an embedded system running GRBL/grblHAL firmware to control a gantry (X/Y) and stylus (Z), plus a USB camera. A compact desktop rig that gives your AI agent physical presence.

## Quickstart

macOS only for now. Hardware bill-of-materials [below](#bill-of-materials).

```bash
# 1. Install the CLI (uv + Python 3.12 + physiclaw, all isolated under ~/.local/bin)
curl -fsSL https://raw.githubusercontent.com/echosprint/PhysiClaw/main/install.sh | bash

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
│  Tools:                               │
│   · screenshot       (camera)         │
│   · park             (retract)        │
│   · move             (X/Y plane)      │
│   · tap / swipe      (Z down + move)  │
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
| **GRBL Arm** | [Paixi Kuaichaobao pen plotter P25](https://e.tb.cn/h.ifgckUqg9Zmph9n?tk=cxpFUxr6Z5C) (X/Y gantry + Z servo) | 1 | ~$80 |
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

### GRBL G-code (USB → GRBL Arm)

All commands used in this project:

```gcode
G91                    # Relative coordinate mode (default)
G0 Xxx Yyy Fxxx        # Rapid move on X/Y plane (position stylus)
G1 Xxx Yyy Fxxx        # Linear move at constant speed (swipe gesture)
M3 S12                 # Stylus down (touch screen)
M3 S0                  # Stylus up (release screen)
M5                     # Servo off
G90                    # Absolute coordinate mode (for park)
G0 X0 Y0 F5000         # Return to home position
$$                     # Query all GRBL parameters
?                      # Query real-time position
```

Protocol: USB serial (CH340, 115200 baud). Send one line at a time, wait for `ok` before next.

### Key GRBL Parameters

| Parameter | Meaning | Typical Value |
| --------- | ------- | ------------- |
| `$100` / `$101` | Steps per mm (X/Y) | 80 |
| `$110` / `$111` | Max speed mm/min (X/Y) | 5000 |
| `$120` / `$121` | Acceleration mm/sec² (X/Y) | 200 |
| `$22` | Enable Homing | 1 |

### MCP Protocol (MCP Client → PhysiClaw)

Tools communicate via stdio or SSE with JSON messages. MCP is a standard, language-agnostic protocol.

## Tech Stack

### Language

Python 3.12+

### Key dependencies

(All installed automatically by `install.sh` — listed here for reference.)

- `pyserial` — send G-code to the GRBL board over USB serial
- `opencv-python` — USB camera capture
- `mcp` — MCP server framework
- `rapidocr` + `onnxruntime` — on-device OCR + icon detection
- `httpx`, `typer`, `croniter` — runtime, CLI, scheduling

No Anthropic SDK needed — Claude (or any other LLM) runs on the MCP client side.

### Platform Compatibility

Mac / Windows / Linux (Raspberry Pi) all supported. The only platform difference is serial device names (Mac: `/dev/tty.usbserial-xxx`, Windows: `COM3`, Linux: `/dev/ttyUSB0`).

## Code Structure

```text
physiclaw/
├── server.py         # MCP Server entry point, exposes tools
├── core.py           # Central orchestrator (arm + camera + calibration)
├── arm.py            # GRBL G-code controller (tap, swipe, move)
├── camera.py         # USB camera capture and green flash detection
├── vision.py         # YOLOX phone detection and camera discovery
├── calibrate.py      # 5-phase calibration workflow
└── grbl.py           # Auto-detect GRBL serial port
```

## Operation

The AI agent does not output coordinates — only direction and distance level. Each step is verified by photo.

**Directions:** up / down / left / right / up-left / up-right / down-left / down-right

**Distance Levels:**

| Level | Think of it as... | Physical Displacement |
| ----- | ----------------- | --------------------- |
| large | half the screen away | 20mm |
| medium | a few icons away | 8mm |
| small | one icon away | 3mm |
| nudge | almost there, fine-tune | 1mm |

**Full Operation Cycle:**

```text
1. park()              → Retract stylus out of frame
2. screenshot()        → Clean screenshot, AI sees screen content
3. AI decides          → e.g. "move down-right, large"
4. move(dir, dist)     → Stylus moves toward target
5. screenshot()        → AI checks stylus position (stylus visible)
6. Aligned?
   → No:  back to step 3 (AI re-evaluates and adjusts)
   → Yes: tap()  → Stylus touches screen
7. park()              → Retract stylus out of frame
8. screenshot()        → Verify result, continue next action
```

### Gesture Implementation

**Single Tap:** G0 to target → stylus down → hold 50-100ms → stylus up

**Long Press:** G0 to target → stylus down → hold 800ms → stylus up

**Swipe:** G0 to start → stylus down → G4 P0.03 → G1 to end F3000 → G4 P0.03 → stylus up

**Double Tap:** stylus down 50ms → up → wait 100ms → stylus down 50ms → up (interval < 300ms)

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
