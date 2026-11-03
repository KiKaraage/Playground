# SPDX-License-Identifier: Apache-2.0
"""Sub-projects: an OS with an app inside it. A focus, not a fence: each part keeps its own tasks and
knowledge, sees what bears on it from above, and the whole sees every part in a line."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from luma_playground.core import areas, events, family, project
from luma_playground.core.model import KnowledgeNote, Member
from luma_playground.core.store import ProjectStore
from luma_playground.mcp import server as mcp

NICK = Member(id="nick", name="Nick", role="owner")


class FamilySetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"PLAYGROUND_HOME": str(self.home / "Projects")})
        env.start()
        self.addCleanup(env.stop)
        family.forget()
        self.luma = project.create_project("Luma", "An operating system", root=self.home / "Projects", owner=NICK,
                                           git=False).path
        self.luma_store = ProjectStore(self.luma)
        self.luma_store.write_knowledge(KnowledgeNote(slug="lucide-only", title="Icons are Lucide only",
                                                      extra={"kind": "rule", "status": "kept"}))
        self.luma_store.write_knowledge(KnowledgeNote(
            slug="apps-layout", title="How apps are laid out", body="Each app lives in src/apps/<name>/.",
            extra={"kind": "part", "status": "kept", "paths": ["src/apps/"]}))
        self.code = self.luma / "src" / "apps" / "calculator"
        self.code.mkdir(parents=True)
        (self.code / "main.py").write_text("print('2 + 2')\n")
        (self.code / "AGENTS.md").write_text("# Calculator\n\nKeep the keypad 4 wide.\n")
        (self.code / ".gitignore").write_text("*.o\n")
        self.calc = project.adopt_folder(self.code, "Calculator", summary="A calculator for Luma").path

    def tearDown(self):
        family.forget()
        self.tmp.cleanup()


class FamilyTests(FamilySetup):
    def test_a_folder_of_code_becomes_a_part_without_losing_a_word(self):
        self.assertEqual(self.calc, self.code)
        self.assertEqual((self.code / "main.py").read_text(), "print('2 + 2')\n")
        text = (self.code / "AGENTS.md").read_text()
        self.assertTrue(text.startswith("# Calculator\n\nKeep the keypad 4 wide.\n"))
        self.assertIn(project.ADOPTED_MARK, text)
        self.assertIn("sub-project of **Luma** (`../../../`): a focus, not a fence", text)
        self.assertIn("This folder is this part of Luma", text)
        self.assertNotIn(str(self.home), text)
        self.assertEqual((self.code / ".gitignore").read_text().splitlines()[0], "*.o")
        self.assertIn(".playground/local/", (self.code / ".gitignore").read_text())
        # Again: it's opened, nothing is added twice.
        project.adopt_folder(self.code)
        self.assertEqual((self.code / "AGENTS.md").read_text().count(project.ADOPTED_MARK), 1)

    def test_the_family_is_found_at_any_depth_and_by_name(self):
        self.assertEqual(family.children_of(self.luma), [self.calc])
        self.assertEqual(family.parent_of(self.calc), self.luma)
        self.assertEqual(family.root_of(self.calc), self.luma)
        self.assertEqual(family.resolve(self.calc, "Luma"), self.luma)
        self.assertEqual(family.resolve(self.calc, "../../.."), self.luma)
        self.assertEqual(family.resolve(self.luma, "calculator"), self.calc)
        with self.assertRaisesRegex(ValueError, "Calculator \\(src/apps/calculator/\\)"):
            family.resolve(self.luma, "Clock")

    def test_each_map_carries_its_own_part_and_a_line_about_the_other(self):
        ProjectStore(self.calc).create_task("Add a percent key")
        part = ProjectStore(self.calc).paths.now.read_text()
        self.assertIn("## Part of Luma", part)
        self.assertIn("Rule from above, it holds here too: Icons are Lucide only", part)
        self.assertIn("Luma knows: How apps are laid out (`../../../.playground/knowledge/apps-layout.md`)", part)
        whole = self.luma_store.paths.now.read_text()
        self.assertIn("## Sub-projects (1)", whole)
        self.assertIn("**Calculator** (`src/apps/calculator/`): A calculator for Luma · 1 ready", whole)
        self.assertNotIn("percent key", whole)            # the whole sees the part, not its insides
        self.assertNotIn("## Part of", whole)

    def test_a_rule_made_above_reaches_the_parts_next_turn(self):
        since = "2000-01-01T00:00:00Z"
        brief = areas.brief_since(ProjectStore(self.calc), since)
        self.assertIn("new rule in Luma, follow it here too: Icons are Lucide only", brief)

    def test_a_new_part_can_sit_anywhere_inside(self):
        clock = project.create_project("Clock", root=self.luma / "src" / "apps", parent=self.luma, owner=NICK,
                                       git=False).path
        self.assertEqual(clock.parent, self.luma / "src" / "apps")
        family.forget()
        self.assertEqual(family.children_of(self.luma), [self.calc, clock])

    def test_a_repository_outside_the_projects_folder_is_listed_once_brought_in(self):
        repo = self.home / "code" / "website"
        repo.mkdir(parents=True)
        project.adopt_folder(repo, "Website")
        self.assertIn(repo, [p.path for p in project.list_projects()])
        self.assertIn(self.luma, [p.path for p in project.list_projects()])


class FamilyToolTests(FamilySetup):
    def server(self, folder):
        return mcp.PlaygroundServer(folder, stdin=io.BytesIO(), stdout=io.BytesIO(), actor="ari")

    def call(self, server, tool, **arguments):
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": tool, "arguments": arguments}})
        return response["result"]

    def test_a_bug_in_a_shared_part_is_filed_where_it_belongs(self):
        part = self.server(self.calc)
        result = self.call(part, "playground_issue_create", title="Buttons ignore the accent colour",
                           severity="medium", project="Luma")
        self.assertFalse(result["isError"], result["content"][0]["text"])
        self.assertEqual([i.title for i in self.luma_store.list_issues()], ["Buttons ignore the accent colour"])
        self.assertEqual(ProjectStore(self.calc).list_issues(), [])
        wrong = self.call(part, "playground_issue_create", title="x", severity="low", project="Clock")
        self.assertTrue(wrong["isError"])
        self.assertIn("No project called Clock", wrong["content"][0]["text"])

    def test_the_now_tool_brings_what_the_projects_above_know_about_the_paths(self):
        text = self.call(self.server(self.calc), "playground_now", paths=["main.py"])["content"][0]["text"]
        self.assertIn("Luma · part: How apps are laid out", text)

    def test_an_agent_makes_a_part_from_the_whole(self):
        whole = self.server(self.luma)
        result = self.call(whole, "playground_subproject_create", name="Clock", folder="src/apps/clock",
                           summary="World clocks")
        self.assertFalse(result["isError"], result["content"][0]["text"])
        clock = self.luma / "src" / "apps" / "clock"
        self.assertTrue(family.is_project(clock))
        self.assertEqual(family.name_of(clock), "Clock")
        outside = self.call(whole, "playground_subproject_create", name="Away", folder="../away")
        self.assertTrue(outside["isError"])


if __name__ == "__main__":
    unittest.main()
