# SPDX-License-Identifier: Apache-2.0
"""Running checks and recording Proof."""
from __future__ import annotations

import json
import os
from pathlib import Path
import unittest

from test_platform_support import PlatformTestCase, quick_http_server

from luma_playground.core import checks, events
from luma_playground.core.model import Check, Story

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")


class ParseTests(unittest.TestCase):
    def test_plain_language_forms(self):
        cases = {
            "run: npm test": ("shell", "npm test"),
            "check: run: pytest -q": ("shell", "pytest -q"),
            "$ make lint": ("shell", "make lint"),
            "`pytest -q` passes": ("shell", "pytest -q"),
        }
        for text, (kind, command) in cases.items():
            with self.subTest(text=text):
                spec = checks.parse_check(text)
                self.assertEqual((spec.kind, spec.command), (kind, command))
        spec = checks.parse_check("GET /health returns 200")
        self.assertEqual((spec.kind, spec.url, spec.status, spec.contains), ("http", "/health", 200, ""))
        spec = checks.parse_check('/ shows "Join the waitlist"')
        self.assertEqual((spec.kind, spec.url, spec.status, spec.contains), ("http", "/", None, "Join the waitlist"))
        spec = checks.parse_check("open /pricing returns 200 and shows “Pro plan”")
        self.assertEqual((spec.url, spec.status, spec.contains), ("/pricing", 200, "Pro plan"))
        spec = checks.parse_check("https://kiln.example/ returns 200")
        self.assertEqual((spec.kind, spec.url), ("http", "https://kiln.example/"))
        spec = checks.parse_check("screenshot of /pricing")
        self.assertEqual((spec.kind, spec.url), ("screenshot", "/pricing"))
        self.assertEqual(checks.parse_check("A seat is taken exactly once").kind, "manual")
        self.assertEqual(checks.parse_check("The page loads fast / feels right").kind, "manual")

    def test_dict_and_model_forms(self):
        spec = checks.parse_check({"kind": "http", "path": "/api", "status": "201", "contains": "ok"})
        self.assertEqual((spec.kind, spec.url, spec.status, spec.contains), ("http", "/api", 201, "ok"))
        self.assertEqual(checks.parse_check(Check(text="run: true")).kind, "shell")
        self.assertEqual(checks.parse_check({"kind": "rocket"}).kind, "manual")


class RunTests(PlatformTestCase):
    def setUp(self):
        super().setUp()
        site = self.tmp / "site"
        site.mkdir()
        (site / "index.html").write_text("<h1>Join the waitlist</h1>")
        (site / "health").write_text("ok")
        self.server = quick_http_server(site)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_every_kind_with_evidence_on_disk(self):
        shots_taken = []

        def screenshot(url: str, out: Path) -> bool:
            shots_taken.append(url)
            out.write_bytes(PNG)
            return True

        data = checks.run_checks(self.project_dir, [
            "run: echo all good",
            "run: echo nope >&2; exit 4",
            '/ shows "Join the waitlist"',
            "GET /health returns 200",
            "GET /missing returns 200",
            '/ shows "Sold out"',
            "screenshot of /",
            "A seat is taken exactly once",
        ], goal_id="add-a-waitlist-7f3a", preview_url=self.server.url, screenshot=screenshot)
        statuses = [c["status"] for c in data["checks"]]
        self.assertEqual(statuses, ["pass", "fail", "pass", "pass", "fail", "fail", "pass", "pending"])
        self.assertEqual(data["summary"], "4 of 8 checks pass, 3 failed, 1 waiting for a look.")
        self.assertIn("exit code 4", data["checks"][1]["note"])
        self.assertIn("Answered 404, expected 200", data["checks"][4]["note"])
        self.assertIn("doesn't say", data["checks"][5]["note"])
        self.assertEqual(shots_taken, [self.server.url])
        self.assertEqual(len(data["shots"]), 1)
        root = self.project_dir
        for record in data["checks"]:
            if "evidence" in record:
                path = root / record["evidence"]
                self.assertTrue(path.exists(), record)
                self.assertTrue(record["evidence"].startswith(".playground/local/proof/add-a-waitlist-7f3a/"))
        self.assertIn("nope", (root / data["checks"][1]["evidence"]).read_text())
        self.assertEqual((root / data["shots"][0]).read_bytes(), PNG)
        saved = json.loads((root / data["evidence_dir"] / "proof.json").read_text())
        self.assertEqual(saved["checks"], data["checks"])

    def test_http_without_a_preview_waits(self):
        data = checks.run_checks(self.project_dir, ["GET / returns 200", "screenshot of /"])
        self.assertEqual([c["status"] for c in data["checks"]], ["pending", "pending"])

    def test_timeouts_and_failed_screenshots(self):
        data = checks.run_checks(self.project_dir, [{"kind": "shell", "command": "sleep 20", "timeout": 1}],
                                 preview_url=self.server.url)
        self.assertEqual(data["checks"][0]["status"], "fail")
        self.assertIn("longer than 1 second,", data["checks"][0]["note"])
        data = checks.run_checks(self.project_dir, ["screenshot of /"], preview_url=self.server.url,
                                 screenshot=lambda url, out: False)
        self.assertEqual(data["checks"][0]["status"], "fail")

    def test_commands_run_in_the_project_without_credentials(self):
        os.environ["OPENAI_API_KEY"] = "sk-should-not-reach-checks"
        try:
            data = checks.run_checks(self.project_dir, ["run: pwd; env"], preview_url=self.server.url)
        finally:
            os.environ.pop("OPENAI_API_KEY")
        log = (self.project_dir / data["checks"][0]["evidence"]).read_text()
        self.assertIn(str(self.project_dir.resolve()), log)
        self.assertIn(f"PREVIEW_URL={self.server.url}", log)
        self.assertNotIn("sk-should-not-reach-checks", log)

    def test_goal_checks_update_the_goal_and_add_a_proof_card(self):
        store = self.store()
        goal = store.create_goal("Add a waitlist", stories=[
            Story(title="People can join", checks=[Check(text='/ shows "Join the waitlist"'),
                                                   Check(text="run: true")]),
            Story(title="It's quick", checks=[Check(text="run: false")]),
        ])
        data = checks.run_goal_checks(self.project_dir, goal.id, preview_url=self.server.url, store=store)
        self.assertEqual(data["passed"], 2)
        updated = store.read_goal(goal.id)
        self.assertEqual([c.status for c in updated.checks], ["pass", "pass", "fail"])
        self.assertTrue(all(c.evidence for c in updated.checks))
        thread = store.read_thread(goal.id)
        self.assertEqual(thread[-1].kind, "proof")
        self.assertEqual(thread[-1].data["summary"], "2 of 3 checks pass, 1 failed.")
        self.assertTrue(self.topics(events.THREAD_APPENDED))

    def test_goal_checks_use_the_running_preview(self):
        store = self.store()
        store.write_preview_state("running", url=self.server.url)
        goal = store.create_goal("Health", stories=[Story(title="Up", checks=[Check(text="GET /health returns 200")])])
        data = checks.run_goal_checks(self.project_dir, goal.id, store=store)
        self.assertEqual(data["checks"][0]["status"], "pass")


if __name__ == "__main__":
    unittest.main()
