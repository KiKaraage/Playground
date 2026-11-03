# SPDX-License-Identifier: Apache-2.0
"""What a Playground project is made of, as plain dataclasses.

These carry no I/O. ``store`` reads and writes them; the MCP server and the UI
pass their ``to_dict()`` forms around. Every ``from_dict`` is forgiving: a
missing field takes its default, and fields this version doesn't know are kept
in ``extra`` so that writing a record back never drops what an agent or a newer
Playground put there.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
import re
from typing import Any

CARD_KINDS = ("message", "brief", "recipe", "plan", "step", "decision", "proof",
              "headsup", "review", "handoff", "cost")
ROLES = ("agent", "person", "system")
CHECK_STATES = ("pass", "fail", "pending")
STEP_STATES = ("running", "done", "failed")
PLACE_ROLES = ("development", "production", "preview")
PLACE_KINDS = ("local", "ssh")
MEMBER_ROLES = ("owner", "builder", "reviewer", "viewer")
GOAL_KINDS = ("goal", "chat")
SIZES = ("small", "medium", "large")

THIS_COMPUTER = "this-computer"
GENERAL_CHAT_ID = "talk-to-ari"


# ── Time ──

def now_iso() -> str:
    """Local time with its offset, to the second: 2026-10-09T09:40:00-07:00."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_ts(value: object) -> datetime | None:
    """An aware datetime from an ISO string, a date or epoch seconds; None if unreadable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment


def ts_sort_key(value: object) -> float:
    moment = parse_ts(value)
    return moment.timestamp() if moment else 0.0


# ── Goals ──

class GoalStatus(str, Enum):
    WORKING = "working"
    NEEDS_YOU = "needs-you"
    READY = "ready"
    LIVE = "live"
    PAUSED = "paused"
    DONE = "done"

    @property
    def label(self) -> str:
        return _STATUS_LABELS[self]

    @classmethod
    def parse(cls, value: object, default: "GoalStatus | None" = None) -> "GoalStatus":
        """Tolerant: "Needs you", "needs_you" and "ready-to-review" all parse."""
        if isinstance(value, GoalStatus):
            return value
        text = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
        text = _STATUS_ALIASES.get(text, text)
        try:
            return cls(text)
        except ValueError:
            if default is not None:
                return default
            raise ValueError(f"unknown goal status: {value!r}") from None


_STATUS_LABELS = {
    GoalStatus.WORKING: "Working",
    GoalStatus.NEEDS_YOU: "Needs you",
    GoalStatus.READY: "Ready to review",
    GoalStatus.LIVE: "Live",
    GoalStatus.PAUSED: "Paused",
    GoalStatus.DONE: "Done",
}
_STATUS_ALIASES = {"ready-to-review": "ready", "review": "ready", "needs": "needs-you",
                   "blocked": "needs-you", "released": "live", "complete": "done", "completed": "done"}


@dataclass
class Check:
    """Behaviour a person can see. ``evidence`` is a project-relative shot or note."""
    text: str
    status: str = "pending"
    evidence: str | None = None

    def to_dict(self) -> dict:
        data = {"text": self.text, "status": self.status}
        if self.evidence:
            data["evidence"] = self.evidence
        return data

    @classmethod
    def from_dict(cls, data: dict | str) -> "Check":
        if isinstance(data, str):
            return cls(text=data)
        status = str(data.get("status") or "pending").lower()
        return cls(text=str(data.get("text", "")), status=status if status in CHECK_STATES else "pending",
                   evidence=data.get("evidence") or None)


@dataclass
class Story:
    """One thing a person can do when the goal is finished, with its checks."""
    title: str
    done: bool = False
    checks: list[Check] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"title": self.title, "done": self.done, "checks": [c.to_dict() for c in self.checks]}

    @classmethod
    def from_dict(cls, data: dict | str) -> "Story":
        if isinstance(data, str):
            return cls(title=data)
        return cls(title=str(data.get("title", "")), done=bool(data.get("done", False)),
                   checks=[Check.from_dict(c) for c in data.get("checks") or []])


@dataclass
class Goal:
    """A thread of work with one outcome. Stored as .playground/goals/<id>.md."""
    id: str
    title: str
    status: GoalStatus = GoalStatus.WORKING
    kind: str = "goal"
    owner: str = ""
    connection: str = ""
    created: str = ""
    updated: str = ""
    size: str = ""
    cost_estimate: str = ""
    status_note: str = ""
    outcome: str = ""
    stories: list[Story] = field(default_factory=list)
    notes: str = ""
    # Body sections other than Outcome, Stories and Notes, kept in order.
    sections: list[tuple[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_chat(self) -> bool:
        return self.kind == "chat"

    @property
    def checks(self) -> list[Check]:
        return [check for story in self.stories for check in story.checks]

    @property
    def checks_passed(self) -> int:
        return sum(1 for check in self.checks if check.status == "pass")

    def to_dict(self) -> dict:
        data = {
            "id": self.id, "title": self.title, "status": self.status.value, "status_label": self.status.label,
            "kind": self.kind, "owner": self.owner, "connection": self.connection, "created": self.created,
            "updated": self.updated, "size": self.size, "cost_estimate": self.cost_estimate,
            "status_note": self.status_note, "outcome": self.outcome,
            "stories": [s.to_dict() for s in self.stories], "notes": self.notes,
        }
        if self.sections:
            data["sections"] = [{"heading": h, "text": t} for h, t in self.sections]
        if self.extra:
            data["extra"] = dict(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Goal":
        sections = [(str(s.get("heading", "")), str(s.get("text", ""))) for s in data.get("sections") or []]
        kind = str(data.get("kind") or "goal")
        return cls(
            id=str(data["id"]), title=str(data.get("title", "")),
            status=GoalStatus.parse(data.get("status"), GoalStatus.WORKING),
            kind=kind if kind in GOAL_KINDS else "goal",
            owner=str(data.get("owner") or ""), connection=str(data.get("connection") or ""),
            created=str(data.get("created") or ""), updated=str(data.get("updated") or ""),
            size=str(data.get("size") or ""), cost_estimate=str(data.get("cost_estimate") or ""),
            status_note=str(data.get("status_note") or ""), outcome=str(data.get("outcome") or ""),
            stories=[Story.from_dict(s) for s in data.get("stories") or []],
            notes=str(data.get("notes") or ""), sections=sections, extra=dict(data.get("extra") or {}),
        )


# ── Decisions ──

@dataclass
class Option:
    id: str
    label: str
    consequence: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "consequence": self.consequence}

    @classmethod
    def from_dict(cls, data: dict | str, index: int = 0) -> "Option":
        if isinstance(data, str):
            return cls(id=f"option-{index + 1}", label=data)
        return cls(id=str(data.get("id") or f"option-{index + 1}"), label=str(data.get("label", "")),
                   consequence=str(data.get("consequence") or ""))


@dataclass
class Decision:
    """A decision that was made, as kept in decisions.md."""
    title: str
    chosen: str = ""
    why: str = ""
    who: str = ""
    date: str = ""
    goal: str = ""
    id: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = {"title": self.title, "chosen": self.chosen, "why": self.why, "who": self.who,
                "date": self.date, "goal": self.goal, "id": self.id}
        if self.extra:
            data["extra"] = dict(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Decision":
        return cls(title=str(data.get("title", "")), chosen=str(data.get("chosen") or ""),
                   why=str(data.get("why") or ""), who=str(data.get("who") or ""),
                   date=str(data.get("date") or ""), goal=str(data.get("goal") or ""),
                   id=str(data.get("id") or ""), extra=dict(data.get("extra") or {}))


@dataclass
class PendingDecision:
    """A question an agent is waiting on: .playground/pending/<id>.json."""
    id: str
    question: str
    options: list[Option]
    goal: str = ""
    recommended: str = ""
    default: str = ""
    context: str = ""
    asked_by: str = ""
    asked_at: str = ""
    expires_at: str = ""
    kind: str = "decision"
    extra: dict[str, Any] = field(default_factory=dict)

    def option(self, option_id: str) -> Option | None:
        return next((o for o in self.options if o.id == option_id), None)

    def to_dict(self) -> dict:
        data = {"id": self.id, "kind": "decision", "goal": self.goal, "question": self.question,
                "options": [o.to_dict() for o in self.options], "recommended": self.recommended,
                "default": self.default, "context": self.context, "asked_by": self.asked_by,
                "asked_at": self.asked_at, "expires_at": self.expires_at}
        data.update({k: v for k, v in self.extra.items() if k not in data})
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "PendingDecision":
        known = {"id", "kind", "goal", "question", "options", "recommended", "default", "context",
                 "asked_by", "asked_at", "expires_at"}
        return cls(id=str(data["id"]), question=str(data.get("question", "")),
                   options=[Option.from_dict(o, i) for i, o in enumerate(data.get("options") or [])],
                   goal=str(data.get("goal") or ""), recommended=str(data.get("recommended") or ""),
                   default=str(data.get("default") or ""), context=str(data.get("context") or ""),
                   asked_by=str(data.get("asked_by") or ""), asked_at=str(data.get("asked_at") or ""),
                   expires_at=str(data.get("expires_at") or ""),
                   extra={k: v for k, v in data.items() if k not in known})


@dataclass
class PendingRequest:
    """Something an agent asked the app to do: kind "screenshot" or "checks"."""
    id: str
    kind: str
    goal: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    asked_by: str = ""
    asked_at: str = ""
    expires_at: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "goal": self.goal, "params": dict(self.params),
                "asked_by": self.asked_by, "asked_at": self.asked_at, "expires_at": self.expires_at}

    @classmethod
    def from_dict(cls, data: dict) -> "PendingRequest":
        return cls(id=str(data["id"]), kind=str(data.get("kind") or ""), goal=str(data.get("goal") or ""),
                   params=dict(data.get("params") or {}), asked_by=str(data.get("asked_by") or ""),
                   asked_at=str(data.get("asked_at") or ""), expires_at=str(data.get("expires_at") or ""))


@dataclass
class Answer:
    """The person's answer (or the app's reply): .playground/answers/<id>.json.

    For a decision, ``option`` is the chosen option id (or "" with ``text`` for
    a free answer). For a screenshot, ``data["path"]`` is the project-relative
    image. For checks, ``data["checks"]`` holds the results.
    """
    id: str
    option: str = ""
    label: str = ""
    text: str = ""
    answered_by: str = ""
    answered_at: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = {"id": self.id, "option": self.option, "label": self.label, "text": self.text,
                "answered_by": self.answered_by, "answered_at": self.answered_at}
        if self.data:
            data["data"] = dict(self.data)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Answer":
        # "by" and "ts" are accepted too: hand-written answers often use them.
        return cls(id=str(data["id"]), option=str(data.get("option") or ""), label=str(data.get("label") or ""),
                   text=str(data.get("text") or ""), answered_by=str(data.get("answered_by") or data.get("by") or ""),
                   answered_at=str(data.get("answered_at") or data.get("ts") or ""), data=dict(data.get("data") or {}))


# ── History and conversation ──

@dataclass
class ActivityEntry:
    """One line of activity.jsonl."""
    summary: str
    kind: str = "note"
    actor: str = ""
    goal: str = ""
    ts: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        record: dict[str, Any] = {"ts": self.ts or now_iso(), "actor": self.actor, "kind": self.kind}
        if self.goal:
            record["goal"] = self.goal
        record["summary"] = self.summary
        if self.data:
            record["data"] = self.data
        return record

    @classmethod
    def from_dict(cls, data: dict) -> "ActivityEntry":
        return cls(summary=str(data.get("summary", "")), kind=str(data.get("kind") or "note"),
                   actor=str(data.get("actor") or ""), goal=str(data.get("goal") or ""),
                   ts=str(data.get("ts") or ""), data=dict(data.get("data") or {}))


@dataclass
class ThreadEntry:
    """One card or message in a goal's conversation: a line of threads/<goal>.jsonl.

    ``by`` names who wrote it: a member id for a person, a connection id for an
    agent ("ari", "priya-claude"). ``kind`` is one of CARD_KINDS.
    """
    kind: str
    role: str = "agent"
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    by: str = ""
    id: str = ""
    ts: str = ""

    def to_dict(self) -> dict:
        record: dict[str, Any] = {"ts": self.ts or now_iso(), "id": self.id, "role": self.role, "kind": self.kind}
        if self.by:
            record["by"] = self.by
        if self.text:
            record["text"] = self.text
        if self.data:
            record["data"] = self.data
        return record

    @classmethod
    def from_dict(cls, data: dict) -> "ThreadEntry":
        role = str(data.get("role") or "agent")
        return cls(kind=str(data.get("kind") or "message"), role=role if role in ROLES else "agent",
                   text=str(data.get("text") or ""), data=dict(data.get("data") or {}),
                   by=str(data.get("by") or ""), id=str(data.get("id") or ""), ts=str(data.get("ts") or ""))


# ── The book ──

@dataclass
class KnowledgeNote:
    slug: str
    title: str
    body: str = ""
    added_by: str = ""
    updated: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"slug": self.slug, "title": self.title, "body": self.body, "added_by": self.added_by,
                "updated": self.updated}

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeNote":
        return cls(slug=str(data["slug"]), title=str(data.get("title", "")), body=str(data.get("body") or ""),
                   added_by=str(data.get("added_by") or ""), updated=str(data.get("updated") or ""))


# Knowledge comes in kinds. Rules are the only kind every agent always has in front of it
# (Playground copies kept rules into AGENTS.md); the rest is read when it's relevant.
KNOWLEDGE_KINDS = ("purpose", "decision", "rule", "part", "lesson", "mistake", "note")
KNOWLEDGE_KIND_WORDS = {"purpose": "Purpose", "decision": "Decisions", "rule": "Rules", "part": "Parts",
                        "lesson": "Lessons", "mistake": "Mistakes", "note": "Notes"}


def knowledge_paths(note: "KnowledgeNote") -> list[str]:
    """Where a part lives in the code (``paths: [src/auth/]``)."""
    value = note.extra.get("paths") or []
    if isinstance(value, str):
        value = [p.strip() for p in value.strip("[]").split(",")]
    return [str(p).strip().strip("'\"") for p in value if str(p).strip()]


def knowledge_kind(note: "KnowledgeNote") -> str:
    kind = str(note.extra.get("kind") or "note").lower()
    return kind if kind in KNOWLEDGE_KINDS else "note"


def knowledge_kept(note: "KnowledgeNote") -> bool:
    """A rule an agent wrote waits for a person to keep it; everything else counts at once."""
    return str(note.extra.get("status") or "kept").lower() != "suggested"


def knowledge_current(note: "KnowledgeNote") -> bool:
    """Kept, and not replaced by a newer note (``status: superseded``, ``superseded_by: <slug>``) or retired.
    Old notes are never deleted, so the history stays; only current ones are listed and followed."""
    return knowledge_kept(note) and str(note.extra.get("status") or "kept").lower() not in ("superseded", "retired")


TASK_STATUSES = ("todo", "doing", "done", "dropped")
TASK_STATUS_WORDS = {"todo": "To do", "doing": "Underway", "done": "Done", "dropped": "Dropped"}
PRIORITIES = ("high", "normal", "low")


@dataclass
class Task:
    """A piece of work: .playground/tasks/<id>.md. One file each, so people and agents never edit the same file."""
    id: str
    title: str
    status: str = "todo"
    priority: str = "normal"
    assignee: str = ""
    created_by: str = ""
    created_at: str = ""
    updated_by: str = ""
    updated_at: str = ""
    claimed_by: str = ""
    claimed_until: str = ""
    conversation: str = ""          # the conversation working on it
    labels: list[str] = field(default_factory=list)
    body: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status in ("todo", "doing")

    def claim_holder(self, now: str | None = None) -> str:
        """Who holds the task right now ("" when nobody, or the claim ran out)."""
        if not self.claimed_by or not self.claimed_until:
            return ""
        return self.claimed_by if ts_sort_key(self.claimed_until) > ts_sort_key(now or now_iso()) else ""

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "status": self.status, "priority": self.priority,
                "assignee": self.assignee, "created_by": self.created_by, "created_at": self.created_at,
                "updated_by": self.updated_by, "updated_at": self.updated_at, "claimed_by": self.claimed_by,
                "claimed_until": self.claimed_until, "conversation": self.conversation, "labels": list(self.labels),
                "body": self.body}


BRIEF_SUMMARY = "What it is"
BRIEF_GOOD = "Good looks like"
BRIEF_NOT_NOW = "Not for now"


@dataclass
class Brief:
    """The one-page brief: brief.md. Front matter holds title, audience and
    platforms; the body is sections ("## What it is", "## Good looks like"…)."""
    title: str = ""
    audience: str = ""
    platforms: list[str] = field(default_factory=list)
    updated: str = ""
    preamble: str = ""
    sections: list[tuple[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def section(self, heading: str) -> str:
        return next((text for h, text in self.sections if h.lower() == heading.lower()), "")

    def set_section(self, heading: str, text: str) -> None:
        for index, (h, _) in enumerate(self.sections):
            if h.lower() == heading.lower():
                self.sections[index] = (h, text.strip())
                return
        self.sections.append((heading, text.strip()))

    @property
    def summary(self) -> str:
        return self.section(BRIEF_SUMMARY)

    @property
    def good_looks_like(self) -> list[str]:
        return _bullets(self.section(BRIEF_GOOD))

    @property
    def not_now(self) -> str:
        return self.section(BRIEF_NOT_NOW)

    def to_dict(self) -> dict:
        return {"title": self.title, "audience": self.audience, "platforms": list(self.platforms),
                "updated": self.updated, "summary": self.summary, "good_looks_like": self.good_looks_like,
                "not_now": self.not_now, "sections": [{"heading": h, "text": t} for h, t in self.sections]}


def _bullets(text: str) -> list[str]:
    items = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            items.append(stripped[2:].strip())
        elif stripped and items and line.startswith((" ", "\t")):
            items[-1] += " " + stripped
        elif stripped:
            items.append(stripped)
    return items


# ── Project ──

@dataclass
class Place:
    """Where the project lives or runs. Never holds a secret."""
    id: str
    name: str
    role: str = "development"
    kind: str = "local"
    host: str = ""
    user: str = ""
    port: int = 0
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = {"id": self.id, "name": self.name, "role": self.role, "kind": self.kind}
        if self.host:
            data["host"] = self.host
        if self.user:
            data["user"] = self.user
        if self.port:
            data["port"] = self.port
        if self.description:
            data["description"] = self.description
        data.update({k: v for k, v in self.extra.items() if k not in data})
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Place":
        known = {"id", "name", "role", "kind", "host", "user", "port", "description"}
        try:
            port = int(data.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        return cls(id=str(data.get("id") or ""), name=str(data.get("name") or ""),
                   role=str(data.get("role") or "development"), kind=str(data.get("kind") or "local"),
                   host=str(data.get("host") or ""), user=str(data.get("user") or ""), port=port,
                   description=str(data.get("description") or ""),
                   extra={k: v for k, v in data.items() if k not in known})


@dataclass
class Member:
    id: str
    name: str
    role: str = "builder"
    connect_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name.split() else self.id

    @property
    def initials(self) -> str:
        words = self.name.split()
        if len(words) >= 2:
            return (words[0][0] + words[-1][0]).upper()
        return (self.name[:1] or self.id[:1]).upper()

    def to_dict(self) -> dict:
        data = {"id": self.id, "name": self.name, "role": self.role}
        if self.connect_id:
            data["connect_id"] = self.connect_id
        data.update({k: v for k, v in self.extra.items() if k not in data})
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Member":
        known = {"id", "name", "role", "connect_id"}
        return cls(id=str(data.get("id") or ""), name=str(data.get("name") or ""),
                   role=str(data.get("role") or "builder"), connect_id=str(data.get("connect_id") or ""),
                   extra={k: v for k, v in data.items() if k not in known})


@dataclass
class RecipePart:
    label: str
    choice: str
    why: str = ""

    def to_dict(self) -> dict:
        return {"label": self.label, "choice": self.choice, "why": self.why}

    @classmethod
    def from_dict(cls, data: dict) -> "RecipePart":
        return cls(label=str(data.get("label", "")), choice=str(data.get("choice", "")), why=str(data.get("why") or ""))


@dataclass
class Recipe:
    """How it's built, in plain words."""
    summary: str = ""
    runs_on: str = ""
    parts: list[RecipePart] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"summary": self.summary, "runs_on": self.runs_on, "parts": [p.to_dict() for p in self.parts]}

    @classmethod
    def from_dict(cls, data: dict | str | None) -> "Recipe":
        if not data:
            return cls()
        if isinstance(data, str):
            return cls(summary=data)
        return cls(summary=str(data.get("summary") or ""), runs_on=str(data.get("runs_on") or ""),
                   parts=[RecipePart.from_dict(p) for p in data.get("parts") or [] if isinstance(p, dict)])


