# SPDX-License-Identifier: Apache-2.0
"""Typed reading and writing of everything under a project's .playground/.

The folder is the source of truth. People, the app and agents (directly, or
through the MCP server) all write here, so every reader is forgiving and every
writer is careful:

- whole files are replaced atomically (a temp file, fsync, then os.replace);
- activity.jsonl, threads/*.jsonl and decisions.md are append-only;
- read-modify-write runs under a lock shared by every process on the machine;
- fields and sections this version doesn't understand are kept on rewrite.

Every write publishes its event on the bus (see events.py). Writes also note
themselves for the watcher, so the app doesn't announce its own changes twice.

Front matter is a small YAML subset, with no dependency:

    ---
    id: add-a-waitlist-7f3a
    title: Add a waitlist
    platforms: [phones, computers]
    tags:
      - one
      - two
    ---

Scalars may be bare or quoted ("..." with JSON escapes, or '...'). Values are
strings, integers, true/false, null, or lists of those. Nothing else.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import tomllib
from typing import Any, Callable, Iterator

from . import events
from .model import (
    ActivityEntry, AnalyticsSnapshot, Answer, Brief, Check, Decision, Dependency, Goal, GoalStatus, Issue,
    ISSUE_PLACES, ISSUE_SECTIONS, ISSUE_SEVERITIES, ISSUE_STATES, KnowledgeNote, Member, PendingDecision,
    PendingRequest, Place, Project, Recipe, RunConfig, Service, ServiceUsage, SERVICE_KINDS, Stack, Story,
    Task, TASK_STATUSES, ThreadEntry, GENERAL_CHAT_ID, looks_like_secret, now_iso, ts_sort_key,
)
from .paths import ProjectPaths, is_valid_id, new_id, require_id, short_suffix, slugify

try:  # POSIX advisory locks; Windows falls back to the in-process lock only.
    import fcntl
except ImportError:  # pragma: no cover - not on Luma or macOS
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


class StoreError(Exception):
    """Something in .playground could not be read or written."""


class NotFound(StoreError, KeyError):
    """No such goal, note or request."""

    def __str__(self) -> str:  # KeyError quotes its message; keep it readable
        return str(self.args[0]) if self.args else "not found"


class AlreadyAnswered(StoreError):
    """Somebody answered this decision first."""


# ══ Small file primitives ══════════════════════════════════════════════════════

def atomic_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    """Replace ``path`` with ``text`` so a reader sees the old file or the new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(path.parent)
    _note_replaced(path)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def append_line(path: Path, line: str) -> tuple[int, int]:
    """Append one line (adding the newline), fsync, and return its byte span.

    If the file doesn't end in a newline (someone wrote half a line), a newline
    goes first, so our record never fuses with theirs.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = line.rstrip("\n").encode("utf-8") + b"\n"
    with open(path, "ab") as handle:
        start = handle.tell()
        if start > 0:
            with open(path, "rb") as reader:
                reader.seek(start - 1)
                if reader.read(1) != b"\n":
                    payload = b"\n" + payload
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        end = handle.tell()
    _note_appended(path, end - len(payload), end)
    return end - len(payload), end


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write bytes atomically (a temp file in the same folder, then os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


# ── Members' photos ──

PHOTO_TYPES = {".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".webp": b"RIFF",
               ".svg": b""}
MAX_PHOTO_BYTES = 2 * 1024 * 1024
# A face is a drawing, nothing more: no scripts, no embedded pages, nothing fetched from elsewhere.
_SVG_REFUSED = re.compile(r"<\s*(script|foreignObject|image|iframe|object|embed)\b|\bon[a-z]+\s*=|"
                          r"href\s*=\s*[\"'](?!#)|url\(\s*(?![\"']?#)|<!ENTITY|@import", re.IGNORECASE)


def photo_problem(data: bytes, suffix: str) -> str:
    """Why these bytes can't be a member's photo, in plain words; "" when they can."""
    suffix = suffix.lower()
    if suffix not in PHOTO_TYPES:
        return "A photo has to be a PNG, JPEG, WebP or SVG image."
    if not data:
        return "That photo is empty."
    if len(data) > MAX_PHOTO_BYTES:
        return "That photo is too big. Use one under 2 MB."
    if suffix == ".svg":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return "That SVG can't be read."
        if "<svg" not in text or _SVG_REFUSED.search(text):
            return "That SVG has more than a drawing in it, so it isn't used."
        return ""
    if not data.startswith(PHOTO_TYPES[suffix]) or (suffix == ".webp" and data[8:12] != b"WEBP"):
        return "That file isn't the kind of image its name says."
    return ""


def member_photo_path(root: Path | str, member: Any) -> Path | None:
    """A member's ``photo`` (a path inside .playground/people/), checked before anything shows it.

    Synced from teammates, so it is treated as untrusted: only a plain file directly in people/,
    of an image type, small, and (for SVG) only a drawing."""
    extra = getattr(member, "extra", None)
    raw = (extra or {}).get("photo") if extra is not None else (member or {}).get("photo")
    if not isinstance(raw, str) or not raw.strip():
        return None
    relative = raw.strip().replace("\\", "/")
    if relative.startswith("./"):
        relative = relative[2:]
    if relative.startswith(".playground/"):
        relative = relative[len(".playground/"):]
    parts = relative.split("/")
    if len(parts) != 2 or parts[0] != "people" or parts[1] in ("", ".", "..") or parts[1].startswith("."):
        return None
    people = ProjectPaths(root).people_dir
    path = people / parts[1]
    suffix = path.suffix.lower()
    if suffix not in PHOTO_TYPES:
        return None
    try:
        if path.is_symlink() or people.is_symlink() or not path.is_file():
            return None
        if path.resolve().parent != people.resolve():
            return None
        size = path.stat().st_size
        if size == 0 or size > MAX_PHOTO_BYTES:
            return None
        with open(path, "rb") as handle:
            head = handle.read(MAX_PHOTO_BYTES if suffix == ".svg" else 16)
    except OSError:
        return None
    if suffix == ".svg":
        return path if not photo_problem(head, suffix) else None
    return path if head.startswith(PHOTO_TYPES[suffix]) else None


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        return path.read_bytes().decode("utf-8", errors="replace")


def read_json(path: Path) -> dict | None:
    text = read_text(path)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("ignoring unreadable JSON in %s", path)
        return None
    return data if isinstance(data, dict) else None


def parse_jsonl(data: bytes, path: Path | None = None) -> list[dict]:
    records = []
    for raw in data.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            log.warning("skipping a damaged line in %s", path)
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def read_jsonl(path: Path) -> list[dict]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    return parse_jsonl(data, path)


def read_jsonl_tail(path: Path, limit: int) -> list[dict]:
    """The last ``limit`` records, read from the end so a long history stays quick."""
    if limit <= 0:
        return []
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return []
    chunk = 64 * 1024
    with open(path, "rb") as handle:
        position, data = size, b""
        while position > 0 and data.count(b"\n") <= limit:
            step = min(chunk, position)
            position -= step
            handle.seek(position)
            data = handle.read(step) + data
    lines = data.splitlines()
    if position > 0 and lines:
        lines = lines[1:]  # the first line may be cut in half
    return parse_jsonl(b"\n".join(lines[-(limit + 5):]), path)[-limit:]


# ══ What this process wrote (so the watcher can skip it) ═══════════════════════

_own_lock = threading.Lock()
_own_states: dict[str, list[tuple[int, int, int]]] = {}   # path → stat signatures after our replaces
_own_ranges: dict[str, list[tuple[int, int]]] = {}        # path → byte spans we appended
_own_deletes: set[str] = set()
_OWN_LIMIT = 64


def _signature(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _note_replaced(path: Path) -> None:
    signature = _signature(path)
    if signature is None:
        return
    key = str(path)
    with _own_lock:
        states = _own_states.setdefault(key, [])
        states.append(signature)
        del states[:-_OWN_LIMIT]
        _own_deletes.discard(key)


def _note_appended(path: Path, start: int, end: int) -> None:
    with _own_lock:
        ranges = _own_ranges.setdefault(str(path), [])
        ranges.append((start, end))
        del ranges[:-_OWN_LIMIT * 4]


def _note_deleted(path: Path) -> None:
    with _own_lock:
        _own_deletes.add(str(path))
        _own_states.pop(str(path), None)


def is_own_state(path: Path, signature: tuple[int, int, int] | None) -> bool:
    """True when the file is exactly as this process last wrote (or deleted) it."""
    key = str(path)
    with _own_lock:
        if signature is None:
            return key in _own_deletes
        return signature in _own_states.get(key, ())


def is_own_append(path: Path, start: int, end: int) -> bool:
    """True when bytes [start, end) of ``path`` are a line this process appended."""
    with _own_lock:
        for own_start, own_end in _own_ranges.get(str(path), ()):
            if own_start <= start and end <= own_end:
                return True
    return False


# ══ Locking across threads and processes ══════════════════════════════════════

class _SharedLock:
    """A re-entrant lock per lock file: a threading.RLock plus flock() held once."""

    _registry: dict[str, "_SharedLock"] = {}
    _registry_lock = threading.Lock()

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rlock = threading.RLock()
        self.depth = 0
        self.fd: int | None = None

    @classmethod
    def for_path(cls, path: Path) -> "_SharedLock":
        with cls._registry_lock:
            lock = cls._registry.get(str(path))
            if lock is None:
                lock = cls._registry[str(path)] = cls(path)
            return lock

    def acquire(self) -> None:
        self.rlock.acquire()
        if self.depth == 0 and fcntl is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
                fcntl.flock(self.fd, fcntl.LOCK_EX)
            except OSError as error:
                # A read-only folder still works for this process; say so once.
                log.warning("couldn't lock %s (%s); continuing with an in-process lock", self.path, error)
                if self.fd is not None:
                    os.close(self.fd)
                self.fd = None
        self.depth += 1

    def release(self) -> None:
        self.depth -= 1
        if self.depth == 0 and self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None
        self.rlock.release()


# ══ Front matter (a small YAML subset) ═════════════════════════════════════════

_BARE_SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9 _.,/@+()'\-]*$")
_YAML_WORDS = {"true", "false", "null", "yes", "no", "on", "off", "~"}
_INT = re.compile(r"^-?\d+$")


def _parse_scalar(text: str) -> Any:
    text = text.strip()
    if not text:
        return ""
    if text.startswith('"'):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text.strip('"')
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    if text.startswith("[") and text.endswith("]"):
        return [_parse_scalar(item) for item in _split_flow(text[1:-1])]
    # A comment after a bare value: "value  # note"
    if " #" in text:
        text = text.split(" #", 1)[0].rstrip()
    lowered = text.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "~"):
        return None
    if _INT.match(text) and len(text) < 16 and not (len(text) > 1 and text.lstrip("-").startswith("0")):
        return int(text)
    return text


def _split_flow(text: str) -> list[str]:
    """Split "a, "b, c", 'd'" on commas outside quotes."""
    items, current, quote, escaped = [], "", "", False
    for char in text:
        if quote:
            current += char
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
            current += char
        elif char == ",":
            items.append(current)
            current = ""
        else:
            current += char
    if current.strip() or items:
        items.append(current)
    return [item for item in (i.strip() for i in items) if item]


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """(fields, body). A file without front matter has no fields and is all body."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
    if end is None:
        return {}, text
    fields: dict[str, Any] = {}
    key: str | None = None
    for raw in lines[1:end]:
        line = raw.rstrip("\n").rstrip("\r")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and key is not None and line[:1] in (" ", "\t", "-"):
            current = fields.get(key)
            if not isinstance(current, list):
                current = fields[key] = []
            current.append(_parse_scalar(stripped[2:]))
            continue
        if ":" not in line or line[:1] in (" ", "\t"):
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        fields[key] = _parse_scalar(value) if value.strip() else ""
    body = "".join(lines[end + 1:])
    return fields, body.lstrip("\n")


