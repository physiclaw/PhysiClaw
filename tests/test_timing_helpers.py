"""The test clock must preserve cancellation semantics and isolate watchdogs."""

import asyncio

import pytest

from tests.timing_helpers import ManualTimeouts, wait_for


async def test_expiring_inner_scope_cancels_its_work_without_expiring_outer():
    controlled = ManualTimeouts(now=100.0)
    cancelled = []

    async def work():
        controlled.expire()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    async with controlled.timeout_at(110.0) as outer:
        with pytest.raises(TimeoutError):
            await wait_for(controlled.wait_for(work(), timeout=2.0))
        assert not outer.expired()

    assert cancelled == [True]
    assert controlled.scopes[1].expired()
    assert controlled.delays == [10.0, 2.0]


async def test_advancing_application_clock_leaves_real_scheduling_clock_alone():
    real_loop = asyncio.get_running_loop()
    controlled = ManualTimeouts(now=100.0)
    scheduled = real_loop.create_future()
    real_loop.call_soon(scheduled.set_result, "scheduled")

    async with controlled.timeout_at(101.0) as scope:
        controlled.now = 100000.0
        assert await wait_for(scheduled) == "scheduled"
        assert controlled.get_running_loop().time() == 100000.0
        assert asyncio.get_running_loop() is real_loop
        # Expiry is explicit; advancing an application clock must not starve
        # real event-loop callbacks or unexpectedly cancel the test runner.
        assert not scope.expired()
