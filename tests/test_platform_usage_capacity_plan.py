# SPDX-License-Identifier: Apache-2.0
"""Shared capacity carries the plan's name and the team-task share, and nothing else new."""
from __future__ import annotations

import json
import unittest

from test_platform_support import PlatformTestCase

from luma_playground.core import usage


class CapacityPlanTests(PlatformTestCase):
    def test_plan_and_team_task_share_are_published(self):
        path = usage.share_capacity(self.project_dir, "priya", [
            {"connection_id": "priya-claude", "label": "Claude", "provider": "claude",
             "team_tasks": True, "plan": "Max plan", "team_task_percent": 20},
            {"connection_id": "priya-codex", "label": "Codex", "provider": "codex",
             "team_tasks": False, "plan": "Plus plan", "team_task_percent": 30},
        ])
        rows = json.loads(path.read_text())["connections"]
        self.assertEqual(rows[0]["plan"], "Max plan")
        self.assertEqual(rows[0]["team_task_percent"], 20)
        # No share is published for a plan that isn't working on team tasks.
        self.assertNotIn("team_task_percent", rows[1])
        self.assertNotIn("connection_id", json.dumps(rows))

    def test_bad_shares_are_dropped(self):
        path = usage.share_capacity(self.project_dir, "sam", [
            {"connection_id": "sam-codex", "label": "Codex", "provider": "codex",
             "team_tasks": True, "team_task_percent": 400},
            {"connection_id": "sam-claude", "label": "Claude", "provider": "claude",
             "team_tasks": True, "team_task_percent": True},
        ])
        rows = json.loads(path.read_text())["connections"]
        self.assertTrue(all("team_task_percent" not in row for row in rows))


if __name__ == "__main__":
    unittest.main()
