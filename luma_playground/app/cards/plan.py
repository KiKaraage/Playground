# SPDX-License-Identifier: Apache-2.0
"""Plan: the goal's stories, each with checks a person can see, and the size and cost before
any work starts."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .. import widgets as w  # noqa: E402
from .base import Card, CardContext, agreed, data_of, spacer  # noqa: E402

SIZES = {"small": "Small", "medium": "Medium", "large": "Large"}


def _estimate_text(estimate) -> str:
    if isinstance(estimate, dict):
        return str(estimate.get("amount_text") or estimate.get("text") or "")
    return str(estimate or "")


def _agreed_words(agreed_by, ctx: CardContext) -> str:
    """'You said go · Tue 4:12 PM', 'Priya said go · 9:23 AM'."""
    if not isinstance(agreed_by, dict):
        return "You said go"
    who = str(agreed_by.get("by") or "")
    you = getattr(ctx, "you", "")
    name = "You" if not who or who == you else (ctx.person_name(who) if getattr(ctx, "person_name", None) else who).split()[0]
    moment = w.parse_ts(agreed_by.get("at"))
    if moment is None:
        return f"{name} said go"
    day = w.day_words(moment)
    when = w.time_words(moment) if day == "Today" else f"{moment.strftime('%a')} {w.time_words(moment)}"
    return f"{name} said go · {when}"


def build(entry: dict, ctx: CardContext) -> Gtk.Widget:
    data = data_of(entry)
    state = str(data.get("state") or data.get("status") or ("agreed" if data.get("agreed") else "proposed"))
    if state == "proposed" and ctx.goal_status == "paused":
        state = "paused"
    card = Card("plan", accessible="Plan")
    if state in ("agreed", "started", "approved"):
        right = agreed(str(data.get("agreed_text") or _agreed_words(data.get("agreed"), ctx)))
    elif state == "paused":
        right = w.label("Paused", "pg-card-meta")
    else:
        right = w.label("Waiting for you", "pg-card-meta")
    card.head("list-checks", "Plan", right=right)
    card.title(str(data.get("title") or ctx.goal_title or "The plan"))
    body = card.body(12)
    stories = w.box("v", 12, "pg-stories")
    for number, story in enumerate(data.get("stories") or [], start=1):
        if isinstance(story, str):
            story = {"title": story, "checks": []}
        row = Gtk.Grid(column_spacing=10, row_spacing=4)
        badge = w.label(str(number), "pg-story-number", xalign=0.5, valign=Gtk.Align.START)
        row.attach(badge, 0, 0, 1, 2)
        row.attach(w.label(str(story.get("title") or ""), "pg-story-title", wrap=True, hexpand=True), 1, 0, 1, 1)
        checks = w.box("v", 3)
        for check in story.get("checks") or []:
            line = w.box("h", 8, "pg-story-check")
            eye = w.icon("eye", 14)
            eye.set_valign(Gtk.Align.START)
            eye.set_margin_top(2)
            line.append(eye)
            line.append(w.label(str(check), wrap=True, hexpand=True))
            checks.append(line)
        row.attach(checks, 1, 1, 1, 1)
        stories.append(row)
    body.append(stories)
    size = str(data.get("size_text") or data.get("size") or "")
    cost = _estimate_text(data.get("estimate"))
    if size or cost:
        grid = Gtk.Grid(column_homogeneous=True)
        grid.add_css_class("pg-estimate")
        for column, (key, value) in enumerate((("Size", SIZES.get(size.lower(), size)), ("Cost", cost))):
            cell = w.box("v", 1, "pg-estimate-cell")
            cell.append(w.label(key, "pg-estimate-key"))
            cell.append(w.label(value or "Not estimated yet", "pg-estimate-value", wrap=True))
            grid.attach(cell, column, 0, 1, 1)
        body.append(grid)
    if state == "proposed" and ctx.send is not None:
        foot = card.foot()
        foot.append(w.button("Go", icon_name_="play", style="key", on_click=lambda: ctx.send("Go")))
        if ctx.focus_composer is not None:
            foot.append(w.button("Change something", icon_name_="pencil",
                                 on_click=lambda: ctx.focus_composer("")))
        foot.append(spacer())
        foot.append(w.label("Checks you can see are how I’ll prove it’s done", "pg-sub", wrap=True))
    return card
