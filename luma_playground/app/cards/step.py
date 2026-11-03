# SPDX-License-Identifier: Apache-2.0
"""Step: live progress while the agent works (what it's doing now, files touched, time and
spend), folding to one line when it's done."""
from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from .. import widgets as w  # noqa: E402
from .base import Card, CardContext, data_of, rich_label  # noqa: E402


def _files(data: dict) -> list[dict]:
    out = []
    for item in data.get("files") or []:
        if isinstance(item, dict):
            out.append({"path": str(item.get("path") or ""), "added": item.get("added"), "removed": item.get("removed"),
                        "new": bool(item.get("new") or item.get("kind") == "A")})
        else:
            out.append({"path": str(item), "added": None, "removed": None, "new": False})
    return out


def _file_list(files: list[dict], ctx: CardContext) -> Gtk.Widget:
    well = w.box("v", 0, "pg-files")
    for item in files:
        row = Gtk.Button()
        row.add_css_class("pg-file")
        line = w.box("h", 8)
        line.append(w.icon("plus" if item["new"] else "file-text", 13))
        line.append(w.label(item["path"], "pg-mono", ellipsize=True, hexpand=True))
        if item["added"]:
            line.append(w.label(f"+{item['added']}", "pg-add"))
        if item["removed"]:
            line.append(w.label(f"−{item['removed']}", "pg-del"))
        row.set_child(line)
        row.update_property([Gtk.AccessibleProperty.LABEL], [f"Show the change in {item['path']}"])
        row.connect("clicked", lambda _b, p=item["path"]: ctx.sheet("code", goal_id=ctx.goal_id, path=p))
        well.append(row)
    return well


def build(entry: dict, ctx: CardContext) -> Gtk.Widget:
    data = data_of(entry)
    state = str(data.get("state") or "running")
    if state == "done":
        return _folded(entry, data, ctx)
    return _live(entry, data, ctx, state)


def _summary(files: list[dict], data: dict) -> str:
    parts = [w.plural(len(files), "file")] if files else []
    if data.get("elapsed_s"):
        parts.append(w.duration_words(data["elapsed_s"]))
    if data.get("spend_text"):
        parts.append(str(data["spend_text"]))
    return " · ".join(parts)


def _folded(entry: dict, data: dict, ctx: CardContext) -> Gtk.Widget:
    files = _files(data)
    title = str(data.get("title") or entry.get("text") or "Done")
    card = Card("step", accessible=f"Done: {title}")
    card.add_css_class("done")
    line = w.box("h", 10, "pg-fold")
    ok = w.box("h", 0, "pg-ok", valign=Gtk.Align.CENTER)
    tick = w.icon("check", 12)
    tick.set_hexpand(True)
    ok.append(tick)
    ok.set_hexpand(False)
    line.append(ok)
    line.append(rich_label(title, ctx, "pg-fold-title", wrap=False, ellipsize=True, hexpand=True))
    # The numbers give way before the title does on a narrow thread (a phone).
    meta = w.label(_summary(files, data), "pg-fold-meta", ellipsize=True)
    meta.set_tooltip_text(_summary(files, data))
    line.append(meta)
    revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                            transition_duration=0 if w.reduced_motion() else 200)
    toggle = w.icon_button("chevron-down", "Show the files", size=15, css="pg-ib")

    def flip(_button) -> None:
        shown = not revealer.get_reveal_child()
        revealer.set_reveal_child(shown)
        toggle.set_child(w.icon("chevron-up" if shown else "chevron-down", 15))
        label = "Hide the files" if shown else "Show the files"
        toggle.set_tooltip_text(label)
        toggle.update_property([Gtk.AccessibleProperty.LABEL], [label])
        toggle.update_state([Gtk.AccessibleState.EXPANDED], [GObject.Value(GObject.TYPE_INT, int(shown))])
    toggle.connect("clicked", flip)
    toggle.update_state([Gtk.AccessibleState.EXPANDED], [GObject.Value(GObject.TYPE_INT, 0)])
    line.append(toggle)
    line.append(w.icon_button("code", "Show the change", size=15,
                              on_click=lambda: ctx.sheet("code", goal_id=ctx.goal_id)))
    card.append(line)
    extra = w.box("v", 0, "pg-fold-extra")
    if files:
        extra.append(_file_list(files, ctx))
    foot = w.box("h", 8, "pg-card-foot", "flat")
    foot.append(w.button("Show the change", icon_name_="diff", small=True,
                         on_click=lambda: ctx.sheet("code", goal_id=ctx.goal_id)))
    foot.append(w.button("Terminal", icon_name_="terminal", small=True,
                         on_click=lambda: ctx.sheet("code", goal_id=ctx.goal_id, terminal=True)))
    extra.append(foot)
    revealer.set_child(extra)
    card.append(revealer)
    return card


