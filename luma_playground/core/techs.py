# SPDX-License-Identifier: Apache-2.0
"""What a project is built with, read from its files: languages, toolkits, frameworks, storage. No agent has
to remember to write it down, so the Stack area always says (and says it again when the code moves from
Python to Rust). A quick look: manifests first, then a sample of the source."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re

SKIP = {".git", ".playground", "node_modules", "target", "__pycache__", ".venv", "venv", "dist", "build", ".preview",
        ".next", ".svelte-kit"}
MAX_FILES = 600
MAX_READ = 64_000

LANGUAGES = {".py": "Python", ".rs": "Rust", ".go": "Go", ".ts": "TypeScript", ".tsx": "TypeScript",
             ".js": "JavaScript", ".jsx": "JavaScript", ".c": "C", ".vala": "Vala", ".swift": "Swift",
             ".kt": "Kotlin", ".java": "Java", ".rb": "Ruby", ".php": "PHP", ".html": "HTML"}
# (pattern in source or manifest, what it means)
MARKERS = (
    (re.compile(r"require_version\(\s*['\"]Gtk['\"],\s*['\"]4"), "GTK 4"),
    (re.compile(r"\bgtk4\b|gtk::|\"gtk4\""), "GTK 4"),
    (re.compile(r"require_version\(\s*['\"]Adw['\"]|libadwaita|\badw::"), "libadwaita"),
    (re.compile(r"\bimport sqlite3\b|\brusqlite\b|\bsqlite3\b|better-sqlite3"), "SQLite"),
    (re.compile(r"\bpostgres|\bpsycopg|\bpg\b\""), "PostgreSQL"),
    (re.compile(r"\"react\""), "React"), (re.compile(r"\"vite\""), "Vite"), (re.compile(r"\"svelte\""), "Svelte"),
    (re.compile(r"\"vue\""), "Vue"), (re.compile(r"\"next\""), "Next.js"), (re.compile(r"\bflask\b", re.I), "Flask"),
    (re.compile(r"\bdjango\b"), "Django"), (re.compile(r"\bfastapi\b"), "FastAPI"),
)


@dataclass
class Tech:
    name: str
    kind: str          # language, toolkit, storage, framework
    files: int = 0     # how many files show it (languages)


KINDS = {"GTK 4": "toolkit", "libadwaita": "toolkit", "SQLite": "storage", "PostgreSQL": "storage"}


def detect(root: Path | str) -> list[Tech]:
    root = Path(root)
    counts: dict[str, int] = {}
    found: dict[str, Tech] = {}
    seen = 0
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP and not d.startswith("."))
        for name in filenames:
            path = Path(directory) / name
            language = LANGUAGES.get(path.suffix.lower())
            if language:
                counts[language] = counts.get(language, 0) + 1
            if language or name in ("Cargo.toml", "package.json", "pyproject.toml", "requirements.txt", "meson.build",
                                    "go.mod"):
                seen += 1
                try:
                    with open(path, encoding="utf-8", errors="ignore") as handle:
                        text = handle.read(MAX_READ)
                except OSError:
                    continue
                for pattern, tech in MARKERS:
                    if tech not in found and pattern.search(text):
                        found[tech] = Tech(tech, KINDS.get(tech, "framework"))
            if seen >= MAX_FILES:
                break
        if seen >= MAX_FILES:
            break
    if (root / "Cargo.toml").exists():
        counts.setdefault("Rust", 1)
    out = [Tech(lang, "language", n) for lang, n in sorted(counts.items(), key=lambda item: -item[1])
           if not (lang == "HTML" and len(counts) > 1)]
    return out + sorted(found.values(), key=lambda t: ("toolkit", "framework", "storage").index(t.kind)
                        if t.kind in ("toolkit", "framework", "storage") else 3)
