# SPDX-License-Identifier: Apache-2.0
"""Building for Luma: a project that uses, or plans, Luma's own kit (LumaUI, `luma-app.toml`, a Flatpak for
Depot) gets pointed at Luma's developer guides. New apps default to LumaUI (sheets/newproject); websites,
and apps the person wants built otherwise, don't use it. So the code or the recipe decides; a project that
just names Luma gets one line.

The guides live online and change often, so agents read the live copies every time; nothing here copies
them. Only a Luma project carries the lines that point at them (in its now.md), so no other project pays
for them.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import time

# Two doors that stay put; what's behind them changes as Luma does, so agents read them fresh each time and
# follow their links (no guide's own address is kept here).
INDEX = "https://simplyluma.com/llms.txt"
KIT = "https://simplyluma.com/developer/docs/kit"
GUIDES = (
    (INDEX, "the index of every current guide for agents (building, design, publishing): read it first, each "
            "time, and follow the guide that fits the task"),
    (KIT, "LumaUI: what it draws, the action bar, one app on desktop, tablet, phone and TV, every component, "
          "and what changed lately"),
)
CODE = re.compile(r"luma_appkit|\bLumaUI\b|org\.simplyluma\.|luma-app\.toml")
WORDS = re.compile(r"\bluma\b", re.I)
SOURCES = (".py", ".c", ".h", ".vala", ".rs", ".js", ".ts", ".json", ".yml", ".yaml", ".toml", ".build")
SKIP = {".git", ".playground", "node_modules", "target", "build", "dist", "__pycache__", ".venv", "venv"}
LOOK_AT = 80               # source files read at most, per look
FRESH_S = 60
_SEEN: dict[str, tuple[float, str]] = {}


def evidence(root: Path | str) -> str:
    """"code" when it is a Luma app (a `luma-app.toml`, LumaUI in the code, a Luma runtime in its manifest),
    "said" when it only names Luma in what it's for (its summary or brief, or a project it sits in), else ""."""
    root = Path(root)
    kept = _SEEN.get(str(root))
    if kept is not None and time.monotonic() - kept[0] < FRESH_S:
        return kept[1]
    found = "code" if _in_code(root) else "said" if _said(root) else ""
    _SEEN[str(root)] = (time.monotonic(), found)
    return found


def _said(root: Path) -> bool:
    from . import family
    for folder in [root, *family.ancestors(root)]:
        if WORDS.search(family.name_of(folder)) or WORDS.search(family.summary_of(folder)):
            return True
        try:
            if WORDS.search((folder / ".playground" / "brief.md").read_text(encoding="utf-8")[:4000]):
                return True
        except OSError:
            pass
    return False


def _in_code(root: Path) -> bool:
    # Planned before there's any code: a recipe (project.toml) or brief that names LumaUI.
    for planned in ("project.toml", "brief.md"):
        try:
            if CODE.search((root / ".playground" / planned).read_text(encoding="utf-8")[:16_000]):
                return True
        except OSError:
            pass
    read = 0
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP and not d.startswith(".")]
        if "luma-app.toml" in filenames:
            return True
        for name in filenames:
            if not name.endswith(SOURCES):
                continue
            read += 1
            try:
                with open(os.path.join(directory, name), encoding="utf-8", errors="ignore") as handle:
                    if CODE.search(handle.read(32_000)):
                        return True
            except OSError:
                pass
            if read >= LOOK_AT:
                return False
    return False


def now_lines(root: Path | str) -> list[str]:
    """now.md's "Building for Luma" section, one line when Luma is only named, nothing for any other project."""
    found = evidence(root)
    if found == "said":
        return ["", f"Made for Luma? An app uses LumaUI unless the person chose otherwise. Luma's guides start at "
                    f"{INDEX} (LumaUI: {KIT}): read them fresh each time, they change often."]
    if not found:
        return []
    lines = ["", "## Building for Luma", "",
             "This project uses Luma's kit. Its guides are online and change often: before working on the Luma "
             "parts, and whenever unsure, fetch the live copies (never a saved one, never from memory)."]
    lines += [f"- {url}: {what}" for url, what in GUIDES]
    return lines