@dataclass
class Project:
    """A project: .playground/project.toml plus where its folder is."""
    name: str
    id: str
    created: str = ""
    home: str = THIS_COMPUTER
    summary: str = ""
    recipe: Recipe = field(default_factory=Recipe)
    places: list[Place] = field(default_factory=list)
    members: list[Member] = field(default_factory=list)
    archived: bool = False
    path: Path | None = None
    # When anything last happened (from activity.jsonl); filled by list_projects, never stored.
    last_activity: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def place(self, place_id: str) -> Place | None:
        return next((p for p in self.places if p.id == place_id), None)

    def member(self, member_id: str) -> Member | None:
        return next((m for m in self.members if m.id == member_id), None)

    @property
    def owner(self) -> Member | None:
        return next((m for m in self.members if m.role == "owner"), None)

    def to_dict(self) -> dict:
        return {"name": self.name, "id": self.id, "created": self.created, "home": self.home,
                "summary": self.summary, "recipe": self.recipe.to_dict(),
                "places": [p.to_dict() for p in self.places], "members": [m.to_dict() for m in self.members],
                "archived": self.archived, "path": str(self.path) if self.path else "",
                "last_activity": self.last_activity}

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        return cls(name=str(data.get("name", "")), id=str(data.get("id", "")), created=str(data.get("created") or ""),
                   home=str(data.get("home") or THIS_COMPUTER), summary=str(data.get("summary") or ""),
                   recipe=Recipe.from_dict(data.get("recipe")),
                   places=[Place.from_dict(p) for p in data.get("places") or []],
                   members=[Member.from_dict(m) for m in data.get("members") or []],
                   archived=bool(data.get("archived", False)),
                   path=Path(data["path"]) if data.get("path") else None)


