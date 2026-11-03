# SPDX-License-Identifier: Apache-2.0
"""The project's box: commands run in it with the project's own caches, installs are remembered, and an
install on the computer goes through the system's password dialog. No podman here: the runner is faked."""
from __future__ import annotations

from pathlib import Path
import shlex
import tempfile
import unittest
from unittest import mock

from luma_playground.core import box, events, project
from luma_playground.core.model import Member


class BoxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = project.create_project("Sticky Notes", root=self.tmp.name, git=False, bus=events.EventBus(),
                                           owner=Member(id="nick", name="Nick", role="owner")).path

    def tearDown(self):
        self.tmp.cleanup()

    def test_commands_run_in_the_project_with_caches_inside_it(self):
        argv = box.argv(self.root, "cargo build --release", "abc123")
        self.assertEqual(argv[:3], ["podman", "exec", "playground-sticky-notes-abc123"])
        script = argv[-1]
        self.assertIn(f"cd {shlex.quote(str(Path(self.root).absolute()))}", script)
        self.assertIn("CARGO_HOME=", script)
        self.assertIn(".playground/local/box-home", script)

    def test_the_box_sees_only_the_project(self):
        argv = box.create_argv(self.root, "abc123")
        root = str(Path(self.root).absolute())
        volumes = [argv[i + 1] for i, a in enumerate(argv) if a == "--volume"]
        self.assertEqual(volumes, [f"{root}:{root}"], "the project folder, and nothing else of the person's")
        self.assertIn("label=disable", argv)
        self.assertEqual(argv[-2:], ["sleep", "infinity"])

    def test_installs_are_remembered_for_the_next_box(self):
        fake = mock.Mock(return_value=mock.Mock(returncode=0, stdout="Complete!", stderr=""))
        result = box.run(self.root, "sudo dnf install -y rust cargo gtk4-devel", "abc", runner=fake)
        self.assertEqual(result.code, 0)
        self.assertEqual(box.packages(self.root), ["cargo", "gtk4-devel", "rust"])
        box.run(self.root, "sudo dnf install -y sqlite-devel && cargo build", "abc", runner=fake)
        self.assertIn("sqlite-devel", box.packages(self.root))
        self.assertIn("It worked.", result.words("sudo dnf install -y rust"))

    def test_the_computer_is_changed_only_through_its_own_password_dialog(self):
        with mock.patch.object(box.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"), \
                mock.patch.object(box.Path, "exists", return_value=True):
            argv = box.computer_install_argv(["gtk4", "evil; rm -rf /"])
        self.assertEqual(argv[:3], ["pkexec", "rpm-ostree", "install"])
        self.assertNotIn("evil; rm -rf /", argv)
        with self.assertRaises(box.BoxError):
            box.computer_install_argv(["; rm"])


if __name__ == "__main__":
    unittest.main()
