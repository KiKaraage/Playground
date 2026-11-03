# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from luma_playground.core import paths


class ProjectsRootTests(unittest.TestCase):
    def test_default_is_documents(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLAYGROUND_HOME", None)
            self.assertEqual(paths.projects_root(), Path.home() / "Documents" / "Playground Projects")

    def test_overrides(self):
        with mock.patch.dict(os.environ, {"PLAYGROUND_HOME": "/tmp/pg-home", "PLAYGROUND_CONFIG_DIR": "/tmp/pg-config",
                                          "PLAYGROUND_DATA_DIR": "/tmp/pg-data"}):
            self.assertEqual(paths.projects_root(), Path("/tmp/pg-home"))
            self.assertEqual(paths.config_dir(), Path("/tmp/pg-config"))
            self.assertEqual(paths.data_dir(), Path("/tmp/pg-data"))
            self.assertEqual(paths.account_home("claude-work"), Path("/tmp/pg-data/accounts/claude-work"))

    def test_xdg(self):
        env = {"XDG_CONFIG_HOME": "/x/config", "XDG_DATA_HOME": "/x/data"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("PLAYGROUND_CONFIG_DIR", None)
            os.environ.pop("PLAYGROUND_DATA_DIR", None)
            self.assertEqual(paths.config_dir(), Path("/x/config/playground"))
            self.assertEqual(paths.data_dir(), Path("/x/data/playground"))

    def test_account_home_refuses_escape(self):
        with self.assertRaises(ValueError):
            paths.account_home("../../etc")


class NameTests(unittest.TestCase):
    def test_safe_names(self):
        cases = {"  Kiln  ": "Kiln", "../etc": "etc", "a/b": "a b", "": "My project", "...": "My project",
                 "con": "con project", "Bubble: Pop?": "Bubble Pop", "tab\there": "tab here", ".hidden": "hidden",
                 "Café": "Café"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(paths.safe_project_name(raw), expected)

    def test_long_names_are_cut(self):
        self.assertLessEqual(len(paths.safe_project_name("x" * 300)), paths.MAX_NAME_LENGTH)

    def test_unique_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(paths.unique_child(root, "Kiln").name, "Kiln")
            (root / "Kiln").mkdir()
            (root / "Kiln 2").mkdir()
            self.assertEqual(paths.unique_child(root, "Kiln").name, "Kiln 3")

    def test_slugs_and_ids(self):
        self.assertEqual(paths.slugify("Add a waitlist!"), "add-a-waitlist")
        self.assertEqual(paths.slugify("Café crème"), "cafe-creme")
        self.assertEqual(paths.slugify("!!!"), "item")
        goal_id = paths.new_id("Add a waitlist")
        self.assertRegex(goal_id, r"^add-a-waitlist-[0-9a-f]{4}$")
        self.assertTrue(paths.is_valid_id(goal_id))
        for bad in ("", "../x", "A", "a/b", "a.b", "-a", None, 3):
            self.assertFalse(paths.is_valid_id(bad), bad)


class InsideProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Kiln"
        (self.root / "src").mkdir(parents=True)
        self.outside = Path(self.tmp.name) / "secret.txt"
        self.outside.write_text("no")

    def tearDown(self):
        self.tmp.cleanup()

    def test_relative_and_absolute_inside(self):
        self.assertEqual(paths.resolve_inside(self.root, "src/app.js"), (self.root / "src/app.js").resolve())
        self.assertEqual(paths.resolve_inside(self.root, self.root / "src"), (self.root / "src").resolve())
        self.assertEqual(paths.relative_to_project(self.root, "./src/../src/app.js"), "src/app.js")
        self.assertEqual(paths.relative_to_project(self.root, "."), ".")

    def test_dotdot_escape(self):
        for bad in ("../secret.txt", "src/../../secret.txt", str(self.outside), "/etc/passwd", "~/x"):
            with self.subTest(bad=bad), self.assertRaises(paths.PathOutsideProject):
                paths.resolve_inside(self.root, bad)

    def test_symlink_escape(self):
        link = self.root / "src" / "sneaky"
        os.symlink(self.outside, link)
        with self.assertRaises(paths.PathOutsideProject):
            paths.resolve_inside(self.root, "src/sneaky")
        dir_link = self.root / "out"
        os.symlink(Path(self.tmp.name), dir_link)
        with self.assertRaises(paths.PathOutsideProject):
            paths.resolve_inside(self.root, "out/secret.txt")
        self.assertFalse(paths.is_inside(self.root, "out/new-file.txt"))

    def test_symlink_inside_is_fine(self):
        os.symlink(self.root / "src", self.root / "code")
        self.assertTrue(paths.is_inside(self.root, "code/app.js"))

    def test_nul_byte(self):
        with self.assertRaises(paths.PathOutsideProject):
            paths.resolve_inside(self.root, "src/a\x00b")

    def test_well_known_files(self):
        p = paths.ProjectPaths(self.root)
        self.assertEqual(p.goal("add-a-waitlist-7f3a"), self.root / ".playground/goals/add-a-waitlist-7f3a.md")
        self.assertEqual(p.thread("talk-to-ari"), self.root / ".playground/threads/talk-to-ari.jsonl")
        with self.assertRaises(ValueError):
            p.goal("../../evil")
        with self.assertRaises(ValueError):
            p.answer("x/y")



MANIFEST = 'schema_version = "0.1"\n\n[application]\nid = "org.projectluma.Playground"\nname = "Playground"\n'
SOURCE = Path(__file__).resolve().parents[1]


class InstalledPathTests(unittest.TestCase):
    """Playground's own files come from the source tree only when there really is one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(os.path.realpath(self.tmp.name))
        paths._source_tree_of.cache_clear()
        self.addCleanup(paths._source_tree_of.cache_clear)

    def tearDown(self):
        self.tmp.cleanup()

    def layout(self, folder: str, manifest: str | None, stray: bool = True) -> Path:
        root = self.base / folder
        package = root / "luma_playground"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        if manifest is not None:
            (root / "luma-app.toml").write_text(manifest)
        if stray:   # what some other wheel could leave next to the package
            (root / "data").mkdir()
            (root / "data" / "playground.css").write_text("/* not ours */")
            (root / "bin").mkdir()
            (root / "bin" / "playground-mcp").write_text("#!/bin/sh\n")
        return package

    def test_a_source_tree_is_found_by_its_manifest(self):
        package = self.layout("src/luma-playground", MANIFEST)
        root = package.parent
        self.assertEqual(paths.source_tree(package), root)
        self.assertEqual(paths.app_data("playground.css", package=package), root / "data" / "playground.css")
        self.assertEqual(paths.app_data("icons", package=package), root / "data" / "icons")
        self.assertEqual(paths.mcp_launcher(package), str(root / "bin" / "playground-mcp"))
        self.assertEqual(paths.code_root(package), root)

    def test_site_packages_with_a_stray_data_folder_is_not_a_source_tree(self):
        package = self.layout("lib/python3.14/site-packages", None)
        installed_mcp = self.base / "usr-bin-playground-mcp"
        installed_mcp.write_text("#!/bin/sh\n")
        self.assertIsNone(paths.source_tree(package))
        self.assertEqual(paths.app_data("playground.css", package=package),
                         Path("/usr/share/playground/playground.css"))
        self.assertEqual(paths.app_data(package=package), paths.INSTALLED_DATA_DIR)
        with mock.patch.object(paths, "INSTALLED_MCP", installed_mcp):
            self.assertEqual(paths.mcp_launcher(package), str(installed_mcp))
        self.assertEqual(paths.code_root(package), package.parent)

    def test_another_apps_manifest_does_not_count(self):
        package = self.layout("site-packages", MANIFEST.replace("org.projectluma.Playground", "org.example.Other"))
        self.assertIsNone(paths.source_tree(package))
        self.assertEqual(paths.app_data("cards.css", package=package), paths.INSTALLED_DATA_DIR / "cards.css")

    def test_a_source_tree_without_its_launcher_falls_back_to_the_installed_one(self):
        package = self.layout("checkout", MANIFEST, stray=False)
        missing = self.base / "nowhere" / "playground-mcp"
        with mock.patch.object(paths, "INSTALLED_MCP", missing), \
                mock.patch.object(paths.shutil, "which", return_value=None):
            self.assertEqual(paths.mcp_launcher(package), str(missing))
        with mock.patch.object(paths, "INSTALLED_MCP", missing), \
                mock.patch.object(paths.shutil, "which", return_value="/opt/bin/playground-mcp"):
            self.assertEqual(paths.mcp_launcher(package), "/opt/bin/playground-mcp")

    def test_an_installed_copy_ignores_stray_files_end_to_end(self):
        # A real copy of the package in a site-packages-like folder, imported by a fresh interpreter.
        package = self.layout("site-packages", None)
        (package / "core").mkdir()
        for name in ("__init__.py", "paths.py"):
            shutil.copy(SOURCE / "luma_playground" / "core" / name, package / "core" / name)
        shutil.copy(SOURCE / "luma_playground" / "__init__.py", package / "__init__.py")
        code = (f"import sys; sys.path.insert(0, {str(package.parent)!r}); from luma_playground.core import paths; "
                "print(paths.source_tree()); print(paths.app_data('playground.css')); print(paths.package_dir())")
        out = subprocess.run([sys.executable, "-I", "-c", code], cwd=str(self.base), capture_output=True, text=True,
                             check=True)
        lines = out.stdout.split("\n")
        self.assertEqual(lines[0], "None")
        self.assertEqual(lines[1], "/usr/share/playground/playground.css")
        self.assertEqual(lines[2], str(package))

    def test_this_checkout_is_a_source_tree_and_everything_uses_it(self):
        from luma_playground.core import project
        self.assertEqual(paths.source_tree(), SOURCE)
        self.assertTrue(paths.app_data("playground.css").is_file())
        self.assertTrue(paths.app_data("icons").is_dir())
        self.assertEqual(project.mcp_command(), str(SOURCE / "bin" / "playground-mcp"))
        self.assertEqual(project.INSTALLED_MCP, paths.INSTALLED_MCP)

    def test_nothing_works_the_location_out_on_its_own(self):
        # One helper decides: no module guesses the source tree from its own position or names /usr/share.
        offenders = []
        for path in sorted((SOURCE / "luma_playground").rglob("*.py")):
            if path.name == "paths.py" and path.parent.name == "core":
                continue
            text = path.read_text(encoding="utf-8")
            for needle in ("parents[2]", "/usr/share/playground", "/usr/bin/playground-mcp"):
                if needle in text and not (path.name == "project.py" and needle == "/usr/bin/playground-mcp"):
                    offenders.append(f"{path.relative_to(SOURCE)}: {needle}")
            if "Path(__file__)" in text or "os.path.dirname(__file__)" in text:
                offenders.append(f"{path.relative_to(SOURCE)}: __file__")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
