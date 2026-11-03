# SPDX-License-Identifier: Apache-2.0
import json
import os
from pathlib import Path
import tempfile
import threading
import tomllib
import unittest

from luma_playground.core import events, store
from luma_playground.core.model import (
    ActivityEntry, Answer, Brief, Check, Decision, Goal, GoalStatus, KnowledgeNote, Member, Option, PendingDecision,
    PendingRequest, Place, Project, Recipe, RecipePart, RunConfig, Story, ThreadEntry,
)


class Recorder:
    def __init__(self, bus):
        self.events = []
        bus.subscribe(None, lambda topic, **payload: self.events.append((topic, payload)))

    def topics(self):
        return [t for t, _ in self.events]


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Kiln"
        self.bus = events.EventBus()
        self.recorder = Recorder(self.bus)
        self.store = store.ProjectStore(self.root, bus=self.bus)
        self.store.ensure_layout()

    def tearDown(self):
        self.tmp.cleanup()


class FrontMatterTests(unittest.TestCase):
    def test_round_trip(self):
        fields = {"id": "a-1", "title": "Maya's view: who's waiting", "count": 3, "on": True, "none": None,
                  "list": ["phones", "two words", "x: y"], "num_text": "007", "word": "yes", "empty": "",
                  "date": "2026-10-09T09:40:00-07:00", "unicode": "Café “quoted”"}
        text = store.dump_front_matter(fields) + "\nbody\n"
        parsed, body = store.parse_front_matter(text)
        self.assertEqual(parsed, fields)
        self.assertEqual(body, "body\n")

    def test_hand_written(self):
        text = "---\ntitle: 'It''s here'\ntags:\n  - one\n  - \"two\"\nplain: hello # a comment\n---\nBody"
        parsed, body = store.parse_front_matter(text)
        self.assertEqual(parsed, {"title": "It's here", "tags": ["one", "two"], "plain": "hello"})
        self.assertEqual(body, "Body")

    def test_no_front_matter(self):
        self.assertEqual(store.parse_front_matter("# Just markdown"), ({}, "# Just markdown"))
        self.assertEqual(store.parse_front_matter("---\nunterminated"), ({}, "---\nunterminated"))


class StoriesTests(unittest.TestCase):
    def test_round_trip(self):
        stories = [Story("Someone can join", done=True, checks=[
            Check("The button says Join waitlist", "pass", ".playground/proof/a.png"),
            Check("They see their place", "fail"), Check("Maya sees the list")])]
        parsed, leftover = store.parse_stories(store.dump_stories(stories))
        self.assertEqual(parsed, stories)
        self.assertEqual(leftover, "")

    def test_agent_variants(self):
        text = ("1. [x] First story\n   - [x] check one\n   - [ ] check two\n   - [-] check three\n"
                "- Second story\n  - plain check\nSome prose an agent wrote.\n")
        parsed, leftover = store.parse_stories(text)
        self.assertEqual([s.title for s in parsed], ["First story", "Second story"])
        self.assertTrue(parsed[0].done)
        self.assertEqual([c.status for c in parsed[0].checks], ["pass", "pending", "fail"])
        self.assertEqual(parsed[1].checks[0].text, "plain check")
        self.assertEqual(leftover, "Some prose an agent wrote.")


class TomlTests(unittest.TestCase):
    def test_dump_is_valid(self):
        import tomllib
        document = {"top": 1, "app": {"command": 'say "hi"', "port": 0, "env": {"A": "b"}},
                    "recipe": {"parts": [{"label": "Pages", "choice": "HTML"}]},
                    "places": [{"id": "x", "name": "X", "port": 22}, {"id": "y", "name": "Y \\ Z"}]}
        self.assertEqual(tomllib.loads(store.dump_toml(document)), document)


