# SPDX-License-Identifier: Apache-2.0
"""Brief: what we're making, for whom, and what good looks like. One page, editable in the
Project book."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .. import widgets as w  # noqa: E402
from .base import Card, CardContext, data_of  # noqa: E402


def _words(value) -> str:
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
        return " ".join(i if i.endswith((".", "!", "?")) else i + "." for i in items) if len(items) > 1 else (items[0] if items else "")
    return str(value or "").strip()


def _platforms(value) -> str:
    if isinstance(value, (list, tuple)):
        names = [str(v) for v in value if str(v).strip()]
        if not names:
            return ""
        return names[0].capitalize() if len(names) == 1 else ", ".join(names[:-1]).capitalize() + " and " + names[-1]
    return str(value or "")


def facts(rows: list[tuple[str, str]]) -> Gtk.Grid:
    """Facts as rows with a fixed label column (v73 `.pgkv`)."""
    grid = Gtk.Grid(column_spacing=12, row_spacing=0)
    grid.add_css_class("pg-facts")
    shown = [(k, v) for k, v in rows if v]
    for index, (key, value) in enumerate(shown):
        name = w.label(key, "pg-fact-key", valign=Gtk.Align.START)
        name.set_size_request(112, -1)
        text = w.label(value, "pg-fact-value", wrap=True, selectable=True, hexpand=True)
        last = index == len(shown) - 1
        for widget in (name, text):
            widget.add_css_class("pg-fact-cell")
            if last:
                widget.add_css_class("last")
        grid.attach(name, 0, index, 1, 1)
        grid.attach(text, 1, index, 1, 1)
    return grid


def build(entry: dict, ctx: CardContext) -> Gtk.Widget:
    data = data_of(entry)
    name = ctx.project_name or "This project"
    card = Card("brief", accessible="Brief")
    card.head("file-text", "Brief", right=f"One page · {name}")
    title = data.get("title") or ""
    card.title(f"{name}: {title}" if title and not title.lower().startswith(name.lower()) else (title or name))
    body = card.body()
    body.append(facts([
        ("What it is", _words(data.get("summary"))),
        ("Who it’s for", _words(data.get("audience"))),
        ("Where", _platforms(data.get("platforms"))),
        ("Good looks like", _words(data.get("good_looks_like"))),
    ]))
    if ctx.open_sheet is not None:
        foot = card.foot(flat=True)
        foot.append(w.button("Edit", icon_name_="pencil", small=True,
                             on_click=lambda: ctx.sheet("book", section="brief")))
    return card
