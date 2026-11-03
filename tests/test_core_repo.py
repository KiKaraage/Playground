# SPDX-License-Identifier: Apache-2.0
"""The Repository area's git: what changed, committing (even when git doesn't know who you are), the history
with restore points, and pushing to and pulling from a remote (a bare repository here, never the network)."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from luma_playground.core import repo


@unittest.skipUnless(shutil.which("git"), "needs git")
class RepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "rally"
        self.root.mkdir()
        # A computer where git was never told who anyone is.
        self.env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                                                "HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_untracked_until_started(self):
        self.assertFalse(repo.state(self.root).tracked)
        repo.start(self.root)
        state = repo.state(self.root)
        self.assertTrue(state.tracked)
        self.assertEqual(state.branch, "main")
        self.assertEqual(state.where, "Only on this computer")

    def test_changes_commit_and_history(self):
        repo.start(self.root)
        (self.root / "app.js").write_text("one\ntwo\n")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "read me.md").write_text("hi\n")
        state = repo.state(self.root)
        self.assertEqual({(c.path, c.state) for c in state.changes},
                         {("app.js", "untracked"), ("docs/read me.md", "untracked")})
        self.assertEqual(next(c for c in state.changes if c.path == "app.js").added, 2)
        self.assertEqual(repo.suggest_message(state.changes), "Add app.js and read me.md")
        short = repo.commit(self.root, "Start the waitlist", fallback=("Nick", "nick@playground.local"))
        self.assertTrue(short)
        (self.root / "app.js").write_text("one\nthree\n")
        (self.root / "docs" / "read me.md").unlink()
        state = repo.state(self.root)
        changes = {c.path: c for c in state.changes}
        self.assertEqual((changes["app.js"].state, changes["app.js"].added, changes["app.js"].removed),
                         ("modified", 1, 1))
        self.assertEqual(changes["docs/read me.md"].state, "deleted")
        self.assertEqual(repo.suggest_message(state.changes), "Update app.js and read me.md")
        self.assertEqual((state.count, state.commits[0].subject, state.commits[0].author),
                         (1, "Start the waitlist", "Nick"))
        with self.assertRaises(repo.RepoError):
            repo.commit(self.root, "   ")

    def test_restore_points_show_in_the_history(self):
        repo.start(self.root)
        (self.root / "a.txt").write_text("a\n")
        repo.commit(self.root, "First")
        repo.git(self.root, "tag", "playground/restore-3")
        self.assertEqual(repo.state(self.root).commits[0].restore, 3)

    def test_push_and_pull_through_a_remote(self):
        remote = Path(self.tmp.name) / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        repo.start(self.root)
        (self.root / "a.txt").write_text("a\n")
        repo.commit(self.root, "First")
        with self.assertRaises(repo.RepoError):
            repo.set_remote(self.root, "not a url; rm -rf /")
        repo.git(self.root, "remote", "add", "origin", str(remote))
        repo.push(self.root)
        state = repo.state(self.root)
        self.assertEqual((state.upstream, state.ahead, state.behind), ("origin/main", 0, 0))
        other = Path(self.tmp.name) / "other"
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
        (other / "b.txt").write_text("b\n")
        repo.commit(other, "Second")
        repo.push(other)
        repo.fetch(self.root)
        self.assertEqual(repo.state(self.root).behind, 1)
        (self.root / "c.txt").write_text("c\n")
        repo.commit(self.root, "Third")
        with self.assertRaises(repo.RepoError) as raised:
            repo.push(self.root)
        self.assertIn("Pull first", str(raised.exception))

    def test_connect_checks_the_remote_is_empty_and_never_overwrites(self):
        repo.start(self.root)
        with self.assertRaises(repo.RepoError):
            repo.connect(self.root, "https://example.invalid/x.git")      # nothing committed yet
        (self.root / "a.txt").write_text("a\n")
        repo.commit(self.root, "First")
        empty = Path(self.tmp.name) / "empty.git"
        theirs = Path(self.tmp.name) / "theirs.git"
        for bare in (empty, theirs):
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        other = Path(self.tmp.name) / "other"
        subprocess.run(["git", "clone", "-q", str(theirs), str(other)], check=True, capture_output=True)
        (other / "README.md").write_text("theirs\n")
        repo.commit(other, "Their README")
        repo.push(other)
        with mock.patch.object(repo, "normal_address", side_effect=lambda url: url):
            with self.assertRaises(repo.RepoError) as raised:
                repo.connect(self.root, str(theirs))
            self.assertIn("already has its own commits", str(raised.exception))
            self.assertEqual(repo.state(self.root).remote_url, "")
            repo.connect(self.root, str(empty))
        state = repo.state(self.root)
        self.assertEqual((state.upstream, state.unpushed), ("origin/main", set()))
        self.assertEqual([c.subject for c in state.remote_commits], ["First"])
        (self.root / "b.txt").write_text("b\n")
        repo.commit(self.root, "Second")
        state = repo.state(self.root)
        self.assertEqual(len(state.unpushed), 1)
        self.assertIn(state.commits[0].hash, state.unpushed)

    def test_page_addresses_become_clone_addresses(self):
        self.assertEqual(repo.normal_address("https://github.com/nick/rally"), "https://github.com/nick/rally.git")
        self.assertEqual(repo.normal_address("git@github.com:nick/rally.git"), "git@github.com:nick/rally.git")
        self.assertEqual(repo.RepoState(tracked=True, remote_url="git@github.com:nick/rally.git").web_url,
                         "https://github.com/nick/rally")
        self.assertEqual(repo.RepoState(tracked=True, remote_url="ssh://git@10.0.0.4/srv/r.git").web_url, "")

    def test_where_names_the_remote(self):
        state = repo.RepoState(tracked=True, remote_url="git@github.com:nick/rally.git")
        self.assertEqual(state.where, "github.com/nick/rally")
        state.remote_url = "https://github.com/nick/rally.git"
        self.assertEqual(state.where, "github.com/nick/rally")

    def test_branch_names_are_checked(self):
        repo.start(self.root)
        with self.assertRaises(repo.RepoError):
            repo.switch(self.root, "../etc", create=True)


if __name__ == "__main__":
    unittest.main()
