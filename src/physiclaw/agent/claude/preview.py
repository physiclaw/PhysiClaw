"""``physiclaw claude-preview`` — dump spawn artifacts without launching claude.

Useful for eyeballing changes to CLAUDE.md, the skill catalog, tool
surface, or permissions before a live run. Builds the exact argv
`spawn_claude` would use, materializes the plugin dir, and prints
each piece.

Lives in agent/claude/ alongside the code it inspects. `cli/__init__.py`
imports this command conditionally — removing agent/claude/ removes the
command without leaving dead code in cli/.
"""
from pathlib import Path
from typing import Annotated

import typer

from physiclaw.agent.claude import spawn as _spawn
from physiclaw.agent.claude.plugin import prepare_plugin_dir
from physiclaw.agent.engine import skill
from physiclaw.agent.runtime.hook import Trigger
from physiclaw.cli._format import ok, section, warn


def _arg_after(argv: list[str], flag: str) -> str:
    """Return the value following `flag` in argv. Raises if absent —
    silent divergence between the previewer and the real spawn path is
    worse than a crash."""
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError) as e:
        raise RuntimeError(f"{flag} not found in argv") from e


def _print_tree(root: Path) -> None:
    """Depth-first listing of `root`. Symlinks show their target."""
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        indent = "  " * len(rel.parts)
        name = rel.name
        if p.is_symlink():
            typer.echo(f"{indent}{name} → {p.readlink()}")
        elif p.is_dir():
            typer.echo(f"{indent}{name}/")
        else:
            size = p.stat().st_size
            typer.echo(f"{indent}{name}  ({size}b)")


def claude_preview(
    trigger: Annotated[
        str,
        typer.Option(
            "--trigger", "-t",
            help="Synthetic trigger description to preview against.",
        ),
    ] = "manual preview wake",
    full: Annotated[
        bool,
        typer.Option("--full", help="Print the full system prompt and full argv."),
    ] = False,
) -> None:
    """Assemble the `claude -p` command for a synthetic trigger without spawning.

    Prints the rendered system prompt, plugin dir contents, MCP config,
    and final argv — everything a live wake would send to Claude Code,
    minus the subprocess. The plugin dir is kept under TMPDIR for manual
    inspection; its path is echoed at the end.
    """
    from physiclaw.config import model_ref_with_source, parse_model_ref
    try:
        ref, _ = model_ref_with_source()
        _, model_id = parse_model_ref(ref)
    except (RuntimeError, ValueError) as e:
        typer.echo(warn(f"agent.model not set or invalid: {e}"))
        raise typer.Exit(2) from None

    trig = Trigger(source="manual", description=trigger)
    sid = "preview"

    # Single source of truth — assemble the same way spawn_claude does.
    # Diverging would defeat the whole point of a preview.
    mcp_tools = _spawn._mcp_tools()
    skills = skill.discover()
    plugin_dir = prepare_plugin_dir(sid, skills=skills)
    try:
        system_prompt = _spawn._render_system_prompt(mcp_tools, skills)
        cmd = _spawn._build_cmd(
            [trig],
            plugin_dir=plugin_dir,
            system_prompt=system_prompt,
            mcp_tools=mcp_tools,
            model_id=model_id,
        )
    except FileNotFoundError as e:
        typer.echo(warn(str(e)))
        raise typer.Exit(2) from None

    system_prompt = _arg_after(cmd, "--append-system-prompt")
    plugin_dir = Path(_arg_after(cmd, "--plugin-dir"))
    mcp_config = _arg_after(cmd, "--mcp-config")
    trigger_prompt = _arg_after(cmd, "-p")
    allowed = _arg_after(cmd, "--allowedTools").split(",")
    disallowed = _arg_after(cmd, "--disallowedTools").split(",")

    # --- System prompt --------------------------------------------------------
    typer.echo(section("System prompt  (--append-system-prompt)"))
    typer.echo(
        f"  size: {len(system_prompt):,} chars, "
        f"{len(system_prompt.splitlines())} lines"
    )
    typer.echo()
    if full:
        typer.echo(system_prompt)
    else:
        head = system_prompt.splitlines()[:30]
        for line in head:
            typer.echo(f"  {line}")
        remainder = len(system_prompt.splitlines()) - len(head)
        if remainder > 0:
            typer.echo(f"  … ({remainder} more lines — pass --full to see all)")

    # --- Plugin dir -----------------------------------------------------------
    typer.echo()
    typer.echo(section("Plugin dir  (--plugin-dir)"))
    typer.echo(f"  {plugin_dir}")
    typer.echo()
    _print_tree(plugin_dir)

    # --- MCP --------------------------------------------------------------
    typer.echo()
    typer.echo(section("MCP  (--mcp-config)"))
    typer.echo(f"  {mcp_config}")
    typer.echo()
    typer.echo("  tools exposed (with Claude Code prefix):")
    for t in mcp_tools:
        typer.echo(f"    {t['name']}")

    # --- Permissions ----------------------------------------------------------
    typer.echo()
    typer.echo(section("Permissions"))
    typer.echo(f"  --allowedTools ({len(allowed)}):")
    for t in allowed:
        typer.echo(f"    {t}")
    typer.echo(f"  --disallowedTools ({len(disallowed)}):")
    for t in disallowed:
        typer.echo(f"    {t}")

    # --- Trigger prompt -------------------------------------------------------
    typer.echo()
    typer.echo(section("Trigger prompt  (-p)"))
    for line in trigger_prompt.splitlines():
        typer.echo(f"  {line}")

    # --- Argv -----------------------------------------------------------------
    typer.echo()
    typer.echo(section("Final argv"))
    for arg in cmd:
        if full or len(arg) < 80:
            typer.echo(f"  {arg}")
        else:
            typer.echo(f"  {arg[:77]}…  ({len(arg)} chars total)")

    typer.echo()
    typer.echo(ok(f"plugin dir kept for inspection: {plugin_dir}"))
    typer.echo(
        "  (it's under TMPDIR; the OS reclaims it on reboot, "
        "or you can `rm -rf` it now)"
    )
