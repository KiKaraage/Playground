# SPDX-License-Identifier: Apache-2.0
"""The Project space: everything about the project in one place.

A page over the window's work area with its own list of sections down the
side; on a phone the list comes first and a section opens over it.

Overview · Goals · Bugs · Stack & services · Usage & money · Analytics ·
Knowledge · Decisions · Brief & recipe · Places · People · History · Code

Every section is a real view of the project folder (core.store and the files
ARCHITECTURE.md's addendum describes: stack.toml, issues/, usage/services.jsonl,
analytics.jsonl), read off the main loop and redrawn when the store says
something changed. Nothing here is made up: a section with nothing in it says
so and says how it fills.

Writing goes through the store: a bug report, a service, a knowledge note, the
brief. Fixing a bug or going back to a restore point starts a goal, because
production only ever changes through a reviewed release.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from gi.repository import Gio, GLib, Gtk

from . import common as c
from .areas_pages import AreaPages
from .repo_page import RepoPages
from .search_page import SearchPages
from .common import Row, Tag, callout, get, text

SECTIONS = [
    ("overview", "Overview", "layout-dashboard"), ("tasks", "Tasks", "list-todo"), ("goals", "Goals", "list-checks"),
    ("bugs", "Bugs", "circle-alert"),
    ("stack", "Stack & services", "layers"), ("money", "Usage & money", "wallet"),
    ("analytics", "Analytics", "chart-column"), ("knowledge", "Knowledge", "book-open"),
    ("decisions", "Decisions", "hand"), ("brief", "Brief & recipe", "file-text"), ("places", "Places", "server"),
    ("people", "People", "users"), ("history", "History", "history"), ("repository", "Repository", "git-branch"),
    ("code", "Code", "code"),
]
GROUPS = {"knowledge": "The project", "places": "Team and places"}
SEVERITY_TONES = {"urgent": "red", "high": "amber", "medium": "violet", "low": None}
PLACE_WORDS = {"stage": "in the preview", "live": "on Live"}
SEVERITY_WORDS = {"urgent": "Urgent", "high": "High", "medium": "Medium", "low": "Low"}
STATUS_WORDS = {"open": "Open", "fixing": "Being fixed", "fixed": "Fixed", "wont-fix": "Won’t fix"}
KIND_GLYPHS = {"email": "mail", "database": "database", "storage": "hard-drive", "hosting": "server",
               "auth": "key-round", "payments": "credit-card", "analytics": "chart-column", "dns": "globe",
               "ai": "sparkles", "messaging": "message-square", "other": "component", "pages": "layout-grid",
               "data": "database", "saving": "database", "sign-in": "key-round", "runs on": "server",
               "texts": "message-square", "game": "gamepad-2"}
KIND_WORDS = {"email": "Email", "database": "Database", "storage": "Storage", "hosting": "Hosting", "auth": "Sign-in",
              "payments": "Payments", "analytics": "Analytics", "dns": "Domain", "ai": "AI", "messaging": "Texts",
              "other": "Service"}
HISTORY_FILTERS = [("all", "Everything"), ("release", "Releases"), ("decision", "Decisions"),
                   ("agent", "Agent work"), ("review", "Reviews"), ("undo", "Undone")]
HISTORY_KINDS = {
    "release": {"release", "released", "restore"},
    "decision": {"decision", "decided", "decision.asked", "decision.answered"},
    "agent": {"agent", "step", "tool", "goal", "created", "handoff", "proof", "checks"},
    "review": {"review", "approved", "release.proposed"},
    "undo": {"undo", "undone", "headsup.undo"},
}
HISTORY_GLYPHS = {"release": "radio-tower", "decision": "hand", "agent": "sparkles", "review": "eye",
                  "undo": "undo-2", "people": "users", "place": "server", "created": "folder-open",
                  "issue": "circle-alert", "service": "layers"}
# Metric names an adapter or an agent reports, in words a person reads.
METRIC_WORDS = {
    "emails_sent": "emails sent", "emails": "emails sent", "bounces": "bounced", "stored_gb": "GB stored",
    "storage_gb": "GB stored", "size_gb": "GB", "size_mb": "MB", "reads": "reads", "class_a_ops": "writes",
    "class_b_ops": "reads", "writes": "writes", "requests": "requests", "texts_sent": "texts sent",
    "messages": "messages", "failed": "failed", "sign_ins": "sign-ins", "users": "people", "rows": "rows",
    "connections": "connections", "cpu_percent": "% CPU", "uptime_percent": "% up", "bandwidth_gb": "GB out",
    "hours": "hours", "invocations": "runs", "delivered": "delivered", "bounced": "bounced",
    "texts_failed": "failed", "database_mb": "MB of data", "connections_peak": "connections at the busiest",
    "data_transfer_gb": "GB sent", "cpu_percent_avg": "% CPU on average", "latency_ms": "ms to answer",
}
FAILURES = {"texts_failed", "failed", "errors", "emails_failed"}
# Counts add up over a month; levels (sizes, peaks, averages) are read from the newest snapshot.
ADDITIVE = {"emails_sent", "emails", "delivered", "bounced", "bounces", "texts_sent", "texts_failed", "failed",
            "messages", "requests", "reads", "writes", "class_a_ops", "class_b_ops", "sign_ins", "invocations",
            "data_transfer_gb", "bandwidth_gb", "hours"}


def present(window: Gtk.Window, **options) -> c.Sheet:
    return ProjectSpace(window, options).sheet


# ── Reading the project ──────────────────────────────────────────────────────


@dataclass
class Space:
    project: Any = None
    brief: Any = None
    goals: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    knowledge: list = field(default_factory=list)
    places: list = field(default_factory=list)
    activity: list = field(default_factory=list)
    services: list = field(default_factory=list)
    dependencies: list = field(default_factory=list)
    service_usage: dict = field(default_factory=dict)     # service id → [snapshot, oldest first]
    issues: list = field(default_factory=list)
    tasks: list = field(default_factory=list)
    channel: list = field(default_factory=list)
    repo: Any = None                                      # core.repo.RepoState
    techs: list = field(default_factory=list)             # core.techs.Tech: what it's built with
    box: Any = None                                       # core.box.BoxState: the project's build sandbox
    analytics: list = field(default_factory=list)
    models: dict = field(default_factory=dict)            # this month's model spending (usage.month_lines)
    statuses: dict = field(default_factory=dict)          # service id → the last check, in plain words


def _store(project_dir: Path):
    return c.module("core.store").ProjectStore(project_dir)


def _as_dict(item: Any) -> dict:
    if isinstance(item, dict):
        return item
    if hasattr(item, "to_dict"):
        return item.to_dict()
    return dict(vars(item))


def read_stack(project_dir: Path, store) -> tuple[list[dict], list[dict]]:
    found = c.call(store, ("read_stack",), default=None)
    if found is not None:
        if isinstance(found, tuple):
            services, deps = found
        else:
            services, deps = get(found, "services") or [], get(found, "dependencies") or []
        return [_as_dict(s) for s in services], [_as_dict(d) for d in deps]
    path = Path(project_dir) / ".playground" / "stack.toml"
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return [], []
    return ([s for s in document.get("services") or [] if isinstance(s, dict)],
            [d for d in document.get("dependencies") or [] if isinstance(d, dict)])


def read_jsonl(path: Path) -> list[dict]:
    store_mod = c.module("core.store")
    return store_mod.read_jsonl(path) if store_mod else []


def read_issues(project_dir: Path, store) -> list[dict]:
    found = c.call(store, ("list_issues",), default=None)
    if found is not None:
        return [_issue_dict(i) for i in found]
    store_mod = c.module("core.store")
    folder = Path(project_dir) / ".playground" / "issues"
    issues = []
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        try:
            fields, body = store_mod.parse_front_matter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        _pre, sections = store_mod.parse_sections(body)
        issue = dict(fields)
        issue["sections"] = {h.lower(): t for h, t in sections}
        issue.setdefault("number", int(path.stem) if path.stem.isdigit() else 0)
        issues.append(issue)
    return issues


def _issue_dict(issue: Any) -> dict:
    data = dict(_as_dict(issue))
    sections = data.get("sections")
    if isinstance(sections, list):
        data["sections"] = {str(get(s, "heading") if not isinstance(s, (list, tuple)) else s[0]).lower():
                            str(get(s, "text") if not isinstance(s, (list, tuple)) else s[1]) for s in sections}
    elif not isinstance(sections, dict):
        data["sections"] = {}
    for key, attr in (("what happened", "what_happened"), ("expected", "expected"), ("steps", "steps"),
                      ("evidence", "evidence")):
        if data.get(attr) and not data["sections"].get(key):
            data["sections"][key] = data[attr] if isinstance(data[attr], str) else "\n".join(map(str, data[attr]))
    return data


def load_space(project_dir: Path) -> Space:
    store = _store(project_dir)
    space = Space(project=store.read_project(), brief=store.read_brief(),
                  goals=[g for g in store.list_goals() if not g.is_chat], decisions=store.read_decisions(),
                  pending=store.list_pending(kind="decision"), knowledge=store.list_knowledge(),
                  activity=list(reversed(store.read_activity(limit=400))))
    places = c.module("core.places")
    space.places = places.list_places(project_dir) if places else [p.to_dict() for p in space.project.places]
    space.services, space.dependencies = read_stack(project_dir, store)
    usage = c.call(store, ("read_service_usage",), default=None)
    records = usage if usage is not None else read_jsonl(Path(project_dir) / ".playground" / "usage" / "services.jsonl")
    for record in sorted((_as_dict(r) for r in records), key=lambda r: str(r.get("ts", ""))):
        space.service_usage.setdefault(str(record.get("service_id") or record.get("service") or ""), []).append(record)
    space.issues = sorted(read_issues(project_dir, store), key=lambda i: -int(i.get("number") or 0))
    space.tasks = c.call(store, ("list_tasks",), default=[]) or []
    space.channel = c.call(store, ("read_channel",), 40, default=[]) or []
    try:
        space.repo = c.module("core.repo").state(project_dir)
    except Exception:  # noqa: BLE001 - no git, or a broken repository: the area says so
        space.repo = None
    try:
        space.techs = c.module("core.techs").detect(project_dir)
        space.box = c.module("core.box").state(project_dir, getattr(space.project, "id", "") or "")
    except Exception:  # noqa: BLE001
        space.techs, space.box = [], None
    services = c.module("core.services")
    if services is not None:
        try:
            space.statuses = c.module("core.store").read_json(services.status_path(project_dir)) or {}
        except Exception:  # noqa: BLE001
            space.statuses = {}
    analytics = c.call(store, ("read_analytics",), default=None)
    space.analytics = sorted((_as_dict(r) for r in (analytics if analytics is not None else
                              read_jsonl(Path(project_dir) / ".playground" / "analytics.jsonl"))),
                             key=lambda r: str(r.get("ts", "")))
    try:
        from .usage import month_lines
        space.models = month_lines(space.project.id)
    except Exception:  # noqa: BLE001 - money from models is shown when it can be read
        space.models = {"lines": [], "total": 0.0, "runs": []}
    return space


# ── Words about services ─────────────────────────────────────────────────────


def metric_line(metrics: dict, limit: int = 2) -> str:
    """{"emails_sent": 4212} → "4,212 emails sent"; the first numbers a person would care about."""
    parts = []
    for key, value in (metrics or {}).items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        words = METRIC_WORDS.get(key, key.replace("_", " "))
        parts.append(f"{c.number_words(value)}{words}" if words.startswith("%") else f"{c.number_words(value)} {words}")
        if len(parts) >= limit:
            break
    return " · ".join(parts)


def month_metrics(snapshots: list[dict]) -> tuple[dict, str]:
    """This month's numbers for a service: counts summed over its days, levels as they are now."""
    if not snapshots:
        return {}, ""
    this_month = datetime.now().strftime("%Y-%m")
    monthly = [s for s in snapshots if str(s.get("period")) == this_month]
    if monthly:
        return dict(monthly[-1].get("metrics") or {}), "This month so far"
    days = [s for s in snapshots if _DAY.match(str(s.get("period", ""))) and str(s["period"]).startswith(this_month)]
    if not days:
        return dict(snapshots[-1].get("metrics") or {}), _period_words(snapshots[-1])
    totals: dict[str, float] = {}
    for snapshot in days:
        for key, value in (snapshot.get("metrics") or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and key in ADDITIVE:
                totals[key] = totals.get(key, 0) + value
    latest = days[-1].get("metrics") or {}
    merged = {}
    for key, value in latest.items():
        merged[key] = round(totals[key], 2) if key in totals else value
    return merged, "This month so far"


def primary_series(snapshots: list[dict]) -> tuple[str, list[float]]:
    """The first metric the snapshots share, as a series for the sparkline."""
    if not snapshots:
        return "", []
    keys = [k for k, v in (snapshots[-1].get("metrics") or {}).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)]
    for key in keys:
        series = [float(s["metrics"][key]) for s in snapshots
                  if isinstance((s.get("metrics") or {}).get(key), (int, float))]
        if len(series) >= 2:
            return key, series[-14:]
    return "", []