@dataclass
class RunConfig:
    """How to run the app for the Stage: .playground/run.toml, table [app].

    ``command`` may contain ``{port}``; with ``port = 0`` the runner picks a
    free port and substitutes it (and also sets PORT in the environment).
    """
    command: str = ""
    cwd: str = "."
    port: int = 0
    url_path: str = "/"
    kind: str = "web"                  # web, gtk (shown inside Playground), or vnc (any screen: a VM, a machine)
    env: dict[str, str] = field(default_factory=dict)
    ready_timeout_s: float = 30.0
    toolkit: str = ""                  # gtk: "gtk4" (default) or "gtk3"
    host: str = ""                     # vnc: the screen's address ("127.0.0.1" for a VM here)
    screen_port: int = 0               # vnc: its port (5900 + display number)
    password_env: str = ""             # vnc: the environment variable holding its password, never the password

    def to_dict(self) -> dict:
        data = {"command": self.command, "cwd": self.cwd, "port": self.port, "url_path": self.url_path,
                "kind": self.kind, "env": dict(self.env), "ready_timeout_s": self.ready_timeout_s}
        for key in ("toolkit", "host", "screen_port", "password_env"):
            if getattr(self, key):
                data[key] = getattr(self, key)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RunConfig":
        try:
            port = int(data.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        try:
            timeout = float(data.get("ready_timeout_s") or 30.0)
        except (TypeError, ValueError):
            timeout = 30.0
        try:
            screen_port = int(data.get("screen_port") or 0)
        except (TypeError, ValueError):
            screen_port = 0
        return cls(command=str(data.get("command") or ""), cwd=str(data.get("cwd") or "."), port=port,
                   url_path=str(data.get("url_path") or "/"), kind=str(data.get("kind") or "web"),
                   env={str(k): str(v) for k, v in (data.get("env") or {}).items()}, ready_timeout_s=timeout,
                   toolkit=str(data.get("toolkit") or ""), host=str(data.get("host") or ""),
                   screen_port=screen_port, password_env=str(data.get("password_env") or ""))


# ── The Project space: stack, bugs, service usage, analytics ──

SERVICE_KINDS = ("email", "database", "storage", "hosting", "auth", "payments", "analytics", "dns", "ai",
                 "messaging", "other")
ISSUE_SEVERITIES = ("low", "medium", "high", "urgent")
ISSUE_STATES = ("open", "fixing", "fixed", "wont-fix")
ISSUE_PLACES = ("stage", "live")

# Shapes that are almost certainly a credential, not the name of one.
_SECRET_SHAPES = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:sk|pk|rk|re|ghp|gho|xox[abpr]|AKIA|ASIA|SG|AC|whsec|eyJ)[-_.]?[A-Za-z0-9_\-]{12,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|[A-Za-z0-9+/_\-]{40,}")


def looks_like_secret(text: str) -> bool:
    """True for values that look like keys or tokens. The project holds secret *names* only."""
    return bool(_SECRET_SHAPES.search(str(text)))


@dataclass
class Service:
    """An outside service the project uses: one [[services]] table in stack.toml."""
    id: str
    name: str
    kind: str = "other"
    provider: str = ""
    purpose: str = ""
    used_by: list[str] = field(default_factory=list)
    environments: dict[str, str] = field(default_factory=dict)   # {"stage": "...", "live": "..."}
    console_url: str = ""
    docs_url: str = ""
    plan: str = ""
    monthly_budget: float = 0.0
    secrets: list[str] = field(default_factory=list)              # vault secret names, never values
    usage_source: str = "manual"
    added_by: str = ""
    added: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    _KNOWN = ("id", "name", "kind", "provider", "purpose", "used_by", "environments", "console_url", "docs_url",
              "plan", "monthly_budget", "secrets", "usage_source", "added_by", "added")

    def to_dict(self) -> dict:
        data = {"id": self.id, "name": self.name, "kind": self.kind, "provider": self.provider, "purpose": self.purpose,
                "used_by": list(self.used_by), "environments": dict(self.environments), "console_url": self.console_url,
                "docs_url": self.docs_url, "plan": self.plan, "monthly_budget": self.monthly_budget,
                "secrets": list(self.secrets), "usage_source": self.usage_source, "added_by": self.added_by,
                "added": self.added}
        data.update({k: v for k, v in self.extra.items() if k not in data})
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Service":
        kind = str(data.get("kind") or "other")
        try:
            budget = float(data.get("monthly_budget") or 0)
        except (TypeError, ValueError):
            budget = 0.0
        environments = data.get("environments") if isinstance(data.get("environments"), dict) else {}
        return cls(id=str(data.get("id") or ""), name=str(data.get("name") or ""),
                   kind=kind if kind in SERVICE_KINDS else "other", provider=str(data.get("provider") or ""),
                   purpose=str(data.get("purpose") or ""), used_by=[str(x) for x in data.get("used_by") or []],
                   environments={str(k): str(v) for k, v in environments.items()},
                   console_url=str(data.get("console_url") or ""), docs_url=str(data.get("docs_url") or ""),
                   plan=str(data.get("plan") or ""), monthly_budget=budget,
                   secrets=[str(x) for x in data.get("secrets") or []],
                   usage_source=str(data.get("usage_source") or "manual"), added_by=str(data.get("added_by") or ""),
                   added=str(data.get("added") or ""),
                   extra={k: v for k, v in data.items() if k not in cls._KNOWN})


@dataclass
class Dependency:
    """A package the app uses, read from package.json or requirements*.txt."""
    name: str
    version: str = ""
    why: str = ""
    source: str = ""        # the file it came from, project-relative
    dev: bool = False

    def to_dict(self) -> dict:
        data = {"name": self.name, "version": self.version, "why": self.why, "source": self.source}
        if self.dev:
            data["dev"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Dependency":
        return cls(name=str(data.get("name") or ""), version=str(data.get("version") or ""),
                   why=str(data.get("why") or ""), source=str(data.get("source") or ""), dev=bool(data.get("dev", False)))


@dataclass
class Stack:
    services: list[Service] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def service(self, service_id: str) -> Service | None:
        return next((s for s in self.services if s.id == service_id), None)

    def to_dict(self) -> dict:
        return {"services": [s.to_dict() for s in self.services],
                "dependencies": [d.to_dict() for d in self.dependencies]}


ISSUE_SECTIONS = ("What happened", "Expected", "Steps", "Evidence")


@dataclass
class Issue:
    """A bug report: .playground/issues/<number>.md."""
    number: int
    title: str
    severity: str = "medium"
    status: str = "open"
    reporter: str = ""
    goal: str = ""
    created: str = ""
    updated: str = ""
    place: str = "live"
    what_happened: str = ""
    expected: str = ""
    steps: str = ""
    evidence: str = ""
    sections: list[tuple[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_closed(self) -> bool:
        return self.status in ("fixed", "wont-fix")

    def to_dict(self) -> dict:
        data = {"number": self.number, "title": self.title, "severity": self.severity, "status": self.status,
                "reporter": self.reporter, "goal": self.goal, "created": self.created, "updated": self.updated,
                "place": self.place, "what_happened": self.what_happened, "expected": self.expected,
                "steps": self.steps, "evidence": self.evidence}
        if self.sections:
            data["sections"] = [{"heading": h, "text": t} for h, t in self.sections]
        return data


@dataclass
class ServiceUsage:
    """One usage snapshot for a service: a line of usage/services.jsonl. Numbers only.

    ``period`` is a day ("2026-10-08"), an ISO week ("2026-W41") or a month ("2026-10").
    """
    service_id: str
    period: str
    metrics: dict[str, float] = field(default_factory=dict)
    cost: float | None = None
    currency: str = "USD"
    source: str = "manual"
    ts: str = ""

    def to_dict(self) -> dict:
        data: dict[str, Any] = {"ts": self.ts or now_iso(), "service_id": self.service_id, "period": self.period,
                                "metrics": dict(self.metrics)}
        if self.cost is not None:
            data["cost"] = self.cost
        data.update(currency=self.currency, source=self.source)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ServiceUsage":
        metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
        cost = data.get("cost")
        return cls(service_id=str(data.get("service_id") or ""), period=str(data.get("period") or ""),
                   metrics={str(k): v for k, v in metrics.items() if _is_number(v)},
                   cost=float(cost) if _is_number(cost) else None, currency=str(data.get("currency") or "USD"),
                   source=str(data.get("source") or "manual"), ts=str(data.get("ts") or ""))


@dataclass
class AnalyticsSnapshot:
    """How the live app was used over a period: a line of analytics.jsonl."""
    period: str
    visitors: int = 0
    sessions: int = 0
    top_pages: list[dict] = field(default_factory=list)   # [{"path": "/", "views": 120}]
    errors: int = 0
    source: str = "manual"
    ts: str = ""

    def to_dict(self) -> dict:
        return {"ts": self.ts or now_iso(), "period": self.period, "visitors": self.visitors, "sessions": self.sessions,
                "top_pages": [dict(p) for p in self.top_pages], "errors": self.errors, "source": self.source}

    @classmethod
    def from_dict(cls, data: dict) -> "AnalyticsSnapshot":
        pages = [p for p in data.get("top_pages") or [] if isinstance(p, dict)]
        return cls(period=str(data.get("period") or ""), visitors=_int(data.get("visitors")),
                   sessions=_int(data.get("sessions")), top_pages=pages, errors=_int(data.get("errors")),
                   source=str(data.get("source") or "manual"), ts=str(data.get("ts") or ""))


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int(value: object) -> int:
    return int(value) if _is_number(value) else 0


def conversation_title(words: str) -> str:
    """A conversation's name from its first message: the first sentence, cut at a word, at most ~48 characters,
    never ending on an unclosed bracket or a dangling "and"."""
    text = " ".join(str(words or "").split())
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0].rstrip(".!?") or "New conversation"
    if len(first) > 48:
        first = first[:48].rsplit(" ", 1)[0]
        if first.count("(") > first.count(")"):
            first = first[:first.rfind("(")]
        words_left = first.split()
        while len(words_left) > 2 and words_left[-1].lower().strip(",;:") in (
                "and", "or", "but", "the", "a", "an", "with", "for", "to", "of", "in", "on", "at", "so"):
            words_left.pop()
        first = " ".join(words_left).rstrip(",;: ")
    return first[:1].upper() + first[1:]
