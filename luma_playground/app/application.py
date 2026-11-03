# SPDX-License-Identifier: Apache-2.0
"""Playground application: startup, actions and the main window.

The window lives in window.py and the sheets in
sheets/; each sheet module exposes ``present(window, **options)``.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID, VERSION  # noqa: E402
from ..core import events  # noqa: E402
from . import snapshot  # noqa: E402

ICON_NAME = APP_ID
SHEETS = ("picker", "usage", "places", "people", "book", "newproject", "review", "code", "setup")


def data_path(*parts: str) -> Path:
    """Data files from the source tree, or the installed copy (core.paths.app_data decides)."""
    from ..core.paths import app_data
    return app_data(*parts)


def _add_icon_path() -> None:
    """Playground's own symbolic glyphs. Stylesheets are registered by the
    window through luma_appkit.add_style_sheet, after the kit's colours exist."""
    display = Gdk.Display.get_default()
    icons = data_path("icons")
    if display is not None and icons.is_dir():
        Gtk.IconTheme.get_for_display(display).add_search_path(str(icons))


class PlaygroundApplication(Adw.Application):
    def __init__(self) -> None:
        # Development: PLAYGROUND_DARK=1 (or 0) gives this process a private GSettings saying dark (or light).
        # It has to happen before anything reads GSettings, so before the application exists.
        from . import devmode
        devmode.apply_colour_scheme()
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if sys.platform == "darwin":
            # The Mac development harness has no session bus to register on.
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        GLib.set_application_name("Playground")
        self.bus = events.bus
        self._add_actions()

    # Actions: app.new-project, app.open-sheet::<name>, app.quit, app.about
    def _add_actions(self) -> None:
        def simple(name: str, callback, parameter: GLib.VariantType | None = None) -> None:
            action = Gio.SimpleAction.new(name, parameter)
            action.connect("activate", callback)
            self.add_action(action)

        simple("open-sheet", lambda _a, value: self.open_sheet(value.get_string()), GLib.VariantType.new("s"))
        simple("new-project", lambda *_: self.open_sheet("newproject"))
        simple("quit", lambda *_: self.quit())
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.new-project", ["<Control><Shift>n"])

    def open_sheet(self, name: str, **options) -> None:
        window = self.props.active_window
        if window is None or name not in SHEETS:
            return
        module = importlib.import_module(f".sheets.{name}", __package__)
        module.present(window, **options)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        _add_icon_path()

    def do_shutdown(self) -> None:
        """Quitting stops every Stage preview this run started; none may outlive Playground."""
        try:
            from ..core import preview
            preview.stop_all()
        except Exception:  # noqa: BLE001 - quitting must finish
            import logging
            logging.getLogger(__name__).exception("previews didn't all stop")
        Adw.Application.do_shutdown(self)

    def _window(self):
        from .window import PlaygroundWindow
        window = self.props.active_window or PlaygroundWindow(self)
        size = os.environ.get("PLAYGROUND_SIZE", "")
        if "x" in size:
            width, height = (int(v) for v in size.split("x", 1))
            window.set_default_size(width, height)
        return window

    def do_activate(self) -> None:
        window = self._window()
        window.present()
        if os.environ.get("PLAYGROUND_OPEN_SHEET"):
            # Development: open a sheet at start, e.g. "book:section=stack".
            from .sheets import common
            GLib.idle_add(lambda: common.open_from_environment(window) and False)
        snapshot.arm(window, self)

    def do_open(self, files, _n_files, _hint) -> None:
        window = self._window()
        for file in files:
            path = file.get_path()
            if path:
                window.open_project(path)
        window.present()
        snapshot.arm(window, self)


def main(argv: list[str] | None = None) -> int:
    return PlaygroundApplication().run(argv if argv is not None else sys.argv)