_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")


def month_cost(snapshots: list[dict]) -> float | None:
    """This month's cost: the newest month-to-date figure, else the sum of this month's days."""
    this_month = datetime.now().strftime("%Y-%m")
    monthly = [s for s in snapshots if str(s.get("period")) == this_month and s.get("cost") is not None]
    if monthly:
        return float(monthly[-1]["cost"])
    days = [float(s["cost"]) for s in snapshots if _DAY.match(str(s.get("period", "")))
            and str(s.get("period")).startswith(this_month + "-") and s.get("cost") is not None]
    return round(sum(days), 2) if days else None


def service_health(snapshots: list[dict], status: dict | None = None) -> tuple[str, str]:
    """('good' | 'warn' | 'bad' | 'off', words): the last check (core.services), else how fresh the numbers are."""
    if status:
        when = c.shared.parse_ts(status.get("checked"))
        ago = c.shared.ago(when) if when else ""
        if status.get("ok"):
            return "good", f"Working · checked {ago}" if ago else "Working"
        return "bad", str(status.get("status") or "Having trouble")
    latest = snapshots[-1] if snapshots else None
    if latest is None:
        return "off", "Not checked yet"
    when = c.shared.parse_ts(latest.get("ts"))
    recent = [s for s in snapshots[-7:] if any(k in FAILURES and (v or 0) > 0
                                               for k, v in (s.get("metrics") or {}).items())]
    if recent:
        failed = sum(int(v) for s in recent for k, v in (s.get("metrics") or {}).items() if k in FAILURES)
        return "warn", f"{failed} failed this week"
    if when is not None and (datetime.now() - when).days <= 2:
        return "good", f"Reporting · {c.shared.ago(when)}"
    return "warn", f"No numbers since {c.shared.day_words(when)}" if when else "No recent numbers"


def budget_of(service: dict) -> float | None:
    value = service.get("monthly_budget")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) or None
    match = re.search(r"[\d.]+", str(value or ""))
    return float(match.group()) if match else None


DOT_TONES = {"good": "good", "warn": "amber", "bad": "red", "off": "off"}


# ── The space ────────────────────────────────────────────────────────────────


class AreaHost:
    """Where the Project space's sections stand when they fill the window's middle island (the sidebar
    picks the section) instead of a page with its own list. It answers what a Sheet would."""

    def __init__(self, window: Gtk.Window, holder: Gtk.Box) -> None:
        self.window = window
        self.card = holder            # toasts and dialogs anchor here
        self.listener = c.Listener()
        self.on_layout: list = []
        self.phone = False
        self.compact = False
        self.closed = False

    def set_body(self, widget: Gtk.Widget, scroll: bool = False) -> None:
        c.clear(self.card)
        self.card.append(widget)

    def present(self) -> None:
        pass

    def close(self) -> None:          # nothing to close: the area stays, the conversation is beside it
        pass

    def set_sizes(self, *, phone: bool, compact: bool) -> None:
        if (phone, compact) != (self.phone, self.compact):
            self.phone, self.compact = phone, compact
            for callback in list(self.on_layout):
                callback()

    def drop(self) -> None:
        self.listener.close()
        self.closed = True


