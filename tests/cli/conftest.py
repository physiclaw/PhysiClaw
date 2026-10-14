"""Boundary fakes for CLI commands that drive a phone or model."""

from types import SimpleNamespace

import pytest


@pytest.fixture
def playbook_mcp(mocker):
    client = mocker.MagicMock()
    client.__aenter__ = mocker.AsyncMock(return_value=client)
    client.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("physiclaw.agent.engine.mcp_tool.McpClient", return_value=client)
    return client


@pytest.fixture
def micro_session(mocker):
    from physiclaw.conductor.bench.decisions import Recorded, Reply, Row

    recorded = Recorded("move", "choose", ("done", "escalate"), (), "done", 0.9, "low")
    reply = Reply("done", 0.8, True, 100, 12, 2)
    provider = mocker.Mock(aclose=mocker.AsyncMock())
    factory = mocker.patch("physiclaw.provider.make_provider", return_value=provider)
    load = mocker.patch(
        "physiclaw.conductor.bench.decisions.load", return_value=[recorded]
    )
    ask = mocker.patch(
        "physiclaw.conductor.bench.decisions.ask",
        new=mocker.AsyncMock(return_value=Row(recorded, (reply,))),
    )
    mocker.patch("physiclaw.cli._sessions.resolve_sid", return_value="recorded-session")
    return SimpleNamespace(
        recorded=recorded,
        reply=reply,
        provider=provider,
        factory=factory,
        load=load,
        ask=ask,
    )