def _format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if (_BARE_SAFE.match(text) and text.lower() not in _YAML_WORDS and not _INT.match(text)
            and not text.endswith(" ") and ": " not in text and " #" not in text):
        return text
    return json.dumps(text, ensure_ascii=False)


def dump_front_matter(fields: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}: [{', '.join(_format_scalar(v) for v in value)}]")
        elif isinstance(value, dict):
            lines.append(f"{key}: {_format_scalar(json.dumps(value, ensure_ascii=False))}")
        else:
            lines.append(f"{key}: {_format_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ══ Markdown sections ══════════════════════════════════════════════════════════

def parse_sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a body at its "## " headings: (text before the first, [(heading, text)])."""
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    fenced = False
    for line in body.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        if not fenced and line.startswith("## ") and not line.startswith("### "):
            sections.append((line[3:].strip(), []))
            continue
        (sections[-1][1] if sections else preamble).append(line)
    return "\n".join(preamble).strip(), [(h, "\n".join(lines).strip()) for h, lines in sections]


def dump_sections(preamble: str, sections: list[tuple[str, str]]) -> str:
    parts = [preamble.strip()] if preamble.strip() else []
    for heading, text in sections:
        parts.append(f"## {heading}\n\n{text.strip()}" if text.strip() else f"## {heading}")
    return "\n\n".join(parts) + "\n"


# ══ Stories and checks in a goal body ══════════════════════════════════════════

_LIST_ITEM = re.compile(r"^(?P<indent>[ \t]*)(?:[-*+]|\d+[.)])\s+(?P<rest>.*)$")
_BOX = re.compile(r"^\[(?P<mark>[ xX~-])\]\s*(?P<text>.*)$")
_CHECK = re.compile(r"^check(?:\s*\((?P<status>[a-z]+)\))?\s*:\s*(?P<text>.*)$", re.IGNORECASE)
_EVIDENCE = re.compile(r"^evidence\s*:\s*(?P<path>.+)$", re.IGNORECASE)


def parse_stories(text: str) -> tuple[list[Story], str]:
    """Stories from the "## Stories" section, and any lines that weren't a story or check.

        - [ ] Someone can join the waitlist for a full class
          - check (pass): When all eight wheels are booked, the button says Join waitlist
            evidence: .playground/local/shots/waitlist-full.png
          - check: After joining, they see their place in line

    Agents also write checks as nested "- [x] ..." items; those parse too.
    """
    stories: list[Story] = []
    leftover: list[str] = []
    last_check: Check | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        item = _LIST_ITEM.match(line)
        indent = len(item.group("indent").expandtabs(4)) if item else len(line) - len(line.lstrip())
        if item and indent < 2:
            rest = item.group("rest").strip()
            box = _BOX.match(rest)
            stories.append(Story(title=(box.group("text") if box else rest).strip(),
                                 done=bool(box and box.group("mark") in "xX")))
            last_check = None
        elif item and stories:
            rest = item.group("rest").strip()
            check_match, box = _CHECK.match(rest), _BOX.match(rest)
            if check_match:
                status = (check_match.group("status") or "pending").lower()
                check = Check(text=check_match.group("text").strip(),
                              status=status if status in ("pass", "fail", "pending") else "pending")
            elif box:
                mark = box.group("mark")
                inner = _CHECK.match(box.group("text").strip())
                check = Check(text=(inner.group("text") if inner else box.group("text")).strip(),
                              status="pass" if mark in "xX" else "fail" if mark == "-" else "pending")
            else:
                check = Check(text=rest)
            stories[-1].checks.append(check)
            last_check = check
        elif last_check is not None and _EVIDENCE.match(line.strip()):
            last_check.evidence = _EVIDENCE.match(line.strip()).group("path").strip()
        else:
            leftover.append(line)
    return stories, "\n".join(leftover).strip()


def dump_stories(stories: list[Story]) -> str:
    lines = []
    for story in stories:
        lines.append(f"- [{'x' if story.done else ' '}] {_one_line(story.title)}")
        for check in story.checks:
            marker = "check" if check.status == "pending" else f"check ({check.status})"
            lines.append(f"  - {marker}: {_one_line(check.text)}")
            if check.evidence:
                lines.append(f"    evidence: {_one_line(check.evidence)}")
    return "\n".join(lines)


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


# ══ TOML (tomllib reads; this writes the shapes Playground uses) ═══════════════

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any, multiline_arrays: bool = True) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dict):
        inner = ", ".join(f"{_toml_key(k)} = {_toml_value(v, False)}" for k, v in value.items() if v is not None)
        return "{ " + inner + " }" if inner else "{}"
    if isinstance(value, (list, tuple)):
        items = [_toml_value(v, False) for v in value if v is not None]
        if multiline_arrays and any(isinstance(v, dict) for v in value):
            return "[\n" + "".join(f"  {item},\n" for item in items) + "]"
        return "[" + ", ".join(items) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def dump_toml(document: dict[str, Any]) -> str:
    """Top-level scalars, then [tables], then [[arrays of tables]]."""
    blocks: list[list[str]] = []
    scalars = [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in document.items()
               if v is not None and not isinstance(v, dict) and not _is_table_array(v)]
    if scalars:
        blocks.append(scalars)
    for key, value in document.items():
        if isinstance(value, dict):
            blocks.append([f"[{_toml_key(key)}]"] + [f"{_toml_key(k)} = {_toml_value(v)}"
                                                      for k, v in value.items() if v is not None])
    for key, value in document.items():
        if _is_table_array(value):
            for table in value:
                blocks.append([f"[[{_toml_key(key)}]]"] + [f"{_toml_key(k)} = {_toml_value(v)}"
                                                            for k, v in table.items() if v is not None])
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


# ══ Decisions markdown ═════════════════════════════════════════════════════════

DECISIONS_HEADER = "# Decisions\n\nWhat was decided, by whom, and why. New decisions are added at the bottom.\n"
_DECISION_FIELDS = ("date", "who", "chosen", "why", "goal", "id")
_FIELD_LINE = re.compile(r"^[-*]\s*(?P<key>[A-Za-z_ ]+?)\s*:\s*(?P<value>.*)$")


def parse_decisions(text: str) -> list[Decision]:
    decisions: list[Decision] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        if not fenced and line.startswith("### "):
            decisions.append(Decision(title=line[4:].strip()))
            continue
        if not decisions or not line.strip():
            continue
        current = decisions[-1]
        match = _FIELD_LINE.match(line.strip())
        key = match.group("key").strip().lower().replace(" ", "_") if match else ""
        if match and key in _DECISION_FIELDS:
            setattr(current, key, match.group("value").strip())
        elif match:
            current.extra[key] = match.group("value").strip()
        else:
            # Prose under a decision belongs to its "why".
            current.why = f"{current.why}\n{line.strip()}".strip()
    return decisions


def format_decision(decision: Decision) -> str:
    lines = [f"### {_one_line(decision.title)}"]
    for key in _DECISION_FIELDS:
        value = getattr(decision, key)
        if value:
            lines.append(f"- {key}: {_one_line(value)}")
    lines.extend(f"- {key}: {_one_line(value)}" for key, value in decision.extra.items() if value)
    return "\n".join(lines) + "\n"


# ══ The store ══════════════════════════════════════════════════════════════════

_GOAL_FIELDS = ("id", "title", "kind", "status", "status_note", "owner", "connection", "created", "updated",
                "size", "cost_estimate")
_GOAL_SECTIONS = {"outcome", "stories", "notes"}


class ProjectStore:
    """Read and write one project's .playground folder."""

    def __init__(self, root: Path | str, bus: events.EventBus | None = None) -> None:
        self.paths = ProjectPaths(root)
        self.root = self.paths.root
        self.bus = bus if bus is not None else events.bus
        self._lock = _SharedLock.for_path(self.paths.lock_file)

    # ── plumbing ──

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the project's write lock (re-entrant, shared across processes)."""
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    def _publish(self, topic: str, **payload: Any) -> None:
        self.bus.publish(topic, project=str(self.root), **payload)

    def ensure_layout(self) -> None:
        for directory in self.paths.meta_dirs():
            directory.mkdir(parents=True, exist_ok=True)

    # ── project.toml ──

    def read_project(self) -> Project:
        text = read_text(self.paths.project_toml)
        if text is None:
            raise NotFound(f"{self.root} is not a Playground project (no .playground/project.toml)")
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise StoreError(f"project.toml can't be read: {error}") from error
        head = dict(document.get("project") or {})
        summary_only = head.pop("recipe", None)  # an older form: [project] recipe = "..."
        recipe_data = document.get("recipe") or summary_only
        known = {"name", "id", "created", "home", "summary", "archived"}
        project = Project(
            name=str(head.get("name") or self.root.name), id=str(head.get("id") or ""),
            created=str(head.get("created") or ""), home=str(head.get("home") or "this-computer"),
            summary=str(head.get("summary") or ""), recipe=Recipe.from_dict(recipe_data),
            places=[Place.from_dict(p) for p in document.get("places") or [] if isinstance(p, dict)],
            members=[Member.from_dict(m) for m in document.get("members") or [] if isinstance(m, dict)],
            archived=bool(head.get("archived", False)), path=self.root,
        )
        project.extra = {"project": {k: v for k, v in head.items() if k not in known},
                         "tables": {k: v for k, v in document.items()
                                    if k not in ("project", "recipe", "places", "members")}}
        return project

    def write_project(self, project: Project) -> None:
        with self.locked():
            try:
                before = self.read_project()
            except StoreError:
                before = None
            head: dict[str, Any] = {"name": project.name, "id": project.id, "created": project.created,
                                    "home": project.home, "summary": project.summary}
            if project.archived:
                head["archived"] = True
            head.update({k: v for k, v in (project.extra.get("project") or {}).items() if k not in head})
            document: dict[str, Any] = {"project": head}
            recipe = project.recipe
            if recipe.summary or recipe.parts or recipe.runs_on:
                document["recipe"] = {"summary": recipe.summary, "runs_on": recipe.runs_on,
                                      "parts": [p.to_dict() for p in recipe.parts]}
            document.update(project.extra.get("tables") or {})
            document["places"] = [p.to_dict() for p in project.places]
            document["members"] = [m.to_dict() for m in project.members]
            text = ("# Playground project settings. People and places only; never secrets.\n"
                    + dump_toml(document))
            atomic_write_text(self.paths.project_toml, text)
        self._publish(events.PROJECT_CHANGED, what="project")
        if before is None or [p.to_dict() for p in before.places] != [p.to_dict() for p in project.places]:
            self._publish(events.PLACES_CHANGED)

    # ── members' photos (.playground/people/) ──

    def member_photo(self, member: Member | dict) -> Path | None:
        """The member's photo when they have one that is safe to show, else None (their initials are shown)."""
        return member_photo_path(self.root, member)

    def set_member_photo(self, member_id: str, data: bytes, suffix: str) -> Path:
        """Keep ``data`` as this member's photo in .playground/people/ and point project.toml at it."""
        require_id(member_id)
        suffix = suffix.lower() if suffix.startswith(".") else "." + suffix.lower()
        problem = photo_problem(data, suffix)
        if problem:
            raise StoreError(problem)
        target = self.paths.people_dir / f"{member_id}{suffix}"
        with self.locked():
            project = self.read_project()
            member = next((m for m in project.members if m.id == member_id), None)
            if member is None:
                raise NotFound(f"{member_id} isn't a member of this project")
            self.paths.people_dir.mkdir(parents=True, exist_ok=True)
            for old in self.paths.people_dir.glob(f"{member_id}.*"):
                if old.suffix.lower() in PHOTO_TYPES and old != target:
                    old.unlink(missing_ok=True)
            atomic_write_bytes(target, data)
            member.extra["photo"] = f"people/{target.name}"
            self.write_project(project)
        return target

    def clear_member_photo(self, member_id: str) -> None:
        with self.locked():
            project = self.read_project()
            member = next((m for m in project.members if m.id == member_id), None)
            if member is None or "photo" not in member.extra:
                return
            path = member_photo_path(self.root, member)
            member.extra.pop("photo", None)
            self.write_project(project)
        if path is not None:
            path.unlink(missing_ok=True)

    # ── brief.md ──

    def read_brief(self) -> Brief:
        text = read_text(self.paths.brief)
        if text is None:
            return Brief()
        fields, body = parse_front_matter(text)
        preamble, sections = parse_sections(body)
        platforms = fields.get("platforms") or []
        if isinstance(platforms, str):
            platforms = [p.strip() for p in platforms.split(",") if p.strip()]
        known = {"title", "audience", "platforms", "updated"}
        return Brief(title=str(fields.get("title") or ""), audience=str(fields.get("audience") or ""),
                     platforms=[str(p) for p in platforms], updated=str(fields.get("updated") or ""),
                     preamble=preamble, sections=sections,
                     extra={k: v for k, v in fields.items() if k not in known})

    def write_brief(self, brief: Brief) -> None:
        brief.updated = now_iso()
        fields: dict[str, Any] = {"title": brief.title, "audience": brief.audience,
                                  "platforms": list(brief.platforms), "updated": brief.updated}
        fields.update({k: v for k, v in brief.extra.items() if k not in fields})
        atomic_write_text(self.paths.brief, dump_front_matter(fields) + "\n" + dump_sections(brief.preamble, brief.sections))
        self._publish(events.PROJECT_CHANGED, what="brief")

    # ── goals ──

    def goal_ids(self) -> list[str]:
        try:
            names = os.listdir(self.paths.goals_dir)
        except FileNotFoundError:
            return []
        return sorted(name[:-3] for name in names if name.endswith(".md") and is_valid_id(name[:-3]))

    def list_goals(self) -> list[Goal]:
        """Every goal, the general chat first, then oldest first."""
        goals = []
        for goal_id in self.goal_ids():
            try:
                goals.append(self.read_goal(goal_id))
            except StoreError as error:
                log.warning("skipping goal %s: %s", goal_id, error)
        goals.sort(key=lambda g: (not g.is_chat, ts_sort_key(g.created), g.id))
        return goals

    def read_goal(self, goal_id: str) -> Goal:
        text = read_text(self.paths.goal(goal_id))
        if text is None:
            raise NotFound(f"There's no goal called {goal_id}.")
        return parse_goal(goal_id, text)

    def write_goal(self, goal: Goal, touch: bool = True) -> None:
        require_id(goal.id)
        if touch:
            goal.updated = now_iso()
        if not goal.created:
            goal.created = goal.updated or now_iso()
        atomic_write_text(self.paths.goal(goal.id), format_goal(goal))
        self._publish(events.GOAL_CHANGED, goal_id=goal.id)

    def create_goal(self, title: str, *, outcome: str = "", stories: list[Story] | None = None,
                    status: GoalStatus | str = GoalStatus.WORKING, owner: str = "", connection: str = "",
                    size: str = "", cost_estimate: str = "", status_note: str = "", kind: str = "goal",
                    goal_id: str | None = None) -> Goal:
        title = _one_line(title)
        if not title:
            raise ValueError("A goal needs a title.")
        with self.locked():
            if goal_id is None:
                goal_id = new_id(title)
                while self.paths.goal(goal_id).exists():
                    goal_id = new_id(title)
            elif self.paths.goal(goal_id).exists():
                raise StoreError(f"A goal called {goal_id} already exists.")
            stamp = now_iso()
            goal = Goal(id=require_id(goal_id), title=title, status=GoalStatus.parse(status, GoalStatus.WORKING),
                        kind=kind, owner=owner, connection=connection, created=stamp, updated=stamp,
                        size=size, cost_estimate=cost_estimate, status_note=status_note, outcome=outcome,
                        stories=list(stories or []))
            self.write_goal(goal, touch=False)
        return goal

    def update_goal(self, goal_id: str, change: Callable[[Goal], None]) -> Goal:
        """Read, change and write a goal under the lock, so nobody's edit is lost."""
        with self.locked():
            goal = self.read_goal(goal_id)
            change(goal)
            self.write_goal(goal)
        return goal

    def ensure_general_chat(self, title: str = "Talk to Ari") -> Goal:
        try:
            return self.read_goal(GENERAL_CHAT_ID)
        except NotFound:
            return self.create_goal(title, kind="chat", goal_id=GENERAL_CHAT_ID,
                                    outcome="The project's general conversation.")

    # ── threads ──

    def close_stale_steps(self, goal_id: str, by: set[str] | frozenset[str] = frozenset()) -> int:
        """Steps a cut-off turn left "running" (Playground closed mid-turn) become "Stopped partway". Only the
        steps of agents in ``by`` (this computer's), and only when nothing of theirs runs for the conversation:
        a teammate's agent working on their own computer is left alone. Returns how many."""
        closed = 0
        for entry in self.read_thread(goal_id, fold=True)[-40:]:
            data = entry.data or {}
            if entry.kind == "step" and data.get("state") == "running" and entry.id and entry.by in by:
                title = data.get("title") if str(data.get("title") or "").lower() not in ("", "working") \
                    else "Stopped partway"
                self.append_thread(goal_id, ThreadEntry(kind="step", role=entry.role, by=entry.by, id=entry.id,
                                                        data={**data, "state": "done", "title": title,
                                                              "cut_off": True}))
                closed += 1
        return closed

    def read_thread(self, goal_id: str, limit: int | None = None, fold: bool = False) -> list[ThreadEntry]:
        """A goal's conversation. Decision cards are folded with their answers:
        ``data["answered"]`` once answered, ``data["open"]`` while someone waits.

        Cards that move on (steps, plans) are appended again with the same id.
        With ``fold``, each id appears once, in its first place, as its latest version.
        """
        path = self.paths.thread(goal_id)
        records = read_jsonl_tail(path, limit) if limit else read_jsonl(path)
        entries = [ThreadEntry.from_dict(r) for r in records]
        if fold:
            latest = {entry.id: entry for entry in entries if entry.id}
            seen: set[str] = set()
            folded = []
            for entry in entries:
                if not entry.id:
                    folded.append(entry)
                elif entry.id not in seen:
                    seen.add(entry.id)
                    folded.append(latest[entry.id])
            # A card whose work the runner folded into its own step card
            # (the agent's step_report) shows once, inside that step.
            entries = [entry for entry in folded if not entry.data.get("folded_into")]
        for entry in entries:
            if entry.kind == "decision":
                self._fold_answer(entry)
        return entries

    def _fold_answer(self, entry: ThreadEntry) -> None:
        decision_id = entry.data.get("id")
        if not is_valid_id(decision_id) or entry.data.get("answered"):
            return
        answer = self.read_answer(decision_id)
        if answer is not None:
            entry.data["answered"] = {"option": answer.option, "label": answer.label, "text": answer.text,
                                      "by": answer.answered_by, "at": answer.answered_at}
            entry.data["open"] = False
        else:
            entry.data["open"] = self.paths.pending(decision_id).exists()

    def append_thread(self, goal_id: str, entry: ThreadEntry) -> ThreadEntry:
        require_id(goal_id)
        if not entry.id:
            entry.id = f"{entry.kind}-{short_suffix()}{short_suffix()}"
        if not entry.ts:
            entry.ts = now_iso()
        with self.locked():
            append_line(self.paths.thread(goal_id), json.dumps(entry.to_dict(), ensure_ascii=False))
        self._publish(events.THREAD_APPENDED, goal_id=goal_id, entry=entry.to_dict())
        return entry

    # ── activity ──

    def read_activity(self, limit: int | None = None, goal: str | None = None) -> list[ActivityEntry]:
        """Oldest first. With ``limit``, the most recent ``limit`` entries."""
        if limit and not goal:
            records = read_jsonl_tail(self.paths.activity, limit)
        else:
            records = read_jsonl(self.paths.activity)
        entries = [ActivityEntry.from_dict(r) for r in records]
        if goal:
            entries = [e for e in entries if e.goal == goal]
            if limit:
                entries = entries[-limit:]
        return entries

    def append_activity(self, entry: ActivityEntry) -> ActivityEntry:
        if not entry.ts:
            entry.ts = now_iso()
        with self.locked():
            append_line(self.paths.activity, json.dumps(entry.to_dict(), ensure_ascii=False))
        self._publish(events.ACTIVITY_APPENDED, entry=entry.to_dict())
        return entry

    def log(self, summary: str, *, kind: str = "note", actor: str = "", goal: str = "",
            data: dict | None = None) -> ActivityEntry:
        return self.append_activity(ActivityEntry(summary=summary, kind=kind, actor=actor, goal=goal,
                                                  data=dict(data or {})))

    def last_activity(self) -> str:
        tail = read_jsonl_tail(self.paths.activity, 1)
        return str(tail[-1].get("ts") or "") if tail else ""

    # ── decisions.md ──

    def read_decisions(self) -> list[Decision]:
        return parse_decisions(read_text(self.paths.decisions) or "")

    def append_decision(self, decision: Decision) -> Decision:
        if not decision.date:
            decision.date = now_iso()
        with self.locked():
            path = self.paths.decisions
            if not path.exists() or path.stat().st_size == 0:
                atomic_write_text(path, DECISIONS_HEADER)
            append_line(path, "\n" + format_decision(decision).rstrip("\n"))
        self._publish(events.PROJECT_CHANGED, what="decisions")
        return decision

    # ── knowledge ──

    def list_knowledge(self) -> list[KnowledgeNote]:
        try:
            names = sorted(os.listdir(self.paths.knowledge_dir))
        except FileNotFoundError:
            return []
        notes = []
        for name in names:
            if name.endswith(".md") and is_valid_id(name[:-3]) and name != "index.md":
                note = self.read_knowledge(name[:-3])
                if note is not None:
                    notes.append(note)
        return notes

    def read_knowledge(self, slug: str) -> KnowledgeNote | None:
        text = read_text(self.paths.knowledge(slug))
        if text is None:
            return None
        fields, body = parse_front_matter(text)
        known = {"title", "added_by", "updated"}
        return KnowledgeNote(slug=slug, title=str(fields.get("title") or slug.replace("-", " ").capitalize()),
                             body=body.strip(), added_by=str(fields.get("added_by") or ""),
                             updated=str(fields.get("updated") or ""),
                             extra={k: v for k, v in fields.items() if k not in known})

    def write_knowledge(self, note: KnowledgeNote) -> KnowledgeNote:
        if not note.slug:
            note.slug = slugify(note.title, 60)
        require_id(note.slug)
        note.updated = note.updated or now_iso()
        fields: dict[str, Any] = {"title": note.title, "added_by": note.added_by, "updated": note.updated}
        fields.update({k: v for k, v in note.extra.items() if k not in fields})
        atomic_write_text(self.paths.knowledge(note.slug), dump_front_matter(fields) + "\n" + note.body.strip() + "\n")
        self.refresh_area("knowledge")
        self._publish(events.PROJECT_CHANGED, what="knowledge", slug=note.slug)
        return note

    def delete_knowledge(self, slug: str) -> bool:
        try:
            self.paths.knowledge(slug).unlink()
        except FileNotFoundError:
            return False
        self.refresh_area("knowledge")
        self._publish(events.PROJECT_CHANGED, what="knowledge", slug=slug)
        return True

    def search_knowledge(self, query: str, limit: int = 10) -> list[tuple[KnowledgeNote, int]]:
        """Notes ranked by how many of the query's words they contain (title words count double)."""
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1]
        scored = []
        for note in self.list_knowledge():
            if not words:
                scored.append((note, 0))
                continue
            title, body = note.title.lower(), note.body.lower()
            score = sum(2 * title.count(w) + body.count(w) for w in words)
            if score:
                scored.append((note, score))
        scored.sort(key=lambda pair: (-pair[1], -ts_sort_key(pair[0].updated)))
        return scored[:limit]

    # ── pending requests and answers ──

    def write_pending(self, request: PendingDecision | PendingRequest) -> None:
        require_id(request.id)
        request.asked_at = request.asked_at or now_iso()
        atomic_write_json(self.paths.pending(request.id), request.to_dict())
        if isinstance(request, PendingDecision):
            self._publish(events.DECISION_PENDING, decision=request.to_dict())
        else:
            self._publish(events.REQUEST_PENDING, request=request.to_dict())

    def read_pending(self, request_id: str) -> PendingDecision | PendingRequest | None:
        data = read_json(self.paths.pending(request_id))
        if data is None:
            return None
        data.setdefault("id", request_id)
        return _pending_from(data)

    def list_pending(self, kind: str | None = None, include_answered: bool = False) -> list[PendingDecision | PendingRequest]:
        """What agents are waiting on, oldest first. Answered ones are left out."""
        try:
            names = os.listdir(self.paths.pending_dir)
        except FileNotFoundError:
            return []
        items = []
        for name in names:
            stem = name[:-5]
            if not name.endswith(".json") or not is_valid_id(stem):
                continue
            if not include_answered and self.paths.answer(stem).exists():
                continue
            item = self.read_pending(stem)
            if item is not None and (kind is None or item.kind == kind):
                items.append(item)
        items.sort(key=lambda item: ts_sort_key(item.asked_at))
        return items

    def remove_pending(self, request_id: str) -> None:
        path = self.paths.pending(request_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        _note_deleted(path)

    def read_answer(self, request_id: str) -> Answer | None:
        data = read_json(self.paths.answer(request_id))
        if data is None:
            return None
        data.setdefault("id", request_id)
        return Answer.from_dict(data)

    def write_answer(self, answer: Answer) -> None:
        """Record a reply (screenshot path, check results, or a decision answer as is)."""
        require_id(answer.id)
        answer.answered_at = answer.answered_at or now_iso()
        with self.locked():
            if self.paths.answer(answer.id).exists():
                raise AlreadyAnswered(f"{answer.id} already has an answer.")
            atomic_write_json(self.paths.answer(answer.id), answer.to_dict())
        self._publish(events.DECISION_ANSWERED, id=answer.id, answer=answer.to_dict())

    def answer_decision(self, decision_id: str, option: str = "", *, text: str = "", by: str = "",
                        why: str = "") -> Answer:
        """The person decided. Writes the answer the agent is waiting for, adds the
        decision to decisions.md and the history. Raises AlreadyAnswered if a
        teammate got there first."""
        pending = self.read_pending(decision_id)
        if not isinstance(pending, PendingDecision):
            raise NotFound(f"Nobody is waiting on a decision called {decision_id}.")
        chosen = pending.option(option) if option else None
        if option and chosen is None:
            raise ValueError(f"{option} isn't one of the options.")
        if not chosen and not text.strip():
            raise ValueError("Choose an option or write an answer.")
        answer = Answer(id=decision_id, option=chosen.id if chosen else "", label=chosen.label if chosen else "",
                        text=text.strip(), answered_by=by)
        with self.locked():
            self.write_answer(answer)
            self.append_decision(Decision(title=pending.question, chosen=answer.label or answer.text,
                                          why=why or (chosen.consequence if chosen else ""), who=by,
                                          goal=pending.goal, id=decision_id))
        self.log(f"Decided: {answer.label or answer.text}", kind="decision", actor=by, goal=pending.goal,
                 data={"decision": decision_id, "question": pending.question, "option": answer.option})
        if str((pending.extra or {}).get("source") or "") not in ("permission", "install"):
            try:
                self._decision_note(pending, answer, by)
            except (StoreError, OSError, ValueError):
                log.exception("couldn't keep the decision in knowledge")
        return answer

    def expire_decisions(self, now: float | None = None) -> list[str]:
        """Questions nobody answered in time are closed. The agent stopped waiting long before (after a short
        wait it carries on with its recommendation, or with work that doesn't depend on it), so the card says
        that, and the conversation stops saying it needs the person. Returns the closed ids."""
        import time as _time
        now = _time.time() if now is None else now
        closed = []
        for pending in self.list_pending("decision"):
            expires = ts_sort_key(pending.expires_at) if pending.expires_at else 0
            if not expires or expires > now:
                continue
            went = pending.option(pending.recommended or pending.default) if (pending.recommended or pending.default) \
                else None
            words = ("No answer in time, so the agent went ahead with its recommendation." if went else
                     "No answer in time, so the question was closed.")
            try:
                self.write_answer(Answer(id=pending.id, option=went.id if went else "", label=went.label if went else "",
                                         text=words, answered_by="playground"))
            except AlreadyAnswered:
                pass
            self.remove_pending(pending.id)
            self.log(f"Closed unanswered: {pending.question}", kind="decision", goal=pending.goal,
                     data={"decision": pending.id, "answered": False})
            if pending.goal and not any(p.goal == pending.goal for p in self.list_pending("decision")):
                def calm(goal) -> None:
                    if goal.status == GoalStatus.NEEDS_YOU:
                        goal.status, goal.status_note = GoalStatus.WORKING, ""
                try:
                    self.update_goal(pending.goal, calm)
                except NotFound:
                    pass
            closed.append(pending.id)
        return closed

    def _decision_note(self, pending: PendingDecision, answer: Answer, by: str) -> None:
        """A decision the person made on a card becomes a knowledge note, in the short ADR form, so the next
        agent knows what was chosen, over what, and why, without anyone remembering to write it down."""
        chosen = pending.option(answer.option) if answer.option else None
        others = [o for o in pending.options if not chosen or o.id != chosen.id]
        context = _one_line(pending.context) or "Asked while working on the project."
        lines = ["## Context", context, "", "## Decision",
                 (chosen.label if chosen else answer.text) + (f": {chosen.consequence}" if chosen and chosen.consequence
                                                              else ""),
                 f"Chosen by {by or 'the person'}" + (f", who added: {answer.text}" if answer.text and chosen else "") + "."]
        if others:
            lines += ["", "## Alternatives"] + [f"- {o.label}" + (f": {o.consequence}" if o.consequence else "")
                                                for o in others]
        stamp = now_iso()
        self.write_knowledge(KnowledgeNote(
            slug="", title=_one_line(pending.question).rstrip("?") + "?", body="\n".join(lines), added_by=by,
            updated=stamp, extra={"kind": "decision", "status": "kept", "created": stamp, "verified": stamp[:10],
                                  "decision": pending.id}))

    # ── run.toml and preview state ──

    def read_run_config(self) -> RunConfig | None:
        text = read_text(self.paths.run_toml)
        if text is None:
            return None
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            log.warning("run.toml can't be read: %s", error)
            return None
        app = document.get("app")
        return RunConfig.from_dict(app) if isinstance(app, dict) else None

    def write_run_config(self, config: RunConfig, comment: str = "") -> None:
        """Rewrite ``[app]``. Every other table in run.toml (``[deploy]`` and anything an agent or a newer
        Playground added) is kept, and so is the file's opening comment when no new one is given."""
        data = config.to_dict()
        if not data["env"]:
            del data["env"]
        document: dict[str, Any] = {"app": data}
        old = read_text(self.paths.run_toml)
        if old is not None:
            try:
                existing = tomllib.loads(old)
            except tomllib.TOMLDecodeError as error:
                raise StoreError(f"run.toml can’t be read, so it wasn’t changed: {error}") from error
            document.update({key: value for key, value in existing.items() if key != "app"})
            if not comment:
                lead = []
                for line in old.splitlines():
                    if not line.startswith("#"):
                        break
                    lead.append(line[1:].strip())
                comment = "\n".join(lead)
        header = "".join(f"# {line}\n" for line in comment.splitlines()) if comment else ""
        atomic_write_text(self.paths.run_toml, header + dump_toml(document))
        self._publish(events.PROJECT_CHANGED, what="run")

    def write_app_heartbeat(self) -> None:
        """The app calls this every few seconds while the project is open, so the
        MCP server knows there's a Stage and a checker to ask."""
        atomic_write_json(self.paths.app_heartbeat, {"pid": os.getpid(), "updated": now_iso()})

    def clear_app_heartbeat(self) -> None:
        try:
            self.paths.app_heartbeat.unlink()
        except FileNotFoundError:
            pass

    def read_app_heartbeat(self) -> dict | None:
        return read_json(self.paths.app_heartbeat)

    def read_preview_state(self) -> dict:
        """What the app's preview last reported: {state, url, error, updated}."""
        return read_json(self.paths.preview_state) or {"state": "stopped", "url": "", "error": ""}

    def write_preview_state(self, state: str, url: str = "", error: str = "") -> None:
        atomic_write_json(self.paths.preview_state, {"state": state, "url": url, "error": error,
                                                     "updated": now_iso(), "pid": os.getpid()})
        self._publish(events.PREVIEW_STATE, state=state, url=url, error=error)


    # ── stack.toml: services and dependencies ──

    def read_stack(self) -> Stack:
        text = read_text(self.paths.stack_toml)
        if text is None:
            return Stack()
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise StoreError(f"stack.toml can't be read: {error}") from error
        return Stack(services=[Service.from_dict(s) for s in document.get("services") or [] if isinstance(s, dict)],
                     dependencies=[Dependency.from_dict(d) for d in document.get("dependencies") or [] if isinstance(d, dict)],
                     extra={k: v for k, v in document.items() if k not in ("services", "dependencies")})

    def write_stack(self, stack: Stack, service_id: str | None = None) -> None:
        for service in stack.services:
            _check_service(service)
        document: dict[str, Any] = dict(stack.extra)
        if stack.services:
            document["services"] = [_without_empty(s.to_dict()) for s in stack.services]
        if stack.dependencies:
            document["dependencies"] = [d.to_dict() for d in stack.dependencies]
        header = ("# What this project is built on: the outside services it uses and the packages it depends on.\n"
                  "# Secret names only. The values live in the project vault, never here.\n")
        atomic_write_text(self.paths.stack_toml, header + (dump_toml(document) if document else ""))
        if service_id:
            self._publish(events.STACK_CHANGED, service_id=service_id)
        else:
            self._publish(events.STACK_CHANGED)

    def add_service(self, service: Service) -> Service:
        if not service.id:
            service.id = slugify(service.name, 40)
        _check_service(service)
        service.added = service.added or now_iso()
        with self.locked():
            stack = self.read_stack()
            if stack.service(service.id) is not None:
                raise StoreError(f"There's already a service called {service.id}.")
            stack.services.append(service)
            self.write_stack(stack, service.id)
        return service

    def update_service(self, service_id: str, change: Callable[[Service], None]) -> Service:
        with self.locked():
            stack = self.read_stack()
            service = stack.service(service_id)
            if service is None:
                raise NotFound(f"There's no service called {service_id}.")
            change(service)
            service.id = service_id
            self.write_stack(stack, service_id)
        return service

    def remove_service(self, service_id: str) -> Service:
        with self.locked():
            stack = self.read_stack()
            service = stack.service(service_id)
            if service is None:
                raise NotFound(f"There's no service called {service_id}.")
            stack.services.remove(service)
            self.write_stack(stack, service_id)
        return service

    def refresh_dependencies(self, notes: dict[str, str] | None = None) -> list[Dependency]:
        """Re-read package.json and requirements*.txt into [[dependencies]].

        A "why" already written is kept for a package that's still there;
        ``notes`` ({name: why}) adds or replaces one-line reasons.
        """
        notes = {k: _one_line(v) for k, v in (notes or {}).items() if str(v).strip()}
        with self.locked():
            stack = self.read_stack()
            known = {(d.name, d.source): d.why for d in stack.dependencies}
            found = scan_dependencies(self.root)
            for dependency in found:
                dependency.why = notes.get(dependency.name) or known.get((dependency.name, dependency.source), "")
            if [d.to_dict() for d in found] != [d.to_dict() for d in stack.dependencies]:
                stack.dependencies = found
                self.write_stack(stack)
        return found

    # ── issues/<n>.md: bug reports ──

    def issue_numbers(self) -> list[int]:
        try:
            names = os.listdir(self.paths.issues_dir)
        except FileNotFoundError:
            return []
        return sorted(int(name[:-3]) for name in names if name.endswith(".md") and name[:-3].isdigit()
                      and int(name[:-3]) > 0)

    def list_issues(self, status: str | None = None, include_closed: bool = True) -> list[Issue]:
        """Bugs, newest first."""
        issues = []
        for number in reversed(self.issue_numbers()):
            try:
                issue = self.read_issue(number)
            except StoreError as error:
                log.warning("skipping issue %s: %s", number, error)
                continue
            if (status is None or issue.status == status) and (include_closed or not issue.is_closed):
                issues.append(issue)
        return issues

    def read_issue(self, number: int) -> Issue:
        text = read_text(self.paths.issue(number))
        if text is None:
            raise NotFound(f"There's no bug #{number}.")
        return parse_issue(number, text)

    def write_issue(self, issue: Issue, touch: bool = True) -> None:
        _check_issue(issue)
        if touch:
            issue.updated = now_iso()
        issue.created = issue.created or issue.updated or now_iso()
        atomic_write_text(self.paths.issue(issue.number), format_issue(issue))
        self.refresh_area("issues")
        self._publish(events.ISSUE_CHANGED, number=issue.number)

    def create_issue(self, title: str, **fields: Any) -> Issue:
        """File a bug with the next number."""
        title = _one_line(title)
        if not title:
            raise ValueError("A bug needs a title.")
        with self.locked():
            numbers = self.issue_numbers()
            stamp = now_iso()
            issue = Issue(number=(numbers[-1] + 1) if numbers else 1, title=title, created=stamp, updated=stamp, **fields)
            self.write_issue(issue, touch=False)
        return issue

    def update_issue(self, number: int, change: Callable[[Issue], None]) -> Issue:
        with self.locked():
            issue = self.read_issue(number)
            change(issue)
            issue.number = number
            self.write_issue(issue)
        return issue

    # ── tasks/<id>.md ──

    def task_ids(self) -> list[str]:
        try:
            names = os.listdir(self.paths.tasks_dir)
        except FileNotFoundError:
            return []
        return sorted(name[:-3] for name in names
                      if name.endswith(".md") and name != "index.md" and is_valid_id(name[:-3]))

    def list_tasks(self, include_closed: bool = True) -> list[Task]:
        from .areas import parse_task
        tasks = []
        for task_id in self.task_ids():
            text = read_text(self.paths.task(task_id))
            if text is None:
                continue
            task = parse_task(task_id, text)
            if include_closed or task.is_open:
                tasks.append(task)
        return tasks

    def read_task(self, task_id: str) -> Task:
        from .areas import parse_task
        text = read_text(self.paths.task(task_id))
        if text is None:
            raise NotFound(f"There's no task {task_id}.")
        return parse_task(task_id, text)

    def write_task(self, task: Task, *, by: str = "", touch: bool = True) -> Task:
        from .areas import format_task
        require_id(task.id)
        task.title = _one_line(task.title)
        if not task.title:
            raise ValueError("A task needs a title.")
        if task.status not in TASK_STATUSES:
            raise ValueError(f"A task's status is one of {', '.join(TASK_STATUSES)}.")
        if touch:
            task.updated_at = now_iso()
            task.updated_by = by or task.updated_by
        task.created_at = task.created_at or task.updated_at or now_iso()
        task.created_by = task.created_by or by
        if not task.is_open:
            task.claimed_by = task.claimed_until = ""
        atomic_write_text(self.paths.task(task.id), format_task(task))
        self.refresh_area("tasks")
        self._publish(events.TASK_CHANGED, id=task.id)
        return task

    def create_task(self, title: str, *, by: str = "", **fields: Any) -> Task:
        """A new task with a short random id (t-7f3k2), so two computers never pick the same one."""
        from .areas import new_task_id
        with self.locked():
            stamp = now_iso()
            task = Task(id=new_task_id(self.task_ids()), title=title, created_at=stamp, updated_at=stamp,
                        created_by=by, updated_by=by, **fields)
            return self.write_task(task, by=by, touch=False)

    def update_task(self, task_id: str, change: Callable[[Task], None], *, by: str = "") -> Task:
        with self.locked():
            task = self.read_task(task_id)
            change(task)
            task.id = task_id
            return self.write_task(task, by=by)

    def claim_task(self, task_id: str, who: str, *, hours: float | None = None) -> Task:
        """Take a task for a while (two hours by default). Refused while someone else's claim is running."""
        from .areas import CLAIM_HOURS
        from datetime import datetime, timedelta, timezone
        if not who:
            raise ValueError("Say who is claiming the task.")
        with self.locked():
            task = self.read_task(task_id)
            holder = task.claim_holder()
            if holder and holder != who:
                raise StoreError(f"{holder} is working on {task_id} until {task.claimed_until}.")
            if not task.is_open:
                raise StoreError(f"{task_id} is already {task.status}.")
            until = datetime.now(timezone.utc) + timedelta(hours=hours if hours is not None else CLAIM_HOURS)
            task.status, task.claimed_by = "doing", who
            task.claimed_until = until.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            return self.write_task(task, by=who)

    def release_task(self, task_id: str, *, by: str = "") -> Task:
        def change(task: Task) -> None:
            task.claimed_by = task.claimed_until = ""
            if task.status == "doing":
                task.status = "todo"
        return self.update_task(task_id, change, by=by)

    def delete_task(self, task_id: str) -> bool:
        try:
            self.paths.task(task_id).unlink()
        except FileNotFoundError:
            return False
        self.refresh_area("tasks")
        self._publish(events.TASK_CHANGED, id=task_id)
        return True

    # ── channel.jsonl: the team channel ──

    def read_channel(self, limit: int | None = None) -> list[dict]:
        entries = [e for e in read_jsonl(self.paths.channel) if isinstance(e, dict)]
        return entries[-limit:] if limit else entries

    def post_channel(self, text: str, *, by: str, kind: str = "note", task: str = "", to: str = "",
                     files: list[str] | None = None, conversation: str = "") -> dict:
        """Add one line to the team channel and refresh its digest."""
        from .areas import CHANNEL_KINDS
        text = _one_line(text)
        if not text:
            raise ValueError("Say something to post.")
        if kind not in CHANNEL_KINDS:
            raise ValueError(f"A channel post is one of {', '.join(CHANNEL_KINDS)}.")
        if looks_like_secret(text):
            raise ValueError("That looks like a password or key. Secrets never go in the channel.")
        entry: dict[str, Any] = {"ts": now_iso(), "from": by or "someone", "kind": kind, "text": text[:400]}
        if task:
            entry["task"] = task
        if to:
            entry["to"] = to
        if files:
            entry["files"] = [str(f) for f in files[:10]]
        if conversation:
            entry["conversation"] = conversation
        with self.locked():
            append_line(self.paths.channel, json.dumps(entry, ensure_ascii=False))
        self.refresh_area("channel")
        self._publish(events.CHANNEL_POSTED, entry=entry)
        return entry

    # ── index.md for each area, and the rules in AGENTS.md ──

    def refresh_area(self, area: str) -> None:
        """Rewrite an area's one-line-per-item index (and, for knowledge, the rules in AGENTS.md).
        Cheap, and only written when it changed. Never fails the write that caused it."""
        from . import areas
        try:
            if area == "tasks":
                text = areas.render_task_index(self.list_tasks())
            elif area == "issues":
                text = areas.render_issue_index(self.list_issues())
            elif area == "knowledge":
                notes = self.list_knowledge()
                text = areas.render_knowledge_index(notes)
                self.sync_rules(notes)
            elif area == "channel":
                text = areas.render_channel_digest(self.read_channel())
            elif area == "journal":
                try:
                    names = {m.id: m.name for m in self.read_project().members}
                except StoreError:
                    names = {}
                text = areas.render_journal(self, names=names)
            elif area == "now":
                text = areas.render_now(self)
            else:
                raise ValueError(f"no area called {area}")
            path = {"channel": self.paths.channel_digest, "journal": self.paths.journal,
                    "now": self.paths.now}.get(area) or self.paths.index(area)
            if area == "now":
                old = read_text(path) or ""
                # Its first lines say when it was made: only a change below them is worth a write.
                if old.split("\n", 3)[-1] == text.split("\n", 3)[-1]:
                    return
            if read_text(path) != text:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, text)
            if area != "now":
                self.refresh_area("now")          # the map follows whatever it summarizes
            else:
                self._refresh_parent_now()
        except (OSError, StoreError) as error:
            log.warning("couldn't refresh the %s index: %s", area, error)

    def _refresh_parent_now(self) -> None:
        """A part's map changed: its parent's map lists it, so that follows too (up the family, never down)."""
        from . import family
        up = family.parent_of(self.root)
        if up is not None:
            try:
                ProjectStore(up).refresh_area("now")
            except Exception:  # noqa: BLE001 - the parent's map is a courtesy
                log.debug("couldn't refresh %s's now.md", up, exc_info=True)

    def refresh_areas(self) -> None:
        for area in ("tasks", "issues", "knowledge", "channel", "journal"):
            self.refresh_area(area)

    def sync_rules(self, notes: list[KnowledgeNote] | None = None) -> bool:
        """Copy the kept rules into AGENTS.md's rules block, so every agent always has them."""
        from . import areas
        text = read_text(self.paths.agents_md)
        if text is None:
            return False
        updated = areas.replace_rules(text, areas.kept_rules(notes if notes is not None else self.list_knowledge()))
        if updated is None or updated == text:
            return False
        atomic_write_text(self.paths.agents_md, updated)
        return True

    # ── usage/services.jsonl and analytics.jsonl ──

    def append_service_usage(self, usage: ServiceUsage) -> ServiceUsage:
        require_id(usage.service_id)
        if not usage.period:
            raise ValueError("A usage snapshot needs a period.")
        bad = [k for k, v in usage.metrics.items() if not isinstance(v, (int, float)) or isinstance(v, bool)]
        if bad:
            raise ValueError(f"Usage numbers only: {', '.join(bad)} isn't a number.")
        usage.ts = usage.ts or now_iso()
        with self.locked():
            append_line(self.paths.service_usage, json.dumps(usage.to_dict(), ensure_ascii=False))
        self._publish(events.USAGE_CHANGED, service_id=usage.service_id, entry=usage.to_dict())
        return usage

    def read_service_usage(self, service_id: str | None = None, limit: int | None = None) -> list[ServiceUsage]:
        """Oldest first; with ``limit``, the most recent ones."""
        entries = [ServiceUsage.from_dict(r) for r in read_jsonl(self.paths.service_usage)]
        if service_id:
            entries = [e for e in entries if e.service_id == service_id]
        return entries[-limit:] if limit else entries

    def append_analytics(self, snapshot: AnalyticsSnapshot) -> AnalyticsSnapshot:
        if not snapshot.period:
            raise ValueError("An analytics snapshot needs a period.")
        snapshot.ts = snapshot.ts or now_iso()
        with self.locked():
            append_line(self.paths.analytics, json.dumps(snapshot.to_dict(), ensure_ascii=False))
        self._publish(events.ANALYTICS_CHANGED, entry=snapshot.to_dict())
        return snapshot

    def read_analytics(self, limit: int | None = None) -> list[AnalyticsSnapshot]:
        records = read_jsonl_tail(self.paths.analytics, limit) if limit else read_jsonl(self.paths.analytics)
        return [AnalyticsSnapshot.from_dict(r) for r in records]


