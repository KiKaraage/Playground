# SPDX-License-Identifier: Apache-2.0
"""Words in the thread: yours (right, in the accent), a teammate's (left, with their face),
the agent's (left, with who said it, where it ran and how long it took), and Playground's
own quiet notes (centred)."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .. import widgets as w  # noqa: E402
from .base import CardContext, data_of, inline_markup, paragraphs  # noqa: E402

YOU = {"", "me", "you", None}


def build(entry: dict, ctx: CardContext) -> Gtk.Widget:
    role = entry.get("role") or "agent"
    data = data_of(entry)
    text = entry.get("text") or data.get("text") or ""
    if role == "person":
        author = entry.get("by") or data.get("by") or ""
        you = getattr(ctx, "you", "")
        if author not in YOU and author != you:
            name = ctx.person_name(author) if getattr(ctx, "person_name", None) else str(author)
            return _teammate(text, name, entry)
        return _yours(text, entry, data)
    if role == "system" and (data.get("action") or data.get("resolved") or data.get("error")):
        return problem(text, data, ctx)
    if role == "system":
        glyph = data.get("icon") or ("radio" if data.get("released") else "pause" if data.get("paused") else "info")
        return note(_system_words(text), glyph)
    return _agent(text, entry, data, ctx)


def _system_words(text: str) -> str:
    """'Released to Production on Tuesday…': the first words carry the weight (v73 `.pgnote b`)."""
    for lead in ("Released to Production", "Released to", "Undone:", "You paused this"):
        if text.startswith(lead) and "**" not in text:
            return f"**{lead}**{text[len(lead):]}"
    return text


def _yours(text: str, entry: dict, data: dict) -> Gtk.Widget:
    holder = w.box("v", 4, "pg-me", halign=Gtk.Align.END)
    bubble = w.box("v", 6, "pg-me-bubble", halign=Gtk.Align.END)
    about = data.get("about") or data.get("point")
    if about:
        chip = w.box("h", 5, "pg-me-point", halign=Gtk.Align.START)
        chip.append(w.icon("mouse-pointer-click", 12))
        chip.append(w.label(str(about), ellipsize=True, max_width_chars=48))
        bubble.append(chip)
    words = w.label(inline_markup(text), "pg-me-text", wrap=True, markup=True, selectable=True)
    words.set_max_width_chars(60)
    bubble.append(words)
    attachments = data.get("attachments") or []
    for item in attachments:
        name = item.get("name") if isinstance(item, dict) else str(item)
        chip = w.box("h", 5, "pg-me-point", halign=Gtk.Align.START)
        chip.append(w.icon("paperclip", 12))
        chip.append(w.label(name or "Attachment", ellipsize=True, max_width_chars=40))
        bubble.append(chip)
    holder.append(bubble)
    moment = w.parse_ts(entry.get("ts"))
    if moment is not None:
        holder.append(w.label(w.time_words(moment), "pg-stamp", xalign=1.0))
    holder.set_accessible_role(Gtk.AccessibleRole.ARTICLE)
    holder.update_property([Gtk.AccessibleProperty.LABEL], [f"You said: {text}"])
    return holder


def _teammate(text: str, name: str, entry: dict) -> Gtk.Widget:
    row = w.box("h", 8, "pg-them", halign=Gtk.Align.START)
    face = w.person_face(name, 28)
    face.set_valign(Gtk.Align.END)
    face.set_margin_bottom(18)
    row.append(face)
    column = w.box("v", 4)
    bubble = w.box("v", 0, "pg-them-bubble", halign=Gtk.Align.START)
    words = w.label(inline_markup(text), "pg-say-text", wrap=True, markup=True, selectable=True)
    words.set_max_width_chars(60)
    bubble.append(words)
    column.append(bubble)
    moment = w.parse_ts(entry.get("ts"))
    first = name.split()[0] if name else ""
    column.append(w.label(f"{first} · {w.time_words(moment)}" if moment else first, "pg-stamp"))
    row.append(column)
    row.set_accessible_role(Gtk.AccessibleRole.ARTICLE)
    row.update_property([Gtk.AccessibleProperty.LABEL], [f"{name} said: {text}"])
    return row


def _agent(text: str, entry: dict, data: dict, ctx: CardContext) -> Gtk.Widget:
    bubble = w.box("v", 0, "pg-say", halign=Gtk.Align.START)
    bubble.append(paragraphs(text, "pg-say-text", ctx=ctx))
    chips = data.get("goals") or data.get("chips") or []
    if chips and ctx.open_goal is not None:
        wrap = Adw.WrapBox(child_spacing=6, line_spacing=6)
        wrap.add_css_class("pg-chips")
        for chip in chips:
            goal_id = (chip.get("goal_id") or chip.get("goal") or chip.get("id")) if isinstance(chip, dict) else str(chip)
            goal = ctx.goal_for(goal_id) if ctx.goal_for else None
            title = (chip.get("label") if isinstance(chip, dict) else None) or (goal or {}).get("title") or goal_id
            button = Gtk.Button()
            button.add_css_class("pg-chip")
            content = w.box("h", 7)
            content.append(w.StateGlyph((goal or {}).get("status", "draft"), 14))
            content.append(w.label(title))
            button.set_child(content)
            button.connect("clicked", lambda _b, g=goal_id: ctx.open_goal(g))
            wrap.append(button)
        bubble.append(wrap)
    # Who said it, where, when and how long it took: one click on the bubble shows it underneath, small.
    details = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, transition_duration=150)
    details.set_child(footer(entry, data, ctx))
    holder = w.box("v", 0, "pg-say-holder", halign=Gtk.Align.START)
    holder.append(bubble)
    holder.append(details)
    click = Gtk.GestureClick()
    click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

    def toggle(gesture, presses, _x, _y) -> None:
        if presses == 1 and not _selecting(bubble):
            details.set_reveal_child(not details.get_reveal_child())
    click.connect("released", toggle)
    bubble.add_controller(click)
    bubble.set_tooltip_text("Click for details")
    bubble.set_accessible_role(Gtk.AccessibleRole.ARTICLE)
    bubble.update_property([Gtk.AccessibleProperty.LABEL], [f"{ctx.agent_name} said: {text}"])
    return holder


def _selecting(widget: Gtk.Widget) -> bool:
    """A click that ended a text selection isn't a request for details."""
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label) and child.get_selectable():
            bounds = child.get_selection_bounds()
            if bounds and bounds[0] and bounds[1] != bounds[2]:
                return True
        if _selecting(child):
            return True
        child = child.get_next_sibling()
    return False


