# SPDX-License-Identifier: Apache-2.0
"""The composer: who's working first, then what you want (v73 `.pgcomp`).

A floating bar over the foot of the thread. The agent chip on the left says which
connection works on this goal and what it has left; it opens the picker. Then the words
("Tell Ari what to change…"), attach, and send. Return sends; Shift+Return starts a new
line. When you've pointed at something on the Stage, it rides along as a chip.

At phone width the chip folds to the agent's mark and the bar sits above the gesture chin.
"""
from __future__ import annotations

import re

from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, GObject, Gtk  # noqa: E402

from . import widgets as w  # noqa: E402

MAX_HEIGHT = 150


class Composer(Gtk.Box):
    """Emits `send(text, attachments, about)` and `pick-agent()`."""

    __gsignals__ = {
        "send": (GObject.SignalFlags.RUN_FIRST, None, (str, GObject.TYPE_PYOBJECT, str)),
        "stop": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "pick-agent": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "pick-options": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("pg-composer")
        self.set_margin_bottom(12)
        self.set_margin_start(24)
        self.set_margin_end(24)
        self.attachments: list[dict] = []
        self.about: str = ""
        self._agent_name = "Ari"
        self._phone = False

        # What rides along: a point on the Stage, files you attached.
        self.chips = w.box("h", 6, "pg-composer-chips")
        self.chips.set_visible(False)
        self.append(self.chips)

        # The words on top, then who's working and the send key under them (Helm's composer), so the field keeps
        # its width in the conversation column beside an area.
        tools = w.box("h", 6, "pg-composer-tools")
        self.agent = Gtk.Button(valign=Gtk.Align.CENTER)
        self.agent.add_css_class("pg-agent-chip")
        self.agent.set_tooltip_text("Who works on this conversation")
        self.agent.connect("clicked", lambda _b: self.emit("pick-agent"))
        self.agent_mark_slot = w.box("h", 0, valign=Gtk.Align.CENTER)
        self.agent_name = w.label("Ari", "pg-agent-name")
        self.agent_sub = w.label("Luma credits", "pg-agent-sub")
        self.agent_words = w.box("v", 0, valign=Gtk.Align.CENTER)
        self.agent_words.append(self.agent_name)
        self.agent_words.append(self.agent_sub)
        self.agent_chevron = w.icon("chevron-down", 14, "pg-agent-chevron")
        content = w.box("h", 8)
        content.append(self.agent_mark_slot)
        content.append(self.agent_words)
        content.append(self.agent_chevron)
        self.agent.set_child(content)
        tools.append(self.agent)
        # The agent's own settings, beside it: the model and how hard it thinks ("GPT-6 · High").
        self.options_button = Gtk.Button(valign=Gtk.Align.CENTER, visible=False)
        self.options_button.add_css_class("pg-options-chip")
        options_line = w.box("h", 6)
        self.options_words = w.label("Model", "pg-options-words", ellipsize=True, max_width_chars=22)
        options_line.append(w.icon("sliders-horizontal", 14))
        options_line.append(self.options_words)
        options_line.append(w.icon("chevron-down", 12, "pg-agent-chevron"))
        self.options_button.set_child(options_line)
        self.options_button.set_tooltip_text("Model, effort and mode")
        self.options_button.connect("clicked", lambda _b: self.emit("pick-options"))
        tools.append(self.options_button)
        # Ari on someone's ChatGPT plan says so, with OpenAI's usage page one press away (their UI rules).
        self.plan_link = Gtk.Button(valign=Gtk.Align.CENTER, visible=False, tooltip_text="Your ChatGPT plan’s usage")
        self.plan_link.add_css_class("pg-plan-link")
        self.plan_link.set_child(w.label("Manage usage"))
        self.plan_link.connect("clicked", lambda _b: self._open_plan())
        self._plan_url = ""
        tools.append(self.plan_link)
        tools.append(w.box("h", 0, hexpand=True))

        field = Gtk.Overlay(hexpand=True)
        field.add_css_class("pg-composer-field")
        self.view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, hexpand=True,
                                 top_margin=10, bottom_margin=10, left_margin=6, right_margin=6)
        self.view.add_css_class("pg-composer-text")
        self.view.update_property([Gtk.AccessibleProperty.LABEL], ["Message"])
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                         propagate_natural_height=True, max_content_height=MAX_HEIGHT)
        self.scroll.set_child(self.view)
        field.set_child(self.scroll)
        self.placeholder = w.label("Tell Ari what to change…", "pg-composer-placeholder", ellipsize=True,
                                   can_target=False, valign=Gtk.Align.START, halign=Gtk.Align.START)
        # Exactly where the first line of text goes (the view's own margins), not centred in the field.
        self.placeholder.set_margin_start(6)
        self.placeholder.set_margin_top(10)
        field.add_overlay(self.placeholder)
        row = w.box("v", 2, "pg-composer-row")
        row.append(field)

        self.attach = w.icon_button("paperclip", "Attach a file or a screenshot", size=18, on_click=self._attach)
        self.attach.add_css_class("pg-composer-attach")
        self.attach.set_valign(Gtk.Align.CENTER)
        tools.append(self.attach)
        self.send_button = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text="Send")
        self.send_button.add_css_class("pg-send")
        self.send_button.set_child(w.icon("arrow-up", 18))
        self.send_button.update_property([Gtk.AccessibleProperty.LABEL], ["Send"])
        self.send_button.connect("clicked", lambda _b: self.emit("stop") if self._stop_mode else self.submit())
        self._busy = False
        self._stop_mode = False
        tools.append(self.send_button)
        row.append(tools)
        self.row = row
        self.append(row)
        # In place of the words: someone else's goal, or a paused one, says so (v73 `.pgban`).
        self.banner = w.box("h", 12, "pg-banner")
        self.banner.set_visible(False)
        self.append(self.banner)

        buffer = self.view.get_buffer()
        buffer.connect("changed", self._changed)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.view.add_controller(keys)
        # Files and images dropped on the composer, or pasted into it, ride along with the next message.
        drop = Gtk.DropTarget.new(GObject.TYPE_NONE, Gdk.DragAction.COPY)
        drop.set_gtypes([Gdk.FileList, Gdk.Texture])
        drop.connect("drop", self._dropped)
        self.add_controller(drop)
        self.view.connect("paste-clipboard", self._paste)
        self.attach_dir: Path | None = None       # where pasted images are saved (the window sets the project's)
        self._changed(buffer)

    # ── who's working ────────────────────────────────────────────────────
    def set_agent(self, connection: dict, subtitle: str) -> None:
        old, self._agent_name = getattr(self, "_agent_name", "Ari"), connection.get("label") or "Ari"
        if old != self._agent_name:
            # Switching agent keeps the hint's wording but names the new one ("Tell Codex what to change…").
            words = self.placeholder.get_label()
            self.set_placeholder(re.sub(rf"\b{re.escape(old)}\b", self._agent_name, words, count=1))
        w.clear(self.agent_mark_slot)
        self.agent_mark_slot.append(w.agent_mark(connection, 24))
        self.agent_name.set_label(self._agent_name)
        self.agent_sub.set_label(subtitle)
        # The chip is the agent's name; its plan and where it runs are in the conversation's heading.
        self.agent_sub.set_visible(False)
        self.agent.set_tooltip_text(f"{self._agent_name}: {subtitle}. Choose who works on this" if subtitle
                                    else "Choose who works on this")
        spoken = f"{self._agent_name}, {subtitle}. Who works on this conversation" if subtitle else "Who works on this conversation"
        self.agent.update_property([Gtk.AccessibleProperty.LABEL], [spoken])

    def set_options(self, options: list[dict] | None, *, can_choose: bool = True) -> None:
        """Show the agent's settings in a few words: the model, then the effort ("GPT-6 · High")."""
        options = list(options or [])
        self.options = options

        def chosen(category: str) -> str:
            option = next((o for o in options if o.get("category") == category), None)
            if option is None:
                return ""
            current = option.get("currentValue")
            choice = next((c for c in option.get("options") or [] if c.get("value") == current), None)
            return str((choice or {}).get("name") or current or "")

        fast = next((o for o in options if any(k in f"{o.get('id', '')} {o.get('name', '')}".lower()
                                               for k in ("fast", "speed"))), None)
        quick = ""
        if fast is not None:
            first = (fast.get("options") or [{}])[0].get("value")
            quick = "Fast" if fast.get("currentValue") not in (None, first) else ""
        words = " · ".join(filter(None, [chosen("model"), chosen("thought_level"), quick])) or \
            ("Model" if can_choose else "")
        self.options_words.set_label(words)
        self.options_button.set_visible(can_choose)
        self.options_button.update_property([Gtk.AccessibleProperty.LABEL], [f"Model and effort: {words}"])

    def set_plan_link(self, url: str | None) -> None:
        """Show "Manage usage" beside the chip while the agent runs on a ChatGPT plan; None hides it."""
        self._plan_url = url or ""
        self.plan_link.set_visible(bool(url) and not self._phone)

    def _open_plan(self) -> None:
        if self._plan_url:
            Gtk.UriLauncher.new(self._plan_url).launch(self.get_root(), None, None)

    def set_banner(self, lead: Gtk.Widget | None, markup: str = "", actions: tuple = ()) -> None:
        """Say why there's nothing to type here, with what you can do instead. None: back to the words."""
        self._banner_args = (lead, markup, actions)
        w.clear(self.banner)
        shown = lead is not None or bool(markup)
        if shown:
            # The conversation is a narrow column (and a phone narrower still): the words take the width and the
            # buttons go underneath, side by side.
            stacked = True
            self.banner.set_orientation(Gtk.Orientation.VERTICAL if stacked else Gtk.Orientation.HORIZONTAL)
            line = w.box("h", 12) if stacked else self.banner
            if lead is not None:
                if lead.get_parent() is not None:
                    lead.unparent()
                lead.set_valign(Gtk.Align.CENTER)
                line.append(lead)
            line.append(w.label(markup, "pg-banner-text", markup=True, wrap=True, hexpand=True))
            row = w.box("h", 8, halign=Gtk.Align.END) if stacked else self.banner
            for button in actions:
                if button.get_parent() is not None:
                    button.unparent()
                button.set_valign(Gtk.Align.CENTER)
                row.append(button)
            if stacked:
                self.banner.append(line)
                self.banner.append(row)
        self.banner.set_visible(shown)
        self.row.set_visible(not shown)
        self.chips.set_visible(not shown and bool(self.about or self.attachments))
        (self.add_css_class if shown else self.remove_css_class)("banner")

    def set_placeholder(self, text: str) -> None:
        self.placeholder.set_label(text)
        self.view.update_property([Gtk.AccessibleProperty.DESCRIPTION], [text])

    def set_phone(self, phone: bool) -> None:
        """At phone width: the chip is the mark alone, the bar spans the width, above the chin."""
        self._phone = phone
        self.agent_words.set_visible(not phone)
        self.agent_chevron.set_visible(not phone)
        (self.add_css_class if phone else self.remove_css_class)("phone")
        self.set_margin_start(12 if phone else 24)
        self.set_margin_end(12 if phone else 24)
        if getattr(self, "_banner_args", None) and self.banner.get_visible():
            self.set_banner(*self._banner_args)

    def set_busy(self, busy: bool) -> None:
        """While the agent works, an empty composer's send button is Stop (Escape too); typing makes it Send
        again, to add to what the agent is doing."""
        name = self._agent_name
        self._busy = busy
        self.set_placeholder(f"{name} is working. Stop it, or add something…" if busy else
                             f"Tell {name} what to change…")
        self._changed(self.view.get_buffer())

    # ── pointing and attachments ─────────────────────────────────────────
    def set_about(self, description: str) -> None:
        """A point on the Stage rides along with the next message (v73 `.pgptchip`)."""
        self.about = description
        self._draw_chips()
        self.view.grab_focus()

    def add_attachment(self, path: str, name: str | None = None, kind: str = "file") -> None:
        self.attachments.append({"path": path, "name": name or Path(path).name, "kind": kind})
        self._draw_chips()

    def _draw_chips(self) -> None:
        w.clear(self.chips)
        if self.about:
            self.chips.append(self._chip("mouse-pointer-click", f"About {self.about}", self._clear_about))
        for index, item in enumerate(self.attachments):
            glyph = "circle-play" if item.get("kind") == "recording" else "paperclip"
            self.chips.append(self._chip(glyph, item["name"], lambda i=index: self._remove(i)))
        self.chips.set_visible(bool(self.about or self.attachments))
        self._changed(self.view.get_buffer())

    def _chip(self, glyph: str, text: str, on_remove) -> Gtk.Widget:
        chip = w.box("h", 6, "pg-ptchip")
        chip.append(w.icon(glyph, 13))
        chip.append(w.label(text, ellipsize=True, max_width_chars=42))
        close = w.icon_button("x", f"Remove {text}", size=12, on_click=on_remove, css="pg-ptchip-x")
        chip.append(close)
        return chip

    def _clear_about(self) -> None:
        self.about = ""
        self._draw_chips()

    def _remove(self, index: int) -> None:
        if 0 <= index < len(self.attachments):
            del self.attachments[index]
        self._draw_chips()

    def _attach(self) -> None:
        dialog = Gtk.FileDialog(title="Attach a file or a screenshot")
        dialog.open_multiple(self.get_root(), None, self._attached)

    def _attached(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        for index in range(files.get_n_items()):
            path = files.get_item(index).get_path()
            if path:
                self.add_attachment(path)

    def _dropped(self, _target, value, _x, _y) -> bool:
        if isinstance(value, Gdk.Texture):
            return self._add_image(value)
        added = False
        for file in value.get_files():
            path = file.get_path()
            if path:
                self.add_attachment(path)
                added = True
        return added

    def _paste(self, view: Gtk.TextView) -> None:
        """Pasting an image (a screenshot) or copied files attaches them; pasting words pastes words."""
        clipboard = view.get_clipboard()
        formats = clipboard.get_formats()
        if formats.contain_gtype(Gdk.FileList):
            view.stop_emission_by_name("paste-clipboard")

            def files(board, result) -> None:
                try:
                    value = board.read_value_finish(result)
                except GLib.Error:
                    return
                for file in value.get_files() if value is not None else []:
                    if file.get_path():
                        self.add_attachment(file.get_path())
            clipboard.read_value_async(Gdk.FileList, GLib.PRIORITY_DEFAULT, None, files)
        elif formats.contain_gtype(Gdk.Texture) and not formats.contain_mime_type("text/plain"):
            view.stop_emission_by_name("paste-clipboard")

            def image(board, result) -> None:
                try:
                    texture = board.read_texture_finish(result)
                except GLib.Error:
                    return
                if texture is not None:
                    self._add_image(texture)
            clipboard.read_texture_async(None, image)

    def _add_image(self, texture: Gdk.Texture) -> bool:
        import tempfile
        import time
        folder = self.attach_dir or Path(tempfile.gettempdir())
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / time.strftime("pasted-%Y%m%d-%H%M%S.png")
            texture.save_to_png(str(path))
        except (GLib.Error, OSError):
            return False
        self.add_attachment(str(path), "Pasted image", kind="image")
        return True

    # ── words ────────────────────────────────────────────────────────────
    def text(self) -> str:
        buffer = self.view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def set_text(self, text: str) -> None:
        self.view.get_buffer().set_text(text)
        self.view.grab_focus()

    def focus(self) -> None:
        self.view.grab_focus()

    def _changed(self, buffer: Gtk.TextBuffer) -> None:
        empty = buffer.get_char_count() == 0
        self.placeholder.set_visible(empty)
        ready = not empty and bool(self.text().strip()) or bool(self.attachments)
        stop = getattr(self, "_busy", False) and not ready
        if stop != getattr(self, "_stop_mode", False):
            self._stop_mode = stop
            self.send_button.set_child(w.icon("stop" if stop else "arrow-up", 18))
            self.send_button.set_tooltip_text("Stop (Esc)" if stop else "Send")
            self.send_button.update_property([Gtk.AccessibleProperty.LABEL], ["Stop" if stop else "Send"])
            (self.send_button.add_css_class if stop else self.send_button.remove_css_class)("stop")
        (self.send_button.add_css_class if ready or stop else self.send_button.remove_css_class)("on")
        self.send_button.set_sensitive(ready or stop)

    def _key(self, _controller, keyval, _code, state) -> bool:
        if keyval == Gdk.KEY_Escape and getattr(self, "_stop_mode", False):
            self.emit("stop")
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            if state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK):
                if state & Gdk.ModifierType.SHIFT_MASK:
                    return False   # Shift+Return: a new line
            self.submit()
            return True
        if keyval == Gdk.KEY_Escape and self.about:
            self._clear_about()
            return True
        return False

    def submit(self) -> None:
        text = self.text().strip()
        if not text and not self.attachments:
            return
        attachments = list(self.attachments)
        about = self.about
        self.view.get_buffer().set_text("")
        self.attachments = []
        self.about = ""
        self._draw_chips()
        self.emit("send", text, attachments, about)
