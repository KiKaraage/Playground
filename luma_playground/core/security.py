# SPDX-License-Identifier: Apache-2.0
"""Keys, server certificates and the project vault.

This is the part of Playground that has to be "rock solid, super safe". It has
three pieces, and none of them ever passes a private key around.

Device key
    One ed25519 SSH keypair per device, made by ``ssh-keygen`` and kept in
    ``<data>/keys/`` (0700 directory, 0600 private key). When the Secret
    Service is reachable (``secret-tool``), the key is encrypted with a random
    passphrase that lives only in the keyring, and OpenSSH reads it through a
    tiny ``SSH_ASKPASS`` helper that calls ``secret-tool lookup``; the
    passphrase never appears on a command line. Without a keyring the key is
    protected by file permissions alone, the same as a stock ``~/.ssh`` key,
    and ``protection`` says so. The key is labelled with the person's Luma
    Connect id. Nothing in this module returns or copies the private half.

Project SSH certificate authority
    Each project gets its own CA on the owner's device
    (``<data>/ca/<project-id>/``), never in the project folder. The CA signs
    short-lived user certificates (12 hours by default, 24 at most) for
    members' device public keys. Principals are project scoped
    (``pg-<id8>-development``…), so a certificate from one project is useless
    on another project's servers, and production access is a separate
    principal that only owners get. Removing a member puts every one of their
    device keys and certificate ids into a KRL; servers load it through
    ``RevokedKeys``. Even without a KRL push, their access ends when the last
    certificate expires.

Vault
    Secrets end-to-end encrypted to members' devices with python3-cryptography:
    X25519 + HKDF-SHA256 + ChaCha20-Poly1305 envelope encryption, one fresh
    content key per secret version, wrapped separately for every recipient
    device with an ephemeral X25519 key (the same composition as age and
    HPKE base mode). The file ``.playground/vault.json`` holds only
    ciphertext, wrapped keys, public keys and signatures, so it is safe to
    sync through the Hub. The device vault keys are separate from the SSH key.

Threat notes
    * A synced vault file can be edited by anyone who can write the project
      folder or the relay. So the member roster is an append-only log in which
      every entry is signed (Ed25519) by an admin of the previous entry, and
      each device pins the first entry it trusted plus the newest version it
      has seen. An injected recipient, a forged roster, a rolled-back roster
      or a forked history are refused. Every secret is signed by its writer,
      who must be in the current roster, and its ciphertext is bound to its
      name and version through AEAD associated data.
    * Removing a member re-encrypts every secret under fresh content keys, but
      they may already have read the values. ``remove_member`` returns the
      names that should be changed where they come from.
    * Secret *names* are visible in the file (so they can be listed without
      unlocking). Values never are.
    * Python cannot reliably wipe memory; plaintext lives only in local
      variables for as long as a call needs it.
    * The CA private key and device keys are as safe as the owner's account
      on this device. Anyone running code as this user can use them; that is
      the same boundary as ``~/.ssh``.
    * Shell-facing values (hosts, users, ids, key text) are validated with
      strict patterns before they reach ``ssh-keygen`` or a setup script.
"""
from __future__ import annotations

import base64
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
from typing import Any, Iterable

try:
    from . import paths as _paths
except Exception:  # pragma: no cover - paths.py is part of core
    _paths = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

SERVICE = "org.projectluma.Playground"
CERT_DEFAULT_VALIDITY_HOURS = 12
CERT_MAX_VALIDITY_HOURS = 24
VAULT_FORMAT = "playground-vault/1"
VAULT_FILE = "vault.json"

# Member role → which place roles their certificate may log in to.
ROLE_ACCESS = {
    "owner": ("development", "preview", "production"),
    "builder": ("development", "preview"),
    "reviewer": (),
    "viewer": (),
}
PLACE_ROLES = ("development", "preview", "production")

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_PUBKEY_RE = re.compile(r"^(ssh-ed25519|sk-ssh-ed25519@openssh\.com) ([A-Za-z0-9+/]+={0,2})(?: .*)?$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")


# ───────────────────────────── errors ─────────────────────────────

class SecurityError(Exception):
    """Something went wrong; ``str(error)`` is plain words a person can read."""


class VaultUnavailable(SecurityError):
    pass


class VaultAccessDenied(SecurityError):
    pass


class VaultTampered(SecurityError):
    pass


# ───────────────────────────── small helpers ─────────────────────────────

def data_dir() -> Path:
    if _paths is not None:
        return _paths.data_dir()
    override = os.environ.get("PLAYGROUND_DATA_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "playground"


def keys_dir() -> Path:
    return data_dir() / "keys"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def require_safe_id(value: object, what: str = "id") -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise SecurityError(f"That {what} has characters Playground can't use safely.")
    return value


def private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and make sure it is 0700, ours, and not a symlink."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(path)
    if os.path.islink(path):
        raise SecurityError(f"{path} is a link; Playground won't keep keys there.")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise SecurityError(f"{path} belongs to someone else; Playground won't keep keys there.")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def write_private(path: Path, data: bytes | str) -> None:
    """Atomically write a 0600 file. The temp file is 0600 from the moment it exists."""
    if isinstance(data, str):
        data = data.encode()
    path = Path(path)
    private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)  # mkstemp creates 0600
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_public(path: Path, data: bytes | str, mode: int = 0o644) -> None:
    """Atomically write a file that holds nothing secret."""
    if isinstance(data, str):
        data = data.encode()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def check_private_file(path: Path) -> Path:
    """Refuse a key file that is a link or someone else's; tighten loose modes."""
    st = os.lstat(path)
    if os.path.islink(path):
        raise SecurityError(f"{path} is a link; Playground won't use it as a key.")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise SecurityError(f"{path} belongs to someone else.")
    if st.st_mode & 0o077:
        log.warning("tightening permissions on %s", path)
        os.chmod(path, 0o600)
    return path


def _ssh_keygen() -> str:
    tool = shutil.which("ssh-keygen")
    if not tool:
        raise SecurityError("This computer doesn't have OpenSSH's ssh-keygen. Install openssh to set up keys.")
    return tool


