# SPDX-License-Identifier: Apache-2.0
"""Proof: what Playground's checker saw. A shot or a recording from the preview, and each
check with its status, linked to its evidence."""
from __future__ import annotations

from pathlib import Path

import gi


gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .. import widgets as w  # noqa: E402
from .base import Card, CardContext, data_of, rich_label  # noqa: E402


def _checks(data: dict) -> list[dict]:
    out = []
    for item in data.get("checks") or []:
        if isinstance(item, (list, tuple)):
            item = {"text": item[0], "status": item[1] if len(item) > 1 else "pending",
                    "evidence": item[2] if len(item) > 2 else None}
        if isinstance(item, dict):
            out.append({"text": str(item.get("text") or ""), "status": str(item.get("status") or "pending"),
                        "evidence": item.get("evidence")})
    return out


def resolve(ctx: CardContext, relpath: str) -> Path | None:
    """A shot's path: relative to the project, or to its .playground folder."""
    if not relpath:
        return None
    path = Path(relpath)
    if path.is_absolute():
        return path if path.is_file() else None
    for base in (Path(ctx.project_dir), Path(ctx.project_dir) / ".playground", Path(ctx.project_dir) / ".playground" / "local"):
        candidate = (base / path).resolve()
        try:
            candidate.relative_to(Path(ctx.project_dir).resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def shot(ctx: CardContext, relpath: str | None, *, recording: str | None = None, width: int = 150,
         height: int = 312) -> Gtk.Widget:
    """A phone-shaped shot (v73 `.pgshot`); a click opens it larger."""
    frame = Gtk.Overlay()
    frame.add_css_class("pg-shot")
    frame.set_overflow(Gtk.Overflow.HIDDEN)
    path = resolve(ctx, relpath or "")
    if path is not None:
        try:
            texture = Gdk.Texture.new_from_filename(str(path))
            picture = Gtk.Picture.new_for_paintable(texture)
            picture.set_content_fit(Gtk.ContentFit.COVER)
            if texture.get_width() > texture.get_height():
                # A desktop app or a wide page: the shot keeps its own shape instead of a phone's.
                width = 300
                height = max(120, round(width * texture.get_height() / texture.get_width()))
            picture.set_can_shrink(True)
            picture.add_css_class("pg-shot-picture")
            frame.set_child(picture)
        except GLib.Error:
            path = None
    if path is None:
        empty = w.box("v", 6, "pg-shot-empty", valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        empty.append(w.icon("smartphone", 22))
        empty.append(w.label("No shot yet", xalign=0.5))
        frame.set_child(empty)
    if recording:
        play = w.box("h", 0, "pg-play", halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, can_target=False)
        glyph = w.icon("play", 20)
        glyph.set_hexpand(True)
        play.append(glyph)
        play.set_hexpand(False)
        frame.add_overlay(play)
    if path is not None and ctx.show_image is not None:
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: ctx.show_image(str(path), "The shot from the preview"))
        frame.add_controller(click)
        frame.set_focusable(True)
        frame.set_cursor(Gdk.Cursor.new_from_name("zoom-in"))
        frame.set_tooltip_text("See it larger")
        frame.set_accessible_role(Gtk.AccessibleRole.BUTTON)
        frame.update_property([Gtk.AccessibleProperty.LABEL], ["See the shot larger"])
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, keyval, *_: keyval in (Gdk.KEY_Return, Gdk.KEY_space, Gdk.KEY_KP_Enter)
                     and (ctx.show_image(str(path), "The shot from the preview") or True))
        frame.add_controller(keys)
    holder = w.Sized(frame, width, height)
    holder.add_css_class("pg-shot-holder")
    return holder


def build(entry: dict, ctx: CardContext) -> Gtk.Widget:
    data = data_of(entry)
    checks = _checks(data)
    passed = sum(1 for c in checks if c["status"] == "pass")
    failed = sum(1 for c in checks if c["status"] == "fail")
    running = any(c["status"] in ("pending", "running") for c in checks)
    tone = "good" if checks and passed == len(checks) else "fail" if failed else "work" if running else ""
    summary = "Checking now…" if running and not failed else f"{passed} of {len(checks)} checks passed"
    card = Card("proof", accessible=f"Proof: {summary}")
    card.head("checker", "Proof", tone, right=summary)
    shots = data.get("shots") or []
    has_shot = bool(shots and resolve(ctx, shots[0])) or bool(data.get("recording"))
    # No shot (a desktop app the checker couldn't capture, or no preview): no empty phone, just the checks.
    picture = shot(ctx, shots[0] if shots else None, recording=data.get("recording")) if has_shot else None
    side = w.box("v", 2, hexpand=True)
    for check in checks:
        evidence = check["evidence"]
        row_content = Gtk.Grid(column_spacing=10)
        row_content.attach(w.check_mark(check["status"]), 0, 0, 1, 1)
        row_content.attach(rich_label(check["text"], ctx, "pg-check-text", hexpand=True), 1, 0, 1, 1)
        if evidence:
            row = Gtk.Button()
            row.add_css_class("pg-check")
            row_content.attach(w.label("See it", "pg-see", valign=Gtk.Align.START), 2, 0, 1, 1)
            row.set_child(row_content)
            row.update_property([Gtk.AccessibleProperty.LABEL], [f"{check['text']}. See it"])
            target = evidence if isinstance(evidence, str) else (evidence or {}).get("shot") or ""
            path = resolve(ctx, target)
            if path is not None and ctx.show_image is not None:
                row.connect("clicked", lambda _b, p=str(path), t=check["text"]: ctx.show_image(p, t))
            else:
                row.set_tooltip_text(str(evidence if isinstance(evidence, str) else evidence.get("text", "")))
            side.append(row)
        else:
            row_content.add_css_class("pg-check")
            side.append(row_content)
    by = w.box("h", 7, "pg-by")
    by.append(w.icon("shield-check", 13))
    when = w.parse_ts(entry.get("ts"))
    words = f"Checked by Playground’s checker, separately from {ctx.agent_name}"
    if when is not None:
        words += " · checked " + (w.ago(when) if w.ago(when) != "just now" else "just now")
    by.append(w.label(words, wrap=True, hexpand=True))
    side.append(by)
    # Side by side when there's room, one above the other when there isn't (v73 @container pgmain 520).
    if picture is not None:
        layout = w.Reflow(picture, side, threshold=460, spacing=16)
        layout.add_css_class("pg-proof")
        card.append(layout)
    else:
        card.append(side)
    foot = card.foot(flat=True)
    foot.append(w.button("Show the change", icon_name_="diff", small=True,
                         on_click=lambda: ctx.sheet("code", goal_id=ctx.goal_id)))
    recording = data.get("recording")
    if recording:
        path = resolve(ctx, str(recording))
        if path is not None:
            foot.append(w.button("Play the recording", icon_name_="circle-play", small=True,
                                 on_click=lambda: _play(card, path)))
    if ctx.show_stage is not None:
        foot.append(w.button("Try it in Preview", icon_name_="app-window", small=True, on_click=ctx.show_stage))
    return card


def _play(card: Gtk.Widget, path: Path) -> None:
    from gi.repository import Gio
    launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(path)))
    launcher.launch(card.get_root(), None, None)
