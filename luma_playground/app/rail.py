# SPDX-License-Identifier: Apache-2.0
"""The rail: your projects, and the goals of the one you're in.

Each goal shows its state the same way everywhere: a shape (so it reads without colour)
and the words (Working, Needs you, Ready to review, Live, Paused). The project's general
chat, Talk to Ari, sits at the top; a teammate's goal carries their face.

The rail stands on the window frame like every Luma sidebar (LumaUI NavigationSidebar).
At phone width it is the drawer the title row's menu button opens, with the Project book,
People, Places and Usage at its foot.
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

def goal_subtitle(goal: dict) -> str:
    """'Needs you · one decision', 'Working · Priya's agent', 'Live since Tuesday'."""
    status = goal.get("status") or "working"
    words = goal.get("status_label") or w.state_label(status)
    detail = goal.get("status_note") or goal.get("detail") or ""
    if detail and detail.lower().startswith(("since ", "after ")) and status in ("live", "paused"):
        return f"{words} {detail}" if status == "live" else f"{words} · {detail}"
    if not detail:
        if status == "needs-you" and goal.get("pending"):
            count = int(goal["pending"])
            detail = "one decision" if count == 1 else f"{count} decisions"
        elif status == "working" and goal.get("agent_name"):
            detail = goal["agent_name"]
        elif status == "live":
            moment = w.parse_ts(goal.get("updated"))
            if moment is not None:
                return f"Live since {w.day_words(moment) if w.day_words(moment) not in ('Today',) else 'today'}"
        elif status == "paused" and goal.get("paused_note"):
            detail = goal["paused_note"]
    return f"{words} · {detail}" if detail else words


class _ProjectRow(SidebarRow):
    def __init__(self, project, current: bool, on_more: Callable[[Gtk.Widget], None] | None) -> None:
        trail = None
        if current and on_more is not None:
            trail = w.icon_button("ellipsis", f"More for {project.name}", size=16)
            trail.add_css_class("pg-row-more")
            trail.connect("clicked", lambda b: on_more(b))
        super().__init__(project.name, lead=w.project_mark(project.name, project.path, 30),
                         subtitle=project.what or "", trail=trail)
        self.add_css_class("pg-project-row")
        if current:
            self.add_css_class("current")
        self.set_selectable(False)
        self.project_path = str(project.path)
        self.update_property([Gtk.AccessibleProperty.LABEL],
                             [f"{project.name}{', open' if current else ''}. {project.what or ''}"])


def _two_lines(label: Gtk.Label) -> None:
    """A goal's title wraps onto a second line before it is cut short ("Studio photos on the home page")."""
    label.set_wrap(True)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_lines(2)
    label.set_ellipsize(Pango.EllipsizeMode.END)
    label.set_xalign(0.0)


class _GoalRow(SidebarRow):
    def __init__(self, goal: dict, owner: dict | None = None) -> None:
        status = goal.get("status") or "working"
        trail = None
        if owner is not None:
            trail = w.Presence(str(owner.get("name") or "?"), 20, here=bool(owner.get("here", True)))
        super().__init__(goal.get("title") or "Untitled goal", lead=w.StateGlyph(status, 18),
                         subtitle=goal_subtitle(goal), trail=trail)
        self.add_css_class("pg-goal-row")
        self.add_css_class(f"state-{w.state_kind(status)}")
        self.goal_id = goal["id"]
        self.glyph = self.lead
        _two_lines(self.title_label)
        self.set_tooltip_text(f"{goal.get('title') or 'Untitled goal'}\n{goal_subtitle(goal)}")
        self.update_property([Gtk.AccessibleProperty.LABEL],
                             [f"{goal.get('title')}, {goal_subtitle(goal)}"])


