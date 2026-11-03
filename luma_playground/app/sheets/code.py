# SPDX-License-Identifier: Apache-2.0
"""The code, one step down (concept principle 7, v73 shot 10).

The files this goal touched, then the rest of the project; the change as a real
diff (git, rendered by GtkSourceView 5) or the whole file; and a terminal (VTE)
already in the project folder. Nobody needs it, and it's always here.

The goal's changes are what git says has changed since the goal's base commit
(``base_commit`` in the goal's front matter, when the runner records one), or
since the last commit, plus any file the agent's step cards say it touched.
Without GtkSourceView the text is shown plain; without VTE the terminal says
what to install. Reading files and running git happen off the main loop.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from gi.repository import Gio, GLib, Gtk, Pango

from .. import difflines
from . import common as c
from .common import text

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".svelte-kit", ".next",
             "target", ".cache"}
MAX_FILES = 600
MAX_BYTES = 512 * 1024

try:
    import gi
    gi.require_version("GtkSource", "5")
    from gi.repository import GtkSource
except (ImportError, ValueError):
    GtkSource = None


# Luma's system monospace is IBM Plex Mono (the kit's `mono` role; the RPM requires ibm-plex-mono-fonts).
# Elsewhere: GNOME's, then the common Linux ones, then the Mac's, then whatever fontconfig calls monospace.
MONO_FAMILIES = "IBM Plex Mono,Adwaita Mono,Noto Sans Mono,DejaVu Sans Mono,Source Code Pro,Menlo,Consolas,monospace"


def present(window: Gtk.Window, **options) -> c.Sheet:
    return CodePage(window, options).sheet


def _git(project_dir: Path, *args: str, timeout: float = 15) -> str:
    git = shutil.which("git")
    if git is None or not (project_dir / ".git").exists():
        return ""
    try:
        result = subprocess.run([git, *args], cwd=project_dir, capture_output=True, text=True, timeout=timeout,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"})
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def read_changes(project_dir: Path, goal_id: str | None) -> dict:
    """What changed in this goal and the project's files, from git and the goal's own record."""
    store_mod = c.module("core.store")
    base = ""
    touched: list[str] = []
    title = ""
    if goal_id and store_mod is not None:
        store = store_mod.ProjectStore(project_dir)
        try:
            goal = store.read_goal(goal_id)
            title = goal.title
            base = str(goal.extra.get("base_commit") or goal.extra.get("base") or "")
            for entry in store.read_thread(goal_id):
                for path in entry.data.get("files") or []:
                    if isinstance(path, str) and path not in touched:
                        touched.append(path)
        except Exception:  # noqa: BLE001 - a goal without a thread still shows the project
            pass
    state: dict[str, str] = {}
    if (project_dir / ".git").exists():
        known = bool(base) and bool(_git(project_dir, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").strip())
        reference = base if known else ("HEAD" if _git(project_dir, "rev-parse", "--verify", "--quiet", "HEAD").strip()
                                        else "")
        for line in _git(project_dir, "diff", "--name-status", reference).splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                state[parts[-1]] = parts[0][:1]
        for line in _git(project_dir, "ls-files", "--others", "--exclude-standard").splitlines():
            state.setdefault(line, "A")
        files = sorted(set(_git(project_dir, "ls-files").splitlines()) |
                       set(_git(project_dir, "ls-files", "--others", "--exclude-standard").splitlines()))
    else:
        reference = ""
        files = []
        for root, dirs, names in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (d == "local" and root.endswith(".playground"))]
            for name in names:
                files.append(str(Path(root, name).relative_to(project_dir)))
                if len(files) >= MAX_FILES:
                    break
        files.sort()
    changed = [p for p in touched if ((project_dir / p).exists() or p in state) and not _is_record(p)]
    for path in sorted(state):
        if path not in changed and not _is_record(path):
            changed.append(path)
    for path in changed:
        # A file the goal touched that git no longer sees as changed was already saved in the history.
        state.setdefault(path, "" if path in files else "A")
    # Playground's own records have areas of their own (Tasks, Knowledge…): the file list is the product's.
    files = [p for p in files if not _is_record(p)]
    return {"changed": changed, "state": state, "files": files[:MAX_FILES], "reference": reference, "title": title}


def _is_record(path: str) -> bool:
    """Playground's own records (.playground/…) aren't part of what a goal changed in the product."""
    return path.startswith(".playground/")


def read_file(project_dir: Path, relpath: str, reference: str, mode: str) -> tuple[str, str]:
    """('diff' | 'file', text) for one file, never outside the project."""
    paths = c.module("core.paths")
    path = paths.resolve_inside(project_dir, relpath) if paths else (project_dir / relpath)
    if mode == "diff" and reference:
        diff = _git(project_dir, "diff", "--no-color", "-U3", reference, "--", relpath)
        if diff:
            return "diff", _trim_header(diff)
        if not _git(project_dir, "ls-files", "--", relpath).strip():
            # New and not in git yet: every line is part of the change.
            current = current_lines(project_dir, relpath)
            if current is not None:
                return "diff", difflines.new_file_diff("\n".join(current))
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError as error:
        return "file", f"This file can’t be opened: {error.strerror or error}"
    if b"\x00" in data[:4096]:
        return "file", "This is a binary file, so there’s nothing to read here."
    words = data[:MAX_BYTES].decode("utf-8", errors="replace")
    if len(data) > MAX_BYTES:
        words += "\n… (the rest of this large file isn’t shown)"
    return "file", words


def current_lines(project_dir: Path, relpath: str) -> list[str] | None:
    """The file as it is now, line by line, for filling in unchanged lines; None when it can't be shown."""
    paths = c.module("core.paths")
    try:
        path = paths.resolve_inside(project_dir, relpath) if paths else (project_dir / relpath)
        with open(path, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(data) > MAX_BYTES or b"\x00" in data[:4096]:
        return None
    text_now = data.decode("utf-8", errors="replace")
    return text_now[:-1].split("\n") if text_now.endswith("\n") else (text_now.split("\n") if text_now else [])


def read_change(project_dir: Path, relpath: str, reference: str, mode: str) -> tuple[str, str, list[str] | None]:
    """read_file, plus the file as it is now when the answer is a diff (for the unchanged lines)."""
    kind, words = read_file(project_dir, relpath, reference, mode)
    return kind, words, current_lines(project_dir, relpath) if kind == "diff" else None


def _trim_header(diff: str) -> str:
    """Hunks only: the diff --git, index and ---/+++ lines say nothing a person needs."""
    keep = []
    for line in diff.splitlines():
        if line.startswith(("diff --git", "index ", "--- ", "+++ ", "new file mode", "deleted file mode",
                            "similarity index", "rename from", "rename to")):
            continue
        keep.append(line)
    return "\n".join(keep) + "\n"


class CodePage:
    def __init__(self, window: Gtk.Window, options: dict) -> None:
        self.window = window
        self.ctx = c.context_for(window, options)
        self.data: dict | None = None
        self.file: str | None = options.get("file") if isinstance(options.get("file"), str) else None
        self.mode = "diff"
        self.terminal_open = options.get("terminal", "open") != "closed"
        self.terminal = None
        back = options.get("back_label") or c.goal_title(self.ctx) or "Back"
        path = text(self.ctx.display_path, "pgs-row-sub", wrap=False, ellipsize=True, selectable=True)
        self.sheet = c.Sheet(window, "Code", "", size="page", name="code", back_label=back, header_end=path)
        self.sheet.on_layout.append(lambda phone, _compact: (self.files_column.set_visible(not phone),
                                                             self.picker.set_visible(phone),
                                                             self.src_name.set_visible(not phone)))
        self.sheet.listener.on("files.changed", lambda **_: self.load())

        self.files = c.vbox(spacing=1)
        files_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        files_scroll.set_child(self.files)
        self.files_column = c.vbox(files_scroll, css="pgs-files", width_request=260, hexpand=False)

        self.src_name = text("", "pgs-src-name", wrap=False, ellipsize=True, hexpand=True)
        self.mode_switch = c.segmented([("diff", "Changes"), ("file", "Whole file")], "diff", label="Show",
                                       on_change=self.set_mode)
        self.picker = Gtk.DropDown.new_from_strings([])
        self.picker.set_visible(False)
        self.picker.update_property([Gtk.AccessibleProperty.LABEL], ["File"])
        self.picker.connect("notify::selected", self.picked)
        head = c.hbox(c.glyph("file-text", 16), self.src_name, self.picker, self.mode_switch, spacing=10,
                      css="pgs-src-head")
        # Said when "Changes" has nothing to compare against, rather than quietly showing the whole file.
        self.src_note = text("", "pgs-src-note", wrap=True)
        self.src_note.set_visible(False)
        self.view = self.make_view()
        src_scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        src_scroll.set_child(self.view)
        # Changes: numbered lines with "N unchanged lines" between them (v73), not git's @@ headers.
        self.differ = DiffBox(self.sheet.on_close)
        self.diff_box = self.differ.box
        self.diff_scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.diff_scroll.set_child(self.diff_box)
        self.src_stack = Gtk.Stack(vexpand=True, hexpand=True)
        self.src_stack.add_named(src_scroll, "file")
        self.src_stack.add_named(self.diff_scroll, "diff")
        self.source = c.vbox(head, self.src_note, self.src_stack, css="pgs-src", hexpand=True, vexpand=True)

        self.term_box = c.vbox(css="pgs-term")
        right = c.vbox(self.source, self.term_box, hexpand=True)
        layout = c.hbox(self.files_column, right, spacing=0, vexpand=True)
        self.sheet.set_body(layout, scroll=False)
        self.build_terminal()
        self.sheet.present()
        self.load()

    # ── the source view ──

    def make_view(self) -> Gtk.Widget:
        if GtkSource is not None:
            buffer = GtkSource.Buffer()
            view = GtkSource.View(buffer=buffer, editable=False, monospace=True, show_line_numbers=True,
                                  top_margin=10, bottom_margin=40, left_margin=6, wrap_mode=Gtk.WrapMode.NONE,
                                  cursor_visible=False)
            view.update_property([Gtk.AccessibleProperty.LABEL], ["The file’s text"])
            self.sync_scheme(buffer)
            self.sheet.on_close.append(c.on_appearance(lambda: self.sync_scheme(buffer)))
            return view
        view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False, top_margin=10, left_margin=16)
        view.add_css_class("pgs-src-plain")
        return view

    def sync_scheme(self, buffer) -> None:
        schemes = GtkSource.StyleSchemeManager.get_default()
        dark = c.is_dark()
        for name in (("Adwaita-dark", "classic-dark", "oblivion") if dark else ("Adwaita", "classic")):
            scheme = schemes.get_scheme(name)
            if scheme is not None:
                buffer.set_style_scheme(scheme)
                return

    def show_text(self, kind: str, words: str, filename: str, current: list[str] | None = None) -> None:
        if kind == "diff":
            self.differ.show(words, current, filename)
            self.src_stack.set_visible_child_name("diff")
            return
        self.differ.forget()
        self.src_stack.set_visible_child_name("file")
        buffer = self.view.get_buffer()
        buffer.set_text(words)
        if GtkSource is not None:
            languages = GtkSource.LanguageManager.get_default()
            language = languages.get_language("diff") if kind == "diff" else \
                languages.guess_language(filename, None)
            buffer.set_language(language)
            buffer.set_highlight_syntax(language is not None)
        buffer.place_cursor(buffer.get_start_iter())
        self.view.scroll_to_iter(buffer.get_start_iter(), 0, False, 0, 0)

    # ── data ──

    def load(self) -> None:
        if self.ctx.project_dir is None:
            self.show_text("file", "Open a project to see its files.", "")
            return
        project_dir, goal_id = self.ctx.project_dir, self.ctx.goal_id
        c.run_async(lambda: read_changes(project_dir, goal_id), self.loaded,
                    lambda error: self.show_text("file", f"The files couldn’t be read: {error}", ""))

    def loaded(self, data: dict) -> None:
        self.data = data
        if self.file is None or (self.file not in data["files"] and self.file not in data["changed"]):
            self.file = (data["changed"] or [p for p in data["files"] if p.endswith("AGENTS.md")] or
                         data["files"] or [None])[0]
        self.fill_files()
        self.open(self.file)

    def fill_files(self) -> None:
        c.clear(self.files)
        data = self.data or {}
        changed = data.get("changed") or []
        state = data.get("state") or {}
        if changed:
            title = data.get("title")
            self.files.append(text(f"Changed in {title}" if title else "Changed", "pgs-files-head"))
            for path in changed:
                self.files.append(self.file_row(path, Path(path).name, state.get(path, "M")))
        name = self.ctx.name or "Project"
        self.files.append(text(name, "pgs-files-head"))
        for path in data.get("files") or []:
            if path in changed:
                continue
            self.files.append(self.file_row(path, path, ""))
        names = changed + [p for p in data.get("files") or [] if p not in changed]
        self._filling = True
        self._picker_paths = names
        self.picker.set_model(Gtk.StringList.new(names))
        if self.file in names:
            self.picker.set_selected(names.index(self.file))
        self._filling = False

    def file_row(self, path: str, label: str, state: str) -> Gtk.Widget:
        button = Gtk.Button(tooltip_text=path)
        button.add_css_class("pgs-file")
        if path == self.file:
            button.add_css_class("on")
        icon = "settings-2" if label.startswith(".") or path.startswith(".") else "file-text"
        line = c.hbox(c.glyph(icon, 14), text(label, wrap=False, ellipsize=True, hexpand=True), spacing=8)
        if state:
            badge = text(state, "pgs-file-state", wrap=False)
            badge.add_css_class({"A": "added", "D": "deleted"}.get(state, "modified"))
            badge.set_tooltip_text({"A": "Added", "D": "Removed", "M": "Changed", "R": "Renamed"}.get(state, "Changed"))
            line.append(badge)
        button.set_child(line)
        words = {"A": "added", "D": "removed", "M": "changed"}.get(state, "")
        button.update_property([Gtk.AccessibleProperty.LABEL], [f"{path}{', ' + words if words else ''}"])
        button.connect("clicked", lambda _b: self.open(path))
        button.path = path
        return button

    def open(self, path: str | None) -> None:
        self.file = path
        child = self.files.get_first_child()
        while child is not None:
            if hasattr(child, "path"):
                (child.add_css_class if child.path == path else child.remove_css_class)("on")
            child = child.get_next_sibling()
        if path is None or self.ctx.project_dir is None:
            self.src_name.set_label("")
            self.show_text("file", "This project has no files yet.", "")
            return
        data = self.data or {}
        is_changed = path in (data.get("changed") or [])
        has_diff = is_changed and bool((data.get("state") or {}).get(path))
        self.mode_switch.set_visible(has_diff and bool(data.get("reference")))
        mode = self.mode if has_diff else "file"
        self.src_name.set_label(path)
        if is_changed and not has_diff:
            self.src_note.set_label(f"{c.goal_title(self.ctx) or 'This goal'} changed this file, and the change is "
                                    "already saved in the project’s history, so here is the whole file as it is now.")
        self.src_note.set_visible(is_changed and not has_diff)
        project_dir, reference = self.ctx.project_dir, data.get("reference") or ""
        c.run_async(lambda: read_change(project_dir, path, reference, mode),
                    lambda result: self.file == path and self.show_text(result[0], result[1], path, result[2]),
                    lambda error: self.show_text("file", str(error), path))

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.open(self.file)

    def picked(self, dropdown, _param) -> None:
        if getattr(self, "_filling", False):
            return
        index = dropdown.get_selected()
        paths = getattr(self, "_picker_paths", [])
        if 0 <= index < len(paths) and paths[index] != self.file:
            self.open(paths[index])

    # ── the terminal ──

    def build_terminal(self) -> None:
        c.clear(self.term_box)
        where = "on this computer"
        project = self.ctx.project
        if project is not None and project.home not in ("", "this-computer"):
            place = project.place(project.home)
            where = f"on this computer · {place.name} has the running app" if place else where
        toggle = c.icon_button("chevron-down" if self.terminal_open else "chevron-up",
                               "Hide the terminal" if self.terminal_open else "Show the terminal",
                               on_click=self.toggle_terminal, size="small")
        head = c.hbox(c.glyph("terminal", 14), text("Terminal", "pgs-term-title", wrap=False),
                      text(f"{where} · {self.ctx.display_path}", wrap=False, ellipsize=True, hexpand=True),
                      toggle, spacing=10, css="pgs-term-head")
        self.term_box.append(head)
        if not self.terminal_open:
            return
        if self.terminal is None:
            self.terminal = self.make_terminal()
        if self.terminal.get_parent() is None:
            self.term_box.append(self.terminal)

    def make_terminal(self) -> Gtk.Widget:
        try:
            import gi
            gi.require_version("Vte", "3.91")
            from gi.repository import Vte
        except (ImportError, ValueError):
            return c.vbox(text("The terminal needs VTE for GTK 4 (the vte291-gtk4 package). Everything else here "
                               "works without it.", "pgs-term-fallback"), css="pgs-term-body")
        terminal = Vte.Terminal(hexpand=True, vexpand=False)
        terminal.set_size(100, 10)
        terminal.add_css_class("pgs-term-body")
        terminal.update_property([Gtk.AccessibleProperty.LABEL], ["Terminal in the project folder"])
        terminal.set_font(Pango.FontDescription.from_string(f"{MONO_FAMILIES} 10.5"))
        terminal.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
        self.style_terminal(terminal)
        shell = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
        cwd = str(self.ctx.project_dir) if self.ctx.project_dir else os.path.expanduser("~")
        terminal.spawn_async(Vte.PtyFlags.DEFAULT, cwd, [shell], None, GLib.SpawnFlags.DEFAULT, None, None, -1,
                             None, None)
        terminal.connect("child-exited", lambda *_a: self.terminal_ended())
        return terminal

    def style_terminal(self, terminal) -> None:
        from gi.repository import Gdk

        def rgba(value: str) -> Gdk.RGBA:
            colour = Gdk.RGBA()
            colour.parse(value)
            return colour

        dark = c.is_dark()
        terminal.set_colors(rgba("#d6d7db"), rgba("#0c0d10" if dark else "#14161a"), [])

    def terminal_ended(self) -> None:
        if self.terminal is not None and self.terminal.get_parent() is not None:
            self.term_box.remove(self.terminal)
        self.terminal = None
        if not self.sheet.closed and self.terminal_open:
            self.build_terminal()

    def toggle_terminal(self) -> None:
        self.terminal_open = not self.terminal_open
        if self.terminal is not None and self.terminal.get_parent() is not None:
            self.term_box.remove(self.terminal)
        self.build_terminal()
        if self.terminal_open and self.terminal is not None:
            self.terminal.grab_focus()