def _run(argv: list[str], *, input: str | None = None, env: dict | None = None,
         timeout: float = 30, check: bool = True) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(argv, input=input, capture_output=True, text=True, env=full_env,
                              timeout=timeout, stdin=None if input is not None else subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise SecurityError(f"Couldn't run {Path(argv[0]).name}: it isn't installed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise SecurityError(f"{Path(argv[0]).name} took too long and was stopped.") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["no detail"]
        raise SecurityError(f"{Path(argv[0]).name} failed: {detail[0]}")
    return proc


def normalize_public_key(text: str) -> str:
    """``"ssh-ed25519 AAAA…"`` with the comment dropped, or SecurityError.

    Only ed25519 (and its security-key form) is accepted. The comment is
    dropped because it is free text that would otherwise reach spec files.
    """
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    match = _PUBKEY_RE.match(line)
    if not match:
        raise SecurityError("That isn't an ed25519 public key Playground can use.")
    kind, blob = match.group(1), match.group(2)
    try:
        raw = base64.b64decode(blob, validate=True)
    except Exception as exc:
        raise SecurityError("That public key is damaged.") from exc
    # The blob starts with the length-prefixed key type; check it matches.
    if len(raw) < 4 or raw[4:4 + int.from_bytes(raw[:4], "big")].decode(errors="replace") != kind:
        raise SecurityError("That public key is damaged.")
    return f"{kind} {blob}"


def fingerprint(public_key: str) -> str:
    """OpenSSH-style ``SHA256:…`` fingerprint of a public key line."""
    blob = base64.b64decode(normalize_public_key(public_key).split()[1])
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


def same_secret(a: str | bytes, b: str | bytes) -> bool:
    """Constant-time equality for fingerprints, ids and tokens."""
    if isinstance(a, str):
        a = a.encode()
    if isinstance(b, str):
        b = b.encode()
    return hmac.compare_digest(a, b)


# ───────────────────────────── keyring ─────────────────────────────

# Whether each secret-tool answers at all, found once per process. In a session with no Secret Service
# (a headless VM, a bare SSH login) every secret-tool call blocks until its timeout, which made a first
# share take minutes; one short probe settles it for the session.
_KEYRING_ANSWERS: dict[str, bool] = {}
_KEYRING_LOCK = threading.Lock()
KEYRING_PROBE_S = 2.0


class Keyring:
    """Passphrases kept in the Secret Service (libsecret) through ``secret-tool``.

    Secrets go to ``secret-tool`` on stdin and come back on stdout; they never
    appear in argv or the environment. ``askpass_script`` writes a helper that
    OpenSSH runs (``SSH_ASKPASS_REQUIRE=force``); the helper holds only the
    lookup attributes, not the passphrase.
    """

    def __init__(self, tool: str | None = None) -> None:
        found = tool or shutil.which("secret-tool")
        self.tool = str(Path(found).resolve()) if found else None

    def available(self) -> bool:
        """Is there a keyring that answers? Probed once per process (about 2 s at worst), then cached."""
        if not self.tool:
            return False
        with _KEYRING_LOCK:
            if self.tool not in _KEYRING_ANSWERS:
                _KEYRING_ANSWERS[self.tool] = self._probe()
            return _KEYRING_ANSWERS[self.tool]

    def _probe(self) -> bool:
        try:
            proc = subprocess.run([self.tool, "search", "service", SERVICE, "kind", "probe"], capture_output=True,
                                  text=True, timeout=KEYRING_PROBE_S, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return False
        # Nothing found is fine (exit 1, quietly); a missing Secret Service says so on stderr.
        return proc.returncode in (0, 1) and not proc.stderr.strip()

    def _gone(self) -> None:
        """A call timed out: treat the keyring as unavailable for the rest of the session."""
        with _KEYRING_LOCK:
            if self.tool:
                _KEYRING_ANSWERS[self.tool] = False

    def _attrs(self, kind: str, ident: str) -> list[str]:
        return ["service", SERVICE, "kind", require_safe_id(kind, "key kind"), "id", require_safe_id(ident, "key id")]

    def store(self, kind: str, ident: str, secret: str, label: str) -> bool:
        if not self.available():
            return False
        try:
            # A locked keyring may ask the person to unlock it, so this one waits longer than the probe.
            proc = subprocess.run([self.tool, "store", "--label", label, *self._attrs(kind, ident)],
                                  input=secret, capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            self._gone()
            return False
        except OSError:
            return False
        return proc.returncode == 0

    def lookup(self, kind: str, ident: str) -> str | None:
        if not self.available():
            return None
        try:
            proc = subprocess.run([self.tool, "lookup", *self._attrs(kind, ident)],
                                  capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            self._gone()
            return None
        except OSError:
            return None
        if proc.returncode != 0 or not proc.stdout:
            return None
        return proc.stdout.rstrip("\n")

    def clear(self, kind: str, ident: str) -> None:
        if self.available():
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run([self.tool, "clear", *self._attrs(kind, ident)], capture_output=True,
                               timeout=15, stdin=subprocess.DEVNULL)

    def askpass_script(self, kind: str, ident: str, directory: Path) -> Path:
        if not self.tool:
            raise SecurityError("There's no keyring on this computer.")
        args = " ".join(shlex.quote(a) for a in [self.tool, "lookup", *self._attrs(kind, ident)])
        path = askpass_path(directory, kind, ident)
        write_private(path, f"#!/bin/sh\n# Playground: reads a key passphrase from the keyring.\nexec {args}\n")
        os.chmod(path, 0o700)
        return path

    def protect_new_key(self, kind: str, ident: str, label: str, directory: Path) -> dict[str, str] | None:
        """Make a fresh passphrase, keep it in the keyring, and return the askpass env.

        Returns None when the keyring can't hold it (no secret-tool, locked or
        no Secret Service), so the caller falls back to file permissions.
        """
        if not self.available():
            return None
        passphrase = secrets.token_urlsafe(32)
        if not self.store(kind, ident, passphrase, label):
            return None
        back = self.lookup(kind, ident)
        if back is None or not same_secret(back, passphrase):
            self.clear(kind, ident)
            return None
        script = self.askpass_script(kind, ident, directory)
        return askpass_env(script)


def askpass_path(directory: Path, kind: str, ident: str) -> Path:
    return Path(directory) / f"askpass-{kind}-{ident}.sh"


def askpass_env(script: Path) -> dict[str, str]:
    return {"SSH_ASKPASS": str(script), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": os.environ.get("DISPLAY", ":0")}


# ───────────────────────────── device key ─────────────────────────────

@dataclass
class DeviceKey:
    """This device's SSH identity. Holds paths and public material only."""
    private_path: Path
    public_key: str
    fingerprint: str
    device_name: str
    connect_id: str = ""
    protection: str = "file-permissions"   # or "keyring"
    created: str = ""

    @property
    def dir(self) -> Path:
        return self.private_path.parent

    def ssh_env(self) -> dict[str, str]:
        """Environment that lets ssh/ssh-keygen unlock the key (empty if not encrypted)."""
        if self.protection != "keyring":
            return {}
        return askpass_env(askpass_path(self.dir, "device-ssh", "device"))

    def certificate_path(self, project_id: str) -> Path:
        return self.dir / "certs" / f"{require_safe_id(project_id, 'project id')}-cert.pub"

    def public_dict(self) -> dict:
        """What may be shown or shared: never a path to the private key."""
        return {"public_key": self.public_key, "fingerprint": self.fingerprint,
                "device": self.device_name, "connect_id": self.connect_id, "protection": self.protection}


def _device_name() -> str:
    name = socket.gethostname().split(".")[0] or "device"
    name = re.sub(r"[^A-Za-z0-9-]+", "-", name).strip("-") or "device"
    return name[:40]


def device_key(create: bool = True, connect_id: str | None = None,
               keyring: Keyring | None = None) -> DeviceKey | None:
    """This device's key, made on first use. ``connect_id`` (re)labels it.

    The private key stays in ``<data>/keys/device_ed25519``; callers get its
    path for ``ssh -i`` and nothing else.
    """
    directory = private_dir(keys_dir())
    private = directory / "device_ed25519"
    public = directory / "device_ed25519.pub"
    meta_path = directory / "device.json"
    if not private.exists():
        if not create:
            return None
        keyring = keyring or Keyring()
        name = _device_name()
        env = keyring.protect_new_key("device-ssh", "device", f"Playground device key ({name})", directory)
        argv = [_ssh_keygen(), "-q", "-t", "ed25519", "-f", str(private), "-C", f"playground@{name}"]
        if env is None:
            argv[2:2] = ["-N", ""]          # no keyring: unencrypted, 0600, like a stock ~/.ssh key
        _run(argv, env=env, timeout=60)
        os.chmod(private, 0o600)
        meta = {"device": name, "connect_id": connect_id or "", "created": _iso(),
                "protection": "keyring" if env else "file-permissions"}
        write_private(meta_path, json.dumps(meta, indent=2))
    check_private_file(private)
    meta = _read_json(meta_path) or {}
    if connect_id is not None and meta.get("connect_id") != connect_id:
        meta["connect_id"] = connect_id
        write_private(meta_path, json.dumps(meta, indent=2))
    pub = normalize_public_key(public.read_text())
    return DeviceKey(private_path=private, public_key=pub, fingerprint=fingerprint(pub),
                     device_name=str(meta.get("device") or _device_name()),
                     connect_id=str(meta.get("connect_id") or ""),
                     protection=str(meta.get("protection") or "file-permissions"),
                     created=str(meta.get("created") or ""))


def bind_identity(connect_id: str) -> DeviceKey:
    """Label this device's key with the person's Luma Connect id."""
    key = device_key(create=True, connect_id=connect_id)
    assert key is not None
    return key


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


# ───────────────────────────── certificates ─────────────────────────────

@dataclass
class Certificate:
    text: str                 # the "-cert.pub" line; public, but useless without the private key
    serial: int
    key_id: str
    principals: list[str]
    valid_from: str
    valid_to: str
    signing_ca: str           # SHA256 fingerprint
    public_key_fingerprint: str = ""
    extensions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"serial": self.serial, "key_id": self.key_id, "principals": list(self.principals),
                "valid_from": self.valid_from, "valid_to": self.valid_to, "signing_ca": self.signing_ca,
                "extensions": list(self.extensions)}


def read_certificate(path_or_text: Path | str) -> Certificate:
    """Parse a certificate with ``ssh-keygen -L`` (we never parse the binary ourselves)."""
    if isinstance(path_or_text, Path) or (isinstance(path_or_text, str) and "\n" not in path_or_text.strip()
                                          and os.path.exists(path_or_text)):
        path = Path(path_or_text)
        text = path.read_text().strip()
        out = _run([_ssh_keygen(), "-L", "-f", str(path)]).stdout
    else:
        text = str(path_or_text).strip()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c-cert.pub"
            p.write_text(text + "\n")
            out = _run([_ssh_keygen(), "-L", "-f", str(p)]).stdout
    return _parse_cert_listing(out, text)


def _parse_cert_listing(out: str, text: str) -> Certificate:
    fields: dict[str, Any] = {"principals": [], "extensions": []}
    section = None
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        indent = len(raw) - len(raw.lstrip())
        if section and indent >= 16 and ":" not in line.split(" ")[0][:-1]:
            fields[section].append(line)
            continue
        section = None
        if line.startswith("Public key:"):
            fields["pk"] = line.split()[-1]
        elif line.startswith("Signing CA:"):
            fields["ca"] = next((w for w in line.split() if w.startswith("SHA256:")), "")
        elif line.startswith("Key ID:"):
            fields["key_id"] = line.split(":", 1)[1].strip().strip('"')
        elif line.startswith("Serial:"):
            fields["serial"] = int(line.split(":", 1)[1].strip())
        elif line.startswith("Valid:"):
            m = re.search(r"from (\S+) to (\S+)", line)
            if m:
                fields["from"], fields["to"] = m.group(1), m.group(2)
            else:
                fields["from"], fields["to"] = "always", "forever"
        elif line.startswith("Principals:"):
            section = "principals"
            rest = line.split(":", 1)[1].strip()
            if rest and rest != "(none)":
                fields["principals"].append(rest)
        elif line.startswith("Extensions:"):
            section = "extensions"
            rest = line.split(":", 1)[1].strip()
            if rest and rest != "(none)":
                fields["extensions"].append(rest)
        elif line.startswith("Critical Options:"):
            section = None
    return Certificate(text=text, serial=int(fields.get("serial", 0)), key_id=str(fields.get("key_id", "")),
                       principals=list(fields["principals"]), valid_from=str(fields.get("from", "")),
                       valid_to=str(fields.get("to", "")), signing_ca=str(fields.get("ca", "")),
                       public_key_fingerprint=str(fields.get("pk", "")), extensions=list(fields["extensions"]))


def _parse_local_time(value: str) -> datetime | None:
    # ssh-keygen -L prints validity in local time without a zone.
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None


# ───────────────────────────── project CA ─────────────────────────────

def principal_for(project_id: str, place_role: str) -> str:
    if place_role not in PLACE_ROLES:
        raise SecurityError(f"Unknown place role: {place_role}")
    return f"{principal_prefix(project_id)}-{place_role}"


def principal_prefix(project_id: str) -> str:
    short = re.sub(r"[^a-z0-9]", "", require_safe_id(project_id, "project id").lower())[:32] or "project"
    return f"pg-{short}"


def sync_principal(project_id: str) -> str:
    """The principal of a sync pass (shared projects): git for this one project, through its gate."""
    return f"{principal_prefix(project_id)}-sync"


_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{20,}$")
_GATE_RE = re.compile(r"^/[A-Za-z0-9_./+@-]+$")


def shell_options(place_roles: Iterable[str]) -> list[str]:
    """``ssh-keygen -O`` options for a shell certificate: a pty; port forwarding unless production-only."""
    options = ["-O", "clear", "-O", "permit-pty"]
    if any(r != "production" for r in place_roles):
        options += ["-O", "permit-port-forwarding"]
    return options


def sync_options(gate: str, member_id: str, fingerprint_text: str) -> list[str]:
    """``ssh-keygen -O`` options for a sync pass: nothing allowed but its forced command.

    The command is written without quotes (every part is checked against a strict pattern first), so the
    server's own sign-in check can repeat it word for word.
    """
    if not _GATE_RE.match(gate or ""):
        raise SecurityError("The project home's folder has characters Playground can't use safely.")
    require_safe_id(member_id, "member id")
    if not _FINGERPRINT_RE.match(fingerprint_text or ""):
        raise SecurityError("That device's fingerprint looks wrong.")
    return ["-O", "clear", "-O", f"force-command={gate} {member_id} {fingerprint_text}"]


def sign_certificate(signer_private: Path, signer_env: dict[str, str] | None, public_key: str, key_id: str,
                     principals: Iterable[str], minutes: int, serial: int, options: Iterable[str],
                     work_dir: Path | None = None) -> Certificate:
    """Sign a user certificate for ``public_key`` with the key at ``signer_private``.

    The signer is the project's CA, or (for a shared project) another owner computer the project trusts.
    Starts five minutes back for clock drift. Returns the certificate; nothing private leaves the signer.
    """
    pub = normalize_public_key(public_key)
    names = [p for p in principals if re.match(r"^[a-z0-9-]{1,80}$", p)]
    if not names:
        raise SecurityError("A certificate needs at least one principal.")
    if not re.match(r"^[A-Za-z0-9._@:-]{1,200}$", key_id):
        raise SecurityError("That certificate id has characters Playground can't use safely.")
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        os.chmod(tmp, 0o700)
        pub_path = Path(tmp) / "member.pub"
        pub_path.write_text(pub + "\n")
        _run([_ssh_keygen(), "-q", "-s", str(signer_private), "-I", key_id, "-n", ",".join(names),
              "-V", f"-5m:+{max(1, int(minutes))}m", "-z", str(int(serial)), *options, str(pub_path)],
             env=signer_env or {}, timeout=60)
        return read_certificate(Path(tmp) / "member-cert.pub")


class ProjectCA:
    """A project's SSH certificate authority, on the owner's device only.

    Files (``<data>/ca/<project-id>/``, 0700):
        ca_ed25519, ca_ed25519.pub   the CA keypair (private 0600, keyring-encrypted when possible)
        state.json                   members, their device public keys, issued serials, revocations
        revoked.spec, revoked.krl    the KRL source and the KRL itself (both public)
    """

    def __init__(self, project_id: str, directory: Path) -> None:
        self.project_id = require_safe_id(project_id, "project id")
        self.dir = directory
        self.private_path = directory / "ca_ed25519"
        self.public_path = directory / "ca_ed25519.pub"
        self.state_path = directory / "state.json"
        self.krl_path = directory / "revoked.krl"
        self.spec_path = directory / "revoked.spec"
        self._state: dict = _read_json(self.state_path) or {}

    # ── lifecycle ──

    @staticmethod
    def directory_for(project_id: str) -> Path:
        return data_dir() / "ca" / require_safe_id(project_id, "project id")

    @classmethod
    def exists(cls, project_id: str) -> bool:
        return (cls.directory_for(project_id) / "ca_ed25519").exists()

    @classmethod
    def create(cls, project_id: str, keyring: Keyring | None = None) -> "ProjectCA":
        directory = cls.directory_for(project_id)
        if (directory / "ca_ed25519").exists():
            raise SecurityError("This project already has a certificate authority on this device.")
        private_dir(directory.parent)
        private_dir(directory)
        keyring = keyring or Keyring()
        env = keyring.protect_new_key("project-ca", project_id, f"Playground project CA ({project_id})", directory)
        argv = [_ssh_keygen(), "-q", "-t", "ed25519", "-f", str(directory / "ca_ed25519"),
                "-C", f"playground-ca:{project_id}"]
        if env is None:
            argv[2:2] = ["-N", ""]
        _run(argv, env=env, timeout=60)
        os.chmod(directory / "ca_ed25519", 0o600)
        ca = cls(project_id, directory)
        ca._state = {"project_id": project_id, "created": _iso(), "next_serial": 1,
                     "protection": "keyring" if env else "file-permissions",
                     "members": {}, "revoked_keys": [], "revoked_ids": [], "issued": []}
        ca._save()
        ca.rebuild_krl()
        return ca

    @classmethod
    def load(cls, project_id: str) -> "ProjectCA | None":
        directory = cls.directory_for(project_id)
        if not (directory / "ca_ed25519").exists():
            return None
        private_dir(directory)
        check_private_file(directory / "ca_ed25519")
        return cls(project_id, directory)

    @classmethod
    def open(cls, project_id: str, create: bool = False) -> "ProjectCA":
        ca = cls.load(project_id)
        if ca is None:
            if not create:
                raise SecurityError("Only the project owner's computer can do this.")
            ca = cls.create(project_id)
        return ca

    def _save(self) -> None:
        write_private(self.state_path, json.dumps(self._state, indent=2, sort_keys=True))

    def _env(self) -> dict[str, str]:
        if self._state.get("protection") == "keyring":
            return askpass_env(askpass_path(self.dir, "project-ca", self.project_id))
        return {}

    # ── public material ──

    @property
    def public_key(self) -> str:
        return normalize_public_key(self.public_path.read_text())

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public_key)

    def members(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._state.get("members", {}).items()}

    # ── membership ──

    def add_member(self, member_id: str, name: str, role: str, device_public_key: str,
                   device_name: str = "device") -> dict:
        """Grant access: record the member's device key. Re-adding lifts an old revocation."""
        require_safe_id(member_id, "member id")
        if role not in ROLE_ACCESS:
            raise SecurityError(f"Unknown role: {role}")
        pub = normalize_public_key(device_public_key)
        device_name = re.sub(r"[^A-Za-z0-9-]+", "-", device_name or "device").strip("-")[:40] or "device"
        members = self._state.setdefault("members", {})
        entry = members.setdefault(member_id, {"name": name, "role": role, "keys": [], "added": _iso()})
        entry.update({"name": name, "role": role, "removed": False})
        if not any(same_secret(k["public_key"], pub) for k in entry["keys"]):
            entry["keys"].append({"public_key": pub, "fingerprint": fingerprint(pub), "device": device_name})
        self._state["revoked_keys"] = [k for k in self._state.get("revoked_keys", []) if not same_secret(k, pub)]
        self._state["revoked_ids"] = [i for i in self._state.get("revoked_ids", [])
                                      if not i.startswith(f"{member_id}@")]
        self._save()
        self.rebuild_krl()
        return {"member": member_id, "role": role, "principals": self.principals_for_role(role)}

    def remove_member(self, member_id: str) -> dict:
        """Revoke: every device key and every certificate id of this member goes into the KRL."""
        members = self._state.get("members", {})
        entry = members.get(member_id)
        if not entry:
            return {"member": member_id, "revoked_keys": 0}
        revoked = self._state.setdefault("revoked_keys", [])
        for key in entry.get("keys", []):
            if not any(same_secret(k, key["public_key"]) for k in revoked):
                revoked.append(key["public_key"])
        ids = self._state.setdefault("revoked_ids", [])
        for issued in self._state.get("issued", []):
            if issued.get("member") == member_id and issued["key_id"] not in ids:
                ids.append(issued["key_id"])
        entry["removed"] = True
        entry["removed_at"] = _iso()
        self._save()
        self.rebuild_krl()
        return {"member": member_id, "revoked_keys": len(entry.get("keys", []))}

    def principals_for_role(self, role: str) -> list[str]:
        return [principal_for(self.project_id, r) for r in ROLE_ACCESS.get(role, ())]

    # ── issuing ──

    def issue(self, member_id: str, device_public_key: str | None = None,
              place_roles: Iterable[str] | None = None, hours: float = CERT_DEFAULT_VALIDITY_HOURS,
              out_path: Path | None = None) -> Certificate:
        """Sign a short-lived certificate for one of a member's registered device keys.

        ``place_roles`` narrows the certificate to some of the member's place
        roles. Production-only certificates don't allow port forwarding.
        """
        entry = self._state.get("members", {}).get(member_id)
        if not entry or entry.get("removed"):
            raise SecurityError("That person isn't a member of this project.")
        allowed = list(ROLE_ACCESS.get(entry["role"], ()))
        roles = [r for r in (place_roles or allowed)]
        if not allowed:
            raise SecurityError(f"{entry['name']} is a {entry['role']}, and {entry['role']}s don't get server access.")
        denied = [r for r in roles if r not in allowed]
        if denied:
            raise SecurityError(f"{entry['name']} can't reach the {denied[0]} servers.")
        keys = entry.get("keys", [])
        if device_public_key is not None:
            pub = normalize_public_key(device_public_key)
            key = next((k for k in keys if same_secret(k["public_key"], pub)), None)
            if key is None:
                raise SecurityError("That device isn't registered for this person.")
        elif keys:
            key = keys[-1]
        else:
            raise SecurityError("This person has no device registered yet.")
        if any(same_secret(k, key["public_key"]) for k in self._state.get("revoked_keys", [])):
            raise SecurityError("That device's access was taken away.")
        hours = float(hours)
        if not 0 < hours <= CERT_MAX_VALIDITY_HOURS:
            raise SecurityError(f"Certificates last at most {CERT_MAX_VALIDITY_HOURS} hours.")
        serial = int(self._state.get("next_serial", 1))
        key_id = f"{member_id}@{key['device']}"
        cert = sign_certificate(self.private_path, self._env(), key["public_key"], key_id,
                                [principal_for(self.project_id, r) for r in roles], max(1, int(round(hours * 60))),
                                serial, shell_options(roles), work_dir=self.dir)
        self._state["next_serial"] = serial + 1
        self._record_issued(serial, key_id, member_id, key["fingerprint"], cert)
        if out_path is not None:
            write_public(Path(out_path), cert.text + "\n")
        return cert

    def issue_sync(self, member_id: str, device_public_key: str, gate: str, days: float = 7) -> Certificate:
        """A sync-only certificate: principal ``pg-<project>-sync``, no pty or forwarding, and a forced command
        (``<gate> <member> <device fingerprint>``) that can only run git for this one project."""
        entry = self._state.get("members", {}).get(member_id)
        if not entry or entry.get("removed"):
            raise SecurityError("That person isn't a member of this project.")
        pub = normalize_public_key(device_public_key)
        key = next((k for k in entry.get("keys", []) if same_secret(k["public_key"], pub)), None)
        if key is None:
            raise SecurityError("That device isn't registered for this person.")
        if any(same_secret(k, pub) for k in self._state.get("revoked_keys", [])):
            raise SecurityError("That device's access was taken away.")
        serial = max(int(self._state.get("next_sync_serial", 1_000_000)), 1_000_000)
        key_id = f"{member_id}@{key['device']}:sync"
        cert = sign_certificate(self.private_path, self._env(), pub, key_id, [sync_principal(self.project_id)],
                                max(1, int(float(days) * 1440)), serial,
                                sync_options(gate, member_id, key["fingerprint"]), work_dir=self.dir)
        self._state["next_sync_serial"] = serial + 1
        self._record_issued(serial, key_id, member_id, key["fingerprint"], cert)
        return cert

    def _record_issued(self, serial: int, key_id: str, member_id: str, fp: str, cert: Certificate) -> None:
        self._state.setdefault("issued", []).append({"serial": serial, "key_id": key_id, "member": member_id,
                                                    "fingerprint": fp, "issued": _iso(), "valid_to": cert.valid_to})
        self._state["issued"] = self._state["issued"][-500:]
        self._save()

    def revoke_key(self, member_id: str, device_public_key: str) -> bool:
        """Take one of a member's computers off (a lost laptop): its key goes into the KRL; the member stays."""
        pub = normalize_public_key(device_public_key)
        entry = self._state.get("members", {}).get(member_id)
        if not entry or not any(same_secret(k["public_key"], pub) for k in entry.get("keys", [])):
            return False
        revoked = self._state.setdefault("revoked_keys", [])
        if any(same_secret(k, pub) for k in revoked):
            return False
        revoked.append(pub)
        self._save()
        self.rebuild_krl()
        return True

    # ── revocation ──

    def revocation_spec(self) -> str:
        """The KRL source: plain text with public keys and certificate ids. No secrets."""
        lines = [f"# Playground revocations for project {self.project_id}"]
        lines += [f"key: {k}" for k in self._state.get("revoked_keys", [])]
        lines += [f"id: {i}" for i in self._state.get("revoked_ids", [])]
        return "\n".join(lines) + "\n"

    def rebuild_krl(self) -> Path:
        spec = self.revocation_spec()
        write_public(self.spec_path, spec)
        tmp = self.dir / "revoked.krl.new"
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        _run([_ssh_keygen(), "-k", "-s", str(self.public_path), "-f", str(tmp), str(self.spec_path)])
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.krl_path)
        return self.krl_path

    def is_revoked(self, path: Path | str) -> bool:
        """``ssh-keygen -Q``: is this key or certificate in the KRL?"""
        proc = _run([_ssh_keygen(), "-Q", "-f", str(self.krl_path), str(path)], check=False)
        if proc.returncode not in (0, 1):
            raise SecurityError("Couldn't read the revocation list.")
        return proc.returncode == 1

    def verify(self, cert: Certificate | str | Path, now: datetime | None = None) -> tuple[bool, str]:
        """Was this signed by this CA, is it in date, and not revoked? Plain-words reason."""
        if not isinstance(cert, Certificate):
            cert = read_certificate(cert)
        if not same_secret(cert.signing_ca, self.fingerprint):
            return False, "This certificate wasn't signed by this project."
        now = now or _now()
        start, end = _parse_local_time(cert.valid_from), _parse_local_time(cert.valid_to)
        if start and now < start:
            return False, "This certificate isn't valid yet."
        if end and now > end:
            return False, "This certificate has expired."
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c-cert.pub"
            p.write_text(cert.text + "\n")
            if self.is_revoked(p):
                return False, "This certificate's access was taken away."
        return True, "Valid."

    # ── servers ──

    def server_setup_instructions(self, place: dict) -> dict:
        return server_setup_instructions(place, self)


SERVER_DIR = "/etc/ssh/playground"
SSHD_LINES = (
    f"TrustedUserCAKeys {SERVER_DIR}/trusted_user_ca_keys",
    f"RevokedKeys {SERVER_DIR}/revoked.krl",
    f"AuthorizedPrincipalsFile {SERVER_DIR}/principals/%u",
)


def server_setup_instructions(place: dict, ca: ProjectCA | None = None, project_id: str | None = None) -> dict:
    """What a server needs to trust this project's certificates.

    Returns ``sshd_config`` lines, the files with their contents, an
    idempotent root script (safe to run again after a member is removed: it
    refreshes the revocation list), and a one-line command the owner can paste.
    Nothing here is secret: CA public key, revocation spec, principal names.
    """
    if ca is None:
        ca = ProjectCA.open(require_safe_id(project_id or "", "project id"))
    role = str(place.get("role") or "development")
    user = str(place.get("user") or "")
    if role not in PLACE_ROLES:
        raise SecurityError(f"Unknown place role: {role}")
    if not _USER_RE.match(user):
        raise SecurityError("The server account name has characters Playground can't use safely.")
    principal = principal_for(ca.project_id, role)
    pdir = f"{SERVER_DIR}/projects/{ca.project_id}"
    ca_pub = f"{ca.public_key} playground-ca:{ca.project_id}"
    spec = ca.revocation_spec()
    script = f"""#!/bin/sh
# Playground: let members of project {ca.project_id} sign in as {user} with short-lived certificates.
# Safe to run again; it also refreshes the revocation list.
set -eu
if [ "$(id -u)" != 0 ]; then echo "Please run this with sudo."; exit 1; fi
umask 022
D={SERVER_DIR}
mkdir -p "$D/projects/{ca.project_id}" "$D/principals"
cat > "{pdir}/ca.pub" <<'PLAYGROUND_EOF'
{ca_pub}
PLAYGROUND_EOF
cat > "{pdir}/revoked.spec" <<'PLAYGROUND_EOF'
{spec.rstrip()}
PLAYGROUND_EOF
touch "$D/principals/{user}"
# A shared project whose home is here is checked at every sign-in instead (its "live" file); leave it out.
[ -f "{pdir}/live" ] || grep -qx '{principal}' "$D/principals/{user}" || echo '{principal}' >> "$D/principals/{user}"
cat "$D"/projects/*/ca.pub > "$D/trusted_user_ca_keys.new"
ssh-keygen -k -f "$D/revoked.krl.new"
for p in "$D"/projects/*/; do
  ssh-keygen -k -u -s "$p/ca.pub" -f "$D/revoked.krl.new" "$p/revoked.spec"
done
mv "$D/trusted_user_ca_keys.new" "$D/trusted_user_ca_keys"
mv "$D/revoked.krl.new" "$D/revoked.krl"
CONF=/etc/ssh/sshd_config.d/40-playground.conf
if [ ! -f "$CONF" ]; then
  current=$(sshd -T 2>/dev/null | grep -Ei '^(trustedusercakeys|revokedkeys|authorizedprincipalsfile) ' | grep -vi ' none$' || true)
  if [ -n "$current" ]; then
    echo "This server already uses SSH certificates of its own:"; echo "$current"
    echo "Add these lines to its sshd_config by hand instead:"
    printf '%s\\n' {" ".join(shlex.quote(line) for line in SSHD_LINES)}
    exit 2
  fi
  if ! grep -Eqs '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\\.d/' /etc/ssh/sshd_config; then
    echo "This server's sshd_config doesn't read sshd_config.d. Add these lines to it by hand:"
    printf '%s\\n' {" ".join(shlex.quote(line) for line in SSHD_LINES)}
    exit 2
  fi
  printf '%s\\n' '# Added by Playground.' {" ".join(shlex.quote(line) for line in SSHD_LINES)} > "$CONF.new"
  mv "$CONF.new" "$CONF"
fi
sshd -t
systemctl reload sshd 2>/dev/null || systemctl reload ssh 2>/dev/null || service ssh reload
echo "Done. Members of this project can now sign in as {user}."
"""
    encoded = base64.b64encode(script.encode()).decode()
    port = int(place.get("port") or 22)
    host = str(place.get("host") or "")
    destination = f"{user}@{host}" if host else user
    remote = f"printf %s {encoded} | base64 -d | sudo sh"
    command = f"ssh -t -p {port} {shlex.quote(destination)} {shlex.quote(remote)}"
    return {
        "sshd_config_lines": list(SSHD_LINES),
        "principal": principal,
        "files": {
            f"{pdir}/ca.pub": ca_pub + "\n",
            f"{pdir}/revoked.spec": spec,
            f"{SERVER_DIR}/principals/{user}": principal + "\n",
        },
        "script": script,
        "one_time_command": command,
        "explanation": (f"Run this once on {place.get('name') or host or 'the server'} as someone who can use sudo. "
                        f"It lets project members sign in as {user} with certificates that last {CERT_DEFAULT_VALIDITY_HOURS} hours. "
                        "Run it again after removing someone to take their access away right away."),
    }


# ───────────────────────────── vault ─────────────────────────────

try:  # python3-cryptography is a runtime Requires on Luma; the Mac dev host may lack it.
    from cryptography.exceptions import InvalidSignature, InvalidTag
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    _CRYPTO_ERROR = ""
except Exception as exc:  # noqa: BLE001
    _CRYPTO_ERROR = f"{type(exc).__name__}: {exc}"


def crypto_available() -> tuple[bool, str]:
    if _CRYPTO_ERROR:
        return False, "The vault needs python3-cryptography, which isn't installed on this computer."
    return True, ""


def _require_crypto() -> None:
    ok, reason = crypto_available()
    if not ok:
        raise VaultUnavailable(reason)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _unb64(text: Any) -> bytes:
    if not isinstance(text, str):
        raise VaultTampered("The vault file is damaged.")
    try:
        return base64.b64decode(text, validate=True)
    except Exception as exc:
        raise VaultTampered("The vault file is damaged.") from exc


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def recipient_id(x25519_public: bytes, ed25519_public: bytes) -> str:
    return hashlib.sha256(b"playground-vault/1 recipient\n" + x25519_public + ed25519_public).hexdigest()[:24]


class VaultIdentity:
    """This device's vault keys: X25519 for receiving secrets, Ed25519 for signing.

    Kept in ``<data>/keys/vault_identity.json`` (0600) as PKCS#8 PEM, encrypted
    with a keyring passphrase when the Secret Service is available.
    """

    def __init__(self, x_private: Any, ed_private: Any, device: str, protection: str) -> None:
        self._x = x_private
        self._ed = ed_private
        self.device = device
        self.protection = protection
        self.x25519_public = x_private.public_key().public_bytes_raw()
        self.ed25519_public = ed_private.public_key().public_bytes_raw()
        self.id = recipient_id(self.x25519_public, self.ed25519_public)

    def public(self) -> dict:
        """The shareable half: what an admin needs to add this device to a vault."""
        return {"id": self.id, "x25519": _b64(self.x25519_public), "ed25519": _b64(self.ed25519_public),
                "device": self.device}

    def sign(self, data: bytes) -> bytes:
        return self._ed.sign(data)

    def exchange(self, peer_public: bytes) -> bytes:
        return self._x.exchange(X25519PublicKey.from_public_bytes(peer_public))

    @classmethod
    def path(cls) -> Path:
        return keys_dir() / "vault_identity.json"

    @classmethod
    def generate(cls, device: str = "device") -> "VaultIdentity":
        """A fresh identity held only in memory (tests, and previews of an invite)."""
        _require_crypto()
        return cls(X25519PrivateKey.generate(), Ed25519PrivateKey.generate(), device, "memory")

    @classmethod
    def load_or_create(cls, keyring: Keyring | None = None, create: bool = True) -> "VaultIdentity | None":
        _require_crypto()
        path = cls.path()
        directory = private_dir(keys_dir())
        keyring = keyring or Keyring()
        if path.exists():
            check_private_file(path)
            data = _read_json(path) or {}
            password = None
            if data.get("protection") == "keyring":
                secret = keyring.lookup("vault", "device")
                if secret is None:
                    raise VaultUnavailable("Your keyring is locked, so the vault can't open. Unlock it and try again.")
                password = secret.encode()
            try:
                x = serialization.load_pem_private_key(data["x25519"].encode(), password)
                ed = serialization.load_pem_private_key(data["ed25519"].encode(), password)
            except Exception as exc:  # noqa: BLE001
                raise VaultUnavailable("This device's vault key couldn't be opened.") from exc
            return cls(x, ed, str(data.get("device") or _device_name()), str(data.get("protection")))
        if not create:
            return None
        x, ed = X25519PrivateKey.generate(), Ed25519PrivateKey.generate()
        passphrase = secrets.token_urlsafe(32)
        protection = "file-permissions"
        encryption: Any = serialization.NoEncryption()
        if keyring.available() and keyring.store("vault", "device", passphrase, "Playground vault key"):
            back = keyring.lookup("vault", "device")
            if back is not None and same_secret(back, passphrase):
                protection = "keyring"
                encryption = serialization.BestAvailableEncryption(passphrase.encode())
        pem = {name: key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       encryption).decode()
               for name, key in (("x25519", x), ("ed25519", ed))}
        device = _device_name()
        write_private(directory / path.name, json.dumps({"format": VAULT_FORMAT, "device": device,
                                                          "protection": protection, "created": _iso(), **pem},
                                                         indent=2))
        return cls(x, ed, device, protection)


def _verify_sig(ed_public_b64: str, sig_b64: str, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(ed_public_b64)).verify(_unb64(sig_b64), message)
        return True
    except (InvalidSignature, ValueError, VaultTampered):
        return False


_ROSTER_DOMAIN = b"playground-vault/1 roster\n"
_SECRET_DOMAIN = b"playground-vault/1 secret\n"


class Vault:
    """A project's end-to-end encrypted secrets, in ``.playground/vault.json``.

    ::

        vault = Vault(project_dir, project_id)
        vault.create("sam", "Sam")                         # owner, once
        vault.add_member("priya", "Priya", priya_public)   # priya_public = her VaultIdentity.public()
        vault.set("DATABASE_URL", "postgres://…")
        vault.get("DATABASE_URL")
        vault.remove_member("priya")                       # → names to change at the source
    """

    def __init__(self, project_dir: Path | str, project_id: str, identity: VaultIdentity | None = None,
                 trust_dir: Path | None = None) -> None:
        self.project_dir = Path(project_dir)
        self.project_id = require_safe_id(project_id, "project id")
        self.path = self.project_dir / ".playground" / VAULT_FILE
        self._identity = identity
        self._trust_dir = trust_dir

    # ── status ──

    def status(self) -> dict:
        ok, reason = crypto_available()
        exists = self.path.exists()
        if not ok:
            return {"available": False, "reason": reason, "exists": exists, "member": False, "names": []}
        try:
            data = self._load() if exists else None
            me = self.identity.id
            members = data["roster"][-1]["body"]["members"] if data else {}
            names = sorted(data["secrets"]) if data else []
            return {"available": True, "reason": "", "exists": exists, "member": me in members,
                    "admin": bool(data) and me in data["roster"][-1]["body"]["admins"], "names": names,
                    "members": sorted({m["member"] for m in members.values()})}
        except SecurityError as exc:
            return {"available": False, "reason": str(exc), "exists": exists, "member": False, "names": []}

    @property
    def identity(self) -> VaultIdentity:
        if self._identity is None:
            ident = VaultIdentity.load_or_create()
            assert ident is not None
            self._identity = ident
        return self._identity

    # ── trust pinning (per device, outside the project) ──

    def _trust_path(self) -> Path:
        base = self._trust_dir or (data_dir() / "vault-trust")
        return private_dir(Path(base)) / f"{self.project_id}.json"

    def _pin(self, roster: list[dict]) -> None:
        head = roster[-1]
        write_private(self._trust_path(), json.dumps({
            "genesis_signer": roster[0]["signer"],
            "genesis_hash": hashlib.sha256(_canonical(roster[0])).hexdigest(),
            "seen_version": head["body"]["version"],
            "seen_hash": hashlib.sha256(_canonical(head)).hexdigest(),
        }, indent=2))

    # ── file I/O ──

    def _load(self) -> dict:
        _require_crypto()
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            raise SecurityError("This project doesn't have a vault yet.") from None
        except ValueError as exc:
            raise VaultTampered("The vault file is damaged.") from exc
        if not isinstance(data, dict) or data.get("format") != VAULT_FORMAT:
            raise VaultTampered("The vault file is damaged or from a newer Playground.")
        if data.get("project_id") != self.project_id:
            raise VaultTampered("This vault belongs to a different project.")
        if not isinstance(data.get("roster"), list) or not data["roster"] or not isinstance(data.get("secrets"), dict):
            raise VaultTampered("The vault file is damaged.")
        self._verify_roster(data["roster"])
        return data

    def _verify_roster(self, roster: list[dict]) -> None:
        prev_admins: dict[str, str] | None = None
        for index, entry in enumerate(roster):
            try:
                body, signer, sig = entry["body"], entry["signer"], entry["sig"]
                members, admins = body["members"], body["admins"]
            except (KeyError, TypeError) as exc:
                raise VaultTampered("The vault's member list is damaged.") from exc
            if body.get("version") != index + 1 or body.get("project_id") != self.project_id:
                raise VaultTampered("The vault's member list is out of order.")
            if any(a not in members for a in admins):
                raise VaultTampered("The vault's member list is damaged.")
            for rid, m in members.items():
                if not same_secret(rid, recipient_id(_unb64(m["x25519"]), _unb64(m["ed25519"]))):
                    raise VaultTampered("A vault member's keys don't match their id.")
            trusted = ({a: members[a]["ed25519"] for a in admins} if prev_admins is None else prev_admins)
            if signer not in trusted or not _verify_sig(trusted[signer], sig, _ROSTER_DOMAIN + _canonical(body)):
                raise VaultTampered("Someone changed the vault's member list without permission.")
            prev_admins = {a: members[a]["ed25519"] for a in admins}
        pin = _read_json(self._trust_path()) if self._trust_path().exists() else None
        if pin:
            if not same_secret(pin["genesis_hash"], hashlib.sha256(_canonical(roster[0])).hexdigest()):
                raise VaultTampered("This isn't the vault you trusted before.")
            seen = int(pin["seen_version"])
            if len(roster) < seen:
                raise VaultTampered("This vault is older than one you've already seen. Someone may have rolled it back.")
            if not same_secret(pin["seen_hash"], hashlib.sha256(_canonical(roster[seen - 1])).hexdigest()):
                raise VaultTampered("The vault's history was rewritten.")
        self._pin(roster)

    def _save(self, data: dict, forbidden: Iterable[bytes] = ()) -> None:
        text = json.dumps(data, indent=2, sort_keys=True)
        raw = text.encode()
        # Belt and braces: refuse to write if any plaintext we were given would land on disk.
        for value in forbidden:
            if len(value) >= 4 and (value in raw or _b64(value).encode() in raw):
                raise SecurityError("Playground refused to write the vault: a secret would have been stored unencrypted.")
        write_public(self.path, text + "\n")

    # ── roster ──

    def _members(self, data: dict) -> dict:
        return data["roster"][-1]["body"]["members"]

    def _append_roster(self, data: dict, members: dict, admins: list[str]) -> None:
        me = self.identity.id
        if me not in data["roster"][-1]["body"]["admins"]:
            raise VaultAccessDenied("Only a project owner can change who can open the vault.")
        body = {"version": len(data["roster"]) + 1, "project_id": self.project_id, "updated": _iso(),
                "members": members, "admins": sorted(admins)}
        data["roster"].append({"body": body, "signer": me,
                               "sig": _b64(self.identity.sign(_ROSTER_DOMAIN + _canonical(body)))})

    def create(self, member_id: str, name: str) -> dict:
        _require_crypto()
        if self.path.exists():
            raise SecurityError("This project already has a vault.")
        me = self.identity
        members = {me.id: {"member": require_safe_id(member_id, "member id"), "name": name,
                           "x25519": _b64(me.x25519_public), "ed25519": _b64(me.ed25519_public),
                           "device": me.device}}
        body = {"version": 1, "project_id": self.project_id, "updated": _iso(), "members": members,
                "admins": [me.id]}
        data = {"format": VAULT_FORMAT, "project_id": self.project_id,
                "roster": [{"body": body, "signer": me.id, "sig": _b64(me.sign(_ROSTER_DOMAIN + _canonical(body)))}],
                "secrets": {}}
        self._save(data)
        self._pin(data["roster"])
        return self.status()

    def add_member(self, member_id: str, name: str, public_identity: dict, admin: bool = False) -> dict:
        """Let a member's device open the vault; every secret is wrapped for it too."""
        data = self._load()
        try:
            x_pub, ed_pub = _unb64(public_identity["x25519"]), _unb64(public_identity["ed25519"])
            X25519PublicKey.from_public_bytes(x_pub)
            Ed25519PublicKey.from_public_bytes(ed_pub)
        except (KeyError, ValueError, VaultTampered) as exc:
            raise SecurityError("That device's vault key isn't valid.") from exc
        rid = recipient_id(x_pub, ed_pub)
        members = dict(self._members(data))
        admins = list(data["roster"][-1]["body"]["admins"])
        members[rid] = {"member": require_safe_id(member_id, "member id"), "name": name,
                        "x25519": _b64(x_pub), "ed25519": _b64(ed_pub),
                        "device": str(public_identity.get("device") or "device")[:40]}
        if admin and rid not in admins:
            admins.append(rid)
        self._append_roster(data, members, admins)
        self._save(data, self._rewrap_all(data))
        return {"member": member_id, "device": rid}

    def remove_member(self, member_id: str) -> dict:
        """Take a member's devices off the vault and re-encrypt everything with new keys.

        Returns ``to_change``: secrets they could read before, which should be
        changed where they come from (the database, the provider…).
        """
        data = self._load()
        members = {rid: m for rid, m in self._members(data).items() if m["member"] != member_id}
        if len(members) == len(self._members(data)):
            return {"member": member_id, "to_change": []}
        if not members:
            raise SecurityError("The vault needs at least one person who can open it.")
        admins = [a for a in data["roster"][-1]["body"]["admins"] if a in members]
        if not admins:
            raise SecurityError("The vault needs at least one owner.")
        names = sorted(data["secrets"])
        self._append_roster(data, members, admins)
        self._save(data, self._rewrap_all(data))
        return {"member": member_id, "to_change": names}

    def remove_device(self, device_id: str) -> dict:
        """Take one computer off the vault (a lost laptop) and re-encrypt everything with new keys. The person
        keeps their other computers. Returns ``to_change`` as ``remove_member`` does."""
        data = self._load()
        current = self._members(data)
        if device_id not in current:
            return {"device": device_id, "to_change": []}
        members = {rid: m for rid, m in current.items() if rid != device_id}
        if not members:
            raise SecurityError("The vault needs at least one person who can open it.")
        admins = [a for a in data["roster"][-1]["body"]["admins"] if a in members]
        if not admins:
            raise SecurityError("The vault needs at least one owner.")
        names = sorted(data["secrets"])
        self._append_roster(data, members, admins)
        self._save(data, self._rewrap_all(data))
        return {"device": device_id, "to_change": names}

    # ── secrets ──

    def _aad(self, name: str, version: int) -> bytes:
        return _canonical({"project_id": self.project_id, "name": name, "version": version})

    def _wrap(self, content_key: bytes, recipient: dict, rid: str, name: str, version: int) -> dict:
        eph = X25519PrivateKey.generate()
        eph_pub = eph.public_key().public_bytes_raw()
        peer = _unb64(recipient["x25519"])
        shared = eph.exchange(X25519PublicKey.from_public_bytes(peer))  # raises on a low-order point
        kek = HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_pub + peer,
                   info=b"playground-vault/1 wrap\n" + self._aad(name, version)).derive(shared)
        nonce = os.urandom(12)
        wrapped = ChaCha20Poly1305(kek).encrypt(nonce, content_key, self._aad(name, version) + rid.encode())
        return {"epk": _b64(eph_pub), "nonce": _b64(nonce), "key": _b64(wrapped)}

    def _unwrap(self, entry: dict, name: str) -> bytes:
        me = self.identity
        wrap = entry["wraps"].get(me.id)
        if wrap is None:
            raise VaultAccessDenied("This device can't open that secret. Ask an owner to add it.")
        version = int(entry["version"])
        eph_pub = _unb64(wrap["epk"])
        try:
            shared = me.exchange(eph_pub)
            kek = HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_pub + me.x25519_public,
                       info=b"playground-vault/1 wrap\n" + self._aad(name, version)).derive(shared)
            return ChaCha20Poly1305(kek).decrypt(_unb64(wrap["nonce"]), _unb64(wrap["key"]),
                                                 self._aad(name, version) + me.id.encode())
        except (InvalidTag, ValueError) as exc:
            raise VaultTampered(f"The secret {name} was tampered with.") from exc

    def _signed_entry(self, name: str, version: int, content_key: bytes, plaintext: bytes, members: dict) -> dict:
        nonce = os.urandom(12)
        ciphertext = ChaCha20Poly1305(content_key).encrypt(nonce, plaintext, self._aad(name, version))
        entry = {"version": version, "nonce": _b64(nonce), "ciphertext": _b64(ciphertext),
                 "wraps": {rid: self._wrap(content_key, m, rid, name, version) for rid, m in members.items()},
                 "writer": self.identity.id, "updated": _iso()}
        entry["sig"] = _b64(self.identity.sign(_SECRET_DOMAIN + name.encode() + b"\n" + _canonical(entry)))
        return entry

    def _open_entry(self, data: dict, name: str) -> bytes:
        entry = data["secrets"].get(name)
        if entry is None:
            raise KeyError(name)
        members = self._members(data)
        if self.identity.id not in members:
            raise VaultAccessDenied("This device isn't a member of the vault.")
        writer = members.get(entry.get("writer"))
        unsigned = {k: v for k, v in entry.items() if k != "sig"}
        if writer is None or not _verify_sig(writer["ed25519"], entry.get("sig", ""),
                                             _SECRET_DOMAIN + name.encode() + b"\n" + _canonical(unsigned)):
            raise VaultTampered(f"The secret {name} wasn't written by a member.")
        content_key = self._unwrap(entry, name)
        try:
            return ChaCha20Poly1305(content_key).decrypt(_unb64(entry["nonce"]), _unb64(entry["ciphertext"]),
                                                         self._aad(name, int(entry["version"])))
        except InvalidTag as exc:
            raise VaultTampered(f"The secret {name} was tampered with.") from exc

    def _rewrap_all(self, data: dict) -> list[bytes]:
        """Re-encrypt every secret under a fresh content key for the current members.

        Returns the plaintexts so the save can check none of them reach disk.
        """
        members = self._members(data)
        plaintexts = []
        for name in sorted(data["secrets"]):
            value = self._open_entry(data, name)
            plaintexts.append(value)
            version = int(data["secrets"][name]["version"]) + 1
            data["secrets"][name] = self._signed_entry(name, version, ChaCha20Poly1305.generate_key(), value, members)
        return plaintexts

    def set(self, name: str, value: str | bytes) -> None:
        if not isinstance(name, str) or not _SECRET_NAME_RE.match(name):
            raise SecurityError("Secret names use letters, numbers and underscores, like DATABASE_URL.")
        plaintext = value.encode() if isinstance(value, str) else bytes(value)
        data = self._load()
        members = self._members(data)
        if self.identity.id not in members:
            raise VaultAccessDenied("This device isn't a member of the vault.")
        old = data["secrets"].get(name)
        version = int(old["version"]) + 1 if old else 1
        data["secrets"][name] = self._signed_entry(name, version, ChaCha20Poly1305.generate_key(), plaintext, members)
        self._save(data, [plaintext])

    def get(self, name: str) -> str:
        return self.get_bytes(name).decode()

    def get_bytes(self, name: str) -> bytes:
        data = self._load()
        if name not in data["secrets"]:
            raise SecurityError(f"There's no secret called {name}.")
        return self._open_entry(data, name)

    def names(self) -> list[str]:
        """Secret names. Values stay encrypted; the names are not secret."""
        return sorted(self._load()["secrets"])

    def delete(self, name: str) -> bool:
        data = self._load()
        if self.identity.id not in self._members(data):
            raise VaultAccessDenied("This device isn't a member of the vault.")
        if data["secrets"].pop(name, None) is None:
            return False
        self._save(data)
        return True

    def rotate(self, name: str | None = None, new_value: str | bytes | None = None) -> list[str]:
        """Re-encrypt one secret (or all) under fresh content keys; optionally change the value."""
        data = self._load()
        members = self._members(data)
        if self.identity.id not in members:
            raise VaultAccessDenied("This device isn't a member of the vault.")
        targets = [name] if name else sorted(data["secrets"])
        forbidden: list[bytes] = []
        for target in targets:
            if target not in data["secrets"]:
                raise SecurityError(f"There's no secret called {target}.")
            value = self._open_entry(data, target)
            if new_value is not None and name:
                value = new_value.encode() if isinstance(new_value, str) else bytes(new_value)
            forbidden.append(value)
            version = int(data["secrets"][target]["version"]) + 1
            data["secrets"][target] = self._signed_entry(target, version, ChaCha20Poly1305.generate_key(),
                                                         value, members)
        self._save(data, forbidden)
        return targets

    def rewrap(self) -> list[str]:
        """Re-encrypt every secret for exactly the current members."""
        return self.rotate()

    def env(self, names: Iterable[str] | None = None) -> dict[str, str]:
        """Secrets as environment variables for a process (never written anywhere)."""
        data = self._load()
        wanted = list(names) if names is not None else sorted(data["secrets"])
        return {n: self._open_entry(data, n).decode() for n in wanted if n in data["secrets"]}


