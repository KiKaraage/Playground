# SPDX-License-Identifier: Apache-2.0
"""The root setup for a Dev server, installed for real with sudo (the VM; skips elsewhere).

The setup script runs as root and installs its checks root-owned under a
scratch trust folder in /run (sshd insists every folder up to the key check is
root's and not writable by anyone else). sshd itself runs unprivileged on a
spare port, as in the other sshd tests, and calls the root-owned key check
directly. One test also runs that check as root, the way a real sshd does, so it
reads the project as the server account through runuser.
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import unittest

import test_sync_ssh as base
from test_sync_ssh import git

from luma_playground.core import security, sync


def _sudo_works() -> bool:
    if not sys.platform.startswith("linux") or not shutil.which("sudo") or not os.path.isdir("/run"):
        return False
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


HAVE_SUDO = _sudo_works()


@unittest.skipUnless(base.HAVE_SSHD and HAVE_SUDO, "needs sshd and passwordless sudo on Linux (the VM)")
class RootSetupTests(base.ServerHomeTests):
    """Everything ServerHomeTests checks, with the checks really belonging to root."""

    def setUp(self) -> None:
        self.root_dir = Path(f"/run/playground-test-{secrets.token_hex(4)}")
        super().setUp()

    def tearDown(self) -> None:
        super().tearDown()
        subprocess.run(["sudo", "-n", "rm", "-rf", str(self.root_dir)], capture_output=True, timeout=30)

    def prepare_server(self, setup: dict) -> None:
        # The real script, as root; only the last part (changing the system's own sshd) is left out.
        script = setup["script"].split("CONF=/etc/ssh/sshd_config.d")[0]
        proc = subprocess.run(["sudo", "-n", "sh", "-c", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.start_sshd()

    def sshd_lines(self) -> list[str]:
        return [f"AuthorizedKeysCommand {self.srv}/bin/keys %u %t %f %k", f"AuthorizedKeysCommandUser {self.user}",
                "AuthorizedKeysFile none", f"RevokedKeys {self.srv}/revoked.krl"]

    @property
    def srv(self) -> Path:  # type: ignore[override]
        return self.root_dir

    @srv.setter
    def srv(self, _value: Path) -> None:
        pass

    def project_dir_on_server(self) -> Path:
        return self.srv / "projects" / self.project_id

    # ── root-only checks ──

    def test_the_checks_belong_to_root_and_the_server_account_cannot_change_them(self):
        pieces = [self.srv / "bin" / "keys", self.project_dir_on_server() / "gate",
                  self.project_dir_on_server() / "hooks" / "pre-receive",
                  self.project_dir_on_server() / "hooks" / "post-receive",
                  self.project_dir_on_server() / "live", self.project_dir_on_server() / "ca.pub"]
        for path in [*pieces, self.srv, self.srv / "bin", self.project_dir_on_server()]:
            st = path.stat()
            self.assertEqual(st.st_uid, 0, path)
            self.assertEqual(st.st_mode & 0o022, 0, path)
            self.assertFalse(os.access(path, os.W_OK), path)
        with self.assertRaises(PermissionError):
            with open(self.project_dir_on_server() / "gate", "a") as handle:
                handle.write("exit 0\n")
        with self.assertRaises(PermissionError):
            (self.project_dir_on_server() / "live").write_text("founder=me SHA256:x\n")
        # Pushes made from a shell on the server run root's hooks too.
        hooks = git(self.server_home / "repo.git", "config", "core.hooksPath").stdout.strip()
        self.assertEqual(hooks, str(self.project_dir_on_server() / "hooks"))

    def test_the_key_check_run_as_root_reads_the_project_as_its_account(self):
        try:
            priya_dir = self.join("Priya", "priya", "builder")
        except sync.SyncError:
            self.fail("joining failed; sshd says:\n" + self.sshd_log())
        self.engine(priya_dir, "priya").sync_now()

        def ask(who: str) -> str:
            device = self.device(who)
            kind, blob = device.certificate_path(self.project_id).read_text().split()[:2]
            proc = subprocess.run(["sudo", "-n", str(self.srv / "bin" / "keys"), self.user, kind, device.fingerprint,
                                   blob], capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout

        prefix = security.principal_prefix(self.project_id)
        self.assertIn(f'cert-authority,principals="{prefix}-development,{prefix}-preview",', ask("priya"))
        self.assertIn(f'{prefix}-production', ask("noor"))
        with self.member("noor"):
            sync.remove_member_access(self.noor_dir, "priya")
        self.assertEqual(ask("priya"), "")
        self.assertIn(f"{prefix}-development", ask("noor"))

    def test_a_shell_can_put_back_an_old_list_but_not_one_whose_time_is_up(self):
        try:
            priya_dir = self.join("Priya", "priya", "builder")
            rae_dir = self.join("Rae", "rae", "viewer")
        except sync.SyncError:
            self.fail("joining failed; sshd says:\n" + self.sshd_log())
        noor, priya, rae = (self.engine(self.noor_dir, "noor"), self.engine(priya_dir, "priya"),
                            self.engine(rae_dir, "rae"))
        self.settle(noor, priya, rae)
        # A list from three days ago, with Rae on it: properly signed, but its time is up.
        with self.member("noor"):
            desk = sync.OwnerDesk(self.noor_dir, self.data["noor"], now=time.time() - sync.ACCESS_VALID_S - 600)
            desk.publish_access(force=True)
        old = git(self.noor_dir, "rev-parse", sync.ACCESS_REF).stdout.strip()
        git(self.noor_dir, "update-ref", "refs/playground/old-list", old)
        self.assertEqual(noor.sync_now()["state"], "up-to-date")          # a fresh list goes out again
        with self.member("noor"):
            sync.remove_member_access(self.noor_dir, "rae")
        self.assertEqual(rae.sync_now()["state"], "removed")
        # Priya, a builder with a shell, puts the old list back on the server and tries to change the gate.
        repo = self.server_home / "repo.git"
        git(repo, "fetch", "-q", str(self.noor_dir), "+refs/playground/old-list:refs/playground/old-list")
        opened = self.shell("priya", f"git --git-dir={repo} update-ref refs/playground/access {old} && "
                                     f"echo put-back; echo x >> {self.project_dir_on_server() / 'gate'}")
        self.assertIn("put-back", opened.stdout)
        self.assertIn("Permission denied", opened.stderr)
        # Rae still can't sync: the list she's on ran out, and only an owner may sync on a list like that.
        status = rae.sync_now()
        self.assertNotEqual(status["state"], "up-to-date", status)
        self.assertNotIn("let-me-in", self.shell("priya", "echo let-me-in").stdout)   # nor shells, for anyone
        # Noor's Playground puts a current list back, and everyone else is in again.
        self.assertEqual(noor.sync_now()["state"], "up-to-date")
        self.assertIn("let-me-in", self.shell("priya", "echo let-me-in").stdout)
        self.assertNotEqual(rae.sync_now()["state"], "up-to-date")


if __name__ == "__main__":
    unittest.main()
