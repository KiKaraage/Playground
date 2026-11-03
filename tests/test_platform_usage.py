# SPDX-License-Identifier: Apache-2.0
"""Usage: runs, caps, headroom, plain-words summaries and the team view."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

from test_platform_support import PlatformTestCase

from luma_playground.core import events, usage

UTC = timezone.utc
# Thursday 9 October 2026, noon UTC.
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


class UsageTests(PlatformTestCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.bus.subscribe(events.USAGE_CHANGED, lambda **kw: self.calls.append(kw))

    def test_runs_are_stored_per_device_with_unknowns_left_unknown(self):
        record = usage.record_run("noor-claude", runs_on="Noor's Claude", tokens_in=1200, tokens_out=800,
                                  duration_s=42.5, goal_id="add-a-waitlist-7f3a", when=NOW, bus=self.bus)
        self.assertNotIn("cost", record)
        usage.record_run("team-key", cost=0.42, tokens_in=10, when=NOW, bus=self.bus)
        self.assertEqual(len(usage.runs()), 2)
        self.assertEqual(usage.runs("noor-claude")[0]["runs_on"], "Noor's Claude")
        path = self.data / "usage" / "runs.jsonl"
        self.assertTrue(path.exists())
        self.assertFalse(str(path).startswith(str(self.project_dir)))
        self.assertEqual(len(self.calls), 2)
        with self.assertRaises(ValueError):
            usage.record_run("../evil")

    def test_summary_from_headroom(self):
        usage.record_headroom("noor-claude", 62, resets_at=datetime(2026, 10, 13, 9, 0, tzinfo=UTC), when=NOW)
        self.assertEqual(usage.summary("noor-claude", NOW, tz=UTC), "62% of the week left · resets Tue")
        usage.record_headroom("noor-codex", 40, window="5h", resets_at=NOW + timedelta(hours=3), when=NOW)
        self.assertEqual(usage.summary("noor-codex", NOW, tz=UTC), "40% of this 5-hour window left · resets at 15:00")
        usage.record_headroom("far", 90, resets_at=datetime(2026, 10, 30, tzinfo=UTC), when=NOW)
        self.assertEqual(usage.summary("far", NOW, tz=UTC), "90% of the week left · resets 30 Oct")
        later = datetime(2026, 10, 14, tzinfo=UTC)
        self.assertEqual(usage.summary("noor-claude", later, tz=UTC), "A new allowance has started; no reading yet.")
        with self.assertRaises(ValueError):
            usage.record_headroom("x", 140)

    def test_summary_from_runs_when_no_headroom(self):
        self.assertEqual(usage.summary("idle", NOW, tz=UTC), "Not used this week.")
        usage.record_run("api", tokens_in=1_000_000, tokens_out=500_000, cost=3.4, when=NOW)
        self.assertEqual(usage.summary("api", NOW, tz=UTC), "1.5M tokens this week · $3.40")
        usage.record_run("local", duration_s=3, when=NOW)
        self.assertEqual(usage.summary("local", NOW, tz=UTC), "1 run this week")

    def test_parse_cli_headroom(self):
        parsed = usage.parse_cli_headroom("Weekly limit: 62% left · resets Tue 09:00", now=NOW, tz=UTC)
        self.assertEqual(parsed, {"percent_left": 62.0, "window": "week", "resets_at": "2026-10-13T09:00:00Z"})
        parsed = usage.parse_cli_headroom("5h limit: [#####     ] 38% used (resets 14:30)", now=NOW, tz=UTC)
        self.assertEqual(parsed, {"percent_left": 62.0, "window": "5h", "resets_at": "2026-10-09T14:30:00Z"})
        parsed = usage.parse_cli_headroom("80% remaining, resets in 2d 3h", now=NOW, tz=UTC)
        self.assertEqual(parsed["resets_at"], "2026-10-11T15:00:00Z")
        parsed = usage.parse_cli_headroom("12% left, resets at 2026-10-12T00:00:00Z", now=NOW)
        self.assertEqual(parsed["resets_at"], "2026-10-12T00:00:00Z")
        parsed = usage.parse_cli_headroom("50% left, resets Thu 9am", now=NOW, tz=UTC)
        self.assertEqual(parsed["resets_at"], "2026-10-15T09:00:00Z")   # this Thursday 9am already passed
        self.assertIsNone(usage.parse_cli_headroom("all good"))
        self.assertIsNone(usage.parse_cli_headroom("250% left"))

    def test_week_follows_the_providers_reset(self):
        usage.record_headroom("c", 50, resets_at=datetime(2026, 10, 13, 9, 0, tzinfo=UTC), when=NOW)
        start, end = usage.week_window("c", NOW)
        self.assertEqual((start, end), (datetime(2026, 10, 6, 9, 0, tzinfo=UTC), datetime(2026, 10, 13, 9, 0, tzinfo=UTC)))
        usage.set_cap("d", week_starts=0)
        start, end = usage.week_window("d", NOW, tz=UTC)
        self.assertEqual(start, datetime(2026, 10, 5, tzinfo=UTC))
        usage.record_run("d", tokens_in=5, when=start - timedelta(seconds=1))
        usage.record_run("d", tokens_in=7, when=start)
        self.assertEqual(usage.week_totals("d", NOW, tz=UTC)["tokens"], 7)

    def test_spending_caps(self):
        usage.set_cap("team-key", weekly_cost=5.0, bus=self.bus)
        usage.record_run("team-key", cost=3.0, when=NOW)
        self.assertEqual(usage.within_caps("team-key", NOW, tz=UTC), (True, "Within this week's caps."))
        usage.record_run("team-key", cost=2.5, when=NOW)
        ok, reason = usage.within_caps("team-key", NOW, tz=UTC)
        self.assertFalse(ok)
        self.assertIn("$5.00", reason)
        usage.set_cap("team-key", clear=["weekly_cost"])
        self.assertTrue(usage.within_caps("team-key", NOW, tz=UTC)[0])

    def test_team_tasks_capped_at_a_share_of_the_week(self):
        conn = "priya-claude"
        self.assertEqual(usage.can_run_team_task(conn, NOW, tz=UTC)[0], False)   # off until she opts in
        usage.set_cap(conn, team_task_percent=20)
        self.assertTrue(usage.can_run_team_task(conn, NOW, tz=UTC)[0])           # nothing used yet
        usage.record_run(conn, tokens_in=8000, team_task=True, when=NOW)
        ok, reason = usage.can_run_team_task(conn, NOW, tz=UTC)
        self.assertFalse(ok)
        self.assertIn("needs a reading", reason)
        # With a headroom reading: 50% used this week, a quarter of tokens were team tasks → 12.5%.
        usage.record_run(conn, tokens_in=24000, when=NOW)
        usage.record_headroom(conn, 50, resets_at=datetime(2026, 10, 13, 9, tzinfo=UTC), when=NOW)
        self.assertAlmostEqual(usage.team_share_used(conn, NOW), 12.5)
        ok, reason = usage.can_run_team_task(conn, NOW, tz=UTC)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "Team tasks have used 12% of the 20% you set aside this week.")
        usage.record_run(conn, tokens_in=16000, team_task=True, when=NOW)
        usage.record_headroom(conn, 30, resets_at=datetime(2026, 10, 13, 9, tzinfo=UTC), when=NOW + timedelta(minutes=1))
        ok, reason = usage.can_run_team_task(conn, NOW + timedelta(minutes=2), tz=UTC)
        self.assertFalse(ok)
        self.assertEqual(reason, "Team tasks have used their 20% of this week.")

    def test_team_share_is_exact_with_a_token_budget(self):
        usage.set_cap("api", weekly_tokens=100_000, team_task_percent=10)
        usage.record_run("api", tokens_in=9_000, team_task=True, when=NOW)
        self.assertAlmostEqual(usage.team_share_used("api", NOW, tz=UTC), 9.0)
        usage.record_run("api", tokens_in=2_000, team_task=True, when=NOW)
        self.assertFalse(usage.can_run_team_task("api", NOW, tz=UTC)[0])

    def test_overview_for_the_sheet(self):
        usage.record_headroom("c", 62, resets_at=datetime(2026, 10, 13, 9, tzinfo=UTC), when=NOW)
        view = usage.overview("c", NOW, tz=UTC)
        self.assertEqual(view["summary"], "62% of the week left · resets Tue")
        self.assertFalse(view["team_tasks"])
        self.assertIn("off", view["team_reason"])

    def test_team_capacity_is_opt_in_and_numbers_only(self):
        usage.record_headroom("priya-claude-acct-7781", 62, resets_at=datetime(2026, 10, 13, 9, tzinfo=UTC), when=NOW)
        path = usage.share_capacity(self.project_dir, "priya", [
            {"connection_id": "priya-claude-acct-7781", "label": "Claude", "provider": "claude", "team_tasks": True,
             "token": "sk-should-never-be-written", "email": "priya@example.com"},
            {"connection_id": "priya-local", "label": "Qwen on this laptop", "provider": "ollama"},
        ], now=NOW, bus=self.bus)
        text = path.read_text()
        for leak in ("sk-should-never-be-written", "priya@example.com", "priya-claude-acct-7781"):
            self.assertNotIn(leak, text)
        self.assertEqual(set(json.loads(text)["connections"][0]), {"label", "provider", "team_tasks", "percent_left",
                                                                 "window", "resets_at"})
        team = usage.team_capacity(self.project_dir, NOW, tz=UTC)
        self.assertEqual(team[0]["member"], "priya")
        self.assertEqual(team[0]["connections"][0]["summary"], "62% of the week left · resets Tue")
        self.assertEqual(team[0]["connections"][1]["summary"], "No reading yet.")
        usage.share_capacity(self.project_dir, "priya", [], opt_in=False)
        self.assertEqual(usage.team_capacity(self.project_dir, NOW), [])

    def test_damaged_capacity_files_are_ignored(self):
        folder = self.project_dir / ".playground" / "capacity"
        folder.mkdir(parents=True)
        (folder / "sam.json").write_text("{not json")
        (folder / "rae.json").write_text(json.dumps({"connections": [{"label": "X", "percent_left": "lots"}, 5]}))
        team = usage.team_capacity(self.project_dir, NOW)
        self.assertEqual([m["member"] for m in team], ["rae"])
        self.assertEqual(team[0]["connections"][0]["summary"], "No reading yet.")


if __name__ == "__main__":
    unittest.main()
