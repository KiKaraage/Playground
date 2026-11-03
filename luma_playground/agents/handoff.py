# SPDX-License-Identifier: Apache-2.0
"""The handoff brief: what the next agent reads when the person switches.

Switching agent or account mid-goal doesn't transplant a vendor's
transcript (those are private formats that change). Instead Playground
writes a short, plain brief from what the project folder already knows:
the goal and its plan, decisions made and still open, what happened
recently in the conversation, which files were touched, and what git says
has changed. The person can edit it before the next agent continues.

``build_brief`` reads only the project folder (and runs ``git`` there with a
timeout). No GTK, no network.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
from typing import Any

from ..core.model import GoalStatus, ThreadEntry
from ..core.store import NotFound, ProjectStore

MAX_BRIEF_CHARS = 9000
RECENT_ENTRIES = 16
SNIPPET = 320


@dataclass
class Handoff:
    from_connection: str
    to_connection: str
    brief: str
    from_label: str = ""
    to_label: str = ""

    def card_data(self) -> dict:
        return {"from_connection": self.from_connection, "to_connection": self.to_connection,
                "from_label": self.from_label, "to_label": self.to_label, "brief": self.brief}


def _clip(text: str, limit: int = SNIPPET) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _label(connection: Any) -> str:
    if connection is None:
        return "another agent"
    if isinstance(connection, str):
        return connection
    return getattr(connection, "label", "") or getattr(connection, "id", "") or "another agent"


def git_summary(root: str | os.PathLike, timeout: float = 5.0, limit: int = 40) -> str:
    """``git status`` and ``git diff --stat`` in short form, or "" when it isn't a repo."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    root = os.fspath(root)

    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                                    timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout if result.returncode == 0 else None

    inside = run("rev-parse", "--is-inside-work-tree")
    if not inside or inside.strip() != "true":
        return ""
    status = run("status", "--porcelain=v1", "--untracked-files=normal") or ""
    stat = run("diff", "--stat", "HEAD") or run("diff", "--stat") or ""
    lines = [line for line in status.splitlines() if line.strip() and ".playground/" not in line][:limit]
    out = []
    if lines:
        out.append("Changed files (git status):")
        out.extend("  " + line for line in lines)
    stat_lines = [line for line in stat.splitlines() if line.strip()]
    if stat_lines:
        out.append("Size of the changes: " + stat_lines[-1].strip())
    return "\n".join(out)


def _story_lines(goal) -> list[str]:
    lines = []
    for story in goal.stories:
        mark = "x" if story.done else " "
        lines.append(f"- [{mark}] {story.title}")
        for check in story.checks:
            lines.append(f"  - check ({check.status}): {check.text}")
    return lines


def _latest_plan(entries: list[ThreadEntry]) -> list[str]:
    for entry in reversed(entries):
        if entry.kind == "plan":
            stories = entry.data.get("stories") or []
            statuses = entry.data.get("entries") or []
            out = []
            for i, story in enumerate(stories):
                title = story.get("title") if isinstance(story, dict) else str(story)
                status = statuses[i].get("status") if i < len(statuses) and isinstance(statuses[i], dict) else ""
                mark = "x" if status == "completed" else " "
                out.append(f"- [{mark}] {title}")
            return out
    return []


