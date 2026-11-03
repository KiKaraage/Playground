# SPDX-License-Identifier: Apache-2.0
"""Playground's MCP server: how any agent keeps the project in order.

An agent starts it through the project's .mcp.json
(``playground-mcp --project <dir>``) and talks newline-delimited JSON-RPC over
stdin and stdout. Every tool writes through core.store, so the app sees the
change through its watcher a moment later. Every call is written to
activity.jsonl.

Both protocol eras are served by the same process:
- legacy clients (2025-11-25 and earlier) open with ``initialize``;
- modern clients (2026-07-28) send ``_meta`` on every request and may call
  ``server/discover`` first. A modern request naming a version we don't speak
  gets UnsupportedProtocolVersionError (-32022).

Tool calls run on their own threads, because ``playground_decision_ask`` waits
(up to 30 minutes) for the person, and pings and other calls must still be
answered meanwhile. While it waits it sends ``notifications/progress`` if the
request carried a progressToken, and it stops waiting on
``notifications/cancelled`` or when stdin closes.

Environment the agent runner may set: PLAYGROUND_ACTOR (the connection id the
agent runs on, such as "ari" or "priya-claude") and PLAYGROUND_GOAL (the goal
the agent was started on; tools that take goal_id default to it).
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import logging
import os
import re
from pathlib import Path
import sys
import threading
import time
from typing import Any, BinaryIO, Callable

from .. import VERSION
from ..core import events
from ..core.model import (
    BRIEF_GOOD, BRIEF_NOT_NOW, BRIEF_SUMMARY, ISSUE_PLACES, ISSUE_SEVERITIES, ISSUE_STATES, SERVICE_KINDS, SIZES,
    AnalyticsSnapshot, Answer, Check, Decision, GoalStatus, KnowledgeNote, Option, PendingDecision, PendingRequest,
    Service, ServiceUsage, Story, ThreadEntry, GENERAL_CHAT_ID, KNOWLEDGE_KINDS, PRIORITIES, TASK_STATUSES,
    knowledge_kept, now_iso, parse_ts,
)
from ..core.areas import CHANNEL_KINDS
from ..core.paths import PathOutsideProject, is_project, is_valid_id, new_id, relative_to_project, slugify
from ..core.store import NotFound, ProjectStore, StoreError, read_json

log = logging.getLogger(__name__)

MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_VERSIONS = (MODERN_VERSION, *LEGACY_VERSIONS)
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_VERSION = -32022

DECISION_TIMEOUT_S = 30 * 60
# How long a decision_ask call itself waits. Agents give up on a tool call after a minute or so (Codex's default
# is 60 s), so a longer wait strands the answer; after this the question stays open, the agent ends its turn,
# and Playground sends the answer as its next message (agents.runner.GoalRunner.answer_decision).
DECISION_WAIT_S = 45
SCREENSHOT_TIMEOUT_S = 60
CHECKS_TIMEOUT_S = 10 * 60
KEEPALIVE_S = 15
POLL_S = 0.5
APP_FRESH_S = 30
IMAGE_INLINE_LIMIT = 1_500_000

SERVER_INFO = {"name": "playground", "title": "Playground", "version": VERSION}
INSTRUCTIONS = (
    "You are working inside a Playground project. Stay inside the project folder. Keep Playground up to date "
    "as you work: goals, steps, decisions and things worth remembering go through these tools. Ask the person "
    "with playground_decision_ask when a decision is theirs. Prove changes with playground_checks_run and "
    "playground_proof_attach before calling them done. Never change production; use playground_release_propose. "
    "AGENTS.md in the project folder has the full rules."
)


class ToolError(Exception):
    """A problem the agent can fix. The message is shown to the agent as the tool result."""


@dataclass
class Call:
    """One tools/call in flight."""
    request_id: Any
    progress_token: Any
    cancelled: threading.Event


@dataclass
class Tool:
    name: str
    title: str
    description: str
    schema: dict
    handler: Callable[["PlaygroundServer", dict, Call], "ToolResult"]
    read_only: bool = False

    def describe(self) -> dict:
        return {"name": self.name, "title": self.title, "description": self.description, "inputSchema": self.schema,
                "annotations": {"title": self.title, "readOnlyHint": self.read_only, "destructiveHint": False,
                                "idempotentHint": self.read_only, "openWorldHint": False}}


@dataclass
class ToolResult:
    message: str
    data: Any = None
    images: list[tuple[str, str]] | None = None  # (base64, mime type)
    activity: str = ""                           # a summary for the history; "" means a plain tool line
    activity_kind: str = "tool"
    goal: str = ""
    logged: bool = False                         # the tool already wrote its own history line

    def to_content(self) -> dict:
        text = self.message
        if self.data is not None:
            text += "\n\n" + json.dumps(self.data, ensure_ascii=False, indent=1)
        content: list[dict] = [{"type": "text", "text": text}]
        for data, mime in self.images or []:
            content.append({"type": "image", "data": data, "mimeType": mime})
        result: dict[str, Any] = {"content": content, "isError": False}
        if self.data is not None:
            result["structuredContent"] = self.data if isinstance(self.data, dict) else {"items": self.data}
        return result


# ══ The server ═════════════════════════════════════════════════════════════════

class PlaygroundServer:
    """Serves one project over a pair of byte streams."""

    def __init__(self, project: Path | str, *, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None,
                 actor: str = "", goal: str = "", decision_timeout: float = DECISION_TIMEOUT_S,
                 keepalive: float = KEEPALIVE_S, poll: float = POLL_S) -> None:
        self.root = Path(project).expanduser().absolute()
        # A private bus: nobody in this process listens; the app learns through its watcher.
        self.store = ProjectStore(self.root, bus=events.EventBus())
        self.stdin = stdin if stdin is not None else sys.stdin.buffer
        self.stdout = stdout if stdout is not None else sys.stdout.buffer
        self.actor = actor or os.environ.get("PLAYGROUND_ACTOR", "")
        self.default_goal = goal or os.environ.get("PLAYGROUND_GOAL", "")
        self.decision_timeout = decision_timeout
        self.decision_wait = DECISION_WAIT_S
        self.keepalive = keepalive
        self.poll = poll
        self.client_name = ""
        self.legacy_version: str | None = None
        self._write_lock = threading.Lock()
        self._calls: dict[Any, Call] = {}
        self._calls_lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self.closing = threading.Event()
        self.tools = {tool.name: tool for tool in TOOLS}

    # ── transport ──

    def serve(self) -> int:
        """Read requests until stdin closes. Returns the exit status."""
        try:
            for raw in iter(self.stdin.readline, b""):
                line = raw.strip()
                if line:
                    self._receive(line)
        except (KeyboardInterrupt, OSError):
            pass
        finally:
            self.closing.set()
            for call in list(self._calls.values()):
                call.cancelled.set()
            for worker in list(self._workers):
                worker.join(timeout=5)
        return 0

    def send(self, message: dict) -> None:
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._write_lock:
            try:
                self.stdout.write(data)
                self.stdout.flush()
            except (BrokenPipeError, ValueError, OSError):
                self.closing.set()

    def _receive(self, line: bytes) -> None:
        try:
            message = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            self.send(_error(None, PARSE_ERROR, "That wasn't valid JSON."))
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self.send(_error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST,
                             "Expected a JSON-RPC 2.0 message."))
            return
        method = message.get("method")
        if not isinstance(method, str):
            return  # a response to something we sent; we never send requests
        if "id" not in message:
            self._notification(method, message.get("params") or {})
            return
        if method == "tools/call":
            worker = threading.Thread(target=self._respond, args=(message,), daemon=True,
                                      name=f"mcp-call-{message.get('id')}")
            self._workers = [w for w in self._workers if w.is_alive()]
            self._workers.append(worker)
            worker.start()
        else:
            self._respond(message)

    def _notification(self, method: str, params: dict) -> None:
        if method == "notifications/cancelled":
            with self._calls_lock:
                call = self._calls.get(params.get("requestId"))
            if call is not None:
                call.cancelled.set()
        # notifications/initialized and anything else need nothing from us.

    def _respond(self, message: dict) -> None:
        response = self.handle(message)
        if response is not None:
            self.send(response)

    # ── dispatch ──

    def handle(self, message: dict) -> dict | None:
        """One request in, one response out (None when the request was cancelled)."""
        request_id = message.get("id")
        method = message.get("method", "")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object.")
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        version = meta.get(META_VERSION)
        modern = version is not None
        if modern and version not in SUPPORTED_VERSIONS:
            return _error(request_id, UNSUPPORTED_VERSION, "Unsupported protocol version",
                          {"supported": list(SUPPORTED_VERSIONS), "requested": version})
        client = meta.get(META_CLIENT_INFO)
        if isinstance(client, dict) and client.get("name") and not self.client_name:
            self.client_name = str(client["name"])
        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "server/discover":
                result = self._discover()
                modern = True
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [tool.describe() for tool in listed_tools()]}
                if modern:
                    result.update(ttlMs=3_600_000, cacheScope="public")
            elif method == "tools/call":
                result = self._call_tool(request_id, params, meta)
                if result is None:
                    return None
            elif method == "logging/setLevel":
                result = {}
            else:
                return _error(request_id, METHOD_NOT_FOUND, f"Playground doesn't offer {method}.")
        except ToolError as error:
            return _error(request_id, INVALID_PARAMS, str(error))
        except Exception as error:  # noqa: BLE001 - report, don't die
            log.exception("%s failed", method)
            return _error(request_id, INTERNAL_ERROR, f"Something went wrong in Playground: {error}")
        if modern:
            result = {"resultType": "complete", **result}
            result.setdefault("_meta", {})[META_SERVER_INFO] = SERVER_INFO
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        self.legacy_version = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        client = params.get("clientInfo")
        if isinstance(client, dict) and client.get("name"):
            self.client_name = str(client["name"])
        return {"protocolVersion": self.legacy_version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}

    def _discover(self) -> dict:
        return {"supportedVersions": list(SUPPORTED_VERSIONS), "capabilities": {"tools": {"listChanged": False}},
                "instructions": INSTRUCTIONS, "ttlMs": 3_600_000, "cacheScope": "public"}

    def _call_tool(self, request_id: Any, params: dict, meta: dict) -> dict | None:
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise ToolError(f"There's no tool called {name}. Call tools/list to see them.")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ToolError("arguments must be an object.")
        call = Call(request_id, meta.get("progressToken"), threading.Event())
        with self._calls_lock:
            self._calls[request_id] = call
        try:
            try:
                there = self
                if tool.name in FAMILY_TOOLS and str(arguments.get("project") or "").strip():
                    there = self.elsewhere(str(arguments["project"]))
                arguments = {k: v for k, v in arguments.items() if k != "project"}
                result = tool.handler(there, arguments, call)
                if there is not self and result.activity:
                    result.activity += f" (in {there.store.read_project().name})"
            except (ToolError, StoreError, PathOutsideProject, ValueError) as error:
                self._record(tool, ok=False, summary=f"{tool.name} didn't work: {error}", goal=self._goal_hint(arguments))
                return {"content": [{"type": "text", "text": str(error)}], "isError": True}
            if call.cancelled.is_set() and not self.closing.is_set():
                return None  # the client cancelled; it expects no response
            if not result.logged:
                self._record(tool, ok=True, summary=result.activity, kind=result.activity_kind,
                             goal=result.goal or self._goal_hint(arguments))
            return result.to_content()
        finally:
            with self._calls_lock:
                self._calls.pop(request_id, None)

    def _record(self, tool: Tool, *, ok: bool, summary: str, kind: str = "tool", goal: str = "") -> None:
        """Every call goes in the history: what it did, or a plain tool line for reads."""
        data = {"tool": tool.name, "ok": ok}
        if self.client_name:
            data["client"] = self.client_name
        try:
            self.store.log(summary or f"Used {tool.name}", kind=kind if ok else "tool", actor=self.who,
                           goal=goal, data=data)
        except OSError as error:
            log.warning("couldn't write the activity log: %s", error)

    # ── helpers for tools ──

    def elsewhere(self, ref: str) -> "PlaygroundServer":
        """This server, pointed at another project of the family (core.family): a bug filed in Luma from the
        calculator lands in Luma's Bugs. Its conversations aren't this one's, so no goal carries over."""
        import copy
        from ..core import family
        try:
            folder = family.resolve(self.root, ref)
        except ValueError as error:
            raise ToolError(str(error)) from error
        if folder == self.root:
            return self
        other = copy.copy(self)
        other.root = folder
        other.store = ProjectStore(folder, bus=events.EventBus())
        other.default_goal = ""
        return other

    @property
    def who(self) -> str:
        return self.actor or (slugify(self.client_name, 24) if self.client_name else "agent")

    def _goal_hint(self, arguments: dict) -> str:
        goal_id = arguments.get("goal_id") or self.default_goal
        return goal_id if is_valid_id(goal_id) else ""

    def goal_id(self, arguments: dict, required: bool = True) -> str:
        goal_id = arguments.get("goal_id") or self.default_goal
        if not goal_id:
            if required:
                raise ToolError("Say which goal: pass goal_id (playground_goal_list shows them).")
            return GENERAL_CHAT_ID
        if not is_valid_id(goal_id):
            raise ToolError(f"{goal_id!r} isn't a goal id. Ids look like add-a-waitlist-7f3a.")
        if not self.store.paths.goal(goal_id).exists():
            raise ToolError(f"There's no goal called {goal_id}. playground_goal_list shows them.")
        return goal_id

    def thread_goal(self, arguments: dict) -> str:
        """The goal a card belongs to: the one named, the default one, or the general chat."""
        goal_id = self.goal_id(arguments, required=False)
        if goal_id == GENERAL_CHAT_ID:
            self.store.ensure_general_chat()
        return goal_id

    def post(self, goal_id: str, kind: str, data: dict, text: str = "") -> ThreadEntry:
        return self.store.append_thread(goal_id, ThreadEntry(kind=kind, role="agent", text=text, data=data, by=self.who))

    def project_paths(self, values: Any, must_exist: bool = False) -> list[str]:
        """Project-relative forms of paths an agent named, refusing anything outside the project."""
        if values is None:
            return []
        if not isinstance(values, list):
            raise ToolError("Paths must be a list of strings.")
        result = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                rel = relative_to_project(self.root, value.strip())
            except PathOutsideProject:
                raise ToolError(f"{value} is outside the project folder.") from None
            if must_exist and not (self.root / rel).exists():
                raise ToolError(f"{rel} doesn't exist.")
            result.append(rel)
        return result

    def owner_id(self) -> str:
        try:
            owner = self.store.read_project().owner
        except StoreError:
            return ""
        return owner.id if owner else ""

    def person_name(self, member_id: str) -> str:
        try:
            member = self.store.read_project().member(member_id)
        except StoreError:
            member = None
        return member.first_name if member else (member_id or "Someone")

    def app_is_open(self) -> bool:
        """True when the Playground app has this project open (it refreshes local/app.json)."""
        data = self.store.read_app_heartbeat()
        updated = parse_ts(data.get("updated")) if data else None
        return bool(updated) and time.time() - updated.timestamp() < APP_FRESH_S

    def wait_for_answer(self, request_id: str, call: Call, timeout: float, waiting_for: str) -> Answer | None:
        """Poll answers/<id>.json until it appears, the call is cancelled, stdin closes, or time runs out."""
        started = time.monotonic()
        next_keepalive = started + self.keepalive
        while True:
            answer = self.store.read_answer(request_id)
            if answer is not None:
                return answer
            now = time.monotonic()
            if now - started >= timeout or call.cancelled.is_set() or self.closing.is_set():
                return None
            if call.progress_token is not None and now >= next_keepalive:
                next_keepalive = now + self.keepalive
                self.send({"jsonrpc": "2.0", "method": "notifications/progress",
                           "params": {"progressToken": call.progress_token, "progress": round(now - started, 1),
                                      "message": waiting_for}})
            call.cancelled.wait(self.poll)


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