def footer(entry: dict, data: dict, ctx: CardContext) -> Gtk.Widget:
    """When, who said it, where it ran, and how long it took (shown under the bubble on a click)."""
    line = Adw.WrapBox(child_spacing=7, line_spacing=2)
    line.add_css_class("pg-say-foot")
    moment = w.parse_ts(entry.get("ts"))
    if moment is not None:
        line.append(w.label(w.time_words(moment)))
        line.append(w.label("·", "pg-sep"))
    connection = ctx.connection_of(data.get("connection") or entry.get("by") or None)
    line.append(w.agent_mark(connection, 16))
    name = connection.get("label") or ctx.agent_name
    plan = connection.get("plan") or ""
    line.append(w.label(f"{name} · {plan}" if plan else name))
    line.add_css_class("pg-say-foot-row")
    where = data.get("where") or connection.get("where") or "this-computer"
    where_text = ctx.where_words(where) if ctx.where_words else "This computer"
    line.append(w.label("·", "pg-sep"))
    line.append(w.icon("server" if where not in ("this-computer", "local", "") else "house", 13))
    line.append(w.label(where_text))
    seconds = data.get("duration_s") or data.get("elapsed_s")
    if seconds:
        line.append(w.label("·", "pg-sep"))
        line.append(w.label(f"took {w.duration_words(seconds)}"))
    return line


PROBLEM_GLYPHS = {"offline": "wifi", "sign-in": "key-round", "plan-off": "key-round", "usage-limit": "gauge",
                  "crashed": "circle-alert", "coming-soon": "timer", "no-model": "cpu"}


