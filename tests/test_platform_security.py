# SPDX-License-Identifier: Apache-2.0
"""Device keys, the project CA, revocation, server setup, and the vault."""
from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import time
import unittest
from unittest import mock

from test_platform_support import CRYPTO_SKIP, HAVE_CRYPTO, HAVE_SSH_KEYGEN, PlatformTestCase

from luma_playground.core import security
from luma_playground.core.security import ProjectCA, SecurityError, Vault, VaultAccessDenied, VaultTampered

PROJECT_ID = "kiln-0123456789abcdef"


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(HAVE_SSH_KEYGEN, "ssh-keygen isn't installed")
class DeviceKeyTests(PlatformTestCase):
    def test_keygen_makes_private_files_and_never_shares_the_private_half(self):
        key = security.device_key(connect_id="nick@luma.connect")
        self.assertEqual(mode(key.private_path), 0o600)
        self.assertEqual(mode(key.private_path.parent), 0o700)
        self.assertTrue(key.public_key.startswith("ssh-ed25519 "))
        self.assertTrue(key.fingerprint.startswith("SHA256:"))
        self.assertEqual(key.connect_id, "nick@luma.connect")
        shared = json.dumps(key.public_dict())
        self.assertNotIn("PRIVATE", shared)
        self.assertNotIn(str(key.private_path), shared)
        # Our fingerprint matches OpenSSH's own.
        out = subprocess.run(["ssh-keygen", "-lf", str(key.private_path) + ".pub"], capture_output=True, text=True).stdout
        self.assertIn(key.fingerprint, out)

    def test_same_key_on_second_call_and_relabel(self):
        first = security.device_key()
        second = security.bind_identity("priya@luma.connect")
        self.assertEqual(first.public_key, second.public_key)
        self.assertEqual(second.connect_id, "priya@luma.connect")

    def test_loose_permissions_are_tightened(self):
        key = security.device_key()
        os.chmod(key.private_path, 0o644)
        security.device_key()
        self.assertEqual(mode(key.private_path), 0o600)

    def test_keyring_encrypts_the_key_and_askpass_unlocks_it(self):
        self.fake_keyring()
        key = security.device_key()
        self.assertEqual(key.protection, "keyring")
        # Without the passphrase, the key can't be read.
        locked = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(key.private_path)],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertNotEqual(locked.returncode, 0)
        # Through the askpass helper (keyring lookup), it can.
        unlocked = subprocess.run(["ssh-keygen", "-y", "-f", str(key.private_path)], capture_output=True, text=True,
                                  env={**os.environ, **key.ssh_env()}, stdin=subprocess.DEVNULL)
        self.assertEqual(unlocked.returncode, 0, unlocked.stderr)
        self.assertEqual(security.normalize_public_key(unlocked.stdout), key.public_key)
        # The helper holds lookup attributes only, never the passphrase.
        helper = Path(key.ssh_env()["SSH_ASKPASS"])
        self.assertEqual(mode(helper), 0o700)
        stored = next((self.tmp / "keyring").iterdir()).read_text()
        self.assertNotIn(stored, helper.read_text())

    def test_bad_public_keys_are_refused(self):
        for bad in ("", "ssh-rsa AAAAB3NzaC1yc2E", "ssh-ed25519 not-base64!", "ssh-ed25519 QUJD\nkey: evil",
                    "ssh-ed25519 AAAAB3NzaC1yc2EAAAADAQAB"):
            with self.subTest(bad=bad), self.assertRaises(SecurityError):
                security.normalize_public_key(bad)


