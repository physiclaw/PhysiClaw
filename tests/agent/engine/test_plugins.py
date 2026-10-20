"""Plugin discovery, identity validation and composition with the default LLM.

The loader produces optional slots. The session composition root always
appends the LLM; all slots obey the same invocation contract.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from engine_fakes import _arbitration_run, _turn_metadata

from physiclaw.agent.engine import plugins as plugins_mod
from physiclaw.agent.engine.provenance import PluginBinding
from physiclaw.agent.engine.session import Session
from physiclaw.common.config import CONFIG, DEBUG_ENV_VAR
from physiclaw.contract.plugin import (
    PluginRegistration,
    PluginServices,
    TurnPlugin,
)


def test_none_is_the_off_switch(monkeypatch, caplog) -> None:
    # The rollback switch: `[agent] plugins = "none"` → every turn is a
    # plain provider call, exactly the pre-plugin engine — and the log
    # says so, so a plugin-less session reads as a choice in forensics.
    monkeypatch.setattr(CONFIG.agent, "plugins", "none")
    with caplog.at_level("DEBUG"):
        assert plugins_mod.load_plugins() == ()
    assert "disabled by config" in caplog.text
    monkeypatch.setattr(CONFIG.agent, "plugins", " None ")
    assert plugins_mod.load_plugins() == ()


def test_empty_config_resolves_to_the_builtin_default(caplog) -> None:
    # The config layer defaults to "" and names no plugin; the loader
    # owns the default set. Playbook then decision are on by default — this
    # test rides the real dotted path on purpose: if it rots, the
    # engine silently loses its conductor. The resolved set is logged
    # per session ("was the conductor even in play?").
    assert CONFIG.agent.plugins == ""
    with caplog.at_level("DEBUG"):
        plugins = plugins_mod.load_plugins()
    assert [(p.name, p.id) for p in plugins] == [
        ("Playbook", "physiclaw.playbook"),
        ("Decision", "physiclaw.decision"),
    ]
    assert isinstance(plugins[0].plugin, TurnPlugin)
    assert isinstance(plugins[1].plugin, TurnPlugin)
    assert plugins[0].presentation == "playbook"
    assert plugins[1].presentation == "plugin"
    assert all(path in caplog.text for path in plugins_mod.DEFAULT_PLUGINS.split(","))


def test_unimportable_path_is_skipped(monkeypatch, caplog) -> None:
    monkeypatch.setattr(CONFIG.agent, "plugins", "no.such.module:build")
    assert plugins_mod.load_plugins() == ()
    assert "failed to load" in caplog.text


def test_factory_crash_is_skipped(monkeypatch, caplog) -> None:
    # `builtins:iter` with no argument raises inside the factory call.
    monkeypatch.setattr(CONFIG.agent, "plugins", "builtins:iter")
    assert plugins_mod.load_plugins() == ()
    assert "failed to load" in caplog.text


def test_wrong_registration_is_skipped(monkeypatch, caplog) -> None:
    # `builtins:object` builds fine but satisfies nothing.
    monkeypatch.setattr(CONFIG.agent, "plugins", "builtins:object")
    assert plugins_mod.load_plugins() == ()
    assert "factory must return PluginRegistration" in caplog.text


def test_registered_factory_must_create_a_turn_plugin(monkeypatch, mocker, caplog):
    monkeypatch.setattr(CONFIG.agent, "plugins", "test.plugin:build")
    registration = PluginRegistration("test.plugin", "Test", object)
    mocker.patch.object(
        plugins_mod.importlib,
        "import_module",
        return_value=SimpleNamespace(build=lambda: registration),
    )

    assert plugins_mod.load_plugins() == ()
    assert "does not satisfy TurnPlugin" in caplog.text


@pytest.mark.parametrize(
    "field,value",
    [("name", ""), ("id", ""), ("name", "  "), ("id", " test.plugin"), ("id", None)],
)
def test_invalid_registration_identity_is_rejected_before_creation(
    monkeypatch, mocker, field, value
):
    monkeypatch.setattr(CONFIG.agent, "plugins", "test.plugin:build")
    create = mocker.Mock()
    metadata = {"name": "Test", "id": "test.plugin"}
    metadata[field] = value
    mocker.patch.object(
        plugins_mod.importlib,
        "import_module",
        return_value=SimpleNamespace(
            build=lambda: PluginRegistration(create=create, **metadata)
        ),
    )

    assert plugins_mod.load_plugins() == ()
    create.assert_not_called()


def test_duplicate_id_is_rejected_before_creating_the_second_instance(
    monkeypatch, mocker, caplog
):
    from physiclaw.decision.plugin import DecisionPlugin

    first = mocker.Mock(return_value=DecisionPlugin())
    duplicate = mocker.Mock(return_value=DecisionPlugin())
    registrations = iter(
        [
            PluginRegistration("test.decision", "Decision", first),
            PluginRegistration("test.decision", "Another decision", duplicate),
        ]
    )
    monkeypatch.setattr(CONFIG.agent, "plugins", "test.plugin:build,test.plugin:build")
    mocker.patch.object(
        plugins_mod.importlib,
        "import_module",
        return_value=SimpleNamespace(build=lambda: next(registrations)),
    )

    bindings = plugins_mod.load_plugins()

    assert [b.id for b in bindings] == ["test.decision"]
    first.assert_called_once_with()
    duplicate.assert_not_called()
    assert "duplicate plugin id" in caplog.text


def test_distinct_registrations_cannot_share_a_runtime_instance(
    monkeypatch, mocker, caplog
):
    from physiclaw.decision.plugin import DecisionPlugin

    shared = DecisionPlugin()
    registrations = iter(
        [
            PluginRegistration("test.first", "First", lambda: shared),
            PluginRegistration("test.second", "Second", lambda: shared),
        ]
    )
    monkeypatch.setattr(CONFIG.agent, "plugins", "test.plugin:build,test.plugin:build")
    mocker.patch.object(
        plugins_mod.importlib,
        "import_module",
        return_value=SimpleNamespace(build=lambda: next(registrations)),
    )

    bindings = plugins_mod.load_plugins()

    assert [b.id for b in bindings] == ["test.first"]
    assert bindings[0].plugin is shared
    assert "distinct instance" in caplog.text


async def test_default_stack_without_a_playbook_or_decision_uses_the_llm(mocker):
    from engine_fakes import _asst, _tc

    from physiclaw.agent.engine.loop import _advance

    bindings = plugins_mod.load_plugins()
    provider = SimpleNamespace(chat=mocker.AsyncMock())
    answer = _asst(tool_calls=[_tc("note"), _tc("end_session", {"status": "DONE"})])
    provider.chat.return_value = answer
    try:
        for binding in bindings:
            context = PluginServices(session_provider=provider)
            await binding.plugin.session_setup(context)
        result = await _advance(
            _arbitration_run(provider.chat, plugins=bindings, attempts=1),
            Session(),
            _turn_metadata(),
        )

        assert result.binding.id == "physiclaw.llm" and result.message is answer
        provider.chat.assert_awaited_once()
        assert provider.chat.call_args.kwargs["metadata"] == _turn_metadata()
    finally:
        for binding in bindings:
            await binding.plugin.aclose()


@pytest.mark.parametrize("outcome", ["accepted", "low_probability", "timeout"])
async def test_active_decision_uses_generic_arbitration_and_private_contexts(
    observation_blocks, mocker, monkeypatch, outcome
):
    import asyncio
    import time

    from engine_fakes import _asst, _tc

    from physiclaw.agent.engine import loop
    from physiclaw.contract.decision import DecisionAnswer, DecisionResult
    from physiclaw.contract.observation import OBSERVATION_META
    from physiclaw.decision.plugin import DecisionPlugin

    monkeypatch.setattr(CONFIG.decision, "model", "test/decision")
    monkeypatch.setattr(CONFIG.decision, "mode", "active")
    monkeypatch.setattr(
        CONFIG.decision, "timeout_seconds", 0.01 if outcome == "timeout" else 2.0
    )
    blocks = observation_blocks()
    blocks[0]["_meta"][OBSERVATION_META]["observed_after"] = time.time()
    session = Session()
    session.facts.observe(blocks, tool="peek")
    session.plan.update(
        user_said="Open search",
        understanding="private LLM understanding",
        steps=[{"content": "Tap search", "status": "in_progress"}],
    )

    async def evaluate(request, *, deadline):
        if outcome == "timeout":
            await asyncio.Event().wait()
        selected = {"phase": "ROUTINE", "action_type": "tap", "target": "b1"}
        answers = {}
        for question in request.questions:
            probability = (
                0.6
                if outcome == "low_probability" and question.id == "action_type"
                else 0.99
            )
            answers[question.id] = DecisionAnswer(
                selected[question.id],
                probabilities={
                    choice.id: probability
                    if choice.id == selected[question.id]
                    else (1 - probability) / (len(question.candidates) - 1)
                    for choice in question.candidates
                },
            )
        return DecisionResult(
            request.id,
            answers,
            provider_id="test",
            model_id="decision",
            requested_model_id="decision",
        )

    decision_provider = SimpleNamespace(
        evaluate=mocker.AsyncMock(side_effect=evaluate), aclose=mocker.AsyncMock()
    )
    plugin = DecisionPlugin()
    decision_advance = mocker.spy(plugin, "advance")
    llm_answer = _asst(tool_calls=[_tc("note"), _tc("peek")])
    chat = mocker.AsyncMock(return_value=llm_answer)
    run = _arbitration_run(
        chat,
        session=session,
        messages=[_asst(content="private LLM conversation")],
        plugins=(PluginBinding("physiclaw.decision", plugin, "Decision"),),
    )
    run.schema_by_name = {
        "tap": {
            "input_schema": {
                "type": "object",
                "properties": {"bbox": {"type": "array", "minItems": 4, "maxItems": 4}},
                "required": ["bbox"],
            }
        }
    }
    llm_advance = mocker.spy(run.plugins[-1].plugin, "advance")
    validate_proposal = mocker.spy(loop, "_proposed_message")
    events = []

    def write(event):
        events.append(event)
        return True

    await plugin.session_setup(
        PluginServices(
            session_provider=run.plugins[-1].plugin.provider,
            events=SimpleNamespace(write=write),
            decision_provider_factory=lambda *args, **kwargs: decision_provider,
        )
    )
    metadata = _turn_metadata()
    try:
        result = await loop._advance(run, session, metadata)
        decision_provider.evaluate.assert_awaited_once()
        request = decision_provider.evaluate.call_args.args[0]
        assert "private LLM" not in repr(request.context)
        assert request.context.observation_id == session.facts.observation.id
        assert decision_advance.call_args.kwargs["metadata"] is metadata
        validate_proposal.assert_called_once()

        if outcome == "accepted":
            assert result.binding.id == "physiclaw.decision"
            assert result.message.tool_names() == ["tap"]
            target = session.facts.observation.ocr[0]
            assert result.message.tool_calls[0].arguments == {"bbox": list(target.bbox)}
            proposal = validate_proposal.call_args.args[0]
            assert proposal.observation_id == session.facts.observation.id
            assert proposal.targets == {
                result.message.tool_calls[0].id: session.facts.observation.element_ref(
                    target
                )
            }
            llm_advance.assert_not_awaited()
            chat.assert_not_awaited()
        else:
            assert result.binding.id == "physiclaw.llm"
            assert result.message is llm_answer
            llm_advance.assert_awaited_once()
            assert llm_advance.call_args.args[0] is decision_advance.call_args.args[0]
            assert llm_advance.call_args.kwargs["metadata"] is metadata
            chat.assert_awaited_once()
            assert request.id not in repr(chat.call_args)
        call = next(event for event in events if event["event"] == "decision_call")
        if outcome == "timeout":
            assert call["status"] == "error" and call["error"] == "TimeoutError"
        else:
            assert call["threshold_gate"]["status"] == (
                "passed" if outcome == "accepted" else "failed"
            )
        assert not run.mcp.tool_calls
    finally:
        await plugin.aclose()
    decision_provider.aclose.assert_awaited_once()


def test_bad_paths_do_not_sink_good_ones(monkeypatch) -> None:
    monkeypatch.setattr(
        CONFIG.agent,
        "plugins",
        f"no.such.module:build, {plugins_mod.DEFAULT_PLUGINS}",
    )
    assert len(plugins_mod.load_plugins()) == 2


# ---------- the seam's own belt: a plugin bug never kills the session ----------


@pytest.mark.asyncio
async def test_advance_passes_a_crashing_plugin_to_the_provider() -> None:
    from engine_fakes import _asst, _tc

    from physiclaw.agent.engine.loop import _advance

    class Crashing:
        async def advance(self, messages, *, metadata):
            raise RuntimeError("plugin bug")

    spoken = _asst(tool_calls=[_tc("note"), _tc("peek")])

    async def chat(messages, tools, *, metadata):
        return spoken

    turn = await _advance(
        _arbitration_run(
            chat,
            plugins=(PluginBinding("crashing", Crashing(), name="test"),),
            attempts=1,
        ),
        Session(),
        _turn_metadata(),
    )

    assert turn.message is spoken
    assert turn.binding.id == "physiclaw.llm"


@pytest.mark.parametrize("interceptor", [object(), SimpleNamespace(intercept=None)])
def test_regression_debug_loader_skips_invalid_interceptor(
    monkeypatch, mocker, interceptor, caplog
):
    monkeypatch.setenv(DEBUG_ENV_VAR, "1")
    module = SimpleNamespace(build=lambda: interceptor)
    mocker.patch.object(plugins_mod.importlib, "import_module", return_value=module)

    result = plugins_mod.load_debug_intercept()

    assert result is None
    assert "failed to load" in caplog.text
    assert "fake-channel active" not in caplog.text


def test_debug_loader_returns_valid_intercept(monkeypatch, mocker):
    monkeypatch.setenv(DEBUG_ENV_VAR, "1")

    def intercept(*args):
        return None

    module = SimpleNamespace(build=lambda: SimpleNamespace(intercept=intercept))
    mocker.patch.object(plugins_mod.importlib, "import_module", return_value=module)

    result = plugins_mod.load_debug_intercept()

    assert result is intercept
