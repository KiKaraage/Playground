# SPDX-License-Identifier: Apache-2.0
"""Cost, shown only when it matters: what a goal is spending, against what's left."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .. import widgets as w  # noqa: E402
from .base import Card, CardContext, data_of  # noqa: E402


def build(entry: dict, ctx: CardContext) -> Gtk.Widget:
    data = data_of(entry)
    title = str(data.get("title") or "")
    text = entry.get("text") or data.get("text") or ""
    if not title:
        title, text = text, ""
    card = Card("cost", accessible=f"Cost: {title}")
    row = w.box("h", 12, "pg-cost")
    tile = w.box("h", 0, "pg-tile", valign=Gtk.Align.START)
    glyph = w.icon("coins", 16)
    glyph.set_hexpand(True)
    tile.append(glyph)
    tile.set_hexpand(False)
    row.append(tile)
    column = w.box("v", 6, hexpand=True)
    column.append(w.label(title, "pg-cost-title", wrap=True))
    fraction = data.get("fraction")
    for key in ("percent", "pct"):
        if fraction is None and data.get(key) is not None:
            fraction = float(data[key]) / 100.0
    if fraction is not None:
        meter = w.Meter(float(fraction), height=6, extra=float(data.get("adds") or 0), accessible=title)
        if float(fraction) > 0.85:
            meter.add_css_class("warn")
        column.append(meter)
    note = text or data.get("sub") or data.get("detail")
    if note:
        column.append(w.label(str(note), "pg-sub", wrap=True))
    row.append(column)
    if data.get("label"):
        side = w.label(str(data["label"]), "pg-sub", valign=Gtk.Align.START)
        row.append(side)
    card.append(row)
    return card