# ══ Argument helpers ═══════════════════════════════════════════════════════════

def _text(arguments: dict, key: str, required: bool = False, limit: int = 4000) -> str:
    value = arguments.get(key)
    if value is None or value == "":
        if required:
            raise ToolError(f"{key} is required.")
        return ""
    if not isinstance(value, (str, int, float)):
        raise ToolError(f"{key} must be text.")
    text = str(value).strip()
    if len(text) > limit:
        raise ToolError(f"{key} is too long (keep it under {limit} characters).")
    return text


def _strings(arguments: dict, key: str) -> list[str]:
    value = arguments.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ToolError(f"{key} must be a list of strings.")
    return [v.strip() for v in value if v.strip()]


def _status(value: Any) -> GoalStatus:
    try:
        return GoalStatus.parse(value)
    except ValueError:
        raise ToolError(f"Status must be one of: {', '.join(s.value for s in GoalStatus)}.") from None


def _stories(value: Any) -> list[Story]:
    if not isinstance(value, list):
        raise ToolError("stories must be a list of {title, checks}.")
    stories = []
    for item in value:
        if isinstance(item, str):
            stories.append(Story(title=item.strip()))
        elif isinstance(item, dict) and str(item.get("title", "")).strip():
            checks = item.get("checks") or []
            if not isinstance(checks, list):
                raise ToolError("Each story's checks must be a list of strings.")
            stories.append(Story(title=str(item["title"]).strip(), done=bool(item.get("done", False)),
                                 checks=[Check(text=str(c).strip()) for c in checks if str(c).strip()]))
        else:
            raise ToolError("Each story needs a title.")
    return stories


def _norm(text: str) -> str:
    return " ".join(text.lower().split()).rstrip(".")


def _plan_card(goal) -> dict:
    data: dict[str, Any] = {"title": goal.title,
                            "stories": [{"title": s.title, "checks": [c.text for c in s.checks]} for s in goal.stories]}
    if goal.size:
        data["size"] = goal.size
    if goal.cost_estimate:
        data["estimate"] = {"connection": goal.connection, "amount_text": goal.cost_estimate}
    return data


def _goal_summary(goal) -> dict:
    return {"id": goal.id, "title": goal.title, "status": goal.status.value, "status_label": goal.status.label,
            "status_note": goal.status_note, "kind": goal.kind, "owner": goal.owner, "connection": goal.connection,
            "updated": goal.updated, "stories": len(goal.stories),
            "stories_done": sum(1 for s in goal.stories if s.done),
            "checks": len(goal.checks), "checks_passed": goal.checks_passed}


def _entry_brief(entry: ThreadEntry) -> dict:
    """A thread entry, trimmed for an agent catching up."""
    record = {"ts": entry.ts, "role": entry.role, "kind": entry.kind, "by": entry.by}
    if entry.text:
        record["text"] = entry.text[:600]
    if entry.data:
        record["data"] = entry.data
    return record


# ══ Schemas ════════════════════════════════════════════════════════════════════

GOAL_ID = {"type": "string", "description": "The goal's id, like add-a-waitlist-7f3a. Leave it out to use the goal you were started on."}
STATUS = {"type": "string", "enum": [s.value for s in GoalStatus]}
STORY = {"type": "object", "properties": {
    "title": {"type": "string", "description": "Something a person can do, in their words: \"Someone can join the waitlist for a full class\"."},
    "checks": {"type": "array", "items": {"type": "string"}, "description": "Behaviour a person could see for themselves: \"When all eight wheels are booked, the button says Join waitlist\"."},
}, "required": ["title"]}
PATHS = {"type": "array", "items": {"type": "string"}, "description": "Paths inside the project, relative to its folder."}
CHECK_RESULT = {"type": "object", "properties": {
    "text": {"type": "string", "description": "The check's text, as written in the plan."},
    "status": {"type": "string", "enum": ["pass", "fail", "pending"]},
    "evidence": {"type": "string", "description": "A screenshot or file inside the project that shows it."},
}, "required": ["text", "status"]}


SERVICE_SCHEMA = {
    "name": {"type": "string", "description": "\"Resend\""},
    "kind": {"type": "string", "enum": list(SERVICE_KINDS)},
    "provider": {"type": "string", "description": "Who runs it: \"Cloudflare R2\", \"AWS\"."},
    "purpose": {"type": "string", "description": "What it does for this project, in plain words: \"Sends booking confirmations and reminders\"."},
    "used_by": {"type": "array", "items": {"type": "string"}, "description": "The journeys or parts that use it."},
    "environments": {"type": "object", "additionalProperties": {"type": "string"},
                     "description": "{\"stage\": \"test mode\", \"live\": \"mail.kiln.example\"}. Descriptions or hostnames, never secrets."},
    "console_url": {"type": "string"}, "docs_url": {"type": "string"}, "plan": {"type": "string"},
    "monthly_budget": {"type": "number", "description": "Dollars a month."},
    "secrets": {"type": "array", "items": {"type": "string"}, "description": "Vault secret NAMES, like \"Email service key\". Never values."},
    "usage_source": {"type": "string", "description": "How usage is read: an adapter id (resend, cloudflare, aws, postgres, http) or manual."},
}


def _schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


# ══ Tools ══════════════════════════════════════════════════════════════════════

def project_info(server: PlaygroundServer, _args: dict, _call: Call) -> ToolResult:
    project = server.store.read_project()
    goals = server.store.list_goals()
    run = server.store.read_run_config()
    data = {"name": project.name, "folder": str(server.root), "summary": project.summary, "home": project.home,
            "recipe": project.recipe.to_dict(), "places": [p.to_dict() for p in project.places],
            "members": [m.to_dict() for m in project.members],
            "goals": {status.value: sum(1 for g in goals if g.status == status and not g.is_chat) for status in GoalStatus},
            "run": run.to_dict() if run else None, "you_are": server.who}
    return ToolResult(f"{project.name} lives in {server.root}.", data)


