# SPDX-License-Identifier: Apache-2.0
"""Making, opening, listing, renaming and archiving projects.

A project is a real folder, ``~/Documents/Playground Projects/<Name>/``:

    AGENTS.md       the rules every agent follows here (from the template below)
    CLAUDE.md       "@AGENTS.md", so Claude Code reads the same rules
    .mcp.json       registers Playground's MCP server for agents that read it
    .gitignore      keeps .playground/local/ (this computer's caches) out of git
    .playground/    project.toml, brief.md, goals/, decisions.md, knowledge/,
                    activity.jsonl, threads/, pending/, answers/, local/

The folder is made whole or not at all: if anything fails part way, the
half-made folder is removed and nothing else is touched.
"""
from __future__ import annotations

import getpass
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from .model import Brief, BRIEF_SUMMARY, Member, Place, Project, Recipe, THIS_COMPUTER, now_iso, ts_sort_key
from . import paths as _paths
from .paths import ProjectPaths, is_project, projects_root, safe_project_name, slugify
from .store import NotFound, ProjectStore, StoreError, atomic_write_json, atomic_write_text, read_text

log = logging.getLogger(__name__)

MCP_SERVER_NAME = "playground"
# The same value as paths.INSTALLED_MCP (a test keeps them equal); the RPM's %check looks for this line.
INSTALLED_MCP = Path("/usr/bin/playground-mcp")

GITIGNORE = """\
# This computer's own Playground files: caches, preview logs, snapshots.
.playground/local/

# Secrets never belong in the project. They live in the project vault.
.env
.env.*

# Things that are rebuilt, not kept.
node_modules/
__pycache__/
.DS_Store
"""


class ProjectError(Exception):
    """A project couldn't be made, opened or changed. The message is for people."""


# ── AGENTS.md ──

OLD_TEMPLATE_MARKS = ("# You are working inside Playground", "## The Playground tools")


def render_agents_md(name: str, path: Path | str, summary: str = "", recipe: Recipe | None = None,
                     parent: str = "", rules=()) -> str:
    """The short start-here file every agent reads (see core.areas). ``recipe`` is kept for callers;
    how the project is built belongs in Stack and Knowledge, not in a file read on every run."""
    from .areas import render_agents_md as render
    return render(name, path, summary, parent=parent, rules=rules)


def upgrade_layout(folder: Path | str) -> list[str]:
    """Bring an older project up to the current layout. Idempotent and cheap; returns what changed.

    The long instructions file of earlier versions is replaced with the short one, after a copy is
    kept in .playground/local/. A file people wrote themselves is never replaced.
    """
    from . import areas
    folder = Path(folder)
    paths = ProjectPaths(folder)
    store = ProjectStore(folder)
    changed: list[str] = []
    paths.tasks_dir.mkdir(parents=True, exist_ok=True)
    text = read_text(paths.agents_md)
    if text is not None and all(mark in text for mark in OLD_TEMPLATE_MARKS) and areas.RULES_START not in text:
        try:
            project = store.read_project()
            name, summary = project.name, project.summary
        except StoreError:
            name, summary = folder.name, ""
        paths.local_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(paths.local_dir / "AGENTS.md.before-upgrade", text)
        atomic_write_text(paths.agents_md, render_agents_md(name, folder, summary))
        changed.append("AGENTS.md")
    elif text is not None and areas.RULES_START in text:
        # Playground's own sections (the standing rules, Keep the project up to date) follow the current
        # template; what a person wrote around them, and the kept-rules block, stay as they are.
        try:
            project = store.read_project()
            name, summary = project.name, project.summary
        except StoreError:
            name, summary = folder.name, ""
        fresh = areas.refresh_agents_md(text, render_agents_md(name, folder, summary))
        if fresh != text:
            atomic_write_text(paths.agents_md, fresh)
            changed.append("AGENTS.md")
    if read_text(paths.guide) != areas.GUIDE_MD:
        atomic_write_text(paths.guide, areas.GUIDE_MD)
        changed.append("guide.md")
    if not paths.gemini_md.exists():
        atomic_write_text(paths.gemini_md, areas.GEMINI_MD)
        changed.append("GEMINI.md")
    store.refresh_areas()
    return changed


CLAUDE_MD = "@AGENTS.md\n"


# ── .mcp.json ──

def mcp_command() -> str:
    """The playground-mcp launcher: this source tree's copy, else the installed one (see paths.mcp_launcher)."""
    return _paths.mcp_launcher()


