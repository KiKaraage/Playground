# SPDX-License-Identifier: Apache-2.0
"""The project's repository, as the Repository area shows it: the branch, what changed, the history, and
fetch / pull / push. Plain git, run in the project folder, off the main loop; every failure in plain words.

Playground's own folders stay out of it the way .gitignore says (``.playground/local/`` is this computer's).
Pull is fast-forward only: it never makes a merge on someone's behalf. Push never forces.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import subprocess

TIMEOUT_S = 60
WEB_HOSTS = ("github.com", "gitlab.com", "codeberg.org", "bitbucket.org")


class RepoError(RuntimeError):
    """A git problem, in plain words."""


@dataclass
class Change:
    path: str
    state: str                  # modified, added, deleted, renamed, untracked
    added: int = 0
    removed: int = 0


@dataclass
class Commit:
    hash: str
    subject: str
    author: str
    when: str                   # ISO 8601
    restore: int | None = None  # the restore point it is, if it is one


@dataclass
class RepoState:
    tracked: bool = False
    branch: str = ""
    upstream: str = ""
    remote_url: str = ""
    ahead: int = 0
    behind: int = 0
    changes: list[Change] = field(default_factory=list)
    commits: list[Commit] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    count: int = 0
    remote_commits: list[Commit] = field(default_factory=list)   # the branch as the remote has it (last fetch)
    unpushed: set[str] = field(default_factory=set)               # local commits the remote doesn't have
    fetched: str = ""                                              # when the remote was last checked (ISO)

    @property
    def web_url(self) -> str:
        """The repository's page (https://github.com/noor/rally), when the remote is on a known host."""
        where = self.where
        return f"https://{where}" if self.remote_url and where.split("/", 1)[0] in WEB_HOSTS else ""

    @property
    def where(self) -> str:
        """github.com/noor/rally, or "Only on this computer"."""
        url = self.remote_url
        if not url:
            return "Only on this computer"
        match = re.search(r"(?:@|://)([^/:]+)[:/](.+?)(?:\.git)?$", url)
        return f"{match.group(1)}/{match.group(2)}" if match else url


