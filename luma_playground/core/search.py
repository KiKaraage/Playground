# SPDX-License-Identifier: Apache-2.0
"""Search everywhere in a project: tasks, bugs, knowledge and conversations (their titles and what was said).

Every word of the query must appear (case and accents aside); a hit in the title outranks one in the body,
and open work outranks finished work. Reads the project's own files and nothing else; small enough to run
on each keystroke off the main loop.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from .store import ProjectStore, StoreError

LIMIT = 40
SNIPPET = 90


@dataclass
class Hit:
    kind: str           # task, bug, knowledge, conversation
    key: str            # task id, bug number, knowledge slug, conversation id
    title: str
    snippet: str = ""
    score: float = 0.0
    done: bool = False


def fold(words: str) -> str:
    """Lower case without accents: "Café" finds "cafe"."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", words or "") if not unicodedata.combining(ch)).lower()


def terms(query: str) -> list[str]:
    return [t for t in re.split(r"\s+", fold(query).strip()) if t]


def snippet(body: str, wanted: list[str]) -> str:
    """The line of the body around the first word found, trimmed to a line's width."""
    plain = re.sub(r"(?m)^\s*(?:[-*+]\s+)?(?:\[[ xX]\]\s*)?", "", body or "")    # list and checklist marks
    flat = re.sub(r"\s+", " ", re.sub(r"[#*_`>]+", "", plain)).strip()
    low = fold(flat)
    at = min((low.find(t) for t in wanted if low.find(t) >= 0), default=-1)
    if at < 0:
        return ""
    start = max(0, at - SNIPPET // 3)
    if start:
        space = flat.find(" ", start)
        start = space + 1 if 0 <= space < at else start
    piece = flat[start:start + SNIPPET].strip()
    return ("…" if start else "") + piece + ("…" if start + SNIPPET < len(flat) else "")


def _score(title: str, body: str, wanted: list[str]) -> float:
    head, rest = fold(title), fold(body)
    score = 0.0
    for t in wanted:
        if t in head:
            score += 3 + (1 if re.search(rf"\b{re.escape(t)}", head) else 0)
        elif t in rest:
            score += 1
        else:
            return 0.0
    return score


def search(store: ProjectStore, query: str, *, limit: int = LIMIT, threads: bool = True) -> list[Hit]:
    wanted = terms(query)
    if not wanted:
        return []
    hits: list[Hit] = []

    def add(kind: str, key: str, title: str, body: str, done: bool) -> None:
        score = _score(title, body, wanted)
        if score:
            hits.append(Hit(kind, key, title, snippet(body, wanted) if body else "", score - (0.5 if done else 0),
                            done))

    for read, each in ((lambda: store.list_tasks(), lambda t: add("task", t.id, t.title, t.body, not t.is_open)),
                       (lambda: store.list_issues(), lambda i: add(
                           "bug", str(i.number), i.title,
                           "\n".join((i.what_happened, i.expected, i.steps)), i.status in ("fixed", "wont-fix"))),
                       (lambda: store.list_knowledge(), lambda n: add("knowledge", n.slug, n.title, n.body, False))):
        try:
            items = read()
        except (StoreError, OSError, ValueError):
            continue
        for item in items:
            each(item)
    try:
        goals = store.list_goals()
    except (StoreError, OSError, ValueError):
        goals = []
    for goal in goals:
        archived = bool((goal.extra or {}).get("archived"))
        said = ""
        if threads:
            try:
                said = "\n".join(e.text for e in store.read_thread(goal.id) if e.text and e.kind in
                                 ("message", "brief", "plan", "decision", "headsup", "handoff"))
            except (StoreError, OSError, ValueError):
                said = ""
        add("conversation", goal.id, goal.title or "Untitled", "\n".join(p for p in (goal.notes, said) if p),
            archived)
    hits.sort(key=lambda h: (-h.score, h.kind, h.title.lower()))
    return hits[:limit]
