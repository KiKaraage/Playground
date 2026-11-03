# SPDX-License-Identifier: Apache-2.0
"""Sync in the window: a small status indicator, and starting or stopping sync.

The window owns one SyncEngine per open project (``core.sync``). These helpers
keep that in one place so window.py needs only a handful of calls:

    from . import sync_ui
    sync_ui.start_sync(window)                 # after a project opens (no-op for an unshared project)
    sync_ui.set_goal(window, goal_id)          # when the selected goal changes (presence)
    sync_ui.stop_sync(window)                  # before the project closes, and on window close
    indicator = sync_ui.SyncIndicator(window)  # in the title bar; hides itself for unshared projects

The indicator reads ``sync.state`` from the bus and shows a dot and a few
words: "Up to date", "Syncing", "Offline, will catch up", or what needs a hand.
The dot turns amber when this computer's pass runs out soon and no owner's
Playground has renewed it (the tooltip says so; People has "Ask for a fresh pass").
Its tooltip holds the plain-words detail; pressing it opens People, where the
full sync status lives.
"""
from __future__ import annotations

import logging
from pathlib import Path
import threading
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from ..core import events, sync  # noqa: E402
from . import widgets as shared  # noqa: E402

log = logging.getLogger(__name__)

_ENGINE_ATTR = "_playground_sync_engine"
_KIND = {"up-to-date": "good", "syncing": "stage", "offline": "warn", "access": "warn", "conflict": "warn",
         "removed": "bad", "error": "bad"}
SYNCING_DELAY_MS = 600     # "Syncing" only shows when a round takes a moment; quick rounds don't flicker


def window_project_dir(window: Gtk.Window) -> Path | None:
    """The open project's folder, however the window names it."""
    for name in ("project_dir", "project_path", "current_project_dir"):
        value = getattr(window, name, None)
        if value:
            return Path(value)
    project = getattr(window, "project", None)
    path = getattr(project, "path", None) or getattr(project, "root", None)
    if path:
        return Path(path)
    return Path(project) if isinstance(project, (str, Path)) else None


def engine_for(window: Gtk.Window) -> sync.SyncEngine | None:
    return getattr(window, _ENGINE_ATTR, None)


def start_sync(window: Gtk.Window, project_dir: Path | str | None = None) -> sync.SyncEngine | None:
    """Start syncing the window's project, if it's shared. Returns the engine, or None.

    Safe to call again (a different project stops the old engine first). Never
    blocks: the first round runs on the engine's own thread.
    """
    folder = Path(project_dir) if project_dir else window_project_dir(window)
    current = engine_for(window)
    if current is not None and folder is not None and current.project_dir == folder.expanduser().absolute():
        return current
    if current is not None:
        stop_sync(window)
    if folder is None:
        return None
    try:
        if not sync.sharing_info(folder).get("shared"):
            return None
        engine = sync.start_engine(folder)
    except sync.SyncError as error:
        log.info("sync didn't start for %s: %s", folder, error)
        return None
    setattr(window, _ENGINE_ATTR, engine)
    goal = getattr(window, "goal_id", None)
    if goal:
        engine.set_goal(goal)
    return engine


def stop_sync(window: Gtk.Window, wait: bool = False) -> None:
    """Stop syncing the window's project. Says goodbye on a thread unless ``wait``."""
    engine = engine_for(window)
    if engine is None:
        return
    setattr(window, _ENGINE_ATTR, None)

    def stop() -> None:
        try:
            sync.stop_engine(engine.project_dir)
        except Exception:  # noqa: BLE001 - stopping is best effort
            log.debug("stopping sync failed", exc_info=True)

    if wait:
        stop()
    else:
        threading.Thread(target=stop, name="playground-sync-stop", daemon=True).start()


def set_goal(window: Gtk.Window, goal_id: str | None) -> None:
    """Presence: tell teammates which goal this person has open."""
    engine = engine_for(window)
    if engine is not None:
        engine.set_goal(goal_id)


def sync_now(window: Gtk.Window) -> None:
    engine = engine_for(window)
    if engine is not None:
        engine.sync_now(wait=False)