# ───────────────────────────── membership, all together ─────────────────────────────

def grant_member(project_dir: Path | str, project_id: str, member_id: str, name: str, role: str,
                 device_public_key: str, device_name: str = "device",
                 vault_public: dict | None = None) -> dict:
    """Adding a member grants access: server certificates, and the vault if a device key is given."""
    ca = ProjectCA.open(project_id)
    result = ca.add_member(member_id, name, role, device_public_key, device_name)
    result["vault"] = False
    if vault_public is not None:
        vault = Vault(project_dir, project_id)
        if vault.path.exists():
            vault.add_member(member_id, name, vault_public, admin=(role == "owner"))
            result["vault"] = True
    return result


def revoke_member(project_dir: Path | str, project_id: str, member_id: str) -> dict:
    """Removing a member revokes it: KRL entries, and vault re-encryption without them."""
    ca = ProjectCA.open(project_id)
    result = ca.remove_member(member_id)
    result["to_change"] = []
    vault = Vault(project_dir, project_id)
    if vault.path.exists() and crypto_available()[0]:
        result["to_change"] = vault.remove_member(member_id)["to_change"]
    result["next_step"] = ("A shared project's home stops letting them in at once. On other servers, run the setup "
                           "command again to take their access away now; otherwise it ends within "
                           f"{CERT_DEFAULT_VALIDITY_HOURS} hours, when their certificate runs out.")
    return result
