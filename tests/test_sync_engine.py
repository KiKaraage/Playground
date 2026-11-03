# SPDX-License-Identifier: Apache-2.0
"""Two teammates syncing one project through a local bare repository (no network)."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest

from sync_support import HAVE_TOOLS, TOOLS_SKIP, SyncWorld

from luma_playground.core import sync
from luma_playground.core.model import ActivityEntry, Story, ThreadEntry
from luma_playground.core.store import ProjectStore, parse_goal


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@unittest.skipUnless(HAVE_TOOLS, TOOLS_SKIP)
class FolderHomeTests(SyncWorld):
    def setUp(self) -> None:
        super().setUp()
        self.nick_dir = self.make_project()
        self.home = self.tmp / "home" / "kiln.git"
        with self.member("nick"):
            info = sync.share_project(self.nick_dir, mode="folder", home=str(self.home))
        self.assertEqual(info["mode"], "folder")
        project = ProjectStore(self.nick_dir).read_project()
        self.project_id = project.id
        pin = sync.Pin(project_id=project.id, member="priya", role="builder", mode="folder", name="Kiln",
                       home=str(self.home.resolve()), host_name="the shared folder")
        pin.save(self.data["priya"])
        self.priya_dir = sync.clone_project(pin, self.roots["priya"], data_dir=self.data["priya"])
        self.nick = self.engine(self.nick_dir, "nick")
        self.priya = self.engine(self.priya_dir, "priya")

    # ── helpers ──

    def store(self, project_dir: Path) -> ProjectStore:
        return ProjectStore(project_dir)

    def thread_texts(self, project_dir: Path, goal_id: str) -> list[str]:
        return [e.text for e in self.store(project_dir).read_thread(goal_id, fold=False)]

    # ── tests ──

    def test_records_leave_the_code_and_arrive_whole(self):
        self.assertEqual(git(self.nick_dir, "ls-files", ".playground", "AGENTS.md", ".mcp.json"), "")
        self.assertIn("/.playground/", (self.nick_dir / ".gitignore").read_text())
        priya = self.store(self.priya_dir).read_project()
        self.assertEqual(priya.id, self.project_id)
        self.assertEqual([g.id for g in self.store(self.priya_dir).list_goals()],
                         [g.id for g in self.store(self.nick_dir).list_goals()])
        self.assertIn("# Kiln", (self.priya_dir / "AGENTS.md").read_text())      # portable: no machine's path
        self.assertNotIn(str(self.nick_dir), (self.priya_dir / "AGENTS.md").read_text())
        self.assertIn(str(self.priya_dir), (self.priya_dir / ".mcp.json").read_text())
        self.assertEqual(self.nick.sync_now()["state"], "up-to-date")
        self.assertEqual(self.priya.sync_now()["state"], "up-to-date")
        self.assertEqual(self.priya.status()["text"], "Up to date")
        # Plain git (a teammate's own tools) reads the same intent from .playground/.gitattributes.
        attrs = git(self.priya_dir, "check-attr", "merge", ".playground/decisions.md", ".playground/goals/x.md",
                    ".playground/activity.jsonl").splitlines()
        self.assertEqual([line.rsplit(": ", 1)[1] for line in attrs], ["union", "playground", "union"])

    def test_concurrent_goal_and_thread_edits_come_together(self):
        nick_store = self.store(self.nick_dir)
        goal = nick_store.create_goal("Add a waitlist", stories=[Story(title="A form on the home page")])
        self.settle(self.nick, self.priya)
        priya_store = self.store(self.priya_dir)
        # Both work at the same time, without syncing.
        nick_store.update_goal(goal.id, lambda g: setattr(g, "status", g.status.parse("ready", g.status)))
        nick_store.update_goal(goal.id, lambda g: g.stories.append(Story(title="Email a confirmation")))
        for i in range(5):
            nick_store.append_thread(goal.id, ThreadEntry(role="person", kind="message", text=f"nick {i}"))
        nick_store.log("Nick did a thing", actor="nick")
        time.sleep(0.01)
        priya_store.update_goal(goal.id, lambda g: setattr(g, "title", "Add a waiting list"))
        priya_store.update_goal(goal.id, lambda g: g.stories.append(Story(title="Show how many are waiting")))
        priya_store.update_goal(goal.id, lambda g: setattr(g.stories[0], "done", True))
        for i in range(5):
            priya_store.append_thread(goal.id, ThreadEntry(role="person", kind="message", text=f"priya {i}"))
        priya_store.log("Priya did a thing", actor="priya")
        self.settle(self.nick, self.priya)
        mine = self.store(self.nick_dir).read_goal(goal.id)
        theirs = self.store(self.priya_dir).read_goal(goal.id)
        for g in (mine, theirs):
            self.assertEqual(g.title, "Add a waiting list")
            self.assertEqual(g.status.value, "ready")
            self.assertEqual(sorted(s.title for s in g.stories),
                             ["A form on the home page", "Email a confirmation", "Show how many are waiting"])
            self.assertTrue(next(s for s in g.stories if s.title == "A form on the home page").done)
        expected = sorted([f"nick {i}" for i in range(5)] + [f"priya {i}" for i in range(5)])
        for project_dir in (self.nick_dir, self.priya_dir):
            texts = [t for t in self.thread_texts(project_dir, goal.id) if t and t.startswith(("nick", "priya"))]
            self.assertEqual(sorted(texts), expected)
            summaries = [a.summary for a in self.store(project_dir).read_activity(limit=200)]
            self.assertIn("Nick did a thing", summaries)
            self.assertIn("Priya did a thing", summaries)
        # Settled: another round changes nothing on either side.
        before = git(self.nick_dir, "rev-parse", sync.STATE_REF)
        self.settle(self.nick, self.priya, rounds=2)
        self.assertEqual(git(self.nick_dir, "rev-parse", sync.STATE_REF), before)
        self.assertEqual(git(self.priya_dir, "rev-parse", sync.STATE_REF), before)

    def test_racing_writers_and_syncs_never_lose_an_entry(self):
        goal = self.store(self.nick_dir).create_goal("Fix reminder texts")
        self.settle(self.nick, self.priya)
        self.nick.start()
        self.priya.start()
        count = 25

        def writer(project_dir: Path, who: str) -> None:
            store = self.store(project_dir)
            for i in range(count):
                store.append_thread(goal.id, ThreadEntry(role="person", kind="message", text=f"{who} {i}"))
                store.append_activity(ActivityEntry(actor=who, kind="note", summary=f"{who} note {i}"))
                time.sleep(0.02)

        threads = [threading.Thread(target=writer, args=(self.nick_dir, "nick")),
                   threading.Thread(target=writer, args=(self.priya_dir, "priya"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.nick.stop()
        self.priya.stop()
        self.settle(self.nick, self.priya)
        expected = Counter([f"nick {i}" for i in range(count)] + [f"priya {i}" for i in range(count)])
        for project_dir in (self.nick_dir, self.priya_dir):
            texts = Counter(t for t in self.thread_texts(project_dir, goal.id) if t and t.startswith(("nick", "priya")))
            self.assertEqual(texts, expected, project_dir)
            notes = Counter(a.summary for a in self.store(project_dir).read_activity(limit=1000)
                            if a.summary.endswith(tuple(f"note {i}" for i in range(count))))
            self.assertEqual(sum(notes.values()), 2 * count)
            self.assertTrue(all(v == 1 for v in notes.values()))

    def test_offline_catch_up(self):
        goal = self.store(self.nick_dir).create_goal("Gift cards")
        self.settle(self.nick, self.priya)
        moved = self.home.with_name("kiln-away.git")
        self.home.rename(moved)                       # the home goes away
        try:
            self.store(self.priya_dir).append_thread(goal.id, ThreadEntry(role="person", kind="message",
                                                                          text="written offline"))
            status = self.priya.sync_now()
            self.assertEqual(status["state"], "offline")
            self.assertEqual(status["text"], "Offline, will catch up")
            self.assertIn("safe here", status["detail"])
            self.store(self.nick_dir).update_goal(goal.id, lambda g: setattr(g, "status_note", "nearly"))
            self.assertEqual(self.nick.sync_now()["state"], "offline")
        finally:
            moved.rename(self.home)
        self.settle(self.priya, self.nick)
        self.assertIn("written offline", self.thread_texts(self.nick_dir, goal.id))
        self.assertEqual(self.store(self.priya_dir).read_goal(goal.id).status_note, "nearly")
        states = [p["state"] for t, p in self.priya.seen if t == sync.SYNC_STATE]
        self.assertIn("offline", states)
        self.assertEqual(states[-1], "up-to-date")

    def test_presence_travels_outside_the_history(self):
        goal = self.store(self.nick_dir).create_goal("Add a waitlist")
        self.settle(self.nick, self.priya)
        self.nick.set_goal(goal.id)
        self.settle(self.nick, self.priya)
        people = {p["member"]: p for p in sync.read_presence(self.priya_dir)}
        self.assertIn("nick", people)
        self.assertTrue(people["nick"]["here"])
        self.assertEqual(people["nick"]["goal"], goal.id)
        self.assertEqual(people["nick"]["goal_title"], "Add a waitlist")
        events = [p for t, p in self.priya.seen if t == sync.PRESENCE_CHANGED]
        self.assertTrue(events and any(person["member"] == "nick" for person in events[-1]["people"]))
        # Not part of the records' history.
        tree = git(self.nick_dir, "ls-tree", "-r", "--name-only", sync.STATE_REF)
        self.assertNotIn("presence/", tree)
        self.assertNotIn("local/", tree)
        heads = git(self.home, "for-each-ref", "--format=%(objecttype) %(refname)", "refs/playground/presence/")
        self.assertIn("blob refs/playground/presence/nick", heads)
        # Leaving says so.
        self.nick.start()
        self.nick.stop()
        self.priya.sync_now()
        people = {p["member"]: p for p in sync.read_presence(self.priya_dir)}
        self.assertFalse(people["nick"]["here"])

    def test_branch_switches_never_rewind_records_and_goal_work_releases(self):
        goal = self.store(self.nick_dir).create_goal("Add a waitlist")
        self.settle(self.nick, self.priya)
        result = self.nick.begin_goal(goal.id)
        self.assertTrue(result["switched"])
        self.assertEqual(git(self.nick_dir, "branch", "--show-current"), f"goal/{goal.id}")
        (self.nick_dir / "waitlist.html").write_text("<form>Join the waitlist</form>\n")
        self.assertTrue(self.nick.checkpoint(goal_id=goal.id))
        self.store(self.nick_dir).update_goal(goal.id, lambda g: setattr(g, "status_note", "form done"))
        git(self.nick_dir, "switch", "-q", "main")
        self.assertEqual(self.store(self.nick_dir).read_goal(goal.id).status_note, "form done")
        git(self.nick_dir, "switch", "-q", f"goal/{goal.id}")
        self.settle(self.nick, self.priya)
        self.assertTrue(git(self.priya_dir, "rev-parse", "--verify", f"refs/remotes/playground/goal/{goal.id}"))
        released = self.nick.release_goal(goal.id, "Add a waitlist")
        self.assertTrue(released["pushed"])
        self.assertEqual(git(self.home, "rev-parse", "main"), released["commit"])
        self.settle(self.priya, self.nick)
        self.assertEqual(git(self.priya_dir, "branch", "--show-current"), "main")
        self.assertTrue((self.priya_dir / "waitlist.html").exists())   # fast-forwarded: Priya's folder was clean
        self.assertFalse((self.priya_dir / ".playground" / "waitlist.html").exists())

    def test_release_conflict_is_explained_and_nothing_breaks(self):
        goal = self.store(self.nick_dir).create_goal("Add a waitlist")
        self.settle(self.nick, self.priya)
        self.nick.begin_goal(goal.id)
        (self.nick_dir / "README.md").write_text("Nick's words\n")
        self.nick.checkpoint(goal_id=goal.id)
        # Meanwhile Priya changes the same file on main and releases it first.
        other = self.store(self.priya_dir).create_goal("Write the readme")
        self.priya.begin_goal(other.id)
        (self.priya_dir / "README.md").write_text("Priya's words\n")
        self.priya.checkpoint(goal_id=other.id)
        self.priya.release_goal(other.id, "Write the readme")
        self.settle(self.nick)
        with self.assertRaises(sync.SyncConflict) as caught:
            self.nick.release_goal(goal.id, "Add a waitlist")
        self.assertIn("README.md", str(caught.exception))
        self.assertIn("Ask Ari", str(caught.exception))
        self.assertEqual(git(self.nick_dir, "branch", "--show-current"), f"goal/{goal.id}")
        self.assertEqual(git(self.nick_dir, "status", "--porcelain", "--untracked-files=no"), "")

    def test_a_removed_goal_file_and_a_concurrent_edit_keeps_the_edit(self):
        goal = self.store(self.nick_dir).create_goal("Temporary")
        self.settle(self.nick, self.priya)
        (self.nick_dir / ".playground" / "goals" / f"{goal.id}.md").unlink()
        self.store(self.priya_dir).update_goal(goal.id, lambda g: setattr(g, "status_note", "still wanted"))
        self.settle(self.nick, self.priya)
        for project_dir in (self.nick_dir, self.priya_dir):
            self.assertEqual(self.store(project_dir).read_goal(goal.id).status_note, "still wanted")

    def test_files_from_the_home_cannot_escape_the_records_folder(self):
        # A hostile tree on the home: paths a teammate's git could never check out, written by hand.
        repo = sync._Repo(self.nick_dir)
        blob = repo.write_blob(b"pwned\n")
        good = repo.ls_tree(repo.rev(sync.STATE_REF))
        evil = dict(good)
        evil["local/app.json"] = ("100644", blob)
        evil["presence/priya.json"] = ("100644", blob)
        tree = repo.make_tree(evil)
        head = repo.rev(sync.STATE_REF)
        commit = repo.commit(tree, [head], "hostile", ("x", "x@x"))
        subprocess.run(["git", "push", "-q", str(self.home), f"{commit}:{sync.STATE_REF}"], cwd=self.nick_dir,
                       check=True, capture_output=True)
        self.priya.sync_now()
        self.assertNotEqual((self.priya_dir / ".playground" / "presence" / "priya.json").exists() and
                            (self.priya_dir / ".playground" / "presence" / "priya.json").read_bytes(), b"pwned\n")
        local = self.priya_dir / ".playground" / "local" / "app.json"
        self.assertFalse(local.exists() and local.read_bytes() == b"pwned\n")

    def test_status_events_and_registry(self):
        self.assertIsNone(sync.engine_for(self.nick_dir))
        statuses = [p for t, p in self.nick.seen if t == sync.SYNC_STATE]
        self.nick.sync_now()
        statuses = [p for t, p in self.nick.seen if t == sync.SYNC_STATE]
        self.assertEqual(statuses[-1]["state"], "up-to-date")
        self.assertEqual(statuses[-1]["mode"], "folder")
        info = sync.sharing_info(self.nick_dir, data_dir=self.data["nick"])
        self.assertTrue(info["shared"])
        self.assertTrue(info["owner_device"])
        info = sync.sharing_info(self.priya_dir, data_dir=self.data["priya"])
        self.assertEqual(info["member"], "priya")
        self.assertFalse(info["owner_device"])


@unittest.skipUnless(HAVE_TOOLS, TOOLS_SKIP)
class NotSharedTests(SyncWorld):
    def test_a_project_that_isnt_shared_says_so_and_does_nothing(self):
        project_dir = self.make_project()
        engine = self.engine(project_dir, "nick")
        self.assertEqual(engine.status()["state"], "not-shared")
        self.assertEqual(engine.sync_now()["text"], "Only on this computer")
        self.assertFalse((project_dir / ".playground" / ".gitattributes").exists())


if __name__ == "__main__":
    unittest.main()
