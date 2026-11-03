# SPDX-License-Identifier: Apache-2.0
"""What every Playground sheet shares: the sheet itself, its parts, and the plumbing.

A sheet hangs from the window's title bar through LumaUI's sheet layer
(``AppWindow.present_sheet``), dims only this window, and closes with Esc, the
close button or a click on the dimmed window. At phone width it becomes a
drawer that fills the screen under the title island. A *page* (the Project
book, the code, a review) takes the whole work area instead of floating, with
Back naming where it returns to.

The parts are small and plain: a section heading, a rounded list of rows, a
tag, a meter, a callout, an agent's mark. They read the kit's tokens through
``sheets.css`` so light, dark and high contrast all follow the system.

Nothing here blocks the main loop: ``run_async`` runs work on a thread and
hands the result back on the main loop, and ``Listener`` marshals event-bus
callbacks (which arrive on the publisher's thread) through ``GLib.idle_add``.

Data comes only from ``core`` and ``agents`` (through ``module()``, so a part
that hasn't landed yet leaves an honest empty state rather than a crash), and
never from anything typed in here.
"""
from __future__ import annotations

import importlib
import logging
import math
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gtk, Pango, PangoCairo  # noqa: E402

from .. import widgets as shared  # noqa: E402  the app's marks, faces, meters and state glyphs

log = logging.getLogger(__name__)

try:  # LumaUI's Python kit. Playground runs without it on a bare GTK (the Mac harness).
    import luma_appkit as kit  # noqa: E402
except Exception:  # noqa: BLE001
    kit = None

PHONE_MAX = 560          # v71 tier: below this the window is a phone
COMPACT_MAX = 900        # v71 tier: 560 to 900 is compact
SHEET_WIDTH = 580        # v73 .pgsheet
WIDE_SHEET_WIDTH = 720   # new project, the picker as a sheet
COMPACT_PAGE_MAX = 1059  # v73 @container win (max-width: 1060px)

# ── Core and agents, looked up when they're needed ───────────────────────────


def module(path: str):
    """``module("core.store")`` → the module, or None while that part isn't installed."""
    try:
        return importlib.import_module(f"luma_playground.{path}")
    except ImportError as error:
        log.debug("%s is not available: %s", path, error)
        return None


def call(target: Any, names: str | Sequence[str], *args, default: Any = None, **kwargs) -> Any:
    """Call the first of ``names`` that ``target`` has. Contract drift is logged, never raised.

    Used where the contract names a capability but not its spelling, so a sheet
    keeps working while the module beside it settles. Errors inside the call
    are the caller's to see: they propagate.
    """
    if target is None:
        return default
    for name in ([names] if isinstance(names, str) else names):
        fn = getattr(target, name, None)
        if callable(fn):
            return fn(*args, **kwargs)
    log.debug("%r has none of %s", target, names)
    return default


def get(obj: Any, name: str, default: Any = None) -> Any:
    """An attribute of a dataclass, or a key of a dict: the two shapes the core hands back."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ── Off the main loop ───────────────────────────────────────────────────────


def run_async(work: Callable[[], Any], done: Callable[[Any], None] | None = None,
              failed: Callable[[BaseException], None] | None = None) -> threading.Thread:
    """Run ``work`` on a thread; ``done(result)`` or ``failed(error)`` runs on the main loop (at default
    priority, so an animation being drawn frame after frame never holds the answer back)."""

    def runner() -> None:
        try:
            result = work()
        except BaseException as error:  # noqa: BLE001 - handed to the UI
            log.warning("background work failed: %s", error, exc_info=True)
            if failed is not None:
                GLib.idle_add(lambda problem=error: (failed(problem), False)[1], priority=GLib.PRIORITY_DEFAULT)
            return
        if done is not None:
            GLib.idle_add(lambda value=result: (done(value), False)[1], priority=GLib.PRIORITY_DEFAULT)

    thread = threading.Thread(target=runner, name="playground-sheet", daemon=True)
    thread.start()
    return thread


class Listener:
    """Event-bus subscriptions owned by one sheet, delivered on the main loop, all dropped at once."""

    def __init__(self) -> None:
        self._tokens: list[int] = []
        events = module("core.events")
        self._bus = getattr(events, "bus", None)

    def on(self, topic: str, callback: Callable[..., None]) -> None:
        if self._bus is None:
            return

        def deliver(**payload) -> None:
            GLib.idle_add(lambda: (callback(**payload), False)[1], priority=GLib.PRIORITY_DEFAULT)

        self._tokens.append(self._bus.subscribe(topic, deliver))

    def close(self) -> None:
        if self._bus is not None:
            for token in self._tokens:
                self._bus.unsubscribe(token)
        self._tokens.clear()


# ── Which project, which goal ───────────────────────────────────────────────


@dataclass
class Context:
    """What a sheet is about: the window, the open project and the selected goal."""

    window: Gtk.Window
    project_dir: Path | None = None
    goal_id: str | None = None
    options: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        project = self.project
        return str(get(project, "name") or (self.project_dir.name if self.project_dir else ""))

    @property
    def project(self) -> Any:
        """The core.model.Project, read once per context (cheap: a small TOML file)."""
        if "_project" not in self.__dict__:
            self.__dict__["_project"] = None
            if self.project_dir is not None:
                project = module("core.project")
                store = module("core.store")
                try:
                    self.__dict__["_project"] = (
                        call(project, ("open_project", "load_project", "read_project"), self.project_dir)
                        or call(store, ("read_project", "load_project"), self.project_dir))
                except Exception as error:  # noqa: BLE001 - shown as an empty project
                    log.warning("could not read %s: %s", self.project_dir, error)
        return self.__dict__["_project"]

    @property
    def display_path(self) -> str:
        if self.project_dir is None:
            return ""
        return home_relative(self.project_dir)


def goal_title(ctx: "Context") -> str:
    """The selected goal's title, for Back buttons that name where they return to."""
    if not (ctx.goal_id and ctx.project_dir):
        return ""
    store = module("core.store")
    try:
        return store.ProjectStore(ctx.project_dir).read_goal(ctx.goal_id).title if store else ""
    except Exception:  # noqa: BLE001 - a goal that's gone just means a plain Back
        return ""


