# SPDX-License-Identifier: Apache-2.0
"""Sharing a project between teammates' computers (V1 sync).

Every teammate runs the full Playground app on their own computer, with their
own copy of the project. The copies meet at the project's *home*:

* a **Development server** ("our dev box in the closet"): the main path;
* the **owner's own computer**, which then has to be on for teammates to sync
  (the app says so plainly);
* a plain **folder** holding the shared repository (tests and advanced use; it
  has no access control beyond file permissions, so the UI never offers it).

The home is one bare git repository, ``<home>/repo.git``. On a server there is
also ``<home>/checkout`` (main, kept current by a post-receive hook) and
``<home>/stage/<member>`` (each builder's preview checkout). Everything travels
through that one repository, over SSH:

``refs/heads/main``, ``refs/heads/goal/<goal-id>``
    The code. Goal work happens on the goal's branch and is merged into main
    on release. Ordinary git: nothing is ever force-pushed.
``refs/playground/state``
    Everything in ``.playground/`` except ``local/`` and ``presence/``: goals,
    threads, decisions, issues, knowledge, activity, stack, usage, capacity,
    proof and the vault (ciphertext only). It has a history of its own, apart
    from the code, so switching branches never rewinds a goal. Merges are done
    here, in Python, file type by file type, and never stop for a conflict
    (see "Merging" below).
``refs/playground/presence/<member>``
    One small JSON blob per person, replaced on each heartbeat. No history:
    nothing points at old heartbeats, so git's gc throws them away. Each
    person may only write their own.
``refs/playground/access``
    The access list: which member, with which role, from which device, and
    where the home is. Signed with the project CA (``ssh-keygen -Y sign``).
    The home verifies it against the root-owned CA public key on every
    connection, and only owners may push it.
``refs/playground/certs``
    Fresh certificates for every member's device. Public, and useless without
    the device's private key; signed by the CA.

Who gets in (no shared keys, no passwords)
    Each device has its own ed25519 key (``core.security``). The owner's
    device holds the project CA. Members sync with a **sync certificate**:
    principal ``pg-<project>-sync``, no pty, no forwarding, and a
    ``force-command`` that runs ``<home>/gate <member> <device>``. The gate
    checks the signed access list and then runs ``git upload-pack`` or
    ``git receive-pack`` on this project's repository and nothing else. Its
    pre-receive hook enforces roles: viewers push only their presence,
    reviewers can't touch code, nobody rewrites main, only owners change the
    access list. Owners and builders also get the usual short-lived
    development certificate (a shell, for previews on the server).
    Removing someone re-signs the access list without them: the gate refuses
    their very next sync. Their keys also go into the CA's revocation list,
    which servers pick up when the setup script runs again.

Merging (``merge_file``; never loses an entry)
    * ``*.jsonl`` (activity, threads, usage, analytics): every line from
      everyone, each once (multiset union). Locally new lines are only ever
      appended, so the watcher sees them as new entries.
    * ``decisions.md``: line merge; when both added at the same place, both
      blocks are kept.
    * Markdown with front matter (goals, issues, knowledge, brief): field by
      field, then section by section, then line by line. Where two people
      changed the same thing, the newer ``updated`` wins, and any entry (a
      story, a check, a note line) that only the other side added is kept.
    * TOML and JSON: key by key; tables in arrays merge by ``id`` (or
      ``name``). Removing a member wins over a concurrent edit; anything
      else that was changed on one side and removed on the other is kept.
    * ``vault.json``: the longer valid roster wins; each secret keeps its
      newest version. A roster fork (two owners at once) is reported.
    * Anything else that both changed: both copies are kept.
    Merges are deterministic: the same three inputs give the same result.

The engine (``SyncEngine``) runs one round every few seconds and soon after
any change: snapshot ``.playground/`` into the state ref, fetch, merge, apply
the result to the folder (under the store's lock, re-checking each file, so a
write made meanwhile is merged rather than lost), push. It publishes
``sync.state`` and ``presence.changed``.

Agents run on the prompting person's own machine against their own clone,
with their own model or account. Sync carries files, never prompts or
credentials. Previews can run on the Dev server through an SSH tunnel
(``SyncEngine.stage_place`` + ``core.preview``).
"""
from __future__ import annotations

import base64
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import difflib
import getpass
import hashlib
import ipaddress
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
import time
import tomllib
from typing import Any, Callable, Iterable
import zlib

from . import events, security
from . import paths as _paths
from .model import Member, now_iso
from .store import (ProjectStore, StoreError, dump_front_matter, dump_sections, dump_toml, parse_front_matter,
                    parse_sections, read_json)

log = logging.getLogger(__name__)

# ── event topics (core.events is the bus; these are sync's own topics) ──
SYNC_STATE = "sync.state"              # project, state, text, detail, last_sync, behind, ahead, changed[]
PRESENCE_CHANGED = "presence.changed"  # project, people[]

# ── refs ──
REMOTE = "playground"
MAIN = "main"
GOAL_PREFIX = "goal/"
STATE_REF = "refs/playground/state"
ACCESS_REF = "refs/playground/access"
CERTS_REF = "refs/playground/certs"
PRESENCE_PREFIX = "refs/playground/presence/"
MIRROR = "refs/playground/mirror/"          # what the home had at the last fetch
MIRROR_STATE = MIRROR + "state"
MIRROR_ACCESS = MIRROR + "access"
MIRROR_CERTS = MIRROR + "certs"
MIRROR_PRESENCE = MIRROR + "presence/"
REMOTE_HEADS = "refs/remotes/playground/"
RESTORE_TAGS = "refs/tags/playground/restore/"     # restore points, per member and device (see places)
REMOTE_RESTORE = "refs/remotes/playground-restore/"  # the home's restore points at the last fetch
FETCH_REFSPECS = ("+refs/heads/*:refs/remotes/playground/*", "+refs/playground/*:refs/playground/mirror/*",
                  f"+{RESTORE_TAGS}*:{REMOTE_RESTORE}*")

# ── timing and limits ──
DEFAULT_INTERVAL = 4.0
OFFLINE_BACKOFF = (5.0, 10.0, 20.0, 30.0, 60.0)
PRESENCE_REFRESH_S = 45
PRESENCE_FRESH_S = 150
SYNC_CERT_DAYS = 7
SYNC_CERT_RENEW_S = 3 * 86400
DEV_CERT_RENEW_S = 4 * 3600
PASS_WARN_S = 2 * 86400          # members are told this long before their pass runs out
ACCESS_VALID_S = 72 * 3600       # a signed access list works this long after it's signed...
ACCESS_REFRESH_S = 48 * 3600     # ...and any owner's Playground signs a fresh one once less than this is left
ACCESS_MAX_S = 96 * 3600         # the home refuses a list that claims to last longer than this
ACCESS_SKEW_S = 600              # clocks may disagree this much
OWNER_DUTY_EVERY_S = 300
MAX_STATE_FILE = 25 * 1024 * 1024
MAX_PRESENCE_BYTES = 4096
INVITE_DAYS = 7

MODES = ("server", "this-computer", "folder")
# Where the home's trust lives. "live": a one-time root setup installed root-owned checks, and sshd asks the
# signed access list at every sign-in (removal is immediate for syncing and shells). "account": the trust lives
# in the server account itself (no root): syncing still stops at once, shells end when their pass runs out.
TRUST_MODES = ("live", "account")
THIS_COMPUTER_WARNING = "Teammates can only work on this project while this computer is on."
STATE_GITIGNORE = """\
# Playground: this computer's own files. They never leave it.
local/
# Who's here right now travels on its own refs, outside the history.
presence/
*.tmp
.DS_Store
"""
STATE_GITATTRIBUTES = """\
# Playground: how teammates' copies of these records come together.
# Playground merges them itself (core/sync.py); these lines are for plain git.
# One file per goal, bug and note: merged field by field and line by line.
*.md merge=playground
*.toml merge=playground
*.json merge=playground
# Append-only logs keep every line from everyone (later lines win, so these come last).
*.jsonl merge=union
decisions.md merge=union
*.png binary
*.jpg binary
*.webm binary
"""
CODE_IGNORE_MARK = "# Playground (shared project):"
CODE_IGNORE = f"""
{CODE_IGNORE_MARK} its records sync on their own, apart from the code.
/.playground/
# Made on each computer for its own folder.
/AGENTS.md
/.mcp.json
"""

STATE_WORDS = {
    "not-shared": "Only on this computer",
    "stopped": "Not syncing",
    "up-to-date": "Up to date",
    "syncing": "Syncing",
    "offline": "Offline, will catch up",
    "access": "Waiting to be let in",
    "removed": "You’re no longer on this project",
    "error": "Sync stopped",
    "conflict": "Needs a hand",
}


# ════════════════════════════════════ errors ════════════════════════════════════

class SyncError(Exception):
    """Something went wrong; ``str(error)`` is plain words a person can read."""


class SyncConflict(SyncError):
    """Two people changed the same code; a person (or Ari) has to bring it together."""


class InviteError(SyncError):
    pass


class AccessRemoved(SyncError):
    pass


class GitFailed(SyncError):
    def __init__(self, args: Iterable[str], proc: subprocess.CompletedProcess) -> None:
        self.args_list = list(args)
        self.returncode = proc.returncode
        self.stderr = (proc.stderr or b"").decode(errors="replace") if isinstance(proc.stderr, bytes) else (proc.stderr or "")
        last = [line for line in self.stderr.strip().splitlines() if line.strip()][-1:] or ["no detail"]
        super().__init__(f"git {self.args_list[0] if self.args_list else ''} failed: {last[0]}")


class _Offline(SyncError):
    pass


# ════════════════════════════════════ small helpers ════════════════════════════════════

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _now()).astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(text: Any) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _write_bytes(path: Path, data: bytes) -> None:
    """Atomic replace. The temp name ends in .tmp, which the snapshot skips."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


def _safe_rel(rel: str) -> bool:
    """A path from a teammate's tree we are willing to write under .playground/."""
    if not rel or rel.startswith("/") or "\\" in rel or "\0" in rel:
        return False
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.lower() == ".git" for p in parts):
        return False
    return not _state_ignored(rel)


def _state_ignored(rel: str) -> bool:
    """Files in .playground/ that never go into the shared records."""
    parts = rel.split("/")
    if parts[0] in ("local", "presence"):
        return True
    name = parts[-1]
    if name.endswith((".tmp", ".lock", ".swp", "~")) or name in (".DS_Store", "Thumbs.db"):
        return True
    if any(p.startswith(".") for p in parts[:-1]):
        return True
    return name.startswith(".") and name not in (".gitignore", ".gitattributes")


def _complete_lines(data: bytes) -> bytes:
    cut = data.rfind(b"\n")
    return data[:cut + 1] if cut >= 0 else b""


def _jsonl_lines(data: bytes | None) -> list[bytes]:
    return [line for line in (data or b"").split(b"\n") if line.strip()]


def _same_lines(a: bytes | None, b: bytes | None) -> bool:
    return Counter(_jsonl_lines(a)) == Counter(_jsonl_lines(b))


def _is_text(data: bytes) -> bool:
    if b"\0" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


_RESTORE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[0-9]{1,9}-[0-9a-f]{6}$")


def _when_words(moment: float, now: float | None = None) -> str:
    """"in 2 days", "tomorrow", "in 5 hours", "today": how soon something runs out, in plain words."""
    left = moment - (time.time() if now is None else now)
    if left <= 0:
        return "now"
    hours = left / 3600
    if hours < 1:
        return "within the hour"
    if hours < 20:
        return f"in {int(round(hours))} hour{'s' if int(round(hours)) != 1 else ''}"
    days = int(round(hours / 24)) or 1
    return "tomorrow" if days == 1 else f"in {days} days"


def expiry_words(moment: float, now: float | None = None) -> str:
    """How soon something runs out, in plain words ("in 2 days", "tomorrow", "in 5 hours")."""
    return _when_words(moment, now)


def _first(name: str) -> str:
    return name.split()[0] if name and name.split() else (name or "")


# ════════════════════════════════════ git ════════════════════════════════════

_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_COMMON_DIR", "GIT_PREFIX",
                 "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "GIT_QUARANTINE_PATH", "GIT_EDITOR")


def _git_tool() -> str:
    tool = shutil.which("git")
    if not tool:
        raise SyncError("Sharing needs git, and this computer doesn't have it.")
    return tool


