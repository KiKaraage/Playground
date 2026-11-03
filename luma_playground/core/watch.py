# SPDX-License-Identifier: Apache-2.0
"""Notice what agents change in a project and say so on the bus.

Agents write to the project directly (their own file tools) or through the MCP
server, which runs in the agent's process, not ours. Either way the app only
learns about it here. The watcher polls, using nothing but the standard
library, so it works the same on Luma, macOS and Windows:

- .playground/ (except local/) is compared file by file; each change becomes
  the matching topic (goal.changed, decision.pending, project.changed…);
- activity.jsonl, threads/*.jsonl, usage/services.jsonl and analytics.jsonl
  are followed like logs: only the new lines are read, and each becomes one
  activity.appended, thread.appended, usage.changed or analytics.changed;
- the rest of the project becomes one files.changed per poll, skipping
  .git, node_modules and other rebuilt folders.

Changes this process made through the store are skipped, so the store's own
events aren't repeated.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import threading
from . import events, store
from .model import Answer, PendingDecision, PendingRequest
from .paths import META_DIR, ProjectPaths, is_valid_id

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 0.75
MAX_PROJECT_FILES = 20_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".svelte-kit", ".next", ".nuxt",
             "dist", "build", "target", ".cache", ".parcel-cache", ".turbo", ".idea", ".gradle"}

Signature = tuple[int, int, int]
# Append-only logs in .playground/ and the topic each new line becomes (threads are handled by name).
_LOG_TOPICS = {"activity.jsonl": events.ACTIVITY_APPENDED, "usage/services.jsonl": events.USAGE_CHANGED,
               "analytics.jsonl": events.ANALYTICS_CHANGED}


class ProjectWatcher:
    """Polls one project. ``start()`` and ``stop()`` may be called from any thread."""

    def __init__(self, root: Path | str, bus: events.EventBus | None = None,
                 interval: float = DEFAULT_INTERVAL, watch_files: bool = True) -> None:
        self.paths = ProjectPaths(root)
        self.root = self.paths.root
        self.bus = bus if bus is not None else events.bus
        self.interval = min(max(interval, 0.1), 5.0)
        self.watch_files = watch_files
        self._meta: dict[Path, Signature] = {}
        self._offsets: dict[Path, int] = {}
        self._files: dict[str, tuple[int, int]] = {}
        self._places: list | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_lock = threading.Lock()
        self._primed = False

    # ── lifecycle ──

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self.prime()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"playground-watch:{self.root.name}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def __enter__(self) -> "ProjectWatcher":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll()
            except Exception:  # noqa: BLE001 - the watcher must outlive one bad poll
                log.exception("watching %s failed this round", self.root)

    # ── polling ──

    def prime(self) -> None:
        """Take the starting picture silently: what's there now isn't news."""
        with self._poll_lock:
            self._meta = self._scan_meta()
            self._offsets = {path: _complete_length(path) for path in self._meta if path.suffix == ".jsonl"}
            self._files = self._scan_files() if self.watch_files else {}
            self._places = self._read_places()
            self._primed = True

    def poll(self) -> None:
        """Look once and publish what changed. Tests call this directly."""
        if not self._primed:
            self.prime()
            return
        with self._poll_lock:
            if not self.paths.meta.is_dir():
                return  # moved, renamed or being deleted: don't announce everything as gone
            current = self._scan_meta()
            for path in sorted(set(current) | set(self._meta)):
                before, after = self._meta.get(path), current.get(path)
                if before == after:
                    continue
                if path.suffix == ".jsonl":
                    self._follow_log(path, after)
                elif not store.is_own_state(path, after):
                    self._dispatch(path, removed=after is None)
            self._meta = current
            if self.watch_files:
                files = self._scan_files()
                changed = sorted(rel for rel in set(files) | set(self._files)
                                 if files.get(rel) != self._files.get(rel)
                                 and not store.is_own_state(self.root / rel, _signature(self.root / rel)))
                self._files = files
                if changed:
                    self._publish(events.FILES_CHANGED, paths=changed)

    def _scan_meta(self) -> dict[Path, Signature]:
        found: dict[Path, Signature] = {}
        local = self.paths.local_dir
        for directory, dirnames, filenames in os.walk(self.paths.meta):
            base = Path(directory)
            if base == local:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and base / d != local]
            for name in filenames:
                if name.startswith(".") or name.endswith(".tmp"):
                    continue  # temp files of atomic writes in flight
                path = base / name
                signature = _signature(path)
                if signature is not None:
                    found[path] = signature
        return found

    def _scan_files(self) -> dict[str, tuple[int, int]]:
        found: dict[str, tuple[int, int]] = {}
        for directory, dirnames, filenames in os.walk(self.root):
            base = Path(directory)
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not (base == self.root and d == META_DIR))
            for name in filenames:
                path = base / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                found[path.relative_to(self.root).as_posix()] = (st.st_mtime_ns, st.st_size)
                if len(found) >= MAX_PROJECT_FILES:
                    log.info("%s has more than %d files; watching the first ones only", self.root, MAX_PROJECT_FILES)
                    return found
        return found

    # ── turning file changes into topics ──

    def _publish(self, topic: str, **payload) -> None:
        self.bus.publish(topic, project=str(self.root), **payload)

    def _dispatch(self, path: Path, removed: bool) -> None:
        rel = path.relative_to(self.paths.meta)
        parts = rel.parts
        stem = path.stem
        if rel == Path("project.toml"):
            self._publish(events.PROJECT_CHANGED, what="project")
            places = self._read_places()
            if places != self._places:
                self._places = places
                self._publish(events.PLACES_CHANGED)
        elif rel == Path("run.toml"):
            self._publish(events.PROJECT_CHANGED, what="run")
        elif rel == Path("stack.toml"):
            self._publish(events.STACK_CHANGED)
        elif len(parts) == 2 and parts[0] == "issues" and path.suffix == ".md" and stem.isdigit():
            if removed:
                self._publish(events.ISSUE_CHANGED, number=int(stem), removed=True)
            else:
                self._publish(events.ISSUE_CHANGED, number=int(stem))
        elif rel == Path("brief.md"):
            self._publish(events.PROJECT_CHANGED, what="brief")
        elif rel == Path("decisions.md"):
            self._publish(events.PROJECT_CHANGED, what="decisions")
        elif len(parts) == 2 and parts[0] == "knowledge" and path.suffix == ".md":
            self._publish(events.PROJECT_CHANGED, what="knowledge", slug=stem)
        elif len(parts) == 2 and parts[0] == "goals" and path.suffix == ".md" and is_valid_id(stem):
            if removed:
                self._publish(events.GOAL_CHANGED, goal_id=stem, removed=True)
            else:
                self._publish(events.GOAL_CHANGED, goal_id=stem)
        elif len(parts) == 2 and parts[0] == "pending" and path.suffix == ".json" and not removed and is_valid_id(stem):
            data = store.read_json(path)
            if data is None:
                return
            data.setdefault("id", stem)
            if (data.get("kind") or "decision") == "decision":
                self._publish(events.DECISION_PENDING, decision=PendingDecision.from_dict(data).to_dict())
            else:
                self._publish(events.REQUEST_PENDING, request=PendingRequest.from_dict(data).to_dict())
        elif len(parts) == 2 and parts[0] == "answers" and path.suffix == ".json" and not removed and is_valid_id(stem):
            data = store.read_json(path)
            if data is not None:
                data.setdefault("id", stem)
                self._publish(events.DECISION_ANSWERED, id=stem, answer=Answer.from_dict(data).to_dict())

    def _follow_log(self, path: Path, signature: Signature | None) -> None:
        """Read the lines added since last time; each one is its own event."""
        rel = path.relative_to(self.paths.meta)
        goal_id = path.stem if rel.parts[0] == "threads" and len(rel.parts) == 2 and is_valid_id(path.stem) else None
        topic = _LOG_TOPICS.get(rel.as_posix()) or (events.THREAD_APPENDED if goal_id else None)
        if topic is None:
            return
        offset = self._offsets.get(path, 0)
        size = signature[1] if signature else 0
        if signature is None or size < offset:
            # Removed or rewritten: start over from its end, and tell listeners to reload.
            self._offsets[path] = _complete_length(path) if signature else 0
            if goal_id:
                self._publish(events.GOAL_CHANGED, goal_id=goal_id)
            else:
                self._publish(events.PROJECT_CHANGED, what=rel.parts[0].split(".")[0])
            return
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(size - offset)
        except OSError:
            return
        complete = data.rfind(b"\n") + 1  # a half-written last line waits for the next poll
        position = offset
        for raw in data[:complete].splitlines(keepends=True):
            start, position = position, position + len(raw)
            line = raw.strip()
            if not line or store.is_own_append(path, start, position):
                continue
            try:
                record = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                log.warning("skipping a damaged line in %s", path)
                continue
            if not isinstance(record, dict):
                continue
            if goal_id:
                self._publish(events.THREAD_APPENDED, goal_id=goal_id, entry=record)
            elif topic == events.USAGE_CHANGED:
                self._publish(topic, service_id=str(record.get("service_id") or ""), entry=record)
            else:
                self._publish(topic, entry=record)
        self._offsets[path] = offset + complete

    def _read_places(self) -> list | None:
        try:
            return [p.to_dict() for p in store.ProjectStore(self.root, bus=events.EventBus()).read_project().places]
        except store.StoreError:
            return None


def _signature(path: Path) -> Signature | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _complete_length(path: Path) -> int:
    """Bytes up to and including the last newline: where whole lines end."""
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    return data.rfind(b"\n") + 1


def watch(root: Path | str, bus: events.EventBus | None = None, interval: float = DEFAULT_INTERVAL) -> ProjectWatcher:
    """Start watching a project; call ``.stop()`` on the result when done."""
    watcher = ProjectWatcher(root, bus=bus, interval=interval)
    watcher.start()
    return watcher
