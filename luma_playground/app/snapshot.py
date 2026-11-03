# SPDX-License-Identifier: Apache-2.0
"""Development tool: render a window to a PNG without capturing the screen.

PLAYGROUND_SNAPSHOT=/tmp/x.png renders the window after it settles and quits.
PLAYGROUND_SNAPSHOT_DELAY (ms, default 1200) waits for layout and loading.
"""
from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402


def _popovers(widget: Gtk.Widget):
    """Every popover showing inside `widget`'s tree (they draw on their own surfaces)."""
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Popover):
            if child.get_mapped():
                yield child
        else:
            yield from _popovers(child)
        child = child.get_next_sibling()


def _popover_origin(window: Gtk.Widget, popover: Gtk.Popover) -> tuple[float, float] | None:
    """Where the popover's widget sits in the window's widget coordinates."""
    surface = popover.get_surface()
    if surface is None or not hasattr(surface, "get_position_x"):
        return None
    wx, wy = window.get_surface_transform() if hasattr(window, "get_surface_transform") else (0, 0)
    px, py = popover.get_surface_transform()
    return surface.get_position_x() + px - wx, surface.get_position_y() + py - wy


def render_to_png(widget: Gtk.Widget, path: str) -> bool:
    """The widget as a PNG, with any open popovers drawn where they float (development shots)."""
    width, height = widget.get_width(), widget.get_height()
    if width <= 0 or height <= 0:
        return False
    paintable = Gtk.WidgetPaintable.new(widget)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    if isinstance(widget, Gtk.Native):
        for popover in list(_popovers(widget)):
            origin = _popover_origin(widget, popover)
            pw, ph = popover.get_width(), popover.get_height()
            if origin is None or pw <= 0 or ph <= 0:
                continue
            # Only the popover's card and a soft shadow: the rest of its surface is see-through on a real
            # desktop, but has no alpha on a bare X server.
            content = popover.get_first_child()
            ok, bounds = content.compute_bounds(popover) if content is not None else (False, None)
            snapshot.save()
            snapshot.translate(Graphene.Point().init(*origin))
            if ok:
                rect = Graphene.Rect()
                # A pixel in from the edge: the popover's own (light-theme) node behind the card would
                # otherwise show as a bright hairline round a dark card.
                rect.init(bounds.get_x() + 1, bounds.get_y() + 1, bounds.get_width() - 2, bounds.get_height() - 2)
                rounded = Gsk.RoundedRect()
                rounded.init_from_rect(rect, 20)
                snapshot.append_outset_shadow(rounded, Gdk.RGBA(red=0, green=0, blue=0, alpha=0.16), 0, 10, 0, 28)
                snapshot.push_rounded_clip(rounded)
            Gtk.WidgetPaintable.new(popover).snapshot(snapshot, pw, ph)
            if ok:
                snapshot.pop()
            snapshot.restore()
    node = snapshot.to_node()
    if node is None:
        return False
    native = widget.get_native()
    renderer = native.get_renderer() if native is not None else None
    if renderer is None:
        renderer = Gsk.CairoRenderer()
        renderer.realize_for_display(widget.get_display())
    texture = renderer.render_texture(node, Graphene.Rect().init(0, 0, width, height))
    data = texture.save_to_png_bytes()
    with open(path, "wb") as handle:
        handle.write(data.get_data())
    return True


def arm(window: Gtk.Window, application) -> None:
    path = os.environ.get("PLAYGROUND_SNAPSHOT")
    if not path:
        return
    delay = int(os.environ.get("PLAYGROUND_SNAPSHOT_DELAY", "1200"))

    def shoot() -> bool:
        ok = render_to_png(window, path)
        print(f"snapshot {'saved' if ok else 'FAILED'}: {path}", flush=True)
        application.quit()
        return False

    GLib.timeout_add(delay, shoot)