def home_relative(path: Path | str) -> str:
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def context_for(window: Gtk.Window, options: dict) -> Context:
    """The project and goal the sheet is about: from the options, else from the window."""
    project = options.get("project") or options.get("project_dir")
    if project is None:
        for name in ("project_dir", "project_path", "current_project_dir"):
            value = getattr(window, name, None)
            if value:
                project = value
                break
        if project is None:
            value = getattr(window, "project", None)
            project = get(value, "path") or get(value, "root") or (value if isinstance(value, (str, Path)) else None)
    goal = options.get("goal") or options.get("goal_id")
    if goal is None and options.get("goal") != "":      # goal="" means the whole project, no conversation
        for name in ("goal_id", "current_goal_id", "selected_goal_id"):
            value = getattr(window, name, None)
            if value:
                goal = value
                break
    return Context(window=window, project_dir=Path(project).expanduser() if project else None,
                   goal_id=str(goal) if goal else None, options=dict(options))


# ── Glyphs, words and small parts ───────────────────────────────────────────

# Lucide names the sheets use that LumaUI doesn't ship, mapped to the nearest glyph it does.
_GLYPH_STANDINS = {
    "laptop": "monitor", "radio": "radio-tower", "arrow-right": "go-next", "log-in": "key-round",
    "tablet": "smartphone", "coins": "wallet", "circle-dashed": "square-dashed", "loader": "refresh-cw",
    "sprout": "leaf", "lightbulb": "sparkle", "bot": "sparkles", "git-compare": "git-branch",
    "file-diff": "file-text", "file-code": "code", "plug": "zap", "user-x": "user-minus",
    "lock-keyhole": "lock", "file": "file-text", "vault": "shield", "cert": "badge-check",
    "restore": "rotate-ccw", "handoff": "forward", "checker": "shield-check", "plan": "list-checks",
    "route": "navigation", "layout-dashboard": "layout-grid", "hard-drive": "drive-harddisk",
    "credit-card": "wallet", "component": "layers", "package": "archive", "chart-column": "bar-chart-3",
}


def _has_icon(name: str) -> bool:
    display = Gdk.Display.get_default()
    return display is not None and Gtk.IconTheme.get_for_display(display).has_icon(name)


def glyph_name(name: str) -> str:
    """The app's own copy of a Lucide glyph (data/icons) first, then LumaUI's, then a stand-in."""
    own = f"pg-{name}-symbolic"
    if _has_icon(own):
        return own
    lumaui = kit.lumaui_icon(name) if kit is not None else f"lumaui-{name}-symbolic"
    if _has_icon(lumaui) or name not in _GLYPH_STANDINS:
        return lumaui
    return glyph_name(_GLYPH_STANDINS[name])


def glyph(name: str, size: int = 16, *classes: str) -> Gtk.Image:
    image = Gtk.Image(icon_name=glyph_name(name), pixel_size=size, accessible_role=Gtk.AccessibleRole.PRESENTATION)
    for css in classes:
        image.add_css_class(css)
    return image


def text(words: str, *classes: str, wrap: bool = True, xalign: float = 0.0, markup: bool = False,
         selectable: bool = False, lines: int = 0, ellipsize: bool = False, **props) -> Gtk.Label:
    label = Gtk.Label(xalign=xalign, selectable=selectable, **props)
    if markup:
        label.set_markup(words)
    else:
        label.set_label(words)
    if wrap and not ellipsize:
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_natural_wrap_mode(Gtk.NaturalWrapMode.WORD)
    if ellipsize:
        label.set_ellipsize(Pango.EllipsizeMode.END)
    if lines:
        label.set_lines(lines)
        label.set_ellipsize(Pango.EllipsizeMode.END)
    for css in classes:
        label.add_css_class(css)
    return label


def escape(words: Any) -> str:
    return GLib.markup_escape_text(str(words or ""))


def hbox(*children: Gtk.Widget | None, spacing: int = 8, css: str | None = None, **props) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing, **props)
    if css:
        for name in css.split():
            box.add_css_class(name)
    for child in children:
        if child is not None:
            box.append(child)
    return box


def vbox(*children: Gtk.Widget | None, spacing: int = 0, css: str | None = None, **props) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **props)
    if css:
        for name in css.split():
            box.add_css_class(name)
    for child in children:
        if child is not None:
            box.append(child)
    return box