def mcp_entry(root: Path) -> dict:
    return {"command": mcp_command(), "args": ["--project", str(root)]}


def ensure_mcp_config(root: Path | str) -> bool:
    """Point .mcp.json's playground server at this folder and this launcher.

    Other servers someone added are kept. A file that isn't valid JSON is left
    alone (we never overwrite what we can't read). Returns True if it changed.
    """
    root = Path(root)
    path = root / ".mcp.json"
    text = read_text(path)
    config: dict = {}
    if text is not None and text.strip():
        try:
            config = json.loads(text)
        except json.JSONDecodeError:
            log.warning("%s isn't valid JSON; leaving it as it is", path)
            return False
        if not isinstance(config, dict):
            return False
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = config["mcpServers"] = {}
    wanted = mcp_entry(root)
    current = servers.get(MCP_SERVER_NAME)
    if isinstance(current, dict) and current.get("command") == wanted["command"] and current.get("args") == wanted["args"]:
        return False
    merged = dict(current) if isinstance(current, dict) else {}
    merged.update(wanted)
    servers[MCP_SERVER_NAME] = merged
    atomic_write_json(path, config)
    return True


# ── People ──

def default_owner() -> Member:
    """The person at this computer, from their account's full name."""
    full = ""
    try:
        import pwd
        full = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
    except (ImportError, KeyError, AttributeError):
        pass
    if not full:
        try:
            full = getpass.getuser()
        except Exception:  # noqa: BLE001 - getuser raises various things when there's no user
            full = "Me"
    first = full.split()[0] if full.split() else "me"
    return Member(id=slugify(first, 24), name=full, role="owner")


# ── Create ──

def _claim_folder(parent: Path, name: str) -> Path:
    """mkdir parent/name, or "name 2", "name 3"…; mkdir is the atomic claim."""
    parent.mkdir(parents=True, exist_ok=True)
    number = 1
    while True:
        candidate = parent / (name if number == 1 else f"{name} {number}")
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            number += 1
            if number > 999:
                raise ProjectError(f"There are already too many projects called {name}.") from None


def create_project(name: str, brief: Brief | str | None = None, recipe: Recipe | str | None = None,
                   home: str = THIS_COMPUTER, *, root: Path | str | None = None, summary: str = "",
                   owner: Member | None = None, git: bool = True, bus=None, parent: Path | str | None = None) -> Project:
    """Make a new project folder with everything Playground and agents need.

    ``name`` is made safe for a folder name; if it is taken, "Kiln 2" is used.
    ``brief`` may be a Brief or just a sentence saying what it is.
    ``parent`` makes a sub-project: a folder inside that project (directly, or under ``root`` when that's
    inside it: Luma's `src/apps/`), sharing its git history, whose AGENTS.md says where the bigger picture is.
    """
    above = Path(parent) if parent is not None else None
    base = Path(root) if root is not None else (above if above is not None else projects_root())
    if above is not None and not _inside(base, above):
        base = above
    folder = _claim_folder(base, safe_project_name(name))
    try:
        project = _scaffold(folder, brief, recipe, home, summary, owner or default_owner(), bus,
                            parent=os.path.relpath(above, folder) + "/" if above is not None else "")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    if git and above is None:
        init_git(folder, f"Start {project.name}")
    return project


def _inside(folder: Path, top: Path) -> bool:
    try:
        Path(folder).absolute().relative_to(Path(top).absolute())
        return True
    except ValueError:
        return False


ADOPTED_MARK = "<!-- playground: the part below is Playground's; everything above is yours -->"


def adopt_folder(folder: Path | str, name: str = "", *, summary: str = "", owner: Member | None = None,
                 bus=None) -> Project:
    """Make a folder that already holds work (the Luma repository, an app inside it) a Playground project, in
    place. Nothing a person wrote is replaced: an AGENTS.md of theirs gets Playground's part added at its end,
    a .gitignore gets one line, a CLAUDE.md or .mcp.json of theirs stays (the playground server is added to
    .mcp.json). Inside another project, it's a sub-project of it. Already a project: it's opened."""
    folder = Path(folder).expanduser().absolute()
    if not folder.is_dir():
        raise ProjectError(f"There's no folder at {folder}.")
    if is_project(folder):
        return open_project(folder)
    made = []
    try:
        project = _scaffold(folder, summary or None, None, THIS_COMPUTER, summary, owner or default_owner(), bus,
                            name=name.strip() or folder.name, adopt=True, made=made)
    except BaseException:
        shutil.rmtree(ProjectPaths(folder).meta, ignore_errors=True)
        for path in made:
            path.unlink(missing_ok=True)
        raise
    from . import family
    family.forget()
    if family.parent_of(folder) is None and not _inside(folder, projects_root()):
        remember_elsewhere(folder)
    return project