@unittest.skipUnless(HAVE_SSH_KEYGEN, "ssh-keygen isn't installed")
class CertificateAuthorityTests(PlatformTestCase):
    def setUp(self):
        super().setUp()
        self.ca = ProjectCA.create(PROJECT_ID)
        self.key = security.device_key()

    def test_ca_lives_on_this_device_never_in_the_project(self):
        self.assertTrue(self.ca.private_path.exists())
        self.assertEqual(mode(self.ca.private_path), 0o600)
        self.assertEqual(mode(self.ca.dir), 0o700)
        self.assertFalse(self.ca.dir.is_relative_to(self.project_dir))
        with self.assertRaises(SecurityError):
            ProjectCA.create(PROJECT_ID)

    def test_issue_short_lived_certificate_and_read_it_with_ssh_keygen(self):
        self.ca.add_member("priya", "Priya", "builder", self.key.public_key, "laptop")
        out = self.tmp / "priya-cert.pub"
        cert = self.ca.issue("priya", out_path=out)
        listing = subprocess.run(["ssh-keygen", "-L", "-f", str(out)], capture_output=True, text=True).stdout
        self.assertIn('Key ID: "priya@laptop"', listing)
        self.assertIn(self.ca.fingerprint, listing)
        self.assertEqual(cert.principals, [security.principal_for(PROJECT_ID, "development"),
                                           security.principal_for(PROJECT_ID, "preview")])
        self.assertIn("permit-pty", cert.extensions)
        self.assertIn("permit-port-forwarding", cert.extensions)
        self.assertNotIn("permit-agent-forwarding", cert.extensions)
        start = security._parse_local_time(cert.valid_from)
        end = security._parse_local_time(cert.valid_to)
        self.assertLessEqual((end - start).total_seconds(), 12 * 3600 + 5 * 60 + 1)
        self.assertEqual(self.ca.verify(cert), (True, "Valid."))
        # Serials go up.
        self.assertEqual(self.ca.issue("priya").serial, cert.serial + 1)

    def test_roles_limit_what_a_certificate_can_reach(self):
        self.ca.add_member("priya", "Priya", "builder", self.key.public_key)
        with self.assertRaises(SecurityError):
            self.ca.issue("priya", place_roles=["production"])
        self.ca.add_member("rae", "Rae", "reviewer", self.key.public_key)
        with self.assertRaisesRegex(SecurityError, "don't get server access"):
            self.ca.issue("rae")
        self.ca.add_member("nick", "Nick", "owner", self.key.public_key)
        prod = self.ca.issue("nick", place_roles=["production"])
        self.assertEqual(prod.principals, [security.principal_for(PROJECT_ID, "production")])
        self.assertNotIn("permit-port-forwarding", prod.extensions)
        with self.assertRaises(SecurityError):
            self.ca.issue("nick", hours=48)
        with self.assertRaises(SecurityError):
            self.ca.issue("stranger")

    def test_certificate_from_another_ca_is_not_valid_here(self):
        other = ProjectCA.create("other-project-1")
        other.add_member("priya", "Priya", "builder", self.key.public_key)
        cert = other.issue("priya")
        ok, reason = self.ca.verify(cert)
        self.assertFalse(ok)
        self.assertIn("wasn't signed by this project", reason)

    def test_removing_a_member_revokes_through_the_krl(self):
        self.ca.add_member("priya", "Priya", "builder", self.key.public_key, "laptop")
        cert_path = self.tmp / "c-cert.pub"
        self.ca.issue("priya", out_path=cert_path)
        self.assertFalse(self.ca.is_revoked(cert_path))
        self.ca.remove_member("priya")
        self.assertTrue(self.ca.is_revoked(cert_path))
        self.assertTrue(self.ca.is_revoked(self.key.private_path.with_suffix(".pub")))
        ok, reason = self.ca.verify(cert_path.read_text())
        self.assertFalse(ok)
        self.assertIn("taken away", reason)
        listing = subprocess.run(["ssh-keygen", "-Q", "-l", "-f", str(self.ca.krl_path)], capture_output=True,
                                 text=True).stdout
        self.assertIn("id: priya@laptop", listing)
        with self.assertRaises(SecurityError):
            self.ca.issue("priya")
        # Adding them back lifts it.
        self.ca.add_member("priya", "Priya", "builder", self.key.public_key, "laptop")
        self.assertFalse(self.ca.is_revoked(self.key.private_path.with_suffix(".pub")))
        fresh = self.tmp / "fresh-cert.pub"
        self.ca.issue("priya", out_path=fresh)
        self.assertFalse(self.ca.is_revoked(fresh))

    def test_keyring_protected_ca_signs_through_askpass(self):
        self.fake_keyring()
        ca = ProjectCA.create("keyring-project")
        locked = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(ca.private_path)], capture_output=True,
                                stdin=subprocess.DEVNULL)
        self.assertNotEqual(locked.returncode, 0)
        ca.add_member("priya", "Priya", "builder", self.key.public_key)
        self.assertTrue(ca.verify(ca.issue("priya"))[0])


