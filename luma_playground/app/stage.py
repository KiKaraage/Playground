# SPDX-License-Identifier: Apache-2.0
"""The Stage: the real product, running (v73 `.pgstage`).

A device switch (Phone, Tablet, Desktop, App window), Stage or Live, and Expand along the
top; the product in a device frame scaled to the room it has; a line saying where it runs;
and Point, Record and Open in a floating bar.

The product is a WebKit 6 web view showing the preview's URL (`preview.state` events from
core.preview). Point injects a small script that outlines what's under the pointer; a click
describes it to the composer. Without WebKit the frame says where the preview runs and
offers to open it in a browser.
"""
from __future__ import annotations

import json
import logging

import socket
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk  # noqa: E402

from luma_appkit import Command, CommandGroup, CommandRegistry, ModeSwitch, command_popover  # noqa: E402

from . import recording  # noqa: E402
from . import widgets as w  # noqa: E402

log = logging.getLogger(__name__)

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit  # noqa: E402
except (ValueError, ImportError):
    WebKit = None

# The device's outside size, the screen's inset and its corners (v73 .pgdev).
DEVICES = {
    "phone": {"size": (391, 806), "inset": (52, 8, 8, 8), "radius": 56, "screen_radius": 48, "label": "Phone"},
    "tablet": {"size": (860, 1140), "inset": (20, 20, 20, 20), "radius": 48, "screen_radius": 30, "label": "Tablet"},
    "desktop": {"size": (1280, 840), "inset": (52, 0, 0, 0), "radius": 14, "screen_radius": 0, "label": "Desktop"},
    # The app on its own: no device around it, filling the Preview (desktop apps open this way). The size is
    # what a screenshot uses; on screen it takes the room it's given.
    "app": {"size": (1000, 700), "inset": (0, 0, 0, 0), "radius": 12, "screen_radius": 12, "label": "App window"},
}

BROADWAY_SCRIPT = r"""
(() => {
  // Broadway says alert("disconnected") whenever the app goes away, which is every restart: Playground
  // shows the preview's own state instead.
  window.alert = () => {};
  const stop = (e) => e.preventDefault();
  document.addEventListener('dragstart', stop, true);
  document.addEventListener('selectstart', stop, true);
  const style = document.createElement('style');
  style.textContent = '* { -webkit-user-drag: none !important; user-select: none !important; }';
  (document.head || document.documentElement).appendChild(style);

  // Where the app's windows are, for Playground to keep (window.surfaces is Broadway's own list): reported
  // after the person lets go of the mouse, and every two seconds when something moved.
  const post = (message) => {
    try { window.webkit.messageHandlers.pgwindows.postMessage(JSON.stringify(message)); } catch (e) {}
  };
  const tops = () => Object.values(window.surfaces || {})
    .filter((s) => s.visible && !s.transientParent && !s.isTemp)
    .sort((a, b) => a.id - b.id);
  let last = '';
  const report = () => {
    if (window.__pgRestoring) return;
    const list = tops().map((s) => ({ x: s.x, y: s.y, w: s.width, h: s.height }));
    const text = JSON.stringify(list);
    if (list.length && text !== last) { last = text; post({ windows: list }); }
  };
  document.addEventListener('mouseup', () => setTimeout(report, 300), true);
  setInterval(report, 2000);

  // Putting them back after a restart: GTK lets no app place its own window, so Playground drags each one
  // there, the way a person would, from a spot that moves it (a title bar, blank space). A spot that doesn't
  // move it (a button, text) is tried no further than a quarter-second; a window nothing moves stays put.
  const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
  const fire = (x, y, down, kind, held) => {
    // During a drag every event goes to where it began, as the browser does for a real held button: the
    // pointer outruns the window (it moves after GTK has seen the motion), and Broadway only hears a window.
    const target = held || document.elementFromPoint(x, y) || document.body;
    const init = { bubbles: true, cancelable: true, clientX: x, clientY: y, screenX: x, screenY: y, button: 0,
                   buttons: down ? 1 : 0, pointerId: 1, pointerType: 'mouse', isPrimary: true, view: window };
    target.dispatchEvent(new PointerEvent('pointer' + kind, init));
    target.dispatchEvent(new MouseEvent('mouse' + kind, init));
  };
  const drag = async (x, y, dx, dy) => {
    const held = document.elementFromPoint(x, y);
    fire(x, y, false, 'move', held); await sleep(40);
    fire(x, y, true, 'down', held); await sleep(60);
    const steps = Math.max(8, Math.ceil(Math.hypot(dx, dy) / 24));
    for (let i = 1; i <= steps; i++) {
      fire(x + dx * i / steps, y + dy * i / steps, true, 'move', held); await sleep(16);
    }
    fire(x + dx, y + dy, false, 'up', held);
  };
  // The window itself, inside its shadow: Broadway offsets the drawing by the shadow's size, which changes
  // when the window becomes the active one, so sizes and places are compared here, never the surface's.
  const frame = (s) => {
    const inner = s.div && s.div.firstElementChild;
    const shift = /translate\(\s*(-?[\d.]+)px,\s*(-?[\d.]+)px/.exec((inner && inner.style.transform) || '');
    const sx = shift ? +shift[1] : 0, sy = shift ? +shift[2] : 0;
    return { left: s.x + sx, top: s.y + sy, width: s.width - 2 * sx, height: s.height - 2 * sy };
  };
  // The size it had, for apps Playground can't size from inside (anything not Python): its bottom-right
  // corner dragged, from just outside the window where GTK's resize handle is. Sizes within a shadow's
  // difference count as the same.
  const resize = async (s, want, guarded) => {
    const dw = want.w - s.width, dh = want.h - s.height;
    if (Math.abs(dw) <= 24 && Math.abs(dh) <= 24) return 'sized';
    // Measured in from the drawing's own corner: the shadow there is a dozen pixels or so, and GTK's handle
    // is the strip just outside the window, so a few spots this far in find it whatever the shadow. (The
    // deeper ones could be inside the window, on text: only for a guarded app.)
    const corners = [[8, 10], [10, 12], [6, 8], [12, 15], [5, 6], [14, 18]];
    for (const [ix, iy] of guarded ? corners : corners.slice(0, 3)) {
      const x = s.x + s.width - ix, y = s.y + s.height - iy;
      const under = document.elementFromPoint(x, y);
      if (!under || !s.div || !s.div.contains(under)) continue;
      const before = [s.width, s.height];
      await drag(x, y, dw, dh);
      await sleep(250);
      if (Math.abs(s.width - before[0]) > 12 || Math.abs(s.height - before[1]) > 12) return 'resized';
    }
    return 'size-kept';
  };
  let personMoved = false;
  document.addEventListener('pointerdown', (e) => { if (e.isTrusted) personMoved = true; }, true);
  window.__pgRestore = async (saved, options) => {
    // Only an app that can't crash on a text drag (Playground's guard is in it) gets more than one try: a
    // grab that lands on text selects it, and a second grab on that selection starts a drag-and-drop, which
    // GTK's Broadway can't show.
    const tries = options && options.guarded ? 99 : 1;
    window.__pgRestoring = true;
    const outcome = [];
    try {
      const deadline = Date.now() + 10000;
      while (tops().length < saved.length && Date.now() < deadline) await sleep(150);
      await sleep(500);                                   // let the windows draw before taking hold of them
      const now = tops();
      // From the top window down: windows a restart opens on top of each other uncover one another in turn.
      for (let i = Math.min(now.length, saved.length) - 1; i >= 0; i--) {
        if (personMoved) { outcome.push('person'); break; }   // they've started using it: leave it to them
        const s = now[i];
        const want = { x: Math.max(0, Math.min(saved[i].x, innerWidth - 120)),
                       y: Math.max(0, Math.min(saved[i].y, innerHeight - 80)) };
        // The size first, where the window opened (a resize straight after a move doesn't take), then the place.
        if (saved[i].w > 0 && saved[i].h > 0) {
          outcome.push(await resize(s, saved[i], tries > 1));
          await sleep(200);
        }
        if (Math.abs(s.x - want.x) < 12 && Math.abs(s.y - want.y) < 12) { outcome.push('there'); continue; }
        const { left, top, width, height } = frame(s);
        // Where a person would take hold: a header bar's middle first, then blank space down the sides and
        // in the middle. Always 14px or more inside the window: its rim resizes (an app may resize from a few
        // pixels in), and a grab that resized instead of moving is dragged straight back.
        const spots = [[0.5, 18], [0.3, 18], [0.7, 18], [0.5, 26], [14, 0.5], [-14, 0.5], [0.5, 0.5], [0.5, -16]];
        let moved = false, tried = 0;
        for (const [fx, fy] of spots) {
          if (tried >= tries) break;
          const x = fx >= 1 ? left + fx : fx < 0 ? left + width + fx : left + width * fx;
          const y = fy >= 1 ? top + fy : fy < 0 ? top + height + fy : top + height * fy;
          const under = document.elementFromPoint(x, y);
          if (!under || !s.div || !s.div.contains(under)) continue;   // another window is over this spot
          const before = frame(s);
          const dx = want.x - s.x, dy = want.y - s.y;
          tried += 1;
          await drag(x, y, dx, dy);
          await sleep(250);
          const after = frame(s);
          // (The shadow grows a few pixels when the window becomes active; a resize is far bigger than that.)
          if (Math.abs(after.width - before.width) > 24 || Math.abs(after.height - before.height) > 24) {
            await drag(x + dx, y + dy, -dx, -dy);         // it resized: put the size back, try elsewhere
            await sleep(250);
            continue;
          }
          if (Math.abs(after.left - before.left) > 12 || Math.abs(after.top - before.top) > 12) { moved = true; break; }
        }
        outcome.push(moved ? 'moved' : 'stuck');

      }
    } finally {
      window.__pgRestoring = false;
      last = '';
      report();
    }
    return outcome.join(',');
  };
})();
"""

