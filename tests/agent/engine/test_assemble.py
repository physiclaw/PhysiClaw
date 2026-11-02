"""Request assembly: public tool visibility, stable history and volatile tails.

Accepted mutation survivors affect only dataclass slots or optional counter
defaults. Assembly always supplies both counts; tests cover the computed values
and observable requests rather than the bundle's storage implementation.
"""

from __future__ import annotations

import pytest
from freezegun import freeze_time
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.agent.engine import compact, jobs, pitfalls
from physiclaw.agent.engine.assemble import (
    apply_request_tails,
    build_initial_messages,
    build_prompt_bundle,
    format_triggers,
)
from physiclaw.agent.engine.session import Session
from physiclaw.agent.runtime.hook import Trigger
from physiclaw.common import config, paths
from physiclaw.contract.dto import SystemMessage, UserMessage

MACRO = """kind: macro
schema: 1
name: notify-user
description: Ping the user
enabled: true

steps:
  - home_screen
"""


def _write_macro(text: str = MACRO, name: str = "notify-user") -> None:
    paths.macros_dir().mkdir(parents=True, exist_ok=True)
    (paths.macros_dir() / f"{name}.yml").write_text(text, encoding="utf-8")


@pytest.fixture(params=["absent", "disabled", "enabled"])
def public_macro_state(request):
    if request.param != "absent":
        _write_macro(
            MACRO.replace("enabled: true", f"enabled: {request.param == 'enabled'}")
        )
    return request.param


@pytest.mark.parametrize("provider_id", ["", "openai"])
def test_regression_private_macros_leave_model_request_unchanged(
    private_macro_pack, public_macro_state, provider_id
) -> None:
    baseline = build_prompt_bundle(provider_id, mcp_schemas=[])

    armed = build_prompt_bundle(
        provider_id, pack_macros=private_macro_pack, mcp_schemas=[]
    )

    assert "run_macro" in armed.local_registry
    assert armed.local_schemas == baseline.local_schemas
    assert armed.system_prompt == baseline.system_prompt
    assert armed.macro_count == baseline.macro_count


@freeze_time("2026-10-15T10:00:00")
def test_build_initial_messages_pins_compaction_slots_and_fired_job_context(
    mem_paths, isolated_job_store
):
    triggers = [Trigger(description="time to check", source="cron:check")]
    jobs.create_job(
        id="check",
        description="Check delivery",
        schedule="0 10 15 10 *",
        context="Order 42 is due today",
    )

    messages = build_initial_messages(triggers, "SYSTEM")

    assert messages[0] == SystemMessage(content="SYSTEM")
    assert "cron:check: time to check" in messages[1].content
    assert "Order 42 is due today" in messages[1].content
    assert messages[2:] == [
        UserMessage(content=compact.SUMMARY_INITIAL),
        UserMessage(content=compact.MEMORY_INITIAL),
        UserMessage(content=compact.SKILLS_INITIAL),
    ]


@pytest.mark.parametrize("keep", [0, 3])
def test_apply_request_tails_orders_scratchpad_plan_checkpoint_and_layout(keep):
    session = Session(scratchpad="current notes")
    messages = [SystemMessage(content="cached"), UserMessage(content="wake")]

    request = apply_request_tails(
        messages, session, layout_incomplete=True, compaction_keep=keep
    )

    assert request[:2] == messages
    assert len(messages) == 2
    assert request[2].content == "<scratchpad>\ncurrent notes\n</scratchpad>"
    assert request[3].content == session.plan.render()
    assert request[4] == compact.inject_checkpoint_tail([], keep=keep)[0]
    assert "First-run setup needed" in request[5].content
    assert len(request) == 6


@given(content=st.text(max_size=200))
def test_apply_request_tails_preserves_history_and_replaces_volatile_state(content):
    session = Session(scratchpad=content)
    messages = [UserMessage(content="cached history")]

    first = apply_request_tails(messages, session, layout_incomplete=False)
    session.scratchpad = "next turn"
    second = apply_request_tails(messages, session, layout_incomplete=False)

    assert messages == [UserMessage(content="cached history")]
    assert len(first) == 2 + bool(content.strip())
    assert first[-1].content == session.plan.render()
    assert second == [
        messages[0],
        UserMessage(content="<scratchpad>\nnext turn\n</scratchpad>"),
        UserMessage(content=session.plan.render()),
    ]


def test_build_prompt_bundle_wires_enabled_macros() -> None:
    _write_macro()

    bundle = build_prompt_bundle("", mcp_schemas=[])

    assert bundle.macro_count == 1
    assert "run_macro" in bundle.local_registry
    assert "run_macro" in {s["name"] for s in bundle.local_schemas}
    assert "## Available Macros" in bundle.system_prompt
    assert "**notify-user** — Ping the user" in bundle.system_prompt
    assert "## MACRO.md" in bundle.system_prompt  # doctrine rides along


def test_build_prompt_bundle_without_macros_carries_zero_macro_bytes() -> None:
    # Pay only for what's used: no section, no tool, no MACRO.md doctrine.
    bundle = build_prompt_bundle("", mcp_schemas=[])

    assert bundle.macro_count == 0
    assert bundle.layout_incomplete is True
    assert "run_macro" not in bundle.local_registry
    assert "note" in {s["name"] for s in bundle.local_schemas}
    assert "## Available Macros" not in bundle.system_prompt
    assert "## MACRO.md" not in bundle.system_prompt


