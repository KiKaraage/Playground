# SPDX-License-Identifier: Apache-2.0
"""Screens in Preview: the run.toml kinds, GTK apps kept inside the project, and the VNC bridge. No network."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
import urllib.request

from luma_playground.core import preview, screens
from luma_playground.core.paths import ProjectPaths


def fake_vnc_server() -> tuple[socket.socket, int]:
    """Says RFB hello, then echoes whatever it's sent."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            def talk(c=conn) -> None:
                try:
                    c.sendall(b"RFB 003.008\n")
                    while data := c.recv(4096):
                        c.sendall(data)
                except OSError:
                    pass
            threading.Thread(target=talk, daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    return server, server.getsockname()[1]


class RunTomlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".playground").mkdir()

    def write(self, text: str) -> None:
        (self.root / ".playground" / "run.toml").write_text(text)

    def test_kinds_in_peoples_words(self):
        self.write('[app]\nkind = "gtk"\ncommand = "python3 notes.py"\ntoolkit = "gtk3"\n')
        plan = preview.detect(self.root)
        self.assertEqual((plan.target, plan.toolkit), ("desktop-app", "gtk3"))
        self.write('[app]\nkind = "vnc"\nhost = "192.168.1.20"\nscreen_port = 5900\nlabel = "The Mac"\n')
        plan = preview.detect(self.root)
        self.assertEqual((plan.target, plan.host, plan.screen_port, plan.label),
                         ("screen", "192.168.1.20", 5900, "The Mac"))
        self.write('[app]\nkind = "vm"\n')
        self.assertIn("screen_port", preview.detect(self.root).problem)

    def test_gtk_apps_are_found_and_their_toolkit_known(self):
        (self.root / "main.py").write_text("import gi\ngi.require_version('Gtk', '3.0')\nfrom gi.repository import Gtk\n")
        plan = preview.detect(self.root)
        self.assertEqual((plan.source, plan.target, plan.toolkit), ("gtk", "desktop-app", "gtk3"))
        (self.root / "main.py").write_text("import gi\ngi.require_version('Adw', '1')\nfrom gi.repository import Adw\n")
        self.assertEqual(preview.detect(self.root).toolkit, "gtk4")

    def test_an_app_in_preview_keeps_its_settings_in_the_project(self):
        env = preview.sandbox_home(ProjectPaths(self.root))
        for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            self.assertTrue(env[var].startswith(str(self.root / ".playground" / "local" / "preview-home")))
            self.assertTrue(os.path.isdir(env[var]))
        self.assertEqual(env["GSETTINGS_BACKEND"], "keyfile")


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.viewer = Path(self.tmp.name) / "novnc"
        self.viewer.mkdir()
        (self.viewer / "vnc.html").write_text("<html>viewer</html>")
        (self.viewer / "secret.txt").write_text("x")
        self.server, self.vnc_port = fake_vnc_server()
        self.addCleanup(self.server.close)
        self.bridge = screens.ScreenBridge("127.0.0.1", self.vnc_port, viewer=self.viewer)
        self.bridge.start()
        self.addCleanup(self.bridge.stop)

    def test_serves_the_viewer_and_nothing_outside_it(self):
        self.assertTrue(self.bridge.screen_answers())
        self.assertIn("/vnc.html?", self.bridge.url)
        self.assertNotIn("password", self.bridge.url)
        with urllib.request.urlopen(f"http://127.0.0.1:{self.bridge.port}/vnc.html") as page:
            self.assertEqual(page.read(), b"<html>viewer</html>")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{self.bridge.port}/../../etc/passwd")

    def test_relays_the_screen_over_a_websocket(self):
        sock = socket.create_connection(("127.0.0.1", self.bridge.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET /websockify HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      f"Sec-WebSocket-Protocol: binary\r\n\r\n").encode())
        reply = b""
        while b"\r\n\r\n" not in reply:
            reply += sock.recv(1024)
        head, rest = reply.split(b"\r\n\r\n", 1)
        self.assertIn(b"101", head.split(b"\r\n")[0])
        self.assertIn(b"Sec-WebSocket-Protocol: binary", head)

        def frame() -> bytes:
            nonlocal rest
            while len(rest) < 2:
                rest += sock.recv(1024)
            size = rest[1] & 0x7F
            while len(rest) < 2 + size:
                rest += sock.recv(1024)
            payload, rest = rest[2:2 + size], rest[2 + size:]
            return payload
        self.assertEqual(frame(), b"RFB 003.008\n")
        mask = os.urandom(4)
        data = b"hello screen"
        sock.sendall(bytes([0x82, 0x80 | len(data)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        self.assertEqual(frame(), data)
        sock.close()


if __name__ == "__main__":
    unittest.main()