POINT_SCRIPT = r"""
(() => {
  if (window.__pgPoint) return;
  const box = document.createElement('div');
  box.setAttribute('aria-hidden', 'true');
  Object.assign(box.style, { position: 'fixed', zIndex: 2147483647, pointerEvents: 'none', display: 'none',
    borderRadius: '8px', boxShadow: '0 0 0 2px rgb(232 169 46), 0 0 0 6px rgb(232 169 46 / .25)',
    transition: 'all .08s ease-out' });
  let on = false, last = null;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const cut = s => s.length > 60 ? s.slice(0, 57) + '…' : s;
  function describe(el) {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      if (n.dataset && n.dataset.kp) return n.dataset.kp;
    }
    const label = clean(el.getAttribute('aria-label'));
    const text = cut(clean(el.innerText || el.textContent));
    const tag = el.tagName.toLowerCase();
    const button = el.closest('button, a, [role="button"]');
    if (button) {
      const t = cut(clean(button.getAttribute('aria-label') || button.innerText));
      return t ? `the “${t}” ${button.tagName === 'A' ? 'link' : 'button'}` : `a ${button.tagName === 'A' ? 'link' : 'button'}`;
    }
    if (/^h[1-6]$/.test(tag) && text) return `the heading “${text}”`;
    if (tag === 'img') return el.alt ? `the picture “${cut(clean(el.alt))}”` : 'a picture';
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      const name = clean(el.labels && el.labels[0] ? el.labels[0].innerText : el.placeholder || el.name);
      return name ? `the “${cut(name)}” field` : 'a field';
    }
    if (label) return `“${cut(label)}”`;
    if (text) return `“${text}”`;
    return `the ${tag}`;
  }
  function frame(el) {
    const r = el.getBoundingClientRect();
    Object.assign(box.style, { left: (r.left - 2) + 'px', top: (r.top - 2) + 'px', width: (r.width + 4) + 'px',
      height: (r.height + 4) + 'px' });
  }
  document.addEventListener('mousemove', e => {
    if (!on || e.target === last) return;
    last = e.target; frame(last);
  }, true);
  document.addEventListener('click', e => {
    if (!on) return;
    e.preventDefault(); e.stopPropagation();
    const el = e.target;
    window.webkit.messageHandlers.pgpoint.postMessage(JSON.stringify({ about: describe(el),
      tag: el.tagName.toLowerCase(), path: location.pathname }));
  }, true);
  window.__pgPoint = { set(v) { on = !!v; last = null; box.style.display = on ? 'block' : 'none';
    document.documentElement.style.cursor = on ? 'crosshair' : '';
    if (on && !box.isConnected) document.documentElement.appendChild(box); } };
})();
"""


# ── the device frame ─────────────────────────────────────────────────────────

class DeviceFrame(Gtk.Widget):
    """A phone, tablet, browser window or app window drawn around the product, at its real size.
    The canvas scales it to fit (a transform, so the product lays out at the device's own width).
    These are the colours of hardware, not of Luma's chrome."""

    __gtype_name__ = "PlaygroundDeviceFrame"

    def __init__(self) -> None:
        super().__init__(overflow=Gtk.Overflow.HIDDEN)
        self.add_css_class("pg-device")
        self.kind = "phone"
        self.title = ""
        self.address = ""
        self.secure = False
        self.status_colour = _rgba("#ffffff")
        # A native app on a phone owns the whole screen, under the clock too (LumaUI keeps a safe area there);
        # a website sits under the phone browser's own bar.
        self.edge_to_edge = False
        self.screen = Gtk.Box()
        self.screen.add_css_class("pg-screen")
        self.screen.set_overflow(Gtk.Overflow.HIDDEN)
        self.screen.set_parent(self)

    def set_kind(self, kind: str) -> None:
        self.kind = kind if kind in DEVICES else "phone"
        for name in DEVICES:
            self.remove_css_class(name)
        self.add_css_class(self.kind)
        self.queue_resize()
        self.queue_draw()

    def set_content(self, widget: Gtk.Widget | None) -> None:
        w.clear(self.screen)
        if widget is not None:
            widget.set_hexpand(True)
            widget.set_vexpand(True)
            self.screen.append(widget)

    def set_chrome(self, title: str, address: str, secure: bool) -> None:
        self.title, self.address, self.secure = title, address, secure
        self.queue_draw()

    def set_status_colour(self, colour: Gdk.RGBA) -> None:
        """The phone's status bar takes the page's own colour (its theme-color), as a real phone shows it."""
        self.status_colour = colour
        self.queue_draw()

    def device_size(self) -> tuple[int, int]:
        if self.kind == "app" and getattr(self, "fit", None):
            return self.fit
        return DEVICES[self.kind]["size"]

    def do_measure(self, orientation, _for_size):
        width, height = self.device_size()
        size = width if orientation == Gtk.Orientation.HORIZONTAL else height
        return size, size, -1, -1

    def set_edge_to_edge(self, edge: bool) -> None:
        if edge != self.edge_to_edge:
            self.edge_to_edge = edge
            # The screen rounds all four corners itself then, as it does its bottom ones (data/playground.css).
            (self.add_css_class if edge else self.remove_css_class)("edge")
            self.queue_allocate()
            self.queue_draw()

    def do_size_allocate(self, width: int, height: int, _baseline: int) -> None:
        top, right, bottom, left = DEVICES[self.kind]["inset"]
        if self.kind == "phone" and self.edge_to_edge:
            top = right                       # the screen's own edge: the status bar is drawn over the app
        self.screen.allocate(max(1, width - left - right), max(1, height - top - bottom), -1,
                             Gsk.Transform().translate(Graphene.Point().init(left, top)))

    def do_dispose(self) -> None:
        if self.screen.get_parent() is self:
            self.screen.unparent()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        spec = DEVICES[self.kind]
        width, height = self.get_width(), self.get_height()
        radius = spec["radius"]

        def rounded(x, y, rw, rh, r):
            rect = Gsk.RoundedRect()
            size = Graphene.Size().init(r, r)
            rect.init(Graphene.Rect().init(x, y, rw, rh), size, size, size, size)
            return rect

        outline = rounded(0, 0, width, height, radius)
        if self.kind in ("phone", "tablet"):
            snapshot.push_rounded_clip(outline)
            snapshot.append_color(_rgba("#0d0d0e"), Graphene.Rect().init(0, 0, width, height))
            snapshot.pop()
            snapshot.append_border(outline, [1.5] * 4, [_rgba("#3a3a3e")] * 4)
            screen = rounded(8, 8, width - 16, height - 16, spec["screen_radius"])
            if self.kind == "phone" and not self.edge_to_edge:
                # The status bar: the page's colour under the clock, the island over it.
                snapshot.push_rounded_clip(screen)
                snapshot.append_color(self.status_colour, Graphene.Rect().init(8, 8, width - 16, 48))
                snapshot.pop()
            if self.kind == "phone":
                snapshot.push_rounded_clip(screen)    # the app's corners are the screen's
                self.snapshot_child(self.screen, snapshot)
                snapshot.pop()
            else:
                self.snapshot_child(self.screen, snapshot)
            if self.kind == "phone":
                island = rounded(width / 2 - 56, 19, 112, 32, 16)
                snapshot.push_rounded_clip(island)
                snapshot.append_color(_rgba("#000000"), Graphene.Rect().init(width / 2 - 56, 19, 112, 32))
                snapshot.pop()
                colour = self.status_colour
                light = 0.2126 * colour.red + 0.7152 * colour.green + 0.0722 * colour.blue > 0.55
                ink = (0.08, 0.07, 0.06) if light else (1, 1, 1)
                cr = snapshot.append_cairo(Graphene.Rect().init(0, 0, width, 60))
                _text(cr, "9:41", 66, 35, 15, ink, centre=True, bold=True)
                cr.set_source_rgb(*ink)
                x = width - 70
                for index, bar in enumerate((4, 6.5, 9, 11.5)):
                    cr.rectangle(x - 26 + index * 4.5, 40 - bar, 3, bar)
                cr.fill()
                _round_rect(cr, x, 30, 22, 11, 3.2)
                cr.set_line_width(1.1)
                cr.stroke()
                _round_rect(cr, x + 2, 32, 16, 7, 1.8)
                cr.fill()
                cr.rectangle(x + 23, 33.5, 1.6, 4)
                cr.fill()
            return
        if self.kind == "app" and getattr(self, "desktop", False):
            # A desktop app's windows on the canvas: the Preview is their desktop, so nothing is drawn round them.
            self.snapshot_child(self.screen, snapshot)
            return
        if self.kind == "app":
            # Just the app: its own window, clipped to soft corners, a hairline round it.
            snapshot.push_rounded_clip(outline)
            snapshot.append_color(_rgba("#ffffff"), Graphene.Rect().init(0, 0, width, height))
            self.snapshot_child(self.screen, snapshot)
            snapshot.pop()
            snapshot.append_border(outline, [1] * 4, [_rgba("#00000033")] * 4)
            return
        top = spec["inset"][0]
        snapshot.push_rounded_clip(outline)
        bar = "#ecebea" if self.kind == "desktop" else "#efe9e1"
        snapshot.append_color(_rgba("#ffffff"), Graphene.Rect().init(0, 0, width, height))
        snapshot.append_color(_rgba(bar), Graphene.Rect().init(0, 0, width, top))
        snapshot.append_color(_rgba("#d9d6d2"), Graphene.Rect().init(0, top - 1, width, 1))
        snapshot.pop()
        snapshot.append_border(outline, [1] * 4, [_rgba("#00000059")] * 4)
        self.snapshot_child(self.screen, snapshot)
        cr = snapshot.append_cairo(Graphene.Rect().init(0, 0, width, top))
        if self.kind == "desktop":
            for index in range(3):
                cr.set_source_rgb(0.83, 0.82, 0.80)
                cr.arc(26 + index * 20, top / 2, 6, 0, 6.2832)
                cr.fill()
            pill_w = min(520, width - 220)
            x = (width - pill_w) / 2
            _round_rect(cr, x, top / 2 - 16, pill_w, 32, 9)
            cr.set_source_rgb(1, 1, 1)
            cr.fill()
            _text(cr, self.address or "Preview", width / 2, top / 2, 14, (0.34, 0.33, 0.31), centre=True)
        else:
            _text(cr, self.title or "App", 18, top / 2, 14, (0.17, 0.13, 0.10), bold=True)
            cr.set_source_rgb(0.54, 0.48, 0.42)
            cr.set_line_width(1.6)
            right = width - 20
            cr.move_to(right - 6, top / 2 - 6); cr.line_to(right + 6, top / 2 + 6)
            cr.move_to(right + 6, top / 2 - 6); cr.line_to(right - 6, top / 2 + 6)
            cr.rectangle(right - 36, top / 2 - 6, 12, 12)
            cr.move_to(right - 66, top / 2); cr.line_to(right - 54, top / 2)
            cr.stroke()


