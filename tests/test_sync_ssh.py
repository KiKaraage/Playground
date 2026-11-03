# SPDX-License-Identifier: Apache-2.0
"""Sharing over a real, unprivileged sshd with real certificates.

Like test_platform_security's sshd test: a spare port on 127.0.0.1, this
account as the server account, and the setup script run against a scratch
folder instead of /etc. sshd asks the project's key check (the same script the
root setup installs) at every sign-in; here it runs as this account through
``/bin/sh``, which sshd accepts because /bin/sh is root's. The root-owned
version of the same setup is tested in test_sync_root.py (the VM, with sudo).
Skips cleanly where sshd can't run.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import time
import unittest

from sync_support import HAVE_TOOLS, TOOLS_SKIP, SyncWorld  # noqa: F401

from luma_playground.core import places, security, sync
from luma_playground.core.model import Place, ThreadEntry
from luma_playground.core.store import ProjectStore

SSHD = next((p for p in ("/usr/sbin/sshd", "/usr/bin/sshd") if os.path.exists(p)), None)
HAVE_SSHD = bool(HAVE_TOOLS and SSHD and shutil.which("ssh"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def git(cwd: Path, *args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check,
                          env={**os.environ, **(env or {})})


class SshWorld(SyncWorld):
    """A project, a scratch trust folder, and an unprivileged sshd on a spare port."""

    trust = "live"

    def setUp(self) -> None:
        super().setUp()
        self.data["priya2"] = self.tmp / "priya2-data"
        self.roots["priya2"] = self.tmp / "priya2-projects"
        self.user = os.environ.get("USER") or subprocess.run(["id", "-un"], capture_output=True,
                                                             text=True).stdout.strip()
        if not security._USER_RE.match(self.user):
            self.skipTest("this account name can't be a principal file name")
        self.port = free_port()
        self.srv = self.tmp / "etc-ssh-playground"

    def tearDown(self) -> None:
        proc = getattr(self, "sshd", None)
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            proc.stdout.close()
        super().tearDown()

    def run_setup(self, script: str, env: dict | None = None) -> None:
        script = script.replace('if [ "$(id -u)" != 0 ]; then echo "Please run this with sudo."; exit 1; fi\n', "")
        script = script.split("CONF=/etc/ssh/sshd_config.d")[0]
        proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True, env={**os.environ, **(env or {})})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def sshd_lines(self) -> list[str]:
        return [f"AuthorizedKeysCommand /bin/sh {self.srv}/bin/keys %u %t %f %k",
                f"AuthorizedKeysCommandUser {self.user}", "AuthorizedKeysFile none",
                f"RevokedKeys {self.srv}/revoked.krl"]

    def start_sshd(self, lines: list[str] | None = None) -> None:
        hostkey = self.tmp / "hostkey"
        if not hostkey.exists():
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(hostkey)], check=True)
        config = self.tmp / "sshd_config"
        config.write_text("\n".join([
            f"Port {self.port}", "ListenAddress 127.0.0.1", f"HostKey {hostkey}", f"PidFile {self.tmp / 'sshd.pid'}",
            "StrictModes no", "PasswordAuthentication no", "KbdInteractiveAuthentication no",
            "UsePAM no", *(lines if lines is not None else self.sshd_lines()), ""]))
        self.sshd = subprocess.Popen([SSHD, "-D", "-e", "-f", str(config)], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.sshd.poll() is not None:
                self.skipTest("sshd won't run unprivileged here: " + self.sshd.stdout.read().decode()[-200:])
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        self.skipTest("sshd didn't start")

    def sshd_log(self) -> str:
        if self.sshd.poll() is None:
            self.sshd.terminate()
        try:
            return self.sshd.communicate(timeout=3)[0].decode()[-1500:]
        except Exception:  # noqa: BLE001
            return ""

    def join(self, name: str, member_id: str, role: str, as_who: str | None = None) -> Path:
        """``as_who``: whose computer (data folder) joins; the same member from another computer if it differs."""
        as_who = as_who or member_id
        with self.member("nick"):
            invite = sync.create_invite(self.nick_dir, role=role, member_id=member_id, name=name)
        self.assertNotIn("PRIVATE", invite.file_text)
        with self.member(as_who):
            seen = sync.read_invite(invite.code)
            self.assertEqual(seen.project_name, "Kiln")
            self.assertEqual(seen.safety, self.safety)          # both screens show the same safety code
            request = sync.request_to_join(seen, name)
        with self.member("nick"):
            checked = sync.read_join_request(self.nick_dir, request.code)
            welcome = sync.approve_join(self.nick_dir, checked)
        self.assertTrue(welcome.extra["pushed"], welcome.extra)
        with self.member(as_who):
            return sync.accept_approval(welcome.code, self.roots[as_who])

    def device(self, who: str) -> security.DeviceKey:
        device = sync._device(self.data[who])
        assert device is not None
        return device

    def shell(self, who: str, words: str = "echo let-me-in") -> subprocess.CompletedProcess:
        """Open a shell on the server with this computer's development pass."""
        device = self.device(who)
        cert = device.certificate_path(self.project_id)
        return subprocess.run(["ssh", "-F", "none", "-p", str(self.port), "-i", str(device.private_path), "-o",
                               f"CertificateFile={cert}", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                               "-o", "LogLevel=ERROR", f"{self.user}@127.0.0.1", words], capture_output=True,
                              text=True, timeout=30, stdin=subprocess.DEVNULL)

    def thread_texts(self, project_dir: Path, goal_id: str) -> list[str]:
        return [e.text for e in ProjectStore(project_dir).read_thread(goal_id, fold=False)]


