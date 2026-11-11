"""Subprocess fixture: real server/launcher/HTTP/MCP, fake hooks and react.

Run only through test_runtime_shutdown.py. No phone or model API is used.
"""

import _thread
import asyncio
import importlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time


def read_interrupts(loop=None) -> None:
    """Inject Ctrl+C without requiring a shared console on a Windows CI runner.

    This pipe belongs to the test harness. The managed runtime still receives
    its real, separate stdin EOF from the application parent during shutdown.
    """
    from physiclaw.port import processes as process_port

    process_port.prepare_managed_runtime()

    def read():
        while os.read(sys.stdin.fileno(), 1):
            if loop is None:
                _thread.interrupt_main()
            else:
                # Wake a standalone runtime even during its 600-second sleep.
                loop.call_soon_threadsafe(_thread.interrupt_main)

    threading.Thread(target=read, name="test-interrupt", daemon=True).start()


def child(base: str, mode: str) -> None:
    from physiclaw.agent.engine import engine, mcp_tool
    from physiclaw.agent.runtime import launcher, runtime
    from physiclaw.agent.runtime.hook import Trigger

    if mode == "unresponsive":
        print("CHILD_UNRESPONSIVE", flush=True)
        while True:
            time.sleep(1)

    owner = None
    real_close = mcp_tool.close_mcp

    async def close():
        assert owner is None or asyncio.current_task() is owner
        await real_close()
        print("CLOSE_MCP_DONE", flush=True)

    async def react(*args, **kwargs):
        nonlocal owner
        owner = asyncio.current_task()
        print("REACT_STARTED", flush=True)
        mcp = await mcp_tool.get_mcp()
        await mcp.list_tools()
        print("MCP_READY", flush=True)
        if mode == "model":
            await asyncio.sleep(0.5)
            # A model response arriving after stop cannot schedule a tool.
            await mcp.call_tool("finish")
        elif mode == "tool":
            await mcp.call_tool("finish")
        if mode == "standalone":
            read_interrupts(asyncio.get_running_loop())
        print("REACT_DONE", flush=True)

    async def hooks():
        return [Trigger(description="test", source="test")]

    engine.run = react
    runtime.load_hooks = lambda: None
    runtime.check_hooks = hooks
    runtime.CONFIG.engine.react_cooldown_seconds = 600
    launcher.close_mcp = close
    launcher.setup_logging = lambda *args, **kwargs: None
    if mode == "early-exit":

        async def early_return(self):
            await asyncio.sleep(0.1)  # The pipe reader is already blocked.

        runtime.Runtime.start = early_return
    sys.argv = ["runtime", "--server", base, "--interval", "600"]
    if mode != "standalone":
        sys.argv.append("--managed")
    launcher.launch()
    print("CHILD_EXIT", flush=True)


def parent(port: int, mode: str, listeners: list[socket.socket]) -> None:
    import httpx
    from mcp.server.mcpserver import MCPServer
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from physiclaw.cli.runtime_process import ManagedRuntime
    from physiclaw.common import logger
    from physiclaw.core import server as core_server

    server_mod = importlib.import_module("physiclaw.cli.server")

    mcp = MCPServer("shutdown-test")
    ready = mode not in {"not-ready", "unconfigured"}

    @mcp.custom_route("/api/status", methods=["GET"])
    async def status(request):
        return JSONResponse({"ready": ready})

    @mcp.custom_route("/api/ready", methods=["POST"])
    async def complete_setup(request):
        nonlocal ready
        ready = True
        return JSONResponse({"ok": True})

    @mcp.tool()
    async def finish() -> str:
        print("TOOL_STARTED", flush=True)

        # Model a hardware thread completing a screenshot via the phone bridge.
        # Closing/cancelling the MCP request cannot stop this thread.
        def hardware():
            time.sleep(0.5)
            with httpx.Client(trust_env=False) as client:
                response = client.get(f"http://127.0.0.1:{port + 1}/bridge")
                response.raise_for_status()
            print("HARDWARE_DONE", flush=True)

        await asyncio.to_thread(hardware)
        return "finished"

    async def bridge(request):
        return JSONResponse({"bridge": "alive"})

    control = mcp.streamable_http_app()
    bridge_app = Starlette(routes=[Route("/bridge", bridge)])
    if mode in {"startup-failure", "bridge-failure"}:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fail(app):
            raise RuntimeError(f"deliberate {mode}")
            yield

        failing_app = control if mode == "startup-failure" else bridge_app
        failing_app.router.lifespan_context = fail
    core_server.build_apps = lambda host: (control, bridge_app)
    core_server.shutdown = lambda: print("HARDWARE_SHUTDOWN", flush=True)
    logger.setup_logging = lambda *args, **kwargs: None
    server_mod._require_vision_model = lambda: None
    server_mod._run_startup_maintenance = lambda **kwargs: None
    server_mod._log_endpoints = lambda *args, **kwargs: None
    server_mod._start_hardware_bringup = lambda *args, **kwargs: print(
        "SERVICES_STARTED", flush=True
    )
    inherited = iter(listeners)

    def open_listener(host, number):
        listener = next(inherited)
        assert listener.getsockname() == (host, number)
        listener.listen()
        return listener

    server_mod._open_listener = open_listener
    real_popen = subprocess.Popen

    def spawn(cmd, **kwargs):
        assert "--managed" in cmd
        proc = real_popen(
            [sys.executable, __file__, "child", f"http://127.0.0.1:{port}", mode],
            **kwargs,
        )
        print(f"CHILD_PID={proc.pid}", flush=True)
        if mode == "early-exit":
            # Keep stdin open through interpreter finalization; CHILD_EXIT
            # alone is too early to detect a blocked BufferedReader daemon.
            proc.wait(timeout=5)
        return proc

    server_mod.subprocess.Popen = spawn
    if mode == "unresponsive":
        server_mod.ManagedRuntime = lambda proc: ManagedRuntime(proc, timeout=0.3)
    if mode == "server-first":
        serve = server_mod._serve

        def wrong_order(*args, **kwargs):
            runtime = kwargs.pop("runtime")
            serve(*args, **kwargs)
            asyncio.run(runtime.stop())

        server_mod._serve = wrong_order
    read_interrupts()
    try:
        server_mod.server(
            host="127.0.0.1",
            port=port,
            no_setup_hardware=True,
            no_runtime=mode == "external",
        )
    finally:
        for listener in listeners:
            listener.close()
        print("PARENT_EXIT", flush=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if sys.argv[3] == "unconfigured":
        for name in list(os.environ):
            if name.endswith("_API_KEY") or name == "PHYSICLAW_MODEL":
                os.environ.pop(name)
    else:
        os.environ["PHYSICLAW_MODEL"] = "openai/test"
        os.environ["OPENAI_API_KEY"] = "synthetic-test-key"
    if sys.argv[1] == "child":
        child(sys.argv[2], sys.argv[3])
    else:
        if sys.platform == "win32":
            listeners = [
                socket.fromshare(bytes.fromhex(shared))
                for shared in json.loads(sys.stdin.buffer.readline())
            ]
        else:
            listeners = [socket.socket(fileno=int(fd)) for fd in sys.argv[4:]]
        parent(
            int(sys.argv[2]),
            sys.argv[3],
            listeners,
        )
