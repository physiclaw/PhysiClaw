"""Session composition and lifecycle for the ordered producer pipeline.

The composition root installs configured plugins followed by the mandatory
LLM producer. The turn driver uses their common contract. Prompt assembly,
retries and conversation management belong to the LLM producer; the host
owns tools, resources, permissions and session outcomes.
"""

import asyncio
import logging
import time
from dataclasses import replace

from physiclaw.agent.engine import assemble, builtin_tool, curate, loop, prompt
from physiclaw.agent.engine import plugins as plugin_loader
from physiclaw.agent.engine.mcp_tool import get_mcp, list_tools_cached
from physiclaw.agent.engine.policy import default_policies
from physiclaw.agent.engine.provenance import PluginBinding
from physiclaw.agent.engine.runspec import EngineRun, Settings
from physiclaw.agent.engine.session import Session
from physiclaw.agent.llm import LLMProducer
from physiclaw.agent.runtime import contract
from physiclaw.agent.runtime.hook import Trigger
from physiclaw.agent.runtime.sentinel import STUCK
from physiclaw.agent.runtime.stopping import checkpoint
from physiclaw.agent.trace import RawLog, Trace, new_sid
from physiclaw.agent.trace.store import Images
from physiclaw.common import paths
from physiclaw.common.config import CONFIG, parse_model_ref
from physiclaw.common.logger import bind_log_context, reset_log_context
from physiclaw.common.provider_errors import ProviderConfigurationError
from physiclaw.common.text import clip
from physiclaw.contract.dto import Message
from physiclaw.contract.plugin import PluginServices
from physiclaw.macros.model import Macro
from physiclaw.provider import ChatProvider, make_chat_provider, make_decision_provider

log = logging.getLogger(__name__)


async def run(triggers: list[Trigger], *, model_ref: str) -> contract.SessionOutcome:
    """Run engine sessions for `triggers` under the session-outcome contract.

    One attempt = one fresh `Session` through `_run_session` (runtime crashes
    close STUCK; configuration errors and cancellation propagate).
    `contract.drive` owns STUCK retries up to `max_attempts` (STUCK here is
    mostly harness-detected — max turns, provider exhausted, crash — so a
    fresh session is worth it; DONE / FAIL / IDLE / WAIT are final), the
    one-shot first-run setup restart, and the jobless-WAIT follow-up. A
    session that exhausted its wall-clock budget reports `retryable=False`
    — a slow environment would burn every retry the same way.

    `model_ref` is a `provider/model` string (e.g. `"qwen/qwen3.6-plus"`).
    Parsed inside `_run_session`; the provider is instantiated via
    `provider.make_chat_provider(provider_id, model_id)`. Required — every
    caller goes through the launcher, which resolves `PHYSICLAW_MODEL`.
    """
    settings = Settings.from_config()

    async def attempt(trigs: list[Trigger], n: int) -> contract.SessionOutcome:
        session = Session()
        await _run_session(
            trigs,
            model_ref=model_ref,
            session=session,
            settings=settings,
        )
        return contract.SessionOutcome(
            status=session.sentinel_status,
            recap=session.sentinel_recap,
            created_job=session.sentinel_turn_created_job,
            restart_requested=session.restart_for_setup,
            retryable=not session.budget_exhausted,
        )

    # The final outcome flows back to the runtime loop, which backs off
    # its wake cadence on unproductive streaks (a dead phone must not
    # burn a full session per watchdog blip).
    return await contract.drive(
        attempt,
        triggers,
        max_attempts=settings.max_session_attempts,
        wait_default_minutes=settings.wait_default_minutes,
    )


