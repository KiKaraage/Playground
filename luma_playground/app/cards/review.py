# SPDX-License-Identifier: Apache-2.0
"""Review: what changes for people, the checks, what happens to data, and where it goes.
"Review and release" opens the review, where Stage and Live sit side by side."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .. import widgets as w  # noqa: E402
from .base import Card, CardContext, data_of, spacer  # noqa: E402


def _checks_summary(value) -> tuple[int, int]:
    if isinstance(value, dict):
        return int(value.get("passed") or 0), int(value.get("total") or 0)
    if isinstance(value, (list, tuple)):
        passed = sum(1 for c in value if (c.get("status") if isinstance(c, dict) else None) == "pass")
        return passed, len(value)
    if isinstance(value, str) and " of " in value:
        try:
            left, right = value.split(" of ", 1)
            return int(left.strip().split()[-1]), int(right.strip().split()[0])
        except (ValueError, IndexError):
            pass
    return 0, 0


def _line(glyph: Gtk.Widget, markup: str) -> Gtk.Widget:
    row = w.box("h", 10, "pg-review-row")
    glyph.set_valign(Gtk.Align.START)
    row.append(glyph)
    row.append(w.label(markup, wrap=True, markup=True, hexpand=True))
    return row


def build(entry: dict, ctx: CardContext) -> Gtk.Widget:
    from html import escape
    data = data_of(entry)
    released_info = data.get("released")
    released = bool(released_info)
    goal_id = str(data.get("goal_id") or ctx.goal_id)
    card = Card("review", accessible="Ready to review")
    if released:
        moment = w.parse_ts(released_info.get("at")) if isinstance(released_info, dict) else None
        when = ""
        if moment is not None:
            day = w.day_words(moment)
            when = w.time_words(moment) if day == "Today" else f"{moment.strftime('%a')} {w.time_words(moment)}"
        card.head("radio", "Released", "good", right=str(data.get("released_text") or when))
    else:
        card.head("eye", "Ready to review", "review")
    card.title(str(data.get("title") or ctx.goal_title or "This goal"))
    rows = w.box("v", 8, "pg-review")
    passed, total = _checks_summary(data.get("checks") or data.get("checks_summary"))
    if total:
        rows.append(_line(w.check_mark("pass" if passed == total else "pending", 18),
                          f"<b>{passed} of {total}</b> checks passed"))
    changes = [str(c) for c in data.get("changes_for_people") or []]
    if changes:
        rows.append(_line(w.icon("users", 16),
                          f"<b>{len(changes)}</b> {'thing changes' if len(changes) == 1 else 'things change'} for people"))
    data_changes = [str(c) for c in data.get("data_changes") or []]
    if data_changes:
        rows.append(_line(w.icon("database", 16), escape(data_changes[0])))
    approved = data.get("approved_by")
    if isinstance(approved, str):
        approved = [approved]
    for who in approved or []:
        name = ctx.person_name(str(who)) if getattr(ctx, "person_name", None) else str(who)
        face = w.person_face(name, 18)
        rows.append(_line(face, f"<b>{escape(name.split()[0])}</b> approved it"))
    card.append(rows)
    foot = card.foot()
    if released:
        foot.append(w.button("See what changed", icon_name_="eye",
                             on_click=lambda: ctx.sheet("review", goal_id=goal_id)))
    else:
        foot.append(w.button("Review and release", icon_name_="radio", style="key",
                             on_click=lambda: ctx.sheet("review", goal_id=goal_id)))
    destination = data.get("destination") or data.get("destination_place")
    if released:
        foot.append(spacer())
        point = released_info.get("restore_point") if isinstance(released_info, dict) else None
        foot.append(w.label(f"Restore point {point} saved" if point else "Restore point saved", "pg-sub",
                            wrap=True, xalign=1.0))
    elif destination:
        name = ctx.place_name(str(destination)) if ctx.place_name else str(destination)
        foot.append(w.label(f"Goes to <b>{escape(name.split(',')[0])}</b>{escape(name[len(name.split(',')[0]):])}",
                            "pg-sub", markup=True, ellipsize=True, xalign=1.0, hexpand=True))
    return card
