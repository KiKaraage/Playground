# SPDX-License-Identifier: Apache-2.0
"""The handoff brief is built from the project folder, in plain words."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from luma_playground.agents.connections import Connection
from luma_playground.agents.handoff import MAX_BRIEF_CHARS, build_brief, git_summary
from luma_playground.core.events import EventBus
from luma_playground.core.model import Check, Decision, Member, PendingDecision, Option, Story, ThreadEntry
from luma_playground.core.project import create_project
from luma_playground.core.store import ProjectStore


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_CONFIG_DIR": str(base / "config"),
                                               "PLAYGROUND_DATA_DIR": str(base / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        project = create_project("Kiln", "A pottery class sign-up", root=base, git=False, bus=EventBus(),
                                 owner=Member(id="noor", name="Noor", role="owner"))
        self.root = project.path
        self.store = ProjectStore(self.root, bus=EventBus())
        self.goal = self.store.create_goal(
            "Add a waitlist", outcome="Parents can join a waitlist when a class is full.", owner="noor",
            stories=[Story("A parent can join the waitlist", checks=[Check("When the class is full, the button says Join waitlist", "pass")]),
                     Story("The teacher sees who is waiting")])
        self.claude = Connection(id="claude", label="Claude", kind="personal-plan", provider="claude")
        self.codex = Connection(id="codex", label="Codex, work", kind="personal-plan", provider="codex")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_brief_has_the_goal_plan_decisions_and_recent_work(self):
        g = self.goal.id
        self.store.append_decision(Decision(title="Waitlist size", chosen="Eight people", why="One class", goal=g))
        self.store.append_decision(Decision(title="Another goal's choice", chosen="Blue", goal="other-goal-1234"))
        self.store.write_pending(PendingDecision(id="email-ab12", question="Email parents when a spot opens?",
                                                 options=[Option("yes", "Yes"), Option("no", "No")], goal=g))
        self.store.append_thread(g, ThreadEntry(kind="message", role="person", text="Add a waitlist please"))
        self.store.append_thread(g, ThreadEntry(kind="step", role="agent", id="step-1",
                                                data={"title": "Working", "state": "running", "files": []}))
        self.store.append_thread(g, ThreadEntry(kind="step", role="agent", id="step-1",
                                                data={"title": "Changed 2 files", "state": "done",
                                                      "files": ["src/waitlist.js", "src/app.js"]}))
        self.store.append_thread(g, ThreadEntry(kind="message", role="agent", text="The button is in. " * 40))
        handoff = build_brief(self.store, g, from_connection=self.claude, to_connection=self.codex, git=False)
        brief = handoff.brief
        self.assertIn("It was being worked on with Claude; you are Codex, work", brief)
        self.assertIn(f"Goal: Add a waitlist (id {g}", brief)
        self.assertIn("- [ ] A parent can join the waitlist", brief)
        self.assertIn("check (pass): When the class is full", brief)
        self.assertIn("Waitlist size: Eight people (One class)", brief)
        self.assertNotIn("Another goal's choice", brief)
        self.assertIn("Email parents when a spot opens?", brief)
        self.assertIn("- Person: Add a waitlist please", brief)
        self.assertIn("- Step: Changed 2 files (done)", brief)
        self.assertNotIn("Working (running)", brief)   # the finished step replaced the running one
        self.assertIn("src/waitlist.js, src/app.js", brief)
        self.assertIn("...", brief)                     # long messages are clipped
        self.assertNotIn("—", brief)               # no em dashes
        self.assertEqual(handoff.card_data()["from_connection"], "claude")
        self.assertEqual(handoff.card_data()["to_label"], "Codex, work")

    def test_brief_is_bounded(self):
        for i in range(200):
            self.store.append_decision(Decision(title=f"Decision {i} " + "x" * 150, chosen="y" * 150, goal=self.goal.id))
            self.store.append_thread(self.goal.id, ThreadEntry(kind="message", role="agent", text="z" * 400))
        brief = build_brief(self.store, self.goal.id, from_connection=self.claude, to_connection=self.codex,
                            git=False).brief
        self.assertLessEqual(len(brief), MAX_BRIEF_CHARS)

    def test_missing_goal_still_gives_a_usable_brief(self):
        brief = build_brief(self.store, "no-such-goal-0000", from_connection=None, to_connection=self.codex,
                            git=False).brief
        self.assertIn("You're picking up a goal", brief)

    @unittest.skipUnless(shutil.which("git"), "git isn't installed")
    def test_git_changes_are_summarised(self):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        run = lambda *a: subprocess.run(["git", "-C", str(self.root), *a], env=env, capture_output=True, check=True)  # noqa: E731
        run("init", "-q")
        Path(self.root, "app.js").write_text("one\n")
        run("add", "-A")
        run("commit", "-qm", "start")
        Path(self.root, "app.js").write_text("one\ntwo\n")
        Path(self.root, "new.js").write_text("x\n")
        summary = git_summary(self.root)
        self.assertIn("M app.js", summary)
        self.assertIn("?? new.js", summary)
        self.assertIn("1 file changed", summary)
        brief = build_brief(self.store, self.goal.id, from_connection=self.claude, to_connection=self.codex).brief
        self.assertIn("Changed files (git status):", brief)
        self.assertNotIn(".playground/", summary)
        self.assertEqual(git_summary(Path(self.tmp.name) / "data"), "")


if __name__ == "__main__":
    unittest.main()
