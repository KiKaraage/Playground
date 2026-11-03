# SPDX-License-Identifier: Apache-2.0
"""The shell a previewed desktop app sees: run as ``preview_shell.py --device phone -- <app argv>`` on the
preview's own session bus (core.preview gives each run one).

A Luma app asks the shell what it's on (LumaUI listens to ``org.project_luma.ShellState1``: a phone is
"mobile" and gets no title bar, a computer is "desktop") and asks the settings portal for light or dark. On
the person's desktop bus the answers would be the desktop's, whatever size the Preview shows. Here they're
the Preview's: the same contract a real Luma phone, tablet or computer keeps, so every app, in any language,
runs as it would there. Then it starts the app and lives as long as it does, ending with its exit code.

Standalone on purpose (GLib and Gio only): it runs from the installed package with the app's Python.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys

from gi.repository import Gio, GLib

SHELL_NAME, SHELL_PATH = "org.project_luma.ShellState1", "/org/project_luma/ShellState1"
PORTAL_NAME, PORTAL_PATH = "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop"

SHELL_XML = """
<node><interface name="org.project_luma.ShellState1">
  <method name="GetSnapshot"><arg type="s" direction="out"/></method>
  <method name="ReportInputCapabilities"><arg type="s" direction="in"/></method>
  <method name="SetAppearance"><arg type="s" direction="in"/></method>
  <method name="SetFavorites"><arg type="as" direction="in"/></method>
  <method name="SetHomeApps"><arg type="as" direction="in"/></method>
  <method name="SetHomeLayout"><arg type="s" direction="in"/><arg type="s" direction="in"/></method>
  <method name="SetIdleDelay"><arg type="u" direction="in"/></method>
  <method name="SetPresentation"><arg type="s" direction="in"/></method>
  <method name="SetWallpaper"><arg type="s" direction="in"/></method>
  <property name="InputCapabilities" type="a{sb}" access="read"/>
  <property name="Presentation" type="s" access="read"/>
  <signal name="Changed"><arg type="s"/><arg type="s"/></signal>
</interface></node>"""

PORTAL_XML = """
<node><interface name="org.freedesktop.portal.Settings">
  <method name="ReadAll"><arg type="as" direction="in"/><arg type="a{sa{sv}}" direction="out"/></method>
  <method name="Read"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
  <method name="ReadOne"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
  <signal name="SettingChanged"><arg type="s"/><arg type="s"/><arg type="v"/></signal>
  <property name="version" type="u" access="read"/>
</interface></node>"""

# Preview size → what the shell says it is (luma_appkit.context.context_from_session knows these three).
PRESENTATION = {"phone": "mobile", "tablet": "tablet", "desktop": "desktop", "app": "desktop"}
# The settings portal's color-scheme: 0 no preference, 1 dark, 2 light.
COLOR_SCHEME = {"dark": 1, "light": 2}


def presentation(device: str) -> str:
    return PRESENTATION.get(device, "desktop")


def capabilities(device: str) -> dict[str, bool]:
    handheld = device in ("phone", "tablet")
    return {"keyboard": not handheld, "pointer": not handheld, "touchscreen": handheld}


def settings(appearance: str) -> dict[str, dict[str, GLib.Variant]]:
    return {"org.freedesktop.appearance": {"color-scheme": GLib.Variant("u", COLOR_SCHEME.get(appearance, 0))}}


class PreviewShell:
    def __init__(self, connection: Gio.DBusConnection, device: str, appearance: str) -> None:
        self.device, self.appearance = device, appearance
        self.values = settings(appearance)
        shell = Gio.DBusNodeInfo.new_for_xml(SHELL_XML).interfaces[0]
        portal = Gio.DBusNodeInfo.new_for_xml(PORTAL_XML).interfaces[0]
        register = getattr(connection, "register_object_with_closures2", None) or connection.register_object
        register(SHELL_PATH, shell, self._shell_call, self._shell_property, None)
        register(PORTAL_PATH, portal, self._portal_call, self._portal_property, None)

    def _shell_call(self, _conn, _sender, _path, _iface, method, _params, invocation) -> None:
        if method == "GetSnapshot":
            snapshot = {"presentation": presentation(self.device), "appearance":
                        {"dark": "prefer-dark", "light": "prefer-light"}.get(self.appearance, "default")}
            invocation.return_value(GLib.Variant("(s)", (json.dumps(snapshot),)))
        else:
            invocation.return_value(None)        # the preview's shell isn't changed from inside the app

    def _shell_property(self, _conn, _sender, _path, _iface, name):
        if name == "Presentation":
            return GLib.Variant("s", presentation(self.device))
        if name == "InputCapabilities":
            return GLib.Variant("a{sb}", capabilities(self.device))
        return None

    def _portal_call(self, _conn, _sender, _path, _iface, method, params, invocation) -> None:
        if method == "ReadAll":
            wanted = params.unpack()[0]
            found = {ns: keys for ns, keys in self.values.items()
                     if not wanted or any(ns == w or (w.endswith("*") and ns.startswith(w[:-1])) for w in wanted)}
            invocation.return_value(GLib.Variant("(a{sa{sv}})", (found,)))
            return
        namespace, key = params.unpack()
        value = self.values.get(namespace, {}).get(key)
        if value is None:
            invocation.return_dbus_error("org.freedesktop.portal.Error.NotFound", f"{namespace} {key} isn't set")
        elif method == "Read":                   # the old call wraps the value once more
            invocation.return_value(GLib.Variant("(v)", (GLib.Variant("v", value),)))
        else:
            invocation.return_value(GLib.Variant("(v)", (value,)))

    def _portal_property(self, _conn, _sender, _path, _iface, name):
        return GLib.Variant("u", 2) if name == "version" else None


def main(argv: list[str]) -> int:
    device, appearance, command = "desktop", "", []
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--device" and args:
            device = args.pop(0)
        elif arg == "--appearance" and args:
            appearance = args.pop(0)
        elif arg == "--":
            command = args
            break
    if not command:
        print("preview_shell: no app to run", file=sys.stderr)
        return 2
    loop = GLib.MainLoop()
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        PreviewShell(connection, device, appearance)
        owned = {"left": 2}

        def got(*_a) -> None:
            owned["left"] -= 1
            if owned["left"] == 0:
                loop.quit()
        for name in (SHELL_NAME, PORTAL_NAME):
            Gio.bus_own_name_on_connection(connection, name, Gio.BusNameOwnerFlags.DO_NOT_QUEUE, got, got)
        patience = GLib.timeout_add(3000, loop.quit)       # never hold the app back for long
        loop.run()                                          # the names are the shell's before the app asks
        if owned["left"] > 0:
            patience = 0                                    # it fired, and GLib has dropped it
        if patience:
            GLib.source_remove(patience)                    # or it would end the app's loop three seconds in
    except GLib.Error as error:
        print(f"preview_shell: no session bus ({error.message}); the app runs without the preview's shell",
              file=sys.stderr)
    app = subprocess.Popen(command)
    try:
        from gi.repository import GLibUnix
        signal_add = GLibUnix.signal_add
    except ImportError:                          # PyGObject before GLibUnix
        signal_add = GLib.unix_signal_add
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal_add(GLib.PRIORITY_DEFAULT, sig, lambda s=sig: (app.send_signal(s), True)[1])
    code = {"value": 0}

    def ended(_pid, status) -> None:
        code["value"] = os.waitstatus_to_exitcode(status) if status else 0
        loop.quit()
    GLib.child_watch_add(GLib.PRIORITY_DEFAULT, app.pid, ended)
    loop.run()
    value = code["value"]
    return 128 - value if value < 0 else value


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
