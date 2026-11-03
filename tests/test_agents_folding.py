# SPDX-License-Identifier: Apache-2.0
"""The agent's own step report folds into the automatic step card for the same work,
so a person sees one card ("Added the Join waitlist button") instead of two."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_agents_runner import RunnerBase, fold  # noqa: E402

from luma_playground.agents.runner import CardTranslator  # noqa: E402
from luma_playground.core.model import ThreadEntry  # noqa: E402

REPORT = "mcp__playground__playground_step_report"


class FoldingTranslatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.cards: list[ThreadEntry] = []
        self.mcp_cards: list[ThreadEntry] = []   # what the MCP server wrote by itself
        self.now = [10.0]

        def find(title, own):
            for card in reversed(self.mcp_cards):
                if card.data.get("title") == title and card.id not in own:
                    return card.id
            return None

        self.t = CardTranslator(self.root, self.cards.append, by="claude", clock=lambda: self.now[0],
                                find_report=find)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def edit(self, call_id, name="app.js"):
        path = os.path.join(self.root, name)
        self.t.handle({"sessionUpdate": "tool_call", "toolCallId": call_id, "title": f"Edit {name}", "kind": "edit",
                       "status": "in_progress", "locations": [{"path": path}]})
        self.t.handle({"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "completed"})

    def report(self, call_id, title, files=(), wrap=False, mcp_id="step-mcp"):
        args = {"title": title, "files": list(files)}
        raw = {"server": "playground", "tool": "playground_step_report", "arguments": args} if wrap else args
        self.t.handle({"sessionUpdate": "tool_call", "toolCallId": call_id, "title": REPORT, "kind": "other",
                       "status": "pending", "rawInput": raw})
        self.mcp_cards.append(ThreadEntry(kind="step", id=mcp_id, data={"title": title, "state": "done"}))
        self.t.handle({"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "completed"})

    def shown(self):
        return fold(self.mcp_cards + self.cards)

    def test_report_while_the_step_is_open_takes_its_words(self):
        self.edit("a")
        self.edit("b", "style.css")
        self.report("r", "Added the waitlist button", files=["index.html"])
        self.t.handle({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Done."}})
        self.t.finish()
        steps = [c for c in self.cards if c.kind == "step"]
        auto_id = steps[0].id
        final = [c for c in steps if c.id == auto_id][-1]
        self.assertEqual(final.data["title"], "Added the waitlist button")
        self.assertTrue(final.data["reported"])
        self.assertEqual(final.data["auto_title"], "Changed 3 files")   # the reported file counts too
        self.assertEqual(final.data["files"], ["app.js", "style.css", "index.html"])
        self.assertEqual(len(final.data["tool_calls"]), 2)          # the report itself isn't a tool call
        folded = [c for c in steps if c.id == "step-mcp"]
        self.assertEqual(folded[-1].data["folded_into"], auto_id)
        visible = [c for c in self.shown() if c.kind == "step"]
        self.assertEqual([c.data["title"] for c in visible], ["Added the waitlist button"])

    def test_report_after_talk_rewrites_the_finished_step(self):
        self.edit("a")
        self.t.handle({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Edited it."}})
        self.report("r", "Made the header green", wrap=True)          # Codex wraps the arguments
        self.t.finish()
        steps = [c for c in self.cards if c.kind == "step" and c.id != "step-mcp"]
        self.assertEqual([c.data["state"] for c in steps], ["running", "done", "done"])
        self.assertEqual(steps[-1].data["title"], "Made the header green")
        self.assertEqual([c.data["title"] for c in self.shown() if c.kind == "step"], ["Made the header green"])

    def test_one_report_per_step(self):
        self.edit("a")
        self.report("r1", "First words", mcp_id="step-m1")
        self.report("r2", "Second words", mcp_id="step-m2")
        self.t.finish()
        titles = sorted(c.data["title"] for c in self.shown() if c.kind == "step")
        # The first joins the work; the second has no work of its own left, so it stands alone.
        self.assertEqual(titles, ["First words", "Second words"])
        self.assertEqual(len([c for c in self.shown() if c.kind == "step"]), 2)

    def test_report_with_no_tool_calls_stands_alone(self):
        self.report("r", "Thought it through")
        self.t.finish()
        self.assertEqual([c for c in self.cards if c.kind == "step"], [])
        self.assertEqual([c.data["title"] for c in self.shown()], ["Thought it through"])

    def test_the_card_found_late_is_folded_at_the_end_of_the_turn(self):
        found = []
        t = CardTranslator(self.root, self.cards.append, clock=lambda: 1.0,
                           find_report=lambda title, own: found[0] if found else None)
        t.handle({"sessionUpdate": "tool_call", "toolCallId": "a", "title": "Edit x", "kind": "edit"})
        t.handle({"sessionUpdate": "tool_call", "toolCallId": "r", "title": REPORT, "rawInput": {"title": "Late"}})
        t.handle({"sessionUpdate": "tool_call_update", "toolCallId": "r", "status": "completed"})
        self.assertFalse(any(c.data.get("folded_into") for c in self.cards))
        found.append("step-late")
        t.finish()
        self.assertEqual(self.cards[-1].id, "step-late")
        self.assertEqual(self.cards[-1].data["title"], "Late")


class FoldingRunnerTests(RunnerBase):
    def test_the_agents_report_and_the_automatic_card_are_one(self):
        runner = self.runner()
        runner.send("report")
        self.assertTrue(runner.wait_idle(30))
        cards = self.thread()
        steps = [c for c in cards if c.kind == "step"]
        self.assertEqual(len(steps), 1, [c.data for c in steps])
        self.assertEqual(steps[0].data["title"], "Added the Join waitlist button")
        self.assertEqual(steps[0].data["files"], ["button.html"])
        self.assertEqual(steps[0].data["tool_calls"][0]["title"], "Edit button.html")
        # The raw log keeps the MCP card, pointing at the step it folded into;
        # the folded view everyone reads shows it only inside that step.
        raw = self.store.read_thread(self.goal.id)
        mcp = [c for c in raw if c.id == "step-mcp1"]
        self.assertEqual(mcp[-1].data["folded_into"], steps[0].id)
        folded = self.store.read_thread(self.goal.id, fold=True)
        self.assertFalse(any(c.id == "step-mcp1" for c in folded))
        self.assertEqual([c.kind for c in cards], ["message", "step", "message"])


if __name__ == "__main__":
    unittest.main()