class ProjectSpace(AreaPages, RepoPages, SearchPages):
    def __init__(self, window: Gtk.Window, options: dict, host: AreaHost | None = None) -> None:
        self.window = window
        self.ctx = c.context_for(window, options)
        self.data: Space | None = None
        wanted = options.get("section") or ("bugs" if options.get("issue") else "overview")
        self.at = wanted if wanted in {k for k, _n, _i in SECTIONS} else "overview"
        self.issue_open: int | None = int(options["issue"]) if str(options.get("issue", "")).isdigit() else None
        self.history_filter = "all"
        self.analytics_days = 30
        self.journey_at = 0
        self.service_mode = "ari"
        self.forms: set[str] = set()
        self.editing_brief = False
        self.phone_detail = bool(options.get("section") or options.get("issue"))
        self.embedded = host is not None
        if host is not None:
            self.sheet = host
            self.phone_detail = True
        else:
            back = options.get("back_label") or c.goal_title(self.ctx) or "Back"
            filer = c.button("Show in Filer", icon="folder", style="fill", small=True, on_click=self.show_in_filer)
            self.sheet = c.Sheet(window, "Project", self.ctx.name, size="page", name="book", back_label=back,
                                 header_end=filer)
        self.sheet.on_layout.append(lambda *_a: self.layout())
        for topic in ("project.changed", "goal.changed", "activity.appended", "places.changed", "usage.changed",
                      "decision.pending", "decision.answered", "stack.changed", "issue.changed", "analytics.changed",
                      "task.changed", "channel.posted"):
            self.sheet.listener.on(topic, lambda **_: self.load())

        self.nav = c.vbox(spacing=1, css="pgs-book-toc")
        nav_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        nav_scroll.set_child(self.nav)
        self.nav_column = c.vbox(nav_scroll, width_request=244, hexpand=False, css="pgs-nav-column")
        self.nav_column.set_visible(not self.embedded)
        self.view = c.vbox(css="pgs-doc", hexpand=True)
        clamp = c.Adw.Clamp(maximum_size=1100, tightening_threshold=900, child=self.view, hexpand=True)
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, hexpand=True)
        self.scroll.set_child(clamp)
        self.sheet.set_body(c.hbox(self.nav_column, self.scroll, spacing=0, vexpand=True), scroll=False)
        self.sheet.present()
        self.layout()
        self.load()

    # ── data ──

    def load(self) -> None:
        if self.ctx.project_dir is None:
            self.render()
            return
        project_dir = self.ctx.project_dir

        def done(space: Space) -> None:
            self.data = space
            self.ctx.__dict__["_project"] = space.project
            self.render(keep_scroll=True)

        c.run_async(lambda: load_space(project_dir), done, self.fail)

    def fail(self, error) -> None:
        c.clear(self.view)
        self.view.append(c.empty("The project couldn’t be read", str(error), "folder"))

    # ── layout ──

    def layout(self) -> None:
        phone = self.sheet.phone
        if self.embedded:
            self.view.add_css_class("pgs-embedded")
            (self.view.add_css_class if phone else self.view.remove_css_class)("phone")
        self.nav_column.set_visible(not self.embedded and (not phone or not self.phone_detail))
        self.scroll.set_visible(not phone or self.phone_detail)
        self.nav_column.set_hexpand(phone)
        self.render(keep_scroll=True)

    def go(self, key: str, item=None) -> None:
        self.at = key if key in {k for k, _n, _i in SECTIONS} | {"search"} else "tasks"
        self.issue_open = int(item) if key == "bugs" and str(item or "").isdigit() else None
        if key == "knowledge":
            self.note_open = str(item) if item else None
        self.phone_detail = True
        self.layout()
        self.scroll.get_vadjustment().set_value(0)

    def counts(self) -> dict[str, tuple[int, bool]]:
        data = self.data
        if data is None:
            return {}
        open_bugs = [i for i in data.issues if i.get("status", "open") in ("open", "fixing")]
        urgent = any(i.get("severity") in ("urgent", "high") for i in open_bugs)
        needs = [g for g in data.goals if g.status.value == "needs-you"]
        return {"bugs": (len(open_bugs), urgent), "decisions": (len(data.pending), bool(data.pending)),
                "goals": (len([g for g in data.goals if g.status.value in ("working", "needs-you", "ready")]),
                          bool(needs))}

    def render(self, keep_scroll: bool = False) -> None:
        offset = self.scroll.get_vadjustment().get_value() if keep_scroll else 0
        self.fill_nav()
        c.clear(self.view)
        if self.ctx.project_dir is None:
            self.view.append(c.empty("No project open", "Open a project to see everything about it.", "folder"))
            return
        if self.data is None:
            self.view.append(c.vbox(c.shared.Spinner(18), text("Opening the project…", "pgs-row-sub", xalign=0.5),
                                    spacing=10, css="pgs-empty"))
            return
        if self.sheet.phone and not self.embedded and not (self.at == "bugs" and self.issue_open is not None):
            back = c.button("Sections", icon="chevron-left", style="plain", small=True,
                            on_click=lambda: (setattr(self, "phone_detail", False), self.layout()))
            back.set_halign(Gtk.Align.START)
            back.set_margin_bottom(8)
            self.view.append(back)
        builder = getattr(self, f"section_{self.at}")
        try:
            self.view.append(builder(self.data))
        except Exception as error:  # noqa: BLE001 - one broken section must not take the space down
            c.log.exception("project space section %s failed", self.at)
            self.view.append(c.empty("This section couldn’t be shown", str(error), "triangle-alert"))
        if self.embedded:
            self.update_bar()
        if offset:
            GLib.idle_add(lambda: (self.scroll.get_vadjustment().set_value(offset), False)[1])

    def fill_nav(self) -> None:
        c.clear(self.nav)
        data = self.data
        name = data.project.name if data else self.ctx.name
        mark = c.shared.project_mark(name or "?", self.ctx.project_dir, 30)
        mark.set_hexpand(False)
        head = c.hbox(mark, c.vbox(text(name or "Project", "pgs-nav-name", wrap=False, ellipsize=True),
                                   text(self.ctx.display_path, "pgs-nav-path", wrap=False, ellipsize=True),
                                   spacing=0, hexpand=True, valign=Gtk.Align.CENTER), spacing=10, css="pgs-nav-head")
        self.nav.append(head)
        counts = self.counts()
        for key, label, icon in SECTIONS:
            if GROUPS.get(key):
                self.nav.append(text(GROUPS[key], "pgs-nav-group", wrap=False))
            row = Gtk.Button()
            row.add_css_class("pgs-toc-row")
            if key == self.at and not (self.sheet.phone and not self.phone_detail):
                row.add_css_class("on")
            line = c.hbox(c.glyph(icon, 16), text(label, wrap=False, hexpand=True), spacing=12)
            count, attention = counts.get(key, (0, False))
            if count:
                badge = text(str(count), "pgs-nav-count", wrap=False, valign=Gtk.Align.CENTER, xalign=0.5)
                if attention:
                    badge.add_css_class("attention")
                line.append(badge)
            if self.sheet.phone:
                line.append(c.glyph("chevron-right", 15, "pgs-chevron"))
            row.set_child(line)
            row.update_property([Gtk.AccessibleProperty.LABEL], [f"{label}{f', {count}' if count else ''}"])
            row.connect("clicked", lambda _b, k=key: self.go(k))
            self.nav.append(row)

    def heading(self, title: str, lede: str, *actions: Gtk.Widget) -> Gtk.Widget:
        name = text(title, "pgs-doc-title", hexpand=True, accessible_role=Gtk.AccessibleRole.HEADING)
        if self.sheet.phone and actions:
            # On a phone the actions sit under the words, never squeezing the title.
            row = c.Adw.WrapBox(child_spacing=8, line_spacing=8)
            for action in actions:
                row.append(action)
            row.set_margin_top(8)
            box = c.vbox(name, text(lede, "pgs-doc-sub"), row, spacing=4)
            box.set_margin_bottom(20)
            return box
        head = c.hbox(name, *actions, spacing=8)
        box = c.vbox(head, text(lede, "pgs-doc-sub"), spacing=4)
        box.set_margin_bottom(20)
        return box

    def subhead(self, title: str, aside: str = "", action: Gtk.Widget | None = None) -> Gtk.Widget:
        line = c.hbox(text(title, "pgs-sub-title", wrap=False),
                      text(aside, "pgs-sub-aside", wrap=False, hexpand=True, ellipsize=True), action, spacing=8)
        line.set_margin_top(26)
        line.set_margin_bottom(10)
        return line

    def columns(self, *widgets: Gtk.Widget, wide: int = 2, phone: int = 1) -> Gtk.Widget:
        """A grid: ``wide`` across in a window, two when compact, ``phone`` across on a phone (stat tiles: 2)."""
        grid = Gtk.Grid(column_spacing=12 if not self.sheet.phone else 10, row_spacing=12 if not self.sheet.phone
                        else 10, column_homogeneous=True)
        per_row = phone if self.sheet.phone else (min(wide, 2) if self.sheet.compact else wide)
        for index, widget in enumerate(widgets):
            grid.attach(widget, index % per_row, index // per_row, 1, 1)
        return grid

    # ── Overview ──

    def section_overview(self, data: Space) -> Gtk.Widget:
        project = data.project
        box = c.vbox()
        mark = c.shared.project_mark(project.name, project.path, 52)
        mark.set_hexpand(False)
        home = project.place(project.home) if project.home not in ("", "this-computer") else None
        live = [g for g in data.goals if g.status.value in ("live", "done")]
        title = c.vbox(text(project.name, "pgs-doc-title", wrap=False, accessible_role=Gtk.AccessibleRole.HEADING),
                       text(project.summary or "No summary yet. It comes from the brief.", "pgs-doc-sub"),
                       c.hbox(Tag(f"Lives on {home.name}" if home else "On this computer", "good"), spacing=6),
                       spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        box.append(c.hbox(mark, title, spacing=16))

        needs = [g for g in data.goals if g.status.value == "needs-you"]
        ready = [g for g in data.goals if g.status.value == "ready"]
        open_bugs = [i for i in data.issues if i.get("status", "open") in ("open", "fixing")]
        spend, budget = self.month_totals(data)
        live_url = next((p.get("url") for p in data.places if p.get("role") == "production" and p.get("url")), "")
        tiles = [
            stat_tile("What’s live", str(len(live)), live_url.split("//")[-1].strip("/") if live_url else
                      ("goal released" if len(live) == 1 else "goals released"), on_click=lambda: self.go("goals")),
            stat_tile("Needs you", str(len(needs) + len(data.pending) + len(ready)),
                      _needs_words(len(data.pending) + len(needs), len(ready)), on_click=lambda: self.go("goals"),
                      tone="amber" if data.pending or needs else None),
            stat_tile("Open bugs", str(len(open_bugs)), _bug_words(open_bugs), on_click=lambda: self.go("bugs"),
                      tone="bad" if any(i.get("severity") == "urgent" for i in open_bugs) else None),
            stat_tile("This month", c.money(spend) if spend else "$0",
                      f"of {c.money(budget)} in budgets" if budget else "no budgets set",
                      on_click=lambda: self.go("money")),
        ]
        grid = self.columns(*tiles, wide=4)
        grid.set_margin_top(22)
        box.append(grid)

        attention = []
        for pending in data.pending:
            attention.append(Row(pending.question, "Only you can decide · waiting in the conversation", lead="hand",
                                 tags=[Tag("Needs you", "amber")],
                                 on_activate=(lambda p=pending: self.open_goal(p.goal)) if pending.goal else None))
        for goal in ready:
            attention.append(Row(goal.title, "Ready to review and release", lead=_state_lead("ready"),
                                 on_activate=lambda g=goal: c.open_sheet(self.window, "review", goal_id=g.id)))
        for issue in [i for i in open_bugs if i.get("severity") in ("urgent", "high")][:3]:
            attention.append(self.bug_row(issue, data))
        if attention:
            box.append(self.subhead("Needs you"))
            box.append(c.ListGroup(*attention, css="pgs-box"))

        services = c.vbox()
        services.append(self.subhead("Services", f"{len(data.services)} in the stack" if data.services else "",
                                     c.link_button("Stack", "arrow-right", lambda: self.go("stack"))))
        rows = []
        for service in data.services[:7]:
            snaps = data.service_usage.get(str(service.get("id")), [])
            tone, words = service_health(snaps, data.statuses.get(str(service.get("id"))))
            rows.append(Row(service.get("name") or service.get("provider", ""),
                            " · ".join(filter(None, [metric_line(month_metrics(snaps)[0], 1),
                                                     words if tone in ("warn", "bad") else ""])) or words,
                            lead=service_tile(service, 30), end=c.shared.dot(DOT_TONES[tone]),
                            on_activate=lambda: self.go("stack")))
        if not rows:
            rows.append(Row("No services yet", "Tell Ari what the app uses (email, a database, storage) and it "
                                               "lists them here.", lead="layers", on_activate=lambda: self.go("stack")))
        services.append(c.ListGroup(*rows, css="pgs-box"))
        recent = c.vbox()
        recent.append(self.subhead("Recently", "", c.link_button("History", "arrow-right", lambda: self.go("history"))))
        recent.append(c.ListGroup(*[self.history_row(e, None) for e in data.activity[:7]] or
                                  [Row("Nothing yet", None, lead="history")], css="pgs-box"))
        box.append(self.columns(services, recent))
        return box

    def month_totals(self, data: Space) -> tuple[float, float]:
        spend = sum(month_cost(data.service_usage.get(str(s.get("id")), [])) or 0.0 for s in data.services)
        spend += float(data.models.get("total") or 0.0)
        budget = sum(budget_of(s) or 0.0 for s in data.services)
        return round(spend, 2), round(budget, 2)

    # ── Goals ──

    def section_goals(self, data: Space) -> Gtk.Widget:
        new = c.button("New goal", icon="plus", style="fill", small=True, on_click=self.new_goal)
        box = c.vbox(self.heading("Goals", "The task list. It moves on its own as goals move.", new))
        by: dict[str, list] = {"now": [], "next": [], "later": [], "live": []}
        for goal in data.goals:
            key = {"working": "now", "needs-you": "now", "ready": "next", "paused": "later"}.get(goal.status.value,
                                                                                                "live")
            by[key].append(goal)
        columns = []
        for key, title, empty in (("now", "Now", "Nothing running"), ("next", "Next", "Nothing waiting"),
                                  ("later", "Later", "Nothing put aside")):
            column = c.vbox(spacing=6, css="pgs-col")
            column.append(c.hbox(text(title, "pgs-col-head", wrap=False, hexpand=True),
                                 text(str(len(by[key])), "pgs-col-count", wrap=False)))
            for goal in by[key]:
                column.append(self.goal_tile(goal, data))
            if not by[key]:
                column.append(text(empty, "pgs-col-empty"))
            columns.append(column)
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, column_homogeneous=True)
        for index, column in enumerate(columns):
            if self.sheet.compact:
                grid.attach(column, 0, index, 1, 1)
            else:
                grid.attach(column, index, 0, 1, 1)
        box.append(grid)
        if by["live"]:
            box.append(self.subhead("Live", f"{len(by['live'])} released"))
            rows = []
            for goal in sorted(by["live"], key=lambda g: g.updated, reverse=True):
                when = c.shared.parse_ts(goal.updated)
                words = goal.status_note or (f"Live since {c.shared.day_words(when)}" if when else goal.status.label)
                rows.append(Row(goal.title, words, lead="check", on_activate=lambda g=goal: self.open_goal(g.id)))
            box.append(c.ListGroup(*rows, css="pgs-box"))
        return box

    def goal_tile(self, goal, data: Space) -> Gtk.Widget:
        button = Gtk.Button()
        button.add_css_class("pgs-tile")
        glyph = c.shared.StateGlyph(goal.status.value, 16)
        glyph.set_valign(Gtk.Align.START)
        owner = data.project.member(goal.owner) if goal.owner else None
        mine = owner is None or owner is data.project.owner or (data.project.owner and owner.id == data.project.owner.id)
        sub = c.hbox(spacing=6)
        if not mine:
            sub.append(c.shared.person_face(owner.name, 14))
        sub.append(text(" · ".join(filter(None, [goal.status_note or goal.status.label,
                                                  "" if mine else f"{c.possessive(owner.first_name)} agent"])),
                        "pgs-tile-sub"))
        bugs = [i for i in data.issues if i.get("goal") == goal.id]
        if bugs:
            sub.append(Tag(f"#{bugs[0].get('number')}", "red"))
        button.set_child(c.hbox(glyph, c.vbox(text(goal.title, "pgs-tile-title"), sub, spacing=3, hexpand=True),
                                spacing=8))
        button.update_property([Gtk.AccessibleProperty.LABEL], [f"{goal.title}, {goal.status.label}"])
        button.connect("clicked", lambda _b: self.open_goal(goal.id))
        return button

    def new_goal(self) -> None:
        handler = getattr(self.window, "new_goal", None)
        if callable(handler):
            self.sheet.close()
            handler()

    def open_goal(self, goal_id: str) -> None:
        for name in ("select_goal", "open_goal", "show_goal"):
            handler = getattr(self.window, name, None)
            if callable(handler) and goal_id:
                self.sheet.close()
                handler(goal_id)
                return

    # ── Bugs ──

    def section_bugs(self, data: Space) -> Gtk.Widget:
        if self.issue_open is not None:
            issue = next((i for i in data.issues if int(i.get("number") or 0) == self.issue_open), None)
            if issue is not None:
                return self.bug_detail(issue, data)
            if getattr(self, "_reread_for", None) != self.issue_open:
                # Just filed (by a person, an agent, a stopped preview): read the project again before giving up.
                self._reread_for = self.issue_open
                self.load()
                return c.vbox(c.shared.Spinner(18), spacing=10, css="pgs-empty")
            self.issue_open = None
        report = c.button("Report a bug", icon="plus", style="key", small=True, on_click=lambda: self.toggle_form("bug"))
        open_count = len([i for i in data.issues if i.get("status", "open") in ("open", "fixing")])
        lede = (f"{open_count} open · {len(data.issues) - open_count} closed. From people, checks and agents; "
                "agents see new ones when they start work.")
        if self.embedded:
            filters = c.segmented([("open", "Open"), ("closed", "Closed"), ("all", "All")], self.bug_filter,
                                  label="Show", on_change=lambda key: self._set_task("bug_filter", key))
            box = c.vbox(self.heading("Bugs", f"{open_count} open · {len(data.issues) - open_count} closed", filters))
            box.append(self.bug_strip(data))
        else:
            box = c.vbox(self.heading("Bugs", lede, report))
        if "bug" in self.forms:
            box.append(self.bug_form())
        opened = [i for i in data.issues if i.get("status", "open") in ("open", "fixing")]
        closed = [i for i in data.issues if i.get("status") in ("fixed", "wont-fix")]
        if self.embedded:
            groups = [(SEVERITY_WORDS[s], [i for i in opened if (i.get("severity") or "medium") == s])
                      for s in ("urgent", "high", "medium", "low")] if self.bug_filter != "closed" else []
            groups += [("Closed", closed)] if self.bug_filter != "open" else []
            if data.issues and not any(issues for _t, issues in groups):
                groups = []
                box.append(c.ListGroup(Row("Nothing open" if self.bug_filter == "open" else "Nothing here",
                                           "Switch to All to see every bug.", lead="circle-check"), css="pgs-box"))
        else:
            groups = [("Open", opened), ("Closed", closed)]
        for title, issues in groups:
            if issues:
                box.append(self.subhead(title, str(len(issues))))
                box.append(c.ListGroup(*[self.bug_row(i, data) for i in issues], css="pgs-box"))
        if not data.issues:
            box.append(c.ListGroup(Row("No bugs reported", "When something goes wrong, report it here, or Ari files "
                                                           "one when a check fails on Live.", lead="circle-check"),
                                   css="pgs-box"))
        return box

    def bug_strip(self, data: Space) -> Gtk.Widget:
        """Triage at a glance: what's open, what's serious, what's being fixed, what got fixed lately."""
        model = c.module("core.model")
        opened = [i for i in data.issues if i.get("status", "open") in ("open", "investigating", "fixing")]
        serious = [i for i in opened if i.get("severity") in ("urgent", "high")]
        fixing = [i for i in opened if i.get("status") in ("fixing", "investigating")]
        recent = c.shared.parse_ts(model.now_iso())
        fixed = [i for i in data.issues if i.get("status") == "fixed" and recent is not None and
                 (c.shared.parse_ts(i.get("updated")) or recent) >= recent - timedelta(days=30)]
        return self.columns(
            stat_tile("Open", str(len(opened)), "waiting or underway",
                      on_click=lambda: self._set_task("bug_filter", "open")),
            stat_tile("Serious", str(len(serious)), "urgent or high", tone="bad" if serious else None,
                      on_click=lambda: self._set_task("bug_filter", "open")),
            stat_tile("Being fixed", str(len(fixing)), "an agent is on it",
                      on_click=lambda: self._set_task("bug_filter", "open")),
            stat_tile("Fixed", str(len(fixed)), "in the last 30 days",
                      on_click=lambda: self._set_task("bug_filter", "closed")),
            wide=4, phone=2)

    def bug_row(self, issue: dict, data: Space) -> Gtk.Widget:
        severity = str(issue.get("severity") or "medium")
        status = str(issue.get("status") or "open")
        goal = next((g for g in data.goals if g.id == issue.get("goal")), None)
        when = c.shared.parse_ts(issue.get("created"))
        parts = [STATUS_WORDS.get(status, status.capitalize()),
                 PLACE_WORDS.get(str(issue.get("place")), f"on {str(issue.get('place')).capitalize()}") if issue.get("place") else "",
                 f"in “{goal.title}”" if goal else "", c.shared.day_words(when) if when else ""]
        lead = text(f"#{issue.get('number')}", "pgs-bug-number", wrap=False, xalign=0.5)
        lead.set_size_request(40, -1)
        return Row(str(issue.get("title") or "A bug"), " · ".join(p for p in parts if p), lead=lead,
                   tags=[Tag(severity.capitalize(), SEVERITY_TONES.get(severity))],
                   end=c.glyph("chevron-right", 15, "pgs-chevron"), on_activate=lambda: self.open_issue(issue),
                   dim=status in ("fixed", "wont-fix"))

    def open_issue(self, issue: dict) -> None:
        self.at = "bugs"
        self.issue_open = int(issue.get("number") or 0)
        self.phone_detail = True
        self.layout()
        self.scroll.get_vadjustment().set_value(0)

    def bug_detail(self, issue: dict, data: Space) -> Gtk.Widget:
        number = issue.get("number")
        severity = str(issue.get("severity") or "medium")
        status = str(issue.get("status") or "open")
        goal = next((g for g in data.goals if g.id == issue.get("goal")), None)
        box = c.vbox()
        back = c.button("All bugs", icon="chevron-left", style="plain", small=True,
                        on_click=lambda: (setattr(self, "issue_open", None), self.render()))
        back.set_halign(Gtk.Align.START)
        box.append(back)
        chips = c.hbox(Tag(f"#{number}"), Tag(severity.capitalize(), SEVERITY_TONES.get(severity)),
                       Tag(STATUS_WORDS.get(status, status), "good" if status == "fixed" else
                           ("run" if status == "fixing" else None)), spacing=6)
        chips.set_margin_top(14)
        box.append(chips)
        title = text(str(issue.get("title") or "A bug"), "pgs-doc-title", accessible_role=Gtk.AccessibleRole.HEADING)
        title.set_margin_top(8)
        box.append(title)
        when = c.shared.parse_ts(issue.get("created"))
        reporter = data.project.member(str(issue.get("reporter") or ""))
        reporter_name = reporter.name if reporter else str(issue.get("reporter") or "")
        if reporter_name.lower() == "ari":
            reporter_name = "Ari"
        meta = " · ".join(filter(None, [f"Reported by {reporter_name}" if reporter_name else "",
                                        c.shared.when_words(when) if when else "",
                                        PLACE_WORDS.get(str(issue.get("place")), f"on {str(issue.get('place')).capitalize()}") if issue.get("place") else ""]))
        box.append(text(meta, "pgs-doc-sub"))
        actions = c.hbox(spacing=8)
        actions.set_margin_top(16)
        actions.set_visible(not self.embedded)
        if goal is not None:
            actions.append(c.button(f"Open “{goal.title}”", icon="arrow-right", style="key", small=True,
                                    on_click=lambda: self.open_goal(goal.id)))
        elif status in ("open", "fixing"):
            actions.append(c.button("Ask Ari to fix it", icon="sparkles", style="key", small=True,
                                    on_click=lambda: self.fix_bug(issue)))
        if status in ("open", "fixing"):
            actions.append(c.button("Mark fixed", icon="check", style="fill", small=True,
                                    on_click=lambda: self.set_issue(issue, status="fixed")))
        else:
            actions.append(c.button("Open it again", icon="rotate-ccw", style="fill", small=True,
                                    on_click=lambda: self.set_issue(issue, status="open")))
        box.append(actions)
        sections = issue.get("sections") or {}
        shown = [(label, _plain(sections.get(key, ""))) for key, label in
                 (("what happened", "What happened"), ("expected", "Expected"), ("steps", "Steps"),
                  ("evidence", "Evidence"))]
        shown = [(label, value) for label, value in shown if value]
        card = c.vbox(css="pgs-box")
        card.set_margin_top(18)
        card.append(c.kv(shown) if shown else text("No details yet.", "pgs-row-sub"))
        box.append(card)
        return box

    def fix_bug(self, issue: dict) -> None:
        """A goal for the fix, linked both ways. The agent starts from the bug's own words."""
        store, model = _store(self.ctx.project_dir), c.module("core.model")
        number = issue.get("number")
        sections = issue.get("sections") or {}
        outcome = "\n\n".join(filter(None, [
            f"Fix bug #{number}: {issue.get('title')}.",
            f"What happened: {sections.get('what happened', '').strip()}" if sections.get("what happened") else "",
            f"Expected: {sections.get('expected', '').strip()}" if sections.get("expected") else "",
            f"Steps: {sections.get('steps', '').strip()}" if sections.get("steps") else "",
            "Prove it with a check that fails before the fix and passes after, then propose a release."]))

        def create():
            goal = store.create_goal(f"Fix: {issue.get('title')}", outcome=outcome, status=model.GoalStatus.WORKING)
            self._write_issue(store, issue, goal=goal.id, status="fixing")
            store.log(f"Asked Ari to fix bug #{number}", kind="issue", goal=goal.id, data={"issue": number})
            return goal

        c.run_async(create, lambda goal: self.open_goal(goal.id),
                    lambda error: c.toast(self.sheet.card, str(error), kind="error"))

    def set_issue(self, issue: dict, **changes) -> None:
        store = _store(self.ctx.project_dir)
        c.run_async(lambda: self._write_issue(store, issue, **changes), lambda _r: self.load(),
                    lambda error: c.toast(self.sheet.card, str(error), kind="error"))

    def _write_issue(self, store, issue: dict, **changes) -> None:
        number = int(issue.get("number") or 0)
        updater = getattr(store, "update_issue", None)
        if callable(updater):
            updater(number, lambda i: [setattr(i, k, v) for k, v in changes.items()])
            return
        store_mod, model = c.module("core.store"), c.module("core.model")
        path = Path(self.ctx.project_dir) / ".playground" / "issues" / f"{number}.md"
        with store.locked():
            fields, body = store_mod.parse_front_matter(path.read_text(encoding="utf-8"))
            fields.update(changes)
            fields["updated"] = model.now_iso()
            store_mod.atomic_write_text(path, store_mod.dump_front_matter(fields) + "\n" + body)
        store.bus.publish("project.changed", project=str(store.root), what="issues")

    def bug_form(self) -> Gtk.Widget:
        title = Gtk.Entry(placeholder_text="The Join waitlist button does nothing on a phone", hexpand=True)
        title.update_property([Gtk.AccessibleProperty.LABEL], ["What went wrong, in a line"])
        severity, place, fields = ["medium"], ["live"], {}
        form = c.vbox(spacing=10, css="pgs-form-card")
        form.set_margin_bottom(16)
        form.append(c.vbox(title, css="pgs-field"))
        choices = c.Adw.WrapBox(child_spacing=10, line_spacing=8)
        choices.append(c.hbox(text("How bad", "pgs-opt-sub", wrap=False),
                              c.segmented([("low", "Low"), ("medium", "Medium"), ("high", "High"), ("urgent", "Urgent")],
                                          "medium", label="How bad", on_change=lambda k: severity.__setitem__(0, k)),
                              spacing=8))
        choices.append(c.hbox(text("Where", "pgs-opt-sub", wrap=False),
                              c.segmented([("live", "Live"), ("stage", "Stage")], "live", label="Where",
                                          on_change=lambda k: place.__setitem__(0, k)), spacing=8))
        form.append(choices)
        for key, label, hint in (("what", "What happened", "What you saw, in your own words"),
                                 ("expected", "Expected", "What should have happened"),
                                 ("steps", "Steps", "How to make it happen again, one step a line")):
            view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, top_margin=8, bottom_margin=8,
                                tooltip_text=hint)
            view.update_property([Gtk.AccessibleProperty.LABEL], [label])
            view.set_size_request(-1, 52)
            fields[key] = view
            form.append(c.vbox(text(label, "pgs-field-hint"), c.vbox(view, css="pgs-field"), spacing=4))
        status = text("", "pgs-field-hint")

        def save() -> None:
            words = title.get_text().strip()
            if not words:
                status.set_label("Say what went wrong in a line first.")
                status.add_css_class("pgs-form-error")
                title.grab_focus()
                return
            body = {k: _buffer_text(v) for k, v in fields.items()}
            project = self.data.project if self.data else None
            reporter = project.owner.name if project and project.owner else ""
            self.forms.discard("bug")

            def done(number: int) -> None:
                self.issue_open = number
                self.load()

            c.run_async(lambda: self.create_issue(words, severity[0], place[0], body, reporter), done,
                        lambda error: c.toast(self.sheet.card, str(error), kind="error"))

        form.append(c.hbox(status, c.vbox(hexpand=True),
                           c.button("Cancel", style="plain", small=True, on_click=lambda: self.toggle_form("bug")),
                           c.button("Report it", style="key", small=True, on_click=save), spacing=8))
        GLib.idle_add(lambda: (title.grab_focus(), False)[1])
        return form

    def create_issue(self, title: str, severity: str, place: str, body: dict, reporter: str) -> int:
        store = _store(self.ctx.project_dir)
        creator = getattr(store, "create_issue", None)
        if callable(creator):
            issue = creator(title, severity=severity, place=place, reporter=reporter,
                            what_happened=body.get("what", ""), expected=body.get("expected", ""),
                            steps=body.get("steps", ""))
            return int(get(issue, "number") or 0)
        store_mod, model = c.module("core.store"), c.module("core.model")
        folder = Path(self.ctx.project_dir) / ".playground" / "issues"
        with store.locked():
            folder.mkdir(parents=True, exist_ok=True)
            number = max((int(p.stem) for p in folder.glob("*.md") if p.stem.isdigit()), default=0) + 1
            stamp = model.now_iso()
            fields = {"number": number, "title": title, "severity": severity, "status": "open", "reporter": reporter,
                      "goal": "", "created": stamp, "updated": stamp, "place": place}
            sections = [("What happened", body.get("what", "")), ("Expected", body.get("expected", "")),
                        ("Steps", body.get("steps", "")), ("Evidence", "")]
            store_mod.atomic_write_text(folder / f"{number}.md", store_mod.dump_front_matter(fields) + "\n" +
                                        store_mod.dump_sections("", sections))
        store.log(f"Reported bug #{number}: {title}", kind="issue", data={"issue": number})
        return number

    def toggle_form(self, name: str) -> None:
        if name in self.forms:
            self.forms.discard(name)
        else:
            self.forms.add(name)
        self.render(keep_scroll=True)

    # ── Stack & services ──

    def section_stack(self, data: Space) -> Gtk.Widget:
        refresh = c.button("Check now", icon="refresh-cw", style="fill", small=True, on_click=self.refresh_services,
                           tooltip="Ask each service for its latest numbers")
        add = c.button("Add a service", icon="plus", style="key", small=True,
                       on_click=lambda: self.toggle_form("service"))
        box = c.vbox(self.heading("Stack", f"What {data.project.name} is made of, what each part does, "
                                           "and how it’s doing.", *(() if self.embedded else (refresh, add))))
        if "service" in self.forms:
            box.append(self.service_form())
        box.append(self.built_with(data))
        journeys = self.journeys(data)
        if journeys:
            box.append(self.journey_map(journeys, data))
        if data.services:
            spend = sum(month_cost(data.service_usage.get(str(s.get("id")), [])) or 0.0 for s in data.services)
            budget = sum(budget_of(s) or 0.0 for s in data.services)
            box.append(self.subhead("Services", f"{len(data.services)} · {c.money(spend)} this month"
                                    + (f" of {c.money(budget)} budgeted" if budget else "")))
            box.append(self.columns(*[self.service_card(s, data) for s in data.services]))
        elif "service" not in self.forms:
            box.append(c.ListGroup(Row("No services yet",
                                       "Email, a database, file storage, hosting: tell an agent what the app uses, or add "
                                       "one yourself. Its numbers and costs are kept together here.", lead="layers",
                                       end=None if self.embedded else
                                       c.button("Add a service", style="fill", small=True,
                                                on_click=lambda: self.toggle_form("service"))), css="pgs-box"))
        if data.dependencies:
            box.append(self.subhead("Dependencies", f"{len(data.dependencies)} packages the code uses"))
            rows = [Row(str(d.get("name", "")), d.get("why") or None, lead="package",
                        end=text(str(d.get("version", "")), "pgs-row-end", "pgs-mono", wrap=False))
                    for d in data.dependencies[:40]]
            box.append(c.ListGroup(*rows, css="pgs-box"))
        return box

    def built_with(self, data: Space) -> Gtk.Widget:
        """What it's made of (read from the code) and where it's built: the project's box, a sandbox."""
        holder = c.vbox(spacing=0)
        holder.append(self.subhead("Built with", "read from the code"))
        chips = c.Adw.WrapBox(child_spacing=8, line_spacing=8)
        for tech in data.techs or []:
            words = tech.name + (f" · {tech.files} files" if tech.kind == "language" and tech.files > 1 else "")
            chip = c.hbox(c.glyph({"language": "code", "toolkit": "app-window", "storage": "database"}.get(
                tech.kind, "component"), 14), text(words, wrap=False), spacing=7, css="pgs-tech")
            chips.append(chip)
        if not data.techs:
            chips.append(text("Nothing to read yet.", "pgs-row-sub"))
        chips.set_margin_bottom(18)
        holder.append(chips)
        state = data.box
        if state is not None and state.tool:
            built = ", ".join(state.packages) if state.packages else "nothing installed yet"
            words = (f"{state.name} · {built}" if state.exists else
                     "Made the first time an agent needs to build or install something")
            remove = c.button("Remove", style="plain", small=True, on_click=self.remove_box) if state.exists else None
            row = Row("The project’s box", words, lead="package", tags=[Tag("Sandbox", "violet")], end=remove)
            row.set_tooltip_text("Agents build and install here: a container that sees only this project's folder. "
                                 "Nothing goes on your computer and no password is needed. What's installed is "
                                 "listed in box.toml, so removing it loses nothing for good.")
            group = c.ListGroup(row, css="pgs-box")
            group.set_margin_top(12)
            holder.append(group)
        return holder

    def remove_box(self) -> None:
        box = c.module("core.box")
        root, project_id = self.ctx.project_dir, getattr(self.data.project, "id", "") if self.data else ""
        c.run_async(lambda: box.remove(root, project_id),
                    lambda _r: (c.toast(self.sheet.card, "The box is gone. It’s made again when an agent needs it"),
                                self.load()),
                    lambda error: c.toast(self.sheet.card, str(error), kind="error"))

    def journeys(self, data: Space) -> list[dict]:
        """Journeys Ari keeps in project.toml's [[journeys]], else derived from each service's used_by."""
        kept = (data.project.extra.get("tables") or {}).get("journeys") or []
        if kept:
            return [j for j in kept if isinstance(j, dict)]
        order: list[str] = []
        members: dict[str, list[dict]] = {}
        for service in data.services:
            used = service.get("used_by") or []
            for journey in ([used] if isinstance(used, str) else used):
                if journey not in members:
                    order.append(journey)
                    members[journey] = []
                members[journey].append(service)
        # The journeys that pass through the most services first: those are the ones worth a map.
        ranked = sorted(order, key=lambda j: (-len(members[j]), order.index(j)))
        return [{"title": j, "services": members[j]} for j in ranked[:6]]

    def journey_map(self, journeys: list[dict], data: Space) -> Gtk.Widget:
        card = c.vbox(css="pgs-journey-map")
        self.journey_at = min(self.journey_at, len(journeys) - 1)
        card.append(text("How it fits together", "pgs-sub-title", wrap=False))
        tabs = c.Adw.WrapBox(child_spacing=6, line_spacing=6)
        tabs.set_margin_top(10)
        first = None
        for index, journey in enumerate(journeys):
            toggle = Gtk.ToggleButton(label=str(journey.get("title", "")), active=index == self.journey_at)
            toggle.add_css_class("pgs-filter")
            if first is None:
                first = toggle
            else:
                toggle.set_group(first)
            toggle.connect("toggled", lambda t, i=index: t.get_active() and self.pick_journey(i))
            tabs.append(toggle)
        card.append(tabs)
        journey = journeys[self.journey_at]
        nodes: list[tuple[str, str, str, str]] = [("users", "People", _people_words(data), ""),
                                                  ("smartphone", data.project.name, "The app", "")]
        if journey.get("steps"):
            for step in journey["steps"]:
                nodes.append((KIND_GLYPHS.get(str(step.get("kind", "")).lower(), "component"),
                              str(step.get("name", "")), str(step.get("file") or step.get("kind") or ""), ""))
        else:
            for service in journey.get("services") or []:
                nodes.append((KIND_GLYPHS.get(str(service.get("kind", "other")), "component"),
                              service.get("name") or service.get("provider", ""),
                              KIND_WORDS.get(str(service.get("kind")), service.get("provider", "")),
                              str(service.get("id", ""))))
        flow = c.hbox(spacing=0)
        for index, (icon, name, sub, service_id) in enumerate(nodes):
            if index:
                link = c.vbox(css="pgs-flow-link", valign=Gtk.Align.CENTER, hexpand=True)
                link.set_size_request(26, -1)
                flow.append(link)
            node = c.hbox(c.lead_tile(icon), c.vbox(text(name, "pgs-node-name", wrap=False),
                                                     text(sub, "pgs-node-file", wrap=False), spacing=1,
                                                     valign=Gtk.Align.CENTER),
                          spacing=10, css="pgs-flow-node")
            if service_id:
                tone, words = service_health(data.service_usage.get(service_id, []), data.statuses.get(service_id))
                if tone in ("bad", "warn"):
                    node.add_css_class("trouble")
                    node.set_tooltip_text(words)
            flow.append(node)
        scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        scroller.set_child(flow)
        scroller.set_margin_top(18)
        card.append(scroller)
        story = " → ".join(n[1] for n in nodes)
        caption = c.hbox(c.glyph("route", 15, "pgs-callout-glyph"), text(story, "pgs-row-sub"), spacing=8)
        caption.set_margin_top(14)
        card.append(caption)
        return card

    def pick_journey(self, index: int) -> None:
        if index != self.journey_at:
            self.journey_at = index
            self.render(keep_scroll=True)

    def service_card(self, service: dict, data: Space) -> Gtk.Widget:
        service_id = str(service.get("id", ""))
        snaps = data.service_usage.get(service_id, [])
        tone, health = service_health(snaps, data.statuses.get(service_id))
        card = c.vbox(spacing=0, css="pgs-service")
        name = service.get("name") or service.get("provider") or "Service"
        provider = service.get("provider") or ""
        title = c.vbox(c.hbox(text(name, "pgs-service-name", wrap=False),
                              text(provider if provider and provider != name else KIND_WORDS.get(
                                  str(service.get("kind")), ""), "pgs-service-provider", wrap=False, ellipsize=True,
                                   hexpand=True), spacing=6),
                       text(str(service.get("purpose") or ""), "pgs-service-purpose"), spacing=2, hexpand=True)
        status = c.hbox(c.shared.dot(DOT_TONES[tone]), text(health.split(" · ")[0], "pgs-service-health",
                                                             wrap=False),
                        spacing=6, valign=Gtk.Align.START)
        status.set_tooltip_text(health)
        card.append(c.hbox(service_tile(service, 40), title, status, spacing=12))

        latest = snaps[-1] if snaps else None
        metric_key, series = primary_series(snaps)
        numbers = c.hbox(spacing=12, css="pgs-service-numbers")
        metrics, when_words = month_metrics(snaps)
        headline = metric_line(metrics, 2)
        source = (latest or {}).get("source")
        note = " · ".join(filter(None, [when_words, "added by hand" if source == "manual" else
                                        (f"from {source}" if source else "")])) if latest else \
            "They arrive when Playground checks, or when an agent reports them."
        numbers.append(c.vbox(text(headline or "No numbers yet", "pgs-service-metric"),
                              text(note, "pgs-service-note"), spacing=1, hexpand=True))
        if series:
            numbers.append(c.Sparkline(series, label=f"{name}: {METRIC_WORDS.get(metric_key, metric_key)} over time"))
        card.append(numbers)

        cost, budget = month_cost(snaps), budget_of(service)
        money = c.vbox(spacing=6, css="pgs-service-money")
        words = (c.money(cost) if cost is not None else "No cost yet") + (
            f" of {c.money(budget)}" if budget else "") + " this month"
        plan = text(str(service.get("plan") or ""), "pgs-service-note", wrap=False, ellipsize=True, xalign=1)
        plan.set_tooltip_text(str(service.get("plan") or ""))
        money.append(c.hbox(text(words, "pgs-service-cost", wrap=False, ellipsize=True), plan, spacing=8))
        plan.set_hexpand(True)
        if budget and cost is not None:
            money.append(c.meter(min(1.0, cost / budget), tone="warn" if cost > 0.8 * budget else None, label=words,
                                 height=5))
        card.append(money)

        facts = c.vbox(spacing=7, css="pgs-service-facts")
        envs = service.get("environments") or {}
        for label, key in (("Stage", "stage"), ("Live", "live")):
            if isinstance(envs, dict) and envs.get(key):
                value = text(str(envs[key]), "pgs-env-value", hexpand=True, selectable=True)
                value.set_valign(Gtk.Align.START)
                label_widget = text(label, "pgs-env-label", wrap=False)
                label_widget.set_valign(Gtk.Align.START)
                facts.append(c.hbox(label_widget, value, spacing=10))
        secrets = service.get("secrets") or []
        if secrets:
            chips = c.Adw.WrapBox(child_spacing=6, line_spacing=6, hexpand=True)
            for secret in secrets:
                chip = c.hbox(c.glyph("key-round", 11), text(str(secret), wrap=False), spacing=5, css="pgs-secret-chip")
                chip.set_tooltip_text("A secret in the project vault. Only its name is shown, never its value.")
                chips.append(chip)
            facts.append(c.hbox(text("Secrets", "pgs-env-label", wrap=False), chips, spacing=10))
        if facts.get_first_child() is not None:
            card.append(facts)

        actions = c.hbox(spacing=8, css="pgs-service-actions")
        if service.get("console_url"):
            actions.append(c.button("Open console", icon="square-arrow-out-up-right", style="fill", small=True,
                                    on_click=lambda: self.open_url(str(service["console_url"]))))
        if service.get("docs_url"):
            actions.append(c.button("Docs", icon="book-open", style="plain", small=True,
                                    on_click=lambda: self.open_url(str(service["docs_url"]))))
        bugs = [i for i in data.issues if i.get("status", "open") in ("open", "fixing") and
                (str(i.get("service", "")) == service_id or
                 str(provider or name).lower() in str(i.get("title", "")).lower())]
        if bugs:
            actions.append(c.vbox(hexpand=True))
            link = c.button(f"Bug #{bugs[0].get('number')}", icon="circle-alert", style="plain", small=True,
                            on_click=lambda b=bugs[0]: self.open_issue(b))
            link.add_css_class("pgs-bug-link")
            actions.append(link)
        if actions.get_first_child() is not None:
            card.append(actions)
        return card

    def service_form(self) -> Gtk.Widget:
        form = c.vbox(spacing=10, css="pgs-form-card")
        form.set_margin_bottom(16)
        form.append(c.segmented([("ari", "Tell Ari"), ("form", "Fill it in")], self.service_mode, label="How to add it",
                                on_change=lambda k: (setattr(self, "service_mode", k), self.render(keep_scroll=True))))
        form.get_last_child().set_halign(Gtk.Align.START)
        if self.service_mode == "ari":
            entry = Gtk.Entry(hexpand=True,
                              placeholder_text="We send emails with Resend, and photos live in Cloudflare R2")
            entry.update_property([Gtk.AccessibleProperty.LABEL], ["Tell Ari about the service"])
            entry.connect("activate", lambda e: self.tell_ari(e.get_text()))
            form.append(c.hbox(c.glyph("message-circle", 16), entry,
                               c.button("Send", style="key", small=True, on_click=lambda: self.tell_ari(entry.get_text())),
                               spacing=8, css="pgs-field"))
            form.append(text("Ari adds it to the stack, asks for any key it needs straight into the vault (never "
                             "into a file), and connects its numbers when it can.", "pgs-field-hint"))
            GLib.idle_add(lambda: (entry.grab_focus(), False)[1])
            return form
        entries: dict[str, Gtk.Entry] = {}
        specs = (("name", "Name", "Resend"), ("provider", "Provider", "Resend"),
                 ("purpose", "What it does, in plain words", "Sends booking confirmations and reminders"),
                 ("console_url", "Console address", "https://resend.com/emails"), ("plan", "Plan", "Pro"),
                 ("monthly_budget", "Monthly budget", "$20"), ("secrets", "Vault secrets it uses", "RESEND_API_KEY"),
                 ("stage", "On Stage", "Sandbox key, test domain"), ("live", "On Live", "kiln.example"))
        grid = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True)
        row = 0
        column = 0
        for key, label, hint in specs:
            field = c.kit.TextField(label, placeholder=hint) if c.kit else Gtk.Entry(placeholder_text=hint)
            entries[key] = field.entry if hasattr(field, "entry") else field
            if key == "purpose":
                row += 1 if column else 0
                grid.attach(field, 0, row, 2, 1)
                row, column = row + 1, 0
                continue
            grid.attach(field, column, row, 1, 1)
            column = 1 - column
            row += 1 if column == 0 else 0
        kinds = list(KIND_WORDS)
        kind = Gtk.DropDown.new_from_strings([KIND_WORDS[k] for k in kinds])
        kind.update_property([Gtk.AccessibleProperty.LABEL], ["Kind of service"])
        form.append(c.hbox(text("Kind", "pgs-opt-sub", wrap=False), kind, spacing=10))
        form.append(grid)
        status = text("", "pgs-field-hint")

        def save() -> None:
            values = {k: e.get_text().strip() for k, e in entries.items()}
            problem = ("Give it a name." if not values["name"] else
                       "The console address starts with https://." if values["console_url"] and
                       not values["console_url"].startswith("https://") else "")
            if problem:
                status.set_label(problem)
                status.add_css_class("pgs-form-error")
                return
            values["kind"] = kinds[kind.get_selected()]
            self.forms.discard("service")
            c.run_async(lambda: self.add_service(values), lambda _r: self.load(),
                        lambda error: c.toast(self.sheet.card, str(error), kind="error"))

        form.append(c.hbox(status, c.vbox(hexpand=True),
                           c.button("Cancel", style="plain", small=True, on_click=lambda: self.toggle_form("service")),
                           c.button("Add it", style="key", small=True, on_click=save), spacing=8))
        return form

    def add_service(self, values: dict) -> None:
        store = _store(self.ctx.project_dir)
        paths, model = c.module("core.paths"), c.module("core.model")
        project = self.data.project if self.data else None
        service = {"id": paths.slugify(values["name"], 32), "name": values["name"], "kind": values["kind"],
                   "provider": values["provider"] or values["name"], "purpose": values["purpose"], "used_by": [],
                   "environments": {k: values[k] for k in ("stage", "live") if values.get(k)},
                   "console_url": values["console_url"], "plan": values["plan"],
                   "monthly_budget": budget_of({"monthly_budget": values["monthly_budget"]}) or 0,
                   "secrets": [s for s in re.split(r"[,\s]+", values["secrets"]) if s],
                   "usage_source": "manual", "added_by": project.owner.name if project and project.owner else "",
                   "added": model.now_iso()}
        adder = getattr(store, "add_service", None)
        if callable(adder) and hasattr(model, "Service"):
            adder(model.Service.from_dict(service))
            store.log(f"Added {service['name']} to the stack", kind="service", data={"service": service["id"]})
            return
        store_mod = c.module("core.store")
        path = Path(self.ctx.project_dir) / ".playground" / "stack.toml"
        with store.locked():
            try:
                document = tomllib.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                document = {}
            services = [s for s in document.get("services") or [] if isinstance(s, dict)]
            taken = {s.get("id") for s in services}
            base, n = service["id"], 2
            while service["id"] in taken:
                service["id"], n = f"{base}-{n}", n + 1
            services.append({k: v for k, v in service.items() if v not in ("", [], {})})
            document["services"] = services
            store_mod.atomic_write_text(path, "# What this project is made of. Names and purposes only; never "
                                              "secrets.\n" + store_mod.dump_toml(document))
        store.log(f"Added {service['name']} to the stack", kind="service", data={"service": service["id"]})
        store.bus.publish("project.changed", project=str(store.root), what="stack")

    def refresh_services(self) -> None:
        services = c.module("core.services")
        if services is None or not hasattr(services, "refresh"):
            c.toast(self.sheet.card, "Checking services needs the services module, which isn’t installed",
                    kind="warning")
            return
        c.toast(self.sheet.card, "Checking each service…", kind="opening")
        project_dir = self.ctx.project_dir
        c.run_async(lambda: services.refresh(project_dir), lambda _r: self.load(),
                    lambda error: c.toast(self.sheet.card, f"Couldn’t check them all: {error}", kind="warning"))

    def tell_ari(self, words: str) -> None:
        words = words.strip()
        if not words:
            return
        store, model = _store(self.ctx.project_dir), c.module("core.model")

        def send() -> None:
            store.append_thread(model.GENERAL_CHAT_ID, model.ThreadEntry(kind="message", role="person", text=words))

        self.forms.discard("service")
        c.run_async(send, lambda _r: (c.toast(self.sheet.card, "Sent to Ari in Talk to Ari", kind="sent"),
                                      self.render(keep_scroll=True)),
                    lambda error: c.toast(self.sheet.card, str(error), kind="error"))

    # ── Usage & money ──

    def section_money(self, data: Space) -> Gtk.Widget:
        settings = c.button("Agents and caps", icon="gauge", style="fill", small=True,
                            on_click=lambda: c.open_sheet(self.window, "usage", **self.ctx.options))
        box = c.vbox(self.heading("Usage & money", "Models and services together, this month, against their budgets "
                                                   "and caps.", settings))
        spend, budget = self.month_totals(data)
        models_total = float(data.models.get("total") or 0)
        box.append(self.columns(
            stat_tile("This month", c.money(spend), f"of {c.money(budget)} in budgets" if budget else "no budgets set"),
            stat_tile("Services", c.money(round(spend - models_total, 2)), c.shared.plural(len(data.services), "service")),
            stat_tile("Models", c.money(models_total), "shared keys and Ari credits"), wide=3))
        rows = []
        for service in data.services:
            snaps = data.service_usage.get(str(service.get("id")), [])
            cost, limit = month_cost(snaps), budget_of(service)
            row = Row(service.get("name") or "", metric_line(month_metrics(snaps)[0]) or
                      (service.get("plan") or None), lead=service_tile(service, 34),
                      end=text((c.money(cost) if cost is not None else "Not recorded") +
                               (f" of {c.money(limit)}" if limit else ""), "pgs-row-end", "strong", wrap=False))
            if limit and cost is not None:
                row.words.append(c.meter(min(1.0, cost / limit), tone="warn" if cost > 0.8 * limit else None,
                                         width=200, height=5))
            rows.append(row)
        for line in data.models.get("lines") or []:
            rows.append(Row(line["label"], line.get("note") or None, lead="key-round",
                            end=text(line["amount_text"], "pgs-row-end", "strong", wrap=False)))
        box.append(self.subhead("Where it goes"))
        box.append(c.ListGroup(*rows or [Row("Nothing spent yet", "Services and shared models show here as they’re "
                                                                  "used.", lead="wallet")], css="pgs-box"))
        note = callout("Personal plans aren’t counted here. Their owners pay for them, and what they have left is in "
                       "Agents and caps.", icon="lock", tone="rule")
        note.set_margin_top(12)
        box.append(note)
        return box

    # ── Analytics ──

    def section_analytics(self, data: Space) -> Gtk.Widget:
        period = c.segmented([("7", "7 days"), ("30", "30 days")], str(self.analytics_days), label="Period",
                             on_change=lambda k: (setattr(self, "analytics_days", int(k)),
                                                  self.render(keep_scroll=True)))
        box = c.vbox(self.heading("Analytics", f"How people use the live {data.project.name}.", period))
        days = [r for r in data.analytics if _DAY.match(str(r.get("period", "")))] or data.analytics
        if not days:
            box.append(c.ListGroup(Row("No numbers yet", "Once the app is live, a daily count of visitors, sessions, "
                                                         "top pages and errors is kept here.", lead="chart-column"),
                                   css="pgs-box"))
            return box
        window = days[-self.analytics_days:]
        before = days[-2 * self.analytics_days:-self.analytics_days]

        def total(rows: list[dict], key: str) -> float:
            return sum(float(r.get(key) or 0) for r in rows)

        tiles = []
        for label, key in (("Visitors", "visitors"), ("Sessions", "sessions"), ("Errors", "errors")):
            now, then = total(window, key), total(before, key)
            note = f"in {len(window)} days"
            if before and then:
                delta = (now - then) / then * 100
                note = f"{'↑' if delta >= 0 else '↓'} {abs(delta):.0f}% on the {len(before)} days before"
            tiles.append(stat_tile(label, c.number_words(int(now)), note,
                                   tone="bad" if key == "errors" and then and now > then else None))
        box.append(self.columns(*tiles, wide=3))
        chart_card = c.vbox(css="pgs-box pgs-chart-card")
        busiest = max(window, key=lambda r: float(r.get("visitors") or 0))
        when = c.shared.parse_ts(busiest.get("period") if _DAY.match(str(busiest.get("period", ""))) else busiest.get("ts"))
        chart_card.append(c.hbox(text("Visitors a day", "pgs-sub-title", wrap=False),
                                 text(f"Busiest: {when.strftime('%A')} {when.day} {when.strftime('%b')}" if when
                                      else "", "pgs-sub-aside", wrap=False), spacing=8))
        labels = []
        for row in window:
            moment = c.shared.parse_ts(row.get("period") if _DAY.match(str(row.get("period", ""))) else row.get("ts"))
            labels.append((moment.strftime("%a") if len(window) <= 8 else f"{moment.day} {moment.strftime('%b')}")
                          if moment else "")
        chart = c.AreaChart([r.get("visitors") or 0 for r in window], labels,
                            label=f"Visitors a day over {len(window)} days")
        chart.set_margin_top(10)
        chart_card.append(chart)
        chart_card.set_margin_top(12)
        box.append(chart_card)

        pages: dict[str, float] = {}
        for row in window:
            for page in row.get("top_pages") or []:
                if isinstance(page, dict):
                    key = str(page.get("path") or page.get("page") or "")
                    pages[key] = pages.get(key, 0) + float(page.get("visits") or page.get("views") or 0)
                elif isinstance(page, (list, tuple)) and len(page) >= 2:
                    pages[str(page[0])] = pages.get(str(page[0]), 0) + float(page[1] or 0)
        top_card = c.vbox(text("Top pages", "pgs-sub-title", wrap=False), spacing=10, css="pgs-box pgs-chart-card")
        whole = sum(pages.values()) or 1
        for path, visits in sorted(pages.items(), key=lambda p: -p[1])[:6]:
            share = visits / whole
            name = text(path or "/", "pgs-mono", wrap=False, ellipsize=True, width_chars=16, max_width_chars=16)
            top_card.append(c.hbox(name, c.meter(share, tone="accent", height=6),
                                   text(f"{share * 100:.0f}%", "pgs-row-end", wrap=False, width_chars=4, xalign=1),
                                   spacing=12))
        if not pages:
            top_card.append(text("Page counts arrive with the next daily snapshot.", "pgs-row-sub"))
        errors_card = c.vbox(text("Errors", "pgs-sub-title", wrap=False), spacing=10, css="pgs-box pgs-chart-card")
        errors = [r for r in reversed(window) if float(r.get("errors") or 0)]
        for row in errors[:5]:
            moment = c.shared.parse_ts(row.get("period") if _DAY.match(str(row.get("period", ""))) else row.get("ts"))
            errors_card.append(c.hbox(text(c.shared.day_words(moment) if moment else "", "pgs-row-sub", wrap=False,
                                           hexpand=True),
                                      text(c.shared.plural(int(row.get("errors") or 0), "error"), "pgs-row-end",
                                           wrap=False), spacing=8))
        if not errors:
            errors_card.append(text("No errors in this period.", "pgs-row-sub"))
        if window[-1].get("source"):
            errors_card.append(text(f"Counted by {window[-1]['source']}", "pgs-field-hint"))
        bottom = self.columns(top_card, errors_card)
        bottom.set_margin_top(12)
        box.append(bottom)
        return box

    # ── Knowledge, Decisions, Brief ──

    def note_form(self) -> Gtk.Widget:
        title = Gtk.Entry(placeholder_text="Classes hold eight people", hexpand=True)
        title.update_property([Gtk.AccessibleProperty.LABEL], ["Note title"])
        body = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, top_margin=8, bottom_margin=8)
        body.update_property([Gtk.AccessibleProperty.LABEL], ["Note"])
        body.set_size_request(-1, 72)

        def save() -> None:
            words = title.get_text().strip()
            if not words:
                title.grab_focus()
                return
            model, store = c.module("core.model"), _store(self.ctx.project_dir)
            owner = self.data.project.owner if self.data else None
            note = model.KnowledgeNote(slug="", title=words, body=_buffer_text(body),
                                       added_by=owner.name if owner else "")
            self.forms.discard("note")
            c.run_async(lambda: store.write_knowledge(note), lambda _r: self.load(),
                        lambda error: c.toast(self.sheet.card, str(error), kind="error"))

        form = c.vbox(c.vbox(title, css="pgs-field"), c.vbox(body, css="pgs-field"),
                      c.hbox(c.vbox(hexpand=True),
                             c.button("Cancel", style="plain", small=True, on_click=lambda: self.toggle_form("note")),
                             c.button("Add note", style="key", small=True, on_click=save), spacing=8), spacing=8)
        form.set_margin_top(12)
        form.set_margin_bottom(12)
        GLib.idle_add(lambda: (title.grab_focus(), False)[1])
        return form

    def section_decisions(self, data: Space) -> Gtk.Widget:
        box = c.vbox(self.heading("Decisions", "What was decided, by whom, and why."))
        rows = []
        for pending in data.pending:
            rows.append(Row(pending.question, f"Asked by {pending.asked_by or 'Ari'} · waiting for you", lead="hand",
                            tags=[Tag("Needs you", "amber")],
                            on_activate=(lambda p=pending: self.open_goal(p.goal)) if pending.goal else None))
        for decision in reversed(data.decisions):
            who = " · ".join(filter(None, [decision.who, _day(decision.date)]))
            sub = " · ".join(filter(None, [who, decision.chosen if decision.chosen not in decision.title else "",
                                           decision.why]))
            rows.append(Row(decision.title, sub or None, lead="check"))
        if not rows:
            rows.append(Row("No decisions yet", "When something is yours to decide, Ari asks in the conversation and it’s "
                                                "kept here.", lead="hand"))
        box.append(c.ListGroup(*rows, css="pgs-box"))
        return box

    def section_brief(self, data: Space) -> Gtk.Widget:
        brief = data.brief
        edit = c.button("Done" if self.editing_brief else "Edit", icon="check" if self.editing_brief else "pencil",
                        style="fill", small=True, on_click=self.toggle_brief)
        box = c.vbox(self.heading("Brief & recipe", "One page: what it is and what good looks like. Ari updates it "
                                                    "when a decision changes it.", edit))
        fields = [("What it is", brief.summary, "summary"), ("Who it’s for", brief.audience, "audience"),
                  ("Where", ", ".join(brief.platforms), "platforms"),
                  ("Good looks like", " ".join(brief.good_looks_like), "good"), ("Not for now", brief.not_now, "not")]
        card = c.vbox(css="pgs-box")
        if self.editing_brief:
            self.brief_entries = {}
            grid = Gtk.Grid(column_spacing=12, row_spacing=8)
            grid.set_margin_top(10)
            grid.set_margin_bottom(10)
            for row, (label, value, key) in enumerate(fields):
                name = text(label, "pgs-kv-key")
                name.set_size_request(112, -1)
                entry = Gtk.Entry(text=value or "", hexpand=True)
                entry.update_property([Gtk.AccessibleProperty.LABEL], [label])
                entry.connect("activate", lambda _e: self.toggle_brief())
                self.brief_entries[key] = entry
                grid.attach(name, 0, row, 1, 1)
                grid.attach(entry, 1, row, 1, 1)
            card.append(grid)
        else:
            shown = [(label, value) for label, value, _k in fields if value]
            card.append(c.kv(shown) if shown else text("The brief is empty. Describe what you’re making in the "
                                                       "conversation and Ari writes it here.", "pgs-row-sub"))
        box.append(card)
        recipe = data.project.recipe
        box.append(self.subhead("The recipe", recipe.summary))
        rows = []
        for part in recipe.parts:
            row = Row(part.label, part.why or None, lead=KIND_GLYPHS.get(part.label.lower(), "layers"))
            row.title.set_markup(f"{c.escape(part.label)} <span alpha='60%'>· {c.escape(part.choice)}</span>")
            rows.append(row)
        if recipe.runs_on:
            rows.append(Row("Runs on", recipe.runs_on, lead="server"))
        box.append(c.ListGroup(*rows or [Row("No recipe yet", "Ari writes it when the project starts.", lead="layers")],
                               css="pgs-box"))
        return box

    def toggle_brief(self) -> None:
        if not self.editing_brief:
            self.editing_brief = True
            self.render(keep_scroll=True)
            return
        values = {k: e.get_text().strip() for k, e in getattr(self, "brief_entries", {}).items()}
        self.editing_brief = False
        model, store = c.module("core.model"), _store(self.ctx.project_dir)

        def write() -> None:
            brief = store.read_brief()
            brief.set_section(model.BRIEF_SUMMARY, values.get("summary", ""))
            brief.audience = values.get("audience", "")
            brief.platforms = [p.strip() for p in values.get("platforms", "").split(",") if p.strip()]
            brief.set_section(model.BRIEF_GOOD, values.get("good", ""))
            brief.set_section(model.BRIEF_NOT_NOW, values.get("not", ""))
            store.write_brief(brief)
            store.log("Edited the brief", kind="note")

        c.run_async(write, lambda _r: self.load(), lambda error: c.toast(self.sheet.card, str(error), kind="error"))

    # ── Places, People ──

    def section_places(self, data: Space) -> Gtk.Widget:
        manage = c.button("Manage places", icon="server", style="fill", small=True,
                          on_click=lambda: c.open_sheet(self.window, "places", **self.ctx.options))
        box = c.vbox(self.heading("Places", "Where it lives and runs, and who can get in.", manage))
        rows = []
        for place in data.places:
            role = place.get("role", "development")
            icon = "monitor" if place.get("kind") != "ssh" else {"production": "globe", "preview": "smartphone"}.get(
                role, "server")
            tags = [Tag(role.capitalize(), {"production": "red", "development": "run"}.get(role))]
            if place.get("is_home"):
                tags.append(Tag("Home", "good"))
            rows.append(Row(place.get("name", ""), place.get("status") or None, lead=icon, tags=tags))
        box.append(c.ListGroup(*rows, css="pgs-box"))
        return box

    def section_people(self, data: Space) -> Gtk.Widget:
        """People and agents, each with what they're on right now (claimed tasks, conversations working)."""
        from .people import ROLE_WORDS
        manage = c.button("Manage people", icon="users", style="fill", small=True,
                          on_click=lambda: c.open_sheet(self.window, "people", **self.ctx.options))
        agents = self._team_agents(data)
        people = list(data.project.members)
        lede = f"{c.shared.plural(len(people), 'person', 'people')} · {c.shared.plural(len(agents), 'agent')}"
        box = c.vbox(self.heading("Team", lede, *(() if self.embedded else (manage,))))
        box.append(self.subhead("People", "what each person can do"))
        cards = []
        for member in people:
            mine = [t for t in data.tasks if t.is_open and (t.assignee == member.id or
                                                             t.claimed_by == f"person:{member.id}")]
            working = [g for g in data.goals if g.owner == member.id and g.status.value in ("working", "needs-you")]
            doing = mine[0].title if mine else (working[0].title if working else "")
            cards.append(_team_card(c.shared.person_face(member.name or member.id, 40), member.name or member.id,
                                    [Tag(member.role.capitalize())], ROLE_WORDS.get(member.role, ""),
                                    f"On “{doing}”" if doing else "Nothing underway",
                                    busy=bool(doing)))
        box.append(self.columns(*cards, wide=3))
        box.append(self.subhead("Agents", "the accounts that work here, and where"))
        if not agents:
            box.append(c.ListGroup(Row("No agents yet", "Add one from the conversation’s agent picker: Claude, "
                                       "Codex, Gemini, or one on another computer.", lead="sparkles"), css="pgs-box"))
            return box
        cards = []
        picker = c.module("app.sheets.picker")
        for conn, goals, tasks in agents:
            where = {"ari": "Built into Playground", "teammate": "On their computer",
                     "elsewhere": "Not set up on this computer"}.get(getattr(conn, "where", ""), "")
            if not where and picker is not None:
                where = picker.where_words(conn, data.project)[1]
            if tasks:
                doing, busy = f"On {tasks[0].id} · {tasks[0].title}", True
            elif goals:
                doing, busy = f"In “{goals[0].title}”", True
            else:
                doing, busy = "Free", False
            mark = c.mark(conn, size=40)
            cards.append(_team_card(mark, conn.label, [], where, doing, busy=busy))
        box.append(self.columns(*cards, wide=3))
        return box

    def _team_agents(self, data: Space) -> list:
        """(connection, its working conversations, its claimed tasks) for every agent account here."""
        connections = c.module("agents.connections")
        try:
            listed = list(connections.ConnectionStore().list()) if connections is not None else []
        except Exception:  # noqa: BLE001
            listed = []
        # Agents this project has seen (its conversations, its claims) count too: a teammate's, or one set up
        # on another computer, isn't in this computer's list.
        from types import SimpleNamespace
        from .areas_pages import who_words
        known = {conn.id for conn in listed}
        seen = [g.connection for g in data.goals if g.connection] + \
            [t.claimed_by.split(":", 1)[-1].split("@", 1)[0] for t in data.tasks if t.claimed_by and
             not t.claimed_by.startswith("person:")]
        names = {m.id: m.name for m in data.project.members}
        for key in dict.fromkeys(seen):
            if key in known:
                continue
            known.add(key)
            owner, _dash, program = key.rpartition("-")
            program = program or key
            label = who_words(f"agent:{program}", {})
            if owner in names:
                label = f"{names[owner].split()[0]}’s {label}"     # a teammate's agent: "Priya’s Claude"
            listed.append(SimpleNamespace(id=key, label=label, provider=program, kind="", extra={},
                                          where="ari" if key == "ari" else ("teammate" if owner in names
                                                                           else "elsewhere")))
        out = []
        for conn in listed:
            goals = [g for g in data.goals if g.connection == conn.id and g.status.value == "working"]
            tasks = [t for t in data.tasks if t.is_open and
                     t.claimed_by.split(":", 1)[-1].split("@", 1)[0] == conn.id]
            out.append((conn, goals, tasks))
        out.sort(key=lambda item: (not (item[1] or item[2]), item[0].label.lower()))
        return out

    # ── History ──

    def section_history(self, data: Space) -> Gtk.Widget:
        box = c.vbox(self.heading("Activity", "Everything that happened, with restore points. Going back puts the "
                                              "code back, and the data too when a copy was saved and you choose it."))
        channel = self.channel_digest(data)
        if channel is not None:
            box.append(channel)
        going_back = self.__dict__.get("_rollback")
        if going_back:
            box.append(self.rollback_note(going_back))
        filters = c.Adw.WrapBox(child_spacing=6, line_spacing=6)
        first = None
        for key, name in HISTORY_FILTERS:
            toggle = Gtk.ToggleButton(label=name, active=key == self.history_filter)
            toggle.add_css_class("pgs-filter")
            if first is None:
                first = toggle
            else:
                toggle.set_group(first)
            toggle.connect("toggled", lambda t, k=key: t.get_active() and self.filter_history(k))
            filters.append(toggle)
        filters.set_margin_bottom(12)
        box.append(filters)
        wanted = HISTORY_KINDS.get(self.history_filter)
        entries = [e for e in data.activity if wanted is None or e.kind in wanted]
        points = self.restore_info(data)
        live = next((n for n, p in points.items() if p.get("live")), None)
        rows = [self.history_row(e, live if live is not None else 0) for e in entries[:150]]
        box.append(c.ListGroup(*rows or [Row("Nothing here yet", None, lead="history")], css="pgs-box"))
        return box

    def restore_info(self, data: Space) -> dict:
        """Restore point id → what going back to it can cover (core.places), read once per activity change.

        Ids are per member and device, so two people's point 3 are different points."""
        key = (len(data.activity), data.activity[0].ts if data.activity else "")
        cached = self.__dict__.get("_restore_info")
        if cached and cached[0] == key:
            return cached[1]
        places = c.module("core.places")
        points: dict = {}
        if places is not None and hasattr(places, "restore_points") and self.ctx.project_dir is not None:
            try:
                points = {p.get("id") or p["number"]: p for p in places.restore_points(self.ctx.project_dir)}
            except Exception:  # noqa: BLE001 - History still shows without its restore actions
                c.log.warning("restore points couldn't be read", exc_info=True)
        self.__dict__["_restore_info"] = (key, points)
        return points

    def point_info(self, entry) -> dict | None:
        """The restore point a release entry made: by its id, or, for entries older than ids, by number."""
        points = self.restore_info(self.data)
        restore_id = entry.data.get("restore_id")
        if restore_id:
            return points.get(restore_id)
        number = entry.data.get("restore_point")
        if not str(number).isdigit():
            return None
        return next((p for p in points.values() if p.get("number") == int(number)), None)

    def history_row(self, entry, newest) -> Gtk.Widget:
        """One line of History. ``newest`` is the live restore point (None in Overview: no actions there)."""
        when = c.shared.parse_ts(entry.ts)
        when_text = f"{c.shared.day_words(when)}, {c.shared.time_words(when)}" if when else ""
        point = entry.data.get("restore_point") if entry.kind == "release" else None
        state = str(entry.data.get("state") or "live")
        end = None
        if newest is not None and point and state == "live" and self.data is not None:
            info = self.point_info(entry)
            if info is not None and info.get("live"):
                end = text("Live now", "pgs-row-end", wrap=False)
            elif info is not None and info.get("code"):
                end = c.button("Go back to this version", icon="rotate-ccw", style="fill", small=True,
                               on_click=lambda: self.go_back(entry))
        group = next((k for k, kinds in HISTORY_KINDS.items() if entry.kind in kinds), entry.kind)
        detail = str(entry.data.get("detail") or "")
        label = f"Restore point {point}" if point else ""
        if state == "failed":
            label = ""
        sub = " · ".join(filter(None, [when_text, label, detail if detail != label else ""]))
        return Row(entry.summary, sub or None, lead=HISTORY_GLYPHS.get(group, "history"), end=end)

    def filter_history(self, key: str) -> None:
        if key != self.history_filter:
            self.history_filter = key
            self.render(keep_scroll=True)

    def go_back(self, entry) -> None:
        """Put an earlier restore point back on production, after an honest word about the data."""
        # Shared projects name restore points per person and computer; the id is
        # exact even when two teammates saved the same number at once.
        point = entry.data.get("restore_id") or int(entry.data.get("restore_point"))
        places = c.module("core.places")
        if places is None or not hasattr(places, "rollback"):
            return
        plan = places.rollback_plan(self.ctx.project_dir, point)
        if not plan.get("ok"):
            c.toast(self.sheet.card, plan.get("problem") or "That version can’t be put back from here.",
                    kind="warning")
            return
        place = plan["place"].get("name") or "Production"

        def moment(ts) -> str:
            when = c.shared.parse_ts(ts)
            if when is None:
                return "then"
            day = c.shared.day_words(when)
            return f"{day.lower() if day in ('Today', 'Yesterday') else day} at {c.shared.time_words(when)}"

        made_words = moment(plan["point"].get("ts"))
        data = plan.get("data")
        option = None
        if data:
            saved_words = moment(data.get("ts"))
            words = (f"{place} goes back to the version released {made_words}. The data stays as it is now. If you "
                     f"put the data back too, anything people added or changed since {saved_words} is lost. "
                     "Playground saves today’s data first, either way.")
            option = f"Also put the data back to {saved_words}"
        elif plan.get("data_saved"):
            words = (f"{place} goes back to the version released {made_words}. The data stays as it is now: this "
                     "computer has no copy of the data from that version, so only the code goes back.")
        else:
            words = (f"{place} goes back to the version released {made_words}. The data stays as it is now, because "
                     "Playground doesn’t save copies of this project’s data yet. If that version expects the data "
                     "to look different, it may not work right.")
        trust = not plan.get("trusted", True)
        if trust:
            words += "\n\nGoing back runs:\n" + "\n".join(f"{where}: {what}" for where, what in plan["commands"])

        def confirm(also_data: bool = False) -> None:
            self.run_rollback(point, place, bool(also_data), trust)

        if c.kit is not None:
            c.kit.DestructiveDialog.ask(self.sheet.card, title=f"Go back to restore point {point}?", body=words,
                                        action="Go back", icon="rotate-ccw", option=option, on_confirm=confirm)
        else:
            confirm(False)

    def run_rollback(self, point: int | str, place: str, also_data: bool, trust: bool) -> None:
        places = c.module("core.places")
        project_dir = self.ctx.project_dir
        project = self.data.project if self.data is not None else self.ctx.project
        from .review import person_id
        me = person_id(self.window, project)
        state = {"point": point, "place": place, "phase": "running", "data": also_data, "trust": trust,
                 "label": f"Getting restore point {point} ready", "message": ""}
        self.__dict__["_rollback"] = state

        def on_step(_step: str, step_state: str, label: str, detail: str) -> None:
            def show() -> bool:
                if step_state == "running":
                    state["label"] = label
                    self.render(keep_scroll=True)
                return False
            GLib.idle_add(show)

        def run() -> dict:
            ticket = places.authorize_rollback(project_dir, point, me)
            return places.rollback(project_dir, point, ticket, approved_by=me, restore_data=also_data,
                                   trust_recipe=trust, on_step=on_step)

        def done(result: dict) -> None:
            health = (result.get("health") or {}).get("message", "")
            state.update(phase="done", message=" ".join(filter(None, [
                "The code and the data went back." if result.get("data_restored") else
                "The code went back; the data stayed as it was.", health])))
            self.load()

        def failed(error: BaseException) -> None:
            state.update(phase="failed", message=str(error), retry=getattr(error, "retry", True))
            self.load()

        self.render(keep_scroll=True)
        c.run_async(run, done, failed)

    def rollback_note(self, state: dict) -> Gtk.Widget:
        point, place = state["point"], state["place"]
        if state["phase"] == "running":
            line = c.hbox(c.shared.Spinner(16), text(f"Going back to restore point {point} on {place}: "
                                                     f"{state['label'].lower()}…", "pgs-callout-text", hexpand=True),
                          spacing=10, css="pgs-callout info")
        elif state["phase"] == "done":
            line = c.callout(c.escape(f"Restore point {point} is live again on {place}. {state['message']}"),
                             icon="check", tone="good")
        else:
            line = c.callout(c.escape(f"Going back to restore point {point} stopped. {state['message']}"),
                             icon="triangle-alert", tone="warn")
            actions = c.hbox(spacing=8)
            if state.get("retry", True):
                actions.append(c.button("Try again", icon="rotate-ccw", style="fill", small=True,
                                        on_click=lambda: self.run_rollback(point, place, state["data"],
                                                                           state["trust"])))
            actions.append(c.button("Dismiss", style="plain", small=True, on_click=self.dismiss_rollback))
            line.append(actions)
        if state["phase"] == "done":
            line.append(c.button("Dismiss", style="plain", small=True, on_click=self.dismiss_rollback))
        line.set_margin_bottom(14)
        return line

    def dismiss_rollback(self) -> None:
        self.__dict__.pop("_rollback", None)
        self.render(keep_scroll=True)

    # ── Code ──

    def section_code(self, data: Space) -> Gtk.Widget:
        box = c.vbox(self.heading("Code", "You never need it, but it’s always one step away."))
        door = Gtk.Button()
        door.add_css_class("pgs-code-door")
        door.set_child(c.hbox(c.glyph("terminal", 20),
                              c.vbox(text("Open the files, the changes and the terminal", "pgs-code-door-title"),
                                     text(self.ctx.display_path, "pgs-code-door-path", wrap=False, ellipsize=True),
                                     spacing=2, hexpand=True),
                              c.glyph("arrow-right", 20), spacing=14))
        door.update_property([Gtk.AccessibleProperty.LABEL], ["Open the files, the changes and the terminal"])
        door.connect("clicked", lambda _b: c.open_sheet(self.window, "code", **self.ctx.options))
        box.append(door)
        if data.dependencies:
            box.append(self.subhead("Dependencies", f"{len(data.dependencies)} packages"))
            box.append(c.ListGroup(*[Row(str(d.get("name", "")), d.get("why") or None, lead="package",
                                         end=text(str(d.get("version", "")), "pgs-row-end", "pgs-mono", wrap=False))
                                     for d in data.dependencies[:40]], css="pgs-box"))
        return box

    # ── outside ──

    def show_in_filer(self) -> None:
        if self.ctx.project_dir is not None:
            Gtk.FileLauncher.new(Gio.File.new_for_path(str(self.ctx.project_dir))).launch(self.window, None, None)

    def open_url(self, url: str) -> None:
        if url.startswith(("https://", "http://")):
            Gtk.UriLauncher.new(url).launch(self.window, None, None)