class ProjectFileTests(StoreTestCase):
    def test_project_round_trip(self):
        project = Project(name="Kiln", id="abc", created="2026-09-26T15:02:00+00:00", home="dev-server",
                          summary="Class sign-ups", recipe=Recipe("A web app", "A VPS", [RecipePart("Pages", "HTML", "Quick")]),
                          places=[Place("dev-server", "Dev server", "development", "ssh", "203.0.113.7", "kiln", 22, "The closet")],
                          members=[Member("nick", "Nick", "owner"), Member("priya", "Priya Raman", "builder", "priya@connect")])
        self.store.write_project(project)
        back = self.store.read_project()
        self.assertEqual(back.to_dict() | {"last_activity": ""}, project.to_dict() | {"path": str(self.root)})
        self.assertIn("project.changed", self.recorder.topics())
        self.assertIn("places.changed", self.recorder.topics())

    def test_unknown_fields_survive(self):
        text = ('[project]\nname = "Kiln"\nid = "x"\nfuture = "kept"\n\n[deploy]\ntarget = "vercel"\n\n'
                '[[places]]\nid = "p"\nname = "P"\nregion = "eu"\n')
        self.store.paths.project_toml.write_text(text)
        project = self.store.read_project()
        project.summary = "changed"
        self.store.write_project(project)
        import tomllib
        document = tomllib.loads(self.store.paths.project_toml.read_text())
        self.assertEqual(document["project"]["future"], "kept")
        self.assertEqual(document["deploy"], {"target": "vercel"})
        self.assertEqual(document["places"][0]["region"], "eu")

    def test_missing_project(self):
        with self.assertRaises(store.NotFound):
            self.store.read_project()

    def test_run_config_round_trip(self):
        self.assertIsNone(self.store.read_run_config())
        config = RunConfig(command="python3 -m http.server {port}", cwd="src", port=0, url_path="/", env={"X": "1"})
        self.store.write_run_config(config, comment="How to run it")
        self.assertEqual(self.store.read_run_config(), config)

    def test_run_config_keeps_the_other_tables(self):
        self.store.paths.run_toml.parent.mkdir(parents=True, exist_ok=True)
        self.store.paths.run_toml.write_text(
            "# How Kiln runs\n[app]\ncommand = \"old\"\n\n[deploy]\nkind = \"ssh\"\npath = \"/srv/kiln\"\n"
            "exclude = [\"node_modules\"]\n\n[deploy.env]\nNODE_ENV = \"production\"\n\n[[checks]]\nname = \"a\"\n",
            encoding="utf-8")
        self.store.write_run_config(RunConfig(command="python3 -m http.server {port}", cwd="src", port=0))
        text = self.store.paths.run_toml.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# How Kiln runs\n"))
        document = tomllib.loads(text)
        self.assertEqual(document["app"]["command"], "python3 -m http.server {port}")
        self.assertEqual(document["deploy"]["kind"], "ssh")
        self.assertEqual(document["deploy"]["exclude"], ["node_modules"])
        self.assertEqual(document["deploy"]["env"], {"NODE_ENV": "production"})
        self.assertEqual(document["checks"], [{"name": "a"}])

    def test_run_config_refuses_to_clobber_a_broken_file(self):
        self.store.paths.run_toml.parent.mkdir(parents=True, exist_ok=True)
        self.store.paths.run_toml.write_text("[deploy\nkind = ", encoding="utf-8")
        with self.assertRaises(store.StoreError):
            self.store.write_run_config(RunConfig(command="x"))
        self.assertEqual(self.store.paths.run_toml.read_text(encoding="utf-8"), "[deploy\nkind = ")

    def test_preview_and_heartbeat(self):
        self.assertEqual(self.store.read_preview_state()["state"], "stopped")
        self.store.write_preview_state("running", "http://127.0.0.1:5000/")
        self.assertEqual(self.store.read_preview_state()["url"], "http://127.0.0.1:5000/")
        self.assertIsNone(self.store.read_app_heartbeat())
        self.store.write_app_heartbeat()
        self.assertEqual(self.store.read_app_heartbeat()["pid"], os.getpid())
        self.store.clear_app_heartbeat()
        self.assertIsNone(self.store.read_app_heartbeat())