@unittest.skipUnless(HAVE_SSHD, "needs git, ssh-keygen, ssh and sshd")
class ServerHomeTests(SshWorld):
    """A Dev server with the (root) setup: every sign-in is checked against the signed access list."""

    def setUp(self) -> None:
        super().setUp()
        self.nick_dir = self.make_project()
        store = ProjectStore(self.nick_dir)
        project = store.read_project()
        self.project_id = project.id
        project.places.append(Place(id="dev-box", name="the dev box", role="development", kind="ssh",
                                    host="127.0.0.1", user=self.user, port=self.port))
        store.write_project(project)
        self.server_home = self.tmp / "server-home" / "kiln"
        with self.member("nick"):
            setup = sync.home_setup_instructions(self.nick_dir, mode="server", place_id="dev-box",
                                                 trust_dir=str(self.srv), trust=self.trust, home=str(self.server_home))
        self.assertEqual(setup["trust"], self.trust)
        self.setup_script = setup["script"]
        self.prepare_server(setup)
        with self.member("nick"):
            info = sync.share_project(self.nick_dir, mode="server", place_id="dev-box", home=str(self.server_home),
                                      trust_dir=str(self.srv), trust=self.trust)
        self.assertEqual(info["home"], "the dev box")
        self.assertEqual(info["trust"], self.trust)
        self.safety = info["safety"]

    def prepare_server(self, setup: dict) -> None:
        self.assertIn("AuthorizedKeysCommand", setup["script"])
        self.assertIn("sudo sh", setup["command"])
        self.run_setup(setup["script"])
        self.start_sshd()

    # ── the tests ──

    def test_invite_round_trip_sync_roles_and_revocation(self):
        # The home exists, main is checked out on the server, and the gate holds the records.
        self.assertTrue((self.server_home / "repo.git").is_dir())
        self.assertTrue((self.server_home / "checkout" / "AGENTS.md").exists() is False)
        self.assertTrue((self.server_home / "checkout" / "CLAUDE.md").exists())
        try:
            priya_dir = self.join("Priya", "priya", "builder")
        except sync.SyncError:
            self.fail("joining failed; sshd says:\n" + self.sshd_log())
        self.assertEqual(ProjectStore(priya_dir).read_project().id, self.project_id)
        members = {m.id: m for m in ProjectStore(priya_dir).read_project().members}
        self.assertEqual(members["priya"].role, "builder")
        self.assertIn("devices", members["priya"].extra)

        nick = self.engine(self.nick_dir, "nick")
        priya = self.engine(priya_dir, "priya")
        goal = ProjectStore(self.nick_dir).create_goal("Add a waitlist")
        self.settle(nick, priya)
        ProjectStore(priya_dir).append_thread(goal.id, ThreadEntry(role="person", kind="message", text="from priya"))
        ProjectStore(self.nick_dir).append_thread(goal.id, ThreadEntry(role="person", kind="message", text="from nick"))
        self.settle(priya, nick)
        for project_dir in (self.nick_dir, priya_dir):
            self.assertIn("from priya", self.thread_texts(project_dir, goal.id))
            self.assertIn("from nick", self.thread_texts(project_dir, goal.id))

        # Code: Priya's goal branch reaches the server; a release updates the server's checkout.
        priya.begin_goal(goal.id)
        (priya_dir / "waitlist.html").write_text("<form></form>\n")
        priya.checkpoint(goal_id=goal.id)
        result = priya.release_goal(goal.id, "Add a waitlist")
        self.assertTrue(result["pushed"])
        self.assertTrue((self.server_home / "checkout" / "waitlist.html").exists())

        # A sync pass runs git for this project and nothing else.
        with self.member("priya"):
            url, env = priya._remote(priya.pin())
        argv = shlex.split(env["GIT_SSH_COMMAND"])
        shell = subprocess.run([*argv, f"{self.user}@127.0.0.1", "echo let-me-in"], capture_output=True, text=True,
                               timeout=20, stdin=subprocess.DEVNULL)
        self.assertNotIn("let-me-in", shell.stdout)
        self.assertIn("only for syncing", shell.stderr)
        # And no one can write someone else's presence.
        fake = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=priya_dir, input='{"member":"nick"}',
                              capture_output=True, text=True, check=True).stdout.strip()
        pushed = git(priya_dir, "push", url, f"+{fake}:refs/playground/presence/nick", env=env, check=False)
        self.assertNotEqual(pushed.returncode, 0)
        self.assertIn("not where others are", pushed.stderr)

        # Previews: Nick's development pass gives a shell and a checkout of his branch on the server.
        with self.member("nick"):
            nick.begin_goal(goal.id, switch=True)
            place = nick.stage_place()
        self.assertTrue(place["path"].endswith("/stage/nick"))
        self.assertTrue((Path(place["path"]) / "waitlist.html").exists())

        # Revocation: the very next sync is refused, with words, and nothing needs running on the server.
        with self.member("nick"):
            removed = sync.remove_member_access(self.nick_dir, "priya")
        self.assertTrue(removed["pushed"], removed)
        ProjectStore(priya_dir).append_thread(goal.id, ThreadEntry(role="person", kind="message", text="after"))
        status = priya.sync_now()
        self.assertEqual(status["state"], "removed", status)
        self.assertIn("took this computer off", status["detail"])
        nick.sync_now()
        self.assertNotIn("after", self.thread_texts(self.nick_dir, goal.id))
        # The same pass, by hand: still in date, but the gate (root's, in a real setup) refuses it, saying why.
        again = git(priya_dir, "ls-remote", url, env=env, check=False)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("isn't on the project's access list", again.stderr)
        if self.trust == "live":
            self.assertNotIn("let-me-in", self.shell("priya").stdout)

    def test_shell_access_ends_the_moment_a_builder_is_removed(self):
        try:
            priya_dir = self.join("Priya", "priya", "builder")
        except sync.SyncError:
            self.fail("joining failed; sshd says:\n" + self.sshd_log())
        self.engine(priya_dir, "priya").sync_now()
        opened = self.shell("priya")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertIn("let-me-in", opened.stdout)
        # Her pass itself is still in date: only the access list changes.
        cert = security.read_certificate(self.device("priya").certificate_path(self.project_id))
        self.assertGreater(sync._valid_to_epoch(cert), time.time() + 3600)
        with self.member("nick"):
            self.assertTrue(sync.remove_member_access(self.nick_dir, "priya")["pushed"])
        refused = self.shell("priya")
        self.assertNotEqual(refused.returncode, 0)
        self.assertNotIn("let-me-in", refused.stdout)
        self.assertIn("Permission denied", refused.stderr)
        # Nick, still on the list, gets in as before.
        self.assertIn("let-me-in", self.shell("nick").stdout)

    def test_an_access_list_whose_time_is_up_is_refused_until_an_owner_renews_it(self):
        try:
            priya_dir = self.join("Priya", "priya", "builder")
        except sync.SyncError:
            self.fail("joining failed; sshd says:\n" + self.sshd_log())
        nick = self.engine(self.nick_dir, "nick")
        priya = self.engine(priya_dir, "priya")
        self.settle(nick, priya)
        # Someone with a shell puts an old list back: properly signed, but its time is up.
        with self.member("nick"):
            desk = sync.OwnerDesk(self.nick_dir, self.data["nick"], now=time.time() - sync.ACCESS_VALID_S - 600)
            self.assertTrue(desk.publish_access(force=True))
        stale = git(self.nick_dir, "rev-parse", sync.ACCESS_REF).stdout.strip()
        git(self.nick_dir, "update-ref", "refs/playground/stale-test", stale)
        repo = self.server_home / "repo.git"
        git(repo, "fetch", "-q", str(self.nick_dir), "+refs/playground/stale-test:refs/playground/access")
        ca = sync.Pin.load(self.data["nick"], self.project_id).ca
        state, _parsed = sync.verify_access(ca, self.project_id,
                                            sync._access_files(sync._Repo(repo), "refs/playground/access"))
        self.assertEqual(state, "expired")
        # Priya's sync and her shell are refused, in plain words.
        status = priya.sync_now()
        self.assertEqual(status["state"], "access", status)
        self.assertIn("run out", status["detail"])
        self.assertNotIn("let-me-in", self.shell("priya").stdout)
        # Nick's Playground comes back: it may only send a fresh list, and does, first thing.
        status = nick.sync_now()
        self.assertEqual(status["state"], "up-to-date", status)
        self.assertEqual(sync.verify_access(nick.pin().ca, self.project_id,
                                            sync._access_files(sync._Repo(repo), "refs/playground/access"))[0], "ok")
        self.assertEqual(priya.sync_now()["state"], "up-to-date")
        self.assertIn("let-me-in", self.shell("priya").stdout)

    def test_a_second_owner_renews_passes_and_the_list_while_the_first_is_away(self):
        try:
            rae_dir = self.join("Rae", "rae", "owner")
            priya_dir = self.join("Priya", "priya", "builder")
        except sync.SyncError:
            self.fail("joining failed; sshd says:\n" + self.sshd_log())
        nick = self.engine(self.nick_dir, "nick")
        rae = self.engine(rae_dir, "rae")
        priya = self.engine(priya_dir, "priya")
        self.settle(nick, rae, priya)
        self.assertIn(self.device("rae").fingerprint, rae.pin().issuers)
        self.assertTrue(sync.sharing_info(rae_dir, self.data["rae"])["issues_passes"])
        # Nick's computer is away from now on. Rae's Playground renews Priya's passes with Rae's own key.
        with self.member("rae"):
            desk = sync.OwnerDesk(rae_dir, self.data["rae"])
            self.assertFalse(desk.is_root)
            self.assertTrue(desk.renew_certs(force_members=["priya"]))
            self.assertTrue(desk.publish_access(force=True))   # and a fresh access list, signed by Rae
        self.settle(rae, priya)
        cert = security.read_certificate(sync.sync_cert_path(self.device("priya"), self.project_id))
        self.assertEqual(cert.signing_ca, self.device("rae").fingerprint)
        home = sync._Repo(self.server_home / "repo.git")
        state, parsed = sync.verify_access(rae.pin().ca, self.project_id, sync._access_files(home, sync.ACCESS_REF))
        self.assertEqual(state, "ok")
        self.assertEqual(security.fingerprint(parsed["signer"]), self.device("rae").fingerprint)
        # Priya syncs with the pass Rae signed, and her shell works with Rae's development pass.
        goal = ProjectStore(priya_dir).create_goal("Gift cards")
        self.settle(priya, rae)
        self.assertEqual(ProjectStore(rae_dir).read_goal(goal.id).title, "Gift cards")
        self.assertIn("let-me-in", self.shell("priya").stdout)
        # When no owner's Playground will be open, Priya asks for a fresh pass with a signed code.
        with self.member("priya"):
            request = sync.request_renewal(priya_dir)
        self.assertTrue(request.code.startswith("PGR1-"))
        self.assertNotIn("PRIVATE", request.file_text)
        with self.member("rae"):
            checked = sync.read_renewal_request(rae_dir, request.code)
            answer = sync.answer_renewal(rae_dir, checked)
        self.assertTrue(answer.code.startswith("PGP1-"))
        forged = json.loads(answer.file_text)
        forged["body"]["member"] = "nick"
        with self.member("priya"):
            with self.assertRaises(sync.InviteError):
                sync.accept_renewal(priya_dir, json.dumps(forged))
            result = sync.accept_renewal(priya_dir, answer.code)
        self.assertIn("sync", result["installed"])
        self.assertEqual(priya.sync_now()["state"], "up-to-date")

    def test_a_second_computer_for_the_same_person(self):
        try:
            priya_dir = self.join("Priya", "priya", "builder")
            laptop_dir = self.join("Priya", "priya", "builder", as_who="priya2")
        except sync.SyncError:
            self.fail("joining failed; sshd says:\n" + self.sshd_log())
        desk_top, laptop = self.device("priya"), self.device("priya2")
        self.assertNotEqual(desk_top.fingerprint, laptop.fingerprint)      # each computer has its own key
        members = ProjectStore(laptop_dir).read_project().members
        self.assertEqual([m.id for m in members].count("priya"), 1)       # still one person
        self.assertEqual(len(next(m for m in members if m.id == "priya").extra["devices"]), 2)
        nick = self.engine(self.nick_dir, "nick")
        one = self.engine(priya_dir, "priya")
        two = self.engine(laptop_dir, "priya2")
        goal = ProjectStore(self.nick_dir).create_goal("Gift cards")
        self.settle(nick, one, two)
        ProjectStore(laptop_dir).append_thread(goal.id, ThreadEntry(role="person", kind="message", text="laptop"))
        self.settle(two, one, nick)
        self.assertIn("laptop", self.thread_texts(priya_dir, goal.id))
        self.assertEqual(sync.member_devices(self.nick_dir, self.data["nick"])["priya"].__len__(), 2)
        for who, key in (("priya", desk_top), ("priya2", laptop)):
            cert = security.read_certificate(sync.sync_cert_path(key, self.project_id))
            self.assertEqual(cert.public_key_fingerprint, key.fingerprint)
            self.assertIn("let-me-in", self.shell(who).stdout)
        # The laptop is lost: take that computer off; the other one keeps working.
        with self.member("nick"):
            sync.remove_device(self.nick_dir, "priya", laptop.fingerprint)
        self.assertEqual(two.sync_now()["state"], "removed")
        self.assertNotIn("let-me-in", self.shell("priya2").stdout)
        self.assertEqual(one.sync_now()["state"], "up-to-date")
        self.assertIn("let-me-in", self.shell("priya").stdout)
        self.assertIn("priya", {m.id for m in ProjectStore(self.nick_dir).read_project().members})

    def test_restore_points_from_two_teammates_never_clash(self):
        try:
            priya_dir = self.join("Priya", "priya", "builder")
        except sync.SyncError:
            self.fail("joining failed; sshd says:\n" + self.sshd_log())
        nick = self.engine(self.nick_dir, "nick")
        priya = self.engine(priya_dir, "priya")
        self.settle(nick, priya)
        # Both release at the same moment, before either has heard of the other's: same number, two points.
        (self.nick_dir / "nick.txt").write_text("nick\n")
        (priya_dir / "priya.txt").write_text("priya\n")
        with self.member("nick"):
            ours = places.release(self.nick_dir, places.THIS_COMPUTER, None, None, title="Nick's change")
        with self.member("priya"):
            theirs = places.release(priya_dir, places.THIS_COMPUTER, None, None, title="Priya's change")
        self.assertEqual(ours["restore_point"], theirs["restore_point"])
        self.assertNotEqual(ours["restore_id"], theirs["restore_id"])
        self.assertTrue(ours["restore_id"].startswith("nick/"))
        self.assertTrue(theirs["restore_id"].startswith("priya/"))
        self.settle(nick, priya, nick)
        # Everyone's history shows both, and each computer has the code for both (tags travel with sync).
        for project_dir in (self.nick_dir, priya_dir):
            points = {p["id"]: p for p in places.restore_points(project_dir)}
            self.assertIn(ours["restore_id"], points)
            self.assertIn(theirs["restore_id"], points)
            self.assertEqual(points[ours["restore_id"]]["code"], "tag")
            self.assertEqual(points[theirs["restore_id"]]["code"], "tag")
            self.assertEqual(points[ours["restore_id"]]["commit"], ours["commit"])
        # The home keeps them as they are: a restore point can't be moved or taken by someone else.
        with self.member("priya"):
            url, env = priya._remote(priya.pin())
        moved = git(priya_dir, "push", url, f"+HEAD:refs/tags/playground/restore/{ours['restore_id']}", env=env,
                    check=False)
        self.assertNotEqual(moved.returncode, 0)
        # The next number goes past both.
        self.assertEqual(places.next_restore_number(self.nick_dir), ours["restore_point"] + 1)

    def test_viewers_read_but_cannot_change(self):
        try:
            rae_dir = self.join("Rae", "rae", "viewer")
        except sync.SyncError:
            self.fail("joining failed; sshd says:\n" + self.sshd_log())
        nick = self.engine(self.nick_dir, "nick")
        rae = self.engine(rae_dir, "rae")
        goal = ProjectStore(self.nick_dir).create_goal("Gift cards")
        self.settle(nick, rae)
        self.assertEqual(ProjectStore(rae_dir).read_goal(goal.id).title, "Gift cards")
        ProjectStore(rae_dir).append_thread(goal.id, ThreadEntry(role="person", kind="message", text="viewer says"))
        self.settle(rae, nick)                              # rae doesn't even try; status stays up to date
        self.assertNotIn("viewer says", self.thread_texts(self.nick_dir, goal.id))
        self.assertIn("rae", {p["member"] for p in sync.read_presence(self.nick_dir)})
        # Forcing it by hand: the home refuses.
        with self.member("rae"):
            url, env = rae._remote(rae.pin())
        head = git(rae_dir, "rev-parse", sync.STATE_REF).stdout.strip()
        forced = git(rae_dir, "push", url, f"{head}:{sync.STATE_REF}", env=env, check=False)
        self.assertNotEqual(forced.returncode, 0)
        self.assertIn("Viewers can look", forced.stderr)

    @unittest.skipUnless(security.crypto_available()[0], "python3-cryptography isn't installed here (the VM has it)")
    def test_the_vault_opens_for_the_new_member_and_closes_after(self):
        with self.member("nick"):
            vault = security.Vault(self.nick_dir, self.project_id)
            vault.create("nick", "Nick")
            vault.set("DATABASE_URL", "postgres://kiln:s3cret-value@db/kiln")
        try:
            priya_dir = self.join("Priya", "priya", "builder")
        except sync.SyncError:
            self.fail("joining failed; sshd says:\n" + self.sshd_log())
        with self.member("priya"):
            self.assertEqual(security.Vault(priya_dir, self.project_id).get("DATABASE_URL"),
                             "postgres://kiln:s3cret-value@db/kiln")
        for path in self.all_files(priya_dir / ".playground", self.server_home):
            self.assertNotIn(b"s3cret-value", path.read_bytes(), path)
        with self.member("nick"):
            removed = sync.remove_member_access(self.nick_dir, "priya")
        self.assertEqual(removed["to_change"], ["DATABASE_URL"])

    def all_files(self, *roots: Path) -> list[Path]:
        return [Path(d) / f for root in roots for d, _dirs, files in os.walk(root) for f in files]

    def test_tampered_invites_and_requests_are_refused(self):
        with self.member("nick"):
            invite = sync.create_invite(self.nick_dir, role="builder", name="Sam")
        envelope = json.loads(invite.file_text)
        envelope["body"]["home"]["hosts"] = ["203.0.113.66"]          # point it somewhere else
        with self.member("priya"):
            with self.assertRaises(sync.InviteError):
                sync.read_invite(json.dumps(envelope))
            request = sync.request_to_join(sync.read_invite(invite.code), "Sam")
        forged = json.loads(request.file_text)
        forged["body"]["name"] = "Someone else"
        with self.member("nick"):
            with self.assertRaises(sync.InviteError):
                sync.read_join_request(self.nick_dir, json.dumps(forged))
            welcome = sync.approve_join(self.nick_dir, sync.read_join_request(self.nick_dir, request.code))
            with self.assertRaises(sync.InviteError):          # an invite works once
                sync.read_join_request(self.nick_dir, request.code)
        forged_welcome = json.loads(welcome.file_text)
        forged_welcome["body"]["member"]["role"] = "owner"
        with self.member("priya"):
            with self.assertRaises(sync.InviteError):
                sync.accept_approval(json.dumps(forged_welcome), self.roots["priya"])