async def _run_session(
    triggers: list[Trigger],
    *,
    model_ref: str,
    session: Session,
    settings: Settings | None = None,
) -> None:
    """One attempt with fresh logs, provider and policies; MCP is process-scoped.
    Writes outcome to `session.sentinel_*`; invalid
    model references, provider configuration errors, and cancellation propagate;
    runtime crashes close STUCK."""
    provider_id, model_id = parse_model_ref(model_ref)
    settings = settings or Settings.from_config()

    sid = new_sid()
    session.session_id = sid
    provider: ChatProvider | None = None
    plugins: tuple[PluginBinding, ...] = ()
    tr: Trace | None = None
    rlog: RawLog | None = None
    context = bind_log_context(session_id=sid)
    try:
        # Open inside the try so the finally block's close() runs even
        # if construction fails midway (disk full, perms, etc.).
        images = Images(sid)
        tr = Trace(sid, images=images)
        rlog = RawLog(sid, images=images)
        tr.write(
            {
                "event": "wake",
                "session": sid,
                "model_ref": model_ref,
                "triggers": [
                    {
                        "source": t.source,
                        "description": t.description,
                        **({"label": t.label} if t.label else {}),
                    }
                    for t in triggers
                ],
            }
        )
        scheduled = bool(triggers) and all(
            (t.source or "").startswith("cron:") for t in triggers
        )
        labels = "; ".join(str(t.label) for t in triggers if t.label)
        headline = "Scheduled task started" if scheduled else "Task started"
        if labels:
            headline += ": " + clip(" ".join(labels.split()), 160)
        elif triggers and all(t.source == "first-run" for t in triggers):
            headline = "Learning phone controls"
        log.info(
            "Session %s started | model=%s | triggers=%s\nDebug files: %s",
            sid,
            model_ref,
            [t.source or "?" for t in triggers],
            paths.sessions_dir() / sid,
            extra={
                "event": "session.started",
                "model_ref": model_ref,
                "console": True,
                "console_message": headline,
            },
        )
        # The provider client exists before plugin setup so the
        # shared services can offer it (plugin chat calls fall back
        # to the session model). Construction is cheap; nothing is sent
        # until the first chat call.
        provider = make_chat_provider(provider_id, model_id, usage_sink=tr)
        mcp = await get_mcp()
        checkpoint()
        mcp_tools = await list_tools_cached()
        checkpoint()
        # Turn-plugin wake-time setup, fail-open throughout: each
        # config-listed plugin loads blindly and contributes its gated
        # macros (dispatchable only by their owner). Nothing model-visible
        # changes. The loader assigns execution grants.
        plugins = plugin_loader.load_plugins()
        services = PluginServices(
            session_provider=provider,
            events=tr,
            wire=rlog,
            decision_provider_factory=make_decision_provider,
        )
        gated_macros: dict[str, Macro] = {}
        active_plugins: list[PluginBinding] = []
        for binding in plugins:
            checkpoint()
            try:
                contribution = await binding.plugin.session_setup(services)
                owned = (
                    dict(contribution.gated_macros) if contribution is not None else {}
                )
                if any(
                    not isinstance(name, str)
                    or name.count("/") != 1
                    or any(not part or part != part.strip() for part in name.split("/"))
                    for name in owned
                ):
                    raise ValueError(
                        "plugin-private macros require a trimmed app/name key"
                    )
                if any(not isinstance(spec, Macro) for spec in owned.values()):
                    raise TypeError("plugin-private macros must be Macro instances")
            except Exception:
                log.exception("turn plugin setup crashed — plugin skipped")
                continue
            taken = gated_macros.keys() & owned.keys()
            if taken:
                log.warning(
                    "gated macro name collision: %s — plugin %s skipped",
                    sorted(taken),
                    binding.id,
                )
                tr.write(
                    {
                        "event": "plugin_macro_collision",
                        "plugin_id": binding.id,
                        "macros": sorted(taken),
                    }
                )
                continue
            gated_macros.update(owned)
            binding = replace(binding, private_macros=frozenset(owned))
            active_plugins.append(binding)
            tr.write(
                {
                    "event": "plugin_bound",
                    "plugin_id": binding.id,
                    "name": binding.name,
                    "presentation": binding.presentation,
                    "private_macros": sorted(binding.private_macros),
                }
            )
        # Built-in skills are inlined full-text into SYSTEM; user skills are
        # indexed and loaded on demand via the Skill tool — so only user
        # skills go into the local registry. The first-run screen-layout skill
        # is dropped once the layout is learned (dead weight after setup).
        # `assemble.build_prompt_bundle` is the offline half, shared with the
        # CLI dump.
        bundle = assemble.build_prompt_bundle(
            provider_id,
            pack_macros=gated_macros or None,
        )
        # Full merged list rides `EngineRun.tool_schemas` to the provider
        # call; the inline `## Tooling` card pulls MCP names from AST so
        # it stays complete even offline. Each source has one consumer.
        tool_schemas = list(mcp_tools) + bundle.local_schemas
        # Dispatch also validates plugin-only tools that aren't advertised to
        # the model. Local handlers take precedence over MCP names here too.
        schema_by_name = {
            s["name"]: s
            for s in [*mcp_tools, *builtin_tool.schemas(bundle.local_registry)]
        }
        tr.write(
            {
                "event": "tools_loaded",
                "mcp": [s["name"] for s in mcp_tools],
                "local": sorted(bundle.local_registry.keys()),
            }
        )
        log.debug(
            "tools loaded: %d MCP + %d local + %d built-in + %d user skills "
            "+ %d pitfalls + %d macros",
            len(mcp_tools),
            len(bundle.local_registry),
            bundle.builtin_skill_count,
            bundle.user_skill_count,
            bundle.pitfall_count,
            bundle.macro_count,
        )

        messages: list[Message] = assemble.build_initial_messages(
            triggers,
            bundle.system_prompt,
        )

        prompt_hash = prompt.prefix_hash(bundle.system_prompt)
        rlog.write_session_start(
            provider=provider_id,
            model=provider.model,
            prompt_hash=prompt_hash,
            tools=tool_schemas,
        )
        engine_run = EngineRun(
            # Off-turn curation and transport lifetime remain host resources.
            plugins=tuple(active_plugins),
            mcp=mcp,
            tool_schemas=tool_schemas,
            schema_by_name=schema_by_name,
            local_registry=bundle.local_registry,
            tr=tr,
            rlog=rlog,
            settings=settings,
            policies=default_policies(layout_incomplete=bundle.layout_incomplete),
            deadline=(
                time.monotonic() + settings.max_session_seconds
                if settings.max_session_seconds > 0
                else None
            ),
            debug_intercept=plugin_loader.load_debug_intercept(),
        )
        llm = LLMProducer(
            provider,
            engine_run,
            session,
            messages,
            layout_incomplete=bundle.layout_incomplete,
        )
        await llm.session_setup(services)
        llm_binding = PluginBinding("physiclaw.llm", llm, "LLM", presentation="model")
        engine_run.plugins = (*engine_run.plugins, llm_binding)
        plugins = (*plugins, llm_binding)
        tr.write({"event": "prefix_pinned", "hash": prompt_hash})
        await loop.drive(engine_run, session)

        log.debug(
            "session done: session=%s status=%s recap=%r",
            sid,
            session.sentinel_status,
            session.sentinel_recap,
        )
        # The outcome is settled: record it BEFORE the optional curation
        # pass, so a Ctrl-C during that extra model call still leaves the
        # summary with the sentinel and recap the session actually reached.
        tr.write(
            {
                "event": "done",
                "sentinel": session.sentinel_status,
                "recap": session.sentinel_recap,
            }
        )
        # Post-session pitfalls curation: a separate LLM pass (still-open
        # provider) that consolidates the list + enforces the cap when the agent
        # added traps this session. Fail-open — never affects the outcome.
        if CONFIG.pitfalls.curate_enabled and session.added_pitfalls:
            checkpoint()
            await curate.curate(provider, tr=tr, rlog=rlog)
    except asyncio.CancelledError:
        loop.log_external_stop(session, tr)
        raise
    except ProviderConfigurationError:
        # The launcher reports setup errors once; retries cannot fix them.
        # Save the traceback before this session's capture closes, since the
        # launcher's later user-facing error is outside the session window.
        log.debug("Session provider configuration failed", exc_info=True)
        raise
    except Exception as e:
        # Crashes count as STUCK so the retry loop gives it another shot.
        log.exception("engine session crashed: session=%s", sid)
        if tr is not None:
            tr.write({"event": "crashed"})
        session.sentinel_status = STUCK
        session.sentinel_recap = f"session crashed: {e}"
    finally:
        try:
            # Retain all loaded plugins for cleanup, including partial setup.
            cancelled: asyncio.CancelledError | None = None
            # Plugins borrow the session provider: release them before their host.
            closers = [binding.plugin.aclose for binding in reversed(plugins)]
            if provider is not None:
                closers.append(provider.aclose)
            for close in closers:
                try:
                    await close()
                except asyncio.CancelledError as exc:
                    # Finish the other resources in this task, then propagate
                    # cancellation. Moving closes to a new task can break MCP
                    # transports whose cancellation scopes belong to this one.
                    cancelled = cancelled or exc
                except Exception:
                    log.warning("session resource close failed", exc_info=True)
            # Trace owns runtime.log and the MCP marker; keep it open until
            # the wire log closes so its final failures stay in this session.
            for sink in (rlog, tr):
                if sink is not None:
                    try:
                        sink.close()
                    except Exception:
                        log.warning("session log close failed", exc_info=True)
            if cancelled is not None:
                raise cancelled
        finally:
            reset_log_context(context)