def goal_list(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    status = _status(args["status"]) if args.get("status") else None
    goals = [g for g in server.store.list_goals()
             if (status is None or g.status == status)
             and (args.get("include_done", True) or g.status not in (GoalStatus.DONE, GoalStatus.LIVE))]
    return ToolResult(f"{len(goals)} goal{'s' if len(goals) != 1 else ''}.", [_goal_summary(g) for g in goals])


def goal_get(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    goal_id = server.goal_id(args)
    goal = server.store.read_goal(goal_id)
    count = args.get("thread_entries", 20)
    count = max(0, min(int(count), 200)) if isinstance(count, (int, float)) else 20
    data = goal.to_dict()
    if count:
        data["recent_thread"] = [_entry_brief(e) for e in server.store.read_thread(goal_id, limit=count, fold=True)]
    data["waiting_decisions"] = [p.to_dict() for p in server.store.list_pending("decision") if p.goal == goal_id]
    return ToolResult(f"{goal.title}: {goal.status.label}.", data)


def goal_create(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    title = _text(args, "title", required=True, limit=120)
    stories = _stories(args.get("stories") or [])
    size = _text(args, "size")
    if size and size not in SIZES:
        raise ToolError("size must be small, medium or large.")
    status = _status(args.get("status") or "working")
    if status == GoalStatus.LIVE:
        raise ToolError("A new goal can't start live. Only a reviewed release makes a goal live.")
    goal = server.store.create_goal(title, outcome=_text(args, "outcome"), stories=stories, status=status,
                                    owner=_text(args, "owner") or server.owner_id(), connection=server.who,
                                    size=size, cost_estimate=_text(args, "cost_estimate"))
    if stories:
        server.post(goal.id, "plan", _plan_card(goal))
    return ToolResult(f"Started the goal {goal.title} ({goal.id}). It's in Playground's rail now.", goal.to_dict(),
                      activity=f"Started {goal.title}", activity_kind="goal", goal=goal.id)


def goal_update(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    goal_id = server.goal_id(args)
    changes: list[str] = []
    new_status = _status(args["status"]) if args.get("status") else None
    if new_status == GoalStatus.LIVE:
        raise ToolError("Only a reviewed release makes a goal live. Use playground_release_propose.")
    new_stories = _stories(args["stories"]) if args.get("stories") is not None else None
    size = _text(args, "size")
    if size and size not in SIZES:
        raise ToolError("size must be small, medium or large.")

    def change(goal) -> None:
        if new_status is not None and new_status != goal.status:
            goal.status = new_status
            changes.append(f"now {new_status.label.lower()}")
        if "status_note" in args:
            goal.status_note = _text(args, "status_note", limit=120)
        for key in ("title", "outcome", "cost_estimate"):
            if args.get(key):
                setattr(goal, key, _text(args, key, limit=120 if key == "title" else 4000))
                changes.append(f"new {key.replace('_', ' ')}")
        if size:
            goal.size = size
        if new_stories is not None:
            known = {_norm(c.text): c for c in goal.checks}
            for story in new_stories:
                story.checks = [known.get(_norm(c.text), c) for c in story.checks]
            goal.stories = new_stories
            changes.append("a new plan")
        for item in args.get("story_done") or []:
            story = _find_story(goal, item.get("story") if isinstance(item, dict) else None)
            story.done = bool(item.get("done", True))
            changes.append(f"“{story.title}” {'done' if story.done else 'not done'}")
        for item in args.get("check_results") or []:
            if not isinstance(item, dict):
                raise ToolError("Each check result needs text and status.")
            check = _find_check(goal, str(item.get("text") or item.get("check") or ""))
            status = str(item.get("status") or "")
            if status not in ("pass", "fail", "pending"):
                raise ToolError("A check's status is pass, fail or pending.")
            check.status = status
            if item.get("evidence"):
                check.evidence = server.project_paths([item["evidence"]])[0]
            changes.append(f"a check {'passed' if status == 'pass' else 'failed' if status == 'fail' else 'reset'}")
        if args.get("note"):
            note = _text(args, "note")
            goal.notes = f"{goal.notes}\n\n{note}".strip()
            changes.append("a note")

    goal = server.store.update_goal(goal_id, change)
    if new_stories is not None and args.get("post_plan", True):
        server.post(goal.id, "plan", _plan_card(goal))
    summary = f"Updated {goal.title}" + (f": {', '.join(changes)}" if changes else "")
    return ToolResult(summary + ".", goal.to_dict(), activity=summary, activity_kind="goal", goal=goal.id)


def _find_story(goal, ref: Any) -> Story:
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        index = int(ref) - 1
        if 0 <= index < len(goal.stories):
            return goal.stories[index]
    elif isinstance(ref, str):
        for story in goal.stories:
            if _norm(story.title) == _norm(ref):
                return story
    raise ToolError(f"No story matches {ref!r}. Use its number (from 1) or its exact title.")


def _find_check(goal, text: str) -> Check:
    wanted = _norm(text)
    matches = [c for c in goal.checks if _norm(c.text) == wanted] or [c for c in goal.checks if wanted and wanted in _norm(c.text)]
    if len(matches) != 1:
        raise ToolError(f"No single check matches {text!r}. Use the check's text as written in the plan.")
    return matches[0]


def step_report(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    goal_id = server.thread_goal(args)
    title = _text(args, "title", required=True, limit=160)
    state = _text(args, "state") or "done"
    if state not in ("running", "done", "failed"):
        raise ToolError("state is running, done or failed.")
    data: dict[str, Any] = {"title": title, "state": state, "files": server.project_paths(args.get("files"))}
    if isinstance(args.get("elapsed_s"), (int, float)):
        data["elapsed_s"] = float(args["elapsed_s"])
    for key in ("spend_text", "details"):
        if args.get(key):
            data[key] = _text(args, key)
    server.post(goal_id, "step", data)
    files = len(data["files"])
    summary = f"{title}" + (f" ({files} file{'s' if files != 1 else ''})" if files else "")
    return ToolResult("Recorded the step.", {"goal_id": goal_id, **data}, activity=summary, activity_kind="step", goal=goal_id)


def headsup_post(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    goal_id = server.thread_goal(args)
    text = _text(args, "text", required=True)
    data: dict[str, Any] = {"text": text, "files": server.project_paths(args.get("files"))}
    undo = args.get("undo")
    if isinstance(undo, dict) and undo.get("kind"):
        data["undo"] = {"kind": str(undo["kind"]), "ref": str(undo.get("ref") or "")}
    server.post(goal_id, "headsup", data)
    return ToolResult("Posted the heads-up. The person can undo it from the card.", data,
                      activity=f"Heads-up: {text[:140]}", activity_kind="agent", goal=goal_id)


def decision_ask(server: PlaygroundServer, args: dict, call: Call) -> ToolResult:
    question = _text(args, "question", required=True, limit=300)
    raw_options = args.get("options")
    if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= 4:
        raise ToolError("Give two to four options.")
    options = []
    for index, raw in enumerate(raw_options):
        option = Option.from_dict(raw, index)
        if not option.label.strip():
            raise ToolError("Every option needs a label.")
        option.id = slugify(option.id, 32) if option.id else f"option-{index + 1}"
        if any(o.id == option.id for o in options):
            option.id = f"{option.id}-{index + 1}"
        options.append(option)
    recommended = _text(args, "recommended")
    if recommended:
        match = next((o for o in options if recommended in (o.id, o.label) or _norm(recommended) == _norm(o.label)), None)
        if match is None:
            raise ToolError("recommended must be one of the options' ids or labels.")
        recommended = match.id
    goal_id = server.thread_goal(args)
    decision_id = new_id(question, 32)
    expires = time.time() + server.decision_timeout
    pending = PendingDecision(id=decision_id, question=question, options=options, goal=goal_id,
                              recommended=recommended, default=_text(args, "default"), context=_text(args, "context"),
                              asked_by=server.who, asked_at=now_iso(),
                              expires_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(expires)))
    server.store.write_pending(pending)
    server.post(goal_id, "decision", {"id": decision_id, "question": question, "options": [o.to_dict() for o in options],
                                      "recommended": recommended, "default": pending.default,
                                      **({"context": pending.context} if pending.context else {})})
    server.store.log(f"Asked: {question}", kind="decision", actor=server.who, goal=goal_id,
                     data={"decision": decision_id, "tool": "playground_decision_ask"})
    previous = _mark_needs_you(server, goal_id)

    if args.get("wait") is False:
        return ToolResult(f"Asked. The decision id is {decision_id}; check it with playground_decision_get.",
                          {"decision_id": decision_id, "state": "waiting"}, logged=True)

    patience = min(server.decision_wait, server.decision_timeout)
    answer = server.wait_for_answer(decision_id, call, patience, "Waiting for the person to decide")
    if answer is None and patience < server.decision_timeout and not call.cancelled.is_set() \
            and not server.closing.is_set():
        # Still open: the card stays, and the answer comes to the agent as its next message.
        return ToolResult("The question is on the person's screen and stays open. Don't ask it again in your "
                          "reply. Carry on with work that doesn't depend on it, or end your turn; their answer "
                          "comes to you as your next message.",
                          {"decision_id": decision_id, "state": "waiting"}, logged=True)
    server.store.remove_pending(decision_id)
    _restore_status(server, goal_id, previous)
    if answer is None:
        reason = ("You stopped waiting" if call.cancelled.is_set() or server.closing.is_set()
                  else "Nobody answered in 30 minutes")
        server.store.log(f"{reason} for an answer to: {question}", kind="decision", actor=server.who, goal=goal_id,
                         data={"decision": decision_id, "answered": False})
        return ToolResult(f"{reason}, so the question is closed. Don't guess: leave this part, say so in the "
                          "conversation, and carry on with work that doesn't depend on it. You can ask again later.",
                          {"decision_id": decision_id, "state": "no answer"}, logged=True)
    if answer.option and not answer.label:
        chosen_option = pending.option(answer.option)
        answer.label = chosen_option.label if chosen_option else answer.option
    who = server.person_name(answer.answered_by)
    chosen = answer.label or answer.text
    data = {"decision_id": decision_id, "state": "answered", "option": answer.option, "label": answer.label,
            "text": answer.text, "answered_by": answer.answered_by}
    # The person's answer is already in the history (store.answer_decision), as is the question.
    return ToolResult(f"{who} decided: {chosen}. Go ahead on that basis.", data, logged=True)


def _mark_needs_you(server: PlaygroundServer, goal_id: str) -> tuple[GoalStatus, str] | None:
    """Show the goal as Needs you while the agent waits; returns what it was."""
    previous: list = []

    def change(goal) -> None:
        previous.append((goal.status, goal.status_note))
        if not goal.is_chat and goal.status in (GoalStatus.WORKING, GoalStatus.NEEDS_YOU):
            goal.status = GoalStatus.NEEDS_YOU
            goal.status_note = "one decision"

    try:
        server.store.update_goal(goal_id, change)
    except NotFound:
        return None
    return previous[0] if previous else None


def _restore_status(server: PlaygroundServer, goal_id: str, previous: tuple[GoalStatus, str] | None) -> None:
    if previous is None or any(p.goal == goal_id for p in server.store.list_pending("decision")):
        return

    def change(goal) -> None:
        if goal.status == GoalStatus.NEEDS_YOU and goal.status_note == "one decision":
            goal.status, goal.status_note = (previous if previous[0] != GoalStatus.NEEDS_YOU
                                             else (GoalStatus.WORKING, ""))

    try:
        server.store.update_goal(goal_id, change)
    except NotFound:
        pass


def decision_get(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    decision_id = _text(args, "decision_id", required=True)
    if not is_valid_id(decision_id):
        raise ToolError(f"{decision_id!r} isn't a decision id.")
    answer = server.store.read_answer(decision_id)
    if answer is not None:
        return ToolResult(f"Answered: {answer.label or answer.text}.", {"state": "answered", **answer.to_dict()})
    if server.store.read_pending(decision_id) is not None:
        return ToolResult("Still waiting for the person.", {"state": "waiting", "decision_id": decision_id})
    raise ToolError(f"There's no decision called {decision_id}.")


def decision_record(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    goal_id = server.goal_id(args, required=False) if args.get("goal_id") or server.default_goal else ""
    decision = Decision(title=_text(args, "title", required=True, limit=200), chosen=_text(args, "chosen", required=True),
                        why=_text(args, "why"), who=_text(args, "who") or server.who, goal=goal_id)
    server.store.append_decision(decision)
    return ToolResult("Added to the project's decisions.", decision.to_dict(),
                      activity=f"Recorded a decision: {decision.title}", activity_kind="decision", goal=goal_id)


def activity_log(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    summary = _text(args, "summary", required=True, limit=300)
    kind = _text(args, "kind") or "agent"
    if kind == "tool":
        kind = "agent"
    goal = server.goal_id(args, required=False) if args.get("goal_id") or server.default_goal else ""
    data: dict[str, Any] = {"tool": "playground_activity_log"}
    files = server.project_paths(args.get("files"))
    if files:
        data["files"] = files
    if isinstance(args.get("data"), dict):
        data["details"] = args["data"]
    entry = server.store.log(summary, kind=kind, actor=server.who, goal="" if goal == GENERAL_CHAT_ID else goal, data=data)
    return ToolResult("Added to the history.", entry.to_dict(), logged=True)


def activity_list(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    limit = args.get("limit", 30)
    limit = max(1, min(int(limit), 200)) if isinstance(limit, (int, float)) else 30
    goal = args.get("goal_id") or ""
    if goal and not is_valid_id(goal):
        raise ToolError(f"{goal!r} isn't a goal id.")
    entries = server.store.read_activity(goal=goal or None)
    if not args.get("include_tool_calls"):
        entries = [e for e in entries if e.kind != "tool"]
    entries = entries[-limit:]
    return ToolResult(f"The last {len(entries)} things that happened, oldest first.", [e.to_dict() for e in entries])


def knowledge_add(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    title = _text(args, "title", required=True, limit=120)
    body = _text(args, "body", required=True, limit=20000)
    slug = _text(args, "slug") or slugify(title, 60)
    if not is_valid_id(slug):
        raise ToolError("slug may use lowercase letters, numbers and hyphens only.")
    existing = server.store.read_knowledge(slug)
    if existing is not None and args.get("mode") == "append":
        body = f"{existing.body}\n\n{body}"
    kind = _text(args, "kind") or (existing.extra.get("kind") if existing else "") or "note"
    if kind not in KNOWLEDGE_KINDS:
        raise ToolError(f"kind is one of {', '.join(KNOWLEDGE_KINDS)}.")
    extra = dict(existing.extra) if existing else {"created": now_iso()}
    extra["kind"] = kind
    for key in ("paths", "uses"):
        value = args.get(key)
        if isinstance(value, list):
            extra[key] = [str(v)[:200] for v in value[:20] if str(v).strip()]
    if kind == "rule" and not (existing and knowledge_kept(existing) and existing.extra.get("kind") == "rule"):
        # Rules are read on every run, so an agent's rule waits for a person to keep it.
        extra["status"] = "suggested"
    note = server.store.write_knowledge(KnowledgeNote(slug=slug, title=title, body=body,
                                                      added_by=existing.added_by if existing else server.who,
                                                      updated=now_iso(), extra=extra))
    verb = "Updated" if existing else "Added"
    waits = " It's a suggestion until a person keeps it." if extra.get("status") == "suggested" else ""
    return ToolResult(f"{verb} the note {title}.{waits}", note.to_dict(), activity=f"{verb} a note: {title}",
                      activity_kind="knowledge")


def knowledge_search(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    query = _text(args, "query", limit=200)
    limit = args.get("limit", 10)
    limit = max(1, min(int(limit), 50)) if isinstance(limit, (int, float)) else 10
    results = []
    for note, score in server.store.search_knowledge(query, limit):
        results.append({"slug": note.slug, "title": note.title, "added_by": note.added_by, "updated": note.updated,
                        "score": score, "text": note.body if len(note.body) <= 800 else note.body[:800] + "…"})
    found = f"{len(results)} note{'s' if len(results) != 1 else ''}"
    return ToolResult(f"{found}{' about ' + repr(query) if query else ''}.", results)


def knowledge_get(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    slug = _text(args, "slug", required=True)
    if not is_valid_id(slug):
        raise ToolError(f"{slug!r} isn't a note's short name.")
    note = server.store.read_knowledge(slug)
    if note is None:
        raise ToolError(f"There's no note called {slug}. playground_knowledge_search lists them.")
    return ToolResult(note.title, note.to_dict())


def brief_get(server: PlaygroundServer, _args: dict, _call: Call) -> ToolResult:
    brief = server.store.read_brief()
    return ToolResult(f"The brief for {server.store.read_project().name}.", brief.to_dict())


def brief_update(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    brief = server.store.read_brief()
    changed = []
    for key in ("title", "audience"):
        if args.get(key):
            setattr(brief, key, _text(args, key, limit=300))
            changed.append(key)
    if args.get("platforms") is not None:
        brief.platforms = _strings(args, "platforms")
        changed.append("platforms")
    if args.get("summary"):
        brief.set_section(BRIEF_SUMMARY, _text(args, "summary"))
        changed.append("what it is")
    if args.get("good_looks_like") is not None:
        brief.set_section(BRIEF_GOOD, "\n".join(f"- {item}" for item in _strings(args, "good_looks_like")))
        changed.append("what good looks like")
    if args.get("not_now"):
        brief.set_section(BRIEF_NOT_NOW, _text(args, "not_now"))
        changed.append("what's not for now")
    if not changed:
        raise ToolError("Nothing to change. Pass at least one field.")
    server.store.write_brief(brief)
    goal_id = server.thread_goal(args)
    server.post(goal_id, "brief", {"title": brief.title, "audience": brief.audience, "platforms": brief.platforms,
                                   "summary": brief.summary, "good_looks_like": brief.good_looks_like})
    return ToolResult("Updated the brief.", brief.to_dict(), activity=f"Updated the brief: {', '.join(changed)}",
                      activity_kind="brief", goal=goal_id)


def preview_status(server: PlaygroundServer, _args: dict, _call: Call) -> ToolResult:
    state = server.store.read_preview_state()
    run = server.store.read_run_config()
    data = {"state": state.get("state", "stopped"), "url": state.get("url", ""), "error": state.get("error", ""),
            "updated": state.get("updated", ""), "app_open": server.app_is_open(), "run": run.to_dict() if run else None}
    if data["state"] == "running" and data["url"]:
        message = f"The preview is running at {data['url']}."
    elif run:
        message = (f"The preview isn't running. Playground starts it while the project is open, and restarts it "
                   f"by itself when the app's files change; you can also run `{run.command}` in {run.cwd} yourself.")
    else:
        message = "The preview isn't running, and there's no .playground/run.toml saying how to run the app yet."
    return ToolResult(message, data)


def now_tool(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    """Where things stand (now.md), and for the paths about to change, everything known about them: part
    notes whose paths cover them, rules and lessons scoped to them, and your own claimed tasks' handoffs."""
    from ..core import areas
    from ..core.model import knowledge_current, knowledge_kind, knowledge_paths
    server.store.refresh_area("now")
    try:
        text = server.store.paths.now.read_text(encoding="utf-8")
    except OSError:
        text = areas.render_now(server.store)
    wanted = [str(p).strip().lstrip("./") for p in (args.get("paths") or []) if str(p).strip()][:20]
    notes = []
    if wanted:
        def touches(scope: str) -> bool:
            scope = scope.strip().lstrip("./").rstrip("*")
            return any(w.startswith(scope) or scope.startswith(w) for w in wanted if scope)
        for note in server.store.list_knowledge():
            if knowledge_current(note) and any(touches(p) for p in knowledge_paths(note)):
                notes.append(f"### {knowledge_kind(note)}: {note.title} ({note.slug}.md)\n{note.body.strip()}")
        # What the projects above know about these paths (their notes name them from their own folder).
        from ..core import family
        for above in family.ancestors(server.root):
            inner = family.rel(above, server.root)
            outer = family.rel(server.root, above)
            name = family.name_of(above)

            def touches_above(scope: str, inner=inner) -> bool:
                scope = scope.strip().lstrip("./").rstrip("*")
                return bool(scope) and any((inner + w).startswith(scope) or scope.startswith(inner + w) for w in wanted)
            for note in ProjectStore(above).list_knowledge():
                if knowledge_current(note) and any(touches_above(p) for p in knowledge_paths(note)):
                    notes.append(f"### {name} · {knowledge_kind(note)}: {note.title} "
                                 f"({outer}.playground/knowledge/{note.slug}.md)\n{note.body.strip()}")
    mine = [t for t in server.store.list_tasks() if t.is_open and t.claimed_by and
            t.claimed_by.split(":", 1)[-1].split("@", 1)[0] in (server.who, server.actor)]
    handoffs = []
    for task in mine:
        part = task.body.split("## Handoff", 1)
        handoffs.append(f"### {task.id} {task.title}\n" + (("## Handoff" + part[1]).strip() if len(part) > 1
                                                             else "(no handoff yet)"))
    out = text
    if notes:
        out += "\n## What's known about those paths\n\n" + "\n\n".join(notes[:12]) + "\n"
    elif wanted:
        out += "\n## What's known about those paths\n\nNothing yet: if you learn how they work, write a part note.\n"
    if handoffs:
        out += "\n## Your tasks\n\n" + "\n\n".join(handoffs) + "\n"
    return ToolResult(out, {"paths": wanted, "notes": len(notes), "tasks": [t.id for t in mine]})


def subproject_create(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    from ..core import family
    from ..core.paths import safe_project_name
    from ..core.project import ProjectError, adopt_folder
    name = _text(args, "name", required=True, limit=80)
    where = _text(args, "folder", limit=300) or safe_project_name(name)
    folder = (server.root / where).resolve()
    try:
        folder.relative_to(server.root.resolve())
    except ValueError as error:
        raise ToolError(f"{where} isn't inside this project.") from error
    if folder == server.root.resolve():
        raise ToolError("That's this project's own folder: give the part's folder inside it.")
    if family.is_project(folder):
        raise ToolError(f"{where} is already a project ({family.name_of(folder)}).")
    existed = folder.is_dir() and any(folder.iterdir())
    folder.mkdir(parents=True, exist_ok=True)
    try:
        made = adopt_folder(folder, name, summary=_text(args, "summary", limit=300), owner=None)
    except ProjectError as error:
        raise ToolError(str(error)) from error
    where = family.rel(server.root, folder)
    how = "made from the code already there" if existed else "a new folder"
    return ToolResult(f"Made the sub-project {made.name} in `{where}` ({how}). Its own tasks, bugs and knowledge are "
                      f"in `{where}.playground/`; the person can open it from the project list, and agents working "
                      f"there start from `{where}AGENTS.md`. To file something there from here, give "
                      f"project: \"{made.name}\" to the playground tools.",
                      {"name": made.name, "folder": where},
                      activity=f"Made the sub-project {made.name} ({where})", activity_kind="created")


def box_run(server: PlaygroundServer, args: dict, call: Call) -> ToolResult:
    """A command in the project's box (core.box), run by the app. Short ones come straight back; long ones
    (the box's first setup, a big build) come to the agent as its next message."""
    command = _text(args, "command", required=True, limit=4000)
    if re.search(r"\b(rpm-ostree|pkexec|su)\b", command):
        raise ToolError("Those change the computer, not the box. Inside the box, `sudo dnf install -y …` needs no "
                        "password. For something the computer itself needs, use playground_install_ask.")
    if not server.app_is_open():
        raise ToolError("Playground isn't open on this project, so its box can't run anything now.")
    goal_id = server.goal_id(args, required=False)
    request = PendingRequest(id=new_id("box", 16), kind="box", goal=goal_id,
                             params={"command": command, "why": _text(args, "why", limit=300)},
                             asked_by=server.who, asked_at=now_iso())
    server.store.write_pending(request)
    answer = server.wait_for_answer(request.id, call, min(server.decision_wait, server.decision_timeout),
                                    "Running in the project's box")
    if answer is None:
        return ToolResult("Still running in the project's box (the first time, Playground also makes the box, "
                          "which takes a few minutes). Carry on with other work or end your turn: the result "
                          "comes to you as your next message.", {"job": request.id, "state": "running"},
                          logged=True)
    server.store.remove_pending(request.id)
    return ToolResult(answer.text or "Done.", dict(answer.data or {}), logged=True)


def install_ask(server: PlaygroundServer, args: dict, call: Call) -> ToolResult:
    """Ask to install packages on the computer itself (not the box). The person answers on a card; yes runs the
    install with the system's own password dialog. The outcome comes as the agent's next message."""
    names = args.get("packages")
    if not isinstance(names, list) or not names:
        raise ToolError("packages is a list of package names.")
    packages = [str(p).strip() for p in names if re.fullmatch(r"[\w.+:@-]+", str(p).strip())][:20]
    if not packages:
        raise ToolError("Those aren't package names.")
    why = _text(args, "why", required=True, limit=400)
    goal_id = server.thread_goal(args)
    decision_id = new_id("install " + " ".join(packages), 32)
    options = [Option(id="install", label="Install it",
                      consequence="Your computer asks for your password in its own window; Playground never sees it."),
               Option(id="no", label="Not now", consequence="Nothing is installed; the agent finds another way.")]
    pending = PendingDecision(id=decision_id, question=f"Install {', '.join(packages)} on this computer?",
                              options=options, goal=goal_id, recommended="", default="no", context=why,
                              asked_by=server.who, asked_at=now_iso(),
                              extra={"source": "install", "packages": packages})
    server.store.write_pending(pending)
    server.post(goal_id, "decision", {"id": decision_id, "question": pending.question,
                                      "options": [o.to_dict() for o in options], "recommended": "",
                                      "default": "no", "context": why, "source": "install"})
    _mark_needs_you(server, goal_id)
    return ToolResult("Asked on the person's screen. Don't ask again in your reply, and don't install anything "
                      "yourself; end your turn or carry on with other work. What happened comes to you as your "
                      "next message.", {"decision_id": decision_id, "state": "waiting"}, logged=True)


def preview_screenshot(server: PlaygroundServer, args: dict, call: Call) -> ToolResult:
    if not server.app_is_open():
        raise ToolError("Playground isn't open on this project, so there's no preview to capture. "
                        "Run the app yourself (see .playground/run.toml) and check it there.")
    state = read_json(server.store.paths.preview_state) or {}
    # A desktop app (or a screen) is shot as its window; a website, at phone size, unless asked otherwise.
    device = _text(args, "device") or ("app" if state.get("target") in ("desktop-app", "screen") else "phone")
    if device not in ("phone", "tablet", "desktop", "app"):
        raise ToolError("device is phone, tablet, desktop or app.")
    goal_id = server.goal_id(args, required=False)
    request = PendingRequest(id=new_id("screenshot", 16), kind="screenshot", goal=goal_id,
                             params={"path": _text(args, "path") or "/", "device": device,
                                     "environment": _text(args, "environment") or "stage"},
                             asked_by=server.who, asked_at=now_iso())
    server.store.write_pending(request)
    answer = server.wait_for_answer(request.id, call, SCREENSHOT_TIMEOUT_S, "Taking a screenshot of the preview")
    server.store.remove_pending(request.id)
    if answer is None:
        raise ToolError("The preview didn't send a screenshot within a minute. Is it running? "
                        "Check with playground_preview_status.")
    if answer.text and not answer.data.get("path"):
        raise ToolError(answer.text)
    rel = server.project_paths([str(answer.data.get("path", ""))], must_exist=True)[0]
    images = []
    image_path = server.root / rel
    if image_path.stat().st_size <= IMAGE_INLINE_LIMIT:
        mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        images.append((base64.b64encode(image_path.read_bytes()).decode("ascii"), mime))
    return ToolResult(f"Saved the screenshot to {rel}.", {"path": rel, "device": device}, images=images,
                      activity="Took a screenshot of the preview", goal=goal_id if goal_id != GENERAL_CHAT_ID else "")


def checks_run(server: PlaygroundServer, args: dict, call: Call) -> ToolResult:
    goal_id = server.goal_id(args)
    goal = server.store.read_goal(goal_id)
    if not goal.checks:
        raise ToolError(f"{goal.title} has no checks yet. Add them with playground_goal_update (stories with checks).")
    results = _run_checks_in_app(server, goal_id, call) if server.app_is_open() else _run_checks_here(server, goal_id)
    by_text = {_norm(r["text"]): r for r in results if isinstance(r, dict) and r.get("text")}

    def change(g) -> None:
        for check in g.checks:
            result = by_text.get(_norm(check.text))
            if result and result.get("status") in ("pass", "fail", "pending"):
                check.status = result["status"]
                if result.get("evidence"):
                    check.evidence = str(result["evidence"])

    goal = server.store.update_goal(goal_id, change)
    passed = goal.checks_passed
    return ToolResult(f"{passed} of {len(goal.checks)} checks passed.",
                      {"goal_id": goal_id, "checks": [c.to_dict() for c in goal.checks]},
                      activity=f"Ran the checks for {goal.title}: {passed} of {len(goal.checks)} passed",
                      activity_kind="proof", goal=goal_id)


def _run_checks_in_app(server: PlaygroundServer, goal_id: str, call: Call) -> list[dict]:
    request = PendingRequest(id=new_id("checks", 16), kind="checks", goal=goal_id, asked_by=server.who, asked_at=now_iso())
    server.store.write_pending(request)
    answer = server.wait_for_answer(request.id, call, CHECKS_TIMEOUT_S, "Playground's checker is running the checks")
    server.store.remove_pending(request.id)
    if answer is None:
        raise ToolError("The checker didn't finish within 10 minutes. Look at the preview, then try again.")
    checks = answer.data.get("checks")
    if not isinstance(checks, list):
        raise ToolError(answer.text or "The checker couldn't run the checks.")
    return checks


def _run_checks_here(server: PlaygroundServer, goal_id: str) -> list[dict]:
    """Without the app, run the checks with core.checks in this process, if it can."""
    try:
        from ..core import checks as checker
    except ImportError:
        checker = None
    run = getattr(checker, "run_goal_checks", None)
    if run is None:
        raise ToolError("Playground isn't open on this project, so its checker can't run. Check the app yourself, "
                        "then record what you saw with playground_proof_attach.")
    outcome = run(server.root, goal_id)
    checks = outcome.get("checks") if isinstance(outcome, dict) else outcome
    return [c.to_dict() if isinstance(c, Check) else c for c in checks or []]


def proof_attach(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    goal_id = server.goal_id(args)
    shots = server.project_paths(args.get("shots"), must_exist=True)
    recording = server.project_paths([args["recording"]], must_exist=True)[0] if args.get("recording") else ""
    raw_checks = args.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ToolError("List the checks you verified, each with text and status.")
    checks = []
    for item in raw_checks:
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            raise ToolError("Each check needs its text and a status.")
        status = str(item.get("status") or "")
        if status not in ("pass", "fail", "pending"):
            raise ToolError("A check's status is pass, fail or pending.")
        evidence = server.project_paths([item["evidence"]])[0] if item.get("evidence") else None
        checks.append(Check(text=str(item["text"]).strip(), status=status, evidence=evidence))

    def change(goal) -> None:
        known = {_norm(c.text): c for c in goal.checks}
        for check in checks:
            target = known.get(_norm(check.text))
            if target is not None:
                target.status = check.status
                target.evidence = check.evidence or target.evidence

    goal = server.store.update_goal(goal_id, change)
    data: dict[str, Any] = {"shots": shots, "checks": [c.to_dict() for c in checks]}
    if recording:
        data["recording"] = recording
    if args.get("summary"):
        data["summary"] = _text(args, "summary")
    server.post(goal_id, "proof", data)
    passed = sum(1 for c in checks if c.status == "pass")
    return ToolResult(f"Attached the proof: {passed} of {len(checks)} checks passed.", data,
                      activity=f"Attached proof to {goal.title}: {passed} of {len(checks)} checks passed",
                      activity_kind="proof", goal=goal_id)


def release_propose(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    goal_id = server.goal_id(args)
    goal = server.store.read_goal(goal_id)
    changes = _strings(args, "changes_for_people")
    if not changes:
        raise ToolError("Say what changes for people, in their words (changes_for_people).")
    project = server.store.read_project()
    destination = _text(args, "destination")
    if destination:
        if project.place(destination) is None:
            raise ToolError(f"There's no place called {destination}. playground_places_list shows them.")
    else:
        production = next((p for p in project.places if p.role == "production"), None)
        destination = production.id if production else ""
    open_decisions = [p for p in server.store.list_pending("decision") if p.goal == goal_id]
    passed, total = goal.checks_passed, len(goal.checks)
    ready = total > 0 and passed == total and not open_decisions
    data = {"title": goal.title, "changes_for_people": changes, "data_changes": _strings(args, "data_changes"),
            "checks": {"passed": passed, "total": total}, "destination": destination, "ready": ready,
            "open_decisions": [p.question for p in open_decisions]}
    server.post(goal_id, "review", data)
    if ready and goal.status in (GoalStatus.WORKING, GoalStatus.NEEDS_YOU):
        server.store.update_goal(goal_id, lambda g: (setattr(g, "status", GoalStatus.READY), setattr(g, "status_note", "")))
    if ready:
        message = "Proposed the release. A person reviews it and releases it; you can't release it yourself."
    elif open_decisions:
        message = "Posted the review, but it can't be released until the open decision is answered."
    else:
        message = f"Posted the review, but only {passed} of {total} checks pass, so it can't be released yet."
    return ToolResult(message, data, activity=f"Proposed releasing {goal.title}", activity_kind="review", goal=goal_id)


def places_list(server: PlaygroundServer, _args: dict, _call: Call) -> ToolResult:
    project = server.store.read_project()
    places = [{**p.to_dict(), "is_home": p.id == project.home} for p in project.places]
    return ToolResult(f"{len(places)} place{'s' if len(places) != 1 else ''}. Agents never write to production.", places)


def cost_report(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    goal_id = server.thread_goal(args)
    text = _text(args, "text", required=True, limit=300)
    data: dict[str, Any] = {"text": text}
    if isinstance(args.get("amount_usd"), (int, float)):
        data["amount_usd"] = float(args["amount_usd"])
    server.post(goal_id, "cost", data)
    return ToolResult("Posted the cost note.", data, activity=f"Cost: {text}", activity_kind="cost", goal=goal_id)


# ── The Project space: stack, bugs, usage and analytics ──

SERVICE_FIELDS = ("name", "kind", "provider", "purpose", "console_url", "docs_url", "plan", "usage_source")


def _service_changes(args: dict) -> dict:
    """The service fields an agent passed, checked."""
    changes: dict[str, Any] = {}
    for key in SERVICE_FIELDS:
        if args.get(key) is not None:
            changes[key] = _text(args, key, limit=400)
    if changes.get("kind") and changes["kind"] not in SERVICE_KINDS:
        raise ToolError(f"kind is one of: {', '.join(SERVICE_KINDS)}.")
    for key in ("used_by", "secrets"):
        if args.get(key) is not None:
            changes[key] = _strings(args, key)
    if args.get("environments") is not None:
        environments = args["environments"]
        if not isinstance(environments, dict) or not all(isinstance(v, str) for v in environments.values()):
            raise ToolError("environments is an object like {\"stage\": \"...\", \"live\": \"...\"}.")
        changes["environments"] = {str(k): v.strip() for k, v in environments.items() if v.strip()}
    if args.get("monthly_budget") is not None:
        if not isinstance(args["monthly_budget"], (int, float)) or args["monthly_budget"] < 0:
            raise ToolError("monthly_budget is a number of dollars, like 5.")
        changes["monthly_budget"] = float(args["monthly_budget"])
    return changes


def _service_view(server: PlaygroundServer, service) -> dict:
    data = service.to_dict()
    latest = server.store.read_service_usage(service.id, limit=1)
    if latest:
        data["latest_usage"] = latest[0].to_dict()
    return data


def stack_list(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    notes = args.get("dependency_notes") or {}
    if not isinstance(notes, dict):
        raise ToolError("dependency_notes is an object of {package name: one-line why}.")
    if args.get("refresh_dependencies") or notes:
        server.store.refresh_dependencies({str(k): str(v) for k, v in notes.items()})
    stack = server.store.read_stack()
    data = {"services": [_service_view(server, s) for s in stack.services],
            "dependencies": [d.to_dict() for d in stack.dependencies]}
    return ToolResult(f"{len(stack.services)} services and {len(stack.dependencies)} packages.", data)


def stack_add_service(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    changes = _service_changes(args)
    if not changes.get("name") or not changes.get("purpose"):
        raise ToolError("A service needs a name and a purpose in plain words.")
    service_id = _text(args, "id") or slugify(changes["name"], 40)
    if not is_valid_id(service_id):
        raise ToolError("id may use lowercase letters, numbers and hyphens only.")
    service = Service(id=service_id, name=changes["name"], added_by=server.who)
    for key, value in changes.items():
        setattr(service, key, value)
    service.provider = service.provider or service.name
    server.store.add_service(service)
    return ToolResult(f"Added {service.name} to the stack. Put its keys in the project vault, never in the folder.",
                      service.to_dict(), activity=f"Added {service.name} to the stack: {service.purpose}",
                      activity_kind="stack")


def stack_update_service(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    service_id = _text(args, "service_id", required=True)
    changes = _service_changes(args)
    if not changes:
        raise ToolError("Nothing to change. Pass at least one field.")
    def change(service) -> None:
        for key, value in changes.items():
            setattr(service, key, value)

    try:
        service = server.store.update_service(service_id, change)
    except NotFound:
        raise ToolError(f"There's no service called {service_id}. playground_stack_list shows them.") from None
    return ToolResult(f"Updated {service.name}.", service.to_dict(),
                      activity=f"Updated {service.name} in the stack: {', '.join(k.replace('_', ' ') for k in changes)}",
                      activity_kind="stack")


def stack_remove_service(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    service_id = _text(args, "service_id", required=True)
    why = _text(args, "why", required=True)
    try:
        service = server.store.remove_service(service_id)
    except NotFound:
        raise ToolError(f"There's no service called {service_id}. playground_stack_list shows them.") from None
    return ToolResult(f"Removed {service.name} from the stack. Its usage history is kept.", service.to_dict(),
                      activity=f"Removed {service.name} from the stack: {why}", activity_kind="stack")


def _issue_text(args: dict, key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
        return "\n".join(f"{i}. {item}" if key == "steps" else f"- {item}" for i, item in enumerate(items, 1))
    return _text(args, key, limit=8000)


def _issue_choice(args: dict, key: str, allowed: tuple) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if value not in allowed:
        raise ToolError(f"{key} is one of: {', '.join(allowed)}.")
    return value


def task_list(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    tasks = server.store.list_tasks(include_closed=bool(args.get("include_done")))
    rows = [{k: v for k, v in t.to_dict().items() if k != "body" and v not in ("", [])} for t in tasks]
    for row, task in zip(rows, tasks):
        row["claimed_by"] = task.claim_holder()
    return ToolResult(f"{len(rows)} task{'s' if len(rows) != 1 else ''}.", rows)


def task_create(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    title = _text(args, "title", required=True, limit=160)
    priority = _text(args, "priority") or "normal"
    if priority not in PRIORITIES:
        raise ToolError(f"priority is one of {', '.join(PRIORITIES)}.")
    body = _text(args, "description", limit=8000) or ""
    checks = [c for c in (args.get("checks") or []) if isinstance(c, str) and c.strip()]
    if checks:
        body = (body + "\n\n" if body else "") + "## Checks\n" + "".join(f"- [ ] {c.strip()}\n" for c in checks)
    task = server.store.create_task(title, by=server.who, priority=priority, body=body,
                                    labels=[str(x) for x in args.get("labels") or []])
    return ToolResult(f"Added task {task.id}: {task.title}.", task.to_dict(),
                      activity=f"Added a task: {task.title}", activity_kind="task")


def task_update(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    task_id = _text(args, "id", required=True)
    if not is_valid_id(task_id):
        raise ToolError("id is a task's id, like t-7f3k2.")
    action = _text(args, "action")
    try:
        if action == "claim":
            task = server.store.claim_task(task_id, server.who)
            return ToolResult(f"{task_id} is yours until {task.claimed_until}.", task.to_dict())
        if action == "release":
            task = server.store.release_task(task_id, by=server.who)
            return ToolResult(f"Let go of {task_id}.", task.to_dict())
    except NotFound as error:
        raise ToolError(str(error)) from error
    except StoreError as error:
        raise ToolError(str(error)) from error
    status = _text(args, "status")
    if status and status not in TASK_STATUSES:
        raise ToolError(f"status is one of {', '.join(TASK_STATUSES)}.")
    outcome = _text(args, "outcome", limit=1000)

    def change(task) -> None:
        if status:
            task.status = status
        for key in ("title", "priority", "assignee"):
            value = _text(args, key)
            if value:
                setattr(task, key, value)
        if outcome:
            task.body = (task.body.rstrip() + "\n\n" if task.body.strip() else "") + f"## Outcome\n{outcome}"
    try:
        task = server.store.update_task(task_id, change, by=server.who)
    except (NotFound, StoreError, ValueError) as error:
        raise ToolError(str(error)) from error
    return ToolResult(f"Updated {task_id}.", task.to_dict(),
                      activity=f"Finished a task: {task.title}" if status == "done" else "", activity_kind="task")


def channel_post(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    try:
        entry = server.store.post_channel(_text(args, "text", required=True, limit=400), by=server.who,
                                          kind=_text(args, "kind") or "note", task=_text(args, "task") or "",
                                          to=_text(args, "to") or "",
                                          files=[str(f) for f in args.get("files") or []][:10])
    except ValueError as error:
        raise ToolError(str(error)) from error
    return ToolResult("Posted to the team channel.", entry)


def channel_read(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    limit = args.get("limit", 25)
    limit = max(1, min(int(limit), 200)) if isinstance(limit, (int, float)) else 25
    entries = server.store.read_channel(limit)
    return ToolResult(f"The last {len(entries)} posts, oldest first.", entries)


def issue_create(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    title = _text(args, "title", required=True, limit=160)
    goal = server.goal_id(args, required=False) if args.get("goal_id") else ""
    issue = server.store.create_issue(
        title, severity=_issue_choice(args, "severity", ISSUE_SEVERITIES) or "medium",
        place=_issue_choice(args, "place", ISSUE_PLACES) or "stage", reporter=_text(args, "reporter") or server.who,
        goal=goal, what_happened=_issue_text(args, "what_happened") or "", expected=_issue_text(args, "expected") or "",
        steps=_issue_text(args, "steps") or "", evidence=_issue_text(args, "evidence") or "")
    return ToolResult(f"Filed bug #{issue.number}: {issue.title}.", issue.to_dict(),
                      activity=f"Filed bug #{issue.number}: {issue.title}", activity_kind="issue", goal=goal)


def issue_update(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    number = args.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ToolError("number is the bug's number, like 13.")
    status = _issue_choice(args, "status", ISSUE_STATES)
    severity = _issue_choice(args, "severity", ISSUE_SEVERITIES)
    goal = server.goal_id(args, required=False) if args.get("goal_id") else None
    texts = {key: _issue_text(args, key) for key in ("title", "what_happened", "expected", "steps", "evidence")}
    note = _text(args, "note")

    def change(issue) -> None:
        if status:
            issue.status = status
        if severity:
            issue.severity = severity
        if goal is not None:
            issue.goal = goal
        for key, value in texts.items():
            if value:
                setattr(issue, key, value)
        if note:
            for index, (heading, text) in enumerate(issue.sections):
                if heading.lower() == "notes":
                    issue.sections[index] = (heading, f"{text}\n\n{note}".strip())
                    break
            else:
                issue.sections.append(("Notes", note))

    try:
        issue = server.store.update_issue(number, change)
    except NotFound:
        raise ToolError(f"There's no bug #{number}. playground_issue_list shows them.") from None
    summary = f"Bug #{number} is now {issue.status.replace('-', ' ')}" if status else f"Updated bug #{number}"
    return ToolResult(summary + ".", issue.to_dict(), activity=f"{summary}: {issue.title}", activity_kind="issue",
                      goal=issue.goal)


def issue_list(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    status = _issue_choice(args, "status", ISSUE_STATES)
    issues = server.store.list_issues(status=status, include_closed=bool(args.get("include_closed")) or status is not None)
    return ToolResult(f"{len(issues)} bug{'s' if len(issues) != 1 else ''}.", [i.to_dict() for i in issues])


def usage_record(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    service_id = _text(args, "service_id", required=True)
    if server.store.read_stack().service(service_id) is None:
        raise ToolError(f"There's no service called {service_id}. Add it with playground_stack_add_service first.")
    metrics = args.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        raise ToolError("metrics is an object of numbers, like {\"emails_sent\": 412}.")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in metrics.values()):
        raise ToolError("Usage numbers only: every metric must be a number.")
    cost = args.get("cost")
    if cost is not None and (not isinstance(cost, (int, float)) or isinstance(cost, bool)):
        raise ToolError("cost is a number, like 0.84.")
    usage = server.store.append_service_usage(ServiceUsage(
        service_id=service_id, period=_text(args, "period", required=True, limit=20), metrics=dict(metrics),
        cost=float(cost) if cost is not None else None, currency=_text(args, "currency") or "USD",
        source=_text(args, "source") or f"agent:{server.who}"))
    return ToolResult("Recorded the usage.", usage.to_dict(),
                      activity=f"Recorded usage for {service_id} ({usage.period})", activity_kind="usage")


def analytics_record(server: PlaygroundServer, args: dict, _call: Call) -> ToolResult:
    numbers = {}
    for key in ("visitors", "sessions", "errors"):
        value = args.get(key, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ToolError(f"{key} is a whole number.")
        numbers[key] = value
    pages = args.get("top_pages") or []
    if not isinstance(pages, list) or not all(isinstance(p, dict) and isinstance(p.get("path"), str)
                                              and isinstance(p.get("views"), int) for p in pages):
        raise ToolError("top_pages is a list like [{\"path\": \"/\", \"views\": 120}].")
    snapshot = server.store.append_analytics(AnalyticsSnapshot(
        period=_text(args, "period", required=True, limit=20), top_pages=[{"path": p["path"], "views": p["views"]} for p in pages],
        source=_text(args, "source") or f"agent:{server.who}", **numbers))
    return ToolResult("Recorded the analytics.", snapshot.to_dict(),
                      activity=f"Recorded analytics for {snapshot.period}: {snapshot.visitors} visitors",
                      activity_kind="analytics")


TOOLS: list[Tool] = [
    Tool("playground_project_info", "About this project",
         "The project's name, folder, recipe, places, people, goal counts and how to run it. Call it when you start.",
         _schema(), project_info, read_only=True),
    Tool("playground_goal_list", "List goals",
         "Every goal in the project with its status (working, needs-you, ready, live, paused, done) and progress.",
         _schema({"status": {**STATUS, "description": "Only goals with this status."},
                  "include_done": {"type": "boolean", "description": "Include live and done goals. Defaults to true."}}),
         goal_list, read_only=True),
    Tool("playground_goal_get", "Read a goal",
         "One goal: its outcome, stories, checks with their status, notes, the recent conversation, and any "
         "decision still waiting. Read it before you start work on a goal.",
         _schema({"goal_id": GOAL_ID, "thread_entries": {"type": "integer", "minimum": 0, "maximum": 200,
                                                         "description": "How many recent conversation entries to include (default 20)."}}),
         goal_get, read_only=True),
    Tool("playground_goal_create", "Start a goal",
         "Start a new goal: one outcome the person asked for, bigger than a quick fix. Give a short plan as "
         "stories in the person's words, each with checks a person could see. It appears in Playground's rail.",
         _schema({"title": {"type": "string", "description": "Short and plain: \"Add a waitlist\"."},
                  "outcome": {"type": "string", "description": "One or two sentences on what's true when it's finished."},
                  "stories": {"type": "array", "items": STORY},
                  "size": {"type": "string", "enum": list(SIZES)},
                  "cost_estimate": {"type": "string", "description": "In plain words: \"About $0.60 of Ari credits\"."},
                  "status": {**STATUS, "description": "Usually working (the default)."},
                  "owner": {"type": "string", "description": "The member id it belongs to. Defaults to the project owner."}},
                 ["title"]),
         goal_create),
    Tool("playground_goal_update", "Update a goal",
         "Change a goal as work moves: its status, the plan (stories and checks), which stories are done, check "
         "results, or a note. You can't set a goal live; only a reviewed release does that.",
         _schema({"goal_id": GOAL_ID, "status": STATUS,
                  "status_note": {"type": "string", "description": "A few words shown after the status: \"after the holidays\"."},
                  "title": {"type": "string"}, "outcome": {"type": "string"},
                  "size": {"type": "string", "enum": list(SIZES)}, "cost_estimate": {"type": "string"},
                  "stories": {"type": "array", "items": STORY, "description": "Replaces the plan. Check results are kept for checks with the same text."},
                  "post_plan": {"type": "boolean", "description": "Show the new plan as a card (default true when stories change)."},
                  "story_done": {"type": "array", "items": {"type": "object", "properties": {
                      "story": {"type": ["string", "integer"], "description": "The story's number (from 1) or exact title."},
                      "done": {"type": "boolean"}}, "required": ["story"]}},
                  "check_results": {"type": "array", "items": CHECK_RESULT},
                  "note": {"type": "string", "description": "Added to the goal's notes."}}),
         goal_update),
    Tool("playground_step_report", "Report a step",
         "Record a piece of work as a step card: what you did, in a short plain sentence, and the files you touched. "
         "Use state running for long work you'll finish later, then report it again as done.",
         _schema({"goal_id": GOAL_ID, "title": {"type": "string", "description": "\"Added the Join waitlist button\"."},
                  "state": {"type": "string", "enum": ["running", "done", "failed"]}, "files": PATHS,
                  "elapsed_s": {"type": "number"}, "spend_text": {"type": "string", "description": "\"$0.31\" or \"4% of your Claude week\"."},
                  "details": {"type": "string"}}, ["title"]),
         step_report),
    Tool("playground_headsup_post", "Post a heads-up",
         "Tell the person you changed something they didn't ask for, and how to undo it. Keep such changes small.",
         _schema({"goal_id": GOAL_ID, "text": {"type": "string", "description": "What you changed and why, in one or two sentences."},
                  "files": PATHS,
                  "undo": {"type": "object", "properties": {
                      "kind": {"type": "string", "description": "\"git\" (ref is a commit) or \"file\" (ref is a backup path)."},
                      "ref": {"type": "string"}}, "required": ["kind"]}},
                 ["text"]),
         headsup_post),
    Tool("playground_decision_ask", "Ask the person to decide",
         "Ask the person a question only they can answer: what the app does, what people see, cost, data or money. "
         "Give two to four options, each with its consequence in plain words, and say which you recommend. It "
         "shows as a card in Playground and waits a little for their answer. If they haven't answered yet, the "
         "question stays open and their answer comes to you as your next message: don't ask it again in text. "
         "Decide technical details yourself instead.",
         _schema({"question": {"type": "string", "description": "\"How should someone hear that a spot opened?\""},
                  "options": {"type": "array", "minItems": 2, "maxItems": 4, "items": {"type": "object", "properties": {
                      "id": {"type": "string", "description": "A short id like \"text\"."},
                      "label": {"type": "string", "description": "\"A text message\"."},
                      "consequence": {"type": "string", "description": "What it means for people, cost or time."}},
                      "required": ["label"]}},
                  "recommended": {"type": "string", "description": "The id or label of the option you'd pick."},
                  "default": {"type": "string", "description": "What you'll do if they say \"you pick\"."},
                  "context": {"type": "string", "description": "Anything they need to know first."},
                  "goal_id": GOAL_ID,
                  "wait": {"type": "boolean", "description": "Wait for the answer (default true). With false, poll playground_decision_get."}},
                 ["question", "options"]),
         decision_ask),
    Tool("playground_decision_get", "Check a decision",
         "Whether the person has answered a decision you asked with wait set to false, and what they chose.",
         _schema({"decision_id": {"type": "string"}}, ["decision_id"]), decision_get, read_only=True),
    Tool("playground_decision_record", "Record a decision",
         "Add something that was settled (in conversation, or by you on a technical point worth remembering) to "
         "the project's decisions, with who decided and why.",
         _schema({"title": {"type": "string", "description": "\"Classes cap at eight\"."},
                  "chosen": {"type": "string"}, "why": {"type": "string"},
                  "who": {"type": "string", "description": "Who decided. Defaults to you."}, "goal_id": GOAL_ID},
                 ["title", "chosen"]),
         decision_record),
    Tool("playground_activity_log", "Add to the history",
         "Add one line to the project's history, for anything worth knowing later that isn't a step, decision or proof.",
         _schema({"summary": {"type": "string", "description": "One plain sentence: \"Fixed how phone numbers are saved\"."},
                  "kind": {"type": "string", "description": "agent, note, release, undo… Defaults to agent."},
                  "goal_id": GOAL_ID, "files": PATHS, "data": {"type": "object"}}, ["summary"]),
         activity_log),
    Tool("playground_activity_list", "Read the history",
         "What happened recently in the project, oldest first. Use it to catch up when you pick up work.",
         _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 200},
                  "goal_id": {"type": "string", "description": "Only this goal's history."},
                  "include_tool_calls": {"type": "boolean"}}),
         activity_list, read_only=True),
    Tool("playground_knowledge_add", "Save a note",
         "Save something the next agent or a teammate should know: a convention, a gotcha, research, how the "
         "studio works. Writing to an existing slug replaces it (or appends, with mode append).",
         _schema({"title": {"type": "string"}, "body": {"type": "string", "description": "Markdown."},
                  "kind": {"type": "string", "enum": list(KNOWLEDGE_KINDS),
                           "description": "decision, rule (always applies; waits for a person to keep it), part "
                                          "(how one part of the code works), lesson, mistake, purpose or note."},
                  "paths": {"type": "array", "items": {"type": "string"},
                            "description": "For a part: where it lives, like src/auth/."},
                  "uses": {"type": "array", "items": {"type": "string"},
                           "description": "For a part: what it relies on, like postgres or resend."},
                  "slug": {"type": "string", "description": "A short name like phone-numbers. Made from the title if left out."},
                  "mode": {"type": "string", "enum": ["replace", "append"]}}, ["title", "body"]),
         knowledge_add),
    Tool("playground_knowledge_search", "Search notes",
         "Search the project's notes. With no query, lists them all.",
         _schema({"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
         knowledge_search, read_only=True),
    Tool("playground_knowledge_get", "Read a note", "One note in full.",
         _schema({"slug": {"type": "string"}}, ["slug"]), knowledge_get, read_only=True),
    Tool("playground_brief_get", "Read the brief",
         "The one-page brief: what the project is, who it's for, where it runs and what good looks like.",
         _schema(), brief_get, read_only=True),
    Tool("playground_brief_update", "Update the brief",
         "Change the brief when a decision changes what the project is. Shows the new brief as a card.",
         _schema({"title": {"type": "string"}, "audience": {"type": "string"},
                  "platforms": {"type": "array", "items": {"type": "string"}},
                  "summary": {"type": "string", "description": "What it is, in a sentence or two."},
                  "good_looks_like": {"type": "array", "items": {"type": "string"}},
                  "not_now": {"type": "string"}, "goal_id": GOAL_ID}),
         brief_update),
    Tool("playground_preview_status", "Preview status",
         "Whether the app is running in Playground's preview, at which address, and how to run it. Playground "
         "restarts the preview by itself when the app's files change: never ask the person to restart it.",
         _schema(), preview_status, read_only=True),
    Tool("playground_now", "Where things stand",
         "Start here: what's underway and by whom, what's waiting, what's ready next, open bugs, what happened "
         "lately, what was decided, how to run and check it. Give the paths you're about to change to get "
         "every note about them too (how those parts work, their rules, lessons and past mistakes).",
         _schema({"paths": {"type": "array", "items": {"type": "string"},
                            "description": "Files or folders you'll change, like src/waitlist.js or server/"}}),
         now_tool, read_only=True),
    Tool("playground_box_run", "Run in the project's box",
         "Run a shell command in the project's box: a Fedora container that sees only the project folder (same "
         "path), where you build and install what building needs without touching the computer. You're root "
         "there: `dnf install -y …` needs no password. Use it for compilers, -devel packages and toolchains. "
         "Short commands return their output; long ones (the box's first setup) come back as your next message.",
         _schema({"command": {"type": "string", "description": "Like: sudo dnf install -y rust cargo gtk4-devel"},
                  "why": {"type": "string"}, "goal_id": GOAL_ID}, ["command"]),
         box_run),
    Tool("playground_install_ask", "Ask to install on the computer",
         "Only for what must be on the computer itself, not the box (a runtime library the finished app needs "
         "here). Shows the person a card; if they say yes, Playground installs it with the system's own password "
         "dialog. Never run sudo, dnf or rpm-ostree on the computer yourself, and never ask for a password.",
         _schema({"packages": {"type": "array", "items": {"type": "string"}},
                  "why": {"type": "string", "description": "In plain words, why the computer needs it."},
                  "goal_id": GOAL_ID}, ["packages", "why"]),
         install_ask),
    Tool("playground_preview_screenshot", "Screenshot the preview",
         "Ask Playground for a screenshot of the running app on a phone, tablet or desktop frame. Returns the "
         "image and its path inside the project, to use as proof.",
         _schema({"path": {"type": "string", "description": "The page to show, like / or /maya."},
                  "device": {"type": "string", "enum": ["phone", "tablet", "desktop", "app"]},
                  "environment": {"type": "string", "enum": ["stage", "live"]}, "goal_id": GOAL_ID}),
         preview_screenshot),
    Tool("playground_checks_run", "Run the checks",
         "Have Playground's checker, which is separate from you, run the goal's checks against the preview. "
         "Updates each check's status and returns the results.",
         _schema({"goal_id": GOAL_ID}), checks_run),
    Tool("playground_proof_attach", "Attach proof",
         "Attach proof that the work does what the plan says: screenshots or a recording from the preview, and "
         "each check with pass or fail. Never mark a check pass unless you saw it work.",
         _schema({"goal_id": GOAL_ID, "shots": PATHS,
                  "recording": {"type": "string", "description": "A path inside the project."},
                  "checks": {"type": "array", "items": CHECK_RESULT}, "summary": {"type": "string"}}, ["checks"]),
         proof_attach),
    Tool("playground_release_propose", "Propose a release",
         "When the goal's checks pass, propose releasing it. This creates a review card for the person; it never "
         "touches production. Say what changes for people and what changes in the data.",
         _schema({"goal_id": GOAL_ID,
                  "changes_for_people": {"type": "array", "items": {"type": "string"},
                                         "description": "\"When a class is full, its button says Join waitlist.\""},
                  "data_changes": {"type": "array", "items": {"type": "string"},
                                   "description": "\"Adds a waitlist table. Nothing is removed.\""},
                  "destination": {"type": "string", "description": "A place id. Defaults to the production place."}},
                 ["changes_for_people"]),
         release_propose),
    Tool("playground_places_list", "List places",
         "Where the project lives and runs: this computer, servers and preview targets, with their roles. "
         "No secrets. Agents never write to production.",
         _schema(), places_list, read_only=True),
    Tool("playground_cost_report", "Note a cost",
         "Tell the person about spend when it matters: \"This goal will take about 6% of your Claude week\".",
         _schema({"text": {"type": "string"}, "amount_usd": {"type": "number"}, "goal_id": GOAL_ID}, ["text"]),
         cost_report),
    Tool("playground_stack_list", "List the stack",
         "The outside services the project uses (what each is for, its plan, budget, consoles, the names of its "
         "secrets, latest usage) and the packages it depends on. Set refresh_dependencies to re-read package.json "
         "and requirements files, and pass dependency_notes to say in one line why a package is there.",
         _schema({"refresh_dependencies": {"type": "boolean"},
                  "dependency_notes": {"type": "object", "additionalProperties": {"type": "string"},
                                       "description": "{\"pg\": \"Talks to the PostgreSQL database\"}"}}),
         stack_list),
    Tool("playground_stack_add_service", "Add a service",
         "Record an outside service as soon as you add it to the project: email, database, storage, hosting, "
         "sign-in, payments and so on. Say what it's for in plain words. Give the NAMES of its secrets only; the "
         "values go in the project vault, never in a file or a tool call.",
         _schema({**SERVICE_SCHEMA, "id": {"type": "string", "description": "A short id like resend. Made from the name if left out."}},
                 ["name", "kind", "purpose"]),
         stack_add_service),
    Tool("playground_stack_update_service", "Update a service",
         "Change a service's details: its plan, budget, environments, consoles or secret names.",
         _schema({"service_id": {"type": "string"}, **SERVICE_SCHEMA}, ["service_id"]), stack_update_service),
    Tool("playground_stack_remove_service", "Remove a service",
         "Take a service out of the stack when the project stops using it, and say why.",
         _schema({"service_id": {"type": "string"}, "why": {"type": "string"}}, ["service_id", "why"]),
         stack_remove_service),
    Tool("playground_issue_create", "File a bug",
         "File a bug you found, or one the person told you about, even if you can't fix it now. Say what happened, "
         "what should have happened, and how to make it happen again.",
         _schema({"title": {"type": "string", "description": "\"Two reminder texts failed\"."},
                  "severity": {"type": "string", "enum": list(ISSUE_SEVERITIES)},
                  "place": {"type": "string", "enum": list(ISSUE_PLACES), "description": "Where it happened: stage is the preview, live the live app."},
                  "what_happened": {"type": "string"}, "expected": {"type": "string"},
                  "steps": {"type": ["string", "array"], "items": {"type": "string"}},
                  "evidence": {"type": ["string", "array"], "items": {"type": "string"},
                               "description": "Error messages, log lines, or screenshots inside the project."},
                  "reporter": {"type": "string", "description": "Who noticed it. Defaults to you."},
                  "goal_id": {"type": "string", "description": "The goal that fixes it, if there is one."}},
                 ["title"]),
         issue_create),
    Tool("playground_issue_update", "Update a bug",
         "Change a bug's status as you work on it (open, fixing, fixed, wont-fix), link it to the goal that fixes "
         "it, or add a note. Mark it fixed only once you've seen the fix work.",
         _schema({"number": {"type": "integer", "minimum": 1}, "status": {"type": "string", "enum": list(ISSUE_STATES)},
                  "severity": {"type": "string", "enum": list(ISSUE_SEVERITIES)}, "title": {"type": "string"},
                  "what_happened": {"type": "string"}, "expected": {"type": "string"},
                  "steps": {"type": ["string", "array"], "items": {"type": "string"}},
                  "evidence": {"type": ["string", "array"], "items": {"type": "string"}},
                  "goal_id": {"type": "string"}, "note": {"type": "string"}}, ["number"]),
         issue_update),
    Tool("playground_issue_list", "List bugs",
         "The project's bugs, newest first. Open and fixing ones by default.",
         _schema({"status": {"type": "string", "enum": list(ISSUE_STATES)}, "include_closed": {"type": "boolean"}}),
         issue_list, read_only=True),
    Tool("playground_task_list", "List tasks",
         "Open tasks, with who has claimed each. Read before starting work so you don't take someone else's.",
         _schema({"include_done": {"type": "boolean"}}), task_list, read_only=True),
    Tool("playground_subproject_create", "Make a sub-project",
         "A part big enough for its own focus (an app in an OS): its own tasks, bugs, knowledge and "
         "conversations, in a folder inside this project (one holding its code stays as it is). Ask first.",
         _schema({"name": {"type": "string", "description": "\"Calculator\"."},
                  "folder": {"type": "string", "description": "Inside this project: \"src/apps/calculator\". "
                             "Default: a folder named after it."},
                  "summary": {"type": "string", "description": "One sentence: what it is."}}, ["name"]),
         subproject_create),
    Tool("playground_task_create", "Add a task",
         "Add a piece of work the person asked for, or that you found needs doing.",
         _schema({"title": {"type": "string"}, "description": {"type": "string"},
                  "priority": {"type": "string", "enum": list(PRIORITIES)},
                  "checks": {"type": "array", "items": {"type": "string"},
                             "description": "What a person could see to know it works."},
                  "labels": {"type": "array", "items": {"type": "string"}}}, ["title"]),
         task_create),
    Tool("playground_task_update", "Update a task",
         "action claim before you work on a task (refused while someone else holds it), release to let go. "
         "Or change status (done needs an outcome), title, priority or assignee.",
         _schema({"id": {"type": "string"}, "action": {"type": "string", "enum": ["claim", "release"]},
                  "status": {"type": "string", "enum": list(TASK_STATUSES)}, "outcome": {"type": "string"},
                  "title": {"type": "string"}, "priority": {"type": "string", "enum": list(PRIORITIES)},
                  "assignee": {"type": "string"}}, ["id"]),
         task_update),
    Tool("playground_channel_post", "Post to the team channel",
         "Tell the other agents and people what you're starting (and which files), what you finished, or a "
         "heads-up they need. One or two short sentences.",
         _schema({"text": {"type": "string"}, "kind": {"type": "string", "enum": list(CHANNEL_KINDS)},
                  "task": {"type": "string"}, "to": {"type": "string"},
                  "files": {"type": "array", "items": {"type": "string"}}}, ["text"]),
         channel_post),
    Tool("playground_channel_read", "Read the team channel",
         "The latest posts, oldest first. .playground/channel.md has the same as a short digest.",
         _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 200}}), channel_read, read_only=True),
    Tool("playground_usage_record", "Record service usage",
         "Record usage numbers for a service when you have them (from its dashboard, CLI or API): emails sent, "
         "GB stored, texts sent, and the cost. Numbers only, never keys.",
         _schema({"service_id": {"type": "string"},
                  "period": {"type": "string", "description": "A day (2026-10-08), a week (2026-W41) or a month (2026-10)."},
                  "metrics": {"type": "object", "additionalProperties": {"type": "number"},
                              "description": "{\"emails_sent\": 412, \"bounced\": 2}"},
                  "cost": {"type": "number"}, "currency": {"type": "string"}, "source": {"type": "string"}},
                 ["service_id", "period", "metrics"]),
         usage_record),
    Tool("playground_analytics_record", "Record analytics",
         "Record how the live app was used over a period, when you have the numbers: visitors, sessions, the "
         "most viewed pages and errors.",
         _schema({"period": {"type": "string", "description": "A day (2026-10-08), a week (2026-W41) or a month (2026-10)."},
                  "visitors": {"type": "integer", "minimum": 0}, "sessions": {"type": "integer", "minimum": 0},
                  "top_pages": {"type": "array", "items": {"type": "object", "properties": {
                      "path": {"type": "string"}, "views": {"type": "integer"}}, "required": ["path", "views"]}},
                  "errors": {"type": "integer", "minimum": 0}, "source": {"type": "string"}}, ["period"]),
         analytics_record),
]


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="playground-mcp", description="Playground's MCP server, over stdio.")
    parser.add_argument("--project", default=os.environ.get("PLAYGROUND_PROJECT", ""),
                        help="the project folder (default: $PLAYGROUND_PROJECT, else the current folder)")
    parser.add_argument("--actor", default="", help="who the agent is, e.g. a connection id (or $PLAYGROUND_ACTOR)")
    parser.add_argument("--goal", default="", help="the goal the agent works on (or $PLAYGROUND_GOAL)")
    options = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="playground-mcp: %(message)s")
    root = Path(options.project or os.getcwd()).expanduser().absolute()
    if not is_project(root):
        print(f"playground-mcp: {root} isn't a Playground project (no .playground/project.toml).", file=sys.stderr)
        return 2
    try:
        keepalive = float(os.environ.get("PLAYGROUND_MCP_KEEPALIVE", KEEPALIVE_S))
    except ValueError:
        keepalive = KEEPALIVE_S
    return PlaygroundServer(root, actor=options.actor, goal=options.goal, keepalive=keepalive).serve()


# What an agent is shown (tools/list). Every tool's schema sits in the agent's context on every turn, so the
# list is only what needs Playground itself: asking the person, proof, release, the preview, the channel, and
# the writes whose rules files can't keep (task ids and claims, bug numbers, a rule an agent suggests).
# Goals, the brief, the stack, knowledge and history are files the agent reads and edits (AGENTS.md says
# where). The rest still answer when called by name, for older instructions; PLAYGROUND_MCP_ALL=1 lists all.
LISTED = (
    "playground_now", "playground_step_report", "playground_decision_ask", "playground_decision_get", "playground_checks_run",
    "playground_proof_attach", "playground_release_propose", "playground_preview_status",
    "playground_preview_screenshot", "playground_headsup_post", "playground_channel_post", "playground_task_create",
    "playground_task_update", "playground_knowledge_add", "playground_issue_create", "playground_box_run",
    "playground_install_ask", "playground_subproject_create",
)

# Tools that can act on another project of the family: `project` names it (core.family.resolve).
FAMILY_TOOLS = ("playground_now", "playground_task_create", "playground_task_update", "playground_knowledge_add",
                "playground_issue_create", "playground_channel_post", "playground_channel_read")
_FAMILY_PROPERTY = {"type": "string", "description": "Another project of this family (name or folder) it belongs in."}
for _tool in TOOLS:
    if _tool.name in FAMILY_TOOLS:
        _tool.schema = {**_tool.schema, "properties": {**_tool.schema.get("properties", {}), "project": _FAMILY_PROPERTY}}


def listed_tools() -> list[Tool]:
    if os.environ.get("PLAYGROUND_MCP_ALL") == "1":
        return list(TOOLS)
    return [tool for tool in TOOLS if tool.name in LISTED]
