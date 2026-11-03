# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the platform tests (preview, checks, places, security, usage).

No tests live here. ``PlatformTestCase`` gives each test its own data dir,
projects root and project, and can put fake ``ssh`` / ``secret-tool`` /
``adb`` programs first on PATH.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from luma_playground.core import events  # noqa: E402
from luma_playground.core.model import Member, Project  # noqa: E402
from luma_playground.core.store import ProjectStore  # noqa: E402

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

CRYPTO_SKIP = "python3-cryptography isn't installed here (expected on the Mac host; the VM has it)"
HAVE_SSH_KEYGEN = shutil.which("ssh-keygen") is not None


FAKE_SSH = r'''#!/usr/bin/env python3
"""A stand-in for ssh: logs its argv, answers by host, can serve a forwarded port."""
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_SSH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
dest = args[args.index("--") + 1] if "--" in args else ""
host = dest.split("@", 1)[-1]
if host.startswith("nowhere"):
    sys.stderr.write(f"ssh: Could not resolve hostname {host}: Name or service not known\n"); sys.exit(255)
if host.startswith("denied"):
    sys.stderr.write(f"{dest}: Permission denied (publickey).\n"); sys.exit(255)
if host.startswith("changed"):
    sys.stderr.write("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key verification failed.\n"); sys.exit(255)
if host.startswith("slow"):
    sys.stderr.write(f"ssh: connect to host {host} port 22: Operation timed out\n"); sys.exit(255)
if "-L" in args:
    lport = args[args.index("-L") + 1].split(":")[0]
    serve = os.environ.get("FAKE_SSH_SERVE", ".")
    sys.path.insert(0, os.environ["FAKE_SSH_ROOT"])
    from luma_playground.core.preview import static_server_argv
    argv = static_server_argv(serve, lport, python=sys.executable)
    os.execvp(argv[0], argv)
print("playground-ok")
print("Linux x86_64")
'''

FAKE_SECRET_TOOL = r'''#!/usr/bin/env python3
"""A file-backed stand-in for libsecret's secret-tool."""
import hashlib, os, sys
args = sys.argv[1:]
store = os.environ["FAKE_KEYRING_DIR"]
cmd, rest = args[0], args[1:]
if cmd == "store":
    rest = rest[2:] if rest and rest[0] == "--label" else [a for a in rest if not a.startswith("--label=")]
key = hashlib.sha256("\0".join(rest).encode()).hexdigest()
path = os.path.join(store, key)
if cmd == "store":
    with open(path, "w") as f:
        f.write(sys.stdin.read())
elif cmd == "lookup":
    if not os.path.exists(path):
        sys.exit(1)
    sys.stdout.write(open(path).read())
elif cmd == "clear":
    if os.path.exists(path):
        os.unlink(path)
'''

FAKE_ADB = r'''#!/bin/sh
echo "List of devices attached"
echo "emulator-5554	device"
'''


def quick_http_server(directory: str | Path):
    """An in-process static server on a free port (no reverse-DNS stall). Call .shutdown() after."""
    import functools
    import http.server
    import socketserver
    import threading

    class Server(http.server.ThreadingHTTPServer):
        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = Server(("127.0.0.1", 0), functools.partial(Quiet, directory=str(directory)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.url = f"http://127.0.0.1:{server.server_port}/"
    return server


class PlatformTestCase(unittest.TestCase):
    """A clean world per test: data dir, projects root, one project, a private bus."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pg-platform-")
        self.tmp = Path(self._tmp.name).resolve()
        self.data = self.tmp / "data"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self._env = {k: os.environ.get(k) for k in ("PLAYGROUND_DATA_DIR", "PLAYGROUND_HOME",
                                                    "PLAYGROUND_CONFIG_DIR", "PATH", "FAKE_SSH_LOG",
                                                    "FAKE_KEYRING_DIR", "FAKE_SSH_SERVE", "FAKE_SSH_ROOT")}
        os.environ["PLAYGROUND_DATA_DIR"] = str(self.data)
        os.environ["PLAYGROUND_HOME"] = str(self.tmp / "projects")
        os.environ["PLAYGROUND_CONFIG_DIR"] = str(self.tmp / "config")
        self.bus = events.EventBus()
        self.seen: list[tuple[str, dict]] = []
        self.bus.subscribe(None, lambda topic, **payload: self.seen.append((topic, payload)))
        self.project_dir = self.make_project("Kiln")

    def tearDown(self) -> None:
        try:
            from luma_playground.core import preview
            preview.stop_all()
        except Exception:  # noqa: BLE001
            pass
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    # ── helpers ──

    def make_project(self, name: str, members: list[Member] | None = None) -> Path:
        root = self.tmp / "projects" / name
        store = ProjectStore(root, bus=self.bus)
        store.ensure_layout()
        store.write_project(Project(name=name, id=f"{name.lower()}-0123456789abcdef", created="2026-10-09T00:00:00Z",
                                    members=members or [Member(id="nick", name="Nick", role="owner"),
                                                        Member(id="priya", name="Priya", role="builder"),
                                                        Member(id="rae", name="Rae", role="reviewer")]))
        return root

    def store(self, root: Path | None = None) -> ProjectStore:
        return ProjectStore(root or self.project_dir, bus=self.bus)

    def write(self, relpath: str, text: str, root: Path | None = None) -> Path:
        path = (root or self.project_dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        return path

    def install_fake(self, name: str, source: str) -> Path:
        path = self.bin / name
        path.write_text(source)
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if not os.environ.get("PATH", "").startswith(str(self.bin)):
            os.environ["PATH"] = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"
        return path

    def fake_ssh(self) -> Path:
        os.environ["FAKE_SSH_LOG"] = str(self.tmp / "ssh-calls.jsonl")
        os.environ["FAKE_SSH_ROOT"] = str(ROOT)
        return self.install_fake("ssh", FAKE_SSH.replace("/usr/bin/env python3", sys.executable, 1))

    def ssh_calls(self) -> list[list[str]]:
        import json
        path = self.tmp / "ssh-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def fake_keyring(self) -> Path:
        directory = self.tmp / "keyring"
        directory.mkdir(exist_ok=True)
        os.environ["FAKE_KEYRING_DIR"] = str(directory)
        return self.install_fake("secret-tool", FAKE_SECRET_TOOL.replace("/usr/bin/env python3", sys.executable, 1))

    def topics(self, topic: str) -> list[dict]:
        return [payload for t, payload in self.seen if t == topic]

    def all_files(self, *roots: Path) -> list[Path]:
        out = []
        for root in roots:
            for dirpath, _dirs, files in os.walk(root):
                out += [Path(dirpath) / f for f in files]
        return out
