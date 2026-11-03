# SPDX-License-Identifier: Apache-2.0
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from luma_playground.core import events, project, store, watch
from luma_playground.core.model import Member, Option, PendingDecision, ThreadEntry

NOOR = Member(id="noor", name="Noor", role="owner")


class WatcherTests(unittest.TestCase):
    """What an agent writes straight to the folder becomes events; our own writes don't repeat."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        made = project.create_project("Kiln", root=self.tmp.name, owner=NOOR, git=False, bus=events.EventBus())
        self.root = made.path
        self.bus = events.EventBus()
        self.seen: list[tuple[str, dict]] = []
        self.bus.subscribe(None, lambda topic, **payload: self.seen.append((topic, payload)))
        self.watcher = watch.ProjectWatcher(self.root, bus=self.bus)
        self.watcher.prime()

    def tearDown(self):
        self.watcher.stop()
        self.tmp.cleanup()

    def poll(self):
        self.seen.clear()
        self.watcher.poll()
        return self.seen

    def agent_write(self, relative: str, text: str) -> Path:
        """Write like an agent would: plainly, and with a new mtime even within the same tick."""
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        stamp = time.time() + 2
        os.utime(path, (stamp, stamp))
        return path

    def test_nothing_changed(self):
        self.assertEqual(self.poll(), [])

    def test_goal_written_by_an_agent(self):
        self.agent_write(".playground/goals/add-a-waitlist-7f3a.md", "---\ntitle: Add a waitlist\n---\n")
        self.assertEqual(self.poll(), [("goal.changed", {"project": str(self.root), "goal_id": "add-a-waitlist-7f3a"})])
        (self.root / ".playground/goals/add-a-waitlist-7f3a.md").unlink()
        self.assertEqual(self.poll(), [("goal.changed", {"project": str(self.root), "goal_id": "add-a-waitlist-7f3a",
                                                         "removed": True})])

    def test_appended_lines_each_become_an_event(self):
        path = self.root / ".playground/activity.jsonl"
        with open(path, "a") as handle:
            handle.write(json.dumps({"summary": "one"}) + "\n" + json.dumps({"summary": "two"}) + "\n")
            handle.write('{"summary": "half')  # not finished yet
        topics = self.poll()
        self.assertEqual([p["entry"]["summary"] for t, p in topics if t == "activity.appended"], ["one", "two"])
        with open(path, "a") as handle:
            handle.write(' done"}\n')
        self.assertEqual([p["entry"]["summary"] for t, p in self.poll()], ["half done"])

    def test_thread_lines(self):
        path = self.root / ".playground/threads/talk-to-ari.jsonl"
        with open(path, "a") as handle:
            handle.write(json.dumps({"kind": "message", "role": "agent", "text": "Hello"}) + "\n")
        self.assertEqual(self.poll(), [("thread.appended", {"project": str(self.root), "goal_id": "talk-to-ari",
                                                            "entry": {"kind": "message", "role": "agent", "text": "Hello"}})])

    def test_pending_answers_and_book(self):
        pending = PendingDecision(id="how-0001", question="How?", options=[Option("a", "A"), Option("b", "B")])
        self.agent_write(".playground/pending/how-0001.json", json.dumps(pending.to_dict()))
        self.agent_write(".playground/pending/shot-0001.json", json.dumps({"id": "shot-0001", "kind": "screenshot"}))
        self.agent_write(".playground/answers/how-0001.json", json.dumps({"option": "a", "answered_by": "noor"}))
        self.agent_write(".playground/knowledge/tip.md", "---\ntitle: Tip\n---\nUse +1.")
        self.agent_write(".playground/brief.md", "---\ntitle: Kiln\n---\n")
        self.agent_write(".playground/decisions.md", "# Decisions\n\n### A\n- who: Noor\n")
        topics = {t: p for t, p in self.poll()}
        self.assertEqual(topics["decision.pending"]["decision"]["question"], "How?")
        self.assertEqual(topics["request.pending"]["request"]["kind"], "screenshot")
        self.assertEqual(topics["decision.answered"]["answer"]["answered_by"], "noor")
        whats = sorted(p["what"] for t, p in self.seen if t == "project.changed")
        self.assertEqual(whats, ["brief", "decisions", "knowledge"])

    def test_places_changed(self):
        s = store.ProjectStore(self.root, bus=events.EventBus())
        current = s.read_project()
        text = (self.root / ".playground/project.toml").read_text()
        self.agent_write(".playground/project.toml", text.replace('name = "This computer"', 'name = "My laptop"'))
        topics = [t for t, _ in self.poll()]
        self.assertIn("project.changed", topics)
        self.assertIn("places.changed", topics)
        self.assertEqual(current.places[0].name, "This computer")

    def test_project_files(self):
        self.agent_write("src/index.html", "<h1>Kiln</h1>")
        self.agent_write("node_modules/big/index.js", "ignored")
        self.agent_write(".git/HEAD", "ignored")
        self.agent_write(".playground/local/preview.log", "ignored")
        self.assertEqual(self.poll(), [("files.changed", {"project": str(self.root), "paths": ["src/index.html"]})])

    def test_own_writes_are_not_repeated(self):
        own = store.ProjectStore(self.root, bus=events.EventBus())
        own.log("from the app")
        own.append_thread("talk-to-ari", ThreadEntry(kind="message", text="from the app"))
        own.update_goal("talk-to-ari", lambda g: setattr(g, "notes", "changed by the app"))
        own.write_pending(PendingDecision(id="own-0001", question="?", options=[Option("a", "A")]))
        own.remove_pending("own-0001")
        self.assertEqual(self.poll(), [])
        # An agent's line right after ours is still reported, and ours isn't.
        own.log("ours")
        with open(own.paths.activity, "a") as handle:
            handle.write(json.dumps({"summary": "theirs"}) + "\n")
        self.assertEqual([p["entry"]["summary"] for t, p in self.poll()], ["theirs"])

    def test_rewritten_log_asks_for_a_reload(self):
        self.agent_write(".playground/activity.jsonl", "")
        self.assertEqual(self.poll(), [("project.changed", {"project": str(self.root), "what": "activity"})])

    def test_start_and_stop(self):
        got = threading.Event()
        self.bus.subscribe("goal.changed", lambda **_: got.set())
        watcher = watch.ProjectWatcher(self.root, bus=self.bus, interval=0.1)
        watcher.start()
        watcher.start()  # twice is harmless
        self.assertTrue(watcher.running)
        self.agent_write(".playground/goals/new-0001.md", "---\ntitle: New\n---\n")
        self.assertTrue(got.wait(5), "the watcher noticed the new goal")
        watcher.stop()
        self.assertFalse(watcher.running)
        watcher.stop()

    def test_project_folder_gone(self):
        import shutil
        shutil.rmtree(self.root / ".playground")
        self.assertEqual(self.poll(), [])


if __name__ == "__main__":
    unittest.main()
