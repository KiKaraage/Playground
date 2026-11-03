# SPDX-License-Identifier: Apache-2.0
"""Decision: only you can decide. Two to four options with what each means, one recommended,
and a default the agent takes if you say "you pick"."""
from __future__ import annotations

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GObject, Gtk  # noqa: E402

from .. import widgets as w  # noqa: E402
from .base import Card, CardContext, agreed, data_of, spacer  # noqa: E402


def _options(data: dict) -> list[dict]:
    options = []
    for index, option in enumerate(data.get("options") or []):
        if isinstance(option, str):
            option = {"id": str(index), "label": option}
        if not isinstance(option, dict):
            continue
        options.append({"id": str(option.get("id") or index), "label": str(option.get("label") or ""),
                        "consequence": str(option.get("consequence") or option.get("description") or "")})
    return options


def _answer_of(data: dict) -> str | None:
    answered = data.get("answered")
    if isinstance(answered, dict):
        return str(answered.get("option") or answered.get("id") or answered.get("chosen") or "") or None
    if isinstance(answered, str) and answered:
        return answered
    if data.get("chosen"):
        return str(data["chosen"])
    return None


class DecisionCard(Card):
    def __init__(self, entry: dict, ctx: CardContext) -> None:
        data = data_of(entry)
        question = str(data.get("question") or entry.get("text") or "")
        super().__init__("decision", accessible=f"Only you can decide: {question}")
        self.ctx = ctx
        self.data = data
        self.decision_id = str(data.get("id") or "")
        self.options = _options(data)
        self.recommended = str(data.get("recommended") or "")
        ids = [o["id"] for o in self.options]
        default = str(data.get("default") or "")
        # `default` is an option id, or the agent's own sentence saying what it would choose.
        self.default_words = default if default and default not in ids else ""
        self.default = default if default in ids else (self.recommended or (ids[0] if ids else ""))
        self.selected: str | None = None
        self.answer = _answer_of(data)
        self.rows: dict[str, Gtk.ToggleButton] = {}
        self._build(question)

    # ── building ─────────────────────────────────────────────────────────
    def _build(self, question: str) -> None:
        answered = self.answer is not None
        if answered:
            self.add_css_class("answered")
            when = self.data.get("answered_text") or self._answered_words()
            self.head("hand", "Your decision", "needs", right=agreed(str(when)))
        else:
            self.head("hand", "Only you can decide", "needs")
        self.title(question)
        body = self.body(6)
        group = w.box("v", 6, "pg-options", accessible_role=Gtk.AccessibleRole.RADIO_GROUP)
        group.update_property([Gtk.AccessibleProperty.LABEL], [question])
        first: Gtk.ToggleButton | None = None
        for option in self.options:
            row = Gtk.ToggleButton(accessible_role=Gtk.AccessibleRole.RADIO)
            row.add_css_class("pg-option")
            if first is None:
                first = row
            else:
                row.set_group(first)
            grid = Gtk.Grid(column_spacing=12, row_spacing=2)
            ring = Gtk.Box(valign=Gtk.Align.START)
            ring.add_css_class("pg-radio")
            if answered and option["id"] == self.answer:
                tick = w.icon("check", 11)
                tick.set_hexpand(True)
                ring.append(tick)
            ring.set_hexpand(False)
            grid.attach(ring, 0, 0, 1, 2)
            title = w.box("h", 8)
            title.append(w.label(option["label"], "pg-option-title", wrap=True))
            if option["id"] == self.recommended:
                title.append(w.tag("Recommended", "", "star"))
            grid.attach(title, 1, 0, 1, 1)
            if option["consequence"]:
                grid.attach(w.label(option["consequence"], "pg-option-text", wrap=True, hexpand=True), 1, 1, 1, 1)
            row.set_child(grid)
            spoken = option["label"] + (", recommended" if option["id"] == self.recommended else "")
            row.update_property([Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
                                [spoken, option["consequence"]])
            if answered:
                row.set_sensitive(False)
                row.add_css_class("picked" if option["id"] == self.answer else "dim")
                row.set_active(option["id"] == self.answer)
            else:
                row.connect("toggled", self._toggled, option["id"])
            self.rows[option["id"]] = row
            group.append(row)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._arrow)
        group.add_controller(keys)
        body.append(group)
        if not answered:
            default_label = next((o["label"] for o in self.options if o["id"] == self.default), "")
            if self.default_words:
                body.append(w.label(self.default_words, "pg-default", wrap=True))
            elif default_label:
                body.append(w.label(f"If you’d rather not choose, say “you pick” and I’ll go with "
                                    f"{default_label[:1].lower() + default_label[1:]}.", "pg-default", wrap=True))
            foot = self.foot()
            self.decide = w.button("Decide", icon_name_="check", style="key", on_click=self._decide)
            self.decide.set_sensitive(False)
            foot.append(self.decide)
            foot.append(w.button("You pick", icon_name_="sparkles", on_click=self._you_pick))
            foot.append(spacer())
            foot.append(w.label(f"{self.ctx.agent_name} waits for this. Nothing else is blocked.", "pg-sub",
                                wrap=True, xalign=1.0))
            self.status = w.label("", "pg-sub", visible=False)
            foot.append(self.status)
            # v73: the recommended option starts chosen, so Decide is one press when you agree.
            if self.recommended in self.rows:
                self.rows[self.recommended].set_active(True)

    def _answered_words(self) -> str:
        answered = self.data.get("answered") if isinstance(self.data.get("answered"), dict) else {}
        who = str(answered.get("by") or "")
        if who == "playground":                 # closed unanswered (store.expire_decisions)
            return ("No answer in time · the agent went ahead with this" if answered.get("option")
                    else "No answer in time · closed")
        you = getattr(self.ctx, "you", "")
        name = "You" if not who or who == you else (
            self.ctx.person_name(who) if getattr(self.ctx, "person_name", None) else who).split()[0]
        moment = w.parse_ts(answered.get("at"))
        if moment is None:
            return f"{name} chose"
        day = w.day_words(moment)
        return f"{name} chose · {'today' if day == 'Today' else day}"

    # ── acting ───────────────────────────────────────────────────────────
    def _toggled(self, row: Gtk.ToggleButton, option_id: str) -> None:
        if row.get_active():
            self.selected = option_id
            for key, other in self.rows.items():
                # CHECKED is a tristate (an int), not a boolean.
                checked = Gtk.AccessibleTristate.TRUE if key == option_id else Gtk.AccessibleTristate.FALSE
                other.update_state([Gtk.AccessibleState.CHECKED], [GObject.Value(GObject.TYPE_INT, int(checked))])
        self.decide.set_sensitive(self.selected is not None)

    def _arrow(self, _controller, keyval, _code, _state) -> bool:
        keys = list(self.rows)
        if not keys or keyval not in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Left, Gdk.KEY_Right):
            return False
        focused = next((k for k, r in self.rows.items() if r.has_focus()), keys[0])
        step = -1 if keyval in (Gdk.KEY_Up, Gdk.KEY_Left) else 1
        target = keys[(keys.index(focused) + step) % len(keys)]
        self.rows[target].grab_focus()
        if self.rows[target].get_sensitive():
            self.rows[target].set_active(True)
        return True

    def _decide(self) -> None:
        if self.selected:
            self._answer(self.selected)

    def _you_pick(self) -> None:
        if self.default:
            self._answer(self.default)

    def _answer(self, option_id: str) -> None:
        if self.ctx.answer_decision is None or not self.decision_id:
            return
        if not self.ctx.answer_decision(self.decision_id, option_id):
            self.status.set_label("That didn’t save. Try again in a moment.")
            self.status.set_visible(True)
            return
        # Settle in place: the same card, now showing what you chose.
        self.answer = option_id
        self.data = dict(self.data, answered={"option": option_id}, answered_text="You chose · just now")
        child = self.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.remove(child)
            child = following
        self.rows = {}
        self._build(str(self.data.get("question") or ""))
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"Your decision: {self.data.get('question', '')}"])


def build(entry: dict, ctx: CardContext) -> Gtk.Widget:
    return DecisionCard(entry, ctx)