def test_build_prompt_bundle_learned_layout_disables_first_run_reminder(mocker):
    mocker.patch("physiclaw.agent.layout.is_learned", return_value=True)

    bundle = build_prompt_bundle("", mcp_schemas=[])

    assert bundle.layout_incomplete is False
    assert "report_screen_layout" not in bundle.local_registry


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("provider_id", ["deepseek", "anthropic", "openai"])
def test_pitfalls_switch_controls_entire_prompt_surface(
    monkeypatch, enabled, provider_id
):
    pitfalls.add(["Example app: old trap → observed fix"])
    monkeypatch.setattr(config.CONFIG.pitfalls, "enabled", enabled)
    if not enabled:
        monkeypatch.setattr(pitfalls, "read", lambda: pytest.fail("disabled read"))

    bundle = build_prompt_bundle(provider_id, mcp_schemas=[])

    assert ("add_pitfall" in bundle.local_registry) is enabled
    assert ("add_pitfall" in {s["name"] for s in bundle.local_schemas}) is enabled
    assert ("pitfall" in bundle.system_prompt.casefold()) is enabled
    assert ("## Learned pitfalls" in bundle.system_prompt) is enabled
    assert ("old trap" in bundle.system_prompt) is enabled
    assert bundle.pitfall_count == int(enabled)
    assert "old trap" in (paths.pitfalls_dir() / "pitfalls.md").read_text(
        encoding="utf-8"
    )


def test_format_triggers_includes_now_and_each_trigger() -> None:
    triggers = [
        Trigger(description="phone IM arrived", source="phone"),
        Trigger(description="cron fired", source="cron:user-greet"),
    ]

    with freeze_time("2026-04-28T14:30:00"):
        out = format_triggers(triggers)

    assert out == (
        "Now: 2026-04-28 Tue 14:30\n\n"
        "[Current wake — act on this]\n"
        "[2026-04-28 Tue 14:30] phone: phone IM arrived\n"
        "[2026-04-28 Tue 14:30] cron:user-greet: cron fired"
    )


def test_format_triggers_uses_manual_for_empty_source() -> None:
    triggers = [Trigger(description="user typed", source="")]

    with freeze_time("2026-04-28T14:30:00"):
        out = format_triggers(triggers)

    assert "manual: user typed" in out


def test_format_triggers_appends_cron_context_when_provided() -> None:
    triggers = [Trigger(description="x", source="phone")]

    with freeze_time("2026-04-28T14:30:00"):
        out = format_triggers(
            triggers, cron_ctx="## Scheduled jobs firing now\n\n### foo"
        )

    assert out.endswith("\n\n## Scheduled jobs firing now\n\n### foo")


def test_format_triggers_omits_cron_section_when_blank() -> None:
    out = format_triggers([Trigger(description="x", source="phone")])

    assert "Scheduled jobs" not in out


@pytest.mark.parametrize("provider_id,index", [("qwen", True), ("anthropic", False)])
def test_turn_output_catalog_drives_index_and_suppresses_free_text_reasoning(
    provider_id, index
):
    tools = [
        {
            "name": "live_only",
            "description": "Inspect live state.",
            "input_schema": {"type": "object"},
        }
    ]

    bundle = build_prompt_bundle(provider_id, mcp_schemas=tools)

    assert "Return exactly one `TurnOutput`" in bundle.system_prompt
    assert "submit_turn" not in bundle.system_prompt
    assert ("## Tooling" in bundle.system_prompt) is index
    assert "<think>" not in bundle.system_prompt
    assert "exactly two native tool calls" not in bundle.system_prompt
    assert "note" not in [tool["name"] for tool in bundle.catalog.tools]
    assert (
        bundle.catalog.decode(
            {"summary": "inspect", "scratchpad": None, "action": {"live_only": {}}}
        ).summary
        == "inspect"
    )


def test_turn_output_assembly_requires_captured_schemas():
    with pytest.raises(TypeError, match="mcp_schemas"):
        build_prompt_bundle("deepseek")


def test_turn_output_catalog_rejects_private_macro_even_when_public_macro_exists(
    private_macro_pack,
):
    from physiclaw.agent.engine.validator import ValidationError

    _write_macro()
    bundle = build_prompt_bundle(
        "deepseek",
        pack_macros=private_macro_pack,
        mcp_schemas=[],
    )

    with pytest.raises(ValidationError):
        bundle.catalog.decode(
            {
                "summary": "private",
                "scratchpad": "must not save",
                "action": {"run_macro": {"name": "demo/leg"}},
            }
        )

    assert bundle.catalog.tools[-1]["name"] == "run_macro"
    assert "demo/leg" not in bundle.system_prompt


def test_turn_output_hidden_local_handler_shadows_remote_catalog_entry(
    private_macro_pack,
):
    remote = [{"name": "run_macro", "input_schema": {"type": "object"}}]

    bundle = build_prompt_bundle(
        "deepseek",
        pack_macros=private_macro_pack,
        mcp_schemas=remote,
    )

    assert "run_macro" in bundle.local_registry
    assert "run_macro" not in [t["name"] for t in bundle.catalog.tools]
    assert "**run_macro**" not in bundle.system_prompt
