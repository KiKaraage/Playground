# SPDX-License-Identifier: Apache-2.0
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from luma_playground.core import events, project
from luma_playground.core.model import Brief, Member, Recipe, RecipePart
from luma_playground.core.paths import ProjectPaths
from luma_playground.core.store import ProjectStore

NICK = Member(id="nick", name="Nick", role="owner")


class CreateProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Playground Projects"
        self.env = mock.patch.dict(os.environ, {"PLAYGROUND_HOME": str(self.root)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def make(self, name="Kiln", **kwargs):
        kwargs.setdefault("owner", NICK)
        kwargs.setdefault("bus", events.EventBus())
        return project.create_project(name, **kwargs)

    def test_scaffolding(self):
        recipe = Recipe("A web app that works great on phones", "This computer",
                        [RecipePart("Pages", "HTML", "Quick on phones")])
        made = self.make(brief="Class sign-ups for a pottery studio", recipe=recipe)
        folder = self.root / "Kiln"
        self.assertEqual(made.path, folder)
        paths = ProjectPaths(folder)
        for path in (paths.agents_md, paths.claude_md, paths.mcp_json, paths.gitignore, paths.project_toml,
                     paths.brief, paths.decisions, paths.activity, paths.goal("talk-to-ari")):
            self.assertTrue(path.is_file(), path)
        for directory in paths.meta_dirs():
            self.assertTrue(directory.is_dir(), directory)
        self.assertEqual(paths.claude_md.read_text(), "@AGENTS.md\n")
        self.assertIn(".playground/local/", paths.gitignore.read_text())

        agents = paths.agents_md.read_text()
        self.assertTrue(agents.startswith("# Kiln · a Playground project"))
        self.assertNotIn(str(folder), agents, "portable: no path of this computer's in it")
        self.assertIn("Stay inside this folder", agents)
        self.assertIn(".playground/now.md", agents)
        self.assertIn(".playground/channel.md", agents)
        self.assertIn("playground:rules", agents)
        self.assertNotIn("—", agents)  # no em dashes in anything people read
        # Loaded on every run by every agent: it has to stay short.
        self.assertLess(len(agents.splitlines()), 40)
        self.assertLess(len(agents), 3000)
        self.assertEqual(paths.gemini_md.read_text(), "@AGENTS.md\n")
        self.assertIn("## Tasks", paths.guide.read_text())
        for area in ("tasks", "issues", "knowledge"):
            self.assertTrue(paths.index(area).is_file(), area)
        self.assertTrue(paths.channel_digest.is_file())

        config = json.loads(paths.mcp_json.read_text())
        server = config["mcpServers"]["playground"]
        self.assertEqual(server["args"], ["--project", str(folder)])
        self.assertTrue(Path(server["command"]).name == "playground-mcp")
        self.assertTrue(os.access(server["command"], os.X_OK), "the launcher in the source tree is executable")

        store = ProjectStore(folder)
        read = store.read_project()
        self.assertEqual((read.name, read.home, read.summary), ("Kiln", "this-computer", "Class sign-ups for a pottery studio"))
        self.assertEqual(read.owner.id, "nick")
        self.assertEqual(read.places[0].id, "this-computer")
        self.assertEqual(store.read_brief().summary, "Class sign-ups for a pottery studio")
        self.assertEqual(store.read_activity()[0].kind, "created")
        self.assertEqual(read.recipe, recipe)

    @unittest.skipUnless(shutil.which("git"), "git isn't installed")
    def test_git_first_commit(self):
        made = self.make()
        log = subprocess.run(["git", "log", "--oneline"], cwd=made.path, capture_output=True, text=True)
        self.assertIn("Start Kiln", log.stdout)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=made.path, capture_output=True, text=True)
        self.assertEqual(status.stdout.strip(), "")
        tracked = subprocess.run(["git", "ls-files"], cwd=made.path, capture_output=True, text=True).stdout
        self.assertNotIn(".playground/local", tracked)

    def test_without_git(self):
        made = self.make(git=False)
        self.assertFalse((made.path / ".git").exists())

    def test_name_clashes_and_unsafe_names(self):
        self.assertEqual(self.make(git=False).name, "Kiln")
        self.assertEqual(self.make(git=False).name, "Kiln 2")
        self.assertEqual(self.make(git=False).name, "Kiln 3")
        sneaky = self.make("../../escape", git=False)
        self.assertEqual(sneaky.path.parent, self.root)
        self.assertEqual(sneaky.name, "escape")
        self.assertEqual(self.make("", git=False).name, "My project")

    def test_failed_scaffold_leaves_nothing(self):
        with mock.patch.object(project, "render_agents_md", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.make(git=False)
        self.assertFalse((self.root / "Kiln").exists())

    def test_brief_object(self):
        brief = Brief(title="Kiln", audience="Students", platforms=["phones"])
        brief.set_section("What it is", "Booking.")
        made = self.make(brief=brief, git=False)
        self.assertEqual(ProjectStore(made.path).read_brief().audience, "Students")
        self.assertEqual(made.summary, "Booking.")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, name):
        return project.create_project(name, root=self.root, owner=NICK, git=False, bus=events.EventBus())

    def test_open_and_list_by_activity(self):
        older = self.make("Older")
        newer = self.make("Newer")
        time.sleep(1.1)
        ProjectStore(older.path).log("Something happened")
        (self.root / "Not a project").mkdir()
        (self.root / ".hidden").mkdir()
        names = [p.name for p in project.list_projects(self.root)]
        self.assertEqual(names, ["Older", "Newer"])
        opened = project.open_project(newer.path)
        self.assertEqual(opened.name, "Newer")
        self.assertTrue(opened.last_activity)
        with self.assertRaises(project.ProjectError):
            project.open_project(self.root / "Not a project")
        self.assertEqual(project.list_projects(self.root / "missing"), [])

    def test_open_repairs_mcp_json_and_keeps_other_servers(self):
        made = self.make("Kiln")
        config_path = made.path / ".mcp.json"
        config_path.write_text(json.dumps({"mcpServers": {"playground": {"command": "/old/path", "args": []},
                                                          "other": {"command": "x"}}}))
        project.open_project(made.path)
        config = json.loads(config_path.read_text())
        self.assertEqual(config["mcpServers"]["other"], {"command": "x"})
        self.assertEqual(config["mcpServers"]["playground"]["args"], ["--project", str(made.path)])
        config_path.write_text("{ not json")
        with self.assertLogs("luma_playground.core.project", "WARNING"):
            project.open_project(made.path)
        self.assertEqual(config_path.read_text(), "{ not json")  # never overwrite what we can't read

    def test_rename(self):
        made = self.make("Kiln")
        self.make("Clay")
        renamed = project.rename_project(made, "Clay")
        self.assertEqual(renamed.name, "Clay 2")
        self.assertFalse(made.path.exists())
        self.assertTrue((self.root / "Clay 2").is_dir())
        self.assertIn("# Clay 2 · a Playground project", (renamed.path / "AGENTS.md").read_text())
        self.assertNotIn(str(renamed.path), (renamed.path / "AGENTS.md").read_text())
        config = json.loads((renamed.path / ".mcp.json").read_text())
        self.assertEqual(config["mcpServers"]["playground"]["args"][1], str(renamed.path))
        self.assertEqual(ProjectStore(renamed.path).read_activity()[-1].summary, "Renamed Kiln to Clay 2")

    def test_archive(self):
        made = self.make("Kiln")
        project.archive_project(made)
        self.assertEqual(project.list_projects(self.root), [])
        self.assertEqual(len(project.list_projects(self.root, include_archived=True)), 1)
        project.archive_project(made, archived=False)
        self.assertEqual([p.name for p in project.list_projects(self.root)], ["Kiln"])
        self.assertTrue(made.path.is_dir())


if __name__ == "__main__":
    unittest.main()
