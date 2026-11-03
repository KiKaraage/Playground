#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Development tool: open one Playground sheet in a real window and save a PNG.

    python3 tools/shoot_sheet.py places --out /tmp/places.png --size 1440x900 [--dark]
                                 [--opt detail=claude] [--project <dir>] [--goal <id>] [--delay 1500]

It uses the app's own window when window.py is there, and otherwise a plain
LumaUI AppWindow standing in for it, so a sheet can be drawn before the rest
of the window lands. Run it inside the VM through vm-run.sh (GTK doesn't run
on the Mac host). PLAYGROUND_HOME should point at a sandbox, never at the real
Documents folder.
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk  # noqa: E402


def parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("sheet")
    parser.add_argument("--out", required=True)
    parser.add_argument("--size", default="1440x900")
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--opt", action="append", default=[])
    parser.add_argument("--project")
    parser.add_argument("--goal")
    parser.add_argument("--delay", type=int, default=1600)
    parser.add_argument("--then", action="append", default=[],
                        help="python run after presenting, with `sheet` and `window` bound (scroll, click…)")
    return parser.parse_args()


def stand_in_window(application: Adw.Application) -> Gtk.Window:
    """A LumaUI window with an empty body, for when window.py hasn't landed."""
    import luma_appkit as kit
    from luma_playground import APP_ID

    window = kit.AppWindow(application=application, app_id=APP_ID, title="Playground", icon_name=APP_ID,
                           commands=kit.CommandRegistry(), default_width=1440, default_height=900)
    body = Gtk.Box(hexpand=True, vexpand=True)
    rail = Gtk.Box(width_request=300)
    rail.add_css_class("pg-shoot-rail")
    body.append(rail)
    body.append(Gtk.Box(hexpand=True))
    setter = getattr(window, "set_body", None) or getattr(window, "set_content_body", None)
    if callable(setter):
        setter(body)
    else:
        window.body.append(body) if isinstance(window.body, Gtk.Box) else None
    return window


def main() -> int:
    args = parse()
    if args.dark:
        os.environ["PLAYGROUND_DARK"] = "1"
    # LumaUI follows the desktop's colour scheme (it resets a forced one), so give this process a
    # private settings store that says dark or light, without touching the VM's own settings.
    os.environ["GSETTINGS_BACKEND"] = "memory"
    from gi.repository import Gio
    schema = Gio.SettingsSchemaSource.get_default().lookup("org.gnome.desktop.interface", True)
    if schema is not None and schema.has_key("color-scheme"):
        Gio.Settings.new("org.gnome.desktop.interface").set_string(
            "color-scheme", "prefer-dark" if args.dark else "prefer-light")
    from luma_playground.app import application as app_module
    from luma_playground.app import snapshot

    width, height = (int(v) for v in args.size.split("x", 1))
    options: dict[str, object] = {}
    for item in args.opt:
        key, _, value = item.partition("=")
        options[key] = value or True
    if args.project:
        options["project"] = args.project
    if args.goal:
        options["goal"] = args.goal

    application = Adw.Application(application_id="org.projectluma.Playground.Shoot",
                                  flags=__import__("gi.repository.Gio", fromlist=["Gio"]).ApplicationFlags.NON_UNIQUE)

    def activate(app: Adw.Application) -> None:
        loader = getattr(app_module, "_load_styles", None) or getattr(app_module, "load_styles", None)
        if callable(loader):
            loader()
        if args.dark:
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        else:
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        window = None
        if not os.environ.get("SHOOT_STAND_IN"):
            try:
                from luma_playground.app.window import PlaygroundWindow
                window = PlaygroundWindow(app)
                if args.project and hasattr(window, "open_project"):
                    window.open_project(args.project)
            except Exception as error:  # noqa: BLE001 - the real window may be mid-change; draw the sheet anyway
                print(f"(real window unavailable: {error!r}; using a stand-in)", flush=True)
                window = None
        if window is None:
            window = stand_in_window(app)
        window.set_default_size(width, height)
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if args.dark else Adw.ColorScheme.FORCE_LIGHT)
        window.present()

        def show() -> bool:
            module = importlib.import_module(f"luma_playground.app.sheets.{args.sheet}")
            sheet = module.present(window, **options)
            for code in args.then:
                GLib.timeout_add(500, lambda c=code: (exec(c, {"sheet": sheet, "window": window, "GLib": GLib, "Gtk": Gtk}), False)[1])
            return False

        def shoot() -> bool:
            ok = snapshot.render_to_png(window, args.out)
            print(f"snapshot {'saved' if ok else 'FAILED'}: {args.out}", flush=True)
            app.quit()
            return False

        GLib.timeout_add(400, show)
        GLib.timeout_add(400 + args.delay, shoot)

    application.connect("activate", activate)
    return application.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