def problem(text: str, data: dict, ctx: CardContext) -> Gtk.Widget:
    """An agent stopped, and here's the way on (Try again, Sign in, Manage usage). Once a later turn goes
    through, the runner writes the card again with `resolved`, and it quiets down: no buttons, a tick."""
    resolved = bool(data.get("resolved"))
    reason = str(data.get("reason") or ("sign-in" if data.get("action") == "sign-in" else ""))
    card = w.box("v", 10, "pg-problem")
    if resolved:
        card.add_css_class("resolved")
    line = w.box("h", 10)
    glyph = w.icon("check" if resolved else PROBLEM_GLYPHS.get(reason, "circle-alert"), 16, "pg-problem-glyph")
    glyph.set_valign(Gtk.Align.START)
    line.append(glyph)
    words = w.label(inline_markup(text), "pg-problem-text", wrap=True, markup=True, hexpand=True, selectable=True)
    line.append(words)
    card.append(line)
    if resolved:
        card.append(w.label("Sorted: the next try went through.", "pg-sub"))
    actions = w.box("h", 8, "pg-problem-actions")
    buttons: list[Gtk.Button] = []

    def pressed(handler, *args) -> None:
        for button in buttons:
            button.set_sensitive(False)
        handler(*args)

    def add(label: str, icon: str, handler, *args, key: bool = False) -> None:
        button = w.button(label, icon_name_=icon, small=True, style="key" if key else "fill",
                          on_click=lambda: pressed(handler, *args))
        buttons.append(button)
        actions.append(button)

    action = "" if resolved else str(data.get("action") or "")
    retry_label = str(data.get("retry_label") or "Try again")
    if action == "retry" and ctx.retry is not None:
        add(retry_label, "refresh-cw", ctx.retry, key=True)
    elif action == "sign-in":
        for method in data.get("methods") or []:
            if not isinstance(method, dict):
                continue
            if method.get("type") == "agent" and ctx.sign_in is not None:
                # Wording is the provider's own where it's required ("Continue with ChatGPT").
                add(str(method.get("name") or "Sign in"), "key-round", ctx.sign_in, method, key=True)
            elif ctx.sign_in is not None:
                add(str(method.get("name") or "Sign in"), "key-round", ctx.sign_in, method, key=not buttons)
        if ctx.signed_in is not None:
            add(retry_label, "refresh-cw", ctx.signed_in)
    elif action == "manage-usage":
        if data.get("url") and ctx.open_url is not None:
            add("Manage usage", "external-link", ctx.open_url, str(data["url"]), key=True)
        if data.get("secondary") == "retry" and ctx.retry is not None:
            add(retry_label, "refresh-cw", ctx.retry)
    if buttons:
        card.append(actions)
    detail = str(data.get("detail") or "").strip()
    if detail and not resolved:
        expander = Gtk.Expander(label="Details")
        expander.add_css_class("pg-problem-detail")
        expander.set_child(w.label(detail, "pg-mono", wrap=True, selectable=True))
        card.append(expander)
    card.set_accessible_role(Gtk.AccessibleRole.ARTICLE)
    card.update_property([Gtk.AccessibleProperty.LABEL], [("Sorted. " if resolved else "") + text])
    return card


def note(text: str, glyph: str = "info") -> Gtk.Widget:
    """Playground's own quiet line in the thread (v73 `.pgnote`)."""
    line = w.box("h", 8, "pg-note", halign=Gtk.Align.CENTER)
    line.append(w.icon(glyph, 14))
    line.append(w.label(inline_markup(text), wrap=True, markup=True, xalign=0.5))
    return line


def day(text: str) -> Gtk.Widget:
    heading = w.label(text, "pg-day", xalign=0.5, halign=Gtk.Align.CENTER)
    heading.set_accessible_role(Gtk.AccessibleRole.HEADING)
    return heading


def thinking(name: str, what: str = "thinking") -> Gtk.Widget:
    """The agent is on it (v73 `.pgthink`): three dots that breathe, or still dots when motion is reduced."""
    holder = w.box("h", 10, "pg-think", halign=Gtk.Align.START)
    dots = w.box("h", 4, "pg-dots", valign=Gtk.Align.CENTER)
    for index in range(3):
        bead = Gtk.Box()
        bead.add_css_class("pg-bead")
        bead.add_css_class(f"b{index}")
        dots.append(bead)
    holder.append(dots)
    holder.append(w.label(f"{name} is {what}"))
    holder.set_accessible_role(Gtk.AccessibleRole.STATUS)
    holder.update_property([Gtk.AccessibleProperty.LABEL], [f"{name} is {what}"])
    return holder
