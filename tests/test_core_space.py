# SPDX-License-Identifier: Apache-2.0
"""The Project space files: stack.toml, issues/, usage/services.jsonl and analytics.jsonl."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from luma_playground.core import events, store, watch
from luma_playground.core.model import AnalyticsSnapshot, Issue, Service, ServiceUsage, Stack, looks_like_secret


class SpaceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Kiln"
        self.bus = events.EventBus()
        self.seen = []
        self.bus.subscribe(None, lambda topic, **payload: self.seen.append((topic, payload)))
        self.store = store.ProjectStore(self.root, bus=self.bus)
        self.store.ensure_layout()

    def tearDown(self):
        self.tmp.cleanup()

    def resend(self, **changes):
        service = Service(id="resend", name="Resend", kind="email", provider="Resend",
                          purpose="Sends booking confirmations and reminders", used_by=["Booking"],
                          environments={"stage": "Test mode", "live": "mail.kiln.example"},
                          console_url="https://resend.com/emails", plan="Free", monthly_budget=0,
                          secrets=["Email service key"], usage_source="resend", added_by="ari")
        for key, value in changes.items():
            setattr(service, key, value)
        return service


class StackTests(SpaceTestCase):
    def test_round_trip_and_events(self):
        self.assertEqual(self.store.read_stack().services, [])
        self.store.add_service(self.resend())
        self.assertEqual(self.seen[-1], ("stack.changed", {"project": str(self.root), "service_id": "resend"}))
        back = self.store.read_stack().service("resend")
        self.assertEqual(back.environments, {"stage": "Test mode", "live": "mail.kiln.example"})
        self.assertTrue(back.added)
        self.assertEqual(back.to_dict() | {"added": ""}, self.resend().to_dict())

        self.store.update_service("resend", lambda s: setattr(s, "plan", "Pro ($20 a month)"))
        self.assertEqual(self.store.read_stack().service("resend").plan, "Pro ($20 a month)")
        with self.assertRaises(store.StoreError):
            self.store.add_service(self.resend())
        self.store.remove_service("resend")
        self.assertEqual(self.store.read_stack().services, [])
        with self.assertRaises(store.NotFound):
            self.store.remove_service("resend")

    def test_unknown_fields_and_tables_survive(self):
        self.store.paths.stack_toml.write_text('[meta]\nnote = "kept"\n\n[[services]]\nid = "x"\nname = "X"\nregion = "eu"\n')
        stack = self.store.read_stack()
        self.store.write_stack(stack)
        import tomllib
        document = tomllib.loads(self.store.paths.stack_toml.read_text())
        self.assertEqual(document["meta"], {"note": "kept"})
        self.assertEqual(document["services"][0]["region"], "eu")

    def test_refuses_secrets_and_bad_values(self):
        for bad in ({"secrets": ["re_9f8a7b6c5d4e3f2a1b0c"]}, {"environments": {"live": "sk-live-abcdefghijklmnopqrstu"}},
                    {"kind": "rocket"}, {"console_url": "javascript:alert(1)"}, {"id": "../x"}, {"name": " "}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.add_service(self.resend(**bad))
        self.assertTrue(looks_like_secret("-----BEGIN OPENSSH PRIVATE KEY-----"))
        self.assertFalse(looks_like_secret("Production database password"))

    def test_dependencies(self):
        (self.root / "package.json").write_text(json.dumps({"dependencies": {"pg": "^8.13.1", "resend": "^4.0.1"},
                                                            "devDependencies": {"vitest": "^2.1.0"}}))
        (self.root / "api").mkdir()
        (self.root / "api" / "requirements.txt").write_text(
            "# the API\nfastapi>=0.115  # web\nuvicorn[standard]==0.32.0\n-r base.txt\nhttpx ; python_version>'3.8'\n"
            "mypkg @ https://example.com/mypkg.whl\n")
        (self.root / "node_modules" / "x").mkdir(parents=True)
        (self.root / "node_modules" / "x" / "package.json").write_text('{"dependencies": {"nope": "1"}}')
        found = self.store.refresh_dependencies({"pg": "Talks to the database"})
        self.assertEqual([(d.name, d.version, d.source, d.dev) for d in found], [
            ("pg", "^8.13.1", "package.json", False), ("resend", "^4.0.1", "package.json", False),
            ("vitest", "^2.1.0", "package.json", True), ("fastapi", ">=0.115", "api/requirements.txt", False),
            ("uvicorn", "==0.32.0", "api/requirements.txt", False), ("httpx", "", "api/requirements.txt", False),
            ("mypkg", "", "api/requirements.txt", False)])
        self.assertEqual(self.store.read_stack().dependencies[0].why, "Talks to the database")
        # A refresh keeps the why for packages still there, and drops removed ones.
        (self.root / "package.json").write_text(json.dumps({"dependencies": {"pg": "^8.14.0"}}))
        (self.root / "api" / "requirements.txt").unlink()
        found = self.store.refresh_dependencies()
        self.assertEqual([(d.name, d.version, d.why) for d in found], [("pg", "^8.14.0", "Talks to the database")])

    def test_broken_package_json_is_skipped(self):
        (self.root / "package.json").write_text("{ not json")
        with self.assertLogs("luma_playground.core.store", "WARNING"):
            self.assertEqual(self.store.refresh_dependencies(), [])


class IssueTests(SpaceTestCase):
    def test_numbers_round_trip_and_events(self):
        first = self.store.create_issue("Glaze night showed 9 of 8 wheels", severity="high", place="live",
                                        reporter="nick", what_happened="Nine booked.", expected="Eight at most.",
                                        steps="1. Book twice at once", evidence="Two bookings 40 ms apart.")
        second = self.store.create_issue("Maya's view is hard to read outside", severity="low")
        self.assertEqual((first.number, second.number), (1, 2))
        self.assertEqual(self.seen[-1], ("issue.changed", {"project": str(self.root), "number": 2}))
        back = self.store.read_issue(1)
        self.assertEqual(back.to_dict(), first.to_dict())
        self.assertTrue(self.store.paths.issue(1).read_text().startswith("---\nnumber: 1\n"))

        self.store.update_issue(1, lambda i: setattr(i, "status", "fixed"))
        self.assertEqual([i.number for i in self.store.list_issues()], [2, 1])
        self.assertEqual([i.number for i in self.store.list_issues(include_closed=False)], [2])
        self.assertEqual([i.number for i in self.store.list_issues(status="fixed")], [1])
        # Numbers continue after the highest, even with gaps.
        self.store.paths.issue(1).rename(self.store.paths.issue(12))
        self.assertEqual(self.store.create_issue("Next").number, 13)

    def test_validation_and_hand_written(self):
        with self.assertRaises(ValueError):
            self.store.create_issue("X", severity="catastrophic")
        with self.assertRaises(ValueError):
            self.store.create_issue("X", status="maybe")
        with self.assertRaises(ValueError):
            self.store.create_issue("  ")
        with self.assertRaises(store.NotFound):
            self.store.read_issue(99)
        with self.assertRaises(ValueError):
            self.store.paths.issue(0)
        self.store.paths.issue(5).write_text("---\ntitle: By hand\nstatus: closed\n---\nIt broke.\n\n## Notes\nkept\n")
        issue = self.store.read_issue(5)
        self.assertEqual((issue.number, issue.status, issue.what_happened), (5, "fixed", "It broke."))
        self.assertEqual(issue.sections, [("Notes", "kept")])


class UsageAnalyticsTests(SpaceTestCase):
    def test_usage(self):
        self.store.append_service_usage(ServiceUsage("resend", "2026-10-08", {"emails_sent": 14, "bounced": 0}, 0.0))
        self.store.append_service_usage(ServiceUsage("r2", "2026-10-08", {"storage_gb": 0.27}, 0.0, source="cloudflare"))
        self.assertEqual(self.seen[-1][0], "usage.changed")
        self.assertEqual(self.seen[-1][1]["service_id"], "r2")
        self.assertEqual([u.metrics for u in self.store.read_service_usage("resend")], [{"emails_sent": 14, "bounced": 0}])
        self.assertEqual(len(self.store.read_service_usage()), 2)
        self.assertEqual(self.store.read_service_usage(limit=1)[0].service_id, "r2")
        with self.assertRaises(ValueError):
            self.store.append_service_usage(ServiceUsage("resend", "2026-10-08", {"emails_sent": "lots"}))
        with self.assertRaises(ValueError):
            self.store.append_service_usage(ServiceUsage("resend", "", {"emails_sent": 1}))

    def test_analytics(self):
        self.store.append_analytics(AnalyticsSnapshot("2026-W41", visitors=120, sessions=150,
                                                      top_pages=[{"path": "/", "views": 300}], errors=1))
        self.assertEqual(self.seen[-1][0], "analytics.changed")
        snapshot = self.store.read_analytics()[0]
        self.assertEqual((snapshot.period, snapshot.visitors, snapshot.top_pages), ("2026-W41", 120, [{"path": "/", "views": 300}]))
        self.assertEqual(len(self.store.read_analytics(limit=5)), 1)


class WatchSpaceTests(SpaceTestCase):
    def test_agent_writes_become_events(self):
        watcher = watch.ProjectWatcher(self.root, bus=self.bus, watch_files=False)
        watcher.prime()

        def touch(relative, text):
            path = self.root / ".playground" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            stamp = time.time() + 2
            os.utime(path, (stamp, stamp))

        touch("stack.toml", '[[services]]\nid = "x"\nname = "X"\n')
        touch("issues/3.md", "---\ntitle: Broken\n---\n")
        with open(self.store.paths.service_usage, "a") as handle:
            handle.write(json.dumps({"service_id": "x", "period": "2026-10", "metrics": {"n": 1}}) + "\n")
        with open(self.store.paths.analytics, "a") as handle:
            handle.write(json.dumps({"period": "2026-10", "visitors": 3}) + "\n")
        self.seen.clear()
        watcher.poll()
        topics = {topic: payload for topic, payload in self.seen}
        self.assertIn("stack.changed", topics)
        self.assertEqual(topics["issue.changed"]["number"], 3)
        self.assertEqual(topics["usage.changed"]["service_id"], "x")
        self.assertEqual(topics["analytics.changed"]["entry"]["visitors"], 3)

        # The store's own writes aren't repeated.
        self.store.create_issue("Ours")
        self.store.append_analytics(AnalyticsSnapshot("2026-10-09", visitors=1))
        self.store.write_stack(Stack())
        self.seen.clear()
        watcher.poll()
        self.assertEqual(self.seen, [])


if __name__ == "__main__":
    unittest.main()