class _ChatRow(SidebarRow):
    def __init__(self, goal: dict, agent: dict) -> None:
        title = goal.get("title") or "Talk to Ari"
        if title == "Talk to Ari":
            title = f"Talk to {agent.get('label') or 'Ari'}"
        super().__init__(title, lead=w.agent_mark(agent, 18))
        self.add_css_class("pg-goal-row")
        self.add_css_class("pg-chat-row")
        self.goal_id = goal["id"]


class _AttentionRow(Gtk.ListBoxRow):
    """Under the open project: what needs a look, quietly (2 open bugs · Resend near budget)."""

    def __init__(self, notes, on_open) -> None:
        super().__init__(selectable=False, activatable=False)
        self.add_css_class("pg-attention-row")
        line = w.box("h", 0, "pg-attention")
        for index, (text, section, item) in enumerate(notes):
            if index:
                line.append(w.label("·", "pg-attention-sep"))
            link = Gtk.Button()
            link.add_css_class("pg-attention-link")
            content = w.box("h", 6)
            content.append(w.dot("bad" if section == "bugs" else "warn"))
            content.append(w.label(text, ellipsize=True))
            link.set_child(content)
            link.set_tooltip_text(f"Open the Project space at {'Bugs' if section == 'bugs' else 'Stack and services'}")
            link.connect("clicked", lambda _b, s=section, i=item: on_open(s, "" if i is None else str(i)))
            line.append(link)
        self.set_child(line)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Needs a look: " + ", ".join(n[0] for n in notes)])


class _AddRow(SidebarRow):
    def __init__(self, text: str, action: str) -> None:
        super().__init__(text, lead=w.icon("plus", 16))
        self.add_css_class("pg-add-row")
        self.set_selectable(False)
        self.action = action


class _DraftRow(SidebarRow):
    def __init__(self, subtitle: str) -> None:
        super().__init__("New project", lead=w.StateGlyph("draft", 18), subtitle=subtitle)
        self.add_css_class("pg-goal-row")
        self.goal_id = "__new__"