def _elsewhere_file() -> Path:
    return projects_root() / ".elsewhere.json"


def elsewhere() -> list[Path]:
    """Projects that live outside the projects folder (a repository brought in where it is)."""
    try:
        data = json.loads(_elsewhere_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [Path(p) for p in data if isinstance(p, str)] if isinstance(data, list) else []


def remember_elsewhere(folder: Path | str) -> None:
    folders = [str(p) for p in elsewhere()]
    if str(folder) not in folders:
        projects_root().mkdir(parents=True, exist_ok=True)
        atomic_write_json(_elsewhere_file(), folders + [str(folder)])


def _scaffold(folder: Path, brief: Brief | str | None, recipe: Recipe | str | None, home: str,
              summary: str, owner: Member, bus, parent: str = "", *, name: str = "", adopt: bool = False,
              made: list | None = None) -> Project:
    name = name or folder.name
    made = made if made is not None else []
    paths = ProjectPaths(folder)
    store = ProjectStore(folder, bus=bus)
    store.ensure_layout()
    recipe_obj = recipe if isinstance(recipe, Recipe) else Recipe(summary=recipe or "")
    if isinstance(brief, Brief):
        brief_obj = brief
    else:
        brief_obj = Brief(title=name)
        if brief:
            brief_obj.set_section(BRIEF_SUMMARY, str(brief))
    if not summary:
        summary = brief_obj.summary.split("\n")[0] if brief_obj.summary else ""
    project = Project(name=name, id=str(uuid.uuid4()), created=now_iso(), home=home or THIS_COMPUTER,
                      summary=summary, recipe=recipe_obj, path=folder,
                      places=[Place(id=THIS_COMPUTER, name="This computer", role="development", kind="local",
                                    description="Where you talk to the agent and run the app.")],
                      members=[owner])
    store.write_project(project)
    from . import family
    family.forget()                       # a new part: the project above lists it in its map
    store.write_brief(brief_obj)
    atomic_write_text(paths.decisions, "# Decisions\n\nWhat was decided, by whom, and why. "
                                       "New decisions are added at the bottom.\n")
    store.ensure_general_chat()
    from . import areas
    start = render_agents_md(name, folder, summary, recipe_obj, parent=parent)
    theirs = read_text(paths.agents_md) if adopt else None
    if theirs is not None and theirs.strip():
        # Their file stays theirs: Playground's part goes after it, from its first paragraph on.
        atomic_write_text(paths.agents_md, theirs.rstrip("\n") + "\n\n" + ADOPTED_MARK + "\n\n"
                          + start[start.index("This folder is"):])
    else:
        made.append(paths.agents_md)
        atomic_write_text(paths.agents_md, start)
    for path, text in ((paths.claude_md, CLAUDE_MD), (paths.gemini_md, areas.GEMINI_MD)):
        if not (adopt and path.exists()):
            made.append(path)
            atomic_write_text(path, text)
    atomic_write_text(paths.guide, areas.GUIDE_MD)
    store.refresh_areas()
    ignored = read_text(paths.gitignore) if adopt else None
    if ignored is None:
        made.append(paths.gitignore)
        atomic_write_text(paths.gitignore, GITIGNORE)
    elif ".playground/local/" not in ignored:
        atomic_write_text(paths.gitignore, ignored.rstrip("\n") + "\n\n# Playground: this computer's own files\n"
                                                                  ".playground/local/\n")
    ensure_mcp_config(folder)
    store.log(f"Created {name}", kind="created", actor=owner.id, data={"path": str(folder)})
    return project


def init_git(folder: Path, message: str) -> bool:
    """git init and a first commit, when git is here. A project works without it."""
    git = shutil.which("git")
    if git is None:
        return False

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([git, *args], cwd=folder, capture_output=True, text=True, timeout=30,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})

    try:
        if run("init", "-q").returncode != 0:
            return False
        identity: list[str] = []
        if not run("config", "user.email").stdout.strip():
            identity = ["-c", "user.name=Playground", "-c", "user.email=playground@localhost"]
        run("add", "-A")
        result = run(*identity, "commit", "-q", "--no-gpg-sign", "-m", message)
        if result.returncode != 0:
            log.warning("first commit in %s failed: %s", folder, result.stderr.strip())
            return False
    except (OSError, subprocess.SubprocessError) as error:
        log.warning("git setup in %s failed: %s", folder, error)
        return False
    return True