# ── small parts ──


def _team_card(lead: Gtk.Widget, name: str, tags: list, what: str, doing: str, *, busy: bool) -> Gtk.Widget:
    lead.set_valign(Gtk.Align.START)
    head = c.hbox(lead, c.vbox(c.hbox(text(name, "pgs-kpart-title", wrap=False, ellipsize=True), *tags, spacing=8),
                               text(what, "pgs-row-sub"), spacing=2, hexpand=True,
                               valign=Gtk.Align.CENTER), spacing=12)
    now = c.hbox(c.vbox(css="pgs-team-dot" + (" busy" if busy else ""), valign=Gtk.Align.CENTER),
                 text(doing, "pgs-row-sub", wrap=False, ellipsize=True, hexpand=True), spacing=8)
    return c.vbox(head, now, spacing=12, css="pgs-team-card")


def stat_tile(label: str, value: str, note: str, *, on_click: Callable[[], None] | None = None,
              tone: str | None = None) -> Gtk.Widget:
    inner = c.vbox(text(label, "pgs-stat-label", wrap=False), text(value, "pgs-stat-value", wrap=False),
                   text(note, "pgs-stat-note", wrap=False, ellipsize=True), spacing=2)
    widget: Gtk.Widget = inner
    if on_click is not None:
        widget = Gtk.Button(child=inner)
        widget.add_css_class("activatable")
        widget.update_property([Gtk.AccessibleProperty.LABEL], [f"{label}: {value}, {note}"])
        widget.connect("clicked", lambda _b: on_click())
    widget.add_css_class("pgs-stat")
    if tone:
        widget.add_css_class(tone)
    return widget


