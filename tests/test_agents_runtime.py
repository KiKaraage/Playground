# SPDX-License-Identifier: Apache-2.0
"""Playground's own Node.js: found, downloaded, checked and unpacked. No network."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from luma_playground.agents import registry, runtime

PLAT = "linux-x86_64"
TOP = f"node-v{runtime.NODE_VERSION}-linux-x64"


def node_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data, mode in ((f"{TOP}/bin/node", b"#!/bin/sh\necho v24.21.0\n", 0o755),
                                 (f"{TOP}/lib/node_modules/npm/bin/npm-cli.js", b"// npm\n", 0o644)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(f"{TOP}/bin/npm")
        link.type, link.linkname = tarfile.SYMTYPE, "../lib/node_modules/npm/bin/npm-cli.js"
        tf.addfile(link)
    return buf.getvalue()


_original_system_node = runtime.system_node   # before the tests patch it out


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (mock.patch.dict(os.environ, {"PLAYGROUND_DATA_DIR": str(Path(self.tmp.name) / "data")}),
                        mock.patch.object(registry, "platform_key", return_value=PLAT),
                        mock.patch.object(runtime, "system_node", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.data = node_tarball()
        self.builds = mock.patch.dict(runtime.NODE_BUILDS, {PLAT: ("linux-x64.tar.gz",
                                                                    hashlib.sha256(self.data).hexdigest())})
        self.builds.start()
        self.addCleanup(self.builds.stop)
        self.urls = []

    def opener(self, request, timeout=None):
        self.urls.append(request.full_url)
        return FakeResponse(self.data)

    def test_downloads_checks_and_unpacks_once(self):
        steps = []
        node = runtime.ensure_node(opener=self.opener, progress=lambda words, part: steps.append((words, part)))
        self.assertTrue(node.own)
        self.assertTrue(os.access(node.node, os.X_OK))
        self.assertTrue(node.npm_cli.endswith("npm-cli.js"))
        self.assertEqual(self.urls, [f"https://nodejs.org/dist/v{runtime.NODE_VERSION}/"
                                     f"node-v{runtime.NODE_VERSION}-linux-x64.tar.gz"])
        self.assertEqual(steps[-2][1], 1.0)   # the download reached 100%
        again = runtime.ensure_node(opener=self.opener)
        self.assertEqual(again.node, node.node)
        self.assertEqual(len(self.urls), 1)   # already there: no second download
        self.assertEqual([p.name for p in runtime.runtime_dir().iterdir()], [TOP])

    def test_bad_checksum_is_thrown_away(self):
        with mock.patch.dict(runtime.NODE_BUILDS, {PLAT: ("linux-x64.tar.gz", "00" * 32)}):
            with self.assertRaises(runtime.SetupError):
                runtime.ensure_node(opener=self.opener)
        self.assertIsNone(runtime.own_node())
        self.assertEqual([p for p in runtime.runtime_dir().iterdir()], [])

    def test_computer_node_is_used_when_good_enough(self):
        mine = runtime.Node("/usr/bin/node", "/usr/lib/node_modules/npm/bin/npm-cli.js", False)
        with mock.patch.object(runtime, "system_node", return_value=mine):
            self.assertEqual(runtime.ensure_node(opener=self.opener), mine)
        self.assertEqual(self.urls, [])

    def test_unknown_computer_says_so(self):
        with mock.patch.object(registry, "platform_key", return_value="plan9-mips"):
            with self.assertRaises(runtime.SetupError):
                runtime.ensure_node(opener=self.opener)

    def test_old_system_node_is_not_used(self):
        bindir = Path(self.tmp.name) / "sys" / "bin"
        npm = Path(self.tmp.name) / "sys" / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
        npm.parent.mkdir(parents=True)
        npm.write_text("")
        bindir.mkdir(parents=True)
        node = bindir / "node"
        node.write_text("#!/bin/sh\necho v18.19.0\n")
        node.chmod(0o755)
        self.assertIsNone(_original_system_node(str(bindir)))
        node.write_text("#!/bin/sh\necho v24.1.0\n")
        found = _original_system_node(str(bindir))
        self.assertIsNotNone(found)
        self.assertEqual(Path(found.npm_cli), npm.resolve())


if __name__ == "__main__":
    unittest.main()
