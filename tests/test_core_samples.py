# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree

from luma_playground.core import events, project, samples
from luma_playground.core.model import CARD_KINDS, GoalStatus
from luma_playground.core.paths import is_inside
from luma_playground.core.store import ProjectStore


class KilnSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.project = samples.create_sample_kiln(cls.tmp.name, git=False, bus=events.EventBus())
        cls.store = ProjectStore(cls.project.path, bus=events.EventBus())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_project(self):
        p = self.store.read_project()
        self.assertEqual(p.name, "Kiln")
        self.assertEqual([m.name for m in p.members], ["Alex", "Priya Raman", "Sam Kaur"])
        self.assertEqual([pl.name for pl in p.places], ["This computer", "Dev server", "Production"])
        self.assertEqual(p.home, "dev-server")
        self.assertEqual(project.open_project(p.path).name, "Kiln")

    def test_goals_and_states(self):
        goals = {g.title: g for g in self.store.list_goals()}
        self.assertEqual(list(goals)[0], "Talk to Ari")
        expected = {"Add a waitlist": GoalStatus.NEEDS_YOU, "Fix reminder texts": GoalStatus.WORKING,
                    "Studio photos on the home page": GoalStatus.READY, "Sign in with Luma Connect": GoalStatus.LIVE,
                    "Gift a class": GoalStatus.PAUSED}
        for title, status in expected.items():
            self.assertEqual(goals[title].status, status, title)
        self.assertEqual(goals["Fix reminder texts"].owner, "priya")
        self.assertEqual(goals["Studio photos on the home page"].status_note, "Sam approved")
        self.assertEqual(goals["Add a waitlist"].checks_passed, 3)

    def test_threads_use_every_card(self):
        kinds = set()
        for goal in self.store.list_goals():
            for entry in self.store.read_thread(goal.id):
                self.assertIn(entry.kind, CARD_KINDS)
                kinds.add(entry.kind)
        self.assertEqual(kinds, set(CARD_KINDS))

    def test_waitlist_decision_is_waiting(self):
        pending = self.store.list_pending("decision")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].question, "How should someone hear that a spot opened?")
        self.assertEqual(pending[0].recommended, "both")
        card = [e for e in self.store.read_thread(pending[0].goal) if e.kind == "decision"][0]
        self.assertTrue(card.data["open"])
        signin = [e for e in self.store.read_thread("sign-in-with-luma-connect-5a70") if e.kind == "decision"][0]
        self.assertEqual(signin.data["answered"]["option"], "match")

    def test_book(self):
        self.assertEqual(len(self.store.read_decisions()), 4)
        self.assertEqual(len(self.store.list_knowledge()), 8)
        activity = self.store.read_activity()
        self.assertGreaterEqual(len(activity), 15)
        stamps = [e.ts for e in activity]
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(self.store.read_brief().summary)

    def test_stack(self):
        stack = self.store.read_stack()
        self.assertEqual([s.name for s in stack.services],
                         ["Resend", "PostgreSQL", "Cloudflare R2", "AWS Lightsail", "Luma Connect", "Twilio"])
        self.assertEqual(stack.service("resend").purpose, "Sends booking confirmations and reminders")
        for service in stack.services:
            with self.subTest(service=service.id):
                self.assertTrue(service.purpose and service.plan and service.console_url.startswith("https://"))
                self.assertTrue(service.environments.get("stage") and service.environments.get("live"))
                self.assertTrue(service.secrets)
                self.assertTrue(service.added)
        self.assertEqual({d.name for d in stack.dependencies}, {"pg", "resend", "twilio", "@aws-sdk/client-s3"})
        self.assertTrue(all(d.why for d in stack.dependencies))
        # The dependencies are what package.json says.
        self.assertEqual([d.to_dict() for d in self.store.refresh_dependencies()], [d.to_dict() for d in stack.dependencies])

    def test_bugs(self):
        issues = {i.number: i for i in self.store.list_issues()}
        self.assertGreaterEqual(len(issues), 4)
        texts = issues[13]
        self.assertEqual((texts.title, texts.status, texts.goal), ("Two reminder texts failed", "fixing", "fix-reminder-texts-2c41"))
        self.assertEqual({i.status for i in issues.values()}, {"open", "fixing", "fixed"})
        self.assertTrue(all(i.what_happened and i.expected for i in issues.values()))

    def test_usage_and_analytics_cover_weeks(self):
        usage = self.store.read_service_usage()
        services = {s.id for s in self.store.read_stack().services}
        self.assertEqual({u.service_id for u in usage}, services)
        resend = self.store.read_service_usage("resend")
        self.assertGreaterEqual(len(resend), 21)
        self.assertTrue(all(u.metrics["emails_sent"] > 0 for u in resend))
        r2 = self.store.read_service_usage("r2")
        self.assertLess(r2[0].metrics["storage_gb"], r2[-1].metrics["storage_gb"])
        self.assertIn("class_b_ops", r2[-1].metrics)
        self.assertEqual(sum(u.metrics["sign_ins"] for u in self.store.read_service_usage("luma-connect")), 21)
        self.assertEqual(sum(u.metrics["texts_failed"] for u in self.store.read_service_usage("twilio")), 2)
        analytics = self.store.read_analytics()
        self.assertGreaterEqual(len(analytics), 21)
        self.assertTrue(all(a.visitors > 0 and a.top_pages for a in analytics))
        self.assertEqual([a.period for a in analytics], sorted(a.period for a in analytics))

    def test_files_the_cards_point_at_exist(self):
        for goal in self.store.list_goals():
            for entry in self.store.read_thread(goal.id):
                for rel in entry.data.get("files", []) + entry.data.get("shots", []):
                    self.assertTrue((self.project.path / rel).is_file(), rel)
                    self.assertTrue(is_inside(self.project.path, rel))
            for check in goal.checks:
                if check.evidence:
                    self.assertTrue((self.project.path / check.evidence).is_file(), check.evidence)

    def test_the_app_can_run(self):
        run = self.store.read_run_config()
        self.assertIn("{port}", run.command)
        self.assertEqual(run.port, 0)
        src = self.project.path / run.cwd
        self.assertTrue((src / "index.html").is_file())
        classes = json.loads((src / "classes.json").read_text())
        self.assertEqual([c["title"] for c in classes], ["Wheel basics", "Glaze night", "Open studio", "Throwing bowls"])
        html = (src / "index.html").read_text()
        for asset in ("styles.css", "kiln.js", "waitlist.js", "mark.svg", "studio/1.svg"):
            self.assertIn(asset, html)
            self.assertTrue((src / asset).is_file(), asset)
        for svg in [src / "mark.svg", *(src / "studio").glob("*.svg"), *(self.project.path / ".playground/proof").rglob("*.svg")]:
            ElementTree.parse(svg)  # well-formed

    def test_copy_has_no_em_dashes(self):
        for path in self.project.path.rglob("*"):
            if path.is_file() and ".git" not in path.parts and path.suffix in (".md", ".jsonl", ".json", ".html", ".toml"):
                self.assertNotIn("—", path.read_text(), path)


class EmptySampleTests(unittest.TestCase):
    def test_fresh_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            made = samples.create_sample_empty(tmp, git=False, bus=events.EventBus())
            store = ProjectStore(made.path)
            self.assertEqual([g.id for g in store.list_goals()], ["talk-to-ari"])
            self.assertEqual([e.kind for e in store.read_thread("talk-to-ari")], ["message", "brief", "recipe"])
            self.assertEqual(len(made.recipe.parts), 4)
            self.assertEqual(store.list_pending(), [])


if __name__ == "__main__":
    unittest.main()
