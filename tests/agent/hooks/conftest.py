"""Load hooks with their real startup state and isolated registration."""

import runpy

import httpx
import pytest_asyncio

from physiclaw.agent.runtime import hook


@pytest_asyncio.fixture
async def fresh_hook_module(monkeypatch):
    namespaces = []
    monkeypatch.setattr(hook, "_hooks", [])

    def load(module, function_name):
        namespace = runpy.run_path(module.__file__)
        # run_path returns a copy; functions retain the live module globals.
        namespaces.append(namespace[function_name].__globals__)

    yield load

    for namespace in namespaces:
        client = namespace.get("_client")
        if isinstance(client, httpx.AsyncClient):
            await client.aclose()
