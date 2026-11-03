# SPDX-License-Identifier: Apache-2.0
"""Luma's developer guides reach a project that uses Luma's kit, a line reaches one that names Luma, and
every other project hears nothing about them."""
from pathlib import Path
import tempfile
import unittest

from luma_playground.core import luma, project
from luma_playground.core.model import Member
from luma_playground.core.store import ProjectStore

NICK = Member(id="nick", name="Nick", role="owner")


class LumaGuideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        luma._SEEN.clear()

    def made(self, name, summary):
        return project.create_project(name, summary, root=self.tmp.name, owner=NICK, git=False).path

    def now(self, root):
        luma._SEEN.clear()
        ProjectStore(root).refresh_area("now")
        return ProjectStore(root).paths.now.read_text()

    def test_a_project_on_lumas_kit_gets_the_live_guides(self):
        root = self.made("Calculator", "Adds things up")
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("from luma_appkit import AppWindow\n")
        text = self.now(root)
        self.assertIn("## Building for Luma", text)
        for url, _what in luma.GUIDES:
            self.assertIn(url, text)

    def test_naming_luma_is_one_line_and_never_a_rule(self):
        root = self.made("Notes", "A sticky notes app for Luma")
        text = self.now(root)
        self.assertNotIn("## Building for Luma", text)
        self.assertIn("unless the person chose otherwise", text)

    def test_a_recipe_that_plans_lumaui_brings_the_guides_before_any_code(self):
        root = self.made("Tiles", "A 2048 game")
        (root / ".playground" / "brief.md").write_text("# Tiles\n\nRecipe: LumaUI on GTK 4, a Flatpak.\n")
        self.assertIn("## Building for Luma", self.now(root))

    def test_any_other_project_hears_nothing_about_luma(self):
        root = self.made("Kiln", "Pottery classes, booked online")
        self.assertNotIn("simplyluma", self.now(root))


if __name__ == "__main__":
    unittest.main()
