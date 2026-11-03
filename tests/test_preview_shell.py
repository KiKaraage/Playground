# SPDX-License-Identifier: Apache-2.0
"""The preview's own shell: on the preview's bus, a phone is a phone to every Luma app (ShellState1) and
light or dark is Playground's (the settings portal), whatever the desktop's own shell says."""
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

SHELL = Path(__file__).resolve().parents[1] / "luma_playground" / "core" / "preview_shell.py"
ASK = ("gdbus introspect --session --dest org.project_luma.ShellState1 --object-path /org/project_luma/ShellState1 "
       "--only-properties; gdbus call --session --dest org.freedesktop.portal.Desktop --object-path "
       "/org/freedesktop/portal/desktop --method org.freedesktop.portal.Settings.ReadOne org.freedesktop.appearance "
       "color-scheme")


@unittest.skipUnless(shutil.which("dbus-run-session") and shutil.which("gdbus"), "needs a session bus to start")
class PreviewShellTests(unittest.TestCase):
    def ask(self, device: str, appearance: str) -> str:
        done = subprocess.run(["dbus-run-session", "--", sys.executable, str(SHELL), "--device", device,
                               "--appearance", appearance, "--", "sh", "-c", ASK],
                              capture_output=True, text=True, timeout=60)
        return done.stdout

    def test_a_phone_is_a_phone_and_dark_is_dark(self):
        said = self.ask("phone", "dark")
        self.assertIn("Presentation = 'mobile'", said)
        self.assertIn("'touchscreen': true", said)
        self.assertIn("(<uint32 1>,)", said)

    def test_the_app_window_view_is_a_computer(self):
        said = self.ask("app", "light")
        self.assertIn("Presentation = 'desktop'", said)
        self.assertIn("(<uint32 2>,)", said)


    def test_the_shell_lives_as_long_as_the_app_and_ends_with_its_code(self):
        import time
        started = time.monotonic()
        done = subprocess.run(["dbus-run-session", "--", sys.executable, str(SHELL), "--device", "phone", "--",
                               "sh", "-c", "sleep 4; exit 7"], capture_output=True, text=True, timeout=60)
        self.assertGreaterEqual(time.monotonic() - started, 4)
        self.assertEqual(done.returncode, 7)


if __name__ == "__main__":
    unittest.main()
