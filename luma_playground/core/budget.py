# SPDX-License-Identifier: Apache-2.0
"""What an agent reads before it does anything, measured. Every token here is spent on every turn of every
agent, so it's kept small on purpose and checked by tests (tests/test_agent_smoke.py).

    python3 -m luma_playground.core.budget [project folder]

prints the table: always loaded (AGENTS.md, the tool list), read at the start of work (the indexes AGENTS.md
points at), and read only when the work needs it (the guide, notes).
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

from .paths import ProjectPaths

CHARS_PER_TOKEN = 4          # a rough rule for English and code


def tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def starting_context(root: Path | str) -> list[dict]:
    """[{name, when, tokens}] for one agent turn in this project. ``when`` is always, start or needed."""
    paths = ProjectPaths(root)
    from ..mcp.server import listed_tools
    tools = json.dumps([tool.describe() for tool in listed_tools()], separators=(",", ":"))
    rows = [{"name": "AGENTS.md (rules, where things are)", "when": "always", "tokens": tokens(_read(paths.agents_md))},
            {"name": f"Playground tools ({len(listed_tools())})", "when": "always", "tokens": tokens(tools)}]
    for name, path in (("Tasks index", paths.index("tasks")), ("Bugs index", paths.index("issues")),
                       ("Team channel digest", paths.channel_digest)):
        rows.append({"name": name, "when": "start", "tokens": tokens(_read(path))})
    knowledge = list(paths.knowledge_dir.glob("*.md")) if paths.knowledge_dir.is_dir() else []
    rows.append({"name": "Knowledge index", "when": "needed", "tokens": tokens(_read(paths.index("knowledge")))})
    rows.append({"name": f"Knowledge notes ({len([k for k in knowledge if k.name != 'index.md'])})", "when": "needed",
                 "tokens": sum(tokens(_read(k)) for k in knowledge if k.name != "index.md")})
    rows.append({"name": "Guide (formats)", "when": "needed", "tokens": tokens(_read(paths.guide))})
    return rows


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".")
    rows = starting_context(root)
    for when, words in (("always", "Every turn"), ("start", "Read when work starts"), ("needed", "Only when needed")):
        group = [r for r in rows if r["when"] == when]
        print(f"{words}: ~{sum(r['tokens'] for r in group):,} tokens")
        for row in group:
            print(f"  {row['tokens']:>7,}  {row['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
