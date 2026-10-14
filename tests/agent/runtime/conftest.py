"""Exercise module startup state without resetting it in test setup."""

import runpy

import httpx
import pytest_asyncio


@pytest_asyncio.fixture
async def fresh_runtime_module():
    namespaces = []

    def load(module, entry):
        namespace = runpy.run_path(module.__file__)
        # run_path returns a copy; functions retain the live module globals.
        state = namespace[entry].__globals__
        namespaces.append(state)
        return state

    yield load

    for state in namespaces:
        client = state.get("_client")
        if isinstance(client, httpx.AsyncClient):
            await client.aclose()