def clear(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        if isinstance(box, Gtk.Box):
            box.remove(child)
        else:
            child.unparent()
        child = following


def button(label: str, *, icon: str | None = None, style: str = "fill", small: bool = False,
           on_click: Callable[[], None] | None = None, sensitive: bool = True, tooltip: str | None = None) -> Gtk.Button:
    """The kit's text button (v70 .bt): plain, fill, key (the one main action) or danger."""
    if kit is not None:
        # The kit draws its own glyph by Lucide name; when LumaUI doesn't ship that one, put ours in its place.
        resolved = glyph_name(icon) if icon else ""
        lucide = resolved[len("lumaui-"):-len("-symbolic")] if resolved.startswith("lumaui-") else None
        widget = kit.TextButton(label, icon=lucide, style=style, small=small, on_click=on_click, sensitive=sensitive)
        if icon and lucide is None:
            widget.set_child(None)
            line = Gtk.Box(halign=Gtk.Align.CENTER)
            line.add_css_class("lumaui-text-button-content")
            line.append(glyph(icon, 16))
            line.append(widget.label_widget)
            widget.set_child(line)
    else:
        widget = Gtk.Button(label=label, sensitive=sensitive, valign=Gtk.Align.CENTER)
        if on_click is not None:
            widget.connect("clicked", lambda _b: on_click())
    widget.add_css_class("pgs-button")
    if tooltip:
        widget.set_tooltip_text(tooltip)
    return widget


def icon_button(icon: str, label: str, *, on_click: Callable[[], None] | None = None, size: str = "regular") -> Gtk.Button:
    if kit is not None:
        resolved = glyph_name(icon)
        widget = kit.IconOnlyButton(resolved[len("lumaui-"):-len("-symbolic")] if resolved.startswith("lumaui-")
                                    else icon, label, on_click=on_click, size=size)
        if not resolved.startswith("lumaui-"):
            widget.set_child(glyph(icon, 18))
        return widget
    widget = Gtk.Button(icon_name=glyph_name(icon), tooltip_text=label, valign=Gtk.Align.CENTER)
    widget.add_css_class("flat")
    widget.update_property([Gtk.AccessibleProperty.LABEL], [label])
    if on_click is not None:
        widget.connect("clicked", lambda _b: on_click())
    return widget


def switch(active: bool, *, label: str, on_toggle: Callable[[bool], None] | None = None,
           sensitive: bool = True) -> Gtk.Switch:
    if kit is not None:
        widget = kit.Switch(active=active, on_toggle=on_toggle, label=label)
    else:
        widget = Gtk.Switch(active=active, valign=Gtk.Align.CENTER)
        widget.update_property([Gtk.AccessibleProperty.LABEL], [label])
        if on_toggle is not None:
            widget.connect("notify::active", lambda s, _p: on_toggle(s.get_active()))
    widget.set_sensitive(sensitive)
    widget.set_valign(Gtk.Align.CENTER)
    return widget


def segmented(modes: Sequence[tuple[str, str]], current: str, *, label: str,
              on_change: Callable[[str], None] | None = None, small: bool = True, fill: bool = False) -> Gtk.Widget:
    """Text-only segments (v70 .seg) from the kit's ModeSwitch; a linked toggle group without it."""
    if kit is not None:
        widget = kit.ModeSwitch(list(modes), current=current, on_change=on_change, label=label,
                                labels_only=True, size="small" if small else "regular", fill=fill)
        widget.set_valign(Gtk.Align.CENTER)
        return widget
    box = hbox(spacing=0, css="linked")
    first = None
    for key, words in modes:
        toggle = Gtk.ToggleButton(label=words, active=key == current)
        if first is None:
            first = toggle
        else:
            toggle.set_group(first)
        toggle.connect("toggled", lambda t, k=key: t.get_active() and on_change and on_change(k))
        box.append(toggle)
    return box


def avatar(name: str, *, size: str = "compact") -> Gtk.Widget:
    """A person, by name: the kit's avatar with the same tone in every Luma app."""
    if kit is not None:
        return kit.Avatar(name or "?", compact=size == "compact", medium=size == "medium", large=size == "large")
    initials = "".join(part[:1] for part in (name or "?").split()[:2]).upper()
    label = Gtk.Label(label=initials or "?")
    label.add_css_class("pgs-avatar-fallback")
    return label


def toast(where: Gtk.Widget, message: str, kind: str = "done", undo: Callable[[], None] | None = None) -> None:
    if kit is not None:
        try:
            kit.Toast.show(where, message, kind=kind, undo=undo)
            return
        except Exception:  # noqa: BLE001 - a toast is never worth a crash
            log.debug("toast failed", exc_info=True)
    log.info("%s", message)


class Tag(Gtk.Label):
    """A short status word on a soft pill (v73 .pgtag): neutral, good, amber, violet, run or red."""

    def __init__(self, words: str, tone: str | None = None) -> None:
        super().__init__(label=words, valign=Gtk.Align.CENTER, halign=Gtk.Align.START)
        self.add_css_class("pgs-tag")
        if tone:
            self.add_css_class(tone)


def meter(fraction: float | None, *, tone: str | None = None, label: str = "", width: int = -1,
          height: int = 6) -> Gtk.Widget:
    """The app's thin meter (v73 .pgmeter), shared with the cards so headroom reads the same everywhere."""
    widget = shared.Meter(fraction or 0.0, width=width, height=height, accessible=label)
    widget.set_hexpand(width < 0)
    if tone:
        widget.add_css_class(tone)
    return widget


def mark(connection: Any, *, size: int = 28, teammate: str | None = None) -> Gtk.Widget:
    """An agent's tile (v73 .pgam), the same one the composer and the cards show.

    A teammate's carries their face in the corner, so "Priya's Claude" is never
    mistaken for yours.
    """
    data = connection if isinstance(connection, dict) else {
        "provider": get(connection, "provider"), "kind": get(connection, "kind"),
        "label": get(connection, "label") or get(connection, "name")}
    if data.get("kind") in ("team-key", "api-key") and data.get("provider") not in ("ari", "ollama", "local"):
        data = {**data, "provider": "team-key"}
    tile = shared.agent_mark(data, size)
    tile.set_hexpand(False)
    tile.set_vexpand(False)
    if not teammate:
        return tile
    overlay = Gtk.Overlay(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    overlay.set_child(tile)
    face = shared.person_face(teammate, max(14, size // 2))
    face.add_css_class("pgs-mark-face")
    face.set_halign(Gtk.Align.END)
    face.set_valign(Gtk.Align.END)
    face.set_can_target(False)
    overlay.add_overlay(face)
    overlay.set_clip_overlay(face, False)
    return overlay


def lead_tile(icon: str) -> Gtk.Widget:
    """The 34px rounded square a row leads with (v73 .pgrow > i)."""
    tile = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER, hexpand=False, vexpand=False)
    tile.add_css_class("pgs-lead")
    image = glyph(icon, 17)
    image.set_hexpand(True)
    image.set_halign(Gtk.Align.CENTER)
    tile.append(image)
    return tile


class Row(Gtk.Box):
    """One line of a list (v73 .pgrow): a lead, a title with tags, a quieter line, and an end."""

    def __init__(self, title: str, subtitle: str | None = None, *, lead: Gtk.Widget | str | None = None,
                 tags: Iterable[Tag] = (), end: Gtk.Widget | None = None, on_activate: Callable[[], None] | None = None,
                 dim: bool = False, markup: bool = False, chevron: bool = False, title_class: str | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                         accessible_role=Gtk.AccessibleRole.BUTTON if on_activate else Gtk.AccessibleRole.GROUP)
        self.add_css_class("pgs-row")
        if dim:
            self.add_css_class("dim")
        if isinstance(lead, str):
            lead = lead_tile(lead)
        if lead is not None:
            lead.set_valign(Gtk.Align.CENTER)
            self.append(lead)
        words = vbox(spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.title = text(escape(title) if markup is False else title, "pgs-row-title", markup=True)
        if title_class:
            self.title.add_css_class(title_class)
        heading = hbox(self.title, spacing=8)
        self.title.set_hexpand(False)
        for tag in tags:
            heading.append(tag)
        words.append(heading)
        self.subtitle = None
        if subtitle:
            self.subtitle = text(subtitle if markup else escape(subtitle), "pgs-row-sub", markup=True)
            words.append(self.subtitle)
        self.words = words
        self.append(words)
        self.end = end
        if end is not None:
            end.set_valign(Gtk.Align.CENTER)
            self.append(end)
        if chevron:
            self.append(glyph("chevron-right", 15, "pgs-chevron"))
        self.activatable = on_activate is not None
        if on_activate is not None:
            self.add_css_class("activatable")
            self.set_focusable(True)
            self.update_property([Gtk.AccessibleProperty.LABEL], [title if not markup else self.title.get_text()])
            if subtitle:
                self.update_property([Gtk.AccessibleProperty.DESCRIPTION], [self.subtitle.get_text()])
            click = Gtk.GestureClick()
            click.connect("released", lambda *_a: on_activate())
            self.add_controller(click)
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", lambda _c, key, _code, _state: (
                on_activate() or True) if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space) else False)
            self.add_controller(keys)
            self.set_cursor(Gdk.Cursor.new_from_name("pointer", None))


class ListGroup(Gtk.Box):
    """Rows on one rounded card (v73 .pglist), separated by a single hairline."""

    def __init__(self, *rows: Gtk.Widget, css: str | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("pgs-list")
        if css:
            for name in css.split():
                self.add_css_class(name)
        for row in rows:
            self.append(row)


def section(title: str, *, action: Gtk.Widget | None = None, note: str | None = None,
            first: bool = False) -> Gtk.Box:
    """A section heading (v73 .pgshb > h3), with a link button at its end and a quiet line under it."""
    box = vbox(css="pgs-section" + (" first" if first else ""))
    heading = hbox(text(title, "pgs-section-title", wrap=False, hexpand=True), spacing=8)
    if action is not None:
        heading.append(action)
    box.append(heading)
    if note:
        box.append(text(note, "pgs-section-note"))
    return box


def link_button(label: str, icon: str | None, on_click: Callable[[], None]) -> Gtk.Button:
    """A small quiet button beside a heading (v73 .pglnk)."""
    child = hbox(glyph(icon, 14) if icon else None, Gtk.Label(label=label), spacing=6)
    widget = Gtk.Button(child=child, valign=Gtk.Align.CENTER)
    widget.add_css_class("pgs-link")
    widget.update_property([Gtk.AccessibleProperty.LABEL], [label])
    widget.connect("clicked", lambda _b: on_click())
    return widget


def callout(markup: str, *, icon: str = "info", tone: str = "info") -> Gtk.Box:
    """A note with a glyph: ``rule`` (the one-line rule), ``warn`` (honest warning) or ``info``."""
    box = hbox(glyph(icon, 16, "pgs-callout-glyph"), spacing=10, css=f"pgs-callout {tone}")
    label = text(markup, "pgs-callout-text", markup=True, hexpand=True)
    box.append(label)
    return box


def card(*children: Gtk.Widget, css: str = "pgs-card") -> Gtk.Box:
    return vbox(*children, css=css)


def empty(title: str, body: str, icon: str = "folder") -> Gtk.Widget:
    """An empty state inside a sheet: centred, quiet, and honest about why."""
    return vbox(glyph(icon, 28, "pgs-empty-glyph"), text(title, "pgs-empty-title", xalign=0.5),
                text(body, "pgs-empty-body", xalign=0.5), spacing=6, css="pgs-empty",
                halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, vexpand=True)


def kv(pairs: Iterable[tuple[str, str]]) -> Gtk.Box:
    """Label and value in two columns (v73 .pgkv): the brief's What it is, Who it's for…"""
    grid = Gtk.Grid(column_spacing=12, row_spacing=0)
    grid.add_css_class("pgs-kv")
    for row, (key, value) in enumerate(pairs):
        k = text(key, "pgs-kv-key")
        k.set_size_request(112, -1)
        k.set_valign(Gtk.Align.START)
        v = text(value, "pgs-kv-value", hexpand=True)
        if row:
            k.add_css_class("ruled")
            v.add_css_class("ruled")
        grid.attach(k, 0, row, 1, 1)
        grid.attach(v, 1, row, 1, 1)
    return grid


# ── The sheet ────────────────────────────────────────────────────────────────


class Sheet:
    """A sheet: title, subtitle, a close button, pages that push and pop, and an optional foot.

    ``size`` is ``regular`` (580, v73 .pgsheet), ``wide`` (720) or ``page``
    (the whole work area: the Project book, code and review).
    """

    def __init__(self, window: Gtk.Window, title: str, subtitle: str = "", *, size: str = "regular",
                 name: str = "sheet", back_label: str | None = None, header_end: Gtk.Widget | None = None) -> None:
        self.window = window
        self.size = size
        self.name = name
        self.listener = Listener()
        self.on_close: list[Callable[[], None]] = []
        self._closed = False
        self._stack_pages: list[tuple[Gtk.Widget, str, str]] = []
        self._fallback_dialog = None

        # The whole area under the title bar while the sheet is up: the dim, and the card on it.
        # A breakpoint turns the card into the phone drawer below 560 px, whatever the window does.
        self.scrim = Gtk.Box(hexpand=True, vexpand=True)
        self.scrim.add_css_class("pgs-sheet-layer")
        self.layer = Adw.BreakpointBin(width_request=320, height_request=240, hexpand=True, vexpand=True,
                                       child=self.scrim)
        # A BreakpointBin applies only the last breakpoint that matches, so the narrower one goes last, and
        # "compact" (v73's 1060 px: side columns fold away) also holds on a phone.
        self._compact = False
        self.phone = False
        compact = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {COMPACT_PAGE_MAX}px"))
        compact.connect("apply", lambda *_a: self._set_compact(True))
        compact.connect("unapply", lambda *_a: self._set_compact(False))
        self.layer.add_breakpoint(compact)
        phone = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {PHONE_MAX - 1}px"))
        phone.connect("apply", lambda *_a: self._set_phone(True))
        phone.connect("unapply", lambda *_a: self._set_phone(False))
        self.layer.add_breakpoint(phone)
        if size == "page":
            self.scrim.add_css_class("page")
        self.on_layout: list[Callable[[bool, bool], None]] = []
        if size == "page":
            self.scrim.add_css_class("page")
        dim = Gtk.GestureClick()
        dim.connect("released", self._dim_clicked)
        self.scrim.add_controller(dim)
        # The clamp keeps the card at its width however long its words are.
        self.clamp = Adw.Clamp(maximum_size=SHEET_WIDTH if size == "regular" else WIDE_SHEET_WIDTH,
                               tightening_threshold=SHEET_WIDTH, hexpand=True, vexpand=True,
                               orientation=Gtk.Orientation.HORIZONTAL)
        if size == "page":
            self.clamp.set_maximum_size(100000)

        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, accessible_role=Gtk.AccessibleRole.DIALOG)
        self.card.add_css_class("pgs-sheet")
        self.card.add_css_class(size)
        self.card.add_css_class(f"pgs-sheet-{name}")
        self.card.set_overflow(Gtk.Overflow.HIDDEN)
        # A click on the card itself must not count as a click on the dim behind it.
        swallow = Gtk.GestureClick(propagation_phase=Gtk.PropagationPhase.BUBBLE)
        swallow.connect("released", lambda g, *_a: g.set_state(Gtk.EventSequenceState.CLAIMED))
        self.card.add_controller(swallow)
        self.clamp.set_child(self.card)
        self.scrim.append(self.clamp)

        # Header: back, title and subtitle, the header's own end, close.
        self.header = hbox(spacing=10, css="pgs-sheet-head")
        self.back = icon_button("chevron-left", "Back", on_click=self.pop)
        self.back.add_css_class("pgs-back")
        self.back.set_visible(False)
        self.page_back: Gtk.Button | None = None
        if size == "page":
            # A page leads with Back naming where it returns to (v73 .pgback).
            self.page_back = button(back_label or "Back", icon="chevron-left", style="plain", on_click=self.close)
            self.page_back.add_css_class("pgs-page-back")
            self.header.append(self.page_back)
        self.header.append(self.back)
        titles = vbox(spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.title_label = text(title, "pgs-sheet-title", wrap=False, ellipsize=True)
        self.subtitle_label = text(subtitle, "pgs-sheet-subtitle", wrap=False, ellipsize=True)
        self.subtitle_label.set_visible(bool(subtitle))
        titles.append(self.title_label)
        titles.append(self.subtitle_label)
        if size == "page":
            titles.set_visible(False)
        self.titles = titles
        self.header.append(titles)
        if size == "page":
            # Keeps the header's own end (Show in Filer, the path) at the right edge while the titles hide.
            self.header.append(Gtk.Box(hexpand=True))
        if header_end is not None:
            self.header.append(header_end)
        self.close_button = icon_button("x", "Close", on_click=self.close)
        self.close_button.add_css_class("pgs-close")
        if size == "page":
            self.close_button.set_visible(False)
        self.header.append(self.close_button)
        self.card.append(self.header)
        self.card.update_property([Gtk.AccessibleProperty.LABEL], [title])

        self.stack = Gtk.Stack(vexpand=True, hhomogeneous=True, vhomogeneous=False,
                               transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT,
                               transition_duration=_motion(220))
        self.card.append(self.stack)
        self.foot = hbox(spacing=10, css="pgs-sheet-foot")
        self.foot.set_visible(False)
        self.card.append(self.foot)

        self.keys = Gtk.EventControllerKey()
        self.keys.connect("key-pressed", self._key)
        follow_dark(self.card)
        self._set_phone(False)

    # Building

    def scroller(self, child: Gtk.Widget, *, padded: bool = True) -> Gtk.ScrolledWindow:
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
                                    propagate_natural_height=True)
        scroll.add_css_class("pgs-sheet-scroll")
        if padded:
            child.add_css_class("pgs-sheet-body")
        scroll.set_child(child)
        return scroll

    def set_body(self, child: Gtk.Widget, *, scroll: bool = True, padded: bool = True) -> None:
        """The sheet's first page."""
        page = self.scroller(child, padded=padded) if scroll else child
        while self._stack_pages:
            widget, _t, _s = self._stack_pages.pop()
            self.stack.remove(widget)
        self.stack.add_child(page)
        self._stack_pages.append((page, self.title_label.get_label(), self.subtitle_label.get_label()))
        self.stack.set_visible_child(page)
        self._sync_header()

    def push(self, child: Gtk.Widget, title: str, subtitle: str = "", *, scroll: bool = True) -> None:
        """A page inside the sheet (Usage → one connection), with Back in the header."""
        page = self.scroller(child) if scroll else child
        self.stack.add_child(page)
        self._stack_pages.append((page, title, subtitle))
        self.stack.set_visible_child(page)
        self._sync_header()

    def replace_top(self, child: Gtk.Widget, title: str | None = None, subtitle: str | None = None) -> None:
        """Rebuild the page on top (after a change) without moving the person."""
        if not self._stack_pages:
            self.set_body(child)
            return
        old, old_title, old_subtitle = self._stack_pages.pop()
        adjustment = old.get_vadjustment() if isinstance(old, Gtk.ScrolledWindow) else None
        offset = adjustment.get_value() if adjustment is not None else 0
        page = self.scroller(child)
        self.stack.add_child(page)
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.stack.set_visible_child(page)
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.remove(old)
        self._stack_pages.append((page, title if title is not None else old_title,
                                  subtitle if subtitle is not None else old_subtitle))
        if offset:
            GLib.idle_add(lambda: (page.get_vadjustment().set_value(offset), False)[1])
        self._sync_header()

    def pop(self) -> None:
        if len(self._stack_pages) <= 1:
            self.close()
            return
        page, _t, _s = self._stack_pages.pop()
        self.stack.set_visible_child(self._stack_pages[-1][0])
        GLib.timeout_add(_motion(260), lambda: (self.stack.remove(page) if page.get_parent() else None, False)[1])
        self._sync_header()

    @property
    def depth(self) -> int:
        return len(self._stack_pages)

    def _sync_header(self) -> None:
        if not self._stack_pages:
            return
        _page, title, subtitle = self._stack_pages[-1]
        self.title_label.set_label(title)
        self.subtitle_label.set_label(subtitle)
        self.subtitle_label.set_visible(bool(subtitle))
        self.back.set_visible(len(self._stack_pages) > 1)
        if self.size == "page":
            self.titles.set_visible(len(self._stack_pages) > 1)
        self.card.update_property([Gtk.AccessibleProperty.LABEL], [title])

    def set_foot(self, *children: Gtk.Widget) -> None:
        clear(self.foot)
        for child in children:
            self.foot.append(child)
        self.foot.set_visible(bool(children))

    # Showing and closing

    def present(self) -> "Sheet":
        window = self.window
        show_page = getattr(window, "show_page", None)
        if self.size == "page" and callable(show_page):
            # The window puts a page in its work area, beside the rail (v73 "pages that take the room").
            self._page_host = True
            show_page(self.layer, on_back=self.close)
            _SHOWING[id(window)] = self
            GLib.idle_add(self._focus_first)
            return self
        if hasattr(window, "present_sheet"):
            window.present_sheet(self.layer, self.keys)
        else:
            # Not a LumaUI window (the Mac harness without the kit): the closest libadwaita has.
            dialog = Adw.Dialog(child=self.layer, title=self.title_label.get_label())
            dialog.set_content_width(SHEET_WIDTH if self.size == "regular" else 960)
            dialog.connect("closed", lambda *_a: self._finish())
            self._fallback_dialog = dialog
            dialog.present(window)
        _SHOWING[id(window)] = self
        GLib.idle_add(self._focus_first)
        return self

    def _focus_first(self) -> bool:
        target = _first_focusable(self.stack) or self.close_button
        if target is not None:
            target.grab_focus()
        return False

    def close(self) -> None:
        if self._closed:
            return
        if self._fallback_dialog is not None:
            self._fallback_dialog.close()
            return
        if getattr(self, "_page_host", False):
            hide_page = getattr(self.window, "hide_page", None)
            if callable(hide_page):
                hide_page(self.layer)
            self._finish()
            return
        if hasattr(self.window, "dismiss_sheet"):
            presented = getattr(self.window, "presented_sheet", None)
            if presented is None or presented is self.layer:
                self.window.dismiss_sheet()
        self._finish()

    def _finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.listener.close()
        if _SHOWING.get(id(self.window)) is self:
            _SHOWING.pop(id(self.window), None)
        for callback in self.on_close:
            try:
                callback()
            except Exception:  # noqa: BLE001
                log.exception("sheet close callback failed")

    @property
    def closed(self) -> bool:
        return self._closed

    def _key(self, _controller, keyval: int, _code: int, state: Gdk.ModifierType) -> bool:
        if keyval == Gdk.KEY_Escape:
            if self.depth > 1:
                self.pop()
            else:
                self.close()
            return True
        if keyval in (Gdk.KEY_Left,) and state & Gdk.ModifierType.ALT_MASK and self.depth > 1:
            self.pop()
            return True
        return False

    def _dim_clicked(self, gesture, _n, x: float, y: float) -> None:
        picked = self.scrim.pick(x, y, Gtk.PickFlags.DEFAULT)
        if picked in (self.scrim, self.clamp) and self.size != "page":
            self.close()

    # Layout

    @property
    def compact(self) -> bool:
        return self._compact or self.phone

    def _set_compact(self, compact: bool) -> None:
        self._compact = compact
        (self.card.add_css_class if self.compact else self.card.remove_css_class)("compact")
        for callback in list(self.on_layout):
            callback(self.phone, compact)

    def _set_phone(self, phone: bool) -> None:
        self.phone = phone
        (self.card.add_css_class if self.compact else self.card.remove_css_class)("compact")
        for callback in list(getattr(self, "on_layout", [])):
            callback(phone, self.compact)
        for widget in (self.scrim, self.card):
            (widget.add_css_class if phone else widget.remove_css_class)("phone")
        if self.size == "page":
            self.card.set_valign(Gtk.Align.FILL)
            return
        if phone:
            self.clamp.set_maximum_size(100000)
            self.clamp.set_valign(Gtk.Align.END)
            self.card.set_valign(Gtk.Align.END)
        else:
            self.clamp.set_maximum_size(SHEET_WIDTH if self.size == "regular" else WIDE_SHEET_WIDTH)
            self.clamp.set_valign(Gtk.Align.START)
            self.card.set_valign(Gtk.Align.START)


_SHOWING: dict[int, Sheet] = {}


_DARK_LISTENERS: list[Callable[[], None]] = []
_DARK_HOOKED = False


def is_dark() -> bool:
    """Whether the surface is dark: LumaUI's treatment (it owns the colours), else libadwaita's."""
    if kit is not None:
        try:
            from luma_appkit import widgets as kit_widgets
            return bool(kit_widgets.appearance.get("light_ink"))
        except Exception:  # noqa: BLE001
            pass
    return Adw.StyleManager.get_default().get_dark()


def on_appearance(callback: Callable[[], None]) -> Callable[[], None]:
    """Call ``callback`` whenever light, dark or the treatment changes. Returns the way to stop."""
    global _DARK_HOOKED
    if not _DARK_HOOKED:
        _DARK_HOOKED = True
        manager = Adw.StyleManager.get_default()
        manager.connect("notify::dark", lambda *_a: _appearance_changed())
        if kit is not None:
            try:
                # The kit rebuilds registered styles on every treatment change: a reliable "it changed".
                kit.add_style_builder(lambda _appearance: (GLib.idle_add(_appearance_changed), "")[1])
            except Exception:  # noqa: BLE001
                pass
    _DARK_LISTENERS.append(callback)
    return lambda: _DARK_LISTENERS.remove(callback) if callback in _DARK_LISTENERS else None


def _appearance_changed() -> bool:
    for callback in list(_DARK_LISTENERS):
        try:
            callback()
        except Exception:  # noqa: BLE001
            log.debug("appearance listener failed", exc_info=True)
    return False


def follow_dark(widget: Gtk.Widget) -> None:
    """Give ``widget`` the ``dark`` class while the surface is dark, for the few tones LumaUI lacks."""
    def sync() -> None:
        (widget.add_css_class if is_dark() else widget.remove_css_class)("dark")

    sync()
    stop = on_appearance(sync)
    widget.connect("destroy", lambda *_a: stop())


def showing(window: Gtk.Window) -> Sheet | None:
    """The Playground sheet up on ``window``, if any."""
    sheet = _SHOWING.get(id(window))
    return None if sheet is None or sheet.closed else sheet


def _motion(milliseconds: int) -> int:
    if kit is not None:
        try:
            return kit.motion_duration(milliseconds)
        except Exception:  # noqa: BLE001
            pass
    return milliseconds


def _first_focusable(widget: Gtk.Widget) -> Gtk.Widget | None:
    child = widget.get_first_child()
    while child is not None:
        if child.get_visible() and child.is_sensitive():
            if child.get_focusable() and not isinstance(child, (Gtk.ScrolledWindow, Gtk.Viewport, Gtk.Label)):
                return child
            found = _first_focusable(child)
            if found is not None:
                return found
        child = child.get_next_sibling()
    return None


def is_phone(window: Gtk.Window) -> bool:
    width = window.get_width() or window.get_default_size()[0]
    return 0 < width < PHONE_MAX


def open_sheet(window: Gtk.Window, name: str, **options) -> None:
    """Open another sheet from inside one (Usage from the picker, Places from the book)."""
    application = window.get_application()
    opener = getattr(application, "open_sheet", None)
    if callable(opener):
        opener(name, **options)
        return
    importlib.import_module(f"luma_playground.app.sheets.{name}").present(window, **options)


# ── Development: open a sheet at startup ─────────────────────────────────────


def open_from_environment(window: Gtk.Window) -> bool:
    """``PLAYGROUND_OPEN_SHEET=usage`` (or ``usage:detail=claude,goal=add-a-waitlist-7f3a``) opens it.

    For screenshots and development only. The window calls this once it is up;
    it returns whether a sheet was asked for.
    """
    spec = os.environ.get("PLAYGROUND_OPEN_SHEET", "").strip()
    if not spec:
        return False
    name, _, rest = spec.partition(":")
    options: dict[str, Any] = {}
    for part in filter(None, rest.split(",")):
        key, _, value = part.partition("=")
        options[key.strip()] = value.strip() or True

    def go() -> bool:
        try:
            open_sheet(window, name.strip(), **options)
        except Exception:  # noqa: BLE001 - a dev hook must never take the app down
            log.exception("PLAYGROUND_OPEN_SHEET=%s failed", spec)
        return False

    GLib.timeout_add(int(os.environ.get("PLAYGROUND_OPEN_SHEET_DELAY", "300")), go)
    return True


# ── Words ───────────────────────────────────────────────────────────────────


def money(amount: float | None) -> str:
    if amount is None:
        return ""
    return f"${amount:,.2f}"


def percent(fraction: float | None) -> str:
    if fraction is None:
        return ""
    return f"{round(fraction * 100)}%"


def first_name(name: str | None) -> str:
    return (name or "").split(" ")[0]


def possessive(name: str) -> str:
    return f"{name}’" if name.endswith("s") else f"{name}’s"


# ── Small charts: drawn from the CSS colour, so they follow light, dark and high contrast ──


class Sparkline(Gtk.Widget):
    """A tiny trend line for a service's usage (no axes, no labels: the number beside it says it)."""

    __gtype_name__ = "PlaygroundSparkline"

    def __init__(self, values: Sequence[float], *, width: int = 96, height: int = 28, label: str = "") -> None:
        super().__init__(valign=Gtk.Align.CENTER, accessible_role=Gtk.AccessibleRole.IMG)
        self.add_css_class("pgs-spark")
        self.values = [float(v) for v in values if v is not None]
        self._size = (width, height)
        if label:
            self.update_property([Gtk.AccessibleProperty.LABEL], [label])

    def do_measure(self, orientation, _for_size):
        size = self._size[0] if orientation == Gtk.Orientation.HORIZONTAL else self._size[1]
        return size, size, -1, -1

    def do_snapshot(self, snapshot) -> None:
        values = self.values
        width, height = self.get_width(), self.get_height()
        if len(values) < 2 or width <= 0:
            return
        colour = self.get_color()
        cr = snapshot.append_cairo(Graphene.Rect().init(0, 0, width, height))
        low, high = min(values), max(values)
        span = (high - low) or 1.0
        step = (width - 4) / (len(values) - 1)
        points = [(2 + i * step, height - 3 - (v - low) / span * (height - 6)) for i, v in enumerate(values)]
        cr.move_to(points[0][0], height)
        for x, y in points:
            cr.line_to(x, y)
        cr.line_to(points[-1][0], height)
        cr.close_path()
        cr.set_source_rgba(colour.red, colour.green, colour.blue, 0.12)
        cr.fill()
        cr.set_line_width(1.6)
        cr.set_line_join(1)
        cr.set_source_rgba(colour.red, colour.green, colour.blue, colour.alpha)
        for index, (x, y) in enumerate(points):
            (cr.move_to if index == 0 else cr.line_to)(x, y)
        cr.stroke()
        x, y = points[-1]
        cr.arc(x, y, 2.4, 0, 6.2832)
        cr.fill()


class AreaChart(Gtk.Widget):
    """A day-by-day line with a soft area under it, light gridlines and a few labels (v72 Analytics, calmer)."""

    __gtype_name__ = "PlaygroundAreaChart"

    def __init__(self, values: Sequence[float], labels: Sequence[str], *, height: int = 220, label: str = "") -> None:
        super().__init__(hexpand=True, accessible_role=Gtk.AccessibleRole.IMG)
        self.add_css_class("pgs-chart")
        self.values = [float(v or 0) for v in values]
        self.labels = list(labels)
        self._height = height
        if label:
            self.update_property([Gtk.AccessibleProperty.LABEL], [label])

    def do_measure(self, orientation, _for_size):
        if orientation == Gtk.Orientation.VERTICAL:
            return self._height, self._height, -1, -1
        return 200, 600, -1, -1

    def do_snapshot(self, snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        values = self.values
        if not values or width <= 0:
            return
        colour = self.get_color()
        cr = snapshot.append_cairo(Graphene.Rect().init(0, 0, width, height))
        layout_font = Pango.FontDescription.from_string("Figtree 9")
        top, bottom, left, right = 10, 24, 34, 8
        high = max(values) or 1.0
        nice = _nice_ceiling(high)
        plot_w, plot_h = width - left - right, height - top - bottom

        def text_at(words: str, x: float, y: float, align: float = 0.0) -> None:
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(layout_font)
            layout.set_text(words, -1)
            w, _h = layout.get_pixel_size()
            cr.move_to(x - w * align, y)
            PangoCairo.show_layout(cr, layout)

        cr.set_line_width(1)
        for fraction in (0, 0.5, 1):
            y = top + plot_h * (1 - fraction)
            cr.set_source_rgba(colour.red, colour.green, colour.blue, 0.10 if fraction else 0.18)
            cr.move_to(left, round(y) + 0.5)
            cr.line_to(width - right, round(y) + 0.5)
            cr.stroke()
            cr.set_source_rgba(colour.red, colour.green, colour.blue, 0.5)
            text_at(_short_number(nice * fraction), left - 8, y - 7, 1.0)
        count = len(values)
        step = plot_w / max(1, count - 1)
        points = [(left + i * step, top + plot_h * (1 - v / nice)) for i, v in enumerate(values)]
        accent = self.get_style_context().lookup_color("luma_accent")
        line = accent[1] if accent[0] else colour
        cr.move_to(points[0][0], top + plot_h)
        for x, y in points:
            cr.line_to(x, y)
        cr.line_to(points[-1][0], top + plot_h)
        cr.close_path()
        cr.set_source_rgba(line.red, line.green, line.blue, 0.13)
        cr.fill()
        cr.set_source_rgba(line.red, line.green, line.blue, 1)
        cr.set_line_width(2)
        cr.set_line_join(1)
        for index, (x, y) in enumerate(points):
            (cr.move_to if index == 0 else cr.line_to)(x, y)
        cr.stroke()
        every = max(1, round(count / 7))
        cr.set_source_rgba(colour.red, colour.green, colour.blue, 0.5)
        for index, words in enumerate(self.labels):
            if index % every and index != count - 1:
                continue
            x = left + index * step
            text_at(words, x, height - 17, 0.0 if index == 0 else (1.0 if index == count - 1 else 0.5))


def _nice_ceiling(value: float) -> float:
    if value <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * magnitude:
            return step * magnitude
    return 10 * magnitude


def _short_number(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    return f"{value:g}"


def number_words(value: float | int | None) -> str:
    """1234 → "1,234"; 18.4 stays 18.4; 1_200_000 → "1.2M"."""
    if value is None:
        return ""
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    value = int(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    return f"{value:,}"