def _live(entry: dict, data: dict, ctx: CardContext, state: str) -> Gtk.Widget:
    files = _files(data)
    title = str(data.get("title") or entry.get("text") or "Working")
    failed = state == "failed"
    card = Card("step", accessible=f"{'Stopped' if failed else 'Working'}: {title}")
    # How long it's been going sits top right (where it runs is in the conversation's details, not here).
    elapsed = w.label("", "pg-card-when")
    if failed:
        card.head("circle-alert", "Stopped", "fail", right=elapsed)
    else:
        agent = ctx.connection_of(entry.get("by") or None)
        owner = agent.get("owner")
        who = ""
        if owner and owner != ctx.you and ctx.person_name:
            who = f"{ctx.person_name(owner).split()[0]}’s agent is working"
        named = title.strip().lower() not in ("working", "working…", "thinking", "")
        # Without a name of its own, the step says who's working, once (not "Working" over "Working").
        card.head(w.Spinner(12), who or ("Working" if named else f"{ctx.agent_name} is working"), "work",
                  right=elapsed)
    if failed or named:
        card.title(title, ctx)
    calls = [c for c in data.get("tool_calls") or [] if isinstance(c, dict)]
    if calls:
        subs = w.box("v", 2, "pg-subs")
        current = next((i for i, c in enumerate(calls) if c.get("status") in ("in_progress", "running", "pending")),
                       len(calls))
        for index, call in enumerate(calls[-6:]):
            real = index + max(0, len(calls) - 6)
            status = call.get("status") or ""
            line = w.box("h", 9, "pg-sub-line")
            if status in ("completed", "done") or real < current:
                line.add_css_class("done")
                line.append(w.icon("check", 14))
            elif status == "failed":
                line.add_css_class("fail")
                line.append(w.icon("x", 14))
            elif real == current:
                line.add_css_class("now")
                line.append(w.Spinner(12))
            else:
                line.add_css_class("next")
                ring = Gtk.Box(valign=Gtk.Align.CENTER)
                ring.add_css_class("pg-ring")
                line.append(ring)
            line.append(rich_label(str(call.get("title") or call.get("kind") or "Working"), ctx, wrap=False,
                                   ellipsize=True, hexpand=True))
            subs.append(line)
        card.append(subs)
    if not failed:
        bar = Gtk.ProgressBar()
        bar.add_css_class("pg-progress")
        fraction = data.get("fraction")
        if fraction is not None:
            bar.set_fraction(float(fraction))
        else:
            bar.pulse()
            bar.set_pulse_step(0.06)
            if not w.reduced_motion():
                _keep_pulsing(bar)
        card.append(bar)
    stats = w.box("h", 12, "pg-stats")
    # The facts wrap onto a second line when the thread is narrow; "Show the change" keeps its place.
    facts = Adw.WrapBox(child_spacing=16, line_spacing=6, hexpand=True, valign=Gtk.Align.CENTER)
    stats.append(facts)
    started = w.parse_ts(entry.get("ts"))
    base = float(data.get("elapsed_s") or 0)

    def tick() -> bool:
        seconds = base
        if not failed and started is not None and state == "running":
            seconds = max(base, time.time() - started.timestamp())
        minutes, secs = divmod(int(seconds), 60)
        hours, minutes = divmod(minutes, 60)
        elapsed.set_label(f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}")
        return GLib.SOURCE_CONTINUE
    tick()
    if state == "running":
        _every_second(card, tick)
    revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                            transition_duration=0 if w.reduced_motion() else 200)
    if files:
        toggle = Gtk.Button()
        toggle.add_css_class("pg-stat-button")
        content = w.box("h", 6)
        content.append(w.icon("file-text", 13))
        content.append(w.label(f"<b>{len(files)}</b> {'file' if len(files) == 1 else 'files'} touched", markup=True))
        chevron = w.icon("chevron-down", 13)
        content.append(chevron)
        toggle.set_child(content)
        toggle.update_property([Gtk.AccessibleProperty.LABEL], [f"{w.plural(len(files), 'file')} touched"])

        def flip(_button) -> None:
            shown = not revealer.get_reveal_child()
            revealer.set_reveal_child(shown)
            chevron.set_from_icon_name(w.icon_name("chevron-up" if shown else "chevron-down"))
        toggle.connect("clicked", flip)
        facts.append(toggle)
        revealer.set_child(_file_list(files, ctx))
    if data.get("spend_text"):
        spend = w.box("h", 6)
        spend.append(w.icon("coins", 13))
        spend.append(w.label(str(data["spend_text"]), "pg-strong"))
        facts.append(spend)
    if failed:
        # The change is worth a look once the step has ended; while it runs it's still being made.
        show = Gtk.Button()
        show.add_css_class("pg-stat-button")
        content = w.box("h", 5)
        content.append(w.icon("code", 13))
        content.append(w.label("Show the change"))
        show.set_child(content)
        show.connect("clicked", lambda _b: ctx.sheet("code", goal_id=ctx.goal_id))
        stats.append(show)
    if failed or files or data.get("spend_text"):
        card.append(stats)
    card.append(revealer)
    if failed and data.get("error"):
        body = card.body()
        body.append(w.label(str(data["error"]), "pg-card-text", wrap=True, selectable=True))
    return card


def _every_second(widget: Gtk.Widget, callback) -> None:
    """Run `callback` once a second while `widget` is on screen, and never after it's gone."""
    source = [0]

    def start(*_args) -> None:
        if not source[0]:
            source[0] = GLib.timeout_add_seconds(1, callback)

    def stop(*_args) -> None:
        if source[0]:
            GLib.source_remove(source[0])
            source[0] = 0
    widget.connect("map", start)
    widget.connect("unmap", stop)
    widget.connect("destroy", stop)


def _keep_pulsing(bar: Gtk.ProgressBar) -> None:
    source = [0]

    def pulse() -> bool:
        bar.pulse()
        return GLib.SOURCE_CONTINUE

    def start(*_args) -> None:
        if not source[0]:
            source[0] = GLib.timeout_add(120, pulse)

    def stop(*_args) -> None:
        if source[0]:
            GLib.source_remove(source[0])
            source[0] = 0
    bar.connect("map", start)
    bar.connect("unmap", stop)
    bar.connect("destroy", stop)