class SyncIndicator(Gtk.Button):
    """A dot and a few words about sync, for the title bar. Hidden for projects that aren't shared."""

    def __init__(self, window: Gtk.Window | None = None, *, project_dir: Path | str | None = None,
                 on_open: Callable[[], None] | None = None, compact: bool = False) -> None:
        super().__init__(valign=Gtk.Align.CENTER, visible=False)
        self.add_css_class("flat")
        self.add_css_class("pg-sync-indicator")
        self.window = window
        self.compact = compact
        self._project = Path(project_dir).expanduser().absolute() if project_dir else None
        self._on_open = on_open
        self._pending: int | None = None
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, valign=Gtk.Align.CENTER,
                               hhomogeneous=True, vhomogeneous=True)
        self.dot = shared.dot("good")
        self.spinner = shared.Spinner(10)
        self.stack.add_named(self.dot, "dot")
        self.stack.add_named(self.spinner, "spin")
        self.words = Gtk.Label(xalign=0.0)
        self.words.add_css_class("caption")
        line = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        line.append(self.stack)
        if not compact:
            line.append(self.words)
        self.set_child(line)
        self.connect("clicked", lambda _b: self._open())
        self._token = events.bus.subscribe(sync.SYNC_STATE, self._on_state)
        self.connect("destroy", lambda _w: events.bus.unsubscribe(self._token))
        self.refresh()

    # ── which project ──

    def project_dir(self) -> Path | None:
        if self._project is not None:
            return self._project
        if self.window is not None:
            folder = window_project_dir(self.window)
            return folder.expanduser().absolute() if folder else None
        return None

    def set_project(self, project_dir: Path | str | None) -> None:
        self._project = Path(project_dir).expanduser().absolute() if project_dir else None
        self.refresh()

    # ── showing ──

    def refresh(self) -> None:
        folder = self.project_dir()
        engine = sync.engine_for(folder) if folder else None
        if engine is not None:
            self.render(engine.status())
            return
        info = sync.sharing_info(folder) if folder else {"shared": False}
        if info.get("shared"):
            self.render({"state": "stopped", "text": sync.STATE_WORDS["stopped"],
                         "detail": "Sync starts when the project is open."})
        else:
            self.set_visible(False)

    def render(self, status: dict) -> None:
        state = str(status.get("state") or "stopped")
        if state == "not-shared":
            self.set_visible(False)
            return
        self.set_visible(True)
        if self._pending is not None:
            GLib.source_remove(self._pending)
            self._pending = None
        if state == "syncing":
            # Only show the spinner if the round is still going a moment later.
            self._pending = GLib.timeout_add(SYNCING_DELAY_MS, self._show_syncing, dict(status))
            return
        self._paint(status)

    def _show_syncing(self, status: dict) -> bool:
        self._pending = None
        self._paint(status)
        return False

    def _paint(self, status: dict) -> None:
        state = str(status.get("state") or "stopped")
        text = str(status.get("text") or sync.STATE_WORDS.get(state, ""))
        if state == "syncing":
            self.stack.set_visible_child_name("spin")
        else:
            self.stack.set_visible_child_name("dot")
            for kind in ("good", "stage", "warn", "bad"):
                self.dot.remove_css_class(kind)
            kind = _KIND.get(state, "warn")
            if kind == "good" and status.get("renew_due"):
                kind = "warn"              # up to date, but the pass runs out soon: People says what to do
            self.dot.add_css_class(kind)
        self.words.set_label(text)
        detail = str(status.get("detail") or "")
        warning = str(status.get("warning") or "")
        home = str(status.get("home") or "")
        tip = "\n".join(filter(None, [f"{text}{f' · {home}' if home else ''}", detail, warning]))
        self.set_tooltip_text(tip)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"Sync: {text}"])
        if detail:
            self.update_property([Gtk.AccessibleProperty.DESCRIPTION], [detail])

    def _on_state(self, project: str = "", **status: Any) -> None:
        folder = self.project_dir()
        if folder is None or str(folder) != project:
            return
        GLib.idle_add(lambda: (self.render({"project": project, **status}), False)[1])

    def _open(self) -> None:
        if self._on_open is not None:
            self._on_open()
            return
        if self.window is None:
            return
        try:
            from .sheets import common
            common.open_sheet(self.window, "people")
        except Exception:  # noqa: BLE001
            log.debug("couldn't open People", exc_info=True)


__all__ = ["SyncIndicator", "start_sync", "stop_sync", "set_goal", "sync_now", "engine_for", "window_project_dir"]
