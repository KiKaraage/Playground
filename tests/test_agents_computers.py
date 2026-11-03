# SPDX-License-Identifier: Apache-2.0
"""Agents on another computer: addresses, the list, the finder (run here, as it runs there), launching. No SSH."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from luma_playground.agents import computers
from luma_playground.agents.connections import Connection


class AddressTests(unittest.TestCase):
    def test_addresses_in_peoples_words(self):
        self.assertEqual(computers.parse_address("root@10.0.0.4"), ("root", "10.0.0.4", 22))
        self.assertEqual(computers.parse_address("nick@build.example:6767"), ("nick", "build.example", 6767))
        self.assertEqual(computers.parse_address("buildbox"), ("", "buildbox", 22))
        with self.assertRaises(computers.ComputerError):
            computers.parse_address("root@bad host; rm -rf /")

    def test_ssh_never_asks_and_can_go_through_another_machine(self):
        argv = computers.ssh_argv(computers.Computer(id="b", name="B", host="10.0.0.4", user="root", port=6767,
                                                     identity="~/.ssh/k", jump="me@gateway"))
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        self.assertEqual(argv[argv.index("-J") + 1], "me@gateway")
        self.assertEqual(argv[argv.index("-p") + 1], "6767")
        self.assertEqual(argv[-1], "root@10.0.0.4")


class StoreTests(unittest.TestCase):
    def test_add_list_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = computers.ComputerStore(Path(tmp) / "computers.toml")
            first = store.add(computers.Computer(id="", name="Build server", host="10.0.0.4", user="root"))
            second = store.add(computers.Computer(id="", name="Build server", host="10.0.0.5"))
            self.assertEqual((first.id, second.id), ("build-server", "build-server-2"))
            self.assertEqual(store.get("build-server").user, "root")
            self.assertTrue(store.remove("build-server"))
            self.assertEqual([c.id for c in store.list()], ["build-server-2"])


class FinderTests(unittest.TestCase):
    """The probe is one Python program sent to the computer; here it runs on this one, in a made-up home."""

    def test_the_probe_finds_accounts_and_never_reads_a_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".codex").mkdir()
            (home / ".codex" / "auth.json").write_text('{"tokens": "SECRET-TOKEN"}')
            (home / ".claude-work").mkdir()
            (home / ".claude-work" / ".credentials.json").write_text('{"secret": "SECRET-TOKEN"}')
            env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
            result = subprocess.run([sys.executable, "-"], input=computers.probe_script(deep=False),
                                    capture_output=True, text=True, env=env, timeout=60)
            self.assertIn("PLAYGROUND-PROBE", result.stdout, result.stderr)
            self.assertNotIn("SECRET-TOKEN", result.stdout)
            import json
            found = json.loads(result.stdout.split("PLAYGROUND-PROBE", 1)[1])
            signed = {(a["provider"], Path(a["home"]).name) for a in found["accounts"] if a["signed_in"]}
            self.assertIn(("codex", ".codex"), signed)
            self.assertIn(("claude", ".claude-work"), signed)

    def test_suggestions_name_the_person_and_skip_what_isnt_signed_in(self):
        computer = computers.Computer(id="build", name="Build server", host="h", user="root")
        found = {"accounts": [
            {"provider": "codex", "home": "/root/.codex", "label": "Codex", "signed_in": False, "user": "root"},
            {"provider": "codex", "home": "/srv/dev/home/.codex", "label": "Codex", "signed_in": True,
             "method": "ChatGPT", "user": "luma-dev", "user_home": "/srv/dev/home"},
            {"provider": "claude", "home": "/srv/dev/home/.claude", "label": "Claude", "signed_in": True,
             "method": "subscription", "user": "luma-dev", "user_home": "/srv/dev/home"}]}
        offered = computers.suggest_connections(computer, found, "nick")
        self.assertEqual([c.label for c in offered], ["Codex · luma-dev", "Claude · luma-dev"])
        codex = offered[0]
        self.assertEqual((codex.where, codex.config_home, codex.kind, codex.private_seat),
                         ("build", "/srv/dev/home/.codex", "personal-plan", True))
        self.assertEqual(codex.extra, {"remote_user": "luma-dev", "remote_home": "/srv/dev/home"})
        codex.validate()
        again = computers.suggest_connections(computer, found, "nick", existing=offered)
        self.assertEqual(again, [])


class LaunchTests(unittest.TestCase):
    def test_runs_as_the_accounts_owner_in_their_copy_of_the_project(self):
        computer = computers.Computer(id="build", name="Build server", host="h", user="root", home="/root")
        conn = Connection(id="codex-dev", label="Codex · luma-dev", kind="personal-plan", provider="codex",
                          where="build", config_home="/srv/dev/home/.codex", private_seat=True,
                          extra={"remote_user": "luma-dev", "remote_home": "/srv/dev/home"})
        info = {"node": "/srv/dev/home/.local/share/playground/runtime/node-v24.21.0-linux-x64/bin/node",
                "script": "/srv/dev/home/.local/share/playground/agents/codex-acp/2.2.2/x/index.js", "args": []}
        with mock.patch.object(computers, "ensure_agent", return_value=info) as setup:
            from luma_playground.agents import registry
            argv, _env, folder = computers.remote_launch(conn, computer, "/home/sam/Projects/Sticky Notes",
                                                         registry.builtin_registry(), project_id="761625ea-aa")
        self.assertEqual(setup.call_args.kwargs["user"], "luma-dev")
        self.assertEqual(folder, "/srv/dev/home/playground/sticky-notes-761625ea")
        command = argv[-1]
        self.assertTrue(command.startswith("runuser -u luma-dev -- env HOME=/srv/dev/home sh -c "))
        inner = shlex.split(command)[-1]
        self.assertIn("CODEX_HOME=/srv/dev/home/.codex", inner)
        self.assertIn("cd /srv/dev/home/playground/sticky-notes-761625ea", inner)

    def test_mirroring_copies_as_the_owner_and_never_deletes(self):
        computer = computers.Computer(id="build", name="Build server", host="h", user="root")
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")
        computers.push(computer, "/p/Sticky", "/srv/dev/home/playground/sticky", user="luma-dev",
                       user_home="/srv/dev/home", runner=fake)
        rsync = calls[-1]
        self.assertIn("--update", rsync)
        self.assertNotIn("--delete", " ".join(rsync))
        self.assertIn("--rsync-path=runuser -u luma-dev -- rsync", rsync)
        self.assertIn("--exclude=.playground/local/", rsync)
        self.assertEqual(rsync[-2:], ["/p/Sticky/", "root@h:/srv/dev/home/playground/sticky/"])

    def test_sign_in_runs_there_as_the_owner_with_a_terminal(self):
        computer = computers.Computer(id="build", name="Build server", host="h", user="root")
        argv = computers.sign_in_argv(computer, "codex", "/srv/dev/home/.codex", user="luma-dev",
                                      user_home="/srv/dev/home")
        self.assertIn("-t", argv)
        self.assertNotIn("-T", argv)
        command = argv[-1]
        self.assertTrue(command.startswith("runuser -u luma-dev -- env HOME=/srv/dev/home env CODEX_HOME="))
        self.assertIn("codex login --device-auth", shlex.split(command)[-1])


if __name__ == "__main__":
    unittest.main()
