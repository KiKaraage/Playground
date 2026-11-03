# SPDX-License-Identifier: Apache-2.0
"""A keyring that never answers costs one short probe per session, not 15 s per call."""
from __future__ import annotations

import stat
import tempfile
import time
import unittest
from pathlib import Path

from luma_playground.core import security


class KeyringProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        security._KEYRING_ANSWERS.clear()

    def tearDown(self) -> None:
        security._KEYRING_ANSWERS.clear()
        self.tmp.cleanup()

    def tool(self, body: str) -> str:
        path = Path(self.tmp.name) / "secret-tool"
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def test_a_silent_keyring_is_given_up_on_quickly_and_once(self) -> None:
        keyring = security.Keyring(tool=self.tool("sleep 30"))
        began = time.monotonic()
        self.assertFalse(keyring.available())
        self.assertIsNone(keyring.protect_new_key("device-ssh", "device", "label", Path(self.tmp.name)))
        self.assertIsNone(keyring.lookup("vault", "device"))
        self.assertFalse(keyring.store("vault", "device", "secret", "label"))
        self.assertLess(time.monotonic() - began, security.KEYRING_PROBE_S + 3)

    def test_no_secret_service_says_so_on_stderr(self) -> None:
        keyring = security.Keyring(tool=self.tool(
            "echo 'secret-tool: The name org.freedesktop.secrets was not provided' >&2; exit 1"))
        self.assertFalse(keyring.available())

    def test_a_working_keyring_is_used(self) -> None:
        keyring = security.Keyring(tool=self.tool('[ "$1" = lookup ] && printf hello; exit 0'))
        self.assertTrue(keyring.available())
        self.assertEqual(keyring.lookup("vault", "device"), "hello")


if __name__ == "__main__":
    unittest.main()
