# SPDX-License-Identifier: Apache-2.0
"""The preview's sitecustomize, in a real GTK app: text takes no presses while the Preview puts windows back."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SITE = Path(__file__).resolve().parents[1] / "luma_playground" / "core" / "preview_site"

APP = r"""
import os, gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib
win = Gtk.Window()
view = Gtk.TextView()
win.set_child(view)
win.present()
before = view.get_can_target()
open(os.path.join(os.environ["MARKS"], "restored"), "w").write("1")
context = GLib.MainContext.default()
end = GLib.get_monotonic_time() + 1_500_000
while GLib.get_monotonic_time() < end and not view.get_can_target():
    context.iteration(False)
print(before, view.get_can_target())
"""


@unittest.skipUnless(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"), "needs a display")
class RestoreHoldTests(unittest.TestCase):
    def run_app(self, windows: list) -> str:
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "positions.json").write_text(json.dumps({"windows": windows}), encoding="utf-8")
            env = dict(os.environ, PYTHONPATH=str(SITE), PLAYGROUND_PREVIEW_FILL="0", MARKS=home,
                       PLAYGROUND_PREVIEW_STATE=str(Path(home) / "windows.json"))
            done = subprocess.run([sys.executable, "-c", APP], env=env, capture_output=True, text=True, timeout=60)
            if "Namespace Gtk not available" in done.stderr:
                self.skipTest("no GTK 4")
            return done.stdout.strip()

    def test_text_waits_for_the_windows_to_go_back(self):
        self.assertEqual(self.run_app([{"x": 40, "y": 40, "w": 300, "h": 300}]), "False True")

    def test_nothing_to_put_back_leaves_text_alone(self):
        self.assertEqual(self.run_app([]), "True True")


PHONE_APP = r"""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk
win = Gtk.Window()
win.present()
layout = Gtk.Settings.get_default().get_property("gtk-decoration-layout")
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit
    agent = WebKit.WebView().get_settings().get_user_agent()
except (ValueError, ImportError):
    agent = "no webkit Mobile"
print(repr(layout), "Mobile" in agent)
"""


@unittest.skipUnless(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"), "needs a display")
class HandheldTests(unittest.TestCase):
    def run_app(self, device: str) -> str:
        env = dict(os.environ, PYTHONPATH=str(SITE), PLAYGROUND_PREVIEW_FILL="1", PLAYGROUND_PREVIEW_DEVICE=device)
        done = subprocess.run([sys.executable, "-c", PHONE_APP], env=env, capture_output=True, text=True, timeout=60)
        if "Namespace Gtk not available" in done.stderr:
            self.skipTest("no GTK 4")
        return done.stdout.strip()

    def test_on_a_phone_it_runs_as_lumas_handheld_shell_runs_it(self):
        self.assertEqual(self.run_app("phone"), "':' True")

    def test_on_a_computer_it_keeps_its_own_buttons_and_browser(self):
        layout, mobile = self.run_app("desktop").rsplit(" ", 1)
        self.assertNotEqual(layout, "':'")
        self.assertEqual(mobile, "False")


if __name__ == "__main__":
    unittest.main()
