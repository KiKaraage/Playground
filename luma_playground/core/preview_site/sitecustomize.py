# SPDX-License-Identifier: Apache-2.0
"""Loaded into a Python GTK app that Playground shows in its Preview (core.preview puts this folder first on
PYTHONPATH). Nothing about the app itself changes; two things about its windows do:

- At the phone, tablet and desktop sizes (PLAYGROUND_PREVIEW_FILL=1) its main windows fill the frame, as an
  app does on a phone.
- At the phone and tablet sizes (PLAYGROUND_PREVIEW_DEVICE) it's on a phone, as Luma's handheld shell runs
  it: no window buttons (its `button-layout` is ':'), and web views say they're a phone's browser, so a site
  inside the app serves its phone pages instead of its desktop ones.
- In the App window view the Preview is a small desktop, and a window comes back at the size (and maximized
  or not) it had before the app restarted, so a hot restart leaves things as they were. Sizes are kept in
  PLAYGROUND_PREVIEW_STATE (a file in the project's own preview folder), by window kind and order: the
  second note window gets the second note window's size. GTK 4 lets no app place its own window, so where a
  window sits isn't kept.
"""
import json
import os
import sys

FILL = os.environ.get("PLAYGROUND_PREVIEW_FILL") == "1"
HANDHELD = os.environ.get("PLAYGROUND_PREVIEW_DEVICE") in ("phone", "tablet")
# What a phone's browser says it is: sites serve their phone pages to "Mobile" on Android.
PHONE_WINDOW_CSS = ("window.csd, window.solid-csd, window.background, window.maximized, window.fullscreen "
                    "{ margin: 0; padding: 0; border: none; border-radius: 0; box-shadow: none; outline: none; }")
PHONE_AGENT = ("Mozilla/5.0 (Linux; Android 14; Luma) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0.0.0 Mobile Safari/537.36")
STATE = os.environ.get("PLAYGROUND_PREVIEW_STATE", "")


def _load() -> dict:
    try:
        with open(STATE, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_SIZES = _load() if STATE else {}
# [restoring, text views held off meanwhile]: true from the start while the Preview has windows to put back
# (its positions.json), until it writes `restored` beside it (or 20 seconds pass).
_RESTORING = [False, []]
_COUNTS: dict = {}
_SAVING = [0]


def _save(glib) -> None:
    if _SAVING[0]:
        return

    def write() -> bool:
        _SAVING[0] = 0
        try:
            with open(STATE + ".new", "w", encoding="utf-8") as handle:
                json.dump(_SIZES, handle)
            os.replace(STATE + ".new", STATE)
        except OSError:
            pass
        return False
    _SAVING[0] = glib.timeout_add(400, write)


def _patch(gtk) -> None:
    window = getattr(gtk, "Window", None)
    if window is None or getattr(window, "_playground_patched", False):
        return
    from gi.repository import GLib
    present = window.present

    def present_as_before(self, *args, **kwargs):
        try:
            main = self.get_transient_for() is None
        except Exception:  # noqa: BLE001
            main = False
        if main and not getattr(self, "_playground_key", None):
            kind = type(self).__name__
            _COUNTS[kind] = _COUNTS.get(kind, 0) + 1
            key = f"{kind}#{_COUNTS[kind]}"
            self._playground_key = key
            if not FILL and STATE:
                _restore(self, key)
                _follow(self, key, GLib)
        result = present(self, *args, **kwargs)
        _guard_text_drags(gtk, self)
        if main and FILL:
            try:
                self.maximize()                       # the frame is the screen
            except Exception:  # noqa: BLE001
                pass
        return result

    window.present = present_as_before
    window._playground_patched = True
    if HANDHELD:
        try:
            gtk.Settings.get_default().set_property("gtk-decoration-layout", ":")
            # A phone shows an app's window frameless: no frame, shadow or rounded corners round it (Luma's GTK
            # draws a solid frame when a window isn't maximized), so the app's own content is the screen.
            frameless = gtk.CssProvider()
            frameless.load_from_string(PHONE_WINDOW_CSS)
            from gi.repository import Gdk
            gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), frameless, 900)
        except Exception:  # noqa: BLE001
            pass
    if _RESTORING[0]:
        _wait_for_restore(GLib)


def _wait_for_restore(glib) -> None:
    marker = os.path.join(os.path.dirname(STATE), "restored")
    tries = [0]

    def check() -> bool:
        tries[0] += 1
        if not os.path.exists(marker) and tries[0] < 100:
            return True
        _RESTORING[0] = False
        for view in _RESTORING[1]:
            try:
                view.set_can_target(True)
            except Exception:  # noqa: BLE001
                pass
        _RESTORING[1].clear()
        return False
    glib.timeout_add(200, check)