@unittest.skipUnless(HAVE_SSH_KEYGEN, "ssh-keygen isn't installed")
class ServerSetupTests(PlatformTestCase):
    PLACE = {"id": "dev-1", "name": "Dev box", "role": "development", "kind": "ssh", "host": "203.0.113.7",
             "user": "deploy", "port": 2222}

    def setUp(self):
        super().setUp()
        self.ca = ProjectCA.create(PROJECT_ID)
        self.key = security.device_key()

    def test_instructions_have_the_exact_sshd_lines_and_a_one_time_command(self):
        info = security.server_setup_instructions(self.PLACE, self.ca)
        self.assertEqual(info["sshd_config_lines"], [
            "TrustedUserCAKeys /etc/ssh/playground/trusted_user_ca_keys",
            "RevokedKeys /etc/ssh/playground/revoked.krl",
            "AuthorizedPrincipalsFile /etc/ssh/playground/principals/%u",
        ])
        self.assertTrue(info["one_time_command"].startswith("ssh -t -p 2222 deploy@203.0.113.7 "))
        encoded = info["one_time_command"].split("printf %s ")[1].split(" |")[0]
        self.assertEqual(base64.b64decode(encoded).decode(), info["script"])
        self.assertIn(self.ca.public_key, info["script"])
        self.assertNotIn("PRIVATE KEY", info["script"])
        subprocess.run(["sh", "-n"], input=info["script"], text=True, check=True)
        with self.assertRaises(SecurityError):
            security.server_setup_instructions({**self.PLACE, "user": "root; rm -rf /"}, self.ca)

    def _run_script_in(self, server: Path) -> dict:
        """Run the setup script against a scratch /etc, without root or sshd."""
        info = security.server_setup_instructions(self.PLACE, self.ca)
        script = info["script"].replace("/etc/ssh/playground", str(server))
        script = script.replace('if [ "$(id -u)" != 0 ]; then echo "Please run this with sudo."; exit 1; fi\n', "")
        script = script.split("CONF=/etc/ssh/sshd_config.d")[0]
        subprocess.run(["sh", "-c", script], check=True, capture_output=True, text=True)
        return info

    def test_script_builds_trust_principals_and_a_merged_krl(self):
        server = self.tmp / "server-etc"
        info = self._run_script_in(server)
        self.assertIn(self.ca.public_key, (server / "trusted_user_ca_keys").read_text())
        self.assertEqual((server / "principals" / "deploy").read_text().split(), [info["principal"]])
        # Running it twice doesn't duplicate the principal.
        self._run_script_in(server)
        self.assertEqual((server / "principals" / "deploy").read_text().split(), [info["principal"]])
        # After a member is removed, re-running revokes them on the server too.
        self.ca.add_member("priya", "Priya", "builder", self.key.public_key)
        cert = self.tmp / "p-cert.pub"
        self.ca.issue("priya", out_path=cert)
        q = lambda: subprocess.run(["ssh-keygen", "-Q", "-f", str(server / "revoked.krl"), str(cert)],
                                   capture_output=True).returncode
        self.assertEqual(q(), 0)
        self.ca.remove_member("priya")
        self._run_script_in(server)
        self.assertEqual(q(), 1)

    def test_real_sshd_accepts_a_certificate_then_refuses_after_revocation(self):
        """End to end with an unprivileged sshd on a spare port. Skips where sshd can't run."""
        sshd = next((p for p in ("/usr/sbin/sshd", "/usr/bin/sshd") if os.path.exists(p)), None)
        if not sshd or not shutil.which("ssh"):
            self.skipTest("no sshd/ssh binary")
        user = os.environ.get("USER") or subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        if not security._USER_RE.match(user):
            self.skipTest("this account name can't be a principal file name")
        place = {**self.PLACE, "user": user}
        server = self.tmp / "srv"
        info = security.server_setup_instructions(place, self.ca)
        script = info["script"].replace("/etc/ssh/playground", str(server))
        script = script.replace('if [ "$(id -u)" != 0 ]; then echo "Please run this with sudo."; exit 1; fi\n', "")
        script = script.split("CONF=/etc/ssh/sshd_config.d")[0]
        subprocess.run(["sh", "-c", script], check=True, capture_output=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.tmp / "hostkey")], check=True)
        port = free_port()
        lines = [line.replace("/etc/ssh/playground", str(server)) for line in info["sshd_config_lines"]]
        config = self.tmp / "sshd_config"
        config.write_text("\n".join([
            f"Port {port}", "ListenAddress 127.0.0.1", f"HostKey {self.tmp / 'hostkey'}",
            f"PidFile {self.tmp / 'sshd.pid'}", "AuthorizedKeysFile none", "StrictModes no",
            "PasswordAuthentication no", "KbdInteractiveAuthentication no", "UsePAM no", *lines, ""]))
        proc = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                if proc.poll() is not None:
                    self.skipTest("sshd won't run unprivileged here: " + proc.stdout.read().decode()[-200:])
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                self.skipTest("sshd didn't start")
            self.ca.add_member("me", "Me", "builder", self.key.public_key)
            cert = self.key.certificate_path(PROJECT_ID)
            self.ca.issue("me", out_path=cert)

            def login() -> subprocess.CompletedProcess:
                return subprocess.run(
                    ["ssh", "-F", "/dev/null", "-i", str(self.key.private_path), "-o", f"CertificateFile={cert}",
                     "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                     "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5", "-p", str(port),
                     f"{user}@127.0.0.1", "echo signed-in"], capture_output=True, text=True, timeout=20,
                    stdin=subprocess.DEVNULL)

            first = login()
            if first.returncode != 0 and "signed-in" not in first.stdout:
                log = ""
                try:
                    proc.terminate()
                    log = proc.communicate(timeout=3)[0].decode()[-600:]
                except Exception:  # noqa: BLE001
                    pass
                if "privilege separation" in log.lower() or "setgroups" in log.lower() or "chroot" in log.lower():
                    self.skipTest("this sshd needs root: " + log)
                self.fail(f"certificate login failed: {first.stderr}\n{log}")
            self.assertIn("signed-in", first.stdout)
            self.ca.remove_member("me")
            script = security.server_setup_instructions(place, self.ca)["script"]
            script = script.replace("/etc/ssh/playground", str(server))
            script = script.replace('if [ "$(id -u)" != 0 ]; then echo "Please run this with sudo."; exit 1; fi\n', "")
            subprocess.run(["sh", "-c", script.split("CONF=/etc/ssh/sshd_config.d")[0]], check=True,
                           capture_output=True)
            second = login()
            self.assertNotEqual(second.returncode, 0)
            self.assertNotIn("signed-in", second.stdout)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            proc.stdout.close()


