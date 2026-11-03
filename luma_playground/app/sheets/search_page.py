# SPDX-License-Identifier: Apache-2.0
"""Search everywhere (Ctrl+K, or the field at the foot of the sidebar): the bar at the foot of the area
becomes the search field, and the area shows what matches, grouped by where it lives. Return opens the
first match; closing the search goes back to the area you were in. core/search.py does the finding.
"""
from __future__ import annotations

from gi.repository import Gtk

from . import common as c
from .common import Row, Tag, text

GROUPS = (("conversation", "Conversations", "message-square"), ("task", "Tasks", "list-todo"),
          ("bug", "Bugs", "bug"), ("knowledge", "Knowledge", "book-open"))


class SearchPages:
    """Methods ProjectSpace gains for search."""

    search_query = ""
    search_hits: list = []
    _search_leaving = False
    _search_serial = 0

    def section_search(self, data) -> Gtk.Widget:
        query = self.search_query.strip()
        hits = self.search_hits if query else []
        lede = (f"{len(hits)} match{'es' if len(hits) != 1 else ''} for “{query}”" if hits else
                f"Nothing matches “{query}”" if query else "Tasks, bugs, knowledge and conversations")
        box = c.vbox(self.heading("Search", lede))
        if not query:
            box.append(c.ListGroup(Row("Type below to search", "Every word has to appear. Return opens the "
                                       "first match; Escape goes back.", lead="search"), css="pgs-box"))
            return box
        if not hits:
            box.append(c.ListGroup(Row("No matches", "Try fewer words, or a different one.", lead="search"),
                                   css="pgs-box"))
            return box
        for kind, words, icon in GROUPS:
            group = [hit for hit in hits if hit.kind == kind]
            if not group:
                continue
            box.append(self.subhead(words, str(len(group))))
            box.append(c.ListGroup(*[self.hit_row(hit, icon) for hit in group], css="pgs-box"))
        return box

    def hit_row(self, hit, icon: str) -> Gtk.Widget:
        tags = []
        if hit.kind == "bug":
            tags.append(Tag(f"#{hit.key}", None))
        if hit.done:
            tags.append(Tag({"conversation": "Archived", "bug": "Closed"}.get(hit.kind, "Done"), None))
        return Row(hit.title, hit.snippet or None, lead=icon, tags=tags, chevron=True,
                   on_activate=lambda h=hit: self.open_hit(h))

    # ── the field in the bar ──

    def search_bar(self) -> None:
        center, kit = self.center, c.kit
        if center is None or kit is None or center.searching:
            return
        center.show_bar([kit.BarAction("search", "Search", primary=True, on_activate=self.search_bar)])
        center.search(self.search_changed, placeholder="Search everything",
                      text=self.search_query, on_activate=lambda _text: self.open_first(),
                      on_close=self.search_closed)

    def search_changed(self, query: str) -> None:
        self.search_query = query
        self._search_serial += 1
        serial, root = self._search_serial, self.ctx.project_dir
        if not query.strip():
            self.search_hits = []
            self.render()
            return
        finder = c.module("core.search")
        store = c.module("core.store").ProjectStore(root)

        def done(hits) -> None:
            if serial == self._search_serial and self.at == "search":
                self.search_hits = hits
                self.render()
        c.run_async(lambda: finder.search(store, query), done, lambda error: c.log.warning("search: %s", error))

    def search_closed(self) -> None:
        if self._search_leaving:
            return
        back = getattr(self.window, "show_area", None)
        if callable(back) and self.at == "search":
            back(getattr(self.window, "_last_area", None) or "tasks")

    def _leave_search(self) -> None:
        center = self.center
        if center is not None and center.searching:
            self._search_leaving = True
            try:
                center.close_search()
            finally:
                self._search_leaving = False

    def open_first(self) -> None:
        if self.search_hits:
            self.open_hit(self.search_hits[0])

    def open_hit(self, hit) -> None:
        window = self.window
        self._leave_search()
        if hit.kind == "conversation":
            window.show_area(getattr(window, "_last_area", None) or "tasks")
            window.open_conversation(hit.key)
        elif hit.kind == "task":
            self.task_open = hit.key
            window.show_area("tasks")
        elif hit.kind == "bug":
            window.show_area("bugs", item=hit.key)
        else:
            window.show_area("knowledge", item=hit.key)
