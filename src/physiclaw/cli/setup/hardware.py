"""``physiclaw setup hardware`` — interactive arm + camera calibration.

Talks to a running ``physiclaw server`` over HTTP. Migrated verbatim from
the old ``scripts/setup.py`` — only the entry point changed.
"""

import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from typing import Annotated

import typer

from physiclaw import paths

BASE = os.environ.get("PHYSICLAW_SERVER", "http://localhost:8048")


def _viewport_cache_candidates() -> list:
    root = paths.calibration_cache_dir()
    return [root / "viewport.png", root / "viewport.jpg"]


def api(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body else (b"" if method == "POST" else None)
    hdrs = {"Content-Type": "application/json"} if body else {}
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return None
    except Exception:
        return None


def ok(r):
    return r is not None and r.get("status") == "ok"


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def wait(msg):
    input(f"  {msg} [Enter] ")


def ask(msg, auto):
    return True if auto else input(f"  {msg} [Enter/q] ").strip().lower() != "q"


def calibrate(step, timeout=60):
    return api("POST", f"/api/calibrate/{step}", timeout=timeout)


def calibrate_retry(step, fail_msg, retry_prompt, auto, predicate=None, timeout=30):
    if predicate is None:
        predicate = ok
    while True:
        r = calibrate(step, timeout)
        if predicate(r):
            return r
        msg = fail_msg(r) if callable(fail_msg) else fail_msg
        _fail(msg)
        if auto or not ask(retry_prompt, auto=False):
            sys.exit(1)


def _done(msg="OK"):
    print(f"  \033[32m✓\033[0m {msg}")


def _fail(msg):
    print(f"  \033[31m✗ {msg}\033[0m")


def _warn(msg):
    print(f"  \033[33m⚠ {msg}\033[0m")


def run(auto: bool = False, trace: bool = False) -> None:
    t0 = time.time()

    status = api("GET", "/api/status")
    if not status:
        sys.exit("Server not running. Start: physiclaw server")
    if status.get("ready"):
        print("Already ready.")
        return
    if status.get("calibrated"):
        print("Already calibrated, finalizing...")
        api("POST", "/api/phone/home")
        time.sleep(3)
        api("POST", "/api/ready")
        _done("Phone on Home Screen, PhysiClaw ready")
        return

    print("\n── 1. Scan QR code ──")
    print(f"  Phone URL: http://{lan_ip()}:8048/bridge")
    if not auto:
        webbrowser.open(f"{BASE}/api/bridge/qr")
        wait("Scan QR on phone, confirm page shows 'PhysiClaw'")
    _done("Phone page ready")

    print("\n── 2. Position phone ──")
    if not auto:
        subprocess.run(["open", "-a", "Photo Booth"])
        wait("Place phone under camera, adjust in Photo Booth, then close Photo Booth")
        _done("Phone positioned")
    else:
        _done("Skipped (auto)")

    print("\n── 3. Connect arm ──")
    if ask("USB plugged, power ON, stylus on?", auto):
        if not ok(api("POST", "/api/connect-arm")):
            _fail("Arm connection failed")
            sys.exit(1)
    _done("Arm connected")

    print("\n── 4. Connect camera ──")
    print("  Auto-picking by the RGBY corner markers on /bridge.")
    print("  If this fails, refresh /bridge in Safari (pull-to-refresh)")
    print("  so it picks up the latest page, then retry.")
    r = api("POST", "/api/connect-camera", {"index": "auto"}, timeout=30)
    if ok(r):
        cam = r.get("index", 0)
        _done(f"Camera {cam} auto-picked")
    else:
        subprocess.run("rm -f /tmp/physiclaw_cam*.jpg", shell=True)
        for i in range(4):
            rr = api("GET", f"/api/camera-preview/{i}?watermark=1", timeout=10)
            if rr and rr.get("image"):
                with open(f"/tmp/physiclaw_cam{i}.jpg", "wb") as f:
                    f.write(base64.b64decode(rr["image"]))
        if auto:
            cam = 0
        else:
            subprocess.run("open /tmp/physiclaw_cam*.jpg", shell=True)
            try:
                cam = int(
                    input(
                        "  Auto-pick failed. Which camera? [0-3, default=0]: "
                    ).strip()
                )
            except ValueError:
                cam = 0
        if not ok(api("POST", "/api/connect-camera", {"index": cam})):
            _fail("Camera connection failed")
            sys.exit(1)
        _done(f"Camera {cam} connected")

    print("\n── 5. Viewport shift ──")
    vp_cache = next(
        (p for p in _viewport_cache_candidates() if p.exists()), None
    )
    if vp_cache is not None:
        print(f"  Using cached screenshot: {vp_cache} (delete to re-measure)")
    else:
        print("  Phone shows an orange square.")
        print("  Tap AssistiveTouch once (screenshot), then double-tap (upload).")
    while True:
        if ok(calibrate("viewport-shift", 35)):
            break
        wait("Failed. Tap AT once, then double-tap. Ready to retry?")
    _done("Viewport shift measured")

    print("\n── 6. Position stylus ──")
    r = api("POST", "/api/bridge/switch", {"mode": "calibrate", "phase": "center"})
    if not r or not r.get("ok"):
        _fail("Failed to show orange circle on phone — is the bridge page open?")
        sys.exit(1)
    time.sleep(0.5)
    print("  Phone should show an orange circle at screen center.")
    print("  If screen is off, wake the phone and reopen the bridge page.")
    if not auto:
        wait("Position stylus tip above the orange circle (~3mm above screen)")
    _done("Stylus positioned")

    print("\n── 7. Arm calibration ──")
    print("  One pass: find Z depth, tap 18 points, fit screen→arm mapping.")
    if ask("Don't touch anything. Ready?", auto):
        def _arm_fail(resp):
            return (
                "Arm calibration failed: "
                f"{(resp or {}).get('message', 'no response')}"
            )

        r = calibrate_retry("arm", _arm_fail, "Retry?", auto, timeout=120)
        z_note = " (cached)" if r.get("z_cached") else ""
        tilt = r.get("tilt_ratio", 0)
        if not r.get("aligned"):
            _fail(
                f"Phone/arm axes skewed (tilt {tilt*100:.1f}%) — "
                "straighten phone and rerun if this persists"
            )
        _done(
            f"Arm ready: z_tap={r.get('z_tap')}mm{z_note}, "
            f"{r.get('pairs')} tap pairs, tilt={tilt:.3f}"
        )

    print("\n── 8. Camera calibration ──")
    print("  Adjust camera, then detect rotation + check phone fills the frame.")
    if not auto:
        subprocess.run(["open", "-a", "Photo Booth"])
        wait("Adjust camera angle/distance if needed, then close Photo Booth")
    api("POST", "/api/connect-camera", {"index": cam})
    r = calibrate("camera", 15)
    if not ok(r):
        _fail(f"Camera calibration failed: {r}")
        sys.exit(1)
    for issue in r.get("issues") or []:
        _warn(issue)
    _done(
        f"Camera ready: {r.get('rotation_name')}, coverage {r.get('coverage'):.0%}"
    )

    print("\n── 9. Camera mapping ──")
    print("  Camera detects 15 red dots on phone screen.")
    if ask("Ready?", auto):
        calibrate_retry(
            "camera-mapping",
            lambda r: (
                "Camera mapping failed: "
                f"{(r or {}).get('message', 'no response')}"
            ),
            "Adjust lighting/glare. Retry?",
            auto,
        )
    _done("Screen→camera mapping computed")

    print("\n── 10. Validate ──")
    print("  Arm taps random dots and compares touch vs expected position.")
    if ask("Ready?", auto):
        r = calibrate("validate", 60)
        if not (r and r.get("calibrated")):
            print(f"  {json.dumps(r, ensure_ascii=False) if r else 'no response'}")
            _fail("Validation failed")
            sys.exit(1)
    _done("Calibration validated")

    print("\n── 11. AssistiveTouch ──")
    print("  Verifying screenshot + clipboard pipeline.")
    calibrate("assistive-touch/show")
    if not auto:
        wait("Drag AssistiveTouch button to overlap the orange circle")

    def _at_fail(resp):
        msg = "AT verification failed — check AT position and iOS Shortcuts"
        clip = (resp or {}).get("clipboard") or {}
        if clip.get("fetched"):
            msg += f" (clipboard fetched: {clip.get('text')!r})"
        return msg

    r = calibrate_retry(
        "assistive-touch/verify",
        _at_fail,
        "Adjust AT position. Retry?",
        auto,
        predicate=lambda resp: resp and resp.get("passed"),
        timeout=20,
    )
    if r.get("clipboard", {}).get("fetched"):
        print(f"  Clipboard text: {r['clipboard'].get('text')}")
        if not auto:
            wait("Paste in Notes to verify it matches")
    _done("Screenshot + clipboard pipeline verified")

    if trace:
        print("\n── 12. Edge trace ──")
        print("  Arm traces phone screen border clockwise, pausing at 8 points.")
        if ask("Watch for accuracy. Ready?", auto):
            calibrate("trace-edge", 60)
        _done("Edge trace complete")

    print("\n── Home Screen ──")
    api("POST", "/api/phone/home")
    time.sleep(3)
    api("POST", "/api/ready")
    _done("Phone on Home Screen, PhysiClaw ready")

    elapsed = time.time() - t0
    mins, secs = int(elapsed // 60), int(elapsed % 60)
    print(f"\n{'='*40}")
    print(f"  Setup completed in {mins}m {secs}s")
    _done("PhysiClaw is ready. All MCP tools available.")
    print(f"{'='*40}")


def hardware(
    auto: Annotated[
        bool,
        typer.Option("-y", "--yes", help="Auto mode: skip prompts."),
    ] = False,
    trace: Annotated[
        bool,
        typer.Option("--trace", help="Run the optional edge-trace step."),
    ] = False,
    server_url: Annotated[
        str,
        typer.Option(
            "--server-url",
            help="Running MCP server URL. Defaults to $PHYSICLAW_SERVER or "
            "http://localhost:8048.",
        ),
    ] = BASE,
) -> None:
    """Calibrate the robotic arm + camera (server must be running)."""
    global BASE
    BASE = server_url
    run(auto=auto, trace=trace)