class VaultWithoutCryptoTests(PlatformTestCase):
    def test_unavailable_vault_says_why_and_writes_nothing(self):
        with mock.patch.object(security, "_CRYPTO_ERROR", "ImportError: no cryptography"):
            vault = Vault(self.project_dir, PROJECT_ID)
            status = vault.status()
            self.assertFalse(status["available"])
            self.assertIn("python3-cryptography", status["reason"])
            for call in (lambda: vault.create("nick", "Nick"), lambda: vault.set("API_KEY", "sk-live-123456"),
                         lambda: vault.get("API_KEY"), lambda: vault.names()):
                with self.assertRaises(security.VaultUnavailable):
                    call()
        self.assertFalse((self.project_dir / ".playground" / "vault.json").exists())
        for path in self.all_files(self.project_dir, self.data):
            self.assertNotIn(b"sk-live-123456", path.read_bytes())

    @unittest.skipIf(HAVE_CRYPTO, "cryptography is installed here")
    def test_really_unavailable_on_this_host(self):
        self.assertFalse(security.crypto_available()[0])


@unittest.skipUnless(HAVE_CRYPTO, CRYPTO_SKIP)
class VaultTests(PlatformTestCase):
    SECRETS = {"DATABASE_URL": "postgres://app:Tr0ub4dor-and-3@db.internal/kiln",
               "STRIPE_KEY": "sk_test_51NzQpL2eZvKYlo2C0aBcDeFgHiJ",
               "SMTP_PASSWORD": "correct horse battery staple"}

    def setUp(self):
        super().setUp()
        self.nick = security.VaultIdentity.generate("nick-laptop")
        self.priya = security.VaultIdentity.generate("priya-laptop")
        self.sam = security.VaultIdentity.generate("sam-desktop")
        self.vaults = {name: Vault(self.project_dir, PROJECT_ID, identity=ident, trust_dir=self.tmp / f"trust-{name}")
                       for name, ident in (("nick", self.nick), ("priya", self.priya), ("sam", self.sam))}
        self.vaults["nick"].create("nick", "Nick")
        for name, value in self.SECRETS.items():
            self.vaults["nick"].set(name, value)
        self.vaults["nick"].add_member("priya", "Priya", self.priya.public())
        self.vaults["nick"].add_member("sam", "Sam", self.sam.public())

    def vault_json(self) -> dict:
        return json.loads((self.project_dir / ".playground" / "vault.json").read_text())

    def test_round_trip_for_three_recipients(self):
        for who, vault in self.vaults.items():
            with self.subTest(who=who):
                self.assertEqual(vault.names(), sorted(self.SECRETS))
                for name, value in self.SECRETS.items():
                    self.assertEqual(vault.get(name), value)
        self.assertEqual(self.vaults["priya"].env(["STRIPE_KEY"]), {"STRIPE_KEY": self.SECRETS["STRIPE_KEY"]})
        status = self.vaults["sam"].status()
        self.assertTrue(status["available"] and status["member"])
        self.assertFalse(status["admin"])
        self.assertEqual(status["members"], ["nick", "priya", "sam"])

    def test_each_secret_is_wrapped_once_per_member_with_fresh_keys(self):
        data = self.vault_json()
        for entry in data["secrets"].values():
            self.assertEqual(set(entry["wraps"]), {self.nick.id, self.priya.id, self.sam.id})
            epks = {w["epk"] for w in entry["wraps"].values()}
            self.assertEqual(len(epks), 3)
        nonces = [e["nonce"] for e in data["secrets"].values()]
        self.assertEqual(len(set(nonces)), len(nonces))

    def test_any_member_can_write_and_others_read_it(self):
        self.vaults["priya"].set("NEW_TOKEN", "tok-priya-998877")
        self.assertEqual(self.vaults["sam"].get("NEW_TOKEN"), "tok-priya-998877")
        self.assertEqual(self.vaults["nick"].get("NEW_TOKEN"), "tok-priya-998877")

    def test_removal_rewraps_and_locks_the_member_out(self):
        before = self.vault_json()
        result = self.vaults["nick"].remove_member("sam")
        self.assertEqual(result["to_change"], sorted(self.SECRETS))
        after = self.vault_json()
        for name in self.SECRETS:
            self.assertNotIn(self.sam.id, after["secrets"][name]["wraps"])
            self.assertNotEqual(before["secrets"][name]["ciphertext"], after["secrets"][name]["ciphertext"])
            self.assertGreater(after["secrets"][name]["version"], before["secrets"][name]["version"])
        with self.assertRaises(VaultAccessDenied):
            self.vaults["sam"].get("DATABASE_URL")
        self.assertEqual(self.vaults["priya"].get("DATABASE_URL"), self.SECRETS["DATABASE_URL"])
        # Sam's old wrapped key can't open the new ciphertext.
        old_wrap = before["secrets"]["DATABASE_URL"]
        tampered = copy.deepcopy(after)
        tampered["secrets"]["DATABASE_URL"]["wraps"][self.sam.id] = old_wrap["wraps"][self.sam.id]
        (self.project_dir / ".playground" / "vault.json").write_text(json.dumps(tampered))
        with self.assertRaises(security.SecurityError):
            self.vaults["sam"].get("DATABASE_URL")

    def test_rotate_and_rewrap(self):
        before = self.vault_json()["secrets"]["STRIPE_KEY"]
        self.vaults["priya"].rotate("STRIPE_KEY", "sk_test_rotated_00112233")
        self.assertEqual(self.vaults["sam"].get("STRIPE_KEY"), "sk_test_rotated_00112233")
        self.assertEqual(self.vaults["nick"].rewrap(), sorted(self.SECRETS))
        after = self.vault_json()["secrets"]["STRIPE_KEY"]
        self.assertNotEqual(before["ciphertext"], after["ciphertext"])
        self.assertEqual(self.vaults["nick"].get("STRIPE_KEY"), "sk_test_rotated_00112233")
        self.assertTrue(self.vaults["nick"].delete("SMTP_PASSWORD"))
        self.assertNotIn("SMTP_PASSWORD", self.vaults["sam"].names())

    def test_only_an_admin_can_change_the_roster(self):
        outsider = security.VaultIdentity.generate("outsider")
        with self.assertRaises(VaultAccessDenied):
            self.vaults["priya"].add_member("mallory", "Mallory", outsider.public())
        self.vaults["nick"].add_member("priya", "Priya", self.priya.public(), admin=True)
        self.vaults["priya"].add_member("mallory", "Mallory", outsider.public())
        mallory = Vault(self.project_dir, PROJECT_ID, identity=outsider, trust_dir=self.tmp / "trust-m")
        self.assertEqual(mallory.get("STRIPE_KEY"), self.SECRETS["STRIPE_KEY"])

    def test_an_injected_recipient_is_refused(self):
        attacker = security.VaultIdentity.generate("attacker")
        data = self.vault_json()
        data["roster"][-1]["body"]["members"][attacker.id] = {"member": "mallory", "name": "Mallory",
                                                              **{k: attacker.public()[k] for k in ("x25519", "ed25519")}}
        (self.project_dir / ".playground" / "vault.json").write_text(json.dumps(data))
        with self.assertRaises(VaultTampered):
            self.vaults["nick"].set("ANOTHER", "would-leak-to-attacker")
        self.assertNotIn(attacker.id, json.dumps(self.vault_json()["secrets"]))

    def test_a_roster_entry_signed_by_a_non_admin_is_refused(self):
        attacker = security.VaultIdentity.generate("attacker")
        evil = Vault(self.project_dir, PROJECT_ID, identity=attacker, trust_dir=self.tmp / "trust-a")
        data = self.vault_json()
        members = dict(data["roster"][-1]["body"]["members"])
        members[attacker.id] = {"member": "mallory", "name": "Mallory", "x25519": attacker.public()["x25519"],
                                "ed25519": attacker.public()["ed25519"], "device": "x"}
        body = {"version": len(data["roster"]) + 1, "project_id": PROJECT_ID, "updated": "2026-10-09T00:00:00Z",
                "members": members, "admins": [attacker.id]}
        data["roster"].append({"body": body, "signer": attacker.id,
                               "sig": security._b64(attacker.sign(security._ROSTER_DOMAIN + security._canonical(body)))})
        (self.project_dir / ".playground" / "vault.json").write_text(json.dumps(data))
        with self.assertRaises(VaultTampered):
            self.vaults["priya"].names()
        with self.assertRaises(VaultTampered):
            evil.names()

    def test_rollback_and_rewritten_history_are_refused(self):
        path = self.project_dir / ".playground" / "vault.json"
        old = path.read_text()
        self.vaults["nick"].remove_member("sam")
        self.vaults["priya"].names()           # priya has now seen the newer roster
        path.write_text(old)                   # someone restores the file from before sam was removed
        with self.assertRaisesRegex(VaultTampered, "rolled it back|older"):
            self.vaults["priya"].names()

    def test_swapped_or_forged_secrets_are_refused(self):
        path = self.project_dir / ".playground" / "vault.json"
        data = self.vault_json()
        swapped = copy.deepcopy(data)
        swapped["secrets"]["STRIPE_KEY"], swapped["secrets"]["SMTP_PASSWORD"] = (
            data["secrets"]["SMTP_PASSWORD"], data["secrets"]["STRIPE_KEY"])
        path.write_text(json.dumps(swapped))
        with self.assertRaises(VaultTampered):
            self.vaults["priya"].get("STRIPE_KEY")
        flipped = copy.deepcopy(data)
        ct = bytearray(base64.b64decode(flipped["secrets"]["STRIPE_KEY"]["ciphertext"]))
        ct[0] ^= 1
        flipped["secrets"]["STRIPE_KEY"]["ciphertext"] = base64.b64encode(bytes(ct)).decode()
        path.write_text(json.dumps(flipped))
        with self.assertRaises(VaultTampered):
            self.vaults["priya"].get("STRIPE_KEY")

    def test_no_plaintext_anywhere_on_disk(self):
        self.vaults["priya"].set("LATE_ADDITION", "plaintext-canary-7f3a9c")
        self.vaults["nick"].remove_member("sam")
        self.vaults["nick"].rotate()
        values = list(self.SECRETS.values()) + ["plaintext-canary-7f3a9c"]
        files = self.all_files(self.project_dir, self.data, self.tmp)
        self.assertTrue(files)
        for path in files:
            blob = path.read_bytes()
            for value in values:
                for form in (value.encode(), base64.b64encode(value.encode()), value.encode().hex().encode()):
                    self.assertNotIn(form, blob, f"{value!r} found in {path}")
        self.assertNotIn(b"PRIVATE KEY", (self.project_dir / ".playground" / "vault.json").read_bytes())

    def test_device_identity_on_disk_is_private_and_reloads(self):
        ident = security.VaultIdentity.load_or_create()
        path = security.VaultIdentity.path()
        self.assertEqual(mode(path), 0o600)
        again = security.VaultIdentity.load_or_create()
        self.assertEqual(ident.id, again.id)
        self.assertFalse(path.is_relative_to(self.project_dir))

    def test_device_identity_encrypted_with_keyring(self):
        self.fake_keyring()
        ident = security.VaultIdentity.load_or_create()
        self.assertEqual(ident.protection, "keyring")
        self.assertIn(b"ENCRYPTED PRIVATE KEY", security.VaultIdentity.path().read_bytes())
        self.assertEqual(security.VaultIdentity.load_or_create().id, ident.id)

    def test_save_refuses_to_write_plaintext(self):
        vault = self.vaults["nick"]
        data = self.vault_json()
        data["secrets"]["LEAK"] = {"oops": "hunter2-hunter2"}
        with self.assertRaises(SecurityError):
            vault._save(data, [b"hunter2-hunter2"])
        self.assertNotIn(b"hunter2-hunter2", (self.project_dir / ".playground" / "vault.json").read_bytes())


