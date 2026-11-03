# SPDX-License-Identifier: Apache-2.0
"""Heads-up: the agent changed something nobody asked for. It says so, with Undo."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .. import widgets as w  # noqa: E402
from .base import Card, CardContext, data_of, rich_label  # noqa: E402


class HeadsUpCard(Card):
    def __init__(self, entry: dict, ctx: CardContext) -> None:
        data = data_of(entry)
        self.text = str(entry.get("text") or data.get("text") or "")
        super().__init__("headsup", accessible=f"Heads-up: {self.text}")
        self.ctx, self.data = ctx, data
        row = w.box("h", 12, "pg-heads")
        self.tile = w.box("h", 0, "pg-tile", "amber", valign=Gtk.Align.START)
        self.glyph = w.icon("triangle-alert", 16)
        self.glyph.set_hexpand(True)
        self.tile.append(self.glyph)
        self.tile.set_hexpand(False)
        row.append(self.tile)
        column = w.box("v", 2, hexpand=True)
        self.heading = w.label("Heads-up: I changed something you didn’t ask for", "pg-heads-title", wrap=True)
        column.append(self.heading)
        self.words = rich_label(self.text, ctx, "pg-heads-text", selectable=True)
        column.append(self.words)
        row.append(column)
        undo = data.get("undo")
        self.undone = bool(data.get("undone"))
        if undo and ctx.undo is not None:
            self.action = w.button("Undo", icon_name_="undo-2", small=True, on_click=self._undo)
            self.action.set_valign(Gtk.Align.START)
            row.append(self.action)
        self.append(row)
        if self.undone:
            self._show_undone()

    def _undo(self) -> None:
        if self.undone:
            return
        if self.ctx.undo(self.data.get("undo") or {}, self.data):
            self.undone = True
            self._show_undone()

    def _show_undone(self) -> None:
        self.add_css_class("undone")
        self.heading.set_label("Undone")
        self.glyph.set_from_icon_name(w.icon_name("rotate-ccw"))
        if hasattr(self, "action"):
            self.action.set_sensitive(False)
            self.action.label_widget.set_label("Undone")


def build(entry: dict, ctx: CardContext) -> Gtk.Widget:
    return HeadsUpCard(entry, ctx)
