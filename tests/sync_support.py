# SPDX-License-Identifier: Apache-2.0
"""Two simulated teammates for the sync tests: separate data folders and project folders.

No tests live here. ``as_member(path)`` switches PLAYGROUND_DATA_DIR for calls
that read it (sharing, inviting, joining); SyncEngine takes its data folder
explicitly, so two engines can run side by side in one process.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from luma_playground.core import events, sync  # noqa: E402
from luma_playground.core.model import Member  # noqa: E402
from luma_playground.core.project import create_project  # noqa: E402
from luma_playground.core.store import ProjectStore  # noqa: E402

HAVE_TOOLS = bool(shutil.which("git") and shutil.which("ssh-keygen"))
TOOLS_SKIP = "needs git and ssh-keygen"


class SyncWorld(unittest.TestCase):
    """Noor (owner) and Priya (builder), each with their own computer, in one temp folder."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pg-sync-")
        self.tmp = Path(self._tmp.name).resolve()
        self._saved = {k: os.environ.get(k) for k in ("PLAYGROUND_DATA_DIR", "PLAYGROUND_HOME",
                                                      "PLAYGROUND_CONFIG_DIR", "PATH")}
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        # No keyring in tests: keys fall back to file permissions at once.
        fake = bin_dir / "secret-tool"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["PLAYGROUND_CONFIG_DIR"] = str(self.tmp / "config")
        self.data = {"noor": self.tmp / "noor-data", "priya": self.tmp / "priya-data", "rae": self.tmp / "rae-data"}
        self.roots = {name: self.tmp / f"{name}-projects" for name in self.data}
        self.engines: list[sync.SyncEngine] = []
        self.as_member("noor")

    def tearDown(self) -> None:
        for engine in self.engines:
            try:
                engine.stop(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def as_member(self, name: str) -> None:
        os.environ["PLAYGROUND_DATA_DIR"] = str(self.data[name])
        os.environ["PLAYGROUND_HOME"] = str(self.roots[name])

    @contextlib.contextmanager
    def member(self, name: str):
        before = os.environ.get("PLAYGROUND_DATA_DIR"), os.environ.get("PLAYGROUND_HOME")
        self.as_member(name)
        try:
            yield
        finally:
            os.environ["PLAYGROUND_DATA_DIR"], os.environ["PLAYGROUND_HOME"] = before

    def make_project(self) -> Path:
        with self.member("noor"):
            project = create_project("Kiln", brief="Pottery classes, booked online.", root=self.roots["noor"],
                                     owner=Member(id="noor", name="Noor", role="owner"))
        store = ProjectStore(project.path)
        current = store.read_project()
        current.members.append(Member(id="priya", name="Priya", role="builder"))
        store.write_project(current)
        return Path(project.path)

    def engine(self, project_dir: Path, name: str, interval: float = 0.2) -> sync.SyncEngine:
        bus = events.EventBus()
        engine = sync.SyncEngine(project_dir, bus=bus, data_dir=self.data[name], interval=interval)
        engine.seen = []  # type: ignore[attr-defined]
        bus.subscribe(None, lambda topic, **payload: engine.seen.append((topic, payload)))  # type: ignore
        self.engines.append(engine)
        return engine

    def settle(self, *engines: sync.SyncEngine, rounds: int = 3) -> None:
        for _ in range(rounds):
            for engine in engines:
                status = engine.sync_now()
                self.assertIn(status["state"], ("up-to-date", "conflict"), status)