def _recent(entries: list[ThreadEntry]) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    files: list[str] = []
    for entry in entries:
        if entry.kind == "step":
            for path in entry.data.get("files") or []:
                if path not in files:
                    files.append(path)
    seen_steps: dict[str, int] = {}
    for entry in entries[-RECENT_ENTRIES * 2:]:
        who = {"person": "Person", "system": "Playground"}.get(entry.role, "Agent")
        if entry.kind == "message" and entry.text.strip():
            lines.append(f"- {who}: {_clip(entry.text)}")
        elif entry.kind == "step":
            text = f"- Step: {entry.data.get('title', '')} ({entry.data.get('state', '')})"
            if entry.id in seen_steps:
                lines[seen_steps[entry.id]] = text  # the finished version replaces the running one
            else:
                seen_steps[entry.id] = len(lines)
                lines.append(text)
        elif entry.kind == "decision":
            answered = entry.data.get("answered") or {}
            choice = answered.get("label") or answered.get("text") or "not answered yet"
            lines.append(f"- Question: {_clip(entry.data.get('question', ''), 160)} Answer: {choice}")
        elif entry.kind == "handoff":
            lines.append(f"- Switched from {entry.data.get('from_label') or entry.data.get('from_connection')}"
                         f" to {entry.data.get('to_label') or entry.data.get('to_connection')}")
        elif entry.kind == "proof":
            checks = entry.data.get("checks") or []
            passed = sum(1 for c in checks if isinstance(c, dict) and c.get("status") == "pass")
            lines.append(f"- Proof: {passed} of {len(checks)} checks passing")
    return lines[-RECENT_ENTRIES:], files


def build_brief(store: ProjectStore, goal_id: str, *, from_connection: Any = None, to_connection: Any = None,
                git: bool = True) -> Handoff:
    """Write the handoff brief for ``goal_id``. Always returns something usable."""
    from_label, to_label = _label(from_connection), _label(to_connection)
    parts: list[str] = [
        f"You're picking up a goal in Playground. It was being worked on with {from_label}; "
        f"you are {to_label} and you continue from here. The project folder is the record: "
        "read AGENTS.md first, and keep Playground updated with its tools as you go.",
    ]
    try:
        goal = store.read_goal(goal_id)
    except NotFound:
        goal = None
    if goal is not None:
        status = goal.status.label if isinstance(goal.status, GoalStatus) else str(goal.status)
        parts.append(f"Goal: {goal.title} (id {goal.id}, {status.lower()})")
        if goal.outcome.strip():
            parts.append("Outcome wanted: " + _clip(goal.outcome, 800))
        stories = _story_lines(goal)
        if stories:
            parts.append("Stories and checks:\n" + "\n".join(stories))
    try:
        entries = store.read_thread(goal_id)
    except (NotFound, OSError, ValueError):
        entries = []
    if goal is None or not goal.stories:
        plan = _latest_plan(entries)
        if plan:
            parts.append("The plan so far:\n" + "\n".join(plan))
    decisions = [d for d in store.read_decisions() if not goal_id or d.goal in ("", goal_id)][-8:]
    if decisions:
        parts.append("Decisions already made (don't ask again):\n" + "\n".join(
            f"- {_clip(d.title, 160)}: {_clip(d.chosen, 160)}" + (f" ({_clip(d.why, 160)})" if d.why else "")
            for d in decisions))
    try:
        waiting = [p for p in store.list_pending(kind="decision") if getattr(p, "goal", "") in ("", goal_id)]
    except OSError:
        waiting = []
    if waiting:
        parts.append("Still waiting on the person:\n" + "\n".join(f"- {_clip(p.question, 200)}" for p in waiting))
    recent, files = _recent(entries)
    if recent:
        parts.append("What happened recently (oldest first):\n" + "\n".join(recent))
    if files:
        shown = files[-30:]
        parts.append("Files touched in this goal: " + ", ".join(shown))
    if git:
        summary = git_summary(store.root)
        if summary:
            parts.append(summary)
    parts.append("Next: look over the files above, then carry on with the next open story. "
                 "If something here is unclear, ask the person before guessing.")
    brief = "\n\n".join(parts)
    if len(brief) > MAX_BRIEF_CHARS:
        brief = brief[: MAX_BRIEF_CHARS - 40].rstrip() + "\n\n(Brief shortened to fit.)"
    return Handoff(
        from_connection=getattr(from_connection, "id", from_connection) or "",
        to_connection=getattr(to_connection, "id", to_connection) or "",
        brief=brief, from_label=from_label, to_label=to_label)