def _guard_text_drags(gtk, root) -> None:
    """Dragging selected text starts a drag-and-drop, and GTK's Broadway (the preview) can't show its drag
    icon: the app crashes (gtk_drag_icon_realize, GTK 4.22). Two things stop that:

    - A press on selected text puts the cursor there and takes the press for itself, so the text view's own
      gestures never see it and no drag-and-drop can begin.
    - While the Preview puts windows back where they were (its drags are fast, and land wherever a person
      would take hold), text takes no presses at all: they reach the window behind it instead."""
    stack = [root]
    while stack:
        widget = stack.pop()
        if isinstance(widget, gtk.TextView) and not getattr(widget, "_playground_guarded", False):
            widget._playground_guarded = True
            gesture = gtk.GestureDrag()
            gesture.set_propagation_phase(gtk.PropagationPhase.CAPTURE)

            def begin(gesture_, x, y, view=widget):
                try:
                    buffer = view.get_buffer()
                    bounds = buffer.get_selection_bounds()
                    if not bounds:
                        return
                    bx, by = view.window_to_buffer_coords(gtk.TextWindowType.WIDGET, int(x), int(y))
                    found, where = view.get_iter_at_location(bx, by)
                    if found and bounds[0].compare(where) <= 0 and where.compare(bounds[1]) <= 0:
                        buffer.place_cursor(where)
                        gesture_.set_state(gtk.EventSequenceState.CLAIMED)
                except Exception:  # noqa: BLE001 - never break the app being built
                    pass
            gesture.connect("drag-begin", begin)
            widget.add_controller(gesture)
            if _RESTORING[0]:
                try:
                    widget.set_can_target(False)
                    _RESTORING[1].append(widget)
                except Exception:  # noqa: BLE001
                    pass
        child = widget.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()


def _mark_guarded() -> None:
    """Tell the Preview this run is guarded, so it may try more than one spot when it puts windows back."""
    if STATE:
        try:
            with open(os.path.join(os.path.dirname(STATE), "guarded"), "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
        except OSError:
            pass


def _restore(win, key: str) -> None:
    saved = _SIZES.get(key)
    if not isinstance(saved, dict):
        return
    try:
        width, height = int(saved.get("width") or 0), int(saved.get("height") or 0)
        if width > 40 and height > 40:
            win.set_default_size(width, height)
        if saved.get("maximized"):
            win.maximize()
    except Exception:  # noqa: BLE001 - never break the app being built
        pass


def _follow(win, key: str, glib) -> None:
    """Keep the window's size and maximized state as the person changes them."""
    def changed(*_args) -> None:
        try:
            entry = dict(_SIZES.get(key) or {})          # a maximized window keeps the size it comes back to
            entry["maximized"] = bool(win.is_maximized())
            if not entry["maximized"]:
                width, height = win.get_default_size()
                if width > 0 and height > 0:
                    entry.update(width=width, height=height)
            _SIZES[key] = entry
            _save(glib)
        except Exception:  # noqa: BLE001
            pass
    for name in ("notify::default-width", "notify::default-height", "notify::maximized"):
        try:
            win.connect(name, changed)
        except Exception:  # noqa: BLE001
            pass


def _patch_web(webkit) -> None:
    """Every web view the app makes says it's a phone's browser (at the phone and tablet sizes)."""
    view = getattr(webkit, "WebView", None)
    if view is None or getattr(view, "_playground_patched", False):
        return
    init, set_settings = view.__init__, view.set_settings

    def phone(settings) -> None:
        try:
            settings.set_user_agent(PHONE_AGENT)
        except Exception:  # noqa: BLE001 - never break the app being built
            pass

    def init_as_phone(self, *args, **kwargs):
        init(self, *args, **kwargs)
        phone(self.get_settings())

    def set_settings_as_phone(self, settings):
        phone(settings)
        return set_settings(self, settings)
    view.__init__ = init_as_phone
    view.set_settings = set_settings_as_phone
    view._playground_patched = True


class _Watch:
    """Patches Gtk (and, on a phone, WebKit) once the app imports them."""

    WANTED = {"gi.repository.Gtk": "gtk", "gi.repository.WebKit": "web", "gi.repository.WebKit2": "web"}

    def __init__(self) -> None:
        self.busy = False

    def find_spec(self, name, path=None, target=None):
        kind = self.WANTED.get(name)
        if kind is None or self.busy or (kind == "web" and not HANDHELD):
            return None
        self.busy = True                       # importing it here comes back through this finder
        try:
            import importlib
            module = importlib.import_module(name)
            if kind == "gtk":
                _patch(module)
                _mark_guarded()
            else:
                _patch_web(module)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.busy = False
        return None


def _windows_to_restore() -> bool:
    try:
        with open(os.path.join(os.path.dirname(STATE), "positions.json"), encoding="utf-8") as handle:
            return bool(json.load(handle).get("windows"))
    except (OSError, ValueError, AttributeError):
        return False


if FILL or STATE or HANDHELD:
    if STATE:
        for stale in ("guarded", "restored"):                          # a new run says so afresh
            try:
                os.remove(os.path.join(os.path.dirname(STATE), stale))
            except OSError:
                pass
        _RESTORING[0] = not FILL and _windows_to_restore()
    sys.meta_path.insert(0, _Watch())
