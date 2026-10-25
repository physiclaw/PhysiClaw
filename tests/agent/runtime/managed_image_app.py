"""Fresh-process fixture for test_managed_startup.py; no hardware or APIs."""

import asyncio
import signal
import sys
from pathlib import Path


async def main():
    from physiclaw.agent.hooks import poll
    from physiclaw.agent.runtime import launcher
    from physiclaw.common.image import image_dimensions

    # Neither startup hooks nor launcher imports may warm up the native stack
    # before _run_runtime gets a chance to exercise its actual startup order.
    assert "numpy" not in sys.modules
    assert "cv2" not in sys.modules
    checkout = Path(__file__).resolve().parents[3] / "src"
    assert Path(launcher.__file__).resolve().is_relative_to(checkout)
    raw = Path(__file__).with_name("fixtures").joinpath("observation.png").read_bytes()
    loop = asyncio.get_running_loop()
    owner = asyncio.current_task()
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    closed = []
    control_reading = asyncio.Event()
    real_read = launcher.os.read

    def read(fd, size):
        if fd == sys.stdin.fileno():
            loop.call_soon_threadsafe(control_reading.set)
        return real_read(fd, size)

    launcher.os.read = read

    def track_close(name, close):
        async def tracked():
            assert asyncio.current_task() is owner
            await close()
            closed.append(name)

        return tracked

    launcher.close_mcp = track_close("mcp", launcher.close_mcp)
    launcher.close_status_client = track_close("status", launcher.close_status_client)
    poll.close_client = track_close("watch", poll.close_client)

    class ImageRuntime:
        def __init__(self):
            self.stopped = asyncio.Event()

        async def start(self):
            # Wait for the actual reader to reach os.read, not an arbitrary
            # scheduling delay. The parent bounds this wait and keeps stdin open.
            await control_reading.wait()
            assert image_dimensions(raw) == (2, 3)
            print("IMAGE_VALIDATED", flush=True)
            await self.stopped.wait()

        def stop(self):
            assert asyncio.current_task() is None  # call_soon callback on the loop
            self.stopped.set()
            print("STOP_REQUESTED", flush=True)

    await launcher._run_runtime(ImageRuntime(), managed=True)
    assert closed == ["mcp", "status", "watch"], closed
    assert all(signal.getsignal(sig) == handler for sig, handler in handlers.items())
    print("CLEAN_EXIT", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