class BriefTests(StoreTestCase):
    def test_round_trip(self):
        brief = Brief(title="Kiln", audience="Maya's students", platforms=["phones", "computers"])
        brief.set_section("What it is", "A booking page.")
        brief.set_section("Good looks like", "- Quick\n- Nobody double-booked")
        brief.set_section("Something new", "kept")
        self.store.write_brief(brief)
        back = self.store.read_brief()
        self.assertEqual(back.summary, "A booking page.")
        self.assertEqual(back.good_looks_like, ["Quick", "Nobody double-booked"])
        self.assertEqual(back.platforms, ["phones", "computers"])
        self.assertEqual(back.section("Something new"), "kept")
        self.assertEqual(self.recorder.events[-1], ("project.changed", {"project": str(self.root), "what": "brief"}))

    def test_missing_is_empty(self):
        self.assertEqual(self.store.read_brief().title, "")


class GoalTests(StoreTestCase):
    def test_round_trip(self):
        goal = Goal(id="add-a-waitlist-7f3a", title="Add a waitlist", status=GoalStatus.NEEDS_YOU, owner="nick",
                    connection="ari", size="medium", cost_estimate="About $0.60", status_note="one decision",
                    outcome="People can wait.", notes="Glaze night fills up.",
                    stories=[Story("Join", True, [Check("Button says Join waitlist", "pass")])],
                    sections=[("Research", "Some links")], extra={"priority": 2})
        self.store.write_goal(goal)
        back = self.store.read_goal(goal.id)
        self.assertEqual(back.to_dict(), goal.to_dict())
        self.assertEqual(self.recorder.events[-1][0], "goal.changed")

    def test_create_update_list(self):
        chat = self.store.ensure_general_chat()
        self.assertTrue(chat.is_chat)
        goal = self.store.create_goal("Fix reminder texts", stories=[Story("Texts arrive", checks=[Check("A")])])
        self.assertRegex(goal.id, r"^fix-reminder-texts-[0-9a-f]{4}$")
        self.store.update_goal(goal.id, lambda g: setattr(g, "status", GoalStatus.READY))
        self.assertEqual(self.store.read_goal(goal.id).status, GoalStatus.READY)
        self.assertEqual([g.id for g in self.store.list_goals()], ["talk-to-ari", goal.id])
        with self.assertRaises(store.NotFound):
            self.store.read_goal("nope-0000")
        with self.assertRaises(ValueError):
            self.store.create_goal("   ")

    def test_hand_written_goal(self):
        self.store.paths.goal("by-hand-0001").write_text(
            "---\ntitle: By hand\nstatus: Ready to review\n---\nIntro line\n\n## Stories\n- [ ] One\n  - [x] it works\n")
        goal = self.store.read_goal("by-hand-0001")
        self.assertEqual(goal.status, GoalStatus.READY)
        self.assertEqual(goal.outcome, "Intro line")
        self.assertEqual(goal.checks[0].status, "pass")

    def test_concurrent_updates_lose_nothing(self):
        goal = self.store.create_goal("Counter")
        other = store.ProjectStore(self.root, bus=self.bus)

        def bump(target):
            for _ in range(20):
                target.update_goal(goal.id, lambda g: setattr(g, "notes", g.notes + "x"))

        threads = [threading.Thread(target=bump, args=(s,)) for s in (self.store, other, self.store)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.store.read_goal(goal.id).notes), 60)