# Green and red that read on light and dark alike.
SIGN_MARKUP = {"add": '<span foreground="#2ea043" weight="bold">+</span>',
               "del": '<span foreground="#e5534b" weight="bold">−</span>'}


class DiffBox:
    """A change, v73 style: numbered lines, "N unchanged lines" between them (not git's @@ headers), the
    file's own highlighting. The code sheet shows one; the Repository area shows one under a changed file."""

    def __init__(self, on_close: list) -> None:
        self.on_close = on_close
        self.box = c.vbox(css="pgs-diff")
        self._diff: tuple[str, list[str] | None, str] | None = None
        self._expanded: set[int] = set()

    def show(self, diff: str, current: list[str] | None, filename: str) -> None:
        if (self._diff or ("", None, ""))[2] != filename:
            self._expanded = set()
        self._diff = (diff, current, filename)
        self.draw_diff()

    def forget(self) -> None:
        self._diff = None


    def draw_diff(self) -> None:
        c.clear(self.box)
        if self._diff is None:
            return
        diff, current, filename = self._diff
        segments = difflines.layout(diff, current, self._expanded)
        if not segments:
            self.box.append(text("Nothing changed in this file.", "pgs-src-note"))
            return
        for part in segments:
            if isinstance(part, difflines.Gap):
                self.box.append(self.gap_row(part))
            else:
                self.box.append(self.segment_view(part.lines, filename))

    def gap_row(self, gap: difflines.Gap) -> Gtk.Widget:
        label = text(gap.words, "pgs-diff-gap-words", wrap=False, hexpand=True)
        if not gap.expandable:
            row = c.hbox(label, css="pgs-diff-gap")
            return row
        button = Gtk.Button(tooltip_text="Show them")
        button.add_css_class("pgs-diff-gap")
        button.set_child(c.hbox(label, text("Show", "pgs-diff-gap-show", wrap=False), spacing=6))
        button.update_property([Gtk.AccessibleProperty.LABEL], [f"{gap.words}. Show them"])

        def expand(_b) -> None:
            self._expanded.add(gap.key)
            self.draw_diff()

        button.connect("clicked", expand)
        return button

    def segment_view(self, lines: list, filename: str) -> Gtk.Widget:
        """One run of shown lines: the file's own highlighting, numbers and +/− in the gutter,
        added and removed lines on a tint."""
        words = "\n".join(line.text for line in lines)
        numbers = ["" if line.new is None else str(line.new) for line in lines]
        signs = [{"add": "+", "del": "−"}.get(line.kind, "") for line in lines]
        marks = [SIGN_MARKUP.get(line.kind, "") for line in lines]
        if GtkSource is None:
            width = max(len(n) for n in numbers) if numbers else 1
            plain = "\n".join(f"{n:>{width}} {s or ' '} {line.text}" for n, s, line in zip(numbers, signs, lines))
            view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False, left_margin=12)
            view.add_css_class("pgs-src-plain")
            view.get_buffer().set_text(plain)
            return view
        buffer = GtkSource.Buffer()
        buffer.set_text(words)
        language = GtkSource.LanguageManager.get_default().guess_language(filename, None)
        buffer.set_language(language)
        buffer.set_highlight_syntax(language is not None)
        _sync_scheme(buffer)
        from gi.repository import Gdk
        tints = {}
        for kind, colour in (("add", "rgba(46,160,67,0.16)"), ("del", "rgba(248,81,73,0.15)")):
            rgba = Gdk.RGBA()
            rgba.parse(colour)
            tints[kind] = buffer.create_tag(None, paragraph_background_rgba=rgba)
        for index, line in enumerate(lines):
            tag = tints.get(line.kind)
            if tag is not None:
                start = buffer.get_iter_at_line(index)[1]
                end = start.copy()
                end.forward_to_line_end()
                buffer.apply_tag(tag, start, end)
        view = GtkSource.View(buffer=buffer, editable=False, monospace=True, show_line_numbers=False,
                              wrap_mode=Gtk.WrapMode.NONE, cursor_visible=False, left_margin=8,
                              top_margin=4, bottom_margin=4, hexpand=True)
        view.add_css_class("pgs-diff-lines")
        view.update_property([Gtk.AccessibleProperty.LABEL], ["Changed lines"])
        digits = max((len(n) for n in numbers), default=1)
        gutter = view.get_gutter(Gtk.TextWindowType.LEFT)
        gutter.insert(_column(numbers, 14 + 8 * max(digits, 2), 1.0, "pgs-diff-number"), 0)
        gutter.insert(_column(marks, 22, 0.5, "pgs-diff-sign", markup=True), 1)
        self.on_close.append(c.on_appearance(lambda: _sync_scheme(buffer)))
        return view



def _sync_scheme(buffer) -> None:
    schemes = GtkSource.StyleSchemeManager.get_default()
    dark = c.is_dark()
    for name in (("Adwaita-dark", "classic-dark", "oblivion") if dark else ("Adwaita", "classic")):
        scheme = schemes.get_scheme(name)
        if scheme is not None:
            buffer.set_style_scheme(scheme)
            return


def _column(texts: list[str], width: int, xalign: float, css: str, markup: bool = False):
    """A gutter column showing one given string per line (line numbers as they are now, or + and −)."""
    renderer = _Column(texts, markup)
    renderer.set_size_request(width, -1)
    renderer.set_xalign(xalign)
    renderer.set_xpad(6)
    renderer.add_css_class(css)
    return renderer


if GtkSource is not None:
    class _Column(GtkSource.GutterRendererText):
        __gtype_name__ = "PlaygroundDiffColumn"

        def __init__(self, texts: list[str], markup: bool = False) -> None:
            super().__init__()
            self.texts = texts
            self.markup = markup

        def do_query_data(self, _lines, line: int) -> None:
            value = self.texts[line] if 0 <= line < len(self.texts) else ""
            (self.set_markup if self.markup else self.set_text)(value, -1)