# ══ Stack and issue files ══════════════════════════════════════════════════════

def _without_empty(data: dict) -> dict:
    return {k: v for k, v in data.items() if v not in ("", [], {}, None) or k in ("id", "name")}


def _check_service(service: Service) -> None:
    require_id(service.id)
    if not service.name.strip():
        raise ValueError("A service needs a name.")
    if service.kind not in SERVICE_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(SERVICE_KINDS)}.")
    for url in (service.console_url, service.docs_url):
        if url and not url.startswith(("https://", "http://")):
            raise ValueError(f"{url} isn't a web address.")
    for value in [*service.secrets, *service.environments.values(), service.plan, service.purpose]:
        if looks_like_secret(value):
            raise ValueError("That looks like a key or password. The project keeps secret names only; "
                             "put the value in the project vault.")


def _check_issue(issue: Issue) -> None:
    if issue.severity not in ISSUE_SEVERITIES:
        raise ValueError(f"severity must be one of: {', '.join(ISSUE_SEVERITIES)}.")
    if issue.status not in ISSUE_STATES:
        raise ValueError(f"status must be one of: {', '.join(ISSUE_STATES)}.")
    if issue.place and issue.place not in ISSUE_PLACES:
        raise ValueError("place is stage or live.")
    if issue.goal:
        require_id(issue.goal)