class LogTests(StoreTestCase):
    def test_activity(self):
        self.store.log("Created Kiln", kind="created", actor="nick")
        self.store.append_activity(ActivityEntry(summary="Asked", kind="decision", actor="ari", goal="g-0001",
                                                 data={"decision": "d"}))
        entries = self.store.read_activity()
        self.assertEqual([e.summary for e in entries], ["Created Kiln", "Asked"])
        self.assertEqual(self.store.read_activity(goal="g-0001")[0].data, {"decision": "d"})
        self.assertEqual(self.store.read_activity(limit=1)[0].summary, "Asked")
        self.assertTrue(self.store.last_activity())
        self.assertEqual(self.recorder.topics().count("activity.appended"), 2)

    def test_damaged_lines_are_skipped_and_never_fused(self):
        with open(self.store.paths.activity, "w") as handle:
            handle.write('{"summary": "ok"}\nnot json\n{"summary": "half')
        self.store.log("after")
        with self.assertLogs("luma_playground.core.store", "WARNING"):
            summaries = [e.summary for e in self.store.read_activity()]
        self.assertEqual(summaries, ["ok", "after"])

    def test_tail_reads_long_history(self):
        lines = "".join(json.dumps({"summary": f"n{i}", "pad": "x" * 200}) + "\n" for i in range(2000))
        self.store.paths.activity.write_text(lines)
        tail = self.store.read_activity(limit=3)
        self.assertEqual([e.summary for e in tail], ["n1997", "n1998", "n1999"])

    def test_thread(self):
        entry = self.store.append_thread("g-0001", ThreadEntry(kind="message", role="person", by="nick", text="Hi"))
        self.assertTrue(entry.id and entry.ts)
        self.store.append_thread("g-0001", ThreadEntry(kind="step", data={"title": "Did it", "files": ["a"]}))
        thread = self.store.read_thread("g-0001")
        self.assertEqual([e.kind for e in thread], ["message", "step"])
        self.assertEqual(thread[0].text, "Hi")
        self.assertEqual(self.recorder.events[-1][1]["goal_id"], "g-0001")
        self.assertEqual(len(self.store.read_thread("g-0001", limit=1)), 1)

    def test_thread_fold(self):
        self.store.append_thread("g-0001", ThreadEntry(kind="message", text="Go", id="m-1"))
        self.store.append_thread("g-0001", ThreadEntry(kind="step", id="s-1", data={"state": "running"}))
        self.store.append_thread("g-0001", ThreadEntry(kind="message", text="Still going"))
        self.store.append_thread("g-0001", ThreadEntry(kind="step", id="s-1", data={"state": "done"}))
        folded = self.store.read_thread("g-0001", fold=True)
        self.assertEqual([(e.kind, e.data.get("state")) for e in folded],
                         [("message", None), ("step", "done"), ("message", None)])
        self.assertEqual(len(self.store.read_thread("g-0001")), 4)


class DecisionTests(StoreTestCase):
    def test_decisions_md(self):
        self.store.append_decision(Decision(title="Classes cap at eight", chosen="Eight", why="One wheel each.",
                                            who="Maya, via Nick"))
        self.store.append_decision(Decision(title="Texts, then email", chosen="Both", who="Nick", goal="g-0001", id="d-1"))
        with open(self.store.paths.decisions, "a") as handle:
            handle.write("\n### By hand\n- who: Sam\n- mood: good\nBecause it reads better.\n")
        decisions = self.store.read_decisions()
        self.assertEqual([d.title for d in decisions], ["Classes cap at eight", "Texts, then email", "By hand"])
        self.assertEqual(decisions[1].goal, "g-0001")
        self.assertEqual(decisions[2].extra, {"mood": "good"})
        self.assertEqual(decisions[2].why, "Because it reads better.")
        self.assertTrue(self.store.paths.decisions.read_text().startswith("# Decisions"))

    def test_pending_answer_flow(self):
        self.store.create_goal("Waitlist", goal_id="waitlist-0001")
        pending = PendingDecision(id="how-to-tell-0001", question="How should they hear?", goal="waitlist-0001",
                                  options=[Option("text", "A text"), Option("email", "An email", "Slower")],
                                  recommended="text", asked_by="ari")
        self.store.write_pending(pending)
        self.store.append_thread("waitlist-0001", ThreadEntry(kind="decision", data={"id": pending.id}))
        self.assertEqual(self.store.read_pending(pending.id).to_dict(), pending.to_dict())
        self.assertEqual([p.id for p in self.store.list_pending("decision")], [pending.id])
        self.assertTrue(self.store.read_thread("waitlist-0001")[0].data["open"])

        answer = self.store.answer_decision(pending.id, "email", by="nick")
        self.assertEqual(answer.label, "An email")
        self.assertEqual(self.store.read_answer(pending.id).to_dict(), answer.to_dict())
        self.assertEqual(self.store.list_pending("decision"), [])
        self.assertEqual(self.store.read_decisions()[-1].chosen, "An email")
        self.assertEqual(self.store.read_activity()[-1].kind, "decision")
        card = self.store.read_thread("waitlist-0001")[0]
        self.assertEqual(card.data["answered"]["option"], "email")
        self.assertIn("decision.answered", self.recorder.topics())
        with self.assertRaises(store.AlreadyAnswered):
            self.store.answer_decision(pending.id, "text", by="priya")
        self.store.write_pending(PendingDecision(id="other-0001", question="q", options=[Option("a", "A")]))
        with self.assertRaises(ValueError):
            self.store.answer_decision("other-0001", "not-an-option")
        with self.assertRaises(ValueError):
            self.store.answer_decision("other-0001")
        with self.assertRaises(store.NotFound):
            self.store.answer_decision("nobody-0001", "a")

    def test_requests_and_answers(self):
        request = PendingRequest(id="screenshot-0001", kind="screenshot", params={"device": "phone"})
        self.store.write_pending(request)
        self.assertEqual(self.recorder.events[-1][0], "request.pending")
        self.assertIsInstance(self.store.read_pending("screenshot-0001"), PendingRequest)
        self.store.write_answer(Answer(id="screenshot-0001", data={"path": "shot.png"}))
        self.assertEqual(self.store.read_answer("screenshot-0001").data["path"], "shot.png")
        self.store.remove_pending("screenshot-0001")
        self.store.remove_pending("screenshot-0001")
        self.assertIsNone(self.store.read_pending("screenshot-0001"))

    def test_hand_written_answer_aliases(self):
        self.store.paths.answer("d-0001").write_text('{"option": "text", "by": "nick", "ts": "2026-10-09"}')
        answer = self.store.read_answer("d-0001")
        self.assertEqual((answer.id, answer.answered_by, answer.answered_at), ("d-0001", "nick", "2026-10-09"))