@unittest.skipUnless(HAVE_SSH_KEYGEN and HAVE_CRYPTO, CRYPTO_SKIP)
class MembershipTests(PlatformTestCase):
    def test_grant_and_revoke_cover_servers_and_vault(self):
        ProjectCA.create(PROJECT_ID)
        owner = security.VaultIdentity.load_or_create()
        Vault(self.project_dir, PROJECT_ID, identity=owner).create("nick", "Nick")
        Vault(self.project_dir, PROJECT_ID, identity=owner).set("API_KEY", "sk-membership-445566")
        key = security.device_key()
        priya = security.VaultIdentity.generate("priya")
        granted = security.grant_member(self.project_dir, PROJECT_ID, "priya", "Priya", "builder", key.public_key,
                                        "laptop", vault_public=priya.public())
        self.assertTrue(granted["vault"])
        pv = Vault(self.project_dir, PROJECT_ID, identity=priya, trust_dir=self.tmp / "trust-p")
        self.assertEqual(pv.get("API_KEY"), "sk-membership-445566")
        revoked = security.revoke_member(self.project_dir, PROJECT_ID, "priya")
        self.assertEqual(revoked["to_change"], ["API_KEY"])
        self.assertEqual(revoked["revoked_keys"], 1)
        with self.assertRaises(VaultAccessDenied):
            pv.get("API_KEY")
        self.assertTrue(ProjectCA.open(PROJECT_ID).is_revoked(key.private_path.with_suffix(".pub")))


if __name__ == "__main__":
    unittest.main()