def git(root: Path | str, *args: str, check: bool = True, timeout: float = TIMEOUT_S) -> subprocess.CompletedProcess:
    program = shutil.which("git")
    if not program:
        raise RepoError("This computer has no git, which the Repository needs.")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        result = subprocess.run([program, "-C", str(root), *args], capture_output=True, text=True, timeout=timeout,
                                env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as error:
        raise RepoError("git took too long. Check the connection and try again.") from error
    if check and result.returncode != 0:
        raise RepoError(_words(result.stderr or result.stdout))
    return result


def _words(stderr: str) -> str:
    text = (stderr or "").strip()
    low = text.lower()
    if "could not read username" in low or "authentication failed" in low or "permission denied (publickey)" in low:
        return "The remote didn’t accept this computer’s sign-in. Set up access to it (an SSH key or a token)."
    if "non-fast-forward" in low or "fetch first" in low or "rejected" in low:
        return "The remote has changes this copy doesn’t. Pull first, then push."
    if "not possible to fast-forward" in low or "diverging" in low or "divergent" in low:
        return "Both this copy and the remote have new commits. Ask an agent to bring them together."
    if "could not read from remote repository" in low or "repository not found" in low:
        return "The online copy can’t be reached from here: check the address, and that this computer can sign in to it."
    if "could not resolve host" in low or "unable to access" in low or "connection timed out" in low:
        return "The remote can’t be reached. Check the internet connection."
    if "nothing to commit" in low:
        return "There’s nothing to commit."
    if "no upstream" in low or "has no upstream" in low:
        return "This branch isn’t on the remote yet."
    lines = [l for l in text.splitlines() if l.strip()]
    return lines[-1][:200] if lines else "git didn’t say what went wrong."


def is_repo(root: Path | str) -> bool:
    return (Path(root) / ".git").exists()


def state(root: Path | str, *, history: int = 30) -> RepoState:
    root = Path(root)
    if not is_repo(root):
        return RepoState()
    out = RepoState(tracked=True)
    head = git(root, "status", "--porcelain=v2", "--branch", "--untracked-files=all", check=False).stdout
    for line in head.splitlines():
        if line.startswith("# branch.head "):
            out.branch = line.split(" ", 2)[2]
        elif line.startswith("# branch.upstream "):
            out.upstream = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            match = re.match(r"# branch\.ab \+(\d+) -(\d+)", line)
            if match:
                out.ahead, out.behind = int(match.group(1)), int(match.group(2))
        elif line.startswith(("1 ", "2 ")):
            xy = line[2:4]
            # "1 XY sub mH mI mW hH hI path" and "2 XY sub mH mI mW hH hI score path<TAB>from"
            path = line.split(" ", 8)[-1] if line.startswith("1 ") else line.split("\t")[0].split(" ", 9)[-1]
            code = (xy[1] if xy[1] != "." else xy[0])
            out.changes.append(Change(path, {"M": "modified", "A": "added", "D": "deleted", "R": "renamed",
                                             "C": "added", "T": "modified"}.get(code, "modified")))
        elif line.startswith("? "):
            out.changes.append(Change(line[2:], "untracked"))
    counts = git(root, "diff", "HEAD", "--numstat", check=False).stdout if _has_commits(root) else ""
    numbers = {}
    for line in counts.splitlines():
        bits = line.split("\t")
        if len(bits) == 3:
            numbers[bits[2]] = (int(bits[0]) if bits[0].isdigit() else 0, int(bits[1]) if bits[1].isdigit() else 0)
    for change in out.changes:
        if change.path in numbers:
            change.added, change.removed = numbers[change.path]
        elif change.state == "untracked":
            change.added = _lines(root / change.path)
    out.remote_url = git(root, "remote", "get-url", "origin", check=False).stdout.strip()
    out.branches = [b.strip() for b in git(root, "branch", "--format=%(refname:short)", check=False).stdout.splitlines()
                    if b.strip()]
    if _has_commits(root):
        out.count = int(git(root, "rev-list", "--count", "HEAD", check=False).stdout.strip() or 0)
        restore = _restore_points(root)
        log = git(root, "log", f"-{history}", "--format=%H%x1f%s%x1f%an%x1f%aI", check=False).stdout
        out.commits = _commits(log, restore)
        if out.upstream:
            out.remote_commits = _commits(git(root, "log", f"-{history}", "--format=%H%x1f%s%x1f%an%x1f%aI",
                                              out.upstream, check=False).stdout, restore)
            out.unpushed = set(git(root, "rev-list", f"{out.upstream}..HEAD", check=False).stdout.split())
        elif out.remote_url:
            out.unpushed = {c.hash for c in out.commits}
        stamp = root / ".git" / "FETCH_HEAD"
        if stamp.exists():
            from datetime import datetime, timezone
            out.fetched = datetime.fromtimestamp(stamp.stat().st_mtime, timezone.utc).isoformat()
    return out


def _commits(log: str, restore: dict[str, int]) -> list[Commit]:
    out = []
    for line in log.splitlines():
        bits = line.split("\x1f")
        if len(bits) == 4:
            out.append(Commit(bits[0], bits[1], bits[2], bits[3], restore.get(bits[0])))
    return out


def _has_commits(root: Path) -> bool:
    return git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0


def _lines(path: Path) -> int:
    try:
        with open(path, "rb") as handle:
            data = handle.read(2_000_000)
    except OSError:
        return 0
    return 0 if b"\0" in data[:8000] else data.count(b"\n")


def _restore_points(root: Path) -> dict[str, int]:
    """Commit → restore point number (the ``playground/restore-N`` tags places.py saves)."""
    from . import places
    return {commit: number for number, commit in places.restore_tags(root).items()}


def suggest_message(changes: list[Change]) -> str:
    """A first draft of a commit message from what changed ("Update waitlist.js and schema.sql")."""
    if not changes:
        return ""
    names = [Path(c.path).name for c in changes]
    verb = "Add" if all(c.state in ("added", "untracked") for c in changes) else \
        "Remove" if all(c.state == "deleted" for c in changes) else "Update"
    if len(names) == 1:
        return f"{verb} {names[0]}"
    if len(names) == 2:
        return f"{verb} {names[0]} and {names[1]}"
    return f"{verb} {names[0]}, {names[1]} and {len(names) - 2} more"


def commit(root: Path | str, message: str, *, fallback: tuple[str, str] = ("Playground", "me@playground.local")) -> str:
    """Commit everything that changed. Returns the new commit's short hash. ``fallback`` is the name and
    address used only when git on this computer hasn't been told who's committing."""
    message = message.strip()
    if not message:
        raise RepoError("Say what changed, and why.")
    root = Path(root)
    if not is_repo(root):
        start(root)
    git(root, "add", "--all")
    who = []
    if not git(root, "config", "user.name", check=False).stdout.strip():
        who += ["-c", f"user.name={fallback[0]}"]
    if not git(root, "config", "user.email", check=False).stdout.strip():
        who += ["-c", f"user.email={fallback[1]}"]
    result = git(root, *who, "commit", "-q", "-m", message, check=False)
    if result.returncode != 0:
        raise RepoError(_words(result.stdout + result.stderr))
    return git(root, "rev-parse", "--short", "HEAD").stdout.strip()


def start(root: Path | str) -> None:
    """Start tracking the project with git (main branch)."""
    git(root, "init", "-q", "-b", "main")


def fetch(root: Path | str) -> None:
    git(root, "fetch", "--quiet", "origin")


def pull(root: Path | str) -> None:
    git(root, "pull", "--ff-only", "--quiet")


def push(root: Path | str) -> None:
    current = state(root)
    if current.upstream:
        git(root, "push", "--quiet")
    else:
        git(root, "push", "--quiet", "--set-upstream", "origin", current.branch or "main")


def set_remote(root: Path | str, url: str) -> None:
    url = normal_address(url)
    if git(root, "remote", "get-url", "origin", check=False).returncode == 0:
        git(root, "remote", "set-url", "origin", url)
    else:
        git(root, "remote", "add", "origin", url)


def normal_address(url: str) -> str:
    """What people paste, as git wants it: a GitHub page address works as well as the clone address."""
    url = url.strip().rstrip("/")
    if not re.match(r"^(https://|git@|ssh://)[\w.@:/~+-]+$", url):
        raise RepoError("That doesn’t look like a repository address. Copy it from GitHub’s Code button.")
    page = re.match(r"^https://(github\.com|gitlab\.com|codeberg\.org)/([\w.-]+)/([\w.-]+?)(?:\.git)?$", url)
    return f"https://{page.group(1)}/{page.group(2)}/{page.group(3)}.git" if page else url


def connect(root: Path | str, url: str) -> str:
    """Put the project on a remote it hasn't been on: check the address answers and is empty (or already this
    project's history), then push. Returns what happened, in a line. Never overwrites what's there."""
    root = Path(root)
    if not _has_commits(root):
        raise RepoError("Commit something first, so there’s a history to put online.")
    url = normal_address(url)
    heads = git(root, "ls-remote", "--heads", url, check=False)
    if heads.returncode != 0:
        raise RepoError(_words(heads.stderr))
    remote_heads = [line.split("\t")[0] for line in heads.stdout.splitlines() if line.strip()]
    if remote_heads:
        known = all(git(root, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0
                    for sha in remote_heads)
        if not known:
            raise RepoError("That repository already has its own commits. Use an empty one (no README), or ask an "
                            "agent to bring the two histories together.")
    set_remote(root, url)
    push(root)
    return "It’s online"


def github_cli() -> tuple[bool, str]:
    """(GitHub's gh tool is here and signed in, the account name)."""
    program = shutil.which("gh")
    if not program:
        return False, ""
    try:
        result = subprocess.run([program, "api", "user", "--jq", ".login"], capture_output=True, text=True,
                                timeout=15, stdin=subprocess.DEVNULL, env=dict(os.environ, GH_PROMPT_DISABLED="1"))
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    return (result.returncode == 0 and bool(result.stdout.strip())), result.stdout.strip()


def create_on_github(root: Path | str, name: str, *, private: bool = True) -> str:
    """A new repository on GitHub with gh, this project pushed to it. Returns its address."""
    root = Path(root)
    name = re.sub(r"[^\w.-]+", "-", name.strip()).strip("-.")[:100]
    if not name:
        raise RepoError("Give it a name.")
    if not _has_commits(root):
        raise RepoError("Commit something first, so there’s a history to put online.")
    if git(root, "remote", "get-url", "origin", check=False).returncode == 0:
        raise RepoError("This project is already connected to a remote.")
    program = shutil.which("gh")
    if not program:
        raise RepoError("GitHub’s gh tool isn’t on this computer.")
    try:
        result = subprocess.run([program, "repo", "create", name, "--private" if private else "--public",
                                 "--source", str(root), "--remote", "origin", "--push"], capture_output=True,
                                text=True, timeout=180, stdin=subprocess.DEVNULL,
                                env=dict(os.environ, GH_PROMPT_DISABLED="1"))
    except subprocess.TimeoutExpired as error:
        raise RepoError("GitHub took too long. Check the connection and try again.") from error
    if result.returncode != 0:
        words = (result.stderr or result.stdout).strip()
        if "already exists" in words:
            raise RepoError(f"You already have a repository called {name}. Pick another name.")
        raise RepoError(_words(words))
    return git(root, "remote", "get-url", "origin").stdout.strip()


def switch(root: Path | str, branch: str, *, create: bool = False) -> None:
    if not re.fullmatch(r"[\w./-]{1,100}", branch or "") or ".." in branch:
        raise RepoError("Branch names use letters, numbers, dashes and slashes.")
    git(root, "switch", *(("-c",) if create else ()), branch)
