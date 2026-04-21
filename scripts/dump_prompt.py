"""Dump the engine SYSTEM prompt without running a session.

Useful for reviewing the rendered prompt, A/B-ing edits to context files
or prompt.py, and counting tokens before they hit a provider.

Runs fully offline: MCP tools come from AST parse of
`physiclaw/server/tools.py`, local tools from `builtin_tool.build_registry`.
No MCP server needed.

Usage:
    uv run python scripts/dump_prompt.py
    uv run python scripts/dump_prompt.py --provider qwen
    uv run python scripts/dump_prompt.py --save tests/output/prompt.md
    uv run python scripts/dump_prompt.py --no-boundary     # strip the cache marker
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.engine import builtin_tool, jobs, memory, prompt, skill  # noqa: E402


def _build(args: argparse.Namespace) -> str:
    skill_registry = skill.discover()
    local_registry = builtin_tool.build_registry(skill_registry)
    return prompt.dump(
        local_tool_schemas=builtin_tool.schemas(local_registry),
        skills_ctx=skill.render_section(skill_registry),
        memory_ctx=memory.load_persistent(),
        cron_ctx=jobs.format_fired([]),  # no triggers — wake context is empty
        provider_name=args.provider,
        keep_boundary=not args.no_boundary,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump engine SYSTEM prompt")
    parser.add_argument(
        "--provider",
        default="qwen",
        help="provider name (gates Qwen-only sections like Reasoning Format)",
    )
    parser.add_argument(
        "--no-boundary",
        action="store_true",
        help="strip the cache-boundary marker from the output",
    )
    parser.add_argument(
        "--save",
        type=Path,
        help="write to PATH instead of stdout",
    )
    args = parser.parse_args()

    text = _build(args)

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(text)
        print(f"wrote {len(text):,} chars to {args.save}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