@unittest.skipUnless(HAVE_SSHD, "needs git, ssh-keygen, ssh and sshd")
class AccountHomeTests(ServerHomeTests):
    """A Dev server where nobody can use sudo: the trust lives in the account's own authorized_keys.

    Syncing still stops at once. Shells can't be checked at every sign-in without root, so a removed builder's
    development pass works until it runs out: the weaker guarantee People states plainly."""

    trust = "account"

    def prepare_server(self, setup: dict) -> None:
        self.assertNotIn("sudo", setup["command"])
        self.assertIn("12 hours", setup["words"])
        self.keys_file = self.tmp / "authorized_keys"
        self.run_setup(setup["script"], env={"PLAYGROUND_AUTHORIZED_KEYS": str(self.keys_file)})
        self.run_setup(setup["script"], env={"PLAYGROUND_AUTHORIZED_KEYS": str(self.keys_file)})   # safe again
        self.assertEqual(self.keys_file.read_text().count("(begin)"), 1)
        self.start_sshd([f"AuthorizedKeysFile {self.keys_file}"])

    def test_shell_access_ends_the_moment_a_builder_is_removed(self):
        try:
            priya_dir = self.join("Priya", "priya", "builder")
        except sync.SyncError:
            self.fail("joining failed; sshd says:\n" + self.sshd_log())
        priya = self.engine(priya_dir, "priya")
        priya.sync_now()
        self.assertIn("let-me-in", self.shell("priya").stdout)
        with self.member("nick"):
            sync.remove_member_access(self.nick_dir, "priya")
        self.assertEqual(priya.sync_now()["state"], "removed")                 # syncing stops at once
        self.assertIn("let-me-in", self.shell("priya").stdout)                 # the shell, honestly, doesn't

    def test_an_access_list_whose_time_is_up_is_refused_until_an_owner_renews_it(self):
        self.skipTest("covered by the server with the root setup; the gate is the same script")

    def test_a_second_owner_renews_passes_and_the_list_while_the_first_is_away(self):
        """Without root, only the project's own key hands out passes, but any owner can renew the list."""
        try:
            rae_dir = self.join("Rae", "rae", "owner")
        except sync.SyncError:
            self.fail("joining failed; sshd says:\n" + self.sshd_log())
        nick, rae = self.engine(self.nick_dir, "nick"), self.engine(rae_dir, "rae")
        self.settle(nick, rae)
        with self.member("rae"):
            desk = sync.OwnerDesk(rae_dir, self.data["rae"])
            self.assertFalse(desk.can_issue_passes())
            self.assertTrue(desk.publish_access(force=True))
        self.assertEqual(rae.sync_now()["state"], "up-to-date")
        self.assertFalse(sync.sharing_info(rae_dir, self.data["rae"])["issues_passes"])

    def test_a_second_computer_for_the_same_person(self):
        self.skipTest("covered by the server with the root setup")

    def test_restore_points_from_two_teammates_never_clash(self):
        self.skipTest("covered by the server with the root setup")