def _rgba(text: str) -> Gdk.RGBA:
    colour = Gdk.RGBA()
    colour.parse(text)
    return colour


def _round_rect(cr, x, y, width, height, radius) -> None:
    import math
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _text(cr, text, x, y, size, rgb, *, centre=False, bold=False) -> None:
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Pango, PangoCairo
    layout = PangoCairo.create_layout(cr)
    font = Pango.FontDescription.from_string(f"Figtree {'Bold ' if bold else ''}{size}px")
    layout.set_font_description(font)
    layout.set_text(text, -1)
    _ink, logical = layout.get_pixel_extents()
    cr.set_source_rgb(*rgb)
    cr.move_to(x - (logical.width / 2 if centre else 0), y - logical.height / 2)
    PangoCairo.show_layout(cr, layout)


class DeviceCanvas(Gtk.Widget):
    """The dotted room the device stands in; it scales the device to fit, never above its real size."""

    __gtype_name__ = "PlaygroundDeviceCanvas"

    def __init__(self, frame: DeviceFrame) -> None:
        super().__init__(hexpand=True, vexpand=True, overflow=Gtk.Overflow.HIDDEN)
        self.add_css_class("pg-canvas")
        self.frame = frame
        frame.set_parent(self)
        self.overlay: Gtk.Widget | None = None
        self.scale = 1.0

    def set_overlay(self, widget: Gtk.Widget | None) -> None:
        """Something in place of the device (nothing to show yet, starting), centred."""
        if self.overlay is not None:
            self.overlay.unparent()
        self.overlay = widget
        if widget is not None:
            widget.set_parent(self)
        self.frame.set_visible(widget is None)
        self.queue_resize()

    def do_measure(self, orientation, for_size):
        minimum = 120
        if self.overlay is not None:
            child_min, child_nat, _, _ = self.overlay.measure(orientation, for_size)
            return max(minimum, child_min), max(minimum, child_nat), -1, -1
        # It would like the device at its real size; it shrinks the device to whatever it gets.
        device_w, device_h = DEVICES[self.frame.kind]["size"]
        natural = (device_w if orientation == Gtk.Orientation.HORIZONTAL else device_h) + 24
        return minimum, natural, -1, -1

    def do_size_allocate(self, width: int, height: int, _baseline: int) -> None:
        if self.overlay is not None and self.overlay.has_css_class("pg-fill"):
            # A panel that takes the room (the log): as wide as reads well, as tall as there is.
            child_w, child_h = max(1, min(width - 24, 820)), max(1, height - 24)
            self.overlay.measure(Gtk.Orientation.VERTICAL, child_w)
            self.overlay.allocate(child_w, child_h, -1, Gsk.Transform().translate(
                Graphene.Point().init((width - child_w) / 2, (height - child_h) / 2)))
        elif self.overlay is not None:
            _m, nat_w, _, _ = self.overlay.measure(Gtk.Orientation.HORIZONTAL, -1)
            child_w = min(width, nat_w)
            _m, nat_h, _, _ = self.overlay.measure(Gtk.Orientation.VERTICAL, child_w)
            child_h = min(height, nat_h)
            self.overlay.allocate(child_w, child_h, -1, Gsk.Transform().translate(
                Graphene.Point().init((width - child_w) / 2, (height - child_h) / 2)))
        if self.frame.get_visible():
            room_w, room_h = max(40, width - 28), max(40, height - 24)
            # An app window takes the room, at its real size (no scaling): the app lays itself out to fit.
            if getattr(self.frame, "desktop", False):
                # A desktop: the pane, less a margin, so a new window (Broadway opens them at its top left)
                # doesn't sit flush against the pane's edge.
                room_w, room_h = max(40, width - 32), max(40, height - 32)
            self.frame.fit = (room_w, room_h) if self.frame.kind == "app" else None
            device_w, device_h = self.frame.device_size()
            self.scale = min(1.0, room_w / device_w, room_h / device_h)
            shown_w, shown_h = device_w * self.scale, device_h * self.scale
            transform = Gsk.Transform().translate(Graphene.Point().init((width - shown_w) / 2, (height - shown_h) / 2))
            transform = transform.scale(self.scale, self.scale)
            self.frame.allocate(device_w, device_h, -1, transform)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        if self.frame.get_visible():
            # The device's shadow on the canvas (v73: 0 30px 60px -20px).
            width, height = self.get_width(), self.get_height()
            device_w, device_h = self.frame.device_size()
            shown_w, shown_h = device_w * self.scale, device_h * self.scale
            x, y = (width - shown_w) / 2, (height - shown_h) / 2
            radius = DEVICES[self.frame.kind]["radius"] * self.scale
            rect = Gsk.RoundedRect()
            size = Graphene.Size().init(radius, radius)
            rect.init(Graphene.Rect().init(x, y, shown_w, shown_h), size, size, size, size)
            shade = None if getattr(self.frame, "desktop", False) else _rgba("rgba(0,0,0,0.35)")
            if shade is not None:
                snapshot.append_outset_shadow(rect, shade, 0, 20 * self.scale, -14 * self.scale, 44 * self.scale)
            self.snapshot_child(self.frame, snapshot)
        if self.overlay is not None:
            self.snapshot_child(self.overlay, snapshot)

    def do_dispose(self) -> None:
        for child in (self.frame, self.overlay):
            if child is not None and child.get_parent() is self:
                child.unparent()


