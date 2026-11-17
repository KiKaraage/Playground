# SPDX-License-Identifier: Apache-2.0
"""New conversations use a real Ari connection, else the first agent set up here. Skips without the Luma kit."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


def gtk_module(name: str):
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        import importlib
        return importlib.import_module(name)
    except (ImportError, ValueError, RuntimeError) as error:
        raise unittest.SkipTest(f"GTK isn't here: {error}")


class DefaultConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_CONFIG_DIR": str(Path(self.tmp.name) / "config")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _default(self, window, items):
        store = mock.Mock()
        store.list.return_value = items
        module = mock.Mock()
        module.ConnectionStore.return_value = store
        with mock.patch.object(window, "_optional", return_value=module):
            return window.default_connection_id()

    @staticmethod
    def _conn(id, provider, *, kind="personal-plan", where="this-computer"):
        return mock.Mock(id=id, provider=provider, kind=kind, where=where)

    def test_first_local_agent_wins_whatever_its_provider(self):
        window = gtk_module("luma_playground.app.window")
        opencode = self._conn("opencode", "opencode")
        claude = self._conn("claude", "claude")
        codex = self._conn("codex", "codex")
        self.assertEqual(self._default(window, [opencode]), "opencode")
        self.assertEqual(self._default(window, [claude]), "claude")
        self.assertEqual(self._default(window, [claude, codex]), "claude")
        self.assertEqual(self._default(window, [codex, claude]), "codex")

    def test_a_real_ari_connection_still_wins(self):
        window = gtk_module("luma_playground.app.window")
        ari = self._conn("ari-chatgpt", "ari", kind="ari")
        self.assertEqual(self._default(window, [self._conn("opencode", "opencode"), ari]), "ari-chatgpt")

    def test_nothing_usable_falls_back_to_the_ari_placeholder(self):
        window = gtk_module("luma_playground.app.window")
        self.assertEqual(self._default(window, []), "ari")
        self.assertEqual(self._default(window, [self._conn("qwen", "ollama", kind="local")]), "ari")
        self.assertEqual(self._default(window, [self._conn("claude-2", "claude", where="other-computer")]), "ari")


if __name__ == "__main__":
    unittest.main()