# ── Open and list ──

def open_project(path: Path | str) -> Project:
    """Read a project, and refresh its .mcp.json if the folder or Playground moved."""
    folder = Path(path).expanduser().absolute()
    if not is_project(folder):
        raise ProjectError(f"{folder} isn't a Playground project.")
    try:
        project = ProjectStore(folder).read_project()
    except StoreError as error:
        raise ProjectError(str(error)) from error
    try:
        ensure_mcp_config(folder)
    except OSError as error:
        log.warning("couldn't update %s/.mcp.json: %s", folder, error)
    try:
        upgrade_layout(folder)
    except (OSError, StoreError) as error:
        log.warning("couldn't bring %s up to date: %s", folder, error)
    project.last_activity = _last_activity(folder)
    return project


def list_projects(root: Path | str | None = None, include_archived: bool = False) -> list[Project]:
    """Every project under the projects root, most recently active first."""
    parent = Path(root) if root is not None else projects_root()
    try:
        children = sorted(parent.iterdir())
    except FileNotFoundError:
        return []
    if root is None or parent == projects_root():
        children += [p for p in elsewhere() if p not in children]
    projects = []
    for child in children:
        if child.name.startswith(".") or not child.is_dir() or not is_project(child):
            continue
        try:
            project = ProjectStore(child).read_project()
        except StoreError as error:
            log.warning("skipping %s: %s", child, error)
            continue
        if project.archived and not include_archived:
            continue
        project.last_activity = _last_activity(child)
        projects.append(project)
    projects.sort(key=lambda p: ts_sort_key(p.last_activity), reverse=True)
    return projects


def _last_activity(folder: Path) -> str:
    store = ProjectStore(folder)
    try:
        last = store.last_activity()
    except OSError:
        last = ""
    if last:
        return last
    try:
        from datetime import datetime
        return datetime.fromtimestamp(store.paths.project_toml.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return ""


# ── Rename and archive ──

def rename_project(project: Project | Path | str, new_name: str, *, actor: str = "", bus=None) -> Project:
    """Rename the project and its folder. Stop its agents and preview first:
    anything holding the old path keeps pointing at it."""
    folder = Path(project.path if isinstance(project, Project) else project).absolute()
    store = ProjectStore(folder, bus=bus)
    current = store.read_project()
    name = safe_project_name(new_name)
    if name == current.name and folder.name == name:
        return current
    target = folder.parent / name
    number = 2
    while target.exists() and not _same_folder(target, folder):
        target = folder.parent / f"{name} {number}"
        number += 1
    old_name = current.name
    if target != folder:
        os.rename(folder, target)
    store = ProjectStore(target, bus=bus)
    current = store.read_project()
    current.name = target.name
    store.write_project(current)
    _retarget_agents_md(target, old_name, folder, current.name)
    ensure_mcp_config(target)
    store.log(f"Renamed {old_name} to {current.name}", kind="project", actor=actor,
              data={"from": str(folder), "to": str(target)})
    current.path = target
    return current


def _same_folder(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return False


def _retarget_agents_md(folder: Path, old_name: str, old_path: Path, new_name: str) -> None:
    """Point AGENTS.md at the new name and path, keeping anything people added."""
    path = folder / "AGENTS.md"
    text = read_text(path)
    if text is None:
        return
    updated = (text.replace(str(old_path), str(folder)).replace(f"**{old_name}**", f"**{new_name}**")
               .replace(f"# {old_name} · a Playground project", f"# {new_name} · a Playground project"))
    if updated != text:
        atomic_write_text(path, updated)


def archive_project(project: Project | Path | str, archived: bool = True, *, actor: str = "", bus=None) -> Project:
    """Hide a project from the list, or bring it back. Nothing is deleted."""
    folder = Path(project.path if isinstance(project, Project) else project).absolute()
    store = ProjectStore(folder, bus=bus)
    current = store.read_project()
    if current.archived == archived:
        return current
    current.archived = archived
    store.write_project(current)
    store.log(f"{'Archived' if archived else 'Brought back'} {current.name}", kind="project", actor=actor)
    return current


__all__ = ["ProjectError", "NotFound", "create_project", "open_project", "list_projects", "rename_project",
           "archive_project", "render_agents_md", "ensure_mcp_config", "mcp_command", "default_owner", "init_git"]