class _IconModes(ModeSwitch):
    """The kit's mode switch, as icons alone: each mode keeps its name for screen readers and tooltips."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.add_css_class("icons-only")
        self._labels()

    def _labels(self) -> None:
        super()._labels()
        for button in self.buttons.values():
            button.label_widget.set_visible(False)


# ── the Stage ────────────────────────────────────────────────────────────────

class Stage(Gtk.Box):
    """Emits `point(description)`, `recorded(folder, words)`, `expand(bool)` and `close()`."""

    __gsignals__ = {
        "point": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "recorded": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        "expand": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "close": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "open-sheet": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "restart": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "stop": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # A desktop app moved between a device frame (it fills it) and the App window view (a small desktop):
        # it starts again in the new way.
        "refill": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "report": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.add_css_class("pg-stage")
        self.device = "phone"
        self._device_chosen = False      # the person picked a size: the kind of app no longer decides it
        self.env = "stage"
        self.wide = False
        self.pointing = False
        self.project_dir: str | None = None
        self.project_name = ""
        self.preview = {"state": "none", "url": "", "error": ""}
        self.live_url = ""
        self.live_services: list[str] = []
        self.where_text = "on this computer · only you can see it"
        self.view = None
        self._loaded_url = ""
        self._recording: dict | None = None

        # the top: device, Stage or Live, Expand
        head = w.box("h", 8, "pg-stage-head")
        self.devices = _IconModes([("phone", "Phone", "pg-smartphone-symbolic"),
                                   ("tablet", "Tablet", "pg-tablet-symbolic"),
                                   ("desktop", "Desktop", "pg-monitor-symbolic"),
                                   ("app", "App window", "pg-app-window-symbolic")],
                                  current="phone", on_change=self._choose_device, label="Device")
        self.devices.add_css_class("pg-devices")
        head.append(self.devices)
        head.append(Gtk.Box(hexpand=True))
        self.envs = ModeSwitch([("stage", "Preview"), ("live", "Live")], current="stage", on_change=self.set_env,
                               label="Preview or Live")
        self.envs.add_css_class("pg-envs")
        head.append(self.envs)
        self.close_button = w.icon_button("x", "Close the preview", on_click=lambda: self.emit("close"))
        self.close_button.set_visible(False)
        head.append(self.close_button)
        self.head = head
        self.append(head)

        # the room and the device
        self.frame = DeviceFrame()
        self.frame.set_kind("phone")
        self.canvas = DeviceCanvas(self.frame)
        self.tip = w.box("h", 8, "pg-point-tip", halign=Gtk.Align.CENTER, valign=Gtk.Align.START, can_target=False)
        self.tip.append(w.icon("mouse-pointer-click", 14))
        self.tip.append(w.label("Click anything to talk about it"))
        self.tip.set_visible(False)
        room = Gtk.Overlay(vexpand=True)
        room.set_child(self.canvas)
        room.add_overlay(self.tip)
        self.append(room)

        # where it runs
        self.caption = w.box("h", 8, "pg-stage-caption", halign=Gtk.Align.CENTER)
        self.caption_dot = w.dot("stage")
        self.caption.append(self.caption_dot)
        self.caption_text = w.label("", markup=True, ellipsize=True)
        self.caption.append(self.caption_text)
        self.append(self.caption)

        # Stop/Run · Restart · Point · Record · Open
        bar = w.box("h", 2, "pg-stage-bar", halign=Gtk.Align.CENTER)
        self.run_button = Gtk.Button()
        self.run_button.add_css_class("pg-bar-button")
        self.run_button.set_child(self._bar_content("square", "Stop"))
        self.run_button.connect("clicked", lambda _b: self.emit("stop" if self._running() else "restart"))
        bar.append(self.run_button)
        self.restart_button = Gtk.Button()
        self.restart_button.add_css_class("pg-bar-button")
        self.restart_button.set_child(w.icon("refresh-cw", 16))
        self.restart_button.set_tooltip_text("Restart it")
        self.restart_button.update_property([Gtk.AccessibleProperty.LABEL], ["Restart it"])
        self.restart_button.connect("clicked", lambda _b: self.emit("restart"))
        bar.append(self.restart_button)
        self.point_button = Gtk.ToggleButton()
        self.point_button.add_css_class("pg-bar-button")
        self.point_button.set_child(self._bar_content("mouse-pointer-click", "Point"))
        self.point_button.set_tooltip_text("Click something in the preview to talk about it")
        self.point_button.connect("toggled", lambda b: self.set_pointing(b.get_active()))
        bar.append(self.point_button)
        self.record_button = Gtk.Button()
        self.record_button.add_css_class("pg-bar-button")
        self.record_button.set_child(self._bar_content("circle-dot", "Record"))
        self.record_button.set_tooltip_text("Record a short clip of the preview")
        self.record_button.connect("clicked", lambda _b: self.toggle_recording())
        bar.append(self.record_button)
        self.open_button = Gtk.Button()
        self.open_button.add_css_class("pg-bar-button")
        content = self._bar_content("external-link", "Open")
        content.append(w.icon("chevron-down", 14))
        self.open_button.set_child(content)
        self.open_button.set_tooltip_text("Open it somewhere else, restart it, or see what it printed")
        self.open_button.connect("clicked", self._open_menu)
        bar.append(self.open_button)
        self.bar = bar
        self.append(bar)
        self._show_state()

    @staticmethod
    def _bar_content(glyph: str, text: str) -> Gtk.Box:
        content = w.box("h", 7)
        content.append(w.icon(glyph, 16))
        content.label_widget = w.label(text)
        content.append(content.label_widget)
        return content

    # ── what it shows ─────────────────────────────────────────────────────
    def set_project(self, project_dir: str | None, name: str, *, live_url: str = "", where_text: str = "") -> None:
        if project_dir != self.project_dir:
            self._device_chosen = False         # another project: its kind of app picks the size again
            self._here = ""
        self.project_dir, self.project_name = project_dir, name
        self.live_url = live_url
        # The green dot on Live means there is a Live to look at, nothing more.
        self.envs.set_status("live", "running" if live_url else None)
        if where_text:
            self.where_text = where_text
        self.frame.set_chrome(name, "", False)
        self._show_state()

    def set_live_services(self, names: list[str]) -> None:
        self.live_services = list(names)
        if self.env == "live":
            self._show_state()

    def set_preview(self, state: str, url: str = "", error: str = "", label: str = "", target: str = "",
                    device: str = "") -> None:
        """``label`` names what runs, in plain words ("the Vite dev server"); ``target`` is web, desktop-app or
        screen; ``device`` the size the run was started for. A desktop app or a screen opens as an app window
        filling the Preview, a website on a phone, until the person picks a size.

        A desktop app runs as the device it was started for (a phone app on a phone: Luma's handheld mode), so
        the frame never shows a run started for another size: it starts it again for this one."""
        if state not in ("running", "ready", "up", "live"):
            # Stopped or starting again: the page is from the old run (often the same address), so the next run
            # loads afresh instead of leaving Broadway's "disconnected" grey on screen.
            self._loaded_url = ""
            self._restored_for = ""            # the next run's windows go back where they were
        self.preview = {"state": state or "none", "url": url or "", "error": error or "",
                        "label": label or self.preview.get("label", ""), "target": target or self.preview.get("target", ""),
                        "device": device if state not in ("none", "stopped") else ""}
        if target and not self._device_chosen:
            wanted = "app" if target in ("desktop-app", "screen") else "phone"
            if wanted != self.device:
                self.device = wanted                   # the run starting now already matches; no restart
                self.frame.set_kind(wanted)
                if self.devices.current != wanted:
                    self.devices.set_current(wanted)
                self.canvas.queue_allocate()
        self._sync_desktop()
        self._show_state()
        if self.mismatched and state in ("running", "ready", "up"):
            self.emit("refill")

    @property
    def mismatched(self) -> bool:
        """A desktop app's run was started for another size than the one shown."""
        launched = self.preview.get("device") or ""
        return self.desktop_app and bool(launched) and launched != self.device

    @property
    def desktop_app(self) -> bool:
        return self.preview.get("target") == "desktop-app"

    def _sync_desktop(self) -> None:
        """A desktop app in the App window view: the Preview is its desktop. GTK's Broadway page is see-through
        and the frame draws nothing, so its windows sit on the canvas, to be moved and resized."""
        desktop = self.desktop_app and self.device == "app"
        # A desktop app on a phone: the whole screen, as Luma runs it there; the clock's ink follows its colour.
        self.frame.set_edge_to_edge(self.desktop_app and self.device == "phone")
        self._follow_status_colour()
        if getattr(self.frame, "desktop", False) != desktop:
            self.frame.desktop = desktop
            self.frame.queue_draw()
        view = self.view
        if WebKit is None or view is None or not hasattr(view, "set_background_color"):
            return
        colour = Gdk.RGBA()
        colour.parse("rgba(0,0,0,0)" if desktop else "#ffffff")
        view.set_background_color(colour)
        manager = view.get_user_content_manager()
        (self.frame.add_css_class if desktop else self.frame.remove_css_class)("desktop-app")
        if desktop and getattr(self, "_desktop_sheet", None) is None:
            self._desktop_sheet = WebKit.UserStyleSheet.new(
                "html, body { background: transparent !important; }", WebKit.UserContentInjectedFrames.TOP_FRAME,
                WebKit.UserStyleLevel.USER, None, None)
            manager.add_style_sheet(self._desktop_sheet)
        elif not desktop and getattr(self, "_desktop_sheet", None) is not None:
            manager.remove_style_sheet(self._desktop_sheet)
            self._desktop_sheet = None
        if self.desktop_app and not getattr(self, "_broadway_script", None):
            # Broadway draws windows as images: a real mouse pressed and moved on one starts WebKit dragging the
            # image (and selecting), which swallows the motion the app needs to move or resize its window.
            self._broadway_script = WebKit.UserScript.new(BROADWAY_SCRIPT, WebKit.UserContentInjectedFrames.TOP_FRAME,
                                                          WebKit.UserScriptInjectionTime.START, None, None)
            manager.add_script(self._broadway_script)

    def _choose_device(self, device: str) -> None:
        self._device_chosen = True
        self.set_device(device)

    def set_device(self, device: str) -> None:
        before = self.device
        self.device = device
        self.frame.set_kind(device)
        self._sync_desktop()
        # A desktop app starts again at every new size: each is its own device (a phone, a tablet, a computer,
        # a window on the Preview's desktop), and it runs as that device does.
        if self.desktop_app and before != device:
            self.emit("refill")
        if self.devices.current != device:
            self.devices.set_current(device)
        self.canvas.queue_allocate()

    def set_env(self, env: str) -> None:
        self.env = env
        if self.envs.current != env:
            self.envs.set_current(env)
        self._show_state()

    def set_narrow(self, phone: bool) -> None:
        """In a sheet (phone) or floating (compact): a close button, no Expand."""
        self.close_button.set_visible(phone)
        (self.add_css_class if phone else self.remove_css_class)("in-sheet")

    def set_floating(self, floating: bool) -> None:
        self.close_button.set_visible(floating or self.has_css_class("in-sheet"))

    def _url(self) -> str:
        if self.env == "live":
            return self.live_url
        return self.preview["url"] if self.preview["state"] in ("running", "ready", "up", "live") else ""

    def _show_state(self) -> None:
        url = self._url()
        self.envs.set_visible(self.project_dir is not None)
        if self.project_dir is None:
            self._empty("Nothing to show yet", "Describe what you want to make, and the first version appears here.")
            self._caption("")
            self._bar_ready(False)
            return
        if self.env == "live" and not url:
            self._empty("Live isn’t set up yet", "When you release, people use it at its address, and it shows here.",
                        ("Places", lambda: self.emit("open-sheet", "places")))
            self._caption("")
            self._bar_ready(False)
            return
        state = self.preview["state"]
        if self.env == "stage" and self.mismatched:
            # Never a run started for another size in this frame: it's starting again for this one.
            spinner = w.box("v", 14, "pg-building", halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            spin = w.Spinner(22)
            spin.set_halign(Gtk.Align.CENTER)
            spinner.append(spin)
            size = DEVICES.get(self.device, {}).get("label", "this size").lower()
            spinner.append(w.label(f"Starting it as a {size} app…" if self.device != "app" else "Starting it as a window…",
                                   xalign=0.5))
            self.canvas.set_overlay(spinner)
            self._caption("")
            self._bar_ready(False)
            return
        if self.env == "stage" and not url:
            if state in ("starting", "installing", "building"):
                spinner = w.box("v", 14, "pg-building", halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
                spin = w.Spinner(22)
                spin.set_halign(Gtk.Align.CENTER)
                spinner.append(spin)
                spinner.append(w.label(f"Starting {self.preview.get('label') or 'the preview'}…", xalign=0.5))
                self.canvas.set_overlay(spinner)
            elif state in ("error", "failed", "stopped") and self.preview["error"]:
                self._empty("The preview stopped", self.preview["error"], ("See what it printed", self.show_log),
                            ("Report it as a bug", lambda: self.emit("report", self.preview["error"])),
                            ("Try again", lambda: self.emit("restart")))
            elif state == "stopped":
                self._empty("The preview is stopped", "Run it to see the app again.", ("Run it", lambda: self.emit("restart")))
            else:
                self._empty("Nothing to show yet", "The first version appears here as soon as there’s something real.")
            self._caption("")
            self._bar_ready(False)
            return
        self.canvas.set_overlay(None)
        self._load(url)
        if self.env == "live":
            host = urlparse(url).netloc or url
            words = " · ".join([host, *self.live_services])
            self._caption(f"<b>Live</b> · {GLib.markup_escape_text(words)}", "good")
            self.frame.set_chrome(self.project_name, host, True)
        else:
            running = self.preview.get("label") or ""
            words = " · ".join(p for p in (running[:1].upper() + running[1:], self.where_text) if p)
            self._caption(f"<b>Preview</b> · {GLib.markup_escape_text(words)}", "stage")
            self.frame.set_chrome(self.project_name, f"Preview · {self.where_text.split(' · ')[0]}", False)
        self._bar_ready(True)

    def _empty(self, title: str, text: str, *actions: tuple | None) -> None:
        empty = w.box("v", 8, "pg-empty", halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        empty.set_size_request(260, -1)
        tile = w.box("h", 0, "pg-empty-tile", halign=Gtk.Align.CENTER)
        glyph = w.icon("smartphone", 26)
        glyph.set_hexpand(True)
        tile.append(glyph)
        tile.set_hexpand(False)
        empty.append(tile)
        empty.append(w.label(title, "pg-empty-title", xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER))
        text_label = w.label(text, "pg-empty-text", xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER)
        text_label.set_max_width_chars(34)
        empty.append(text_label)
        buttons = [w.button(a[0], small=True, on_click=a[1]) for a in actions if a is not None]
        if buttons:
            row = w.box("h", 8, halign=Gtk.Align.CENTER)
            for button in buttons:
                row.append(button)
            empty.append(row)
        self.canvas.set_overlay(empty)
        if self.pointing:
            self.point_button.set_active(False)

    def _caption(self, markup: str, kind: str = "stage") -> None:
        # Kept for its words (read out to screen readers), never shown: the Preview says enough by itself.
        self.caption_text.set_markup(markup)
        self.caption.set_visible(False)
        for name in ("stage", "good"):
            self.caption_dot.remove_css_class(name)
        self.caption_dot.add_css_class(kind)

    def _running(self) -> bool:
        return self.preview.get("state") in ("starting", "installing", "building", "running", "ready", "up")

    def _bar_ready(self, ready: bool) -> None:
        for button in (self.point_button, self.record_button, self.open_button):
            button.set_sensitive(ready)
        # Stop while it runs (or starts); Run once it's stopped or has crashed. Live isn't ours to stop.
        stage = self.project_dir is not None and self.env == "stage" and self.preview.get("state") != "none"
        running = self._running()
        self.run_button.set_visible(stage)
        self.restart_button.set_visible(stage and running)
        glyph, words = ("square", "Stop") if running else ("play", "Run")
        self.run_button.set_child(self._bar_content(glyph, words))
        self.run_button.set_tooltip_text("Stop the app" if running else "Start the app again")

    # ── the web view ──────────────────────────────────────────────────────
    def _ensure_view(self) -> Gtk.Widget:
        if self.view is not None:
            return self.view
        if WebKit is None:
            self.view = self._no_webkit()
            return self.view
        manager = WebKit.UserContentManager()
        manager.add_script(WebKit.UserScript.new(POINT_SCRIPT, WebKit.UserContentInjectedFrames.TOP_FRAME,
                                                 WebKit.UserScriptInjectionTime.END, None, None))
        manager.register_script_message_handler("pgpoint", None)
        manager.register_script_message_handler("pgwindows", None)
        manager.connect("script-message-received::pgwindows", self._windows_moved)
        manager.connect("script-message-received::pgpoint", self._pointed)
        view = WebKit.WebView(user_content_manager=manager)
        settings = view.get_settings()
        settings.set_enable_developer_extras(True)
        view.add_css_class("pg-webview")
        view.connect("load-changed", self._load_changed)
        view.connect("load-failed", self._load_failed)
        # A desktop app's page never gets to show the browser's own dialogs over the preview.
        view.connect("script-dialog", lambda _v, dialog: self.desktop_app)
        self.view = view
        self._sync_desktop()
        return view

    def _no_webkit(self) -> Gtk.Widget:
        holder = w.box("v", 10, "pg-noweb", valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        holder.append(w.label("The preview is running", "pg-empty-title", xalign=0.5))
        self.noweb_url = w.label("", "pg-mono", xalign=0.5, wrap=True, selectable=True)
        holder.append(self.noweb_url)
        holder.append(w.button("Open in browser", icon_name_="external-link", style="key",
                               on_click=lambda: self.open_in_browser()))
        return holder

    def _load(self, url: str) -> None:
        view = self._ensure_view()
        if view.get_parent() is None:
            self.frame.set_content(view)
        if WebKit is None:
            self.noweb_url.set_label(url)
            return
        if url != self._loaded_url:
            self._loaded_url = url
            # After a restart, back to the page the person was on (same path, the new address).
            here = getattr(self, "_here", "")
            view.load_uri(urljoin(url, here) if here and not self.desktop_app else url)

    # ── a desktop app's windows: where they were, back after a restart ──

    def _positions_path(self) -> Path | None:
        if not self.project_dir:
            return None
        return Path(self.project_dir) / ".playground" / "local" / "preview-home" / "positions.json"

    def _windows_moved(self, _manager, value) -> None:
        """The page says where the app's windows are (after the person moved one): kept for the next run."""
        path = self._positions_path()
        if path is None or not (self.desktop_app and self.device == "app"):
            return
        try:
            message = json.loads(value.to_string())
            windows = [{k: int(w[k]) for k in ("x", "y", "w", "h")} for w in message.get("windows") or []][:20]
        except (ValueError, TypeError, KeyError, AttributeError):
            return
        if not windows or getattr(self, "_restoring", False):
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"windows": windows}), encoding="utf-8")
        except OSError:
            pass

    def _restore_windows(self, view) -> None:
        """A fresh run of a desktop app: drag its windows back where they were (the page script does it)."""
        path = self._positions_path()
        if path is None or not (self.desktop_app and self.device == "app"):
            return
        try:
            saved = json.loads(path.read_text(encoding="utf-8")).get("windows") or []
        except (OSError, ValueError, AttributeError):
            return
        if not saved:
            return
        self._restoring = True

        def done(web, result) -> None:
            self._restoring = False
            try:
                path.with_name("restored").write_text("1", encoding="utf-8")   # the app's text takes presses again
            except OSError:
                pass
            try:
                outcome = web.call_async_javascript_function_finish(result).to_string()
            except GLib.Error:
                return
            if "stuck" in outcome:
                log.info("some preview windows couldn't be moved back (%s)", outcome)
        # A Python app says it's guarded once it has loaded GTK, which can be just after the page is up.
        waited = [0]

        def start() -> bool:
            guarded = path.with_name("guarded").exists()
            if not guarded and waited[0] < 10:
                waited[0] += 1
                return GLib.SOURCE_CONTINUE
            view.call_async_javascript_function(
                "return window.__pgRestore ? await window.__pgRestore(" + json.dumps(saved) + ", " +
                json.dumps({"guarded": guarded}) + ") : 'no script'", -1, None, None, None, None, done)
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(300, start)

    def _load_changed(self, view, event) -> None:
        if event != WebKit.LoadEvent.FINISHED:
            return
        uri = view.get_uri() or ""
        if self.desktop_app and uri != getattr(self, "_restored_for", ""):
            self._restored_for = uri
            self._restore_windows(view)
        parts = urlparse(view.get_uri() or "")
        if parts.scheme in ("http", "https") and parts.hostname in ("127.0.0.1", "localhost", "::1"):
            self._here = parts.path + (f"?{parts.query}" if parts.query else "")
        if self.pointing:
            self._script(True)
        script = ("(document.querySelector('meta[name=theme-color]')||{}).content || "
                  "getComputedStyle(document.body).backgroundColor")
        view.evaluate_javascript(script, -1, None, None, None, self._page_colour)

    STATUS_EVERY_MS = 1500

    def _follow_status_colour(self) -> None:
        """While an app owns the phone's screen, keep reading the colour under the clock from what's shown, so
        the time and icons stay legible on it (dark ink on a light app, light on a dark one)."""
        wanted = self.frame.edge_to_edge
        if wanted and not getattr(self, "_status_timer", 0):
            self._status_timer = GLib.timeout_add(self.STATUS_EVERY_MS, self._sample_status)
        elif not wanted and getattr(self, "_status_timer", 0):
            GLib.source_remove(self._status_timer)
            self._status_timer = 0

    def _sample_status(self) -> bool:
        view = self.view
        if not self.frame.edge_to_edge:
            self._status_timer = 0
            return GLib.SOURCE_REMOVE
        if WebKit is None or view is None or not view.get_mapped() or not hasattr(view, "get_snapshot"):
            return GLib.SOURCE_CONTINUE
        view.get_snapshot(WebKit.SnapshotRegion.VISIBLE, WebKit.SnapshotOptions.NONE, None, self._sampled)
        return GLib.SOURCE_CONTINUE

    def _sampled(self, view, result) -> None:
        try:
            texture = view.get_snapshot_finish(result)
            downloader = Gdk.TextureDownloader.new(texture)
            downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
            data, stride = downloader.download_bytes()
        except (GLib.Error, AttributeError, TypeError):
            return
        pixels = data.get_data()
        width, height = texture.get_width(), texture.get_height()
        # Beside the island, in the band the clock sits in: the app's own colour there.
        points = [(int(width * fx), int(height * 0.025)) for fx in (0.08, 0.2, 0.8, 0.92)]
        total = [0, 0, 0]
        for x, y in points:
            at = y * stride + x * 4
            for channel in range(3):
                total[channel] += pixels[at + channel]
        colour = Gdk.RGBA()
        colour.red, colour.green, colour.blue = (value / (255 * len(points)) for value in total)
        colour.alpha = 1.0
        old = self.frame.status_colour
        if abs(old.red - colour.red) + abs(old.green - colour.green) + abs(old.blue - colour.blue) > 0.03:
            self.frame.set_status_colour(colour)

    def _page_colour(self, view, result) -> None:
        try:
            value = view.evaluate_javascript_finish(result)
        except GLib.Error:
            return
        colour = Gdk.RGBA()
        text = value.to_string() if value is not None else ""
        if text and colour.parse(text) and colour.alpha > 0.5:
            self.frame.set_status_colour(colour)

    def _load_failed(self, _view, _event, uri, error) -> bool:
        if error.matches(WebKit.NetworkError.quark(), WebKit.NetworkError.CANCELLED):
            return False
        self._loaded_url = ""
        if self.env == "live":
            host = urlparse(uri).netloc or uri
            self._empty("Live can’t be reached", f"{host} didn’t answer. Check this computer’s internet "
                        "connection, or whether the live server is up.", ("Try again", lambda: self._show_state()))
        else:
            self._empty("The preview can’t be reached", f"{uri} didn’t answer. It may still be starting.",
                        ("Try again", lambda: self._show_state()), ("See what it printed", self.show_log))
        return True

    def reload(self) -> None:
        if self.view is not None and WebKit is not None:
            self.view.reload()

    # ── a screenshot for an agent (playground_preview_screenshot) ────────
    def capture(self, destination: str, *, device: str = "", path: str = "/", environment: str = "stage",
                done=None) -> None:
        """Save what the Stage shows as a PNG at `destination`, then call `done(ok, error)`."""
        done = done or (lambda _ok, _error: None)
        if environment in ("stage", "live") and environment != self.env:
            self.set_env(environment)
        if device in DEVICES and device != self.device:
            self._choose_device(device)      # chosen, so the restart a new size needs doesn't pick its own
        base = self._url()
        if WebKit is None or self.view is None or not base:
            done(False, "The preview isn’t showing anything yet. It may still be starting.")
            return
        from urllib.parse import urljoin
        target = urljoin(base, path or "/")

        def finished(view, result) -> None:
            try:
                texture = view.get_snapshot_finish(result)
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                texture.save_to_png(destination)
            except (GLib.Error, OSError) as error:
                done(False, str(getattr(error, "message", error)))
                return
            done(True, "")

        def snap() -> bool:
            self.view.get_snapshot(WebKit.SnapshotRegion.VISIBLE, WebKit.SnapshotOptions.NONE, None, finished)
            return GLib.SOURCE_REMOVE

        current = (self.view.get_uri() or "").split("#")[0]
        if current.rstrip("/") == target.rstrip("/"):
            GLib.timeout_add(150, snap)
            return
        handler = [0]

        def loaded(view, event) -> None:
            if event == WebKit.LoadEvent.FINISHED:
                view.disconnect(handler[0])
                GLib.timeout_add(400, snap)
        handler[0] = self.view.connect("load-changed", loaded)
        self._loaded_url = target
        self.view.load_uri(target)

    # ── Point ─────────────────────────────────────────────────────────────
    def set_pointing(self, on: bool) -> None:
        self.pointing = on
        if self.point_button.get_active() != on:
            self.point_button.set_active(on)
        self.tip.set_visible(on)
        (self.add_css_class if on else self.remove_css_class)("pointing")
        self._script(on)

    def _script(self, on: bool) -> None:
        if self.view is None or WebKit is None:
            return
        self.view.evaluate_javascript(f"window.__pgPoint && window.__pgPoint.set({'true' if on else 'false'})",
                                      -1, None, None, None, None, None)

    def _pointed(self, _manager, value) -> None:
        try:
            message = json.loads(value.to_string())
        except (ValueError, AttributeError):
            return
        about = str(message.get("about") or "").strip()
        if about:
            self.set_pointing(False)
            self.emit("point", about)

    # ── Record ────────────────────────────────────────────────────────────
    def toggle_recording(self) -> None:
        if self._recording is None:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self) -> None:
        if self.view is None or WebKit is None or not self.project_dir:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        folder = Path(self.project_dir) / ".playground" / "local" / "recordings" / stamp
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        self._recording = {"folder": folder, "frames": 0, "began": time.monotonic(), "source": 0, "busy": False}
        self.record_button.add_css_class("rec")
        self._recording["source"] = GLib.timeout_add(200, self._frame)
        self._recording["clock"] = GLib.timeout_add(500, self._record_label)
        self._record_label()

    def _record_label(self) -> bool:
        if self._recording is None:
            return GLib.SOURCE_REMOVE
        seconds = int(time.monotonic() - self._recording["began"])
        content = self.record_button.get_child()
        content.label_widget.set_label(f"{seconds // 60}:{seconds % 60:02d}  Stop")
        if seconds >= 60:
            self._stop_recording()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _frame(self) -> bool:
        record = self._recording
        if record is None or self.view is None:
            return GLib.SOURCE_REMOVE
        if record["busy"]:
            return GLib.SOURCE_CONTINUE
        record["busy"] = True

        def done(view, result) -> None:
            record["busy"] = False
            try:
                texture = view.get_snapshot_finish(result)
            except GLib.Error:
                return
            record["frames"] += 1
            texture.save_to_png(str(record["folder"] / f"{record['frames']:04d}.png"))
        self.view.get_snapshot(WebKit.SnapshotRegion.VISIBLE, WebKit.SnapshotOptions.NONE, None, done)
        return GLib.SOURCE_CONTINUE

    def _stop_recording(self) -> None:
        record, self._recording = self._recording, None
        if record is None:
            return
        for key in ("source", "clock"):
            if record.get(key):
                GLib.source_remove(record[key])
        self.record_button.remove_css_class("rec")
        seconds = time.monotonic() - record["began"]
        if not record["frames"]:
            self.record_button.get_child().label_widget.set_label("Record")
            return
        # The frames become one WebM on a worker thread; the button says so meanwhile.
        self.record_button.get_child().label_widget.set_label("Saving…")
        self.record_button.set_sensitive(False)
        length = f"{int(seconds) // 60}:{int(seconds) % 60:02d}"

        def work() -> None:
            time.sleep(0.4)   # the last snapshot may still be on its way to disk
            result = recording.make_video(record["folder"], seconds)
            GLib.idle_add(finished, result, priority=GLib.PRIORITY_DEFAULT)

        def finished(result) -> bool:
            self.record_button.get_child().label_widget.set_label("Record")
            self.record_button.set_sensitive(self._url() != "")
            words = f"Recording, {length}" if result.video else f"Recording, {length} (still frames)"
            if not result.video:
                self._tell(result.words)
            self.emit("recorded", str(result.path), words)
            return GLib.SOURCE_REMOVE
        threading.Thread(target=work, daemon=True, name="recording").start()

    def _tell(self, words: str) -> None:
        try:
            from luma_appkit import Toast
            Toast.show(self, words)
        except Exception:  # noqa: BLE001 - a toast is a courtesy
            pass

    # ── Open ──────────────────────────────────────────────────────────────
    def _open_menu(self, button: Gtk.Button) -> None:
        live = self.env == "live"
        registry = CommandRegistry((
            CommandGroup(f"Open {'Live' if live else 'the preview'}", (
                Command("stage.viola", "In Viola", self.open_in_browser, icon="pg-globe-symbolic",
                        description="In your browser" if live else "On this computer"),
                Command("stage.qr", "On your phone", self.show_qr, icon="pg-qr-code-symbolic",
                        description="Scan a code, no app needed"),
                Command("stage.app", "Launch as an app", self.open_as_app, icon="pg-app-window-symbolic",
                        description="In its own window, as it installs"),
            )),
            *(() if live else (CommandGroup("This preview", (
                Command("stage.restart", "Restart it", lambda: self.emit("restart"), icon="pg-refresh-cw-symbolic",
                        description="Stop it and start it again"),
                Command("stage.log", "What it printed", self.show_log, icon="pg-terminal-symbolic",
                        description="The last lines of its output"),
            )),)),
            CommandGroup(None, (
                Command("stage.android", "In the Android emulator", lambda: self.emit("open-sheet", "places"),
                        icon="pg-smartphone-symbolic", description="Set it up in Places"),
            )),
        ))
        popover = command_popover(registry)
        # The bar sits at the foot of the Stage, so its menu opens upward, over the device.
        popover.set_parent(button)
        popover.set_position(Gtk.PositionType.TOP)
        popover.popup()

    def show_log(self) -> None:
        """The last lines the app printed, over the device, in place (a close button gives the device back)."""
        if not self.project_dir:
            return
        from ..core import preview as preview_core
        tail = preview_core.read_log_tail(Path(self.project_dir) / ".playground" / "local" / "preview.log", lines=200)
        panel = w.box("v", 0, "pg-log-panel")
        panel.add_css_class("pg-fill")
        head = w.box("h", 8, "pg-log-head")
        head.append(w.label("What it printed", "pg-log-title", hexpand=True))
        head.append(w.icon_button("x", "Close", on_click=self._show_state))
        panel.append(head)
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.NONE, cursor_visible=False,
                            top_margin=10, bottom_margin=10, left_margin=14, right_margin=14)
        view.add_css_class("pg-log-text")
        view.get_buffer().set_text(tail.strip() or "Nothing yet.")
        scroll = Gtk.ScrolledWindow(vexpand=True, child=view)
        panel.append(scroll)
        self.canvas.set_overlay(panel)
        GLib.idle_add(lambda: (scroll.get_vadjustment().set_value(scroll.get_vadjustment().get_upper()), False)[1])

    def open_in_browser(self) -> None:
        url = self._url()
        if not url:
            return
        viola = _app_info("org.projectluma.Viola.desktop")
        if viola is not None:
            try:
                viola.launch_uris([url], None)
                return
            except GLib.Error:
                pass
        Gtk.UriLauncher.new(url).launch(self.get_root(), None, None)

    def open_as_app(self) -> None:
        url = self._url()
        if not url:
            return
        viola = _app_info("org.projectluma.Viola.desktop")
        if viola is not None:
            try:
                Gio.Subprocess.new([viola.get_executable(), f"--app={url}"], Gio.SubprocessFlags.NONE)
                return
            except GLib.Error:
                pass
        Gtk.UriLauncher.new(url).launch(self.get_root(), None, None)

    def show_qr(self) -> None:
        url = phone_url(self._url())
        if not url:
            return
        dialog = Adw.Dialog(title="Open it on your phone", content_width=360)
        body = w.box("v", 12, "pg-qr")
        body.set_margin_top(18)
        body.set_margin_bottom(22)
        body.set_margin_start(22)
        body.set_margin_end(22)
        header = Adw.HeaderBar(show_title=True)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        matrix = qr_matrix(url.encode())
        if matrix is not None:
            area = Gtk.DrawingArea(content_width=196, content_height=196, halign=Gtk.Align.CENTER)
            area.add_css_class("pg-qr-code")
            area.set_draw_func(_draw_qr, matrix)
            area.update_property([Gtk.AccessibleProperty.LABEL], [f"A code that opens {url}"])
            area.set_accessible_role(Gtk.AccessibleRole.IMG)
            body.append(area)
        body.append(w.label("Point your phone’s camera at the code. Your phone needs to be on the same Wi‑Fi "
                            "as this computer.", "pg-sub", wrap=True, xalign=0.5, justify=Gtk.Justification.CENTER))
        line = w.box("h", 8, halign=Gtk.Align.CENTER)
        line.append(w.label(url, "pg-mono", selectable=True, wrap=True))
        line.append(w.icon_button("copy", "Copy the address",
                                  on_click=lambda: self.get_clipboard().set(url)))
        body.append(line)
        toolbar.set_content(body)
        dialog.set_child(toolbar)
        dialog.present(self.get_root())

    def shutdown(self) -> None:
        if self._recording is not None:
            self._stop_recording()


