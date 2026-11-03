# SPDX-License-Identifier: Apache-2.0
"""Handoff: switching agent or account. The brief the next agent reads first, which you can
change before it starts."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .. import widgets as w  # noqa: E402
from .base import Card, CardContext, agreed, data_of, spacer  # noqa: E402


def build(entry: dict, ctx: CardContext) -> Gtk.Widget:
    data = data_of(entry)
    source = ctx.connection_of(data.get("from_connection"))
    target = ctx.connection_of(data.get("to_connection"))
    to_name = target.get("label") or "the next agent"
    draft = bool(data.get("draft"))
    card = Card("handoff", accessible=f"Handoff to {to_name}")
    right = agreed(f"{to_name} picked it up") if data.get("picked_up") else None
    card.head("handoff", "Handoff", right=right)
    card.title(f"Handing this goal to {to_name}" if draft else f"Handed to {to_name}")
    line = w.box("h", 10, "pg-handoff")
    line.append(w.agent_mark(source, 22))
    line.append(w.label(source.get("label") or "Ari", "pg-strong"))
    line.append(w.icon("arrow-right", 16))
    line.append(w.agent_mark(target, 22))
    line.append(w.label(to_name, "pg-strong"))
    line.append(spacer())
    kind = target.get("plan") or target.get("kind_label") or ""
    if kind:
        line.append(w.label(str(kind), "pg-sub"))
    card.append(line)
    brief = w.box("v", 6, "pg-brief")
    brief.append(w.label(f"What {to_name} reads first. Change anything before it starts." if draft
                         else f"What {to_name} read first", "pg-brief-label"))
    text = str(data.get("brief") or entry.get("text") or "")
    if draft:
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
        view.add_css_class("pg-well-text")
        view.get_buffer().set_text(text)
        view.set_size_request(-1, 128)
        view.update_property([Gtk.AccessibleProperty.LABEL], [f"What {to_name} reads first"])
        brief.append(view)
    else:
        brief.append(w.label(text, "pg-well-text", wrap=True, selectable=True))
    card.append(brief)
    if draft and ctx.continue_handoff is not None:
        foot = card.foot(flat=True)

        def go() -> None:
            buffer = view.get_buffer()
            ctx.continue_handoff(data, buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False))
        foot.append(w.button(f"Continue with {to_name}", icon_name_="arrow-right", style="key", on_click=go))
        foot.append(spacer())
        foot.append(w.label("The plan, decisions and history stay in the project folder", "pg-sub", wrap=True,
                            xalign=1.0))
    else:
        card.append(Gtk.Box(height_request=14))
    return card
