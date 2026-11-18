# SPDX-License-Identifier: Apache-2.0
"""Notification click actions across PyGObject versions. Skips without the Luma kit."""
from __future__ import annotations

import unittest


def gtk_module(name: str):
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        import importlib
        return importlib.import_module(name)
    except (ImportError, ValueError, RuntimeError) as error:
        raise unittest.SkipTest(f"GTK isn't here: {error}")


class NotificationTests(unittest.TestCase):
    def test_notification_action_works_with_either_pygobject_name(self):
        window = gtk_module("luma_playground.app.window")
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        target = GLib.Variant.new_string("goal-1")
        note = Gio.Notification.new("hello")
        window.notification_action(note, "app.open-conversation", target)  # no AttributeError

        class OnlyNewApi:
            def __init__(self):
                self.called = None

            def set_default_action_and_target(self, action, value):
                self.called = (action, value)

        stub = OnlyNewApi()
        window.notification_action(stub, "app.open-conversation", target)
        self.assertEqual(stub.called, ("app.open-conversation", target))


if __name__ == "__main__":
    unittest.main()
