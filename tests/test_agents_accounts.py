# SPDX-License-Identifier: Apache-2.0
"""Finding every Claude and Codex account, however the person keeps it. No real home folder is touched."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from luma_playground.agents import accounts
from luma_playground.agents.connections import Connection


class AccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.data = Path(self.tmp.name) / "data"
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_DATA_DIR": str(self.data)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = {"HOME": str(self.home), "PATH": "/nonexistent", "XDG_DATA_DIRS": str(self.home / "none")}

    def write(self, rel: str, text: str = "", mode: int = 0o644) -> Path:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
        return path

    def found(self, provider: str, saved=()) -> list[accounts.Account]:
        return accounts.discover(provider, self.env, saved)

    def test_normal_folder_always_comes_first(self):
        found = self.found("codex")
        self.assertEqual([(a.label, a.home, a.id) for a in found], [("Codex", "", "codex")])

    def test_launcher_script_names_a_second_codex(self):
        profile = self.home / ".local/share/codex-profile-2"
        self.write(".local/share/codex-profile-2/auth.json", "{}")
        self.write(".local/bin/codex-2", f"#!/bin/sh\nexport CODEX_HOME={profile}\nexec chatgpt \"$@\"\n", 0o755)
        found = self.found("codex")
        self.assertEqual([a.label for a in found], ["Codex", "Codex 2"])
        self.assertEqual(found[1].home, str(profile))
        self.assertEqual(found[1].source, "your codex-2 launcher")
        self.assertEqual(found[1].id, "codex-2")

    def test_shell_alias_with_home_variable(self):
        self.write(".zshrc", "alias claude-me='CLAUDE_CONFIG_DIR=$HOME/.hidden/me claude'\n")
        found = self.found("claude")
        self.assertEqual(found[1].home, str(self.home / ".hidden/me"))
        self.assertEqual(found[1].source, "your shell settings")
        self.assertEqual(found[1].label, "Claude (me)")

    def test_fish_and_desktop_shortcuts(self):
        self.write(".config/fish/config.fish", "set -gx CODEX_HOME ~/work-codex\n")
        apps = self.home / ".local/share/applications"
        self.write(".local/share/applications/codex-play.desktop",
                   "[Desktop Entry]\nName=Codex Play\nExec=env CODEX_HOME=/opt/play/codex chatgpt\n")
        env = dict(self.env, XDG_DATA_HOME=str(apps.parent))
        found = accounts.discover("codex", env)
        homes = {a.home: a.source for a in found}
        self.assertEqual(homes[str(self.home / "work-codex")], "your shell settings")
        self.assertEqual(homes["/opt/play/codex"], "the Codex Play shortcut")

    def test_named_folders_only_when_they_look_like_the_programs(self):
        self.write(".codex-work/auth.json", "{}")
        (self.home / ".codex-empty").mkdir()
        self.write(".claude-old/.claude.json", "{}")
        self.assertEqual([a.label for a in self.found("codex")], ["Codex", "Codex (work)"])
        self.assertEqual([a.label for a in self.found("claude")], ["Claude", "Claude (old)"])

    def test_extra_desktop_app_profiles_get_their_own_folder(self):
        self.write(".local/share/applications/claude.desktop",
                   "[Desktop Entry]\nName=Claude\nExec=claude\n\n[Desktop Action Uninstall]\nName=Uninstall\n")
        self.write(".local/share/applications/claude-2.desktop", "[Desktop Entry]\nName=Claude 2\nExec=claude-2\n")
        self.write(".local/share/applications/claude-work.desktop",
                   "[Desktop Entry]\nName=Claude (Work)\nExec=claude-work\n")
        self.write(".local/share/applications/other.desktop", "[Desktop Entry]\nName=Claude Helper\n")
        env = dict(self.env, XDG_DATA_HOME=str(self.home / ".local/share"))
        found = accounts.discover("claude", env)
        self.assertEqual([a.label for a in found], ["Claude", "Claude (Work)", "Claude 2"])
        self.assertEqual(found[2].source, "the Claude 2 app")
        self.assertEqual(found[2].home, str(self.data / "accounts" / "claude-2"))

    def test_saved_accounts_keep_their_names_and_nothing_is_listed_twice(self):
        folder = self.home / ".codex-work"
        self.write(".codex-work/auth.json", "{}")
        self.write(".bashrc", f"export CODEX_HOME={folder}\n")
        saved = [Connection(id="codex-job", label="Codex at work", kind="personal-plan", provider="codex",
                            config_home=str(folder)),
                 Connection(id="gone", label="Codex gone", kind="personal-plan", provider="codex",
                            config_home=str(self.home / "nowhere"))]
        found = self.found("codex", saved)
        self.assertEqual([a.label for a in found], ["Codex", "Codex at work", "Codex gone"])
        self.assertEqual(found[1].source, "Playground")

    def test_default_folder_from_the_environment_is_not_repeated(self):
        self.write("elsewhere/auth.json", "{}")
        self.write(".profile", f"export CODEX_HOME={self.home / 'elsewhere'}\n")
        env = dict(self.env, CODEX_HOME=str(self.home / "elsewhere"))
        self.assertEqual([a.label for a in accounts.discover("codex", env)], ["Codex"])

    def test_signed_in_only_checks_the_file_is_there(self):
        self.assertFalse(accounts.signed_in("codex", "", self.env))
        self.write(".codex/auth.json", "secret")
        self.assertTrue(accounts.signed_in("codex", "", self.env))

    def test_big_or_binary_files_are_skipped(self):
        self.write(".local/bin/big", "CODEX_HOME=/x\n" + "#" * (accounts.MAX_SCAN_BYTES + 1))
        (self.home / ".local/bin/bin").write_bytes(b"\0ELF CODEX_HOME=/y\n")
        self.assertEqual(len(self.found("codex")), 1)

    def test_search_path_adds_home_installs_newest_node_first(self):
        for version in ("v20.1.0", "v24.3.0", "v22.10.0"):
            (self.home / ".nvm/versions/node" / version / "bin").mkdir(parents=True)
        (self.home / ".npm-global/bin").mkdir(parents=True)
        with mock.patch.object(accounts, "SYSTEM_BIN_DIRS", ()):
            parts = accounts.search_path(self.env).split(os.pathsep)
        self.assertEqual(parts[0], "/nonexistent")
        nvm = [p for p in parts if ".nvm" in p]
        self.assertEqual([Path(p).parent.name for p in nvm], ["v24.3.0", "v22.10.0", "v20.1.0"])
        self.assertIn(str(self.home / ".npm-global/bin"), parts)


if __name__ == "__main__":
    unittest.main()