_ISSUE_FIELDS = ("number", "title", "severity", "status", "reporter", "goal", "place", "created", "updated")
_ISSUE_SECTION_KEYS = {"what happened": "what_happened", "expected": "expected", "steps": "steps", "evidence": "evidence"}


def parse_issue(number: int, text: str) -> Issue:
    fields, body = parse_front_matter(text)
    preamble, sections = parse_sections(body)
    issue = Issue(number=number, title=_str(fields.get("title")) or f"Bug #{number}",
                  severity=_str(fields.get("severity")).lower() or "medium", status=_str(fields.get("status")).lower() or "open",
                  reporter=_str(fields.get("reporter")), goal=_str(fields.get("goal")), place=_str(fields.get("place")),
                  created=_str(fields.get("created")), updated=_str(fields.get("updated")),
                  extra={k: v for k, v in fields.items() if k not in _ISSUE_FIELDS})
    if issue.severity not in ISSUE_SEVERITIES:
        issue.severity = "medium"
    if issue.status not in ISSUE_STATES:
        issue.status = {"done": "fixed", "closed": "fixed", "in-progress": "fixing",
                        "investigating": "fixing"}.get(issue.status, "open")
    if issue.place and issue.place not in ISSUE_PLACES:
        # An agent's own word for where it happened ("dev", "preview", a place's id) reads as the preview,
        # so the bug can still be changed and saved.
        issue.extra.setdefault("where", issue.place)
        issue.place = "live" if issue.place.lower() in ("live", "production", "prod") else "stage"
    for heading, section in sections:
        key = _ISSUE_SECTION_KEYS.get(heading.strip().lower())
        if key:
            setattr(issue, key, section)
        else:
            issue.sections.append((heading, section))
    if preamble:
        issue.what_happened = f"{preamble}\n\n{issue.what_happened}".strip()
    return issue


