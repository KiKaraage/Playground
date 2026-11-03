# SPDX-License-Identifier: Apache-2.0
"""Smoke tests for the harness, from the agent's side: what it reads, what it can add and remove, and what the
app shows for it. No model: the agent's moves are scripted, so these check Playground, not the agent's judgement.

1. Budget: what every turn costs before the agent does anything stays small (core.budget).
2. A working session through the tools agents see: claim a task, file a bug, suggest a rule, post to the
   channel, finish the task. Each lands where the app reads it (the areas, the indexes, the counts), and the
   next agent's brief says so.
3. Removing: a dropped task and a forgotten note leave the indexes, so no agent reads them again.
4. A conversation: the first message carries the goal context once; the next carries only what changed.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from luma_playground.core import areas, budget, events, project  # noqa: E402
from luma_playground.core.model import Member  # noqa: E402
from luma_playground.core.samples import create_sample_kiln  # noqa: E402
from luma_playground.core.store import ProjectStore  # noqa: E402
from luma_playground.mcp import server as mcp  # noqa: E402

NICK = Member(id="nick", name="Nick", role="owner")


class BudgetTests(unittest.TestCase):
    def test_what_every_turn_costs_stays_small(self):
        with tempfile.TemporaryDirectory() as tmp:
            made = create_sample_kiln(tmp, git=False)
            ProjectStore(made.path).refresh_areas()
            rows = {r["name"].split(" (")[0]: r for r in budget.starting_context(made.path)}
            always = sum(r["tokens"] for r in rows.values() if r["when"] == "always")
            start = sum(r["tokens"] for r in rows.values() if r["when"] == "start")
            self.assertLess(rows["AGENTS.md"]["tokens"], 1000, "AGENTS.md is read on every turn: keep it short")
            self.assertLess(always, 5000, "AGENTS.md and the tool list together")
            self.assertLess(start, 1500, "the indexes an agent reads when it starts work")
            # The guide is long on purpose, and read only for a format: never on every turn.
            self.assertEqual(rows["Guide"]["when"], "needed")

    def test_indexes_grow_by_a_line_an_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            made = project.create_project("Big", root=tmp, owner=NICK, git=False, bus=events.EventBus())
            store = ProjectStore(made.path)
            for n in range(120):
                store.create_task(f"Task number {n} with a reasonably long title", by="person:nick")
            store.refresh_areas()
            index = (Path(made.path) / ".playground" / "tasks" / "index.md").read_text()
            self.assertLess(budget.tokens(index) / 120, 30, "each task costs one short line in the index")


class ColdStartTests(unittest.TestCase):
    """The thumb-drive test: someone is handed only the folder, with no Playground, and points an agent at it.
    Reading AGENTS.md and the one file it sends them to, the agent can answer what a teammate of three months
    would: what it's for, what's underway and whose, what's next, what's broken, what was decided and why,
    and how to run and check it. No path of the original computer is in what it reads."""

    def test_a_stranger_finds_everything_from_the_start_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            made = create_sample_kiln(tmp, git=False)
            root = Path(made.path)
            store = ProjectStore(root)
            store.refresh_areas()
            agents = (root / "AGENTS.md").read_text()
            now_md = (root / ".playground" / "now.md").read_text()
            self.assertIn(".playground/now.md", agents.split("## Start here")[1].split("\n")[1],
                          "the first thing it says to read is now.md")
            self.assertNotIn(str(root), agents + now_md, "nothing ties it to the computer it came from")
            for answer, words in (("what it's for", "Class sign-ups"),
                                  ("what's underway, and whose", "Let students join a waitlist"),
                                  ("what's waiting on the person", "How should someone hear that a spot opened"),
                                  ("what's ready next", "Send a reminder the evening before"),
                                  ("what's broken", "#13 Two reminder texts failed"),
                                  ("what was decided", "Classes hold eight people"),
                                  ("what happened lately", "asked:"),
                                  ("how to run it", "Run (web)")):
                self.assertIn(words, now_md, f"now.md should say {answer}")
            self.assertLess(budget.tokens(now_md), 900, "now.md is a map, not the territory")
            # The why is one hop away: the decision's note, named in now.md and in the knowledge index.
            index = (root / ".playground" / "knowledge" / "index.md").read_text()
            self.assertIn("classes-hold-eight-people.md · Classes hold eight people: ", index)

    def test_a_decision_on_a_card_becomes_knowledge(self):
        from luma_playground.core.model import Option, PendingDecision
        with tempfile.TemporaryDirectory() as tmp:
            made = project.create_project("Notes", root=tmp, owner=NICK, git=False, bus=events.EventBus())
            store = ProjectStore(made.path)
            store.write_pending(PendingDecision(
                id="python-or-rust", question="Keep the app in Python, or move it to Rust?",
                context="Rust starts in 40 ms instead of 300 ms but needs a build step.",
                options=[Option(id="python", label="Stay in Python", consequence="No build step"),
                         Option(id="rust", label="Move to Rust", consequence="Faster start, a build step")]))
            store.answer_decision("python-or-rust", "rust", by="nick")
            note = next(n for n in store.list_knowledge() if n.extra.get("decision") == "python-or-rust")
            self.assertEqual(note.extra["kind"], "decision")
            for words in ("## Context", "40 ms", "## Decision", "Move to Rust", "## Alternatives", "Stay in Python"):
                self.assertIn(words, note.body)
            self.assertIn("Keep the app in Python, or move it to Rust?", (Path(made.path) / ".playground" / "now.md").read_text())


class SessionTests(unittest.TestCase):
    """One agent's session through the 14 listed tools, checked against what the app and the next agent see."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        made = project.create_project("Kiln", root=self.tmp.name, owner=NICK, git=False, bus=events.EventBus())
        self.root = made.path
        self.store = ProjectStore(self.root)
        self.server = mcp.PlaygroundServer(self.root, stdin=io.BytesIO(), stdout=io.BytesIO(),
                                           actor="codex", decision_timeout=5, keepalive=0.2, poll=0.05)
        self.ids = 0

    def tearDown(self):
        self.tmp.cleanup()

    def tool(self, name, **arguments):
        self.ids += 1
        listed = {t.name for t in mcp.listed_tools()}
        self.assertIn(f"playground_{name}", listed, "the session uses only tools agents are shown")
        result = self.server.handle({"jsonrpc": "2.0", "id": self.ids, "method": "tools/call",
                                     "params": {"name": f"playground_{name}", "arguments": arguments}})["result"]
        self.assertFalse(result["isError"], result["content"][0]["text"])
        return result.get("structuredContent") or {}

    def index(self, area: str) -> str:
        return (Path(self.root) / ".playground" / area / "index.md").read_text()

    def test_a_session_lands_where_the_app_and_the_next_agent_look(self):
        since = "2000-01-01T00:00:00Z"
        task = self.tool("task_create", title="Let students join a waitlist", priority="high",
                         checks=["A full class shows Join waitlist"])
        self.tool("task_update", id=task["id"], action="claim")
        bug = self.tool("issue_create", title="Join button does nothing on a phone", severity="high",
                        what_happened="Tapping Join waitlist on an iPhone does nothing.")
        self.tool("knowledge_add", title="Always test on a phone", body="Maya books on her phone.", kind="rule")
        self.tool("knowledge_add", title="The waitlist", body="Seats offered in order.", kind="part",
                  paths=["src/waitlist.js"], uses=["postgres"])
        self.tool("channel_post", text="Changing the classes table", kind="start", task=task["id"])

        # The app's areas read these.
        tasks = {t.id: t for t in self.store.list_tasks()}
        self.assertEqual((tasks[task["id"]].status, tasks[task["id"]].claim_holder()), ("doing", "codex"))
        self.assertEqual([i.title for i in self.store.list_issues()], ["Join button does nothing on a phone"])
        notes = {n.title: n for n in self.store.list_knowledge()}
        self.assertEqual(notes["Always test on a phone"].extra["status"], "suggested",
                         "an agent's rule waits for a person")
        self.assertEqual(notes["The waitlist"].extra["paths"], ["src/waitlist.js"])
        # Agents read the indexes, one line an item; a part says where it lives.
        self.assertIn(task["id"], self.index("tasks"))
        self.assertIn(f"{bug['number']}", self.index("issues"))
        self.assertIn("src/waitlist.js", self.index("knowledge"))
        self.assertIn("suggested, not kept yet", self.index("knowledge"))
        # A suggested rule is not in front of agents until a person keeps it.
        self.assertNotIn("Always test on a phone", (Path(self.root) / "AGENTS.md").read_text())

        # Another agent's next brief: everything above, from someone else, in a few lines.
        brief = areas.brief_since(self.store, since, conversation="other", actor="claude")
        for words in ("new task", "new bug", "Changing the classes table"):
            self.assertIn(words, brief)
        self.assertLessEqual(len(brief.splitlines()), areas.BRIEF_LINES + 2)
        # And not to the agent that did it: its own moves aren't news to it.
        self.assertNotIn("Changing the classes table", areas.brief_since(self.store, since, actor="codex"))

        # A person keeps the rule: now it's in AGENTS.md for every agent.
        note = notes["Always test on a phone"]
        note.extra["status"] = "kept"
        note.updated = ""
        self.store.write_knowledge(note)
        self.store.sync_rules()
        self.assertIn("Always test on a phone", (Path(self.root) / "AGENTS.md").read_text())

        # Done: the claim is released and the task leaves the active list.
        self.tool("task_update", id=task["id"], status="done", outcome="Join waitlist works on phones.")
        done = {t.id: t for t in self.store.list_tasks()}[task["id"]]
        self.assertEqual((done.status, done.claimed_by), ("done", ""))

    def test_a_bug_an_agent_writes_by_hand_can_still_be_changed(self):
        # Agents may write files instead of calling tools; words outside the store's own still read and save.
        path = Path(self.root) / ".playground" / "issues" / "1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\ntitle: Seats double-booked\nseverity: high\nstatus: investigating\nplace: dev\n---\n"
                        "## What happened\nTwo people got seat 3.\n")
        self.store.update_issue(1, lambda issue: setattr(issue, "status", "fixed"))
        self.assertEqual(self.store.read_issue(1).status, "fixed")

    def test_the_guide_offers_only_words_the_store_takes(self):
        from luma_playground.core.model import ISSUE_PLACES, ISSUE_SEVERITIES, ISSUE_STATES
        import re
        guide = areas.GUIDE_MD
        for field, allowed in (("severity", ISSUE_SEVERITIES), ("status", ISSUE_STATES), ("place", ISSUE_PLACES)):
            line = next(l for l in guide.splitlines() if l.strip().startswith(f"{field}:") and "#" in l
                        and ("open" in l or "high" in l or "live" in l))
            offered = set(re.findall(r"[a-z-]+", line.split("#", 1)[1])) - {"or", "where", "it", "happened",
                                                                             "the", "preview"}
            self.assertTrue(offered <= set(allowed), f"{field}: the guide offers {offered - set(allowed)}")

    def test_the_next_brief_says_when_the_preview_started(self):
        import json
        from luma_playground.core.model import now_iso
        state = Path(self.root) / ".playground" / "local" / "preview.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"state": "running", "label": "the app", "updated": now_iso()}))
        brief = areas.brief_since(self.store, "2000-01-01T00:00:00Z", conversation="x", actor="codex")
        self.assertIn("the preview is running", brief)
        self.assertIn("playground_preview_screenshot", brief)

    def test_the_now_tool_brings_what_is_known_about_the_paths(self):
        self.tool("knowledge_add", title="The waitlist", body="Seats are offered in order. Offers expire in 2 hours.",
                  kind="part", paths=["src/waitlist.js"])
        self.tool("knowledge_add", title="Never text after 9pm", body="Local time.", kind="lesson", paths=["src/"])
        self.tool("knowledge_add", title="Billing", body="Stripe.", kind="part", paths=["server/billing/"])
        out = self.server.handle({"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": {
            "name": "playground_now", "arguments": {"paths": ["src/waitlist.js"]}}})["result"]
        text = out["content"][0]["text"]
        self.assertIn("# Now", text)
        self.assertIn("Offers expire in 2 hours", text)
        self.assertIn("Never text after 9pm", text)
        self.assertNotIn("Stripe", text, "only what touches the paths")

    def test_a_replaced_note_leaves_the_index_but_stays_in_the_folder(self):
        self.tool("knowledge_add", title="Seats hold for ten minutes", body="Old rule of thumb.", kind="decision")
        old = next(n for n in self.store.list_knowledge() if n.title == "Seats hold for ten minutes")
        old.extra.update(status="superseded", superseded_by="seats-hold-for-five-minutes")
        old.updated = ""
        self.store.write_knowledge(old)
        self.assertNotIn("Seats hold for ten minutes", self.index("knowledge"))
        self.assertIn("replaced by newer ones", self.index("knowledge"))
        self.assertTrue((Path(self.root) / ".playground" / "knowledge" / f"{old.slug}.md").exists())

    def test_removing_takes_things_out_of_what_agents_read(self):
        task = self.tool("task_create", title="Something we won't do")
        self.tool("knowledge_add", title="An old idea", body="Not true any more.", kind="note")
        self.tool("task_update", id=task["id"], status="dropped")
        self.store.delete_knowledge("an-old-idea")
        self.store.refresh_areas()
        self.assertNotIn("An old idea", self.index("knowledge"))
        self.assertFalse(any(t.is_open for t in self.store.list_tasks()))
        self.assertNotIn("Something we won't do", self.index("tasks").split("## Recently finished")[0])


