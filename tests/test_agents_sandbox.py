# SPDX-License-Identifier: Apache-2.0
"""Agents stay inside the project: paths, protected files, commands."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from luma_playground.agents import sandbox


class PathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.root = base / "proj"
        self.root.mkdir()
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("nope")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_relative_and_absolute_inside(self):
        self.assertEqual(sandbox.resolve_in_project(self.root, "a/b.txt"), self.root / "a" / "b.txt")
        self.assertEqual(sandbox.resolve_in_project(self.root, str(self.root / "c.txt")), self.root / "c.txt")
        self.assertEqual(sandbox.resolve_in_project(self.root, "a/../c.txt"), self.root / "c.txt")

    def test_escape_attempts_are_refused(self):
        attempts = ["../outside/secret.txt", str(self.outside / "secret.txt"), "a/../../outside/x",
                    "/etc/passwd", "~/.ssh/id_ed25519", "", "   ", "a\x00b"]
        for attempt in attempts:
            with self.subTest(attempt=attempt), self.assertRaises(PermissionError):
                sandbox.resolve_in_project(self.root, attempt)

    def test_symlink_out_of_project_is_refused(self):
        (self.root / "link").symlink_to(self.outside)
        (self.root / "file-link").symlink_to(self.outside / "secret.txt")
        for attempt in ("link/secret.txt", "file-link", "link/new.txt"):
            with self.subTest(attempt=attempt), self.assertRaises(sandbox.PathEscape):
                sandbox.resolve_in_project(self.root, attempt)
        with self.assertRaises(PermissionError):
            sandbox.read_text_file(self.root, "file-link")
        with self.assertRaises(PermissionError):
            sandbox.write_text_file(self.root, "link/new.txt", "x")
        self.assertFalse((self.outside / "new.txt").exists())

    def test_symlink_inside_project_is_fine(self):
        (self.root / "real").mkdir()
        (self.root / "alias").symlink_to(self.root / "real")
        sandbox.write_text_file(self.root, "alias/x.txt", "ok")
        self.assertEqual((self.root / "real" / "x.txt").read_text(), "ok")

    def test_protected_files_can_be_read_but_not_written(self):
        (self.root / ".mcp.json").write_text("{}")
        self.assertEqual(sandbox.read_text_file(self.root, ".mcp.json"), "{}")
        for path in (".mcp.json", ".git/hooks/pre-commit", ".git/config", ".claude/settings.json",
                     ".playground/project.toml", ".playground/answers/x.json", ".codex/config.toml"):
            with self.subTest(path=path), self.assertRaises(sandbox.ProtectedPath):
                sandbox.write_text_file(self.root, path, "x")
        # But ordinary project files under .playground are fine.
        sandbox.write_text_file(self.root, ".playground/knowledge/note.md", "hello")

    def test_read_ranges_and_binary(self):
        sandbox.write_text_file(self.root, "lines.txt", "one\ntwo\nthree\nfour\n")
        self.assertEqual(sandbox.read_text_file(self.root, "lines.txt", 2, 2), "two\nthree\n")
        (self.root / "bin.dat").write_bytes(b"\x00\x01\x02")
        with self.assertRaises(ValueError):
            sandbox.read_text_file(self.root, "bin.dat")
        with self.assertRaises(FileNotFoundError):
            sandbox.read_text_file(self.root, "missing.txt")

    def test_write_is_atomic_and_keeps_mode(self):
        target = self.root / "run.sh"
        target.write_text("old")
        os.chmod(target, 0o755)
        sandbox.write_text_file(self.root, "run.sh", "new")
        self.assertEqual(target.read_text(), "new")
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".pg-")], [])

    def test_write_through_an_inside_link_changes_its_target(self):
        (self.root / "target.txt").write_text("keep")
        (self.root / "link.txt").symlink_to(self.root / "target.txt")
        sandbox.write_text_file(self.root, "link.txt", "changed")
        self.assertEqual((self.root / "target.txt").read_text(), "changed")  # resolved inside: writes the target
        self.assertEqual(sandbox.relative_to_project(self.root, self.outside), None)


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.policy = sandbox.SandboxPolicy(self.root, backend="none")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_run_command_captures_output_and_exit(self):
        result = sandbox.run_command("echo hello; echo oops >&2; exit 3", self.policy)
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("hello", result["output"])
        self.assertIn("oops", result["output"])
        log = Path(self.root, ".playground/local/logs/sandbox.log").read_text()
        self.assertIn("network=allowed", log)

    def test_timeout_kills_the_whole_group(self):
        started = time.monotonic()
        result = sandbox.run_command("sleep 30 & sleep 30; echo never", self.policy, timeout_s=1)
        self.assertTrue(result["timed_out"])
        self.assertNotIn("never", result["output"])
        self.assertLess(time.monotonic() - started, 10)

    def test_output_limit_truncates_on_a_character_boundary(self):
        manager = sandbox.TerminalManager(self.policy)
        term = manager.create(sys.executable, ["-c", "print('é' * 5000)"], output_byte_limit=1025)
        manager.wait_for_exit(term, timeout=20)
        out = manager.output(term)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["output"].encode("utf-8")), 1025)
        self.assertNotIn("�", out["output"])
        self.assertEqual(out["exitStatus"]["exitCode"], 0)
        manager.release(term)
        with self.assertRaises(KeyError):
            manager.output(term)

    def test_kill_and_signal(self):
        manager = sandbox.TerminalManager(self.policy)
        term = manager.create("sleep", ["30"])
        manager.kill(term)
        status = manager.wait_for_exit(term, timeout=10)
        self.assertIsNone(status["exitCode"])
        self.assertIn(status["signal"], ("SIGTERM", "SIGKILL"))
        manager.release_all()

    def test_cwd_must_be_inside(self):
        manager = sandbox.TerminalManager(self.policy)
        with self.assertRaises(PermissionError):
            manager.create("ls", [], cwd="/")

    def test_env_is_cleaned(self):
        env = sandbox.clean_env({"MY_FLAG": "1"}, base={"PATH": "/bin:/usr/bin", "OPENAI_API_KEY": "sk-x",
                                                        "GITHUB_TOKEN": "t", "CLAUDE_CONFIG_DIR": "/x", "HOME": "/h"})
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertEqual(env["MY_FLAG"], "1")
        self.assertEqual(env["HOME"], "/h")

    def test_shell_line_without_args(self):
        self.assertEqual(sandbox.command_argv("npm test && echo ok", []), ["/bin/sh", "-c", "npm test && echo ok"])
        self.assertEqual(sandbox.command_argv("ls", ["-la"]), ["ls", "-la"])


class BackendTests(unittest.TestCase):
    """The real OS sandbox, where this machine has one (bubblewrap on Linux, Seatbelt on macOS)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = os.path.realpath(self.tmp.name)
        self.root = os.path.join(base, "proj")
        os.makedirs(self.root)
        self.outside = os.path.join(base, "outside.txt")
        self.policy = sandbox.SandboxPolicy(self.root)
        if sandbox.backend(self.policy) == "none":
            self.skipTest("no OS sandbox here")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_writes_land_only_in_the_project(self):
        home_probe = os.path.join(os.path.expanduser("~"), f".pg-sandbox-probe-{os.getpid()}")
        result = sandbox.run_command(f"echo in > inside.txt; echo out > {home_probe}; cat inside.txt", self.policy)
        try:
            self.assertIn("in", result["output"])
            self.assertTrue(os.path.exists(os.path.join(self.root, "inside.txt")))
            self.assertFalse(os.path.exists(home_probe), "a sandboxed command wrote to the home folder")
        finally:
            if os.path.exists(home_probe):
                os.remove(home_probe)

    def test_credentials_are_hidden(self):
        result = sandbox.run_command("ls ~/.ssh ~/.claude ~/.codex 2>&1; echo done", self.policy)
        self.assertIn("done", result["output"])
        for name in ("id_rsa", "id_ed25519", ".credentials.json", "auth.json"):
            self.assertNotIn(name, result["output"])


if __name__ == "__main__":
    unittest.main()