def _app_info(desktop_id: str):
    try:
        return Gio.DesktopAppInfo.new(desktop_id)
    except (TypeError, GLib.Error):
        return None


def phone_url(url: str) -> str:
    """A localhost preview, as a phone on the same network reaches it."""
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.hostname not in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return url
    address = _lan_address()
    if not address:
        return url
    netloc = address + (f":{parsed.port}" if parsed.port else "")
    return parsed._replace(netloc=netloc).geturl()


def _lan_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))   # TEST-NET: nothing is sent; this only picks the route
            return probe.getsockname()[0]
    except OSError:
        return ""


def _draw_qr(_area, cr, width, height, matrix) -> None:
    size = len(matrix)
    quiet = 2
    cell = min(width, height) / (size + 2 * quiet)
    offset_x = (width - cell * (size + 2 * quiet)) / 2
    offset_y = (height - cell * (size + 2 * quiet)) / 2
    cr.set_source_rgb(1, 1, 1)
    cr.rectangle(offset_x, offset_y, cell * (size + 2 * quiet), cell * (size + 2 * quiet))
    cr.fill()
    cr.set_source_rgb(0, 0, 0)
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                cr.rectangle(offset_x + (x + quiet) * cell, offset_y + (y + quiet) * cell, cell + 0.3, cell + 0.3)
    cr.fill()