def service_tile(service: dict, size: int) -> Gtk.Widget:
    """A service's glyph on a tile tinted by its kind (never the company's logo)."""
    kind = str(service.get("kind") or "other")
    tile = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, hexpand=False, vexpand=False)
    tile.add_css_class("pgs-service-tile")
    tile.add_css_class(f"kind-{kind}")
    tile.set_size_request(size, size)
    image = c.glyph(KIND_GLYPHS.get(kind, "component"), max(14, round(size * 0.46)))
    image.set_hexpand(True)
    image.set_halign(Gtk.Align.CENTER)
    tile.append(image)
    tile.set_tooltip_text(KIND_WORDS.get(kind, "Service"))
    return tile


def _state_lead(status: str) -> Gtk.Widget:
    tile = c.vbox(css="pgs-lead", valign=Gtk.Align.CENTER, hexpand=False)
    glyph = c.shared.StateGlyph(status, 18)
    glyph.set_vexpand(True)
    tile.append(glyph)
    return tile


def _buffer_text(view: Gtk.TextView) -> str:
    buffer = view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).strip()


def _plain(words: str) -> str:
    """Markdown lists and emphasis read as plain lines."""
    lines = []
    for line in (words or "").splitlines():
        if line.strip().startswith("```"):
            continue                       # a code fence's marks; the lines inside read as they are
        stripped = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "• ", line)
        lines.append(re.sub(r"[*_`]{1,2}([^*_`]+)[*_`]{1,2}", r"\1", stripped))
    return "\n".join(lines).strip()