def format_issue(issue: Issue) -> str:
    fields: dict[str, Any] = {"number": issue.number, "title": issue.title, "severity": issue.severity,
                              "status": issue.status}
    for key in ("reporter", "goal", "place", "created", "updated"):
        if getattr(issue, key):
            fields[key] = getattr(issue, key)
    fields.update({k: v for k, v in issue.extra.items() if k not in fields})
    sections = [(heading, getattr(issue, key)) for heading, key in
                zip(ISSUE_SECTIONS, ("what_happened", "expected", "steps", "evidence"))]
    return dump_front_matter(fields) + "\n" + dump_sections("", sections + list(issue.sections))


_REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?P<spec>.*)$")


def scan_dependencies(root: Path) -> list[Dependency]:
    """Packages from package.json and requirements*.txt, in the project folder and one level down."""
    root = Path(root)
    folders = [root]
    try:
        folders += sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()
                          and not p.name.startswith(".") and p.name not in ("node_modules", "__pycache__", "venv"))
    except OSError:
        pass
    found: list[Dependency] = []
    for folder in folders:
        package = folder / "package.json"
        if package.is_file():
            found.extend(_npm_dependencies(package, package.relative_to(root).as_posix()))
        for requirements in sorted(folder.glob("requirements*.txt")):
            found.extend(_pip_dependencies(requirements, requirements.relative_to(root).as_posix()))
    return found


