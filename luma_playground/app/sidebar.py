# SPDX-License-Identifier: Apache-2.0
"""The sidebar: the project you're in, its areas, and its conversations.

    ┌──────────────────────────┐
    │ [R] Rally            ⇅   │  the project picker: every project and sub-project, New project…
    │                          │
    │ ▢ Preview                │
    │ ☰ Tasks               4  │  areas open in the middle of the window and stay there
    │ 🐞 Bugs                3  │
    │ 📖 Knowledge           1  │
    │ ≋ Stack                  │
    │ ↗ Analytics              │
    │ ⟲ Activity               │
    │ 👥 Team                   │
    │ Conversations ─────── +  │  conversations open on the right, beside the area you're in
    │ ◉ Rides draft            │
    │ ◉ Talk to Codex          │
    └──────────────────────────┘

It stands on the window frame like every Luma sidebar (LumaUI NavigationSidebar). At phone width
it is the drawer the title island's menu button opens.

The list keeps one selection, the area; the open conversation is marked "current" instead, because
both are on screen at once.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import GObject, Gtk, Pango  # noqa: E402

from luma_appkit import NavigationSidebar, SidebarRow  # noqa: E402

from . import widgets as w  # noqa: E402
from .rail import goal_subtitle  # noqa: E402

# key, words, Lucide icon
AREAS = (
    ("preview", "Preview", "app-window"),
    ("tasks", "Tasks", "list-todo"),
    ("bugs", "Bugs", "bug"),
    ("knowledge", "Knowledge", "book-open"),
    ("stack", "Stack", "layers"),
    ("files", "Files", "folder"),
    ("repository", "Repository", "git-branch"),
    ("analytics", "Analytics", "chart-line"),
    ("activity", "Activity", "history"),
    ("team", "Team", "users-round"),
)
AREA_KEYS = tuple(key for key, _words, _icon in AREAS)


def sub_projects(folder: Path) -> list[Path]:
    """Projects inside a project, at any depth (Luma's `src/apps/calculator/`), the nearest down each branch."""
    from ..core import family
    return family.children_of(folder)


class ProjectPicker(Gtk.Button):
    """The project you're in, at the top of the sidebar (Helm's switcher). A click lists every project."""

    def __init__(self, on_open: Callable[["ProjectPicker"], None]) -> None:
        super().__init__()
        self.add_css_class("pg-project-picker")
        self.mark_slot = w.box("h", 0)
        self.name = w.label("Playground", "pg-picker-name", ellipsize=True)
        self.status = w.label("", "pg-picker-status", ellipsize=True)
        self.status_dot = w.dot("good")
        status_line = w.box("h", 6)
        status_line.append(self.status_dot)
        status_line.append(self.status)
        words = w.box("v", 1, hexpand=True, valign=Gtk.Align.CENTER)
        words.append(self.name)
        words.append(status_line)
        line = w.box("h", 10)
        line.append(self.mark_slot)
        line.append(words)
        line.append(w.icon("chevrons-up-down", 16, "pg-picker-chevrons"))
        self.set_child(line)
        self.connect("clicked", lambda _b: on_open(self))

    def show_project(self, name: str, folder, status: str, live: bool) -> None:
        w.clear(self.mark_slot)
        self.mark_slot.append(w.project_mark(name, folder, 34))
        self.name.set_label(name)
        self.status.set_label(status)
        self.status_dot.set_visible(live)
        self.set_tooltip_text(f"{name}: switch project")
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{name}, {status}. Switch project"])


class _AreaRow(SidebarRow):
    def __init__(self, key: str, words: str, icon: str) -> None:
        super().__init__(words, lead=w.icon(icon, 18))
        self.add_css_class("pg-area-row")
        self.area = key


class _ConversationRow(SidebarRow):
    def __init__(self, goal: dict, agent: dict, owner: dict | None) -> None:
        status = goal.get("status") or "working"
        if goal.get("is_chat"):
            # The project's first conversation (from before conversations had names) reads like any other.
            title = goal.get("display_title") or "Earlier conversation"
            lead, subtitle = w.icon("message-square", 18), ""
        else:
            title = goal.get("title") or "Untitled"
            lead, subtitle = w.StateGlyph(status, 18), goal_subtitle(goal)
        trail = w.Presence(str(owner.get("name") or "?"), 20, here=bool(owner.get("here", True))) if owner else None
        extra = goal.get("extra") or {}
        super().__init__(title, lead=lead, subtitle=subtitle, trail=trail, pinned=bool(extra.get("pinned")),
                         muted=bool(extra.get("archived")))
        self.add_css_class("pg-conversation-row")
        self.add_css_class(f"state-{w.state_kind(status)}")
        self.goal_id = goal["id"]
        self.glyph = lead if isinstance(lead, w.StateGlyph) else None
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        # The status line is cut short too: "Working · Codex 2" mustn't widen the whole sidebar.
        if getattr(self, "subtitle_label", None) is not None:
            self.subtitle_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.set_selectable(False)
        self.set_tooltip_text(f"{title}\n{subtitle}" if subtitle else title)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{title}, {subtitle}" if subtitle else title])


class _AddRow(SidebarRow):
    def __init__(self, text: str, action: str) -> None:
        super().__init__(text, lead=w.icon("plus", 16))
        self.add_css_class("pg-add-row")
        self.set_selectable(False)
        self.action = action


class Sidebar(Gtk.Box):
    """Emits area-chosen(key), conversation-chosen(id), new-conversation(), project-chosen(path),
    new-project(), new-subproject(), project-menu(widget) and search()."""

    __gsignals__ = {
        "area-chosen": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "conversation-chosen": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "new-conversation": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "project-chosen": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "new-project": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "new-subproject": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "open-folder": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "project-menu": (GObject.SignalFlags.RUN_FIRST, None, (Gtk.Widget,)),
        # A conversation's menu (right-click, long-press): rename, details, pin, fork, archive, delete.
        "conversation-menu": (GObject.SignalFlags.RUN_FIRST, None, (str, Gtk.Widget, float, float)),
        "search": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "usage": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("pg-rail-holder")
        self.sidebar = NavigationSidebar(variant="destinations")
        self.sidebar.add_css_class("pg-rail")
        self.sidebar.add_css_class("pg-sidebar")
        self.sidebar.set_vexpand(True)
        self.sidebar.list.update_property([Gtk.AccessibleProperty.LABEL], ["Project areas and conversations"])
        self.sidebar.list.connect("row-activated", self._activated)
        self.picker = ProjectPicker(self._open_picker)
        self.sidebar.append_header(self.picker)
        self.append(self.sidebar)
        # Search everywhere, at the foot (Ctrl+K does the same).
        self.search_button = Gtk.Button(visible=False)
        self.search_button.add_css_class("pg-sidebar-search")
        line = Gtk.Box(spacing=10)
        line.append(w.icon("search", 16))
        line.append(Gtk.Label(label="Search", hexpand=True, xalign=0))
        keys = Gtk.Label(label="Ctrl K")
        keys.add_css_class("pg-sidebar-search-keys")
        line.append(keys)
        self.search_button.set_child(line)
        self.search_button.set_tooltip_text("Search tasks, bugs, knowledge and conversations (Ctrl+K)")
        self.search_button.connect("clicked", lambda _b: self.emit("search"))
        self.search_button.set_hexpand(True)
        # Usage: the agents' plan this week (or the project's month), a small meter beside Search.
        self.usage_button = Gtk.Button(visible=False, valign=Gtk.Align.CENTER)
        self.usage_button.add_css_class("pg-sidebar-usage")
        usage = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        usage.append(w.icon("gauge", 16))
        self.usage_meter = w.Meter(0.0, width=18, height=3, accessible="Usage this week")
        self.usage_meter.add_css_class("pg-mini")
        usage.append(self.usage_meter)
        self.usage_button.set_child(usage)
        self.usage_button.set_tooltip_text("Usage")
        self.usage_button.update_property([Gtk.AccessibleProperty.LABEL], ["Usage"])
        self.usage_button.connect("clicked", lambda _b: self.emit("usage"))
        foot = Gtk.Box(spacing=8)
        foot.add_css_class("pg-sidebar-foot")
        foot.append(self.search_button)
        foot.append(self.usage_button)
        self.sidebar.append_footer(foot)
        self._projects: list = []
        self._current = None
        self._area_rows: dict[str, _AreaRow] = {}
        self._rows: dict[str, _ConversationRow] = {}
        self._selecting = False
        self.show_archived = False
        self.area: str = "tasks"
        self.current_goal: str | None = None

    @property
    def list(self) -> Gtk.ListBox:
        return self.sidebar.list

    def set_phone(self, phone: bool) -> None:
        (self.add_css_class if phone else self.remove_css_class)("phone")

    # ── filling ──────────────────────────────────────────────────────────
    def show(self, projects: list, current, goals: list[dict], agent: dict, *, counts: dict | None = None,
             status: str = "", live: bool = False, attention=None, drafting: str | None = None) -> None:
        """Draw the picker, the areas (with their counts) and the open project's conversations."""
        self._projects, self._current = list(projects), current
        self._last_goals, self._last_agent, self._counts = list(goals), agent, dict(counts or {})
        self._selecting = True
        self.sidebar.clear()
        self._area_rows, self._rows = {}, {}
        if current is not None:
            self.picker.show_project(current.name, current.path, status or (current.what or "On this computer"), live)
        else:
            self.picker.show_project("Playground", None, "No project open", False)
        self.picker.set_sensitive(bool(projects) or current is not None)
        self.search_button.set_visible(current is not None)
        self.usage_button.set_visible(current is not None)
        for key, words, icon in (AREAS if current is not None else ()):     # areas belong to a project
            row = _AreaRow(key, words, icon)
            self._area_rows[key] = row
            self.sidebar.append_row(row)
        self.set_counts(counts or {})
        if current is not None:
            self.sidebar.append_section("Conversations", action=("pg-plus-symbolic", "New conversation",
                                                                 lambda: self.emit("new-conversation")))
            you = current.you() if hasattr(current, "you") else ""
            shown = [g for g in goals if not (g.get("is_chat") and g.get("empty"))]   # an unused general chat hides
            archived = [g for g in shown if (g.get("extra") or {}).get("archived")]
            live = [g for g in shown if not (g.get("extra") or {}).get("archived")]
            live.sort(key=lambda g: not (g.get("extra") or {}).get("pinned"))         # pinned first, order kept
            for goal in live + (archived if self.show_archived else []):
                owner_id = goal.get("owner") or ""
                owner = (current.member(owner_id) if owner_id and owner_id != you and hasattr(current, "member")
                         else None)
                row = _ConversationRow(goal, agent, owner)
                self._watch_menu(row)
                self._rows[goal["id"]] = row
                self.sidebar.append_row(row)
            self.sidebar.append_row(_AddRow("New conversation", "new-conversation"))
            if archived:
                toggle = _AddRow(("Hide archived" if self.show_archived else f"Archived · {len(archived)}"),
                                 "toggle-archived")
                toggle.lead.set_from_icon_name(w.icon_name("archive"))
                self.sidebar.append_row(toggle)
        self._selecting = False
        self.select_area(self.area)
        self.mark_conversation(self.current_goal)
        self.set_unread(getattr(self, "_unread", set()))

    def _watch_menu(self, row: "_ConversationRow") -> None:
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", lambda _g, _n, x, y, r=row: self.emit("conversation-menu", r.goal_id, r, x, y))
        row.add_controller(click)
        hold = Gtk.GestureLongPress(touch_only=True)
        hold.connect("pressed", lambda _g, x, y, r=row: self.emit("conversation-menu", r.goal_id, r, x, y))
        row.add_controller(hold)

    def set_usage(self, fraction: float | None, tooltip: str) -> None:
        self.usage_meter.set_visible(fraction is not None)
        if fraction is not None:
            self.usage_meter.set_fraction(fraction)
            (self.usage_meter.add_css_class if fraction >= 0.85 else self.usage_meter.remove_css_class)("warn")
        self.usage_button.set_tooltip_text(tooltip or "Usage")

    def set_unread(self, unread: set) -> None:
        """A dot on conversations with something new the person hasn't opened (a question, a finished turn)."""
        self._unread = set(unread)
        for key, row in self._rows.items():
            setter = getattr(row, "set_attention", None)
            if callable(setter):
                setter(key in self._unread)

    def set_counts(self, counts: dict) -> None:
        """counts: area → (number, needs attention). Shown as the kit's count badge on the row."""
        for key, row in self._area_rows.items():
            number, attention = counts.get(key, (0, False))
            set_count = getattr(row, "set_count", None)
            if callable(set_count):
                set_count(int(number or 0))
            (row.add_css_class if attention and number else row.remove_css_class)("attention")

    def update_goal(self, goal: dict) -> bool:
        row = self._rows.get(goal.get("id"))
        if row is None or row.glyph is None:
            return False
        for kind in ("work", "needs", "review", "live", "paused", "draft"):
            row.remove_css_class(f"state-{kind}")
        status = goal.get("status") or "draft"
        row.add_css_class(f"state-{w.state_kind(status)}")
        row.glyph.set_status(status)
        row.title_label.set_label(goal.get("title") or "Untitled")
        row.subtitle_label.set_label(goal_subtitle(goal))
        return True

    def goal_order(self) -> list[str]:
        return list(self._rows)

    def select_area(self, key: str | None) -> None:
        if key in self._area_rows:
            self.area = key
        row = self._area_rows.get(key or "")
        self._selecting = True
        if row is not None:
            self.list.select_row(row)
        else:
            self.list.unselect_all()
        self._selecting = False

    def mark_conversation(self, goal_id: str | None) -> None:
        """The conversation open on the right is marked, without taking the area's selection."""
        if goal_id:
            self.current_goal = goal_id
        for key, row in self._rows.items():
            (row.add_css_class if key == goal_id else row.remove_css_class)("current")

    # The window's older name for marking the open conversation.
    def select(self, goal_id: str | None) -> None:
        self.mark_conversation(goal_id)

    def focus_current(self) -> None:
        row = self.list.get_selected_row()
        (row or self.list).grab_focus()

    # ── the project picker ───────────────────────────────────────────────
    def _open_picker(self, anchor: Gtk.Widget) -> None:
        popover = Gtk.Popover(has_arrow=False, position=Gtk.PositionType.BOTTOM)
        popover.add_css_class("pg-picker-popover")
        popover.set_parent(anchor)
        body = w.box("v", 2, "pg-picker-menu")
        body.append(w.label("Projects", "pg-picker-heading"))
        current = Path(self._current.path) if self._current is not None else None

        def entry(name: str, folder: Path, subtitle: str, depth: int) -> Gtk.Button:
            button = Gtk.Button()
            button.add_css_class("pg-picker-row")
            if depth:
                button.add_css_class("child")
            line = w.box("h", 10)
            line.append(w.project_mark(name, folder, 28 if depth == 0 else 22))
            words = w.box("v", 0, hexpand=True, valign=Gtk.Align.CENTER)
            words.append(w.label(name, "pg-picker-row-name", ellipsize=True))
            if subtitle:
                words.append(w.label(subtitle, "pg-picker-row-sub", ellipsize=True))
            line.append(words)
            if current is not None and folder == current:
                line.append(w.icon("check", 16, "pg-picker-check"))
                button.add_css_class("current")
            button.set_child(line)
            button.connect("clicked", lambda _b, f=folder: (popover.popdown(), self.emit("project-chosen", str(f))))
            return button

        rows = w.box("v", 2)
        for project in self._projects:
            folder = Path(project.path)
            rows.append(entry(project.name, folder, project.what or "", 0))

            def nested(above: Path, above_name: str, depth: int) -> None:
                from ..core import family
                for child in sub_projects(above):
                    name = family.name_of(child)
                    rows.append(entry(name, child, f"In {above_name}", min(depth, 2)))
                    if depth < 4:
                        nested(child, name, depth + 1)
            nested(folder, project.name, 1)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                    max_content_height=420)
        scroll.set_child(rows)
        body.append(scroll)
        body.append(Gtk.Separator())
        for glyph, words, signal in (("plus", "New project…", "new-project"),
                                     ("folder-plus", "New sub-project…", "new-subproject"),
                                     ("folder-open", "Open a folder…", "open-folder")):
            if signal == "new-subproject" and self._current is None:
                continue
            button = Gtk.Button()
            button.add_css_class("pg-picker-action")
            line = w.box("h", 10)
            line.append(w.icon(glyph, 16))
            line.append(w.label(words, hexpand=True))
            button.set_child(line)
            button.connect("clicked", lambda _b, s=signal: (popover.popdown(), self.emit(s)))
            body.append(button)
        if self._current is not None:
            button = Gtk.Button()
            button.add_css_class("pg-picker-action")
            line = w.box("h", 10)
            line.append(w.icon("ellipsis", 16))
            line.append(w.label(f"More for {self._current.name}…", hexpand=True))
            button.set_child(line)
            button.connect("clicked", lambda _b: (popover.popdown(), self.emit("project-menu", anchor)))
            body.append(button)
        popover.set_child(body)
        popover.connect("closed", lambda p: p.unparent())
        popover.popup()

    # ── choosing ─────────────────────────────────────────────────────────
    def _activated(self, _list, row: Gtk.ListBoxRow) -> None:
        if isinstance(row, _AreaRow):
            self.area = row.area
            self.emit("area-chosen", row.area)
            return
        if isinstance(row, _AddRow):
            if row.action == "toggle-archived":
                self.show_archived = not self.show_archived
                self.show(self._projects, self._current, self._last_goals, self._last_agent, counts=self._counts)
                return
            self.emit(row.action)
            return
        if isinstance(row, _ConversationRow):
            self.mark_conversation(row.goal_id)
            self.select_area(self.area)      # the area keeps the list's selection
            self.emit("conversation-chosen", row.goal_id)