def _git(cwd: Path | str, *args: str, env: dict[str, str] | None = None, input: bytes | None = None,
         check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    full = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    full.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANGUAGE": "C", "GIT_MERGE_AUTOEDIT": "no"})
    if env:
        full.update(env)
    try:
        proc = subprocess.run([_git_tool(), *args], cwd=str(cwd), env=full, input=input, capture_output=True,
                              timeout=timeout, stdin=None if input is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise _Offline("The project home took too long to answer.") from exc
    if check and proc.returncode != 0:
        raise GitFailed(args, proc)
    return proc


class _Repo:
    """Plumbing on the project's own repository (or a bare one)."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        proc = _git(self.root, "rev-parse", "--absolute-git-dir", check=False)
        if proc.returncode != 0:
            raise SyncError("This project isn't a git repository yet.")
        self.git_dir = Path(proc.stdout.decode().strip())
        common = _git(self.root, "rev-parse", "--git-common-dir").stdout.decode().strip()
        self.common_dir = Path(common) if os.path.isabs(common) else (self.root / common).resolve()
        self.private = self.common_dir / "playground"
        self.private.mkdir(parents=True, exist_ok=True)
        fmt = _git(self.root, "rev-parse", "--show-object-format", check=False).stdout.decode().strip()
        self.hash_name = "sha256" if fmt == "sha256" else "sha1"
        self.zero = "0" * (64 if self.hash_name == "sha256" else 40)

    def run(self, *args: str, **kw: Any) -> subprocess.CompletedProcess:
        return _git(self.root, *args, **kw)

    def text(self, *args: str, **kw: Any) -> str:
        return self.run(*args, **kw).stdout.decode(errors="replace").strip()

    def rev(self, ref: str) -> str | None:
        proc = self.run("rev-parse", "--verify", "-q", ref, check=False)
        value = proc.stdout.decode().strip()
        return value if proc.returncode == 0 and value else None

    def refs(self, prefix: str) -> dict[str, str]:
        out = self.text("for-each-ref", "--format=%(objectname) %(refname)", prefix)
        result = {}
        for line in out.splitlines():
            sha, _, name = line.partition(" ")
            if name.startswith(prefix):
                result[name[len(prefix):]] = sha
        return result

    def is_ancestor(self, older: str, newer: str) -> bool:
        return self.run("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0

    def merge_base(self, a: str, b: str) -> str | None:
        proc = self.run("merge-base", a, b, check=False)
        value = proc.stdout.decode().strip()
        return value if proc.returncode == 0 and value else None

    def ls_tree(self, rev: str | None) -> dict[str, tuple[str, str]]:
        if not rev:
            return {}
        out = self.run("ls-tree", "-r", "-z", "--full-tree", rev).stdout
        entries: dict[str, tuple[str, str]] = {}
        for record in out.split(b"\0"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            mode, kind, sha = meta.decode().split(" ")
            if kind == "blob":
                entries[path.decode("utf-8", errors="surrogateescape")] = (mode, sha)
        return entries

    def read_blobs(self, shas: Iterable[str]) -> dict[str, bytes]:
        wanted = list(dict.fromkeys(s for s in shas if s))
        if not wanted:
            return {}
        out = self.run("cat-file", "--batch", input=("\n".join(wanted) + "\n").encode()).stdout
        result: dict[str, bytes] = {}
        pos = 0
        for sha in wanted:
            newline = out.index(b"\n", pos)
            header = out[pos:newline].decode().split()
            if len(header) < 3 or header[1] == "missing":
                pos = newline + 1
                continue
            size = int(header[2])
            start = newline + 1
            result[sha] = out[start:start + size]
            pos = start + size + 1
        return result

    def blob(self, spec: str) -> bytes | None:
        proc = self.run("cat-file", "blob", spec, check=False)
        return proc.stdout if proc.returncode == 0 else None

    def hash_bytes(self, data: bytes) -> str:
        return hashlib.new(self.hash_name, b"blob %d\0" % len(data) + data).hexdigest()

    def write_blob(self, data: bytes) -> str:
        return self.text("hash-object", "-w", "--no-filters", "--stdin", input=data)

    def write_files(self, files: list[Path]) -> list[str]:
        if not files:
            return []
        data = "".join(f"{p}\n" for p in files).encode()
        return self.text("hash-object", "-w", "--no-filters", "--stdin-paths", input=data).splitlines()

    def make_tree(self, entries: dict[str, tuple[str, str]]) -> str:
        fd, index = tempfile.mkstemp(prefix="index-", dir=self.private)
        os.close(fd)
        os.unlink(index)
        try:
            env = {"GIT_INDEX_FILE": index}
            if entries:
                data = b"".join(f"{mode} {sha}\t".encode() + path.encode("utf-8", errors="surrogateescape") + b"\0"
                                for path, (mode, sha) in sorted(entries.items()))
                self.run("update-index", "-z", "--add", "--index-info", input=data, env=env)
            return self.text("write-tree", env=env)
        finally:
            try:
                os.unlink(index)
            except FileNotFoundError:
                pass

    def commit(self, tree: str, parents: Iterable[str], message: str, who: tuple[str, str]) -> str:
        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        name, email = who
        env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
               "GIT_COMMITTER_EMAIL": email}
        return self.text(*args, input=message.encode(), env=env)

    def update_ref(self, ref: str, new: str, old: str | None = None) -> bool:
        args = ["update-ref", "-m", "playground sync", ref, new]
        if old is not None:
            args.append(old or self.zero)
        return self.run(*args, check=False).returncode == 0

    def current_branch(self) -> str | None:
        proc = self.run("symbolic-ref", "--short", "-q", "HEAD", check=False)
        value = proc.stdout.decode().strip()
        return value if proc.returncode == 0 and value else None

    def dirty(self) -> bool:
        """Uncommitted changes to tracked files (untracked files don't count)."""
        return bool(self.text("status", "--porcelain", "--untracked-files=no"))

    def anything_to_save(self) -> bool:
        return bool(self.text("status", "--porcelain"))


def _who(member: str, name: str) -> tuple[str, str]:
    return (name or member or "Playground", f"{member or 'someone'}@playground.invalid")


# ════════════════════════════════════ merging ════════════════════════════════════

_MISSING: Any = object()
TIME_KEYS = {"updated", "ts", "at", "answered_at", "modified", "last_seen", "checked"}


@dataclass
class MergeResult:
    content: bytes | None
    extra: dict[str, bytes] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def merge_file(path: str, base: bytes | None, ours: bytes | None, theirs: bytes | None) -> MergeResult:
    """Bring two changed copies of one ``.playground/`` file together. Never fails."""
    if ours == theirs:
        return MergeResult(ours)
    if base == ours:
        return MergeResult(theirs)
    if base == theirs:
        return MergeResult(ours)
    if ours is None or theirs is None:
        # Removed on one side and changed on the other: keep the change.
        return MergeResult(ours if ours is not None else theirs)
    name = path.rsplit("/", 1)[-1]
    try:
        if name.endswith(".jsonl"):
            return MergeResult(merge_jsonl(base, ours, theirs))
        if path == "vault.json":
            return merge_vault(base, ours, theirs)
        if path == "decisions.md":
            return MergeResult(merge_append_text(base, ours, theirs))
        if name.endswith(".md"):
            return MergeResult(merge_markdown(base, ours, theirs))
        if name.endswith(".toml"):
            return MergeResult(merge_toml(base, ours, theirs))
        if name.endswith(".json"):
            return MergeResult(merge_json(base, ours, theirs))
        if _is_text(ours) and _is_text(theirs) and _is_text(base or b""):
            return MergeResult(merge_text(base, ours, theirs))
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        log.info("merging %s field by field failed (%s); keeping both", path, exc)
        if _is_text(ours) and _is_text(theirs) and _is_text(base or b""):
            return MergeResult(merge_text(base, ours, theirs))
    return merge_binary(path, ours, theirs)


def merge_jsonl(base: bytes | None, ours: bytes, theirs: bytes) -> bytes:
    """Every line from both sides, each as many times as the side that has it most."""
    mine = _jsonl_lines(ours)
    have = Counter(mine)
    seen: Counter = Counter()
    out = list(mine)
    for line in _jsonl_lines(theirs):
        seen[line] += 1
        if seen[line] > have[line]:
            out.append(line)
    return b"".join(line + b"\n" for line in out)


def merge_binary(path: str, ours: bytes, theirs: bytes) -> MergeResult:
    """Keep both. The copy with the larger hash keeps the name, so both sides agree."""
    keep, other = sorted((ours, theirs), key=lambda d: hashlib.sha256(d).hexdigest(), reverse=True)
    stem, dot, suffix = path.rpartition(".")
    if not dot or "/" in suffix:
        stem, suffix = path, ""
    alt = f"{stem}.other-{hashlib.sha256(other).hexdigest()[:8]}{'.' + suffix if suffix else ''}"
    return MergeResult(keep, {alt: other}, [f"Two versions of {path} were kept"])


def _merge3(base: list, a: list, b: list):
    """diff3 regions as (kind, base_part, a_part, b_part); kind is same, a, b or conflict."""
    am = [m for m in difflib.SequenceMatcher(None, base, a, autojunk=False).get_matching_blocks() if m.size]
    bm = [m for m in difflib.SequenceMatcher(None, base, b, autojunk=False).get_matching_blocks() if m.size]
    regions = []
    ia = ib = 0
    while ia < len(am) and ib < len(bm):
        abase, amatch, alen = am[ia]
        bbase, bmatch, blen = bm[ib]
        lo, hi = max(abase, bbase), min(abase + alen, bbase + blen)
        if lo < hi:
            regions.append((lo, hi, amatch + lo - abase, amatch + hi - abase, bmatch + lo - bbase, bmatch + hi - bbase))
        if abase + alen < bbase + blen:
            ia += 1
        else:
            ib += 1
    regions.append((len(base), len(base), len(a), len(a), len(b), len(b)))
    iz = ja = jb = 0
    for zs, ze, as_, ae, bs, be in regions:
        base_part, a_part, b_part = base[iz:zs], a[ja:as_], b[jb:bs]
        if a_part or b_part or base_part:
            if a_part == b_part:
                yield ("same", base_part, a_part, b_part)
            elif a_part == base_part:
                yield ("b", base_part, a_part, b_part)
            elif b_part == base_part:
                yield ("a", base_part, a_part, b_part)
            else:
                yield ("conflict", base_part, a_part, b_part)
        if ze > zs:
            yield ("same", base[zs:ze], a[as_:ae], b[bs:be])
        iz, ja, jb = ze, ae, be


_LIST_MARK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_BOX_MARK = re.compile(r"^\[[ xX~-]\]\s*")
_CHECK_MARK = re.compile(r"^check\s*(?:\([a-z]+\))?\s*:", re.IGNORECASE)


def entry_identity(line: str) -> str:
    """What a line is *about*: a story stays the same story when it is ticked."""
    text = _LIST_MARK.sub("", line.strip())
    text = _BOX_MARK.sub("", text)
    text = _CHECK_MARK.sub("check:", text)
    return " ".join(text.lower().split())


def _merge_lines(base: list[str], ours: list[str], theirs: list[str], prefer_ours: bool,
                 identity: Callable[[str], str] = lambda s: s.strip()) -> list[str]:
    entries = _merge_entries(base, ours, theirs, prefer_ours)
    if entries is not None:
        return entries
    out: list[str] = []
    for kind, base_part, ours_part, theirs_part in _merge3(base, ours, theirs):
        if kind != "conflict":
            out.extend(theirs_part if kind == "b" else ours_part)
            continue
        win, lose = (ours_part, theirs_part) if prefer_ours else (theirs_part, ours_part)
        out.extend(win)
        have = {identity(x) for x in win} | {identity(x) for x in base_part}
        for line in lose:
            key = identity(line)
            if line.strip() and key not in have:
                out.append(line)
                have.add(key)
    return out


def _merge_entries(base: list[str], ours: list[str], theirs: list[str], prefer_ours: bool) -> list[str] | None:
    """Merge list-like text entry by entry (a story is the same story when ticked).

    Order comes from a diff3 of the entries' identities; each entry's text from
    a three-way choice. Returns None when the text isn't list-like (blank lines
    or repeated entries), so the caller merges line by line instead.
    """
    every = base + ours + theirs
    if not every or any(not line.strip() for line in every):
        return None
    bi, oi, ti = ([entry_identity(x) for x in side] for side in (base, ours, theirs))
    if any(len(set(ids)) != len(ids) for ids in (bi, oi, ti)):
        return None
    bm, om, tm = dict(zip(bi, base)), dict(zip(oi, ours)), dict(zip(ti, theirs))
    order: list[str] = []
    for kind, bp, op, tp in _merge3(bi, oi, ti):
        if kind == "same":
            order += op
        elif kind in ("a", "b"):
            changed, other = (op, tm) if kind == "a" else (tp, om)
            order += changed
            # dropped here, but the other side edited it: keep the edit
            order += [k for k in bp if k not in changed and k in other and other[k] != bm[k]]
        else:
            win, lose, lose_map = (op, tp, tm) if prefer_ours else (tp, op, om)
            order += win
            order += [k for k in lose if k not in win and not (k in bm and lose_map[k] == bm[k])]
    out, seen = [], set()
    for k in order:
        if k in seen:
            continue
        seen.add(k)
        b_line, o_line, t_line = bm.get(k), om.get(k), tm.get(k)
        if o_line is None or t_line is None:
            out.append(o_line if t_line is None else t_line)
        elif o_line == t_line or b_line == t_line:
            out.append(o_line)
        elif b_line == o_line:
            out.append(t_line)
        else:
            out.append(o_line if prefer_ours else t_line)
    return out


def _prefer_ours(our_stamp: Any, their_stamp: Any, ours: bytes, theirs: bytes) -> bool:
    """Deterministic and symmetric: the newer stamp wins, then the larger content."""
    return (str(our_stamp or ""), ours) >= (str(their_stamp or ""), theirs)


def merge_text(base: bytes | None, ours: bytes, theirs: bytes) -> bytes:
    prefer = _prefer_ours("", "", ours, theirs)
    lines = _merge_lines((base or b"").decode().splitlines(), ours.decode().splitlines(),
                         theirs.decode().splitlines(), prefer)
    return ("\n".join(lines) + "\n").encode() if lines else b""


def merge_append_text(base: bytes | None, ours: bytes, theirs: bytes) -> bytes:
    """Like git's union driver: where both added, keep both blocks, newest-looking first."""
    prefer = _prefer_ours("", "", ours, theirs)
    out: list[str] = []
    for kind, _base_part, ours_part, theirs_part in _merge3((base or b"").decode().splitlines(),
                                                            ours.decode().splitlines(), theirs.decode().splitlines()):
        if kind != "conflict":
            out.extend(theirs_part if kind == "b" else ours_part)
            continue
        win, lose = (ours_part, theirs_part) if prefer else (theirs_part, ours_part)
        out.extend(win)
        if "\n".join(lose).strip() and "\n".join(lose).strip() not in "\n".join(win):
            if out and out[-1].strip():
                out.append("")
            out.extend(lose)
    return ("\n".join(out).rstrip("\n") + "\n").encode()


def _merge_fields(base: dict, ours: dict, theirs: dict, prefer_ours: bool) -> dict:
    out: dict[str, Any] = {}
    for key in list(ours) + [k for k in theirs if k not in ours]:
        value = _merge_scalarish(base.get(key, _MISSING), ours.get(key, _MISSING), theirs.get(key, _MISSING),
                                 prefer_ours, key)
        if value is not _MISSING:
            out[key] = value
    return out


def _merge_scalarish(b: Any, o: Any, t: Any, prefer_ours: bool, key: str) -> Any:
    if o == t:
        return o
    if b == o:
        return t
    if b == t:
        return o
    if o is _MISSING or t is _MISSING:
        return o if t is _MISSING else t
    if isinstance(o, list) and isinstance(t, list):
        bl = b if isinstance(b, list) else []
        return [x for x in _union_list(bl, o, t)]
    if key in TIME_KEYS:
        return max(o, t, key=str)
    return o if prefer_ours else t


def _union_list(base: list, ours: list, theirs: list) -> list:
    out: list = []
    for item in ours + theirs:
        if item in out:
            continue
        if item in base and (item not in ours or item not in theirs):
            continue          # one side removed it
        out.append(item)
    return out


def _sections_keyed(sections: list[tuple[str, str]]) -> list[tuple[tuple[str, int], str, str]]:
    counts: Counter = Counter()
    out = []
    for heading, text in sections:
        norm = heading.strip().lower()
        out.append(((norm, counts[norm]), heading, text))
        counts[norm] += 1
    return out


def merge_markdown(base: bytes | None, ours: bytes, theirs: bytes) -> bytes:
    """Front matter field by field, then each ``## `` section line by line."""
    b_fields, b_body = parse_front_matter((base or b"").decode())
    o_fields, o_body = parse_front_matter(ours.decode())
    t_fields, t_body = parse_front_matter(theirs.decode())
    prefer = _prefer_ours(o_fields.get("updated"), t_fields.get("updated"), ours, theirs)
    fields = _merge_fields(b_fields, o_fields, t_fields, prefer)
    b_pre, b_secs = parse_sections(b_body)
    o_pre, o_secs = parse_sections(o_body)
    t_pre, t_secs = parse_sections(t_body)
    preamble = "\n".join(_merge_lines(b_pre.splitlines(), o_pre.splitlines(), t_pre.splitlines(), prefer,
                                      entry_identity))
    bk = {k: text for k, _h, text in _sections_keyed(b_secs)}
    ok = _sections_keyed(o_secs)
    tk = _sections_keyed(t_secs)
    o_map = {k: (h, text) for k, h, text in ok}
    t_map = {k: (h, text) for k, h, text in tk}
    order = [k for k, _h, _t in ok]
    for index, (k, _h, _t) in enumerate(tk):
        if k in order:
            continue
        before = next((tk[j][0] for j in range(index - 1, -1, -1) if tk[j][0] in order), None)
        order.insert(order.index(before) + 1 if before is not None else len(order), k)
    sections: list[tuple[str, str]] = []
    for k in order:
        bv = bk.get(k, _MISSING)
        ov = o_map[k][1] if k in o_map else _MISSING
        tv = t_map[k][1] if k in t_map else _MISSING
        heading = (o_map.get(k) or t_map.get(k))[0]
        if ov == tv:
            text = ov
        elif bv == ov:
            text = tv
        elif bv == tv:
            text = ov
        elif ov is _MISSING or tv is _MISSING:
            text = ov if tv is _MISSING else tv
        else:
            text = "\n".join(_merge_lines((bv if bv is not _MISSING else "").splitlines(), ov.splitlines(),
                                          tv.splitlines(), prefer, entry_identity))
        if text is not _MISSING:
            sections.append((heading, text))
    body = dump_sections(preamble, sections)
    if fields or o_fields or t_fields:
        return (dump_front_matter(fields) + "\n" + body).encode()
    return body.encode()


def _record_stamp(record: dict) -> str:
    for key in ("updated", "ts", "at", "answered_at", "modified"):
        if record.get(key):
            return str(record[key])
    return ""


def _list_key(items: list) -> str | None:
    if not items or not all(isinstance(x, dict) for x in items):
        return None
    for key in ("id", "name", "number"):
        if all(key in x for x in items):
            return key
    return None


def merge_value(b: Any, o: Any, t: Any, prefer_ours: bool = True, path: tuple = ()) -> Any:
    """Three-way merge of parsed JSON or TOML. ``_MISSING`` marks an absent value."""
    if o == t:
        return o
    if b == o:
        return t
    if b == t:
        return o
    if o is _MISSING or t is _MISSING:
        return o if t is _MISSING else t
    if isinstance(o, dict) and isinstance(t, dict):
        bd = b if isinstance(b, dict) else {}
        stamp_o, stamp_t = _record_stamp(o), _record_stamp(t)
        prefer = (stamp_o, _canonical(o)) >= (stamp_t, _canonical(t)) if (stamp_o or stamp_t) else prefer_ours
        out = {}
        for key in list(o) + [k for k in t if k not in o]:
            value = merge_value(bd.get(key, _MISSING), o.get(key, _MISSING), t.get(key, _MISSING), prefer,
                                path + (key,))
            if value is not _MISSING:
                out[key] = value
        return out
    if isinstance(o, list) and isinstance(t, list):
        bl = b if isinstance(b, list) else []
        key = _list_key(bl + o + t)
        if key:
            return _merge_keyed(bl, o, t, key, prefer_ours, path)
        return _union_list(bl, o, t)
    if path and path[-1] in TIME_KEYS:
        return max(o, t, key=str)
    return o if prefer_ours else t


def _merge_keyed(base: list, ours: list, theirs: list, key: str, prefer_ours: bool, path: tuple) -> list:
    bmap = {x[key]: x for x in base}
    omap = {x[key]: x for x in ours}
    tmap = {x[key]: x for x in theirs}
    order = list(omap) + [k for k in tmap if k not in omap]
    removal_wins = bool(path) and path[-1] == "members"
    out = []
    for k in order:
        bv, ov, tv = bmap.get(k, _MISSING), omap.get(k, _MISSING), tmap.get(k, _MISSING)
        if ov is _MISSING or tv is _MISSING:
            present = ov if tv is _MISSING else tv
            if bv is _MISSING:
                out.append(present)
            elif not removal_wins and present != bv:
                out.append(present)       # removed on one side, changed on the other: keep the change
            continue
        out.append(merge_value(bv, ov, tv, prefer_ours, path + (str(k),)))
    return out


def _toml_header(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.startswith("#"):
            lines.append(line)
        else:
            break
    return "\n".join(lines) + "\n" if lines else ""


def merge_toml(base: bytes | None, ours: bytes, theirs: bytes) -> bytes:
    b = tomllib.loads(base.decode()) if base else {}
    o = tomllib.loads(ours.decode())
    t = tomllib.loads(theirs.decode())
    prefer = _prefer_ours("", "", ours, theirs)
    merged = merge_value(b, o, t, prefer)
    return (_toml_header(ours.decode()) + dump_toml(merged)).encode()


def merge_json(base: bytes | None, ours: bytes, theirs: bytes) -> bytes:
    b = json.loads(base.decode()) if base else _MISSING
    o = json.loads(ours.decode())
    t = json.loads(theirs.decode())
    merged = merge_value(b, o, t, _prefer_ours("", "", ours, theirs))
    return (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode()


def merge_vault(base: bytes | None, ours: bytes, theirs: bytes) -> MergeResult:
    """The vault's signed roster is a log: keep the longer valid one; each secret's newest version."""
    o = json.loads(ours.decode())
    t = json.loads(theirs.decode())
    b = json.loads(base.decode()) if base else {}
    ro, rt = o.get("roster") or [], t.get("roster") or []

    def prefix(short: list, long: list) -> bool:
        return len(short) <= len(long) and all(_canonical(x) == _canonical(y) for x, y in zip(short, long))

    notes = []
    if prefix(ro, rt):
        roster = rt
    elif prefix(rt, ro):
        roster = ro
    else:
        roster = max((ro, rt), key=lambda r: (len(r), hashlib.sha256(_canonical(r[-1] if r else {})).hexdigest()))
        notes.append("vault-fork")
    secrets_out: dict[str, Any] = {}
    bs, os_, ts = b.get("secrets") or {}, o.get("secrets") or {}, t.get("secrets") or {}
    for name in sorted(set(os_) | set(ts)):
        oe, te, be = os_.get(name), ts.get(name), bs.get(name)
        if oe == te:
            entry = oe
        elif oe is None or te is None:
            present = oe if te is None else te
            if be is not None and _canonical(be) == _canonical(present):
                continue                       # deleted on one side, untouched on the other
            entry = present
        else:
            entry = max((oe, te), key=lambda e: (int(e.get("version") or 0), str(e.get("sig") or "")))
        secrets_out[name] = entry
    members = set(((roster[-1].get("body") or {}).get("members") or {}).keys()) if roster else set()
    if any(set((e.get("wraps") or {}).keys()) != members for e in secrets_out.values()):
        notes.append("vault-rewrap")
    merged = dict(o)
    merged["roster"] = roster
    merged["secrets"] = secrets_out
    return MergeResult((json.dumps(merged, indent=2, sort_keys=True) + "\n").encode(), {}, notes)


# ════════════════════════════════════ signatures and codes ════════════════════════════════════

CODE_PREFIX = {"invite": "PGI1-", "join": "PGJ1-", "approval": "PGA1-", "renew": "PGR1-", "passes": "PGP1-"}
FILE_SUFFIX = {"invite": ".playground-invite", "join": ".playground-request", "approval": ".playground-approval",
               "renew": ".playground-renewal", "passes": ".playground-passes"}
_MAX_CODE_BYTES = 256 * 1024


def _keygen() -> str:
    tool = shutil.which("ssh-keygen")
    if not tool:
        raise SyncError("Sharing needs OpenSSH's ssh-keygen, which isn't on this computer.")
    return tool


def ssh_sign(private_key: Path, namespace: str, data: bytes, env: dict[str, str] | None = None) -> str:
    """An SSHSIG signature (``ssh-keygen -Y sign``), armored text."""
    with tempfile.TemporaryDirectory(prefix="pg-sign-") as tmp:
        message = Path(tmp) / "message"
        message.write_bytes(data)
        proc = subprocess.run([_keygen(), "-q", "-Y", "sign", "-f", str(private_key), "-n", namespace, str(message)],
                              capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                              env={**os.environ, **(env or {})})
        signature = Path(f"{message}.sig")
        if proc.returncode != 0 or not signature.exists():
            raise SyncError("Couldn't sign that with this computer's key: "
                            + ((proc.stderr or "").strip().splitlines() or ["no detail"])[-1])
        return signature.read_text()


def ssh_verify(public_key: str | Iterable[str], namespace: str, data: bytes, signature: str) -> bool:
    """Was ``data`` signed (SSHSIG, ``namespace``) by this key, or by any one of these keys?"""
    keys = [public_key] if isinstance(public_key, str) else list(public_key)
    clean = []
    for key in keys:
        try:
            clean.append(security.normalize_public_key(key))
        except security.SecurityError:
            continue
    if not clean or not isinstance(signature, str) or "BEGIN SSH SIGNATURE" not in signature:
        return False
    with tempfile.TemporaryDirectory(prefix="pg-verify-") as tmp:
        allowed = Path(tmp) / "allowed"
        allowed.write_text("".join(f'signer namespaces="{namespace}" {key}\n' for key in clean))
        sig = Path(tmp) / "sig"
        sig.write_text(signature)
        proc = subprocess.run([_keygen(), "-Y", "verify", "-f", str(allowed), "-I", "signer", "-n", namespace,
                               "-s", str(sig)], input=data, capture_output=True, timeout=30)
        return proc.returncode == 0


def signature_key(signature: str) -> str:
    """The public key inside an SSHSIG signature (who claims to have signed it), or ""."""
    try:
        body = "".join(line for line in signature.strip().splitlines() if not line.startswith("-----"))
        raw = base64.b64decode(body)
    except (ValueError, AttributeError):
        return ""
    if not raw.startswith(b"SSHSIG") or len(raw) < 14:
        return ""
    at = 10                                       # "SSHSIG" + uint32 version
    length = int.from_bytes(raw[at:at + 4], "big")
    blob = raw[at + 4:at + 4 + length]
    if len(blob) != length or length < 8:
        return ""
    kind_len = int.from_bytes(blob[:4], "big")
    kind = blob[4:4 + kind_len].decode(errors="replace")
    try:
        return security.normalize_public_key(f"{kind} {base64.b64encode(blob).decode()}")
    except security.SecurityError:
        return ""


def encode_code(kind: str, envelope: dict) -> str:
    raw = _canonical(envelope)
    return CODE_PREFIX[kind] + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")


def decode_code(kind: str, text: str) -> dict:
    """A code (``PGI1-…``) or the contents of a saved file, as the signed envelope."""
    text = (text or "").strip()
    words = {"invite": "an invite", "join": "a request to join", "approval": "a welcome code",
             "renew": "a request for a fresh pass", "passes": "a fresh pass"}[kind]
    try:
        if text.startswith("{"):
            envelope = json.loads(text)
        else:
            compact = "".join(text.split())
            prefix = CODE_PREFIX[kind]
            if not compact.startswith(prefix):
                raise ValueError("wrong prefix")
            body = compact[len(prefix):]
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            inflater = zlib.decompressobj()
            data = inflater.decompress(raw, _MAX_CODE_BYTES)
            if inflater.unconsumed_tail:
                raise ValueError("too big")
            envelope = json.loads(data)
    except (ValueError, zlib.error) as exc:
        raise InviteError(f"That isn't {words} Playground can read. Copy the whole code and try again.") from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("body"), dict) \
            or envelope["body"].get("kind") != kind or not isinstance(envelope.get("sig"), str):
        raise InviteError(f"That isn't {words}.")
    return envelope


def safety_code(ca_public_key: str) -> str:
    """Twelve digits two people can read to each other to know it's the same project."""
    digest = hashlib.sha256(security.normalize_public_key(ca_public_key).encode()).digest()
    number = int.from_bytes(digest[:8], "big") % 10**12
    text = f"{number:012d}"
    return " ".join(text[i:i + 4] for i in range(0, 12, 4))


# ════════════════════════════════════ this device ════════════════════════════════════

@dataclass
class Pin:
    """What this device trusts about one shared project. Per device, never synced.

    Set from the owner's own share, or from a signed invite and welcome. A
    newer access list signed by the same CA can move the home; nothing in the
    synced project folder can.
    """
    project_id: str
    member: str
    role: str = "builder"
    mode: str = "server"
    name: str = ""
    ca: str = ""
    home: str = ""                 # the home folder on the home machine; in folder mode, the bare repo itself
    user: str = ""
    port: int = 22
    hosts: list[str] = field(default_factory=list)
    host_key: str = ""
    host_name: str = ""
    owner_device: str = ""
    owner_name: str = ""
    access_version: int = 0
    synced_once: bool = False
    trust: str = "account"         # "live" (root-owned checks on the home) or "account" (see TRUST_MODES)
    gate: str = ""                 # what a sync pass runs on the home; empty means <home>/gate
    issuers: list[str] = field(default_factory=list)   # owner computers the project trusts to hand out passes
    access_expires: float = 0.0    # when the newest access list this device has checked runs out

    @property
    def repo_path(self) -> str:
        return self.home if self.mode == "folder" else f"{self.home.rstrip('/')}/repo.git"

    @property
    def gate_path(self) -> str:
        return self.gate or f"{self.home.rstrip('/')}/gate"

    @property
    def ca_fingerprint(self) -> str:
        return security.fingerprint(self.ca) if self.ca else ""

    def trusted_signers(self) -> list[str]:
        """Fingerprints whose passes this device accepts: the project's own key, and owner computers it trusts."""
        return [fp for fp in [self.ca_fingerprint, *self.issuers] if fp]

    def home_record(self) -> dict:
        return {"mode": self.mode, "home": self.home, "user": self.user, "port": self.port, "hosts": list(self.hosts),
                "host_key": self.host_key, "host_name": self.host_name, "owner_device": self.owner_device,
                "owner_name": self.owner_name, "trust": self.trust, "gate": self.gate}

    @staticmethod
    def path(data_dir: Path, project_id: str) -> Path:
        return Path(data_dir) / "sync" / security.require_safe_id(project_id, "project id") / "pin.json"

    def save(self, data_dir: Path) -> None:
        security.write_private(self.path(data_dir, self.project_id), json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, data_dir: Path, project_id: str) -> "Pin | None":
        try:
            data = read_json(cls.path(data_dir, project_id))
        except (security.SecurityError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def _validate_home(record: dict) -> dict:
    mode = str(record.get("mode") or "")
    if mode not in MODES:
        raise SyncError("That project home is of a kind this Playground doesn't know.")
    home = str(record.get("home") or "")
    if not re.match(r"^/[A-Za-z0-9_./ ~+@-]{1,400}$", home) or ".." in home.split("/"):
        raise SyncError("The project home's folder has characters Playground can't use safely.")
    if mode != "folder":
        if not re.match(r"^[A-Za-z0-9_./~+@-]+$", home):
            raise SyncError("The project home's folder can't have spaces in it.")
        from . import places
        places.validate_user(str(record.get("user") or ""))
        places.validate_port(record.get("port") or 22)
        hosts = [str(h) for h in record.get("hosts") or []]
        if not hosts:
            raise SyncError("The invite doesn't say where the project home is.")
        for host in hosts:
            places.validate_host(host)
        if record.get("host_key"):
            security.normalize_public_key(str(record["host_key"]))
    if str(record.get("trust") or "account") not in TRUST_MODES:
        raise SyncError("That project home is of a kind this Playground doesn't know.")
    if record.get("gate") and not re.match(r"^/[A-Za-z0-9_./+@-]{1,400}$", str(record["gate"])):
        raise SyncError("The project home's gate has characters Playground can't use safely.")
    return record


def _device(data_dir: Path) -> security.DeviceKey | None:
    """This device's key from a given data folder (doesn't create one)."""
    directory = Path(data_dir) / "keys"
    private, public = directory / "device_ed25519", directory / "device_ed25519.pub"
    if not private.exists() or not public.exists():
        return None
    meta = read_json(directory / "device.json") or {}
    key = security.normalize_public_key(public.read_text())
    return security.DeviceKey(private_path=private, public_key=key, fingerprint=security.fingerprint(key),
                              device_name=str(meta.get("device") or "device"),
                              connect_id=str(meta.get("connect_id") or ""),
                              protection=str(meta.get("protection") or "file-permissions"),
                              created=str(meta.get("created") or ""))


def sync_cert_path(device: security.DeviceKey, project_id: str) -> Path:
    return device.dir / "certs" / f"{security.require_safe_id(project_id, 'project id')}-sync-cert.pub"


def sync_principal(project_id: str) -> str:
    return security.sync_principal(project_id)


def _key_tag(public_key: str) -> str:
    blob = base64.b64decode(security.normalize_public_key(public_key).split()[1])
    return hashlib.sha256(blob).hexdigest()[:16]


def _local_marker(project_dir: Path) -> Path:
    return Path(project_dir) / ".playground" / "local" / "sync.json"


def _write_marker(project_dir: Path, project_id: str, member: str) -> None:
    path = _local_marker(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes(path, (json.dumps({"project_id": project_id, "member": member}, indent=2) + "\n").encode())


# ════════════════════════════════════ the access list ════════════════════════════════════

ACCESS_FORMAT = "playground-access/2"
ISSUERS_FORMAT = "playground-issuers/1"
_FP_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{20,}$")


@dataclass(frozen=True)
class Seat:
    """One computer that may sync: whose it is, their role, and its public key (never a private one)."""
    member: str
    role: str
    fingerprint: str
    public_key: str
    device: str = "device"


def _device_label(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]+", "-", str(name or "device")).strip("-")[:40] or "device"


def access_text(project_id: str, version: int, pin: Pin, seats: Iterable[Seat], *, issued: float, expires: float,
                issuers_version: int = 0) -> str:
    """The signed access list: who may sync from which computer, where the home is, and until when it counts.

    The home refuses it once ``expires`` has passed (an owner's Playground signs a fresh one long before), so
    an old list someone puts back stops working by itself.
    """
    lines = [f"# {ACCESS_FORMAT}", f"project {project_id}", f"version {int(version)}", f"issued {int(issued)}",
             f"expires {int(expires)}", f"issuers {int(issuers_version)}", f"trust {pin.trust}",
             f"home {pin.mode} {pin.user or '-'} {int(pin.port or 22)} {pin.home}"]
    if pin.mode != "folder":
        lines.append(f"gate {pin.gate_path}")
    lines += [f"host {h}" for h in pin.hosts]
    if pin.host_key:
        lines.append(f"hostkey {security.normalize_public_key(pin.host_key)}")
    if pin.owner_device:
        lines.append(f"ownerdevice {pin.owner_device}")
    for seat in sorted(set(seats), key=lambda s: (s.member, s.fingerprint)):
        security.require_safe_id(seat.member, "member id")
        key = security.normalize_public_key(seat.public_key)
        if seat.role not in security.ROLE_ACCESS or not security.same_secret(security.fingerprint(key),
                                                                              seat.fingerprint):
            raise SyncError("The access list has an entry Playground can't write safely.")
        lines.append(f"member {seat.member} {seat.role} {seat.fingerprint} {key} {_device_label(seat.device)}")
    return "\n".join(lines) + "\n"


def parse_access(text: str) -> dict:
    out: dict[str, Any] = {"project": "", "version": 0, "members": [], "seats": [], "hosts": [], "home": {},
                           "issued": 0, "expires": 0, "issuers_version": 0, "trust": "account", "gate": "",
                           "format": ""}
    for line in text.splitlines():
        words = line.split()
        if not words:
            continue
        if words[0] == "#" and len(words) == 2:
            out["format"] = words[1]
            continue
        if words[0].startswith("#"):
            continue
        if words[0] == "project" and len(words) == 2:
            out["project"] = words[1]
        elif words[0] in ("version", "issued", "expires") and len(words) == 2 and words[1].isdigit():
            out[words[0]] = int(words[1])
        elif words[0] == "issuers" and len(words) == 2 and words[1].isdigit():
            out["issuers_version"] = int(words[1])
        elif words[0] == "trust" and len(words) == 2 and words[1] in TRUST_MODES:
            out["trust"] = words[1]
        elif words[0] == "gate" and len(words) == 2:
            out["gate"] = words[1]
        elif words[0] == "home" and len(words) >= 5 and words[3].isdigit():
            out["home"] = {"mode": words[1], "user": "" if words[2] == "-" else words[2], "port": int(words[3]),
                           "home": " ".join(words[4:])}
        elif words[0] == "host" and len(words) == 2:
            out["hosts"].append(words[1])
        elif words[0] == "hostkey" and len(words) >= 3:
            out["host_key"] = f"{words[1]} {words[2]}"
        elif words[0] == "ownerdevice" and len(words) == 2:
            out["owner_device"] = words[1]
        elif words[0] == "member" and len(words) >= 4:
            out["members"].append((words[1], words[2], words[3]))
            if len(words) >= 6:
                try:
                    key = security.normalize_public_key(f"{words[4]} {words[5]}")
                except security.SecurityError:
                    continue
                if security.same_secret(security.fingerprint(key), words[3]):
                    out["seats"].append(Seat(words[1], words[2], words[3], key,
                                             words[6] if len(words) > 6 else "device"))
    return out


def issuers_text(project_id: str, version: int, seats: Iterable[Seat],
                 retired: Iterable[tuple[str, str, str, int]] = ()) -> str:
    """The owner computers the project trusts to sign access lists and hand out passes. Only the project's
    own key (on the computer that shared it) signs this list.

    ``retired``: (member, fingerprint, public key, until) for computers that stopped being an owner's. Until
    then, passes they signed still count (so nobody is locked out while fresh ones arrive); they can't sign
    an access list. A pass only ever works for a computer on the current access list, so this can't let
    anyone in.
    """
    lines = [f"# {ISSUERS_FORMAT}", f"project {project_id}", f"version {int(version)}"]
    for seat in sorted(set(seats), key=lambda s: (s.member, s.fingerprint)):
        lines.append(f"issuer {security.require_safe_id(seat.member, 'member id')} {seat.fingerprint} "
                     f"{security.normalize_public_key(seat.public_key)}")
    for member, fp, key, until in sorted(set(retired), key=lambda r: (r[0], r[1])):
        lines.append(f"retired {security.require_safe_id(member, 'member id')} {fp} "
                     f"{security.normalize_public_key(key)} {int(until)}")
    return "\n".join(lines) + "\n"


def parse_issuers(text: str) -> dict:
    out: dict[str, Any] = {"project": "", "version": 0, "issuers": [], "retired": []}
    for line in text.splitlines():
        words = line.split()
        if not words or words[0].startswith("#"):
            continue
        if words[0] == "project" and len(words) == 2:
            out["project"] = words[1]
        elif words[0] == "version" and len(words) == 2 and words[1].isdigit():
            out["version"] = int(words[1])
        elif words[0] in ("issuer", "retired") and len(words) >= 5:
            try:
                key = security.normalize_public_key(f"{words[3]} {words[4]}")
            except security.SecurityError:
                continue
            if not security.same_secret(security.fingerprint(key), words[2]):
                continue
            if words[0] == "issuer":
                out["issuers"].append((words[1], words[2], key))
            elif len(words) >= 6 and words[5].isdigit():
                out["retired"].append((words[1], words[2], key, int(words[5])))
    return out


def verify_access(ca: str, project_id: str, files: dict[str, bytes], now: float | None = None) -> tuple[str, dict]:
    """Check an access list the way the home does. Returns ``(state, parsed)``.

    ``state`` is "ok", "expired" (signed properly, but its time is up) or "bad" (not signed by the
    project's own key or an owner computer it trusts, or for another project). ``files`` holds
    ``access``, ``access.sig`` and, when owners' computers sign too, ``issuers`` and ``issuers.sig``.
    """
    now = time.time() if now is None else now
    text, sig = files.get("access"), files.get("access.sig")
    if not ca or not text or not sig:
        return "bad", {}
    signers = [ca]
    issuers: dict[str, Any] = {"version": 0, "issuers": [], "retired": []}
    if files.get("issuers"):
        if not ssh_verify(ca, "playground-issuers", files["issuers"], (files.get("issuers.sig") or b"").decode(
                errors="replace")):
            return "bad", {}
        issuers = parse_issuers(files["issuers"].decode(errors="replace"))
        if issuers["project"] != project_id:
            return "bad", {}
        signers += [key for _m, _fp, key in issuers["issuers"]]
    if not ssh_verify(signers, "playground-access", text, sig.decode(errors="replace")):
        return "bad", {}
    parsed = parse_access(text.decode(errors="replace"))
    if parsed["project"] != project_id or parsed["format"] != ACCESS_FORMAT \
            or parsed["issuers_version"] != issuers["version"]:
        return "bad", {}
    issued, expires = parsed["issued"], parsed["expires"]
    if not issued or not expires or expires - issued > ACCESS_MAX_S or issued - ACCESS_SKEW_S > now:
        return "bad", {}
    parsed["issuers"] = issuers["issuers"]
    parsed["retired"] = [r for r in issuers["retired"] if r[3] > now]
    parsed["signer"] = signature_key(sig.decode(errors="replace"))
    return ("ok" if now < expires else "expired"), parsed


def _access_files(repo: "_Repo", rev: str | None) -> dict[str, bytes]:
    if not rev:
        return {}
    tree = repo.ls_tree(rev)
    wanted = {name: entry[1] for name, entry in tree.items()
              if name in ("access", "access.sig", "issuers", "issuers.sig")}
    blobs = repo.read_blobs(wanted.values())
    return {name: blobs.get(sha, b"") for name, sha in wanted.items()}


# ════════════════════════════════════ scripts on the home ════════════════════════════════════

_VERIFY_FN = r'''
pg_git() {  # git on the project's repository; as its own account when we're root (sshd's key check)
  if [ -n "${PG_AS:-}" ]; then
    command -v runuser >/dev/null 2>&1 || return 1
    runuser -u "$PG_AS" -- env -i PATH="$PATH" HOME=/ GIT_CONFIG_NOSYSTEM=1 git --git-dir="$REPO" "$@"
  elif [ -n "${REPO:-}" ]; then
    git --git-dir="$REPO" "$@"
  else
    git "$@"
  fi
}
pg_blob() { pg_git cat-file blob "$1" 2>/dev/null; }
pg_num() {  # $1 rev, $2 file, $3 key: the number after the key in that file, or nothing
  pg_blob "$1:$2" | awk -v k="$3" '$1 == k && $2 ~ /^[0-9]+$/ { print $2; exit }'
}
verify_access() {  # $1: a rev with the access list. $2: where to copy it (and $2.issuers).
  # 0: signed by the project (or an owner computer it trusts) and current. 2: signed, but its time is up.
  # 1: anything else.
  t=$(mktemp -d 2>/dev/null) || return 1
  r=1
  if [ -n "$CA_PUB" ] && [ -r "$CA_PUB" ] && pg_blob "$1:access" > "$t/a" && pg_blob "$1:access.sig" > "$t/a.sig"; then
    root=$(cut -d' ' -f1,2 "$CA_PUB")
    printf 'playground-signer namespaces="playground-access" %s\n' "$root" > "$t/allowed"
    iv=0
    : > "$t/i"
    if pg_blob "$1:issuers" > "$t/i" && [ -s "$t/i" ]; then
      iv=-1
      printf 'playground-root namespaces="playground-issuers" %s\n' "$root" > "$t/root"
      if pg_blob "$1:issuers.sig" > "$t/i.sig" &&
         ssh-keygen -Y verify -f "$t/root" -I playground-root -n playground-issuers -s "$t/i.sig" < "$t/i" >/dev/null 2>&1 &&
         grep -qx "project $PROJECT" "$t/i"; then
        iv=$(awk '$1 == "version" && $2 ~ /^[0-9]+$/ { print $2; exit }' "$t/i")
        awk '$1 == "issuer" && NF >= 5 { print "playground-signer namespaces=\"playground-access\" " $4 " " $5 }' "$t/i" >> "$t/allowed"
      fi
    else
      : > "$t/i"
    fi
    if [ "${iv:--1}" != -1 ] &&
       ssh-keygen -Y verify -f "$t/allowed" -I playground-signer -n playground-access -s "$t/a.sig" < "$t/a" >/dev/null 2>&1 &&
       grep -qx "# @FORMAT@" "$t/a" && grep -qx "project $PROJECT" "$t/a" && grep -qx "issuers $iv" "$t/a"; then
      issued=$(awk '$1 == "issued" && $2 ~ /^[0-9]+$/ { print $2; exit }' "$t/a")
      expires=$(awk '$1 == "expires" && $2 ~ /^[0-9]+$/ { print $2; exit }' "$t/a")
      now=$(date +%s)
      if [ -n "$issued" ] && [ -n "$expires" ] && [ $((expires - issued)) -le @MAXVALID@ ] &&
         [ $((issued - @SKEW@)) -le "$now" ]; then
        if [ "$now" -lt "$expires" ]; then r=0; else r=2; fi
        if [ -n "${2:-}" ]; then cp "$t/a" "$2" && cp "$t/i" "$2.issuers" || r=1; fi
      fi
    fi
  fi
  rm -rf "$t"
  return $r
}
'''

GATE_SCRIPT = r'''#!/bin/sh
# Playground gate for project @PROJECT@.
# A sync pass runs this and nothing else. It checks the project's signed access list, then hands over to
# git for this project's repository only.
set -u
PROJECT='@PROJECT@'
CA_PUB='@CA_PUB@'
LIVE='@LIVE@'
if [ -n "$LIVE" ]; then
  # Installed by the root setup: settings and hooks are root's, so nobody with a shell here can change them.
  REPO=$(sed -n 's/^repo=//p' "$LIVE" | head -n 1)
  HOOKS=$(dirname "$LIVE")/hooks
  FOUNDER=$(sed -n 's/^founder=//p' "$LIVE" | head -n 1)
else
  H=$(cd "$(dirname "$0")" && pwd -P) || exit 1
  REPO="$H/repo.git"
  HOOKS=""
  FOUNDER=""
  [ "$CA_PUB" != home ] || CA_PUB="$H/ca.pub"
fi
member="${1:-}"
device="${2:-}"
deny() { echo "Playground: $1" >&2; exit 1; }
case "${SSH_ORIGINAL_COMMAND:-}" in
  "git-upload-pack "*|"git upload-pack "*) service=upload-pack ;;
  "git-receive-pack "*|"git receive-pack "*) service=receive-pack ;;
  *) deny "This pass is only for syncing the project." ;;
esac
[ -n "$REPO" ] && [ -d "$REPO" ] || deny "The project home isn't set up on this server yet."
@VERIFY@
list=$(mktemp 2>/dev/null) || deny "The project home couldn't check your access."
verify_access refs/playground/access "$list"
rc=$?
role=""
if [ "$rc" = 0 ] || [ "$rc" = 2 ]; then
  role=$(awk -v m="$member" -v d="$device" '$1 == "member" && $2 == m && $4 == d { print $3; exit }' "$list")
elif [ -n "$FOUNDER" ] && [ "$FOUNDER" = "$member $device" ]; then
  role=owner          # the computer that shared the project, before its first access list arrives
else
  rm -f "$list" "$list.issuers"
  deny "The project's access list isn't signed by its owner, so nobody can sync until an owner opens Playground."
fi
rm -f "$list" "$list.issuers"
[ -n "$role" ] || deny "This computer isn't on the project's access list."
stale=""
if [ "$rc" = 2 ]; then
  [ "$role" = owner ] || deny "The project's access list has run out. It renews by itself when an owner opens Playground."
  stale=1             # an owner may come in to send a fresh one, and nothing else
fi
export PLAYGROUND_MEMBER="$member" PLAYGROUND_ROLE="$role" PLAYGROUND_STALE="$stale"
if [ -n "$HOOKS" ]; then
  exec git -c core.hooksPath="$HOOKS" "$service" "$REPO"
fi
exec git "$service" "$REPO"
'''

PRE_RECEIVE_SCRIPT = r'''#!/bin/sh
# Playground: who may change what in project @PROJECT@.
member="${PLAYGROUND_MEMBER:-}"
role="${PLAYGROUND_ROLE:-}"
stale="${PLAYGROUND_STALE:-}"
PROJECT='@PROJECT@'
CA_PUB='@CA_PUB@'
REPO=$(cd "${GIT_DIR:-.}" && pwd -P)
[ "$CA_PUB" != home ] || CA_PUB="$(dirname "$REPO")/ca.pub"
fail() { echo "Playground: $1" >&2; exit 1; }
is_zero() { case "$1" in *[!0]*) return 1 ;; *) return 0 ;; esac; }
@VERIFY@
check_list() {  # $1 old, $2 new: a newer list, signed and current, with no older list of owners' computers
  is_zero "$2" && fail "The access list can't be deleted."
  verify_access "$2" "" || fail "That access list isn't signed by one of the project's owners, or its time is up."
  if ! is_zero "$1"; then
    o=$(pg_num "$1" access version); n=$(pg_num "$2" access version)
    [ "${n:-0}" -gt "${o:-0}" ] 2>/dev/null || fail "That access list is older than the one here."
    o=$(pg_num "$1" issuers version); n=$(pg_num "$2" issuers version)
    [ "${n:-0}" -ge "${o:-0}" ] 2>/dev/null || fail "That list of owners' computers is older than the one here."
  fi
}
while read -r old new ref; do
  if [ -z "$member" ]; then
    # The home's own account: an owner or builder with a shell, or the owner's own computer.
    if [ "$ref" = refs/playground/access ] && [ -n "$CA_PUB" ] && [ -r "$CA_PUB" ]; then
      check_list "$old" "$new"
    fi
    continue
  fi
  if [ -n "$stale" ]; then
    case "$ref" in
      refs/playground/access|refs/playground/certs) ;;
      *) fail "The project's access list has run out. Playground sends a fresh one first, then the rest." ;;
    esac
  fi
  case "$ref" in
    refs/playground/access)
      [ "$role" = owner ] || fail "Only an owner can change who's on the project."
      check_list "$old" "$new" ;;
    refs/playground/certs)
      [ "$role" = owner ] || fail "Only an owner can hand out passes." ;;
    refs/playground/presence/*)
      [ "$ref" = "refs/playground/presence/$member" ] || fail "You can only say where you are, not where others are."
      if ! is_zero "$new"; then
        size=$(git cat-file -s "$new" 2>/dev/null || echo 999999)
        [ "$size" -le 4096 ] || fail "That presence note is too big."
      fi ;;
    refs/playground/state)
      case "$role" in owner|builder|reviewer) ;; *) fail "Viewers can look but not change the project." ;; esac
      is_zero "$new" && fail "The project's records can't be deleted."
      is_zero "$old" || git merge-base --is-ancestor "$old" "$new" ||
        fail "Someone else synced first. Playground brings the changes together and tries again." ;;
    refs/tags/playground/restore/*)
      case "$role" in owner|builder) ;; *) fail "Only owners and builders save restore points." ;; esac
      case "$ref" in "refs/tags/playground/restore/$member/"*) ;; *) fail "You can only add your own restore points." ;; esac
      is_zero "$old" || fail "A restore point can't be changed once it's saved."
      is_zero "$new" && fail "A restore point can't be deleted." ;;
    refs/heads/*|refs/tags/*)
      case "$role" in owner|builder) ;; *) fail "Only owners and builders can change the code." ;; esac
      if is_zero "$new"; then
        [ "$ref" = refs/heads/main ] && fail "Main can't be deleted."
      elif ! is_zero "$old" && [ "${ref#refs/heads/}" != "$ref" ]; then
        git merge-base --is-ancestor "$old" "$new" ||
          fail "That would throw away someone's work on ${ref#refs/heads/}. Bring their changes in first."
      fi ;;
    *) fail "Playground doesn't use $ref." ;;
  esac
done
exit 0
'''

POST_RECEIVE_SCRIPT = r'''#!/bin/sh
# Playground: keep the server's checkout of main current.
REPO=$(pwd -P)
H=$(dirname "$REPO")
while read -r old new ref; do
  if [ "$ref" = refs/heads/main ] && [ -d "$H/checkout" ]; then
    case "$new" in *[!0]*)
      env -u GIT_DIR -u GIT_QUARANTINE_PATH git --git-dir="$REPO" --work-tree="$H/checkout" checkout -f -q main -- . 2>/dev/null ||
        echo "Playground: the server's copy of main couldn't be updated." >&2 ;;
    esac
  fi
done
exit 0
'''

KEYS_SCRIPT = r'''#!/bin/sh
# Playground: which project passes may sign in to this server, decided at every sign-in from each project's
# signed access list. sshd runs this as root (AuthorizedKeysCommand); it reads a project's repository as
# that project's account, and checks every signature itself. Someone taken off a project gets nothing here
# from the moment the new list reaches the server. Installed by Playground's one-time setup.
set -u
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/homebrew/bin
export PATH
umask 077
D='@TRUST@'
user="${1:-}"
type="${2:-}"
fp="${3:-}"
key="${4:-}"
case "$type" in ssh-ed25519-cert-v01@openssh.com|sk-ssh-ed25519-cert-v01@openssh.com) ;; *) exit 0 ;; esac
case "$user" in ''|*[!A-Za-z0-9_.-]*) exit 0 ;; esac
case "$fp" in SHA256:*) ;; *) exit 0 ;; esac
case "$fp" in *[!A-Za-z0-9:+/]*) exit 0 ;; esac
case "$key" in ''|*[!A-Za-z0-9+/=]*) exit 0 ;; esac
@VERIFY@
T=$(mktemp -d 2>/dev/null) || exit 0
trap 'rm -rf "$T"' EXIT
printf '%s %s\n' "$type" "$key" > "$T/pass.pub"
ssh-keygen -L -f "$T/pass.pub" > "$T/pass" 2>/dev/null || exit 0
signer=$(awk '$1 == "Signing" && $2 == "CA:" { print $4; exit }' "$T/pass")
case "$signer" in SHA256:*) ;; *) exit 0 ;; esac
forced=$(awk '$1 == "force-command" { $1 = ""; sub(/^ +/, ""); print; exit }' "$T/pass")
for P in "$D"/projects/*; do
  [ -f "$P/live" ] || continue
  [ "$(sed -n 's/^account=//p' "$P/live" | head -n 1)" = "$user" ] || continue
  PROJECT=$(basename "$P")
  REPO=$(sed -n 's/^repo=//p' "$P/live" | head -n 1)
  PREFIX=$(sed -n 's/^prefix=//p' "$P/live" | head -n 1)
  FOUNDER=$(sed -n 's/^founder=//p' "$P/live" | head -n 1)
  CA_PUB="$P/ca.pub"
  GATE="$P/gate"
  [ -n "$REPO" ] && [ -n "$PREFIX" ] && [ -r "$CA_PUB" ] || continue
  PG_AS=""
  [ "$(id -u)" != 0 ] || PG_AS="$user"
  rootfp=$(ssh-keygen -lf "$CA_PUB" 2>/dev/null | awk '{ print $2; exit }')
  rm -f "$T/list" "$T/list.issuers"
  verify_access refs/playground/access "$T/list"
  rc=$?
  # Who signed this pass: the project's own key, or an owner's computer the project's own key lists (a pass
  # only ever works for a computer on the current list, with that list's role, so a signer can't let anyone in).
  issuer=""
  now=$(date +%s)
  if [ "$signer" = "$rootfp" ]; then
    issuer=$(cut -d' ' -f1,2 "$CA_PUB")
  elif { [ "$rc" = 0 ] || [ "$rc" = 2 ]; } && [ -f "$T/list.issuers" ]; then
    issuer=$(awk -v f="$signer" -v now="$now" '$3 == f && NF >= 5 && ($1 == "issuer" || ($1 == "retired" && $6 + 0 > now + 0)) { print $4 " " $5; exit }' "$T/list.issuers")
  fi
  [ -n "$issuer" ] || continue
  # A sync pass goes to the gate, which reads the list again and says why when it refuses (taken off, or the
  # list ran out). Only a pass whose forced command is exactly this project's gate, for this very key.
  set -f
  set -- $forced
  set +f
  if [ "$#" = 3 ] && [ "$1" = "$GATE" ] && [ "$3" = "$fp" ]; then
    case "$2" in
      ''|*[!A-Za-z0-9._-]*) ;;
      *) printf 'cert-authority,principals="%s-sync",restrict,command="%s %s %s" %s\n' "$PREFIX" "$GATE" "$2" "$fp" "$issuer" ;;
    esac
  fi
  # A shell: only for a builder's or owner's computer on the current list, and only while the list is current.
  role=""
  if [ "$rc" = 0 ]; then
    role=$(awk -v f="$fp" '$1 == "member" && $4 == f { print $3; exit }' "$T/list")
  elif [ "$rc" = 1 ] && [ -n "$FOUNDER" ] && [ "${FOUNDER#* }" = "$fp" ] && [ "$signer" = "$rootfp" ]; then
    role=owner                 # the computer that shared the project, before its first access list arrives
  fi
  case "$role" in
    owner) shells="$PREFIX-development,$PREFIX-preview,$PREFIX-production" ;;
    builder) shells="$PREFIX-development,$PREFIX-preview" ;;
    *) shells="" ;;
  esac
  [ -z "$shells" ] || printf 'cert-authority,principals="%s",no-agent-forwarding,no-X11-forwarding,no-user-rc %s\n' "$shells" "$issuer"
done
exit 0
'''


def _fill_script(text: str, project_id: str, ca_pub_path: str, live: str = "", trust_dir: str = "") -> str:
    return (text.replace("@VERIFY@", _VERIFY_FN.strip()).replace("@PROJECT@", project_id)
            .replace("@CA_PUB@", ca_pub_path).replace("@LIVE@", live).replace("@TRUST@", trust_dir)
            .replace("@FORMAT@", ACCESS_FORMAT).replace("@MAXVALID@", str(int(ACCESS_MAX_S)))
            .replace("@SKEW@", str(int(ACCESS_SKEW_S))))


def _safe_path(path: str, what: str = "The certificate folder") -> str:
    if path and not re.match(r"^/[A-Za-z0-9_./+@-]+$", path):
        raise SyncError(f"{what} has characters Playground can't use safely.")
    return path


def home_scripts(project_id: str, ca_pub_path: str) -> dict[str, str]:
    """The gate and hooks kept in a home folder, keyed by their path inside it.

    ``ca_pub_path`` is the project's public key on the home: a root-owned file, "home" for the copy in the
    home folder itself (a server set up without root), or "" for a plain folder (no checks).
    """
    security.require_safe_id(project_id, "project id")
    if ca_pub_path != "home":
        _safe_path(ca_pub_path)
    return {"gate": _fill_script(GATE_SCRIPT, project_id, ca_pub_path),
            "repo.git/hooks/pre-receive": _fill_script(PRE_RECEIVE_SCRIPT, project_id, ca_pub_path),
            "repo.git/hooks/post-receive": POST_RECEIVE_SCRIPT}


def root_scripts(project_id: str, trust_dir: str) -> dict[str, str]:
    """What the one-time root setup installs, keyed by path inside ``trust_dir``: the key check sshd asks
    at every sign-in, and the project's gate and hooks. All root's; the server account can't change them."""
    security.require_safe_id(project_id, "project id")
    _safe_path(trust_dir)
    project = f"{trust_dir.rstrip('/')}/projects/{project_id}"
    ca_pub, live = f"{project}/ca.pub", f"{project}/live"
    return {f"projects/{project_id}/gate": _fill_script(GATE_SCRIPT, project_id, ca_pub, live=live),
            f"projects/{project_id}/hooks/pre-receive": _fill_script(PRE_RECEIVE_SCRIPT, project_id, ca_pub),
            f"projects/{project_id}/hooks/post-receive": POST_RECEIVE_SCRIPT,
            "bin/keys": _fill_script(KEYS_SCRIPT, project_id, "", trust_dir=trust_dir.rstrip("/"))}


def _home_setup_shell(project_id: str, home: str, ca_pub_path: str, checkout: bool, *, hooks: str = "",
                      ca_public_key: str = "") -> str:
    """A shell script, run as the home's account, that makes or refreshes the home. Prints its path.

    ``hooks``: the root-owned hooks folder (a server with the root setup); pushes made from a shell there run
    them too. ``ca_public_key``: written to ``<home>/ca.pub`` for a server set up without root.
    """
    lines = ["set -eu", "umask 022", f"H={home}", 'mkdir -p "$H"', 'H=$(cd "$H" && pwd -P)',
             '[ -d "$H/repo.git" ] || git init -q --bare "$H/repo.git"',
             'git --git-dir="$H/repo.git" symbolic-ref HEAD refs/heads/main',
             'git --git-dir="$H/repo.git" config gc.pruneExpire 1.day.ago',
             'git --git-dir="$H/repo.git" config gc.reflogExpireUnreachable 1.day.ago',
             'git --git-dir="$H/repo.git" config core.logAllRefUpdates false']
    if hooks:
        _safe_path(hooks)
        lines.append(f'[ ! -d {hooks} ] || git --git-dir="$H/repo.git" config core.hooksPath {hooks}')
    if ca_public_key:
        key = security.normalize_public_key(ca_public_key)
        lines.append(f"printf '%s\\n' '{key}' > \"$H/ca.pub\"")
    for rel, text in home_scripts(project_id, "home" if ca_public_key else ca_pub_path).items():
        encoded = base64.b64encode(text.encode()).decode()
        lines += [f'printf %s {encoded} | base64 -d > "$H/{rel}.new"', f'chmod 755 "$H/{rel}.new"',
                  f'mv "$H/{rel}.new" "$H/{rel}"']
    if checkout:
        lines += ['mkdir -p "$H/checkout" "$H/stage"']
    lines.append('echo "PLAYGROUND_HOME=$H"')
    return "\n".join(lines) + "\n"


def _make_local_home(project_id: str, home: Path, ca_pub_path: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    repo = home / "repo.git"
    if not repo.exists():
        _git(home, "init", "-q", "--bare", str(repo))
    for args in (("symbolic-ref", "HEAD", "refs/heads/main"), ("config", "gc.pruneExpire", "1.day.ago"),
                 ("config", "gc.reflogExpireUnreachable", "1.day.ago"), ("config", "core.logAllRefUpdates", "false")):
        _git(repo, *args)
    for rel, text in home_scripts(project_id, ca_pub_path).items():
        if rel == "repo.git/hooks/post-receive":
            continue
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes(target, text.encode())
        os.chmod(target, 0o755)
    return home.resolve()


def _make_folder_home(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / "HEAD").exists():
        _git(repo, "init", "-q", "--bare", str(repo))
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    hook = repo / "hooks" / "pre-receive"
    hook.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes(hook, home_scripts("folder", "")["repo.git/hooks/pre-receive"].encode())
    os.chmod(hook, 0o755)
    return repo.resolve()


def live_setup_script(project_id: str, account: str, home: str, ca: "security.ProjectCA", founder: tuple[str, str],
                      trust_dir: str = security.SERVER_DIR) -> str:
    """The one-time root setup for a project living on a server, with checks that take effect at once.

    It installs, root-owned: the project's public key, a small settings file (which account, where the
    repository is), the sync gate and its hooks, and the key check sshd runs at every sign-in
    (``AuthorizedKeysCommand``). From then on, who may sync or open a shell is read from the project's
    signed, expiring access list each time, so taking someone off works at once. Safe to run again.
    """
    from . import places
    places.validate_user(account)
    security.require_safe_id(project_id, "project id")
    _safe_path(trust_dir)
    if not re.match(r"^[A-Za-z0-9_./~+@-]+$", home) or ".." in home.split("/"):
        raise SyncError("The folder on the server can't have spaces or odd characters in it.")
    member, device = founder
    security.require_safe_id(member, "member id")
    if not _FP_RE.match(device):
        raise SyncError("This computer's key looks wrong.")
    D = trust_dir.rstrip("/")
    prefix = sync_principal(project_id)[: -len("-sync")]
    ca_pub = f"{ca.public_key} playground-ca:{project_id}"
    spec = ca.revocation_spec()
    begin, end = f"# Playground project {project_id} (begin)", f"# Playground project {project_id} (end)"
    installs = []
    for rel, text in root_scripts(project_id, D).items():
        encoded = base64.b64encode(text.encode()).decode()
        installs.append(f'printf %s {encoded} | base64 -d > "$D/{rel}.new"; chmod 755 "$D/{rel}.new"; '
                        f'mv "$D/{rel}.new" "$D/{rel}"')
    home_expr = home if home.startswith("/") else f'"$U"/{home}'
    sshd_lines = [f"AuthorizedKeysCommand {D}/bin/keys %u %t %f %k", "AuthorizedKeysCommandUser root",
                  f"RevokedKeys {D}/revoked.krl"]
    install_text = "\n".join(installs)
    return f"""#!/bin/sh
# Playground: project {project_id} lives in {account}'s home on this server. This one-time setup makes the
# server ask the project's signed access list at every sign-in, so taking someone off works at once.
# Everything it installs belongs to root; {account} can't change it. Safe to run again.
set -eu
if [ "$(id -u)" != 0 ]; then echo "Please run this with sudo."; exit 1; fi
umask 022
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/homebrew/bin
export PATH
D={D}
A={account}
P="$D/projects/{project_id}"
for tool in git ssh-keygen awk sed; do
  command -v "$tool" >/dev/null 2>&1 || {{ echo "This server needs $tool first."; exit 2; }}
done
if [ "$(id -u)" = 0 ] && ! command -v runuser >/dev/null 2>&1; then
  echo "This server needs runuser (part of util-linux) so the sign-in check can read the project as $A."; exit 2
fi
U=$(getent passwd "$A" 2>/dev/null | cut -d: -f6 || true)
[ -n "$U" ] || U=$(eval echo "~$A")
case "$U" in /*) ;; *) echo "Couldn't find the home folder of $A on this server."; exit 2 ;; esac
H={home_expr}
mkdir -p "$P/hooks" "$D/bin" "$D/principals"
cat > "$P/ca.pub" <<'PLAYGROUND_EOF'
{ca_pub}
PLAYGROUND_EOF
cat > "$P/revoked.spec" <<'PLAYGROUND_EOF'
{spec.rstrip()}
PLAYGROUND_EOF
printf '%s\\n' "account=$A" "repo=$H/repo.git" "founder={member} {device}" "prefix={prefix}" > "$P/live.new"
mv "$P/live.new" "$P/live"
{install_text}
# This project's passes are checked at every sign-in from now on, so they leave the fixed lists.
for f in "$D"/principals/*; do
  [ -f "$f" ] || continue
  grep -vx -e '{prefix}-development' -e '{prefix}-preview' -e '{prefix}-production' -e '{prefix}-sync' "$f" > "$f.new" || true
  mv "$f.new" "$f"
done
K="$U/.ssh/authorized_keys"
if [ -f "$K" ] && grep -qx '{begin}' "$K"; then
  awk -v b='{begin}' -v e='{end}' '$0 == b {{ skip = 1 }} !skip {{ print }} $0 == e {{ skip = 0 }}' "$K" > "$K.playground"
  cat "$K.playground" > "$K"
  rm -f "$K.playground"
fi
ssh-keygen -k -f "$D/revoked.krl.new"
for p in "$D"/projects/*/; do
  [ -f "$p/revoked.spec" ] && [ -f "$p/ca.pub" ] || continue
  ssh-keygen -k -u -s "$p/ca.pub" -f "$D/revoked.krl.new" "$p/revoked.spec"
done
mv "$D/revoked.krl.new" "$D/revoked.krl"
CONF=/etc/ssh/sshd_config.d/41-playground-live.conf
current=$(sshd -T 2>/dev/null | grep -i '^authorizedkeyscommand ' | grep -vi ' none$' || true)
case "$current" in
  ''|*"$D/bin/keys"*) ;;
  *) echo "This server already asks another program for SSH keys:"; echo "$current"
     echo "Playground can't add its own check next to it. Use the setup without sudo instead."; exit 2 ;;
esac
if ! grep -Eqs '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\\.d/' /etc/ssh/sshd_config; then
  echo "This server's sshd_config doesn't read sshd_config.d. Add these lines to it by hand:"
  printf '%s\\n' {" ".join(shlex.quote(line) for line in sshd_lines)}
  exit 2
fi
printf '%s\\n' '# Added by Playground.' {" ".join(shlex.quote(line) for line in sshd_lines)} > "$CONF.new"
mv "$CONF.new" "$CONF"
sshd -t
systemctl reload sshd 2>/dev/null || systemctl reload ssh 2>/dev/null || service ssh reload
echo "Done. {account} now checks the project's access list at every sign-in."
"""


def account_setup_script(project_id: str, ca: "security.ProjectCA") -> str:
    """The setup for a server where nobody can use sudo: the project's key goes into the account's own
    ``~/.ssh/authorized_keys``, limited to the project's passes. Syncing still checks the signed access list
    every time; shells can't be checked live without root, so they end when the person's pass runs out."""
    prefix = sync_principal(project_id)[: -len("-sync")]
    key = security.normalize_public_key(ca.public_key)
    begin, end = f"# Playground project {project_id} (begin)", f"# Playground project {project_id} (end)"
    lines = [f'cert-authority,principals="{prefix}-sync",restrict {key}',
             f'cert-authority,principals="{prefix}-development,{prefix}-preview,{prefix}-production",'
             f'no-agent-forwarding,no-X11-forwarding,no-user-rc {key}']
    quoted = " ".join(shlex.quote(line) for line in (begin, *lines, end))
    return f"""#!/bin/sh
# Playground: let members of project {project_id} sign in to this account with their project passes.
# No sudo needed. Safe to run again.
set -eu
umask 077
K="${{PLAYGROUND_AUTHORIZED_KEYS:-$HOME/.ssh/authorized_keys}}"
mkdir -p "$(dirname "$K")"
touch "$K"
awk -v b='{begin}' -v e='{end}' '$0 == b {{ skip = 1 }} !skip {{ print }} $0 == e {{ skip = 0 }}' "$K" > "$K.new"
printf '%s\\n' {quoted} >> "$K.new"
chmod 600 "$K.new"
mv "$K.new" "$K"
echo "Done. Members of this project can now sign in here with their passes."
"""


# ════════════════════════════════════ ssh for git ════════════════════════════════════

def _known_hosts(data_dir: Path, pin: Pin) -> tuple[Path, str]:
    alias = f"playground-{re.sub(r'[^a-z0-9]', '', pin.project_id.lower())[:24]}"
    path = Path(data_dir) / "sync" / pin.project_id / "known_hosts"
    line = f"{alias} {security.normalize_public_key(pin.host_key)}\n" if pin.host_key else ""
    try:
        current = path.read_text()
    except OSError:
        current = None
    if current != line:
        security.write_private(path, line)
    return path, alias


def _quote_opt(path: Path) -> str:
    text = str(path)
    if '"' in text:
        raise SyncError("Playground's data folder has a quote in its name, which SSH can't read.")
    return f'"{text}"' if " " in text else text


def _ssh_argv(device: security.DeviceKey, cert: Path, port: int, known_hosts: Path, alias: str | None,
              strict: str = "yes", timeout: int = 8) -> tuple[list[str], dict[str, str]]:
    """ssh with no config files, only this device's key and one certificate, no prompts, pinned host."""
    ssh = shutil.which("ssh")
    if not ssh:
        raise SyncError("Sharing needs SSH, and this computer doesn't have it.")
    env = device.ssh_env()
    argv = [ssh, "-F", "none", "-p", str(int(port)), "-i", str(device.private_path),
            "-o", f"CertificateFile={_quote_opt(cert)}", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", f"StrictHostKeyChecking={strict}", "-o", f"UserKnownHostsFile={_quote_opt(known_hosts)}",
            "-o", "GlobalKnownHostsFile=none", "-o", "UpdateHostKeys=no", "-o", f"ConnectTimeout={timeout}",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", "-o", "ForwardAgent=no",
            "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes", "-o", "LogLevel=ERROR"]
    if alias:
        argv += ["-o", f"HostKeyAlias={alias}"]
    argv += ["-o", "BatchMode=yes"] if not env else ["-o", "NumberOfPasswordPrompts=1"]
    return argv, env


def _remote_sh(script: str) -> str:
    """A remote command that runs ``script`` with /bin/sh, whatever the account's login shell is."""
    return "sh -c " + shlex.quote(script)


def _url_host(host: str) -> str:
    try:
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            return f"[{host}]"
    except ValueError:
        pass
    return host


def _reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        socket.create_connection((host, int(port)), timeout=timeout).close()
        return True
    except OSError:
        return False


_TRANSPORT_FAILURES = (
    (("REMOTE HOST IDENTIFICATION HAS CHANGED", "Host key verification failed", "host key for"), "error",
     "The project home's identity doesn't match the one in your invite. Playground stopped syncing to be safe. "
     "Don't go on until you know why: it could be a reinstalled server, or someone pretending to be it."),
    (("Permission denied", "Too many authentication failures", "Certificate invalid", "no matching key"), "access",
     "The project home didn't let this computer in. Your pass may have run out: it renews whenever the owner's "
     "Playground is open. If this keeps happening, ask the owner to look at People."),
    (("Could not resolve hostname", "Name or service not known", "nodename nor servname", "Connection refused",
      "timed out", "No route to host", "Network is unreachable", "Host is down", "Connection closed",
      "Connection reset", "does not appear to be a git repository", "Could not read from remote repository",
      "kex_exchange_identification", "Broken pipe"), "offline", ""),
)


def _classify(stderr: str) -> tuple[str, str]:
    message = ""
    for line in stderr.splitlines():
        marker = line.find("Playground: ")
        if marker >= 0:
            message = line[marker + len("Playground: "):].strip()
    if message:
        return "denied", message
    for needles, state, words in _TRANSPORT_FAILURES:
        if any(n.lower() in stderr.lower() for n in needles):
            return state, words
    return "error", "Sync hit a problem it didn't expect: " + (stderr.strip().splitlines() or ["no detail"])[-1]


# ════════════════════════════════════ the owner's desk ════════════════════════════════════

def issue_sync_certificate(ca: security.ProjectCA, member_id: str, public_key: str, gate: str,
                           days: float = SYNC_CERT_DAYS) -> security.Certificate:
    """A sync-only certificate from the project's own key (``ProjectCA.issue_sync``)."""
    try:
        return ca.issue_sync(member_id, public_key, gate, days=days)
    except security.SecurityError as exc:
        raise SyncError(str(exc)) from exc


def _valid_to_epoch(cert: security.Certificate) -> float:
    if cert.valid_to in ("forever", ""):
        return float("inf")
    try:
        return datetime.fromisoformat(cert.valid_to).timestamp()   # ssh-keygen -L prints local time
    except ValueError:
        return 0.0


def _seat_key(seat: Seat) -> tuple:
    return (seat.member, seat.role, seat.fingerprint, seat.public_key)


class OwnerDesk:
    """An owner computer's duties: the signed access list and everyone's passes.

    The computer that shared the project holds the project's own key (its CA) and signs with it. Any other
    owner's computer signs with its own device key, once the project's key has listed it as one the project
    trusts (the ``issuers`` file that travels with the access list; the home checks both). So passes and the
    list keep being renewed while any owner's Playground is open.

    The newest access list that checks out (this computer's or the home's) is the record of who is on the
    project. Changes made here (let in, take off, change a role) are kept as pending steps in
    ``<data>/sync/<project>/pending.json`` and applied on top of it each time, until the home has a list
    that grew from one of ours; so two owners changing things at once both land.
    """

    def __init__(self, project_dir: Path | str, data_dir: Path | None = None, now: float | None = None) -> None:
        self.project_dir = Path(project_dir)
        self.data_dir = Path(data_dir) if data_dir else _paths.data_dir()
        self.store = ProjectStore(self.project_dir)
        self.project = self.store.read_project()
        self.pin = Pin.load(self.data_dir, self.project.id)
        if self.pin is None:
            raise SyncError("This project isn't shared yet.")
        self.repo = _Repo(self.project_dir)
        self.device = _device(self.data_dir)
        self.ca: security.ProjectCA | None = None
        directory = self.data_dir / "ca" / security.require_safe_id(self.project.id, "project id")
        if (directory / "ca_ed25519").exists():
            ca = security.ProjectCA(self.project.id, directory)
            if not self.pin.ca or security.same_secret(ca.public_key, security.normalize_public_key(self.pin.ca)):
                self.ca = ca
        if self.ca is None and (self.device is None or self.pin.role != "owner"):
            raise SyncError("Only an owner's computer can do this.")
        self.now = time.time() if now is None else now
        self.access_rev, self.access_state, self.access = self._newest_access()
        self._settle()

    @classmethod
    def available(cls, project_id: str, data_dir: Path) -> bool:
        if (Path(data_dir) / "ca" / project_id / "ca_ed25519").exists():
            return True
        pin = Pin.load(Path(data_dir), project_id)
        return bool(pin and pin.role == "owner" and pin.mode != "folder")

    @property
    def is_root(self) -> bool:
        return self.ca is not None

    # ── the newest list ──

    def _newest_access(self) -> tuple[str | None, str, dict]:
        best: tuple[str | None, str, dict] = (None, "bad", {})
        seen = set()
        for ref in (ACCESS_REF, MIRROR_ACCESS):
            rev = self.repo.rev(ref)
            if not rev or rev in seen:
                continue
            seen.add(rev)
            state, parsed = verify_access(self.pin.ca, self.project.id, _access_files(self.repo, rev), self.now)
            if state == "bad":
                continue
            if best[0] is None or parsed["version"] > best[2]["version"] or (
                    ref == MIRROR_ACCESS and parsed["version"] == best[2]["version"]):
                best = (rev, state, parsed)
        return best

    def trusted_issuer(self) -> bool:
        """This computer may sign: the project's own key is here, or the project trusts this owner computer."""
        if self.ca is not None:
            return True
        if self.device is None or not self.access:
            return False
        fp = self.device.fingerprint
        listed = any(security.same_secret(i_fp, fp) for _m, i_fp, _k in self.access.get("issuers", []))
        owner = any(s.role == "owner" and security.same_secret(s.fingerprint, fp) for s in self.access["seats"])
        return listed and owner

    def can_issue_passes(self) -> bool:
        """Passes from this computer work on the home: always from the project's own key; from another owner's
        computer only where the home checks every sign-in itself (the root setup)."""
        if self.ca is not None:
            return True
        return self.trusted_issuer() and (self.access or {}).get("trust", self.pin.trust) == "live"

    def _signer(self) -> tuple[Path, dict[str, str], str]:
        if self.ca is not None:
            return self.ca.private_path, self.ca._env(), self.ca.public_key
        if self.device is not None and self.trusted_issuer():
            return self.device.private_path, self.device.ssh_env(), self.device.public_key
        raise SyncError("This computer can't sign for the project yet. It can once the computer that shared the "
                        "project has been open since you became an owner.")

    # ── pending changes ──

    def _pending_path(self) -> Path:
        return self.data_dir / "sync" / self.project.id / "pending.json"

    def _load_pending(self) -> dict:
        data = read_json(self._pending_path()) if self._pending_path().exists() else None
        data = data if isinstance(data, dict) else {}
        data.setdefault("ops", [])
        data.setdefault("published", [])
        return data

    def _save_pending(self, data: dict) -> None:
        data["published"] = data.get("published", [])[-20:]
        security.write_private(self._pending_path(), json.dumps(data, indent=2, sort_keys=True))

    def queue(self, op: str, **fields: Any) -> None:
        """Remember a change to who's on the project, to apply to every list from now until the home has it."""
        data = self._load_pending()
        data["ops"].append({"op": op, "id": secrets.token_hex(6), "at": _iso(), **fields})
        self._save_pending(data)

    def pending(self) -> list[dict]:
        return list(self._load_pending()["ops"])

    def _settle(self) -> None:
        """Forget changes the home has: they're in a list we published that the home's list grew from."""
        home = self.repo.rev(MIRROR_ACCESS)
        data = self._load_pending()
        if not home or not data["published"]:
            return
        done: set[str] = set()
        keep = []
        for record in data["published"]:
            commit = record.get("commit") or ""
            if commit and (commit == home or self.repo.is_ancestor(commit, home)):
                done.update(record.get("ops") or [])
            else:
                keep.append(record)
        if done:
            data["ops"] = [op for op in data["ops"] if op.get("id") not in done]
            data["published"] = keep
            self._save_pending(data)

    def _apply_ops(self, seats: list[Seat], ops: list[dict]) -> list[Seat]:
        out = list(seats)
        for op in ops:
            kind, member = op.get("op"), str(op.get("member") or "")
            if kind == "add":
                try:
                    key = security.normalize_public_key(str(op.get("public_key") or ""))
                    security.require_safe_id(member, "member id")
                except security.SecurityError:
                    continue
                role = str(op.get("role") or "builder")
                fp = security.fingerprint(key)
                out = [s for s in out if not security.same_secret(s.fingerprint, fp)]
                out = [Seat(s.member, role, s.fingerprint, s.public_key, s.device) if s.member == member else s
                       for s in out]
                out.append(Seat(member, role, fp, key, _device_label(str(op.get("device") or "device"))))
            elif kind == "remove":
                out = [s for s in out if s.member != member]
            elif kind == "remove-device":
                out = [s for s in out if not security.same_secret(s.fingerprint, str(op.get("fingerprint") or ""))]
            elif kind == "role":
                role = str(op.get("role") or "")
                if role in security.ROLE_ACCESS:
                    out = [Seat(s.member, role, s.fingerprint, s.public_key, s.device) if s.member == member else s
                           for s in out]
        return out

    # ── the project's own key keeps its records in step ──

    def _ca_seats(self) -> list[Seat]:
        assert self.ca is not None
        state = read_json(self.ca.state_path) or {}
        revoked = list(state.get("revoked_keys") or [])
        out = []
        for member, entry in sorted(self.ca.members().items()):
            if entry.get("removed"):
                continue
            for key in entry.get("keys", []):
                if any(security.same_secret(key["public_key"], r) for r in revoked):
                    continue
                out.append(Seat(member, entry["role"], key["fingerprint"], key["public_key"],
                                _device_label(key.get("device") or "device")))
        return out

    def _absorb_ca_changes(self) -> None:
        """Changes made to the CA directly (the Places sheet takes someone's access away, or gives it back)
        become pending changes, so they reach the access list."""
        if self.ca is None:
            return
        data = self._load_pending()
        now = sorted(_seat_key(s) for s in self._ca_seats())
        before = data.get("ca_seen")
        if before is None:
            data["ca_seen"] = [list(k) for k in now]
            self._save_pending(data)
            return
        before_keys = {tuple(k) for k in before}
        now_keys = set(now)
        if before_keys == now_keys:
            return
        by_fp_before = {k[2]: k for k in before_keys}
        by_fp_now = {k[2]: k for k in now_keys}
        members_now = {k[0] for k in now_keys}
        ops = data["ops"]
        for fp, k in by_fp_now.items():
            old = by_fp_before.get(fp)
            if old is None:
                seat = next(s for s in self._ca_seats() if s.fingerprint == fp)
                ops.append({"op": "add", "id": secrets.token_hex(6), "at": _iso(), "member": k[0], "role": k[1],
                            "public_key": k[3], "device": seat.device})
            elif old[1] != k[1]:
                ops.append({"op": "role", "id": secrets.token_hex(6), "at": _iso(), "member": k[0], "role": k[1]})
        for fp, k in by_fp_before.items():
            if fp in by_fp_now:
                continue
            if k[0] not in members_now:
                ops.append({"op": "remove", "id": secrets.token_hex(6), "at": _iso(), "member": k[0]})
            else:
                ops.append({"op": "remove-device", "id": secrets.token_hex(6), "at": _iso(), "fingerprint": fp})
        data["ca_seen"] = [list(k) for k in sorted(now_keys)]
        self._save_pending(data)

    def _adopt_into_ca(self, seats: list[Seat]) -> None:
        """Make the project's own records match the list: people other owners took off are revoked here too
        (their keys go into the revocation list), and roles follow."""
        if self.ca is None:
            return
        wanted = {s.fingerprint: s for s in seats}
        members = {s.member for s in seats}
        for member, entry in self.ca.members().items():
            if entry.get("removed"):
                continue
            if member not in members:
                self.ca.remove_member(member)
                continue
            for key in entry.get("keys", []):
                if key["fingerprint"] not in wanted:
                    self.ca.revoke_key(member, key["public_key"])
        current = self.ca.members()
        for seat in seats:
            entry = current.get(seat.member) or {}
            known = any(security.same_secret(k["public_key"], seat.public_key) for k in entry.get("keys", []))
            if not known or entry.get("removed") or entry.get("role") != seat.role:
                self.ca.add_member(seat.member, (self.project.member(seat.member).name
                                                 if self.project.member(seat.member) else seat.member),
                                   seat.role, seat.public_key, seat.device)
        data = self._load_pending()
        data["ca_seen"] = [list(k) for k in sorted(_seat_key(s) for s in self._ca_seats())]
        self._save_pending(data)

    # ── who's on the project, as this computer means it ──

    def seats(self) -> list[Seat]:
        if self.access and self.access.get("seats"):
            base = list(self.access["seats"])
        elif self.ca is not None:
            base = self._ca_seats()
        else:
            base = []
        return self._apply_ops(base, self.pending())

    def current_access(self) -> tuple[int, dict]:
        return int((self.access or {}).get("version") or 0), dict(self.access or {})

    # ── signing the list ──

    def publish_access(self, force: bool = False) -> bool:
        """Sign a new access list when who's on the project, the home, or the owners' computers changed, or
        when the current one has less than ``ACCESS_REFRESH_S`` left. Returns True if the access ref moved."""
        self._absorb_ca_changes()
        seats = self.seats()
        if not any(s.role == "owner" for s in seats):
            raise SyncError("A project needs at least one owner.")
        self._adopt_into_ca(seats)
        base = self.access or {}
        files = _access_files(self.repo, self.access_rev) if self.access_rev else {}
        issuers_version = int(base.get("issuers_version") or 0)
        issuers_blob, issuers_sig = files.get("issuers"), files.get("issuers.sig")
        if self.ca is not None:
            owners = sorted({s for s in seats if s.role == "owner"}, key=lambda s: s.fingerprint)
            owner_fps = {s.fingerprint for s in owners}
            have = base.get("issuers", [])
            retired = {r[1]: r for r in base.get("retired", []) if r[1] not in owner_fps}
            for member, fp, key in have:
                if fp not in owner_fps and fp not in retired:
                    retired[fp] = (member, fp, key, int(self.now + SYNC_CERT_DAYS * 86400 + 3600))
            if (sorted(owner_fps) != sorted(fp for _m, fp, _k in have)
                    or sorted(retired) != sorted(r[1] for r in base.get("retired", []))
                    or (owners and not issuers_blob)):
                issuers_version += 1
                issuers_blob = issuers_text(self.project.id, issuers_version, owners, retired.values()).encode()
                issuers_sig = ssh_sign(self.ca.private_path, "playground-issuers", issuers_blob,
                                       env=self.ca._env()).encode()
        pin = self.pin
        same = (base and self.access_state == "ok"
                and sorted(_seat_key(s) for s in base.get("seats", [])) == sorted(_seat_key(s) for s in seats)
                and len(base.get("seats", [])) == len(base.get("members", []))
                and int(base.get("issuers_version") or 0) == issuers_version
                and (base.get("home") or {}).get("home") == pin.home and base.get("hosts") == list(pin.hosts)
                and (base.get("host_key") or "") == (security.normalize_public_key(pin.host_key) if pin.host_key else "")
                and base.get("trust") == pin.trust and (base.get("gate") or "") == (pin.gate_path if pin.mode != "folder"
                                                                                    else "")
                and base.get("expires", 0) - self.now > ACCESS_REFRESH_S)
        if same and not force:
            return False
        signer_path, signer_env, _signer_key = self._signer()
        version = max(int(base.get("version") or 0), pin.access_version if self.access_rev is None else 0) + 1
        text = access_text(self.project.id, version, pin, seats, issued=self.now, expires=self.now + ACCESS_VALID_S,
                           issuers_version=issuers_version if issuers_blob else 0).encode()
        sig = ssh_sign(signer_path, "playground-access", text, env=signer_env).encode()
        entries = {"access": ("100644", self.repo.write_blob(text)), "access.sig": ("100644", self.repo.write_blob(sig))}
        if issuers_blob:
            entries["issuers"] = ("100644", self.repo.write_blob(issuers_blob))
            entries["issuers.sig"] = ("100644", self.repo.write_blob(issuers_sig or b""))
        parent = self.access_rev
        commit = self.repo.commit(self.repo.make_tree(entries), [parent] if parent else [],
                                  f"Access list, version {version}", _who(pin.member, pin.owner_name))
        self.repo.update_ref(ACCESS_REF, commit)
        data = self._load_pending()
        data["published"].append({"commit": commit, "ops": [op["id"] for op in data["ops"]]})
        self._save_pending(data)
        state, parsed = verify_access(pin.ca, self.project.id, _access_files(self.repo, commit), self.now)
        if state != "ok":
            raise SyncError("Playground couldn't sign the project's access list properly.")
        self.access_rev, self.access_state, self.access = commit, state, parsed
        pin.access_version = version
        pin.access_expires = float(parsed["expires"])
        pin.issuers = _owner_issuers(parsed)
        pin.save(self.data_dir)
        return True

    # ── passes ──

    def _certs_head(self) -> str | None:
        local, home = self.repo.rev(CERTS_REF), self.repo.rev(MIRROR_CERTS)
        if home and local and home != local and not self.repo.is_ancestor(home, local):
            return home                     # another owner renewed first: build on theirs
        return local or home

    def renew_certs(self, force_members: Iterable[str] = (), now: float | None = None,
                    force_devices: Iterable[str] = ()) -> bool:
        """Issue fresh passes where they run out soon, or where the computer that signed them is no longer
        trusted. Returns True if the certs ref moved."""
        now = self.now if now is None else now
        force = set(force_members)
        force_fps = set(force_devices)
        if self.pin.mode == "folder" or not self.can_issue_passes():
            self._install_own()
            return False
        head = self._certs_head()
        tree = self.repo.ls_tree(head)
        try:
            index = json.loads(self.repo.blob(f"{head}:index.json") or b"{}") if head else {}
        except ValueError:
            index = {}
        trusted = set(self._trusted_signer_fps())
        entries: dict[str, tuple[str, str]] = {}
        new_index: dict[str, Any] = {}
        changed = False
        for seat in self.seats():
            tag = _key_tag(seat.public_key)
            for kind in ("sync", "dev"):
                rel = f"{seat.member}/{tag}.{kind}"
                if kind == "dev" and not security.ROLE_ACCESS.get(seat.role):
                    continue
                record = index.get(rel)
                until, by, role = (float(record), "", "") if isinstance(record, (int, float)) else (
                    float((record or {}).get("until") or 0), str((record or {}).get("by") or ""),
                    str((record or {}).get("role") or ""))
                left = until - now
                renew_at = SYNC_CERT_RENEW_S if kind == "sync" else DEV_CERT_RENEW_S
                signer_ok = not by or by in trusted        # passes from a retired owner computer are replaced
                role_ok = not role or role == seat.role
                if (rel in tree and left > renew_at and signer_ok and role_ok and seat.member not in force
                        and seat.fingerprint not in force_fps):
                    entries[rel] = tree[rel]
                    new_index[rel] = record
                    continue
                cert = self._issue(seat, kind)
                entries[rel] = ("100644", self.repo.write_blob((cert.text.strip() + "\n").encode()))
                new_index[rel] = {"until": _valid_to_epoch(cert), "by": cert.signing_ca, "role": seat.role}
                changed = True
        if set(entries) != {k for k in tree if k != "index.json"}:
            changed = True
        if not changed:
            if head and head != self.repo.rev(CERTS_REF):
                self.repo.update_ref(CERTS_REF, head)
            self._install_own()
            return False
        entries["index.json"] = ("100644", self.repo.write_blob(
            (json.dumps(new_index, indent=2, sort_keys=True) + "\n").encode()))
        commit = self.repo.commit(self.repo.make_tree(entries), [head] if head else [], "Fresh passes",
                                  _who(self.pin.member, self.pin.owner_name))
        self.repo.update_ref(CERTS_REF, commit)
        self._install_own()
        return True

    def _trusted_signer_fps(self) -> list[str]:
        """Signers whose passes can stay as they are: the project's own key, and current owner computers."""
        out = [self.pin.ca_fingerprint] if self.pin.ca else []
        if self.access and (self.access.get("trust") or self.pin.trust) == "live":
            out += [fp for _m, fp, _k in self.access.get("issuers", [])]
        return out

    def _issue(self, seat: Seat, kind: str) -> security.Certificate:
        try:
            if self.ca is not None:
                if kind == "sync":
                    return self.ca.issue_sync(seat.member, seat.public_key, self.pin.gate_path, days=SYNC_CERT_DAYS)
                return self.ca.issue(seat.member, seat.public_key)
            assert self.device is not None
            roles = list(security.ROLE_ACCESS.get(seat.role, ()))
            if kind == "sync":
                principals = [sync_principal(self.project.id)]
                options = security.sync_options(self.pin.gate_path, seat.member, seat.fingerprint)
                minutes, key_id = int(SYNC_CERT_DAYS * 1440), f"{seat.member}@{seat.device}:sync"
            else:
                principals = [security.principal_for(self.project.id, r) for r in roles]
                options = security.shell_options(roles)
                minutes, key_id = security.CERT_DEFAULT_VALIDITY_HOURS * 60, f"{seat.member}@{seat.device}"
            directory = security.private_dir(self.data_dir / "sync" / self.project.id)
            return security.sign_certificate(self.device.private_path, self.device.ssh_env(), seat.public_key, key_id,
                                             principals, minutes, 2_000_000_000 + secrets.randbelow(2**40), options,
                                             work_dir=directory)
        except security.SecurityError as exc:
            raise SyncError(str(exc)) from exc

    def cert_texts(self, member: str, public_key: str) -> dict[str, str]:
        head = self.repo.rev(CERTS_REF)
        tag = _key_tag(public_key)
        out = {}
        for kind in ("sync", "dev"):
            data = self.repo.blob(f"{head}:{member}/{tag}.{kind}") if head else None
            out[kind] = data.decode() if data else ""
        return out

    def _install_own(self) -> None:
        device = self.device
        if device is None:
            return
        texts = self.cert_texts(self.pin.member, device.public_key)
        install_certificates(device, self.pin, texts)


def _owner_issuers(parsed: dict) -> list[str]:
    """Owner computers whose passes count: listed by the project's own key (and, for a while, ones that
    stopped being an owner's, so passes they signed keep working until fresh ones arrive)."""
    return sorted({fp for _m, fp, _k in parsed.get("issuers", [])} | {r[1] for r in parsed.get("retired", [])})


def install_certificates(device: security.DeviceKey, pin: Pin, texts: dict[str, str]) -> list[str]:
    """Keep certificates that are for this device, signed by a key this project trusts, and newer than what
    we have."""
    installed = []
    trusted = pin.trusted_signers()
    for kind, text in texts.items():
        if not text or not text.strip() or kind not in ("sync", "dev"):
            continue
        target = sync_cert_path(device, pin.project_id) if kind == "sync" else device.certificate_path(pin.project_id)
        try:
            cert = security.read_certificate(text.strip())
        except security.SecurityError:
            continue
        if not any(security.same_secret(cert.signing_ca, fp) for fp in trusted):
            continue
        if not security.same_secret(cert.public_key_fingerprint, device.fingerprint):
            continue
        if target.exists():
            try:
                current = security.read_certificate(target)
                current_trusted = any(security.same_secret(current.signing_ca, fp) for fp in trusted)
                if current_trusted and _valid_to_epoch(current) >= _valid_to_epoch(cert):
                    continue
            except security.SecurityError:
                pass
        security.write_public(target, cert.text.strip() + "\n")
        installed.append(kind)
    return installed


def pass_expiry(device: security.DeviceKey | None, project_id: str) -> float:
    """When this computer's sync pass runs out (0 when it has none)."""
    if device is None:
        return 0.0
    path = sync_cert_path(device, project_id)
    if not path.exists():
        return 0.0
    try:
        return _valid_to_epoch(security.read_certificate(path))
    except security.SecurityError:
        return 0.0


# ════════════════════════════════════ the engine ════════════════════════════════════

_LISTEN = (events.GOAL_CHANGED, events.THREAD_APPENDED, events.ACTIVITY_APPENDED, events.PROJECT_CHANGED,
           events.DECISION_PENDING, events.DECISION_ANSWERED, events.REQUEST_PENDING, events.ISSUE_CHANGED,
           events.STACK_CHANGED, events.USAGE_CHANGED, events.ANALYTICS_CHANGED, events.PLACES_CHANGED)


class SyncEngine:
    """Keeps one project folder in step with its home.

    ::

        engine = SyncEngine(project_dir)        # bus defaults to events.bus
        engine.start()                          # a round every few seconds, and soon after any change
        engine.sync_now()                       # one round now, in this thread; returns status()
        engine.status()                         # {"state", "text", "detail", "last_sync", ...}
        engine.set_goal(goal_id)                # presence: which goal I'm in
        engine.begin_goal(goal_id)              # work happens on goal/<goal_id>
        engine.release_goal(goal_id, title)     # merge into main and push
        engine.stop()
    """

    def __init__(self, project_dir: Path | str, bus: events.EventBus | None = None, *,
                 interval: float = DEFAULT_INTERVAL, data_dir: Path | str | None = None,
                 auto_checkpoint: bool = True) -> None:
        self.project_dir = Path(project_dir).expanduser().absolute()
        self.bus = bus if bus is not None else events.bus
        self.store = ProjectStore(self.project_dir, bus=self.bus)
        self.data_dir = Path(data_dir) if data_dir else _paths.data_dir()
        self.interval = max(0.5, float(interval))
        self.auto_checkpoint = auto_checkpoint
        self.state_dir = self.project_dir / ".playground"
        try:
            self.project_id = self.store.read_project().id
        except StoreError as exc:
            raise SyncError(str(exc)) from exc
        self._round_lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tokens: list[int] = []
        self._status: dict = {"state": "stopped", "text": STATE_WORDS["stopped"], "detail": ""}
        self._stat_cache: dict[str, tuple[tuple, str]] = {}
        self._failures = 0
        self._halted = ""
        self._goal: str = ""
        self._goal_title: str = ""
        self._since = _iso()
        self._presence_pushed: tuple[float, str] = (0.0, "")
        self._leaving = False
        self._peers: list[dict] = []
        self._certs_seen = ""
        self._access_seen = ""
        self._last_owner_duty = 0.0
        self._owner_dirty = True
        self._repo: _Repo | None = None
        self._pin_cache: Pin | None = None
        self._host_choice: tuple[str, float] = ("", 0.0)
        if self.pin() is None:
            self._status = {"state": "not-shared", "text": STATE_WORDS["not-shared"], "detail": ""}

    # ── lifecycle ──

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "SyncEngine":
        if self.running:
            return self
        self._stop.clear()
        self._leaving = False
        self._halted = ""
        self._tokens = [self.bus.subscribe(topic, self._on_change) for topic in _LISTEN]
        self._tokens.append(self.bus.subscribe(events.AGENT_STATE, self._on_agent))
        self._thread = threading.Thread(target=self._run, name=f"playground-sync:{self.project_dir.name}",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 10.0) -> None:
        """Stop syncing. Says goodbye (presence "left") if the home is reachable quickly."""
        for token in self._tokens:
            self.bus.unsubscribe(token)
        self._tokens = []
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        if self.pin() is not None and self._status.get("state") in ("up-to-date", "syncing", "conflict") \
                and not self._other_device_here():
            self._leaving = True
            try:
                with self._round_lock:
                    self._push_presence_only()
            except Exception:  # noqa: BLE001 - saying goodbye is best effort
                log.debug("goodbye presence failed", exc_info=True)
        if self.pin() is not None:
            self._set("stopped")

    def _other_device_here(self) -> bool:
        """Another of this person's computers has been here lately: saying "left" would be wrong."""
        pin = self.pin()
        device = _device(self.data_dir)
        if pin is None or device is None:
            return False
        data = self.repo.blob(f"{MIRROR_PRESENCE}{pin.member}") if self.repo.rev(MIRROR_PRESENCE + pin.member) else None
        try:
            doc = json.loads(data) if data else None
        except ValueError:
            return False
        if not isinstance(doc, dict) or doc.get("device_key") in ("", None, _key_tag(device.public_key)):
            return False
        moment = _parse_iso(doc.get("ts"))
        return bool(moment and (_now() - moment).total_seconds() < PRESENCE_FRESH_S and doc.get("state") != "left")

    def sync_now(self, wait: bool = True) -> dict:
        """One round now (in this thread) and its status, or just a nudge when ``wait`` is False."""
        if not wait:
            self._wake.set()
            return self.status()
        self._round_safely()
        return self.status()

    def status(self) -> dict:
        status = dict(self._status)
        pin = self.pin()
        status.setdefault("project", str(self.project_dir))
        if pin is not None:
            status["mode"] = pin.mode
            status["home"] = pin.host_name
            status["member"] = pin.member
            status["role"] = pin.role
            if pin.mode == "this-computer" and self._is_host(pin):
                status["warning"] = THIS_COMPUTER_WARNING
            elif status.get("renew_due") and status.get("renew_words"):
                status["warning"] = status["renew_words"]
        return status

    def presence(self) -> list[dict]:
        return read_presence(self.project_dir)

    def set_goal(self, goal_id: str | None, title: str = "") -> None:
        """Presence: which goal this person has open. Goes out on the next round."""
        goal_id = goal_id or ""
        if goal_id and not title:
            try:
                title = self.store.read_goal(goal_id).title
            except Exception:  # noqa: BLE001
                title = ""
        if (goal_id, title) != (self._goal, self._goal_title):
            self._goal, self._goal_title = goal_id, title
            self._presence_pushed = (0.0, "")
            self._wake.set()

    # ── identity and home ──

    def pin(self) -> Pin | None:
        if self._pin_cache is None:
            self._pin_cache = Pin.load(self.data_dir, self.project_id)
        return self._pin_cache

    def _reload_pin(self) -> Pin | None:
        self._pin_cache = None
        return self.pin()

    @property
    def repo(self) -> _Repo:
        if self._repo is None:
            self._repo = _Repo(self.project_dir)
        return self._repo

    def _device(self) -> security.DeviceKey:
        device = _device(self.data_dir)
        if device is None:
            raise SyncError("This computer doesn't have its Playground key yet.")
        return device

    def _is_host(self, pin: Pin) -> bool:
        if pin.mode != "this-computer":
            return False
        device = _device(self.data_dir)
        return bool(device and pin.owner_device and security.same_secret(device.fingerprint, pin.owner_device)
                    and Path(pin.repo_path).is_dir())

    def _remote(self, pin: Pin, *, dev_cert: bool = False) -> tuple[str, dict[str, str]]:
        """The URL git uses for the home, and the environment that carries our ssh command."""
        if pin.mode == "folder":
            if not Path(pin.repo_path).is_dir():
                raise _Offline("")
            return pin.repo_path, {}
        if self._is_host(pin):
            return pin.repo_path, {}
        device = self._device()
        cert = device.certificate_path(pin.project_id) if dev_cert else sync_cert_path(device, pin.project_id)
        if not cert.exists():
            raise SyncError("This computer doesn't have a pass for the project home yet. The owner's Playground "
                            "hands one out when it lets you in.")
        known, alias = _known_hosts(self.data_dir, pin)
        argv, env = _ssh_argv(device, cert, pin.port, known, alias)
        host = self._pick_host(pin)
        url = f"ssh://{pin.user}@{_url_host(host)}:{int(pin.port)}{pin.repo_path}"
        env = {**env, "GIT_SSH_COMMAND": shlex.join(argv), "GIT_SSH_VARIANT": "ssh"}
        return url, env

    def _pick_host(self, pin: Pin) -> str:
        if len(pin.hosts) <= 1:
            return pin.hosts[0] if pin.hosts else ""
        chosen, when = self._host_choice
        if chosen in pin.hosts and time.monotonic() - when < 300:
            return chosen
        for host in pin.hosts:
            if _reachable(host, pin.port):
                self._host_choice = (host, time.monotonic())
                return host
        raise _Offline("")

    def _offline_words(self, pin: Pin) -> str:
        if pin.mode == "this-computer":
            who = f"{_first(pin.owner_name)}’s computer" if pin.owner_name else "The owner’s computer"
            return (f"{who} is off or out of reach. Your changes are safe here and go out as soon as it’s back.")
        if pin.mode == "folder":
            return "The shared folder isn't there right now. Your changes are safe here and go out when it's back."
        return (f"Can’t reach {pin.host_name or 'the project home'} right now. Your changes are safe here and "
                "go out as soon as it’s back.")

    # ── status ──

    def _set(self, state: str, text: str | None = None, detail: str = "", **extra: Any) -> None:
        changed = extra.pop("changed", None)
        status = {"state": state, "text": text or STATE_WORDS.get(state, state), "detail": detail,
                  "project": str(self.project_dir), "last_sync": self._status.get("last_sync", ""),
                  "behind": self._status.get("behind", 0), "ahead": self._status.get("ahead", 0),
                  "branch": self._status.get("branch", "")}
        status.update(extra)
        for key in ("pass_expires", "access_expires", "renew_due", "renew_words"):
            status.setdefault(key, self._status.get(key))
        before = {k: self._status.get(k) for k in ("state", "text", "detail", "behind", "ahead", "branch",
                                                   "renew_due", "renew_words")}
        after = {k: status.get(k) for k in before}
        self._status = status
        if before != after or changed:
            payload = {k: v for k, v in self.status().items() if k != "project"}
            if changed:
                payload["changed"] = changed
            self.bus.publish(SYNC_STATE, project=str(self.project_dir), **payload)

    # ── the loop ──

    def _on_change(self, **payload: Any) -> None:
        if payload.get("project") in (None, str(self.project_dir), str(self.store.root)):
            self._wake.set()

    def _on_agent(self, **payload: Any) -> None:
        if not self.auto_checkpoint or payload.get("state") not in ("idle", "stopped"):
            return
        if payload.get("project") not in (None, str(self.project_dir), str(self.store.root)):
            return
        goal_id = payload.get("goal_id")
        threading.Thread(target=self._checkpoint_quietly, args=(goal_id,), daemon=True).start()

    def _checkpoint_quietly(self, goal_id: str | None) -> None:
        try:
            if self.checkpoint(goal_id=goal_id):
                self._wake.set()
        except Exception:  # noqa: BLE001
            log.debug("checkpoint failed", exc_info=True)

    def _run(self) -> None:
        delay = 0.0
        while not self._stop.is_set():
            self._wake.wait(delay)
            if self._stop.is_set():
                break
            self._wake.clear()
            if delay == 0.0 or self._wake.is_set():
                time.sleep(0.3)          # let a burst of writes land together
                self._wake.clear()
            self._round_safely()
            state = self._status.get("state")
            if state == "offline":
                delay = OFFLINE_BACKOFF[min(self._failures - 1, len(OFFLINE_BACKOFF) - 1)] if self._failures else 5.0
            elif state in ("removed", "not-shared") or self._halted:
                delay = 60.0
            elif state in ("access", "error"):
                delay = 30.0
            else:
                delay = self.interval

    def _round_safely(self) -> None:
        with self._round_lock:
            pin = self._reload_pin()
            if pin is None:
                self._set("not-shared")
                return
            if self._halted:
                self._set("error", detail=self._halted)
                return
            try:
                self._round(pin)
                self._failures = 0
            except _Offline as exc:
                self._failures += 1
                self._set("offline", detail=str(exc) or self._offline_words(pin))
            except AccessRemoved as exc:
                self._set("removed", detail=str(exc))
            except GitFailed as exc:
                self._transport_failure(pin, exc.stderr)
            except SyncError as exc:
                self._set("error", detail=str(exc))
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                log.exception("sync round failed")
                self._set("error", detail=f"Sync hit a problem it didn't expect ({type(exc).__name__}).")

    def _transport_failure(self, pin: Pin, stderr: str) -> None:
        kind, words = _classify(stderr)
        if kind == "offline":
            self._failures += 1
            self._set("offline", detail=self._offline_words(pin))
        elif kind == "denied":
            if "has run out" in words and "access list" in words:
                who = _first(pin.owner_name) or "an owner"
                self._set("access", text="Waiting for an owner",
                          detail=f"The project's access list has run out, so the home isn't letting anyone sync. It "
                                 f"renews by itself as soon as an owner opens Playground. Your changes are safe here. "
                                 f"You can also ask {who} for a fresh pass in People.", renew_due=True)
            elif "access list" in words and "isn't on" in words:
                if pin.synced_once:
                    self._set("removed", detail="An owner took this computer off the project. Your copy stays "
                                                "here; it just doesn't sync any more.")
                else:
                    who = _first(pin.owner_name) or "the owner"
                    self._set("access", detail=f"The project home doesn't know this computer yet. Ask {who} to "
                                               "open Playground, so it can pass your welcome along.")
            else:
                self._set("error", detail=words)
        elif kind == "access":
            self._set("access", detail=words)
        else:
            if "identity doesn't match" in words:
                self._halted = words
            self._set("error", detail=words)

    def _round(self, pin: Pin) -> None:
        self._owner_duties(pin)
        remote, env = self._remote(pin)
        _ensure_state_files(self.project_dir)
        with self.store.locked():
            snap = self._snapshot(pin)
        if (snap and pin.role != "viewer" and snap != self.repo.rev(MIRROR_STATE)
                and self._status.get("state") == "up-to-date"):
            self._set("syncing")
        changed: list[str] = []
        notes: list[str] = []
        for _attempt in range(3):
            self._fetch(remote, env)
            remote_state = self.repo.rev(MIRROR_STATE)
            merged, merge_notes = self._merge_state(pin, snap, remote_state)
            notes += merge_notes
            if merged and merged != snap:
                with self.store.locked():
                    changed += self._apply(snap, merged)
                    self.repo.update_ref(STATE_REF, merged, snap)
                snap = merged
            self._check_access(pin)
            self._take_certs(pin)
            self._apply_presence(pin)
            code = self._code_round(pin)
            refspecs = list(code["push"])
            if snap and snap != remote_state and pin.role != "viewer":
                refspecs.append(f"{snap}:{STATE_REF}")
            presence = self._presence_refspec(pin)
            if presence:
                refspecs.append(presence)
            self._push_owner_refs(pin, remote, env)
            refspecs += self._restore_tags_round(pin)
            if not refspecs:
                break
            result = self._push(remote, env, refspecs)
            if result.get(STATE_REF, (" ",))[0] == "!":
                continue                  # someone synced first: fetch, merge and try again
            break
        self._after_vault_merge(pin, notes)
        if not pin.synced_once:
            pin.synced_once = True
            pin.save(self.data_dir)
        conflict = code.get("conflict", "")
        extra = {"last_sync": _iso(), "behind": code.get("behind", 0), "ahead": code.get("ahead", 0),
                 "branch": code.get("branch", ""), **self._renewal(pin)}
        if "vault-fork" in notes:
            conflict = conflict or ("Two owners changed who can open the vault at the same time. One change was "
                                    "kept; check People and add anyone who's missing again.")
        if conflict:
            self._set("conflict", text="Needs a hand", detail=conflict, changed=changed, **extra)
        else:
            self._set("up-to-date", detail=code.get("note", ""), changed=changed, **extra)

    # ── state: snapshot, merge, apply ──

    def _scan(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        root = self.state_dir
        if not root.is_dir():
            return found
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            keep = []
            for d in dirnames:
                rel = f"{rel_dir}/{d}" if rel_dir else d
                if d.startswith(".") or (not rel_dir and d in ("local", "presence")) or os.path.islink(
                        os.path.join(dirpath, d)):
                    continue
                keep.append(d)
            dirnames[:] = keep
            for name in filenames:
                rel = f"{rel_dir}/{name}" if rel_dir else name
                path = Path(dirpath) / name
                if _state_ignored(rel) or path.is_symlink() or "\n" in rel:
                    continue
                found[rel] = path
        return found

    def _snapshot(self, pin: Pin) -> str | None:
        """Record .playground/ as it is now on the state ref. Returns the state head."""
        repo = self.repo
        head = repo.rev(STATE_REF)
        tracked = repo.ls_tree(head)
        entries: dict[str, tuple[str, str]] = {}
        to_write: list[tuple[str, Path]] = []
        now_ns = time.time_ns()
        for rel, path in self._scan().items():
            try:
                st = path.lstat()
            except OSError:
                continue
            if st.st_size > MAX_STATE_FILE:
                continue
            mode = "100755" if st.st_mode & 0o111 else "100644"
            signature = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)
            cached = self._stat_cache.get(rel)
            if cached and cached[0] == signature:
                entries[rel] = (mode, cached[1])
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            old = tracked.get(rel)
            if rel.endswith(".jsonl"):
                data = _complete_lines(data)
                if old and _same_lines(data, repo.blob(old[1])):
                    sha = old[1]
                else:
                    sha = repo.write_blob(data)
            else:
                sha = repo.hash_bytes(data)
                if not old or old[1] != sha:
                    to_write.append((rel, path))
            if now_ns - st.st_mtime_ns > 2_000_000_000:
                self._stat_cache[rel] = (signature, sha)
            entries[rel] = (mode, sha)
        if to_write:
            written = repo.write_files([p for _r, p in to_write])
            for (rel, _p), sha in zip(to_write, written):
                if entries[rel][1] != sha:            # changed between hashing and writing
                    entries[rel] = (entries[rel][0], sha)
                    self._stat_cache.pop(rel, None)
        if head and entries == tracked:
            return head
        if not entries and not head:
            return None
        tree = repo.make_tree(entries)
        if head and tree == repo.text("rev-parse", f"{head}^{{tree}}"):
            return head
        name = self._my_name(pin)
        commit = repo.commit(tree, [head] if head else [], f"{name or pin.member}: Playground records",
                             _who(pin.member, name))
        repo.update_ref(STATE_REF, commit, head)
        return commit

    def _merge_state(self, pin: Pin, local: str | None, remote: str | None) -> tuple[str | None, list[str]]:
        repo = self.repo
        if not remote or remote == local:
            return local, []
        if not local or repo.is_ancestor(local, remote):
            return remote, []
        if repo.is_ancestor(remote, local):
            return local, []
        base = repo.merge_base(local, remote)
        b, o, t = repo.ls_tree(base), repo.ls_tree(local), repo.ls_tree(remote)
        result = dict(o)
        need = []
        for path in sorted(set(b) | set(o) | set(t)):
            bo, oo, to = b.get(path), o.get(path), t.get(path)
            if oo == to or bo == to:
                continue
            if bo == oo:
                if to is None:
                    result.pop(path, None)
                else:
                    result[path] = to
                continue
            need.append(path)
        notes: list[str] = []
        if need:
            blobs = repo.read_blobs([e[1] for p in need for e in (b.get(p), o.get(p), t.get(p)) if e])
            for path in need:
                def content(entry):
                    return blobs.get(entry[1]) if entry else None
                merged = merge_file(path, content(b.get(path)), content(o.get(path)), content(t.get(path)))
                notes += merged.notes
                mode = (o.get(path) or t.get(path))[0]
                if merged.content is None:
                    result.pop(path, None)
                else:
                    result[path] = (mode, repo.write_blob(merged.content))
                for extra_path, data in merged.extra.items():
                    if _safe_rel(extra_path):
                        result[extra_path] = ("100644", repo.write_blob(data))
        tree = repo.make_tree(result)
        name = self._my_name(pin)
        commit = repo.commit(tree, [local, remote], f"{name or pin.member}: brought teammates' records together",
                             _who(pin.member, name))
        return commit, notes

    def _apply(self, snap: str | None, merged: str) -> list[str]:
        """Make .playground/ match ``merged``, re-checking each file against the snapshot first."""
        repo = self.repo
        before, after = repo.ls_tree(snap), repo.ls_tree(merged)
        changed_paths = [p for p in sorted(set(before) | set(after)) if before.get(p) != after.get(p)]
        blobs = repo.read_blobs([e[1] for p in changed_paths for e in (before.get(p), after.get(p)) if e])
        root = self.state_dir.resolve()
        applied = []
        for rel in changed_paths:
            if not _safe_rel(rel):
                log.warning("ignoring an unsafe path from the project home: %r", rel)
                continue
            target_entry = after.get(rel)
            if target_entry and target_entry[0] not in ("100644", "100755"):
                continue
            path = self.state_dir / rel
            try:
                parent = path.parent.resolve()
            except OSError:
                continue
            if parent != root and root not in parent.parents:
                continue
            if path.is_symlink():
                continue
            snap_data = blobs.get(before[rel][1]) if rel in before else None
            target = blobs.get(target_entry[1]) if target_entry else None
            try:
                live = path.read_bytes() if path.exists() else None
            except OSError:
                continue
            jsonl = rel.endswith(".jsonl")
            unchanged = (live == snap_data) or (jsonl and live is not None and snap_data is not None
                                                 and _same_lines(_complete_lines(live), snap_data)
                                                 and live.endswith(b"\n"))
            if target is None:
                if unchanged and live is not None:
                    path.unlink()
                    self._stat_cache.pop(rel, None)
                    applied.append(rel)
                continue
            if jsonl:
                if live is not None and live and not live.endswith(b"\n"):
                    continue                     # someone is mid-line; catch it next round
                missing = Counter(_jsonl_lines(target)) - Counter(_jsonl_lines(live))
                if live is None:
                    _write_bytes(path, target)
                elif missing:
                    extra = []
                    for line in _jsonl_lines(target):
                        if missing[line] > 0:
                            extra.append(line)
                            missing[line] -= 1
                    with open(path, "ab") as handle:
                        handle.write(b"".join(line + b"\n" for line in extra))
                        handle.flush()
                        os.fsync(handle.fileno())
                else:
                    continue
            elif unchanged:
                _write_bytes(path, target)
                if target_entry[0] == "100755":
                    os.chmod(path, 0o755)
            else:
                merged_data = merge_file(rel, snap_data, live, target).content
                if merged_data is None or merged_data == live:
                    continue
                _write_bytes(path, merged_data)
            self._stat_cache.pop(rel, None)
            applied.append(rel)
        return applied

    def _after_vault_merge(self, pin: Pin, notes: list[str]) -> None:
        if "vault-rewrap" not in notes:
            return
        try:
            vault = security.Vault(self.project_dir, pin.project_id)
            if vault.status().get("admin"):
                vault.rewrap()
        except Exception:  # noqa: BLE001 - an owner's Playground will try again; the vault stays valid
            log.info("vault re-lock after merge didn't happen", exc_info=True)

    # ── transport ──

    def _fetch(self, remote: str, env: dict[str, str]) -> None:
        self.repo.run("fetch", "--prune", "--no-tags", "--quiet", "--no-write-fetch-head", remote,
                      *FETCH_REFSPECS, env=env, timeout=90)

    def _push(self, remote: str, env: dict[str, str], refspecs: list[str]) -> dict[str, tuple[str, str]]:
        proc = self.repo.run("push", "--porcelain", "--no-verify", remote, *refspecs, env=env, check=False,
                             timeout=120)
        result: dict[str, tuple[str, str]] = {}
        for line in proc.stdout.decode(errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and len(parts[0]) == 1:
                src, _, dst = parts[1].partition(":")
                result[dst] = (parts[0], parts[2] if len(parts) > 2 else "")
                if parts[0] != "!":
                    sha = self.repo.rev(src) if not re.match(r"^[0-9a-f]{40,64}$", src) else src
                    mirror = self._mirror_for(dst)
                    if mirror and sha:
                        self.repo.update_ref(mirror, sha)
        if proc.returncode != 0 and not result:
            raise GitFailed(["push"], proc)
        stderr = proc.stderr.decode(errors="replace")
        if "Playground: " in stderr:
            _kind, words = _classify(stderr)
            if "synced first" not in words and "Bring their changes in first" not in words:
                raise GitFailed(["push"], proc)       # the home said no: report it in plain words
        return result

    @staticmethod
    def _mirror_for(dst: str) -> str | None:
        if dst.startswith("refs/heads/"):
            return REMOTE_HEADS + dst[len("refs/heads/"):]
        if dst.startswith("refs/playground/"):
            return MIRROR + dst[len("refs/playground/"):]
        return None

    # ── presence ──

    def _my_name(self, pin: Pin) -> str:
        try:
            member = self.store.read_project().member(pin.member)
        except StoreError:
            member = None
        return member.name if member else ""

    def _presence_refspec(self, pin: Pin) -> str | None:
        device = _device(self.data_dir)
        doc = {"member": pin.member, "name": self._my_name(pin), "device": device.device_name if device else "",
               "device_key": _key_tag(device.public_key) if device else "", "goal": self._goal, "goal_title": self._goal_title, "state": "left" if self._leaving else "here",
               "since": self._since, "ts": _iso()}
        key = json.dumps({k: v for k, v in doc.items() if k != "ts"}, sort_keys=True)
        last_time, last_key = self._presence_pushed
        if key == last_key and time.time() - last_time < PRESENCE_REFRESH_S:
            return None
        data = (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode()
        local = self.state_dir / "presence" / f"{pin.member}.json"
        try:
            _write_bytes(local, data)
        except OSError:
            pass
        blob = self.repo.write_blob(data)
        self._presence_pushed = (time.time(), key)
        return f"+{blob}:{PRESENCE_PREFIX}{pin.member}"

    def _push_presence_only(self) -> None:
        pin = self.pin()
        if pin is None:
            return
        self._presence_pushed = (0.0, "")
        spec = self._presence_refspec(pin)
        if not spec:
            return
        try:
            remote, env = self._remote(pin)
            self.repo.run("push", "--porcelain", "--no-verify", remote, spec, env=env, check=False, timeout=8)
        except SyncError:
            pass

    def _apply_presence(self, pin: Pin) -> None:
        mirrors = self.repo.refs(MIRROR_PRESENCE)
        folder = self.state_dir / "presence"
        wanted = {}
        blobs = self.repo.read_blobs(mirrors.values())
        for member, sha in mirrors.items():
            if member == pin.member or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$", member):
                continue
            data = blobs.get(sha)
            if not data or len(data) > MAX_PRESENCE_BYTES:
                continue
            try:
                doc = json.loads(data)
            except ValueError:
                continue
            if not isinstance(doc, dict) or doc.get("member") != member:
                continue
            wanted[member] = data
        folder.mkdir(parents=True, exist_ok=True)
        changed = False
        for member, data in wanted.items():
            path = folder / f"{member}.json"
            try:
                current = path.read_bytes() if path.exists() else None
            except OSError:
                current = None
            if current != data:
                _write_bytes(path, data)
                changed = True
        for path in folder.glob("*.json"):
            if path.stem != pin.member and path.stem not in wanted:
                path.unlink(missing_ok=True)
                changed = True
        people = read_presence(self.project_dir)
        signature = [(p["member"], p.get("goal"), p.get("here")) for p in people]
        if changed or signature != [(p["member"], p.get("goal"), p.get("here")) for p in self._peers]:
            self._peers = people
            self.bus.publish(PRESENCE_CHANGED, project=str(self.project_dir), people=people)

    # ── certificates and the access list ──

    def _take_certs(self, pin: Pin) -> None:
        head = self.repo.rev(MIRROR_CERTS)
        if not head or head == self._certs_seen or pin.mode == "folder":
            return
        device = _device(self.data_dir)
        if device is None:
            return
        tag = _key_tag(device.public_key)
        texts = {}
        for kind in ("sync", "dev"):
            data = self.repo.blob(f"{head}:{pin.member}/{tag}.{kind}")
            texts[kind] = data.decode(errors="replace") if data else ""
        install_certificates(device, pin, texts)
        self._certs_seen = head

    def _check_access(self, pin: Pin) -> None:
        head = self.repo.rev(MIRROR_ACCESS)
        if not head or head == self._access_seen or pin.mode == "folder":
            return
        state, parsed = verify_access(pin.ca, pin.project_id, _access_files(self.repo, head))
        if state == "bad":
            log.warning("the home's access list isn't signed by this project or an owner's computer; ignoring it")
            self._access_seen = head
            return
        if parsed["version"] < pin.access_version:
            self._access_seen = head
            return
        device = _device(self.data_dir)
        mine = [s for s in parsed["seats"] if device and s.member == pin.member
                and security.same_secret(s.fingerprint, device.fingerprint)]
        if not mine and not self._is_host(pin):
            self._access_seen = head
            raise AccessRemoved("An owner took this computer off the project. Your copy stays here; it just "
                                "doesn't sync any more.")
        before = asdict(pin)
        if mine:
            pin.role = mine[0].role
        home = parsed.get("home") or {}
        if home.get("home") and (home.get("home") != pin.home or parsed["hosts"] != pin.hosts
                                 or (parsed.get("host_key") or "") != (pin.host_key or "")):
            try:
                _validate_home({**pin.home_record(), **home, "hosts": parsed["hosts"],
                                "host_key": parsed.get("host_key") or ""})
                pin.home, pin.user, pin.port = home["home"], home.get("user", pin.user), home.get("port", pin.port)
                pin.hosts, pin.host_key = parsed["hosts"], parsed.get("host_key") or pin.host_key
            except (SyncError, security.SecurityError, ValueError):
                log.warning("ignoring a home move with unsafe details")
        if parsed.get("gate") and re.match(r"^/[A-Za-z0-9_./+@-]{1,400}$", parsed["gate"]):
            pin.gate = parsed["gate"]
        pin.trust = parsed.get("trust") or pin.trust
        pin.issuers = _owner_issuers(parsed)
        pin.access_version = parsed["version"]
        pin.access_expires = float(parsed["expires"])
        if asdict(pin) != before:
            if pin.issuers != before.get("issuers"):
                self._certs_seen = ""        # passes signed by a newly trusted owner computer can come in now
            if pin.role != before.get("role") or pin.issuers != before.get("issuers"):
                self._owner_dirty = True
            pin.save(self.data_dir)
        if OwnerDesk.available(pin.project_id, self.data_dir) and parsed.get("signer") and device is not None \
                and not security.same_secret(security.fingerprint(parsed["signer"]), device.fingerprint):
            self._owner_dirty = True            # another owner changed things: keep this computer's records in step
        self._access_seen = head

    def _owner_duties(self, pin: Pin) -> None:
        if not OwnerDesk.available(pin.project_id, self.data_dir):
            return
        try:      # membership changed elsewhere (People, Places): re-sign the access list now
            ca_state = (self.data_dir / "ca" / pin.project_id / "state.json").stat().st_mtime_ns
        except OSError:
            ca_state = 0
        if ca_state != getattr(self, "_ca_state_seen", None):
            self._ca_state_seen = ca_state
            self._owner_dirty = True
        if pin.access_expires and pin.access_expires - time.time() < ACCESS_REFRESH_S:
            self._owner_dirty = True
        if not self._owner_dirty and time.monotonic() - self._last_owner_duty < OWNER_DUTY_EVERY_S:
            return
        if pin.mode == "this-computer" and not pin.host_key:
            pin.host_key = _this_computer_host_key()       # sshd may have been switched on since
            if pin.host_key:
                pin.save(self.data_dir)
        try:
            desk = OwnerDesk(self.project_dir, self.data_dir)
            desk.publish_access()
            desk.renew_certs()
            self._pin_cache = desk.pin
        except (SyncError, security.SecurityError, StoreError) as exc:
            log.info("owner duties skipped: %s", exc)
        self._last_owner_duty = time.monotonic()
        self._owner_dirty = False

    def _owner_refspecs(self, pin: Pin) -> list[str]:
        if pin.role != "owner" and not OwnerDesk.available(pin.project_id, self.data_dir):
            return []
        out = []
        mine, theirs = self.repo.rev(ACCESS_REF), self.repo.rev(MIRROR_ACCESS)
        if mine and mine != theirs and self._list_version(mine) > self._list_version(theirs):
            # The home keeps whichever properly signed list is newest (its hook checks), so a list that
            # didn't grow from the home's can still replace it; the other owner's pending changes come back.
            out.append(f"+{mine}:{ACCESS_REF}")
        mine, theirs = self.repo.rev(CERTS_REF), self.repo.rev(MIRROR_CERTS)
        if mine and mine != theirs and not (theirs and self.repo.is_ancestor(mine, theirs)):
            out.append(f"{mine}:{CERTS_REF}")
        return out

    def _list_version(self, rev: str | None) -> int:
        if not rev:
            return 0
        data = self.repo.blob(f"{rev}:access") or b""
        match = re.search(rb"^version (\d+)$", data, re.MULTILINE)
        return int(match.group(1)) if match else 0

    def _push_owner_refs(self, pin: Pin, remote: str, env: dict[str, str]) -> None:
        """A newer access list (and passes) go out on their own, before the rest: a home whose list has run
        out (or someone put an old one back) takes only those from an owner, then lets everything else in again
        in the same round."""
        refspecs = self._owner_refspecs(pin)
        if not refspecs or pin.mode == "folder":
            return
        try:
            self._push(remote, env, refspecs)
        except GitFailed:
            log.info("sending the fresh access list first didn't work; the round tries again", exc_info=True)

    def _renewal(self, pin: Pin) -> dict:
        """When this computer's pass and the project's access list run out, and whether to say so."""
        device = _device(self.data_dir)
        now = time.time()
        expires = pass_expiry(device, pin.project_id) if pin.mode != "folder" else 0.0
        out: dict[str, Any] = {"pass_expires": expires, "access_expires": pin.access_expires, "renew_due": False,
                               "renew_words": ""}
        if pin.mode == "folder":
            return out
        signs_itself = OwnerDesk.available(pin.project_id, self.data_dir) and (
            (self.data_dir / "ca" / pin.project_id / "ca_ed25519").exists()
            or (device is not None and pin.trust == "live" and device.fingerprint in pin.issuers))
        if signs_itself:
            return out
        who = _first(pin.owner_name) or "an owner"
        if expires and expires - now < PASS_WARN_S:
            out["renew_due"] = True
            out["renew_words"] = (f"Your pass to the project home runs out {_when_words(expires, now)}. It renews by "
                                  f"itself when an owner's Playground is open. If nobody will be around, ask {who} for "
                                  "a fresh pass in People.")
        elif pin.access_expires and pin.access_expires - now < 86400:
            out["renew_due"] = True
            out["renew_words"] = (f"The project's access list runs out {_when_words(pin.access_expires, now)} unless an "
                                  f"owner opens Playground before then. If nobody will be around, let {who} know.")
        return out

    def _restore_tags_round(self, pin: Pin) -> list[str]:
        """Everyone's restore points: ours go to the home, teammates' come here as tags (never moved once made)."""
        repo = self.repo
        remote = repo.refs(REMOTE_RESTORE)
        local = repo.refs(RESTORE_TAGS)
        for name, sha in remote.items():
            if name not in local and _RESTORE_NAME.match(name):
                repo.update_ref(RESTORE_TAGS + name, sha, "")
        if pin.role not in ("owner", "builder"):
            return []
        prefix = f"{pin.member}/"
        return [f"{sha}:{RESTORE_TAGS}{name}" for name, sha in local.items()
                if name.startswith(prefix) and _RESTORE_NAME.match(name) and name not in remote]

    # ── code ──

    def _code_round(self, pin: Pin) -> dict:
        repo = self.repo
        out: dict[str, Any] = {"push": [], "behind": 0, "ahead": 0, "branch": "", "conflict": "", "note": ""}
        current = repo.current_branch()
        out["branch"] = current or ""
        heads = repo.refs("refs/heads/")
        remotes = repo.refs(REMOTE_HEADS)
        may_push = pin.role in ("owner", "builder")
        for name, sha in heads.items():
            if not (name == MAIN or name.startswith(GOAL_PREFIX)):
                continue
            theirs = remotes.get(name)
            if theirs == sha:
                continue
            if theirs is None or repo.is_ancestor(theirs, sha):
                if may_push:
                    out["push"].append(f"{sha}:refs/heads/{name}")
                if name == current:
                    out["ahead"] = int(repo.text("rev-list", "--count", f"{theirs}..{sha}") if theirs else 0) or 0
                continue
            if repo.is_ancestor(sha, theirs):
                if name != current:
                    repo.update_ref(f"refs/heads/{name}", theirs, sha)
                elif not repo.dirty() and repo.run("merge", "--ff-only", "-q", theirs, check=False).returncode == 0:
                    pass
                else:
                    out["behind"] = int(repo.text("rev-list", "--count", f"{sha}..{theirs}") or 0)
                    out["note"] = ("Teammates' changes to this branch are waiting. They come in once your "
                                   "work in progress is saved.")
                continue
            if name == current or name == MAIN:
                who = "Teammates" if name == MAIN else "A teammate"
                label = "main" if name == MAIN else f"the goal {name[len(GOAL_PREFIX):]}"
                out["conflict"] = (f"{who} and you both changed {label}. Ask Ari to bring the two together, "
                                   "then it syncs again.")
        return out

    def goal_branch(self, goal_id: str) -> str:
        if not _paths.is_valid_id(goal_id):
            raise SyncError("That goal id can't be a branch name.")
        return GOAL_PREFIX + goal_id

    def checkpoint(self, message: str | None = None, goal_id: str | None = None) -> str | None:
        """Commit the work in progress on the current goal branch. Returns the commit, or None."""
        with self._round_lock:
            repo = self.repo
            branch = repo.current_branch()
            if not branch or not branch.startswith(GOAL_PREFIX):
                return None
            if goal_id and branch != self.goal_branch(goal_id):
                return None
            if not repo.anything_to_save():
                return None
            pin = self.pin()
            member = pin.member if pin else ""
            name = self._my_name(pin) if pin else ""
            title = message
            if not title:
                try:
                    title = f"Work on {self.store.read_goal(branch[len(GOAL_PREFIX):]).title}"
                except Exception:  # noqa: BLE001
                    title = "Work in progress"
            repo.run("add", "-A")
            who = _who(member, name)
            env = {"GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1], "GIT_COMMITTER_NAME": who[0],
                   "GIT_COMMITTER_EMAIL": who[1]}
            repo.run("-c", "commit.gpgSign=false", "commit", "-q", "--no-verify", "-m", title, env=env)
            return repo.rev("HEAD")

    def begin_goal(self, goal_id: str, switch: bool = True) -> dict:
        """Make sure goal work happens on ``goal/<goal_id>``; switch to it when that's safe."""
        with self._round_lock:
            repo = self.repo
            branch = self.goal_branch(goal_id)
            heads = repo.refs("refs/heads/")
            remotes = repo.refs(REMOTE_HEADS)
            if branch not in heads:
                start = remotes.get(branch)
                if start is None:
                    main, remote_main = heads.get(MAIN), remotes.get(MAIN)
                    if remote_main and (not main or repo.is_ancestor(main, remote_main)):
                        start = remote_main
                    else:
                        start = main or repo.rev("HEAD")
                if not start:
                    raise SyncError("The project has no code saved yet, so there's nothing to branch from.")
                repo.run("branch", branch, start)
            current = repo.current_branch()
            if not switch or current == branch:
                return {"branch": branch, "switched": current == branch, "reason": ""}
            if current and current.startswith(GOAL_PREFIX):
                self.checkpoint(goal_id=current[len(GOAL_PREFIX):])
            proc = repo.run("switch", "-q", branch, check=False)
            if proc.returncode != 0:
                return {"branch": branch, "switched": False,
                        "reason": "There are unsaved changes that clash with this goal's code. Save or undo them "
                                  "first."}
            self._wake.set()
            return {"branch": branch, "switched": True, "reason": ""}

    def release_goal(self, goal_id: str, title: str = "") -> dict:
        """Merge the goal's branch into main and send it to the home. Raises SyncConflict with plain words."""
        with self._round_lock:
            repo = self.repo
            pin = self.pin()
            branch = self.goal_branch(goal_id)
            remote = env = None
            if pin is not None:
                try:
                    remote, env = self._remote(pin)
                    self._fetch(remote, env)
                except SyncError:
                    remote = None
            if repo.current_branch() == branch:
                self.checkpoint(goal_id=goal_id)
            if not repo.rev(f"refs/heads/{branch}"):
                raise SyncError("This goal has no code of its own to release.")
            if repo.dirty():
                raise SyncError("There are unsaved changes in the project folder. Save or undo them, then release.")
            title = title or goal_id
            original = repo.current_branch()
            if not repo.rev(f"refs/heads/{MAIN}"):
                repo.run("branch", MAIN, repo.rev(REMOTE_HEADS + MAIN) or branch)
            repo.run("switch", "-q", MAIN)
            try:
                remote_main = repo.rev(REMOTE_HEADS + MAIN)
                if remote_main and not repo.is_ancestor(remote_main, "HEAD"):
                    self._merge_or_explain(remote_main, f"Bring in teammates' releases", "the latest main")
                self._merge_or_explain(branch, f"Release: {title}", f"‘{title}’")
            except SyncConflict:
                if original and original != MAIN:
                    repo.run("switch", "-q", original, check=False)
                raise
            commit = repo.rev("HEAD")
            pushed = False
            if remote is not None and pin is not None and pin.role in ("owner", "builder"):
                result = self._push(remote, env or {}, [f"refs/heads/{MAIN}:refs/heads/{MAIN}",
                                                        f"refs/heads/{branch}:refs/heads/{branch}"])
                pushed = result.get(f"refs/heads/{MAIN}", ("!",))[0] != "!"
            self._wake.set()
            return {"commit": commit, "branch": MAIN, "pushed": pushed}

    def _merge_or_explain(self, rev: str, message: str, what: str) -> None:
        repo = self.repo
        pin = self.pin()
        who = _who(pin.member if pin else "", self._my_name(pin) if pin else "")
        env = {"GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1], "GIT_COMMITTER_NAME": who[0],
               "GIT_COMMITTER_EMAIL": who[1]}
        proc = repo.run("-c", "commit.gpgSign=false", "merge", "--no-ff", "--no-edit", "-q", "-m", message, rev,
                        env=env, check=False)
        if proc.returncode == 0:
            return
        files = repo.text("diff", "--name-only", "--diff-filter=U").splitlines()
        repo.run("merge", "--abort", check=False)
        named = ", ".join(files[:3]) + (f" and {len(files) - 3} more" if len(files) > 3 else "")
        raise SyncConflict(f"{what} and main both changed {named or 'the same files'}. Ask Ari to bring the goal "
                           "up to date with main, then release again.")

    def bring_in_changes(self) -> dict:
        """Merge teammates' commits on the current branch into ours (when a fast-forward isn't possible)."""
        with self._round_lock:
            repo = self.repo
            branch = repo.current_branch()
            theirs = repo.rev(REMOTE_HEADS + branch) if branch else None
            if not branch or not theirs or repo.is_ancestor(theirs, "HEAD"):
                return {"merged": False}
            if repo.dirty():
                raise SyncError("Save or undo the work in progress first.")
            self._merge_or_explain(theirs, f"Bring in teammates' work on {branch}", "Your work")
            self._wake.set()
            return {"merged": True, "commit": repo.rev("HEAD")}

    # ── previews on the Dev server ──

    def stage_place(self) -> dict:
        """A place for core.preview: this person's own checkout of their branch on the Dev server.

        Pushes the current branch first. Builders and owners only; it uses the
        development certificate, which gives a shell for running the app.
        """
        pin = self.pin()
        if pin is None or pin.mode != "server":
            raise SyncError("Previews run on the Dev server only when the project lives there.")
        if pin.role not in ("owner", "builder"):
            raise SyncError("Only owners and builders can run the app on the Dev server.")
        self.sync_now()
        branch = self.repo.current_branch() or MAIN
        if not re.match(r"^[A-Za-z0-9._/-]+$", branch):
            raise SyncError("This branch name can't be used on the server.")
        device = self._device()
        known, alias = _known_hosts(self.data_dir, pin)
        argv, env = _ssh_argv(device, device.certificate_path(pin.project_id), pin.port, known, alias)
        stage = f"{pin.home.rstrip('/')}/stage/{pin.member}"
        script = "\n".join([
            "set -eu", f"H={shlex.quote(pin.home)}", f"W={shlex.quote(stage)}", f"B={shlex.quote(branch)}",
            'mkdir -p "$H/stage"',
            '[ -d "$W/.git" ] || git clone -q --no-checkout "$H/repo.git" "$W"',
            'git -C "$W" fetch -q origin "+refs/heads/${B}:refs/remotes/origin/${B}"',
            'git -C "$W" checkout -q --force --detach "refs/remotes/origin/${B}"', 'echo "$W"'])
        proc = subprocess.run([*argv, "--", f"{pin.user}@{self._pick_host(pin)}", _remote_sh(script)],
                              capture_output=True,
                              text=True, timeout=120, stdin=subprocess.DEVNULL, env={**os.environ, **env})
        if proc.returncode != 0:
            kind, words = _classify(proc.stderr or "")
            raise SyncError(words or "Couldn't get the app ready on the Dev server.")
        project = self.store.read_project()
        place = next((p.to_dict() for p in project.places if p.kind == "ssh" and p.role == "development"
                      and p.host in pin.hosts), None) or {"id": "dev-server", "name": pin.host_name, "role": "development",
                                                          "kind": "ssh", "host": pin.hosts[0], "user": pin.user,
                                                          "port": pin.port}
        place["path"] = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else stage
        return place

    # ── first push (owner, when sharing) ──

    def first_push(self, *, dev_cert: bool = False) -> None:
        """Send main, the records, the access list and the passes to a new home."""
        pin = self.pin()
        if pin is None:
            raise SyncError("This project isn't shared yet.")
        _ensure_state_files(self.project_dir)
        with self.store.locked():
            snap = self._snapshot(pin)
        if dev_cert:
            device = self._device()
            known, alias = _known_hosts(self.data_dir, pin)
            argv, env = _ssh_argv(device, device.certificate_path(pin.project_id), pin.port, known, alias)
            remote = f"ssh://{pin.user}@{_url_host(pin.hosts[0])}:{int(pin.port)}{pin.repo_path}"
            env = {**env, "GIT_SSH_COMMAND": shlex.join(argv), "GIT_SSH_VARIANT": "ssh"}
        else:
            remote, env = self._remote(pin)
        refspecs = [f"refs/heads/{MAIN}:refs/heads/{MAIN}"]
        if snap:
            refspecs.append(f"{snap}:{STATE_REF}")
        for ref in (ACCESS_REF, CERTS_REF):
            if self.repo.rev(ref):
                refspecs.append(f"{ref}:{ref}")
        proc = self.repo.run("push", "--porcelain", "--no-verify", remote, *refspecs, env=env, check=False,
                             timeout=180)
        if proc.returncode != 0:
            kind, words = _classify(proc.stderr.decode(errors="replace"))
            if kind == "offline":
                raise _Offline(self._offline_words(pin))
            raise SyncError(words)
        self._fetch(remote, env)
        pin.synced_once = True
        pin.save(self.data_dir)


# ════════════════════════════════════ presence (read side) ════════════════════════════════════

def read_presence(project_dir: Path | str, now: datetime | None = None) -> list[dict]:
    """Everyone's last heartbeat, from ``.playground/presence/``. ``here`` is True for a fresh one."""
    folder = Path(project_dir) / ".playground" / "presence"
    now = now or _now()
    people = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            if path.stat().st_size > MAX_PRESENCE_BYTES:
                continue
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or doc.get("member") != path.stem:
            continue
        moment = _parse_iso(doc.get("ts"))
        fresh = bool(moment and (now - moment).total_seconds() < PRESENCE_FRESH_S)
        doc["here"] = fresh and doc.get("state") != "left"
        people.append(doc)
    return people


# ════════════════════════════════════ preparing a project to be shared ════════════════════════════════════

def _ensure_state_files(project_dir: Path) -> None:
    folder = Path(project_dir) / ".playground"
    for name, text in ((".gitignore", STATE_GITIGNORE), (".gitattributes", STATE_GITATTRIBUTES)):
        path = folder / name
        try:
            current = path.read_text()
        except OSError:
            current = None
        if current is None:
            _write_bytes(path, text.encode())


def _prepare_code_repo(project_dir: Path, project, who: tuple[str, str]) -> _Repo:
    from . import project as project_module
    if _git(project_dir, "rev-parse", "--git-dir", check=False).returncode != 0:
        if not project_module.init_git(project_dir, f"Start {project.name}"):
            raise SyncError("Playground couldn't set up git in this project.")
    repo = _Repo(project_dir)
    env = {"GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1], "GIT_COMMITTER_NAME": who[0],
           "GIT_COMMITTER_EMAIL": who[1]}
    if not repo.rev("HEAD"):
        repo.run("add", "-A")
        repo.run("-c", "commit.gpgSign=false", "commit", "-q", "--no-verify", "--allow-empty", "-m",
                 f"Start {project.name}", env=env)
    current = repo.current_branch()
    heads = repo.refs("refs/heads/")
    if MAIN not in heads:
        if current and current in ("master", "trunk") and current in heads:
            repo.run("branch", "-m", current, MAIN)
        else:
            repo.run("branch", MAIN, "HEAD")
    gitignore = project_dir / ".gitignore"
    text = gitignore.read_text() if gitignore.exists() else ""
    if CODE_IGNORE_MARK not in text:
        _write_bytes(gitignore, (text.rstrip("\n") + "\n" + CODE_IGNORE).lstrip("\n").encode())
    repo.run("rm", "-r", "-q", "--cached", "--ignore-unmatch", ".playground", "AGENTS.md", ".mcp.json")
    repo.run("add", ".gitignore")
    if repo.run("diff", "--cached", "--quiet", check=False).returncode != 0:
        repo.run("-c", "commit.gpgSign=false", "commit", "-q", "--no-verify", "-m",
                 f"Share {project.name}: Playground's records now sync on their own", env=env)
    return repo


def _host_key_from_known_hosts(known_hosts: Path, host: str, port: int) -> str:
    name = host if int(port) == 22 else f"[{host}]:{int(port)}"
    proc = subprocess.run([_keygen(), "-F", name, "-f", str(known_hosts)], capture_output=True, text=True,
                          timeout=15, stdin=subprocess.DEVNULL)
    keys = [line.split(None, 1)[1] for line in proc.stdout.splitlines()
            if line.strip() and not line.startswith("#") and len(line.split()) >= 3]
    for key in keys:
        if key.startswith("ssh-ed25519 "):
            return security.normalize_public_key(key)
    raise SyncError("Playground couldn't find the server's ed25519 identity. Test the connection in Places first.")


def local_addresses() -> list[str]:
    """Addresses teammates might reach this computer on: the LAN address, Tailscale, and name.local."""
    found: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 9))          # no packet is sent; this picks the outgoing interface
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    tool = shutil.which("tailscale")
    if tool:
        try:
            out = subprocess.run([tool, "ip", "-4"], capture_output=True, text=True, timeout=3).stdout
            found += [line.strip() for line in out.splitlines() if line.strip()]
        except (OSError, subprocess.SubprocessError):
            pass
    for argv in (["ip", "-o", "-4", "addr", "show"], ["ifconfig"]):
        if not shutil.which(argv[0]):
            continue
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for match in re.finditer(r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", out):
            address = ipaddress.ip_address(match.group(1))
            if address in ipaddress.ip_network("100.64.0.0/10") or (address.is_private and not address.is_loopback):
                found.append(str(address))
        break
    name = socket.gethostname().split(".")[0]
    if name and re.match(r"^[A-Za-z0-9-]+$", name):
        found.append(f"{name}.local")
    out = []
    for address in found:
        if address and address not in out and not address.startswith("127."):
            out.append(address)
    return out


def _this_computer_host_key() -> str:
    for path in ("/etc/ssh/ssh_host_ed25519_key.pub", "/etc/ssh_host_ed25519_key.pub"):
        try:
            return security.normalize_public_key(Path(path).read_text())
        except (OSError, security.SecurityError):
            continue
    return ""


def share_project(project_dir: Path | str, *, mode: str = "server", place_id: str | None = None,
                  home: str | None = None, user: str | None = None, port: int = 22,
                  addresses: list[str] | None = None, host_key: str | None = None,
                  trust_dir: str = security.SERVER_DIR, trust: str = "live",
                  bus: events.EventBus | None = None) -> dict:
    """Give a project a home teammates can sync with. Run on the owner's computer.

    ``mode`` is "server" (``place_id``: a Development place reachable with this
    device's certificate, after the one-time setup), "this-computer" (this
    computer's sshd serves it; teammates can only work while it's on) or
    "folder" (``home``: a path for a bare repository; tests and advanced use).
    For a server, ``trust`` says which setup ran there: "live" (the root setup:
    every sign-in is checked against the access list, so removal is immediate
    for shells too) or "account" (no sudo: syncing still stops at once, shells
    end when the pass runs out). Returns ``{mode, home, warning, safety, setup, trust}``.
    """
    if mode not in MODES:
        raise SyncError("Pick where the project lives: a server, this computer or a folder.")
    if trust not in TRUST_MODES:
        raise SyncError("Pick how the server was set up: with sudo, or without.")
    project_dir = Path(project_dir).expanduser().absolute()
    store = ProjectStore(project_dir, bus=bus)
    project = store.read_project()
    owner = project.owner
    if owner is None:
        raise SyncError("Only the project's owner can share it, and this project has no owner yet.")
    data_dir = _paths.data_dir()
    who = _who(owner.id, owner.name)
    _prepare_code_repo(project_dir, project, who)
    security.device_key(create=True)
    device = _device(data_dir)
    assert device is not None
    ca = security.ProjectCA.open(project.id, create=True)
    ca.add_member(owner.id, owner.name, "owner", device.public_key, device.device_name)
    _ensure_state_files(project_dir)
    trust_root = trust_dir.rstrip("/") if trust_dir else ""
    project_trust = f"{trust_root}/projects/{project.id}" if trust_root else ""
    ca_pub_path = f"{project_trust}/ca.pub" if project_trust else ""
    pin = Pin(project_id=project.id, member=owner.id, role="owner", mode=mode, name=project.name, ca=ca.public_key,
              owner_device=device.fingerprint, owner_name=owner.name, trust="account")
    setup = None
    if mode == "folder":
        if not home:
            raise SyncError("Say which folder holds the shared repository.")
        pin.home = str(_make_folder_home(Path(home).expanduser().absolute()))
        pin.host_name = "the shared folder"
    elif mode == "this-computer":
        location = Path(home).expanduser().absolute() if home else data_dir / "homes" / project.id
        pin.home = str(_make_local_home(project.id, location, ca_pub_path))
        pin.user = user or getpass.getuser()
        pin.port = int(port or 22)
        pin.hosts = list(addresses) if addresses else local_addresses()
        pin.host_key = security.normalize_public_key(host_key) if host_key else _this_computer_host_key()
        pin.host_name = f"{_first(owner.name)}’s computer" if owner.name else "the owner’s computer"
        _validate_home(pin.home_record())
        setup = home_setup_instructions(project_dir, mode="this-computer", user=pin.user, trust_dir=trust_dir)
    else:
        from . import places
        place_obj = project.place(place_id or project.home)
        if place_obj is None or place_obj.kind != "ssh":
            raise SyncError("Pick the Development server the project should live on.")
        if place_obj.role == "production":
            raise SyncError("The project can't live on the production server. Pick a development server.")
        place = place_obj.to_dict()
        ca.issue(owner.id, device.public_key, out_path=device.certificate_path(project.id))
        location = home or f"playground/{project.id}"
        if not re.match(r"^[A-Za-z0-9_./~+@-]+$", location) or ".." in location.split("/"):
            raise SyncError("The folder on the server can't have spaces or odd characters in it.")
        pin.trust = trust
        if trust == "live":
            _safe_path(project_trust)
            pin.gate = f"{project_trust}/gate"
            script = _home_setup_shell(project.id, location if location.startswith("/") else f"\"$HOME\"/{location}",
                                       ca_pub_path, checkout=True, hooks=f"{project_trust}/hooks")
        else:
            script = _home_setup_shell(project.id, location if location.startswith("/") else f"\"$HOME\"/{location}",
                                       "home", checkout=True, ca_public_key=ca.public_key)
        known = places.known_hosts_file()
        argv, env = _ssh_argv(device, device.certificate_path(project.id), int(place.get("port") or 22), known,
                              None, strict="accept-new", timeout=10)
        proc = subprocess.run([*argv, "--", places.destination(place), _remote_sh(script)], capture_output=True,
                              text=True,
                              timeout=120, stdin=subprocess.DEVNULL, env={**os.environ, **env})
        marker = [line for line in proc.stdout.splitlines() if line.startswith("PLAYGROUND_HOME=")]
        if proc.returncode != 0 or not marker:
            kind, words = _classify(proc.stderr or "")
            if kind == "access":
                words = ("The server didn't let this computer in yet. Run the one-time setup on it first, then try "
                         "again.")
            raise SyncError(words or "Playground couldn't set the project up on the server.")
        pin.home = marker[-1].split("=", 1)[1].strip()
        pin.user, pin.port, pin.hosts = place["user"], int(place.get("port") or 22), [place["host"]]
        pin.host_key = _host_key_from_known_hosts(known, place["host"], pin.port)
        pin.host_name = place_obj.name or "the Dev server"
        _validate_home(pin.home_record())
    pin.save(data_dir)
    desk = OwnerDesk(project_dir, data_dir)
    desk.publish_access(force=True)
    desk.renew_certs(force_members=[owner.id])
    with store.locked():
        current = store.read_project()
        tables = current.extra.setdefault("tables", {})
        tables["sharing"] = {"mode": mode, "home": pin.host_name, "since": _iso(), "by": owner.id,
                             "ca_fingerprint": ca.fingerprint, "trust": pin.trust}
        if mode == "server" and place_id:
            current.home = place_id
        me = current.member(owner.id)
        if me is not None:
            devices = list(me.extra.get("devices") or [])
            if device.fingerprint not in devices:
                devices.append(device.fingerprint)
            me.extra["devices"] = devices
        store.write_project(current)
    store.log(f"Shared {project.name}: it now lives on {pin.host_name}", kind="people", actor=owner.id)
    _write_marker(project_dir, project.id, owner.id)
    engine = SyncEngine(project_dir, bus=bus, data_dir=data_dir)
    engine.first_push(dev_cert=(mode == "server"))
    return {"mode": mode, "home": pin.host_name, "warning": THIS_COMPUTER_WARNING if mode == "this-computer" else "",
            "safety": safety_code(ca.public_key), "setup": setup, "trust": pin.trust}


TRUST_WORDS = {
    "live": ("Taking someone off works at once, for syncing and for shells: the server checks the project's "
             "signed access list every time anyone signs in."),
    "account": ("Without sudo, the trust lives in the server account itself. Taking someone off still stops their "
                "syncing at once. But anyone who could open a shell there (a builder or owner) keeps working "
                "shell access until their pass runs out, within 12 hours, and while they had a shell they could "
                "have changed the account's own settings. If you remove someone you don't trust, check the "
                "account yourself, or run the setup with sudo when you can."),
}


def home_setup_instructions(project_dir: Path | str, *, mode: str = "server", place_id: str | None = None,
                            user: str | None = None, trust_dir: str = security.SERVER_DIR, trust: str = "live",
                            home: str | None = None) -> dict:
    """The one-time step that lets a home trust this project's passes.

    For a server with ``trust="live"`` (recommended): a root script that installs root-owned checks, so every
    sign-in is decided from the signed access list (``live_setup_script``). With ``trust="account"``: a script
    run as the server account, no sudo, with the weaker guarantee in ``words``. For this computer: core.security's
    root setup trusting **only** the sync principal, so teammates get git on this one project and never a shell.
    Returns ``{script, command, explanation, words, trust, principals, steps?}``.
    """
    project = ProjectStore(project_dir).read_project()
    ca = security.ProjectCA.open(project.id, create=True)
    sync = sync_principal(project.id)
    if mode == "server":
        if trust not in TRUST_MODES:
            raise SyncError("Pick how the server should be set up.")
        place_obj = project.place(place_id or project.home)
        if place_obj is None or place_obj.kind != "ssh":
            raise SyncError("Pick the Development server first.")
        place = place_obj.to_dict()
        from . import places
        places.validate_user(str(place.get("user") or ""))
        port = int(place.get("port") or 22)
        destination = f"{place['user']}@{place['host']}"
        name = place_obj.name or place["host"]
        if trust == "live":
            security.device_key(create=True)
            device = _device(_paths.data_dir())
            assert device is not None
            owner = project.owner
            founder = (owner.id if owner else "owner", device.fingerprint)
            pin = Pin.load(_paths.data_dir(), project.id)
            location = home or (_home_relative(pin, place) if pin else "") or f"playground/{project.id}"
            script = live_setup_script(project.id, place["user"], location, ca, founder, trust_dir=trust_dir)
            remote = f"printf %s {base64.b64encode(script.encode()).decode()} | base64 -d | sudo sh"
            return {"script": script, "command": f"ssh -t -p {port} {shlex.quote(destination)} {shlex.quote(remote)}",
                    "principals": [sync], "trust": "live", "words": TRUST_WORDS["live"],
                    "explanation": (f"Run this once on {name} as someone who can use sudo. It lets teammates sign "
                                    f"in as {place['user']} with their own passes, and from then on the server "
                                    "checks the project's signed access list at every sign-in. You never need "
                                    "to run it again after taking someone off.")}
        script = account_setup_script(project.id, ca)
        remote = f"printf %s {base64.b64encode(script.encode()).decode()} | base64 -d | sh"
        return {"script": script, "command": f"ssh -p {port} {shlex.quote(destination)} {shlex.quote(remote)}",
                "principals": [sync], "trust": "account", "words": TRUST_WORDS["account"],
                "explanation": (f"Run this once from a computer that can already sign in to {name} as "
                                f"{place['user']}. No sudo needed: it adds the project's key to that account, "
                                "limited to the project's passes.")}
    user = user or getpass.getuser()
    place = {"role": "development", "user": user, "name": "this computer"}
    info = security.server_setup_instructions(place, ca)
    script = info["script"].replace(info["principal"], sync)
    script = script.replace("systemctl reload sshd 2>/dev/null || systemctl reload ssh 2>/dev/null || service ssh reload",
                            "systemctl reload sshd 2>/dev/null || systemctl reload ssh 2>/dev/null || "
                            "service ssh reload 2>/dev/null || true")
    if trust_dir != security.SERVER_DIR:
        script = script.replace(security.SERVER_DIR, trust_dir)
    encoded = base64.b64encode(script.encode()).decode()
    steps = ["Turn on Remote Login (macOS: System Settings › General › Sharing) or start the SSH service "
             "(Luma and Linux: sudo systemctl enable --now sshd).",
             "Then run the command below once, in Terminal. It asks for your password because it changes "
             "how this computer's SSH trusts sign-ins."]
    return {"script": script, "command": f"printf %s {encoded} | base64 -d | sudo sh", "principals": [sync],
            "steps": steps, "trust": "account",
            "words": "Teammates never get a shell here, so taking someone off works at once.",
            "explanation": "Teammates sign in with passes from this project that can only run git on this one "
                           "project: no shell, no files outside it."}


def _home_relative(pin: Pin, place: dict) -> str:
    """The home folder as the setup script wants it, for a project that's already shared there."""
    if pin.mode != "server" or not pin.home or place.get("host") not in pin.hosts:
        return ""
    return pin.home


def check_this_computer(project_dir: Path | str, trust_dir: str = security.SERVER_DIR, port: int = 22) -> dict:
    """Plain-words checklist for hosting on this computer."""
    project = ProjectStore(project_dir).read_project()
    sshd = _reachable("127.0.0.1", port, timeout=0.5)
    ca_pub = Path(trust_dir) / "projects" / project.id / "ca.pub"
    trusted = False
    try:
        ca = security.ProjectCA.load(project.id)
        trusted = bool(ca and ca_pub.exists() and ca.public_key in ca_pub.read_text())
    except (OSError, security.SecurityError):
        trusted = False
    return {"sshd": sshd, "trusted": trusted, "addresses": local_addresses(),
            "ready": sshd and trusted,
            "words": ("Ready for teammates." if sshd and trusted else
                      "SSH isn't switched on yet." if not sshd else
                      "Run the one-time setup so this computer trusts the project's passes.")}


# ════════════════════════════════════ invites and joining ════════════════════════════════════

@dataclass
class Signed:
    """A signed envelope: an invite, a request to join, or a welcome."""
    kind: str
    body: dict
    sig: str
    extra: dict = field(default_factory=dict)

    @property
    def code(self) -> str:
        return encode_code(self.kind, {"body": self.body, "sig": self.sig})

    @property
    def file_text(self) -> str:
        return json.dumps({"body": self.body, "sig": self.sig}, indent=2, sort_keys=True) + "\n"

    @property
    def file_name(self) -> str:
        base = re.sub(r"[^A-Za-z0-9 ._-]+", "", str((self.body.get("project") or {}).get("name") or "Project"))
        return f"{base.strip() or 'Project'}{FILE_SUFFIX[self.kind]}"

    @property
    def project_id(self) -> str:
        return str((self.body.get("project") or {}).get("id") or self.body.get("project") or "")

    @property
    def project_name(self) -> str:
        return str((self.body.get("project") or {}).get("name") or "")

    @property
    def safety(self) -> str:
        ca = self.body.get("ca") or self.extra.get("ca") or ""
        return safety_code(ca) if ca else ""


def _invites_path(ca: security.ProjectCA) -> Path:
    return ca.dir / "invites.json"


def create_invite(project_dir: Path | str, *, role: str = "builder", member_id: str = "", name: str = "",
                  for_whom: str = "", days: float = INVITE_DAYS) -> Signed:
    """A signed invite. It holds no secret: the project, the home and the CA's public key."""
    if role not in ("owner", "builder", "reviewer", "viewer"):
        raise SyncError("Pick a role for them.")
    project_dir = Path(project_dir)
    project = ProjectStore(project_dir).read_project()
    data_dir = _paths.data_dir()
    pin = Pin.load(data_dir, project.id)
    if pin is None or pin.mode == "folder":
        raise SyncError("Share the project first: pick where it lives, then invite people.")
    ca = security.ProjectCA.load(project.id)
    if ca is None:
        raise SyncError("Invites come from the computer that shared the project. Ask whoever shared it to invite "
                        "them.")
    if member_id:
        security.require_safe_id(member_id, "member id")
    body = {"kind": "invite", "format": "playground-invite/1", "id": secrets.token_hex(8),
            "project": {"id": project.id, "name": project.name}, "role": role, "member": member_id,
            "name": name, "for": for_whom, "from": pin.owner_name, "created": _iso(),
            "expires": _iso(_now() + timedelta(days=days)), "home": pin.home_record(), "ca": ca.public_key}
    sig = ssh_sign(ca.private_path, "playground-invite", _canonical(body), env=ca._env())
    invites = read_json(_invites_path(ca)) or {}
    invites = {k: v for k, v in invites.items() if (_parse_iso(v.get("expires")) or _now()) > _now() - timedelta(days=30)}
    invites[body["id"]] = {"role": role, "member": member_id, "name": name, "for": for_whom, "created": body["created"],
                           "expires": body["expires"], "used": False}
    security.write_private(_invites_path(ca), json.dumps(invites, indent=2))
    return Signed("invite", body, sig)


def read_invite(text: str) -> Signed:
    """Check an invite's signature and dates. Raises InviteError in plain words."""
    envelope = decode_code("invite", text)
    body = envelope["body"]
    ca = str(body.get("ca") or "")
    if not ca or not ssh_verify(ca, "playground-invite", _canonical(body), envelope["sig"]):
        raise InviteError("This invite has been changed since it was made. Ask for a new one.")
    expires = _parse_iso(body.get("expires"))
    if expires is None or expires < _now():
        raise InviteError("This invite has run out. Ask for a new one.")
    try:
        security.require_safe_id(str((body.get("project") or {}).get("id") or ""), "project id")
        _validate_home(dict(body.get("home") or {}))
    except (security.SecurityError, SyncError) as exc:
        raise InviteError(f"This invite can't be used: {exc}") from exc
    return Signed("invite", body, envelope["sig"])


def _joining_dir() -> Path:
    return _paths.data_dir() / "sync" / "joining"


def request_to_join(invite: Signed, name: str, connect_id: str = "") -> Signed:
    """This device's signed answer to an invite: its public keys, nothing secret."""
    if invite.kind != "invite":
        raise InviteError("That isn't an invite.")
    name = " ".join(str(name or "").split())[:80]
    if not name:
        raise InviteError("Say your name, so the owner knows who's asking.")
    security.device_key(create=True)
    device = _device(_paths.data_dir())
    assert device is not None
    vault = None
    try:
        if security.crypto_available()[0]:
            identity = security.VaultIdentity.load_or_create()
            vault = identity.public() if identity else None
    except security.SecurityError:
        vault = None
    body = {"kind": "join", "format": "playground-join/1", "invite": invite.body["id"],
            "project": {"id": invite.project_id, "name": invite.project_name}, "name": name,
            "connect_id": connect_id, "created": _iso(),
            "device": {"public_key": device.public_key, "name": device.device_name, "fingerprint": device.fingerprint},
            "vault": vault}
    sig = ssh_sign(device.private_path, "playground-join", _canonical(body), env=device.ssh_env())
    request = Signed("join", body, sig)
    directory = _joining_dir()
    security.private_dir(directory)
    security.write_private(directory / f"{security.require_safe_id(invite.body['id'], 'invite id')}.json",
                           json.dumps({"invite": {"body": invite.body, "sig": invite.sig},
                                       "request": {"body": body, "sig": sig}}, indent=2))
    return request


def pending_joins() -> list[dict]:
    """Projects this computer asked to join and hasn't heard back from: for "Paste the welcome"."""
    directory = _joining_dir()
    out = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        record = read_json(path) or {}
        invite, request = record.get("invite") or {}, record.get("request") or {}
        body = invite.get("body") or {}
        if not body or not request:
            continue
        out.append({"invite": body.get("id"), "project": (body.get("project") or {}).get("name", ""),
                    "from": body.get("from", ""), "role": body.get("role", ""), "created": request["body"].get("created"),
                    "safety": safety_code(body["ca"]) if body.get("ca") else "",
                    "request": Signed("join", request["body"], request["sig"])})
    return out


def forget_join(invite_id: str) -> None:
    try:
        (_joining_dir() / f"{security.require_safe_id(invite_id, 'invite id')}.json").unlink()
    except (OSError, security.SecurityError):
        pass


def read_join_request(project_dir: Path | str, text: str) -> Signed:
    """On the owner's computer: check a request against our own invites."""
    envelope = decode_code("join", text)
    body = envelope["body"]
    device = body.get("device") or {}
    key = str(device.get("public_key") or "")
    if not key or not ssh_verify(key, "playground-join", _canonical(body), envelope["sig"]):
        raise InviteError("This request wasn't signed by the computer it came from. Ask them to make it again.")
    project = ProjectStore(project_dir).read_project()
    if (body.get("project") or {}).get("id") != project.id:
        raise InviteError(f"This request is for a different project, not {project.name}.")
    ca = security.ProjectCA.load(project.id)
    if ca is None:
        raise InviteError("Only the project owner's computer can let people in.")
    record = (read_json(_invites_path(ca)) or {}).get(str(body.get("invite") or ""))
    if record is None:
        raise InviteError("This request doesn't match an invite from this computer.")
    if record.get("used"):
        raise InviteError("That invite has already been used. Make a new one if they need another computer added.")
    if (_parse_iso(record.get("expires")) or _now()) < _now():
        raise InviteError("That invite has run out. Make a new one.")
    vault = body.get("vault")
    if vault is not None and not (isinstance(vault, dict) and {"x25519", "ed25519"} <= set(vault)):
        raise InviteError("This request's vault key is damaged.")
    request = Signed("join", body, envelope["sig"], extra={"invite": record, "ca": ca.public_key})
    request.extra["fingerprint"] = security.fingerprint(key)
    return request


def approve_join(project_dir: Path | str, request: Signed, role: str | None = None,
                 bus: events.EventBus | None = None) -> Signed:
    """Let them in: a certificate, the vault, project.toml, the access list. Returns their welcome."""
    project_dir = Path(project_dir)
    store = ProjectStore(project_dir, bus=bus)
    project = store.read_project()
    record = request.extra.get("invite") or {}
    role = role or record.get("role") or "builder"
    if role not in ("owner", "builder", "reviewer", "viewer"):
        raise SyncError("Pick a role for them.")
    body = request.body
    name = body.get("name") or record.get("name") or "Teammate"
    member_id = record.get("member") or ""
    if not member_id:
        taken = {m.id for m in project.members}
        base = _paths.slugify(name, 24)
        member_id, n = base, 2
        while member_id in taken:
            member_id, n = f"{base}-{n}", n + 1
    device = body["device"]
    data_dir = _paths.data_dir()
    desk = OwnerDesk(project_dir, data_dir)
    if not desk.is_root:
        raise SyncError("Only the computer that shared the project can let people in.")
    desk.queue("add", member=member_id, role=role, public_key=security.normalize_public_key(device["public_key"]),
               device=_device_label(device.get("name") or "device"))
    vault_added, vault_note, vault_rid = False, "", ""
    if body.get("vault"):
        try:
            vault = security.Vault(project_dir, project.id)
            if vault.path.exists():
                vault_rid = vault.add_member(member_id, name, body["vault"], admin=(role == "owner"))["device"]
                vault_added = True
        except security.SecurityError as exc:
            vault_note = str(exc)
    fingerprint = security.fingerprint(device["public_key"])
    with store.locked():
        current = store.read_project()
        member = current.member(member_id)
        if member is None:
            member = Member(id=member_id, name=name, role=role, connect_id=body.get("connect_id") or "")
            current.members.append(member)
        member.role = role
        member.extra.pop("invited", None)
        member.extra["joined"] = _iso()
        devices = list(member.extra.get("devices") or [])
        if fingerprint not in devices:
            devices.append(fingerprint)
        member.extra["devices"] = devices
        if vault_rid:
            member.extra["vault_devices"] = {**(member.extra.get("vault_devices") or {}), fingerprint: vault_rid}
        store.write_project(current)
    ca = security.ProjectCA.open(project.id)
    invites = read_json(_invites_path(ca)) or {}
    if body.get("invite") in invites:
        invites[body["invite"]]["used"] = True
        invites[body["invite"]]["member"] = member_id
        security.write_private(_invites_path(ca), json.dumps(invites, indent=2))
    desk.publish_access()
    desk.renew_certs(force_devices=[fingerprint])
    texts = desk.cert_texts(member_id, device["public_key"])
    another = len([s for s in desk.seats() if s.member == member_id]) > 1
    store.log(f"Let {name} in from another computer" if another else f"Let {name} in as a {role}", kind="people",
              data={"member": member_id})
    pushed, note = _push_now(project_dir, bus)
    welcome = {"kind": "approval", "format": "playground-approval/1", "invite": body.get("invite"),
               "project": {"id": project.id, "name": project.name},
               "member": {"id": member_id, "name": name, "role": role}, "device": fingerprint,
               "certs": texts, "home": desk.pin.home_record(), "ca": ca.public_key,
               "access_version": desk.pin.access_version, "issuers": list(desk.pin.issuers),
               "access_expires": desk.pin.access_expires, "created": _iso(), "vault": vault_added}
    sig = ssh_sign(ca.private_path, "playground-approval", _canonical(welcome), env=ca._env())
    return Signed("approval", welcome, sig, extra={"pushed": pushed, "note": note, "vault_note": vault_note})


def _push_now(project_dir: Path, bus: events.EventBus | None) -> tuple[bool, str]:
    engine = engine_for(project_dir)
    try:
        if engine is None:
            engine = SyncEngine(project_dir, bus=bus)
        engine._owner_dirty = True
        status = engine.sync_now()
    except SyncError as exc:
        return False, str(exc)
    ok = status.get("state") in ("up-to-date", "conflict")
    return ok, "" if ok else ("Playground couldn't reach the project home just now. It passes this along as "
                              "soon as it can, while Playground is open.")


def accept_approval(text: str, dest_root: Path | str | None = None, bus: events.EventBus | None = None) -> Path:
    """On the invitee's computer: check the welcome, keep the passes, and fetch the project."""
    envelope = decode_code("approval", text)
    body = envelope["body"]
    invite_id = str(body.get("invite") or "")
    try:
        record = read_json(_joining_dir() / f"{security.require_safe_id(invite_id, 'invite id')}.json")
    except security.SecurityError:
        record = None
    if not record:
        raise InviteError("This welcome doesn't match a request from this computer.")
    invite = record["invite"]["body"]
    ca = str(invite.get("ca") or "")
    if not ssh_verify(ca, "playground-approval", _canonical(body), envelope["sig"]) or body.get("ca") != ca:
        raise InviteError("This welcome isn't from the project you asked to join. Ask the owner to send it again.")
    project = body.get("project") or {}
    if project.get("id") != (invite.get("project") or {}).get("id"):
        raise InviteError("This welcome is for a different project.")
    data_dir = _paths.data_dir()
    device = _device(data_dir)
    if device is None or not security.same_secret(str(body.get("device") or ""), device.fingerprint):
        raise InviteError("This welcome is for a different computer.")
    home = _validate_home(dict(body.get("home") or {}))
    member = body.get("member") or {}
    security.require_safe_id(str(member.get("id") or ""), "member id")
    pin = Pin(project_id=project["id"], member=member["id"], role=str(member.get("role") or "viewer"),
              mode=home["mode"], name=str(project.get("name") or ""), ca=ca, home=home["home"],
              user=str(home.get("user") or ""), port=int(home.get("port") or 22), hosts=list(home.get("hosts") or []),
              host_key=str(home.get("host_key") or ""), host_name=str(home.get("host_name") or ""),
              owner_device=str(home.get("owner_device") or ""), owner_name=str(home.get("owner_name") or ""),
              access_version=int(body.get("access_version") or 0), trust=str(home.get("trust") or "account"),
              gate=str(home.get("gate") or ""),
              issuers=[str(fp) for fp in body.get("issuers") or [] if _FP_RE.match(str(fp))],
              access_expires=float(body.get("access_expires") or 0))
    pin.save(data_dir)
    install_certificates(device, pin, dict(body.get("certs") or {}))
    dest = clone_project(pin, dest_root, bus=bus)
    try:
        (_joining_dir() / f"{invite_id}.json").unlink()
    except OSError:
        pass
    return dest


def clone_project(pin: Pin, dest_root: Path | str | None = None, bus: events.EventBus | None = None,
                  data_dir: Path | None = None) -> Path:
    """Make this person's own copy of a shared project and open its records."""
    from . import project as project_module
    data_dir = Path(data_dir) if data_dir else _paths.data_dir()
    parent = Path(dest_root) if dest_root else _paths.projects_root()
    parent.mkdir(parents=True, exist_ok=True)
    dest = _paths.unique_child(parent, _paths.safe_project_name(pin.name or "Shared project"))
    probe = SyncEngine.__new__(SyncEngine)       # borrow the engine's home logic without a project yet
    probe.data_dir, probe._host_choice, probe._pin_cache = data_dir, ("", 0.0), pin
    remote, env = SyncEngine._remote(probe, pin)
    proc = _git(parent, "clone", "-q", "--no-tags", "--origin", REMOTE, remote, str(dest), env=env, check=False,
                timeout=600)
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        kind, words = _classify(proc.stderr.decode(errors="replace"))
        if kind == "offline":
            words = SyncEngine._offline_words(probe, pin)
        elif kind == "denied" and "access list" in words:
            who = _first(pin.owner_name) or "the owner"
            words = f"The project home doesn't know this computer yet. Ask {who} to open Playground, then try again."
        raise SyncError(words)
    try:
        repo = _Repo(dest)
        repo.run("fetch", "--prune", "--no-tags", "--quiet", "--no-write-fetch-head", remote, *FETCH_REFSPECS,
                 env=env, timeout=300)
        state = repo.rev(MIRROR_STATE)
        if not state:
            raise SyncError("The project home doesn't have the project's records yet. Ask the owner to open "
                            "Playground, then try again.")
        entries = repo.ls_tree(state)
        blobs = repo.read_blobs(sha for _m, sha in entries.values())
        meta = dest / ".playground"
        for rel, (mode, sha) in entries.items():
            if not _safe_rel(rel) or mode not in ("100644", "100755"):
                continue
            _write_bytes(meta / rel, blobs.get(sha, b""))
        repo.update_ref(STATE_REF, state)
        store = ProjectStore(dest, bus=bus)
        store.ensure_layout()
        project = store.read_project()
        if project.id != pin.project_id:
            raise SyncError("The project home holds a different project than the invite said.")
        _write_marker(dest, pin.project_id, pin.member)
        agents = dest / "AGENTS.md"
        if not agents.exists():
            _write_bytes(agents, project_module.render_agents_md(project.name, dest, project.summary,
                                                                 project.recipe).encode())
        project_module.ensure_mcp_config(dest)
        pin.synced_once = True
        pin.save(data_dir)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


# ════════════════════════════════════ members ════════════════════════════════════

def _founder(desk: OwnerDesk) -> str:
    """The member whose computer holds the project's own key (the one that shared it)."""
    fp = desk.pin.owner_device
    return next((s.member for s in desk.seats() if fp and security.same_secret(s.fingerprint, fp)), "")


def remove_member_access(project_dir: Path | str, member_id: str, bus: events.EventBus | None = None) -> dict:
    """Take someone off a shared project, from any owner's computer.

    A new access list without them is signed and sent: the home stops letting them in from the moment it
    arrives, for syncing and (with the root setup) for shells. On the computer that shared the project their
    keys also go into the revocation list. The vault is re-locked without them, and they leave project.toml.
    Returns ``to_change`` (secrets they could read), ``pushed`` and ``note``.
    """
    project_dir = Path(project_dir)
    store = ProjectStore(project_dir, bus=bus)
    project = store.read_project()
    member = project.member(member_id)
    owners = [m for m in project.members if m.role == "owner"]
    if member is not None and member.role == "owner" and len(owners) <= 1:
        raise SyncError("A project needs at least one owner.")
    result: dict[str, Any] = {"member": member_id, "to_change": [], "pushed": False, "note": ""}
    data_dir = _paths.data_dir()
    pin = Pin.load(data_dir, project.id)
    desk = OwnerDesk(project_dir, data_dir) if pin is not None and OwnerDesk.available(project.id, data_dir) else None
    if desk is not None:
        if member_id == desk.pin.member:
            raise SyncError("You can't take yourself off from here.")
        if member_id == _founder(desk) and not desk.is_root:
            who = _first(member.name if member else member_id) or "They"
            raise SyncError(f"{who}’s computer holds the project’s own key, so taking them off from here wouldn’t "
                            "hold. It has to happen on that computer.")
    if security.ProjectCA.exists(project.id) and (desk is None or desk.is_root):
        result.update(security.revoke_member(project_dir, project.id, member_id))
    else:
        result["to_change"] = _vault_remove(project_dir, project.id, member_id)
    with store.locked():
        current = store.read_project()
        current.members = [m for m in current.members if m.id != member_id]
        store.write_project(current)
    if desk is not None:
        desk.queue("remove", member=member_id)
        desk.publish_access()
        desk.renew_certs()
        result["pushed"], result["note"] = _push_now(project_dir, bus)
        result["trust"] = desk.pin.trust
    name = member.name if member else member_id
    store.log(f"Took {name} off the project", kind="people", data={"member": member_id})
    return result


def _vault_remove(project_dir: Path, project_id: str, member_id: str, rid: str = "") -> list[str]:
    try:
        vault = security.Vault(project_dir, project_id)
        if not vault.path.exists() or not security.crypto_available()[0]:
            return []
        if rid:
            return vault.remove_device(rid)["to_change"]
        return vault.remove_member(member_id)["to_change"]
    except security.SecurityError:
        log.info("the vault wasn't re-locked from this computer", exc_info=True)
        return []


def remove_device(project_dir: Path | str, member_id: str, fingerprint: str,
                  bus: events.EventBus | None = None) -> dict:
    """Take one of someone's computers off (a lost laptop). They stay on the project on their other ones."""
    project_dir = Path(project_dir)
    store = ProjectStore(project_dir, bus=bus)
    data_dir = _paths.data_dir()
    desk = OwnerDesk(project_dir, data_dir)
    seats = [s for s in desk.seats() if s.member == member_id]
    if not any(security.same_secret(s.fingerprint, fingerprint) for s in seats):
        raise SyncError("That computer isn't on the project.")
    if len(seats) <= 1:
        raise SyncError("That's their only computer on the project. Take them off the project instead.")
    project = store.read_project()
    member = project.member(member_id)
    rid = str(((member.extra.get("vault_devices") or {}) if member else {}).get(fingerprint) or "")
    to_change = _vault_remove(project_dir, project.id, member_id, rid) if rid else []
    desk.queue("remove-device", member=member_id, fingerprint=fingerprint)
    desk.publish_access()
    desk.renew_certs()
    with store.locked():
        current = store.read_project()
        found = current.member(member_id)
        if found is not None:
            found.extra["devices"] = [d for d in found.extra.get("devices") or [] if d != fingerprint]
            vault_devices = dict(found.extra.get("vault_devices") or {})
            vault_devices.pop(fingerprint, None)
            if vault_devices:
                found.extra["vault_devices"] = vault_devices
            else:
                found.extra.pop("vault_devices", None)
            store.write_project(current)
    pushed, note = _push_now(project_dir, bus)
    name = member.first_name if member else member_id
    store.log(f"Took one of {name}’s computers off the project", kind="people", data={"member": member_id})
    return {"member": member_id, "device": fingerprint, "pushed": pushed, "note": note, "to_change": to_change}


def set_member_role(project_dir: Path | str, member_id: str, role: str, bus: events.EventBus | None = None) -> dict:
    """Change a role everywhere it matters, from any owner's computer: project.toml, the access list, their
    passes (and the project's own records on the computer that shared it)."""
    if role not in ("owner", "builder", "reviewer", "viewer"):
        raise SyncError("Pick a role.")
    project_dir = Path(project_dir)
    store = ProjectStore(project_dir, bus=bus)
    with store.locked():
        project = store.read_project()
        member = project.member(member_id)
        if member is None:
            raise SyncError("That person isn't on the project.")
        if member.role == "owner" and role != "owner" and sum(m.role == "owner" for m in project.members) <= 1:
            raise SyncError("A project needs at least one owner.")
        member.role = role
        store.write_project(project)
    result: dict[str, Any] = {"member": member_id, "role": role, "pushed": False}
    data_dir = _paths.data_dir()
    if Pin.load(data_dir, project.id) and OwnerDesk.available(project.id, data_dir):
        desk = OwnerDesk(project_dir, data_dir)
        desk.queue("role", member=member_id, role=role)
        desk.publish_access()
        desk.renew_certs(force_members=[member_id])
        result["pushed"], result["note"] = _push_now(project_dir, bus)
    else:
        ca = security.ProjectCA.load(project.id)
        entry = ca.members().get(member_id) if ca is not None else None
        for key in (entry or {}).get("keys", []):
            if not (entry or {}).get("removed"):
                ca.add_member(member_id, member.name, role, key["public_key"], key.get("device") or "device")
    store.log(f"{member.first_name} is now a {role}", kind="people", data={"member": member_id})
    return result


def member_devices(project_dir: Path | str, data_dir: Path | None = None) -> dict[str, list[dict]]:
    """Each member's computers on the project ({fingerprint, device}), from the newest access list this
    computer has checked. Cheap: no network."""
    project_dir = Path(project_dir)
    data_dir = Path(data_dir) if data_dir else _paths.data_dir()
    try:
        project = ProjectStore(project_dir).read_project()
    except StoreError:
        return {}
    pin = Pin.load(data_dir, project.id)
    if pin is None:
        return {}
    repo = _Repo(project_dir)
    best: dict = {}
    for ref in (ACCESS_REF, MIRROR_ACCESS):
        rev = repo.rev(ref)
        if not rev:
            continue
        state, parsed = verify_access(pin.ca, project.id, _access_files(repo, rev))
        if state != "bad" and parsed["version"] >= best.get("version", -1):
            best = parsed
    out: dict[str, list[dict]] = {}
    for seat in best.get("seats", []):
        out.setdefault(seat.member, []).append({"fingerprint": seat.fingerprint, "device": seat.device})
    return out


# ── fresh passes on request ──

RENEW_DAYS = 7


def request_renewal(project_dir: Path | str, name: str = "") -> Signed:
    """This computer asks an owner for a fresh pass (when no owner's Playground has been open to renew it).
    The same kind of signed code as asking to join: this computer's public key, signed by it. Nothing secret."""
    project_dir = Path(project_dir)
    project = ProjectStore(project_dir).read_project()
    data_dir = _paths.data_dir()
    pin = Pin.load(data_dir, project.id)
    device = _device(data_dir)
    if pin is None or device is None:
        raise SyncError("This project isn't shared with this computer.")
    member = project.member(pin.member)
    body = {"kind": "renew", "format": "playground-renew/1", "id": secrets.token_hex(8),
            "project": {"id": project.id, "name": project.name}, "member": pin.member,
            "name": " ".join(str(name or (member.name if member else pin.member)).split())[:80],
            "device": {"public_key": device.public_key, "fingerprint": device.fingerprint, "name": device.device_name},
            "created": _iso()}
    sig = ssh_sign(device.private_path, "playground-renew", _canonical(body), env=device.ssh_env())
    return Signed("renew", body, sig)


def read_renewal_request(project_dir: Path | str, text: str) -> Signed:
    """On an owner's computer: check a request for a fresh pass. Raises InviteError in plain words."""
    envelope = decode_code("renew", text)
    body = envelope["body"]
    device = body.get("device") or {}
    key = str(device.get("public_key") or "")
    if not key or not ssh_verify(key, "playground-renew", _canonical(body), envelope["sig"]):
        raise InviteError("This request wasn't signed by the computer it came from. Ask them to make it again.")
    project = ProjectStore(project_dir).read_project()
    if (body.get("project") or {}).get("id") != project.id:
        raise InviteError(f"This request is for a different project, not {project.name}.")
    created = _parse_iso(body.get("created"))
    if created is None or created < _now() - timedelta(days=RENEW_DAYS) or created > _now() + timedelta(minutes=10):
        raise InviteError("This request is too old. Ask them to make a new one.")
    try:
        desk = OwnerDesk(project_dir)
    except SyncError as exc:
        raise InviteError("Only an owner's computer can hand out fresh passes.") from exc
    fp = security.fingerprint(key)
    if not any(s.member == body.get("member") and security.same_secret(s.fingerprint, fp) for s in desk.seats()):
        raise InviteError("That computer isn't on the project any more, so it can't get a fresh pass. Invite them "
                          "again if they should be.")
    if not desk.can_issue_passes():
        raise InviteError("This computer can't hand out passes for this project yet. The computer that shared the "
                          "project can, or this one once that computer has been open since you became an owner.")
    return Signed("renew", body, envelope["sig"], extra={"fingerprint": fp})


def answer_renewal(project_dir: Path | str, request: Signed, bus: events.EventBus | None = None) -> Signed:
    """Fresh passes for that computer, and a fresh access list for everyone, signed by this owner computer."""
    project_dir = Path(project_dir)
    project = ProjectStore(project_dir).read_project()
    desk = OwnerDesk(project_dir)
    body = request.body
    key = security.normalize_public_key(str((body.get("device") or {}).get("public_key") or ""))
    fp = security.fingerprint(key)
    member = str(body.get("member") or "")
    desk.publish_access(force=desk.access_state != "ok" or
                        (desk.access or {}).get("expires", 0) - time.time() < ACCESS_VALID_S - 3600)
    desk.renew_certs(force_devices=[fp])
    texts = desk.cert_texts(member, key)
    if not texts.get("sync"):
        raise SyncError("Playground couldn't make a pass for that computer.")
    pushed, note = _push_now(project_dir, bus)
    signer_path, signer_env, signer_key = desk._signer()
    answer = {"kind": "passes", "format": "playground-passes/1", "request": body.get("id"),
              "project": {"id": project.id, "name": project.name}, "member": member, "device": fp, "certs": texts,
              "issuers": list(desk.pin.issuers), "access_version": desk.pin.access_version,
              "access_expires": desk.pin.access_expires, "signer": signer_key, "created": _iso()}
    sig = ssh_sign(signer_path, "playground-passes", _canonical(answer), env=signer_env)
    store = ProjectStore(project_dir, bus=bus)
    store.log(f"Gave {body.get('name') or member} a fresh pass", kind="people", data={"member": member})
    return Signed("passes", answer, sig, extra={"pushed": pushed, "note": note})


def accept_renewal(project_dir: Path | str, text: str) -> dict:
    """On the member's computer: check the owner's answer and keep the fresh passes."""
    envelope = decode_code("passes", text)
    body = envelope["body"]
    project = ProjectStore(project_dir).read_project()
    data_dir = _paths.data_dir()
    pin = Pin.load(data_dir, project.id)
    device = _device(data_dir)
    if pin is None or device is None:
        raise InviteError("This project isn't shared with this computer.")
    if (body.get("project") or {}).get("id") != project.id:
        raise InviteError(f"These passes are for a different project, not {project.name}.")
    signer = str(body.get("signer") or "")
    try:
        signer_fp = security.fingerprint(signer)
    except security.SecurityError:
        raise InviteError("These passes aren't from this project.") from None
    root = security.same_secret(signer_fp, pin.ca_fingerprint)
    if not (root or any(security.same_secret(signer_fp, fp) for fp in pin.issuers)):
        raise InviteError("These passes aren't from this project or one of its owners' computers.")
    if not ssh_verify(signer, "playground-passes", _canonical(body), envelope["sig"]):
        raise InviteError("These passes have been changed since they were made. Ask for them again.")
    if not security.same_secret(str(body.get("device") or ""), device.fingerprint) or body.get("member") != pin.member:
        raise InviteError("These passes are for a different computer.")
    if root:
        pin.issuers = [str(fp) for fp in body.get("issuers") or [] if _FP_RE.match(str(fp))]
    if int(body.get("access_version") or 0) >= pin.access_version:
        pin.access_expires = max(pin.access_expires, float(body.get("access_expires") or 0))
    pin.save(data_dir)
    installed = install_certificates(device, pin, dict(body.get("certs") or {}))
    if "sync" not in installed and pass_expiry(device, project.id) < time.time():
        raise InviteError("Those passes didn't fit this computer. Ask for them again.")
    engine = engine_for(project_dir)
    if engine is not None:
        engine._reload_pin()
        engine.sync_now(wait=False)
    return {"installed": installed, "pass_expires": pass_expiry(device, project.id)}


def restore_tag_name(project_dir: Path | str, number: int, data_dir: Path | None = None) -> str | None:
    """The git tag for a restore point in a shared project: ``playground/restore/<member>/<n>-<device>``.

    Restore points travel to teammates, so their names must never clash: each carries who made it and on
    which computer. None when the project isn't shared (``core.places`` keeps ``playground/restore-N``).
    """
    marker = read_json(_local_marker(Path(project_dir))) if _local_marker(Path(project_dir)).exists() else None
    if not isinstance(marker, dict) or not marker.get("member") or not marker.get("project_id"):
        return None
    data_dir = Path(data_dir) if data_dir else _paths.data_dir()
    if Pin.load(data_dir, str(marker["project_id"])) is None:
        return None
    device = _device(data_dir)
    if device is None:
        return None
    member = security.require_safe_id(str(marker["member"]), "member id")
    return f"playground/restore/{member}/{int(number)}-{_key_tag(device.public_key)[:6]}"


def sharing_info(project_dir: Path | str, data_dir: Path | None = None) -> dict:
    """What the People sheet needs to know, cheaply (no network)."""
    project_dir = Path(project_dir)
    data_dir = Path(data_dir) if data_dir else _paths.data_dir()
    try:
        project = ProjectStore(project_dir).read_project()
    except StoreError:
        return {"shared": False}
    table = (project.extra.get("tables") or {}).get("sharing") or {}
    pin = Pin.load(data_dir, project.id) if project.id else None
    owner_device = OwnerDesk.available(project.id, data_dir) if project.id else False
    root = bool(project.id) and (data_dir / "ca" / project.id / "ca_ed25519").exists()
    device = _device(data_dir)
    info = {"shared": pin is not None, "listed": bool(table), "mode": (pin.mode if pin else table.get("mode", "")),
            "home": (pin.host_name if pin else table.get("home", "")), "owner_device": owner_device,
            "can_invite": root and pin is not None, "root": root,
            "member": pin.member if pin else "", "role": pin.role if pin else "",
            "safety": safety_code(pin.ca) if pin and pin.ca else "", "warning": "",
            "trust": pin.trust if pin else table.get("trust", ""),
            "trust_words": TRUST_WORDS.get(pin.trust, "") if pin and pin.mode == "server" else "",
            "pass_expires": pass_expiry(device, project.id) if pin and pin.mode != "folder" else 0.0,
            "access_expires": pin.access_expires if pin else 0.0,
            "issues_passes": bool(pin) and (root or (pin.trust == "live" and device is not None
                                                      and device.fingerprint in pin.issuers))}
    if pin and pin.mode == "this-computer":
        if device and security.same_secret(device.fingerprint, pin.owner_device):
            info["warning"] = THIS_COMPUTER_WARNING
            info["hosting"] = True
        else:
            info["hosting"] = False
            info["owner_note"] = (f"This project lives on {pin.host_name}. You can sync while it's on.")
    return info


# ════════════════════════════════════ engines in this process ════════════════════════════════════

_ENGINES: dict[str, SyncEngine] = {}
_ENGINES_LOCK = threading.Lock()


def engine_for(project_dir: Path | str, create: bool = False, bus: events.EventBus | None = None) -> SyncEngine | None:
    key = str(Path(project_dir).expanduser().absolute())
    with _ENGINES_LOCK:
        engine = _ENGINES.get(key)
        if engine is None and create:
            engine = _ENGINES[key] = SyncEngine(key, bus=bus)
        return engine


def start_engine(project_dir: Path | str, bus: events.EventBus | None = None) -> SyncEngine:
    engine = engine_for(project_dir, create=True, bus=bus)
    assert engine is not None
    return engine.start()


def stop_engine(project_dir: Path | str) -> None:
    key = str(Path(project_dir).expanduser().absolute())
    with _ENGINES_LOCK:
        engine = _ENGINES.pop(key, None)
    if engine is not None:
        engine.stop()


def stop_all() -> None:
    with _ENGINES_LOCK:
        engines = list(_ENGINES.values())
        _ENGINES.clear()
    for engine in engines:
        engine.stop()


__all__ = [
    "SYNC_STATE", "PRESENCE_CHANGED", "THIS_COMPUTER_WARNING", "STATE_WORDS",
    "SyncError", "SyncConflict", "InviteError", "AccessRemoved",
    "SyncEngine", "Pin", "OwnerDesk", "Signed", "MergeResult",
    "merge_file", "merge_jsonl", "merge_markdown", "merge_toml", "merge_json", "merge_vault", "merge_text",
    "share_project", "home_setup_instructions", "check_this_computer", "local_addresses",
    "create_invite", "read_invite", "request_to_join", "read_join_request", "approve_join", "accept_approval",
    "pending_joins", "forget_join",
    "clone_project", "remove_member_access", "set_member_role", "sharing_info", "read_presence", "safety_code",
    "remove_device", "member_devices", "request_renewal", "read_renewal_request", "answer_renewal", "accept_renewal",
    "restore_tag_name", "verify_access", "expiry_words", "Seat", "TRUST_MODES", "TRUST_WORDS", "live_setup_script",
    "account_setup_script", "root_scripts", "issuers_text", "parse_issuers", "pass_expiry",
    "issue_sync_certificate", "install_certificates", "home_scripts", "access_text", "parse_access",
    "engine_for", "start_engine", "stop_engine", "stop_all",
]
