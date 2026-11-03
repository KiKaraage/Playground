# SPDX-License-Identifier: Apache-2.0
"""Where Playground keeps things, and the rules that keep agents inside a project.

Projects are ordinary folders under ``~/Documents/Playground Projects``. What
belongs to this device (connections, account homes, caches) lives in the XDG
config and data directories and never inside a project.

Overrides, for tests and screenshots:
    PLAYGROUND_HOME         the projects root
    PLAYGROUND_CONFIG_DIR   per-device settings (else $XDG_CONFIG_HOME/playground)
    PLAYGROUND_DATA_DIR     per-device state (else $XDG_DATA_HOME/playground)

Playground's own files (style sheets, icons, the MCP launcher) come from the
source tree when the package runs from one, and from the installed locations
otherwise. ``source_tree()`` decides, by a marker file, never by guessing from
the folder layout: see ``app_data()`` and ``mcp_launcher()``.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path
import re
import secrets
import shutil
import unicodedata

PROJECTS_FOLDER = "Playground Projects"
META_DIR = ".playground"

# Ids that name files inside .playground: goal ids, pending ids, knowledge slugs.
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
# Characters no folder name may hold on Luma, macOS or Windows.
_UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
MAX_NAME_LENGTH = 80


class PathOutsideProject(ValueError):
    """A path that would leave the project folder."""


def projects_root() -> Path:
    override = os.environ.get("PLAYGROUND_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Documents" / PROJECTS_FOLDER


def config_dir() -> Path:
    override = os.environ.get("PLAYGROUND_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "playground"


def data_dir() -> Path:
    override = os.environ.get("PLAYGROUND_DATA_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "playground"


def account_home(connection_id: str) -> Path:
    """The sealed per-account directory an agent program keeps its sign-in in."""
    return data_dir() / "accounts" / require_id(connection_id)


# ── Playground's own files: the source tree, or where the package installs them ──

APP_ID = "org.projectluma.Playground"
INSTALLED_DATA_DIR = Path("/usr/share/playground")
INSTALLED_MCP = Path("/usr/bin/playground-mcp")
# The source tree is the folder holding the luma_playground package *and* this
# app's manifest. An installed copy sits in site-packages, where neither a stray
# top-level data/ nor bin/ (from some other wheel) can pass for it.
SOURCE_MARKER = "luma-app.toml"


def package_dir() -> Path:
    """The luma_playground package folder this code is running from."""
    return Path(__file__).resolve().parents[1]


@functools.lru_cache(maxsize=8)
def _source_tree_of(package: Path) -> Path | None:
    root = package.parent
    marker = root / SOURCE_MARKER
    try:
        if not marker.is_file() or marker.stat().st_size > 64 * 1024:
            return None
        text = marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # Another app's manifest next to site-packages must not count.
    if not re.search(r'^\s*id\s*=\s*"' + re.escape(APP_ID) + r'"', text, re.MULTILINE):
        return None
    return root


def source_tree(package: Path | str | None = None) -> Path | None:
    """The source checkout (``src/luma-playground``) this package runs from, or
    None when it runs installed."""
    return _source_tree_of(Path(package).resolve() if package is not None else package_dir())


def code_root(package: Path | str | None = None) -> Path:
    """The folder Python imports ``luma_playground`` from: the source tree, or site-packages."""
    return (Path(package).resolve() if package is not None else package_dir()).parent


def app_data(*parts: str, package: Path | str | None = None) -> Path:
    """One of Playground's data files (style sheets, icons, the app icon):
    ``<source tree>/data/...`` when running from source, else ``/usr/share/playground/...``."""
    tree = source_tree(package)
    base = tree / "data" if tree is not None else INSTALLED_DATA_DIR
    return base.joinpath(*parts)


def mcp_launcher(package: Path | str | None = None) -> str:
    """The playground-mcp launcher agents start: the source tree's own copy, else
    the installed one, else whatever is on PATH."""
    tree = source_tree(package)
    if tree is not None:
        local = tree / "bin" / "playground-mcp"
        if local.is_file():
            return str(local)
    if INSTALLED_MCP.is_file():
        return str(INSTALLED_MCP)
    return shutil.which("playground-mcp") or str(INSTALLED_MCP)


# ── Names and ids ──

def safe_project_name(name: str) -> str:
    """A folder name that is safe everywhere and still reads like what was typed.

    "  Kiln  " → "Kiln"; "../etc" → "etc"; "a/b" → "a b"; "" → "My project".
    """
    text = unicodedata.normalize("NFC", str(name or ""))
    text = _UNSAFE_NAME_CHARS.sub(" ", text)
    text = " ".join(text.split())
    text = text.lstrip(". ").rstrip(". ")
    if len(text) > MAX_NAME_LENGTH:
        text = text[:MAX_NAME_LENGTH].rstrip(". ")
    if not text or text.split(".")[0].lower() in _WINDOWS_RESERVED:
        text = f"{text} project".strip() if text else "My project"
    return text


def unique_child(parent: Path, name: str) -> Path:
    """parent/name, or parent/"name 2", "name 3"… when that is taken."""
    candidate = parent / name
    number = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = parent / f"{name} {number}"
        number += 1
    return candidate


def slugify(text: str, limit: int = 40) -> str:
    """Lowercase ASCII words joined by hyphens: "Add a waitlist!" → "add-a-waitlist"."""
    ascii_text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if len(slug) > limit:
        slug = slug[:limit].rstrip("-")
    return slug or "item"


def short_suffix() -> str:
    return secrets.token_hex(2)


def new_id(text: str, limit: int = 40) -> str:
    """A readable unique id: "add-a-waitlist-7f3a"."""
    return f"{slugify(text, limit)}-{short_suffix()}"


def is_valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID.match(value))


def require_id(value: object) -> str:
    if not is_valid_id(value):
        raise ValueError(f"not a valid id: {value!r}")
    return value  # type: ignore[return-value]


# ── Staying inside a project ──

def resolve_inside(root: Path | str, path: Path | str) -> Path:
    """The real path of ``path`` (relative to ``root``, or absolute), if it is inside ``root``.

    Symlinks are followed before the check, so a link inside the project that
    points out of it is refused, as is any ``..`` that climbs out. The result
    may not exist yet; its existing parents are resolved.
    """
    text = os.fspath(path)
    if "\x00" in text:
        raise PathOutsideProject("path contains a NUL byte")
    base = Path(root).resolve()
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    real = candidate.resolve(strict=False)
    if real != base and not real.is_relative_to(base):
        raise PathOutsideProject(f"{text} is outside the project")
    return real


def is_inside(root: Path | str, path: Path | str) -> bool:
    try:
        resolve_inside(root, path)
    except PathOutsideProject:
        return False
    return True


def relative_to_project(root: Path | str, path: Path | str) -> str:
    """The project-relative, forward-slash form of a path inside the project."""
    real = resolve_inside(root, path)
    rel = real.relative_to(Path(root).resolve())
    return rel.as_posix() if rel.parts else "."


class ProjectPaths:
    """Every well-known file in a project, in one place."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().absolute()
        self.meta = self.root / META_DIR

    @property
    def agents_md(self) -> Path:
        return self.root / "AGENTS.md"

    @property
    def claude_md(self) -> Path:
        return self.root / "CLAUDE.md"

    @property
    def gemini_md(self) -> Path:
        return self.root / "GEMINI.md"

    @property
    def guide(self) -> Path:
        """How to keep each area up to date: read by agents only when they need it."""
        return self.meta / "guide.md"

    @property
    def channel(self) -> Path:
        """The team channel: agents and people say what they're starting, finishing, or warning about."""
        return self.meta / "channel.jsonl"

    @property
    def now(self) -> Path:
        """Where the project stands, made by Playground (core.areas.render_now): a new agent reads it first."""
        return self.meta / "now.md"

    @property
    def journal(self) -> Path:
        """How the project got here, made by Playground from the conversations (core.areas.render_journal)."""
        return self.meta / "journal.md"

    @property
    def channel_digest(self) -> Path:
        return self.meta / "channel.md"

    @property
    def tasks_dir(self) -> Path:
        return self.meta / "tasks"

    def index(self, area: str) -> Path:
        """The one-line-per-item list Playground keeps for an area (tasks, issues, knowledge)."""
        folder = {"tasks": self.tasks_dir, "issues": self.issues_dir, "knowledge": self.knowledge_dir}[area]
        return folder / "index.md"

    @property
    def mcp_json(self) -> Path:
        return self.root / ".mcp.json"

    @property
    def gitignore(self) -> Path:
        return self.root / ".gitignore"

    @property
    def project_toml(self) -> Path:
        return self.meta / "project.toml"

    @property
    def run_toml(self) -> Path:
        return self.meta / "run.toml"

    @property
    def brief(self) -> Path:
        return self.meta / "brief.md"

    @property
    def decisions(self) -> Path:
        return self.meta / "decisions.md"

    @property
    def activity(self) -> Path:
        return self.meta / "activity.jsonl"

    @property
    def stack_toml(self) -> Path:
        return self.meta / "stack.toml"

    @property
    def issues_dir(self) -> Path:
        return self.meta / "issues"

    @property
    def usage_dir(self) -> Path:
        return self.meta / "usage"

    @property
    def service_usage(self) -> Path:
        return self.usage_dir / "services.jsonl"

    @property
    def analytics(self) -> Path:
        return self.meta / "analytics.jsonl"

    @property
    def people_dir(self) -> Path:
        """Members' photos (``photo = "people/priya.svg"`` in project.toml). Synced with the records."""
        return self.meta / "people"

    @property
    def goals_dir(self) -> Path:
        return self.meta / "goals"

    @property
    def knowledge_dir(self) -> Path:
        return self.meta / "knowledge"

    @property
    def threads_dir(self) -> Path:
        return self.meta / "threads"

    @property
    def pending_dir(self) -> Path:
        return self.meta / "pending"

    @property
    def answers_dir(self) -> Path:
        return self.meta / "answers"

    @property
    def local_dir(self) -> Path:
        return self.meta / "local"

    @property
    def preview_state(self) -> Path:
        """Written by the app's preview module; read by the MCP server."""
        return self.local_dir / "preview.json"

    @property
    def app_heartbeat(self) -> Path:
        """Refreshed by the app while it has the project open."""
        return self.local_dir / "app.json"

    @property
    def lock_file(self) -> Path:
        return self.local_dir / "store.lock"

    def goal(self, goal_id: str) -> Path:
        return self.goals_dir / f"{require_id(goal_id)}.md"

    def thread(self, goal_id: str) -> Path:
        return self.threads_dir / f"{require_id(goal_id)}.jsonl"

    def knowledge(self, slug: str) -> Path:
        return self.knowledge_dir / f"{require_id(slug)}.md"

    def pending(self, request_id: str) -> Path:
        return self.pending_dir / f"{require_id(request_id)}.json"

    def answer(self, request_id: str) -> Path:
        return self.answers_dir / f"{require_id(request_id)}.json"

    def task(self, task_id: str) -> Path:
        return self.tasks_dir / f"{require_id(task_id)}.md"

    def issue(self, number: int) -> Path:
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError(f"not an issue number: {number!r}")
        return self.issues_dir / f"{number}.md"

    def meta_dirs(self) -> tuple[Path, ...]:
        return (self.goals_dir, self.knowledge_dir, self.threads_dir, self.pending_dir,
                self.answers_dir, self.issues_dir, self.tasks_dir, self.usage_dir, self.local_dir)

    def inside(self, path: Path | str) -> Path:
        return resolve_inside(self.root, path)


def is_project(path: Path | str) -> bool:
    return (Path(path) / META_DIR / "project.toml").is_file()