class KnowledgeTests(StoreTestCase):
    def test_round_trip_and_search(self):
        self.store.write_knowledge(KnowledgeNote(slug="", title="Phone numbers need the country code",
                                                 body="The texting service rejects numbers without +1.", added_by="priya"))
        self.store.write_knowledge(KnowledgeNote(slug="how-kiln-looks", title="How Kiln looks", body="Warm clay."))
        note = self.store.read_knowledge("phone-numbers-need-the-country-code")
        self.assertEqual(note.added_by, "priya")
        self.assertEqual(note.body, "The texting service rejects numbers without +1.")
        results = self.store.search_knowledge("texting phone")
        self.assertEqual(results[0][0].slug, "phone-numbers-need-the-country-code")
        self.assertEqual(len(self.store.search_knowledge("")), 2)
        self.assertEqual(self.store.search_knowledge("zebra"), [])
        self.assertIsNone(self.store.read_knowledge("missing"))


class AtomicWriteTests(unittest.TestCase):
    def test_no_temp_files_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a" / "b.json"
            store.atomic_write_json(path, {"x": 1})
            store.atomic_write_json(path, {"x": 2})
            self.assertEqual(json.loads(path.read_text()), {"x": 2})
            self.assertEqual(os.listdir(path.parent), ["b.json"])


if __name__ == "__main__":
    unittest.main()


class ExpiredDecisionTests(StoreTestCase):
    def test_a_question_nobody_answered_closes_and_stops_saying_needs_you(self):
        from luma_playground.core.model import GoalStatus, Option, PendingDecision
        self.store.ensure_layout()
        goal = self.store.create_goal("TickTock")
        self.store.update_goal(goal.id, lambda g: (setattr(g, "status", GoalStatus.NEEDS_YOU),
                                                   setattr(g, "status_note", "one decision")))
        self.store.write_pending(PendingDecision(
            id="which-1", question="Which one?", options=[Option("adaptive", "Adaptive"), Option("embeds", "Embeds")],
            goal=goal.id, recommended="adaptive", asked_at="2026-10-01T18:04:01-05:00",
            expires_at="2026-10-01T18:34:01-0500"))
        self.assertEqual(self.store.expire_decisions(now=0), [])            # not yet
        self.assertEqual(self.store.expire_decisions(), ["which-1"])
        self.assertEqual(self.store.list_pending("decision"), [])
        answer = self.store.read_answer("which-1")
        self.assertEqual((answer.option, answer.answered_by), ("adaptive", "playground"))
        self.assertEqual(self.store.read_goal(goal.id).status, GoalStatus.WORKING)
        self.assertEqual(self.store.list_knowledge(), [])     # nobody decided: no decision note