# ── a QR code, byte mode, error correction M, versions 1 to 6 ────────────────
# After the structure of Project Nayuki's QR Code generator (MIT); written for the few bytes
# of an address, which is all the Stage ever needs to encode.

_ECC_PER_BLOCK = (10, 16, 26, 18, 24, 16)
_BLOCKS = (1, 1, 1, 2, 2, 4)
_ALIGN = ((), (6, 18), (6, 22), (6, 26), (6, 30), (6, 34))


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coefficient in enumerate(divisor):
            result[i] ^= _gf_mul(coefficient, factor)
    return result


def _raw_modules(version: int) -> int:
    result = (16 * version + 128) * version + 64
    if version >= 2:
        count = version // 7 + 2
        result -= (25 * count - 10) * count - 55
    return result


def qr_matrix(data: bytes) -> list[list[bool]] | None:
    for version in range(1, 7):
        total = _raw_modules(version) // 8
        ecc, blocks = _ECC_PER_BLOCK[version - 1], _BLOCKS[version - 1]
        capacity = total - ecc * blocks
        if 4 + 8 + 8 * len(data) <= capacity * 8:
            break
    else:
        return None
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(length)))
    put(0b0100, 4)
    put(len(data), 8)
    for byte in data:
        put(byte, 8)
    put(0, min(4, capacity * 8 - len(bits)))
    put(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < capacity * 8:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    block_len = capacity // blocks
    divisor = _rs_divisor(ecc)
    data_blocks = [codewords[i * block_len:(i + 1) * block_len] for i in range(blocks)]
    ecc_blocks = [_rs_remainder(block, divisor) for block in data_blocks]
    final = [block[i] for i in range(block_len) for block in data_blocks]
    final += [block[i] for i in range(ecc) for block in ecc_blocks]

    size = version * 4 + 17
    modules = [[False] * size for _ in range(size)]
    function = [[False] * size for _ in range(size)]

    def setf(x: int, y: int, dark: bool) -> None:
        modules[y][x] = dark
        function[y][x] = True

    for i in range(size):
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    setf(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    positions = _ALIGN[version - 1]
    for i, ax in enumerate(positions):
        for j, ay in enumerate(positions):
            if (i == 0 and j == 0) or (i == 0 and j == len(positions) - 1) or (i == len(positions) - 1 and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def format_bits(mask: int) -> None:
        value = (0 << 3) | mask   # error correction M is 00
        remainder = value
        for _ in range(10):
            remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
        bits_ = ((value << 10) | remainder) ^ 0x5412

        def bit(i: int) -> bool:
            return (bits_ >> i) & 1 == 1
        for i in range(6):
            setf(8, i, bit(i))
        setf(8, 7, bit(6))
        setf(8, 8, bit(7))
        setf(7, 8, bit(8))
        for i in range(9, 15):
            setf(14 - i, 8, bit(i))
        for i in range(8):
            setf(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            setf(8, size - 15 + i, bit(i))
        setf(8, size - 8, True)

    format_bits(0)
    index = 0
    stream = [(byte >> (7 - k)) & 1 for byte in final for k in range(8)]
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vertical in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = size - 1 - vertical if upward else vertical
                if not function[y][x] and index < len(stream):
                    modules[y][x] = stream[index] == 1
                    index += 1
        right -= 2

    masks = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)
    base = [row[:] for row in modules]
    best, best_score = None, None
    for mask_index, rule in enumerate(masks):
        for y in range(size):
            for x in range(size):
                modules[y][x] = base[y][x] ^ (not function[y][x] and rule(x, y))
        format_bits(mask_index)
        score = _penalty(modules)
        if best_score is None or score < best_score:
            best, best_score = [row[:] for row in modules], score
    return best


def _penalty(modules: list[list[bool]]) -> int:
    size = len(modules)
    score = 0
    for lines in (modules, [list(col) for col in zip(*modules)]):
        for line in lines:
            run, colour = 0, None
            for dark in line:
                if dark == colour:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, colour = 1, dark
            if run >= 5:
                score += run - 2
            text = "".join("1" if d else "0" for d in line)
            score += 40 * (text.count("1011101") and (text.count("00001011101") + text.count("10111010000")))
    for y in range(size - 1):
        for x in range(size - 1):
            c = modules[y][x]
            if c == modules[y][x + 1] == modules[y + 1][x] == modules[y + 1][x + 1]:
                score += 3
    dark = sum(sum(row) for row in modules)
    total = size * size
    score += (abs(dark * 20 - total * 10) + total - 1) // total * 10
    return score