@unittest.skipUnless(os.access(sys.executable, os.X_OK), "needs python for the fake agent")
class ConversationTests(unittest.TestCase):
    """What the agent is sent: context once, then only the person's words and what changed."""

    def test_context_once_then_only_what_changed(self):
        from test_agents_runner import RunnerBase

        class Case(RunnerBase):
            def runTest(self):
                pass
        case = Case()
        case.setUp()
        try:
            runner = case.runner()
            runner.send("hello there")
            case.wait_for(lambda: any("prompt" in c for c in case.calls()))
            case.wait_for(lambda: not runner._busy.is_set())
            first = [c["prompt"] for c in case.calls() if "prompt" in c][-1]
            self.assertIn("Follow AGENTS.md", first)
            self.assertIn("hello there", first)
            self.assertLess(budget.tokens(first), 400, "the first message is the goal and the person's words")
            # Meanwhile, someone else files a bug.
            case.store.create_issue("Prices show without the euro sign", severity="medium")
            runner.send("and now?")
            case.wait_for(lambda: len([c for c in case.calls() if "prompt" in c]) >= 2)
            second = [c["prompt"] for c in case.calls() if "prompt" in c][-1]
            self.assertNotIn("Follow AGENTS.md", second, "the context goes once a session")
            self.assertIn("since your last turn", second)
            self.assertIn("Prices show without the euro sign", second)
            self.assertIn("and now?", second)
        finally:
            case.tearDown()


if __name__ == "__main__":
    unittest.main()
