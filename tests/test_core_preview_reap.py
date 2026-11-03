# SPDX-License-Identifier: Apache-2.0
"""Stage previews never outlive Playground: what a killed run left behind is stopped next time."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from luma_playground.core import preview
from luma_playground.core.paths import ProjectPaths


def _dead_pid() -> int:
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    return int(done.stdout.strip())


class ReapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name).resolve() / "Kiln"
        (self.project / "src").mkdir(parents=True)
        self.paths = ProjectPaths(self.project)
        self.paths.local_dir.mkdir(parents=True, exist_ok=True)
        self.procs: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.tmp.cleanup()

    def spawn(self) -> subprocess.Popen:
        proc = subprocess.Popen(["sleep", "60"], cwd=self.project / "src", start_new_session=True)
        self.procs.append(proc)
        return proc

    def record(self, owner: int, procs: list[subprocess.Popen]) -> Path:
        path = self.paths.local_dir / "preview-pids.json"
        path.write_text(json.dumps({"owner": owner, "project": str(self.project),
                                    "groups": [{"pid": p.pid, "command": "sleep"} for p in procs]}))
        return path

    def test_a_killed_runs_previews_are_stopped(self) -> None:
        server = self.spawn()
        path = self.record(_dead_pid(), [server])
        self.assertEqual(preview.reap_stale(self.project), 1)
        server.wait(timeout=5)
        self.assertIsNotNone(server.poll())
        self.assertFalse(path.exists())

    def test_a_running_playgrounds_previews_are_left_alone(self) -> None:
        server = self.spawn()
        self.record(os.getpid(), [server])          # this process is the owner: it's still running
        self.assertEqual(preview.reap_stale(self.project), 0)
        time.sleep(0.2)
        self.assertIsNone(server.poll())

    def test_a_reused_number_is_not_ours(self) -> None:
        stranger = subprocess.Popen(["sleep", "60"], cwd=self.tmp.name, start_new_session=True)
        self.procs.append(stranger)
        path = self.paths.local_dir / "preview-pids.json"
        path.write_text(json.dumps({"owner": _dead_pid(), "groups": [{"pid": stranger.pid, "command": "node"}]}))
        self.assertEqual(preview.reap_stale(self.project), 0)
        time.sleep(0.2)
        self.assertIsNone(stranger.poll())

    def test_a_run_records_and_forgets_its_processes(self) -> None:
        from luma_playground.core import events
        from luma_playground.core.model import RunConfig
        from luma_playground.core.store import ProjectStore
        ProjectStore(self.project, bus=events.EventBus()).write_run_config(RunConfig(command="sleep 30", port=0))
        preview.start_preview(self.project, bus=events.EventBus(), wait=False)
        path = self.paths.local_dir / "preview-pids.json"
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not path.exists():
                time.sleep(0.05)
            record = json.loads(path.read_text())
            self.assertEqual(record["owner"], os.getpid())
            self.assertTrue(record["groups"])
        finally:
            preview.stop_preview(self.project)
        self.assertFalse(path.exists())

    def test_nothing_recorded(self) -> None:
        self.assertEqual(preview.reap_stale(self.project), 0)


if __name__ == "__main__":
    unittest.main()
