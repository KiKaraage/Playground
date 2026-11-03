# SPDX-License-Identifier: Apache-2.0
"""Recipe: how it's built, in plain words, with the why beside each part."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .. import widgets as w  # noqa: E402
from .base import Card, CardContext, data_of  # noqa: E402

GLYPHS = (("page", "layout-grid"), ("screen", "layout-grid"), ("app", "layout-grid"), ("data", "database"),
          ("sav", "database"), ("stor", "database"), ("sign", "user"), ("account", "user"), ("run", "house"),
          ("host", "server"), ("game", "gamepad-2"), ("pay", "coins"), ("text", "message-square"),
          ("mail", "message-square"), ("test", "checker"))


def glyph_for(label: str) -> str:
    lowered = label.lower()
    for word, glyph in GLYPHS:
        if word in lowered:
            return glyph
    return "layers"


def part_row(glyph: str, name: str, choice: str, why: str) -> Gtk.Widget:
    row = w.box("h", 12, "pg-recipe-row")
    tile = w.box("h", 0, "pg-tile", valign=Gtk.Align.START)
    image = w.icon(glyph, 16)
    image.set_hexpand(True)
    tile.append(image)
    tile.set_hexpand(False)
    row.append(tile)
    words = w.box("v", 1, hexpand=True)
    heading = f"<b>{_esc(name)}</b>" + (f" <span alpha='70%'>· {_esc(choice)}</span>" if choice else "")
    words.append(w.label(heading, "pg-recipe-name", markup=True, wrap=True))
    if why:
        words.append(w.label(why, "pg-recipe-why", wrap=True))
    row.append(words)
    return row


def _esc(text: str) -> str:
    from html import escape
    return escape(str(text), quote=False)


def build(entry: dict, ctx: CardContext) -> Gtk.Widget:
    data = data_of(entry)
    card = Card("recipe", accessible="Recipe")
    card.head("layers", "Recipe", right="How it’s built, in plain words")
    card.title(data.get("summary") or "How it’s built")
    body = card.body(0)
    rows = w.box("v", 0, "pg-recipe")
    for part in data.get("parts") or []:
        if not isinstance(part, dict):
            continue
        label = str(part.get("label") or "")
        rows.append(part_row(glyph_for(label), label, str(part.get("choice") or ""), str(part.get("why") or "")))
    runs_on = data.get("runs_on")
    if runs_on and not any("run" in str((p or {}).get("label", "")).lower() for p in data.get("parts") or [] if isinstance(p, dict)):
        where = ctx.place_name(str(runs_on)) if ctx.place_name else str(runs_on)
        rows.append(part_row("house", "Runs on", where, ""))
    body.append(rows)
    more = data.get("more") or data.get("explain") or []
    if isinstance(more, str):
        more = [more]
    if more:
        revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                                transition_duration=0 if w.reduced_motion() else 200)
        well = w.box("v", 6, "pg-more")
        for paragraph in more:
            well.append(w.label(str(paragraph), wrap=True, selectable=True))
        revealer.set_child(well)
        body.append(revealer)
        foot = card.foot(flat=True)
        toggle = w.button("Explain more", icon_name_="info", small=True)

        def flip(_button) -> None:
            shown = not revealer.get_reveal_child()
            revealer.set_reveal_child(shown)
            toggle.label_widget.set_label("Less" if shown else "Explain more")
        toggle.connect("clicked", flip)
        foot.append(toggle)
    return card
