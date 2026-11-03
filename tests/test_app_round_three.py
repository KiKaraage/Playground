# SPDX-License-Identifier: Apache-2.0
"""The picker's headroom words and teammates, and the honest status of a goal nobody here can work on.
These need GTK (the modules draw), so they are skipped where it isn't installed (the Mac host)."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def gtk_module(name: str):
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        import importlib
        return importlib.import_module(name)
    except (ImportError, ValueError, RuntimeError) as error:
        raise unittest.SkipTest(f"GTK isn't here: {error}")


class DeviceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.base = base
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_DATA_DIR": str(base / "data"),
                                               "PLAYGROUND_CONFIG_DIR": str(base / "config"),
                                               "PLAYGROUND_HOME": str(base / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def fixture_connections(self):
        (self.base / "config").mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "tools" / "fixtures" / "connections.toml", self.base / "config" / "connections.toml")
        from luma_playground.agents.connections import ConnectionStore
        return {c.id: c for c in ConnectionStore().list()}


class PickerWordsTests(DeviceCase):
    def test_every_connection_says_what_it_has_left_like_v73(self):
        picker = gtk_module("luma_playground.app.sheets.picker")
        from luma_playground.core import samples
        mine = self.fixture_connections()
        samples.create_sample_device_usage()
        words = {cid: picker.headroom(conn, short=True).get("text") for cid, conn in mine.items()}
        self.assertEqual(words["claude"], "62% of the week left")
        self.assertEqual(words["codex"], "Plenty left today")
        self.assertEqual(words["ari"], "$8.20 of credits left this month")
        self.assertEqual(words["team-key"], "$6.40 of $25 this month")
        self.assertEqual(words["qwen"], "Free, and nothing leaves this computer")
        self.assertIn("resets", picker.headroom(mine["claude"]).get("text"))   # the Usage sheet keeps the reset
        self.assertAlmostEqual(picker.headroom(mine["ari"])["fraction"], 0.82, places=2)

    def test_without_readings_nothing_is_made_up(self):
        picker = gtk_module("luma_playground.app.sheets.picker")
        mine = self.fixture_connections()
        self.assertEqual(picker.headroom(mine["claude"]).get("text"), "")
        self.assertEqual(picker.headroom(mine["team-key"], short=True).get("text"), "$0 of $25 this month")
        self.assertEqual(picker.spend_cap(mine["ari"]), (10.0, "month"))
        self.assertIsNone(picker.spend_cap({"spend_cap": "lots"}))

    def test_teammates_from_the_capacity_files(self):
        picker = gtk_module("luma_playground.app.sheets.picker")
        from luma_playground.core import events, samples
        project = samples.create_sample_kiln(self.base / "home", git=False, bus=events.EventBus())
        mates = picker.teammates(project.path)
        self.assertEqual([(m["member_name"], m["label"], m["plan"]) for m in mates],
                         [("Priya Raman", "Claude", "Max plan"), ("Sam Kaur", "Codex", "Plus plan")])
        priya, sam = mates
        self.assertTrue(priya["work_on_team_tasks"])
        self.assertEqual((priya["team_task_cap"], priya["pronoun"]), (20, "her"))
        self.assertEqual(priya["id"], "priya-claude")    # what the goal's connection becomes when given
        self.assertFalse(sam["work_on_team_tasks"])


class WaitingStatusTests(DeviceCase):
    def window(self):
        return gtk_module("luma_playground.app.window")

    def test_a_teammates_goal_stays_working_on_their_computer(self):
        window = self.window()
        from luma_playground.core import events, samples
        project = samples.create_sample_kiln(self.base / "home", git=False, bus=events.EventBus())
        data = window.ProjectData(project.path, events.EventBus())
        data.you = lambda: "nick"
        by_id = {g["id"]: g for g in data.goals()}
        texts = by_id[samples.TEXTS]                    # Priya's goal runs on her computer
        self.assertEqual(texts["status"], "working")
        self.fixture_connections()
        data.forget()
        self.assertEqual({g["id"]: g for g in data.goals()}[samples.TEXTS]["status"], "working")

    def test_the_first_goal_of_a_new_project_says_so_honestly(self):
        window = self.window()
        rail = gtk_module("luma_playground.app.rail")
        widgets = gtk_module("luma_playground.app.widgets")
        from luma_playground.core import events
        from luma_playground.core.model import GoalStatus, Member
        from luma_playground.core.project import create_project
        from luma_playground.core.store import ProjectStore
        bus = events.EventBus()
        made = create_project("Sprig", root=self.base / "home", git=False, bus=bus,
                              owner=Member(id="nick", name="Nick", role="owner"))
        goal = ProjectStore(made.path, bus=bus).create_goal("Get a first version on screen", owner="nick")
        ProjectStore(made.path, bus=bus).update_goal(goal.id, lambda g: setattr(g, "status", GoalStatus.WORKING))
        data = window.ProjectData(made.path, bus)
        data.you = lambda: "nick"
        first = {g["id"]: g for g in data.goals()}[goal.id]
        self.assertEqual(first["status"], "waiting")
        self.assertEqual(rail.goal_subtitle(first), "Waiting for an agent")
        self.assertNotIn("Working", rail.goal_subtitle(first))
        self.assertEqual(widgets.state_kind("waiting"), "draft")   # no turning ring: nothing is running
        # Once Ari is connected here, it is working again.
        self.fixture_connections()
        data.forget()
        self.assertEqual(rail.goal_subtitle({g["id"]: g for g in data.goals()}[goal.id]), "Working · Ari")


if __name__ == "__main__":
    unittest.main()