def _npm_dependencies(path: Path, source: str) -> list[Dependency]:
    data = read_json(path)
    if data is None:
        return []
    found = []
    for key, dev in (("dependencies", False), ("devDependencies", True)):
        table = data.get(key)
        if isinstance(table, dict):
            found.extend(Dependency(name=str(name), version=str(version), source=source, dev=dev)
                         for name, version in sorted(table.items()))
    return found


def _pip_dependencies(path: Path, source: str) -> list[Dependency]:
    found = []
    for raw in (read_text(path) or "").splitlines():
        line = raw.split(" #", 1)[0].split(";", 1)[0].strip()
        if not line or line.startswith(("#", "-")):
            continue
        match = _REQUIREMENT.match(line)
        if match:
            spec = match.group("spec").strip()
            found.append(Dependency(name=match.group("name"), version="" if spec.startswith("@") else spec,
                                    source=source, dev="dev" in path.stem or "test" in path.stem))
    return found


# ══ Goal files ═════════════════════════════════════════════════════════════════

def parse_goal(goal_id: str, text: str) -> Goal:
    fields, body = parse_front_matter(text)
    preamble, sections = parse_sections(body)
    outcome, notes, stories, others = "", "", [], []
    leftover = ""
    for heading, section in sections:
        key = heading.strip().lower()
        if key == "outcome":
            outcome = section
        elif key == "stories":
            stories, leftover = parse_stories(section)
        elif key == "notes":
            notes = section
        else:
            others.append((heading, section))
    if leftover:
        notes = f"{notes}\n\n{leftover}".strip()
    if preamble:
        outcome = f"{preamble}\n\n{outcome}".strip() if outcome else preamble
    kind = str(fields.get("kind") or "goal")
    return Goal(
        id=goal_id, title=str(fields.get("title") or goal_id), status=GoalStatus.parse(fields.get("status"), GoalStatus.WORKING),
        kind=kind if kind in ("goal", "chat") else "goal", owner=_str(fields.get("owner")),
        connection=_str(fields.get("connection")), created=_str(fields.get("created")),
        updated=_str(fields.get("updated")), size=_str(fields.get("size")),
        cost_estimate=_str(fields.get("cost_estimate")), status_note=_str(fields.get("status_note")),
        outcome=outcome, stories=stories, notes=notes, sections=others,
        extra={k: v for k, v in fields.items() if k not in _GOAL_FIELDS},
    )


def format_goal(goal: Goal) -> str:
    fields: dict[str, Any] = {"id": goal.id, "title": goal.title}
    if goal.kind != "goal":
        fields["kind"] = goal.kind
    fields["status"] = goal.status.value
    for key in ("status_note", "owner", "connection", "created", "updated", "size", "cost_estimate"):
        value = getattr(goal, key)
        if value:
            fields[key] = value
    fields.update({k: v for k, v in goal.extra.items() if k not in fields})
    sections: list[tuple[str, str]] = [("Outcome", goal.outcome)]
    if goal.stories or not goal.is_chat:
        sections.append(("Stories", dump_stories(goal.stories)))
    sections.extend(goal.sections)
    sections.append(("Notes", goal.notes))
    return dump_front_matter(fields) + "\n" + dump_sections("", sections)


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def _pending_from(data: dict) -> PendingDecision | PendingRequest:
    if (data.get("kind") or "decision") == "decision":
        return PendingDecision.from_dict(data)
    return PendingRequest.from_dict(data)
