# SPDX-License-Identifier: Apache-2.0
"""A project's family: the project it sits in and the ones inside it.

A sub-project is any folder, at any depth inside a project, with its own `.playground/` (Luma's calculator in
`src/apps/calculator/`). It isn't a fence: agents working in it can read and change anything in the whole
family. It's a focus: its own tasks, bugs, knowledge and conversations, so an agent there carries the
calculator in its head, not the whole OS, and reaches up (or across) only when the work needs it.
"""
from __future__ import annotations

import os
from pathlib import Path
import time
import tomllib

MAX_DEPTH = 5              # how deep under a project a sub-project is looked for
SKIP = {"node_modules", "target", "build", "dist", "__pycache__", "venv", ".venv", "vendor", "third_party"}
FRESH_S = 30               # an OS's source tree is big: a look down it is kept this long
_FOUND: dict[Path, tuple[float, list[Path]]] = {}


def forget() -> None:
    """A sub-project was just made or removed: look again."""
    _FOUND.clear()


def is_project(folder: Path | str) -> bool:
    return (Path(folder) / ".playground" / "project.toml").is_file()


def _settings(folder: Path | str) -> dict:
    try:
        data = tomllib.loads((Path(folder) / ".playground" / "project.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    project = data.get("project")
    return project if isinstance(project, dict) else data


def name_of(folder: Path | str) -> str:
    return str(_settings(folder).get("name") or Path(folder).name)


def summary_of(folder: Path | str) -> str:
    return " ".join(str(_settings(folder).get("summary") or "").split())


def parent_of(folder: Path | str) -> Path | None:
    """The nearest project above this folder, or None for a top project."""
    here = Path(folder).absolute()
    for above in here.parents:
        if is_project(above):
            return above
    return None


def ancestors(folder: Path | str) -> list[Path]:
    """Nearest first: the parent, its parent, … the top project."""
    out, here = [], parent_of(folder)
    while here is not None:
        out.append(here)
        here = parent_of(here)
    return out


def root_of(folder: Path | str) -> Path:
    """The top project of the family (the folder itself when it has no parent)."""
    chain = ancestors(folder)
    return chain[-1] if chain else Path(folder).absolute()


def children_of(folder: Path | str, depth: int = MAX_DEPTH) -> list[Path]:
    """The sub-projects directly inside this one: the nearest project down each branch (theirs are theirs)."""
    top = Path(folder).absolute()
    kept = _FOUND.get(top)
    if kept is not None and time.monotonic() - kept[0] < FRESH_S and depth == MAX_DEPTH:
        return list(kept[1])
    found: list[Path] = []
    for directory, dirnames, _files in os.walk(top):
        here = Path(directory)
        if here != top and is_project(here):
            found.append(here)
            dirnames[:] = []
            continue
        level = len(here.relative_to(top).parts)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP) if level < depth else []
    found.sort(key=lambda p: str(p.relative_to(top)).lower())
    if depth == MAX_DEPTH:
        _FOUND[top] = (time.monotonic(), list(found))
    return found


def members(folder: Path | str) -> list[Path]:
    """The whole family, the top project first, each one before the projects inside it."""
    out: list[Path] = []

    def walk(here: Path) -> None:
        out.append(here)
        for child in children_of(here):
            walk(child)
    walk(root_of(folder))
    return out


def rel(here: Path | str, there: Path | str) -> str:
    """A path from one project to another, as an agent would type it (`../../`, `src/apps/calculator/`)."""
    path = os.path.relpath(Path(there).absolute(), Path(here).absolute())
    return "./" if path == "." else path.rstrip("/") + "/"


def resolve(here: Path | str, ref: str) -> Path:
    """Another project in the family by name ("Luma", "calculator") or folder ("..", "src/apps/calculator").
    Raises ValueError naming the ones there are."""
    ref = (ref or "").strip()
    here = Path(here).absolute()
    if not ref or ref in (".", "./"):
        return here
    family = members(here)
    as_path = (here / ref).resolve() if not os.path.isabs(ref) else Path(ref).resolve()
    for member in family:
        if member.resolve() == as_path:
            return member
    wanted = ref.lower().rstrip("/")
    matches = [m for m in family if name_of(m).lower() == wanted or m.name.lower() == wanted]
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(f"{name_of(m)} ({rel(here, m)})" for m in family)
    if matches:
        raise ValueError(f"More than one project is called {ref}: give its folder instead. The family: {names}.")
    raise ValueError(f"No project called {ref} in this family. The family: {names}.")