class Rail(Gtk.Box):
    """Emits `goal-chosen(goal_id)`, `project-chosen(path)`, `new-goal()` and `new-project()`."""

    __gsignals__ = {
        "goal-chosen": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "project-chosen": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "new-goal": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "new-project": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "project-menu": (GObject.SignalFlags.RUN_FIRST, None, (Gtk.Widget,)),
        "place-action": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "section": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("pg-rail-holder")
        self.sidebar = NavigationSidebar(variant="resources")
        self.sidebar.add_css_class("pg-rail")
        self.sidebar.set_size_request(232, -1)
        self.sidebar.set_vexpand(True)
        self.sidebar.list.update_property([Gtk.AccessibleProperty.LABEL], ["Projects and goals"])
        self.sidebar.list.connect("row-activated", self._activated)
        self.append(self.sidebar)
        # At phone width the rail is the drawer; these four live at its foot (v73 .pgphonly).
        self.phone_foot = w.box("v", 2, "pg-rail-foot")
        for key, glyph, text in (("book", "book-open", "Project space"), ("people", "users", "People"),
                                 ("places", "server", "Places"), ("usage", "gauge", "Usage")):
            row = Gtk.Button()
            row.add_css_class("pg-rail-foot-row")
            line = w.box("h", 12)
            line.append(w.icon(glyph, 18))
            line.append(w.label(text, hexpand=True))
            row.set_child(line)
            row.connect("clicked", lambda _b, k=key: self.emit("place-action", k))
            self.phone_foot.append(row)
        self.phone_foot.set_visible(False)
        self.sidebar.append_footer(self.phone_foot)
        self.sidebar.footer.set_visible(False)
        self._goal_rows: dict[str, SidebarRow] = {}
        self._selecting = False
        self.current_goal: str | None = None

    @property
    def list(self) -> Gtk.ListBox:
        return self.sidebar.list

    def set_phone(self, phone: bool) -> None:
        self.phone_foot.set_visible(phone)
        self.sidebar.footer.set_visible(phone)

    # ── filling ──────────────────────────────────────────────────────────
    def show(self, projects: list, current, goals: list[dict], agent: dict, *, drafting: str | None = None,
             attention: list | None = None) -> None:
        """Draw the rail: every project, and under the open one its chat and goals."""
        self._selecting = True
        self.sidebar.clear()
        self._goal_rows = {}
        self.sidebar.append_section("Projects", action=("pg-plus-symbolic", "New project",
                                                        lambda: self.emit("new-project")))
        for project in projects:
            is_current = current is not None and Path(project.path) == Path(current.path)
            self.sidebar.append_row(_ProjectRow(project, is_current,
                                                (lambda b: self.emit("project-menu", b)) if is_current else None))
            if not is_current:
                continue
            if attention:
                self.sidebar.append_row(_AttentionRow(attention, lambda sec, item: self.emit("section", sec, item)))
            you = project.you() if hasattr(project, "you") else ""
            for goal in goals:
                if goal.get("is_chat"):
                    row = _ChatRow(goal, agent)
                else:
                    owner_id = goal.get("owner") or ""
                    owner = project.member(owner_id) if owner_id and owner_id != you and hasattr(project, "member") else None
                    row = _GoalRow(goal, owner)
                self._goal_rows[goal["id"]] = row
                self.sidebar.append_row(row)
            self.sidebar.append_row(_AddRow("New goal", "new-goal"))
        if drafting is not None:
            draft = _DraftRow(drafting)
            self._goal_rows["__new__"] = draft
            self.sidebar.append_row(draft)
        else:
            self.sidebar.append_row(_AddRow("New project", "new-project"))
        self._selecting = False
        self.select(self.current_goal if drafting is None else "__new__")

    def set_draft(self, subtitle: str) -> None:
        """The New project row says where the new project has got to ("A few quick questions")."""
        row = self._goal_rows.get("__new__")
        if isinstance(row, _DraftRow):
            row.subtitle_label.set_label(subtitle)

    def update_goal(self, goal: dict) -> bool:
        """Refresh one goal's state in place, keeping selection and focus. False if it isn't shown."""
        row = self._goal_rows.get(goal.get("id"))
        if not isinstance(row, _GoalRow):
            return False
        for kind in ("work", "needs", "review", "live", "paused", "draft"):
            row.remove_css_class(f"state-{kind}")
        status = goal.get("status") or "draft"
        row.add_css_class(f"state-{w.state_kind(status)}")
        row.glyph.set_status(status)
        row.title_label.set_label(goal.get("title") or "Untitled goal")
        row.subtitle_label.set_label(goal_subtitle(goal))
        row.set_tooltip_text(f"{goal.get('title') or 'Untitled goal'}\n{goal_subtitle(goal)}")
        row.update_property([Gtk.AccessibleProperty.LABEL], [f"{goal.get('title')}, {goal_subtitle(goal)}"])
        return True

    def goal_order(self) -> list[str]:
        return [key for key in self._goal_rows if key != "__new__"]

    def select(self, goal_id: str | None) -> None:
        if goal_id is not None and goal_id != "__new__":
            self.current_goal = goal_id
        row = self._goal_rows.get(goal_id or "")
        self._selecting = True
        if row is not None:
            self.list.select_row(row)
        else:
            self.list.unselect_all()
        self._selecting = False

    def focus_current(self) -> None:
        row = self.list.get_selected_row()
        (row or self.list).grab_focus()

    # ── choosing ─────────────────────────────────────────────────────────
    def _activated(self, _list, row: Gtk.ListBoxRow) -> None:
        if isinstance(row, _ProjectRow):
            if not row.has_css_class("current"):
                self.emit("project-chosen", row.project_path)
            return
        if isinstance(row, _AddRow):
            self.emit(row.action)
            return
        goal_id = getattr(row, "goal_id", None)
        if goal_id and goal_id != "__new__":
            self.current_goal = goal_id
            self.emit("goal-chosen", goal_id)