def _day(value: str) -> str:
    moment = c.shared.parse_ts(value)
    return c.shared.day_words(moment) if moment else (value or "")


def _needs_words(decisions: int, reviews: int) -> str:
    parts = []
    if decisions:
        parts.append(c.shared.plural(decisions, "decision"))
    if reviews:
        parts.append(c.shared.plural(reviews, "review"))
    return " and ".join(parts) if parts else "Nothing waiting"


def _bug_words(bugs: list[dict]) -> str:
    serious = [b for b in bugs if b.get("severity") in ("urgent", "high")]
    fixing = [b for b in bugs if b.get("status") == "fixing"]
    parts = []
    if serious:
        parts.append(f"{len(serious)} serious")
    if fixing:
        parts.append(f"{len(fixing)} being fixed")
    return " · ".join(parts) if parts else ("None open" if not bugs else "None serious")


def _people_words(data: Space) -> str:
    audience = (data.brief.audience if data.brief is not None else "").split(".")[0].split(",")[0].strip()
    return (audience[:30] + ("…" if len(audience) > 30 else "")) if audience else "Who uses it"


def _period_words(snapshot: dict | None) -> str:
    if not snapshot:
        return ""
    period = str(snapshot.get("period") or "")
    if _DAY.match(period):
        words = c.shared.day_words(c.shared.parse_ts(period))
    elif _MONTH.match(period):
        words = "This month so far" if period == datetime.now().strftime("%Y-%m") else period
    elif "-W" in period:
        words = "This week"
    else:
        moment = c.shared.parse_ts(snapshot.get("ts"))
        words = f"As of {c.shared.day_words(moment)}" if moment else ""
    source = snapshot.get("source")
    tail = "added by hand" if source == "manual" else (f"from {source}" if source else "")
    return " · ".join(filter(None, [words, tail]))