@unittest.skipUnless(HAVE_SSHD, "needs git, ssh-keygen, ssh and sshd")
class ThisComputerHomeTests(ServerHomeTests):
    """The owner's computer is the home: same passes and gate, sync-only trust, a warning."""

    def setUp(self) -> None:
        SshWorld.setUp(self)
        self.nick_dir = self.make_project()
        self.project_id = ProjectStore(self.nick_dir).read_project().id
        with self.member("nick"):
            setup = sync.home_setup_instructions(self.nick_dir, mode="this-computer", user=self.user,
                                                 trust_dir=str(self.srv))
        self.assertNotIn(security.principal_for(self.project_id, "development"), setup["script"])
        self.run_setup(setup["script"])
        self.start_sshd(["AuthorizedKeysFile none", "StrictModes no",
                         f"TrustedUserCAKeys {self.srv}/trusted_user_ca_keys", f"RevokedKeys {self.srv}/revoked.krl",
                         f"AuthorizedPrincipalsFile {self.srv}/principals/%u"])
        hostkey = (self.tmp / "hostkey.pub").read_text()
        with self.member("nick"):
            info = sync.share_project(self.nick_dir, mode="this-computer", port=self.port, addresses=["127.0.0.1"],
                                      host_key=hostkey, trust_dir=str(self.srv), user=self.user)
        self.assertEqual(info["warning"], sync.THIS_COMPUTER_WARNING)
        self.safety = info["safety"]

    def test_invite_round_trip_sync_roles_and_revocation(self):
        try:
            priya_dir = self.join("Priya", "priya", "builder")
        except sync.SyncError:
            self.fail("joining failed; sshd says:\n" + self.sshd_log())
        nick = self.engine(self.nick_dir, "nick")
        priya = self.engine(priya_dir, "priya")
        self.assertEqual(nick.sync_now()["warning"], sync.THIS_COMPUTER_WARNING)   # the host is told
        self.assertNotIn("warning", priya.sync_now())
        goal = ProjectStore(priya_dir).create_goal("Gift cards")
        self.settle(priya, nick)
        self.assertEqual(ProjectStore(self.nick_dir).read_goal(goal.id).title, "Gift cards")
        # Teammates never get a shell on the owner's computer: no development principal is trusted here.
        self.assertTrue(self.device("priya").certificate_path(self.project_id).exists())
        self.assertNotIn("let-me-in", self.shell("priya").stdout)
        # Removing her works at once: the gate checks the list on every sync.
        with self.member("nick"):
            sync.remove_member_access(self.nick_dir, "priya")
        self.assertEqual(priya.sync_now()["state"], "removed")

    def test_offline_says_whose_computer(self):
        try:
            priya_dir = self.join("Priya", "priya", "builder")
        except sync.SyncError:
            self.fail("joining failed; sshd says:\n" + self.sshd_log())
        priya = self.engine(priya_dir, "priya")
        self.sshd.terminate()
        try:
            self.sshd.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.sshd.kill()
            self.sshd.wait(timeout=10)
        status = priya.sync_now()
        self.assertEqual(status["state"], "offline")
        self.assertIn("Nick’s computer", status["detail"])

    def test_shell_access_ends_the_moment_a_builder_is_removed(self):
        self.skipTest("teammates never get a shell on the owner's computer")

    def test_an_access_list_whose_time_is_up_is_refused_until_an_owner_renews_it(self):
        self.skipTest("covered by the server home")

    def test_a_second_owner_renews_passes_and_the_list_while_the_first_is_away(self):
        self.skipTest("the computer holding the project is the one that hands out passes")

    def test_a_second_computer_for_the_same_person(self):
        self.skipTest("covered by the server home")

    def test_restore_points_from_two_teammates_never_clash(self):
        self.skipTest("covered by the server home")

    def test_the_vault_opens_for_the_new_member_and_closes_after(self):
        self.skipTest("covered by the server home; the vault doesn't depend on where the home is")


if __name__ == "__main__":
    unittest.main()
