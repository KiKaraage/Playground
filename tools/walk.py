#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Development harness: walk Playground the way a person would, inside the real app process.

    vm-run.sh python3 tools/walk.py WALK --home <projects root> --out <dir> [--dark] [--size 1440x900]
                                         [--only name,name] [--keep]

A walk is a list of steps. Each step does something a person does (a click is the widget's own
`clicked`/`activate`/`row-activated`, typing goes into the real entry or text view), waits, saves a
PNG of the window (open popovers included), and collects what went wrong meanwhile: Python
exceptions, warnings logged by Playground, and GTK/GLib criticals and warnings from stderr.
At the end it writes <out>/report.txt and exits 1 if anything went wrong.

Run it in the VM (GTK doesn't run on the Mac host). PLAYGROUND_HOME is set from --home, and the
per-device folders (connections, usage, window memory) go to a scratch folder under --out, so a
walk never touches real settings. Use a scratch copy of a sample as the home: walks write to it.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

NOISE = re.compile(r"a11y|accessibility bus|MESA|libEGL|dri2|AT-SPI|Gdk-Message.*Unable to load|"
                   r"Failed to load module \"(canberra|xapp)|GtkSourceView.*language|dconf|"
                   r"Gsk-Message|^\s*$|Theme parser warning.*-gtk-icon-size|MemoryPressureMonitor|"
                   r"Couldn.t connect to accessibility|"
                   # GTK's cairo renderer (Xvfb only): an overlay scrollbar fading out after a short thread is
                   # swapped for a long one. Not Playground's drawing; the GL renderer on Luma doesn't do it.
                   r"pixman_region32_init_rect|_pixman_log_error|^\*\*\* BUG \*\*\*$")


class Harness:
    def __init__(self, args) -> None:
        self.args = args
        self.out = Path(args.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.steps: list[tuple[str, Callable, int, bool]] = []
        self.results: list[dict] = []
        self.errors: list[str] = []
        self.index = 0
        self.app = None
        self.started = time.monotonic()

    # ── finding things ───────────────────────────────────────────────────
    @property
    def win(self):
        return self.app.props.active_window

    def walk_tree(self, root=None):
        from gi.repository import Gtk  # noqa: F401
        stack = [root or self.win]
        while stack:
            widget = stack.pop()
            yield widget
            child = widget.get_last_child()
            while child is not None:
                stack.append(child)
                child = child.get_prev_sibling()

    def texts(self, widget) -> list[str]:
        from gi.repository import Gtk
        found = []
        for item in self.walk_tree(widget):
            if isinstance(item, Gtk.Label) and item.get_mapped():
                found.append(item.get_text())
        return found

    def find(self, kind=None, *, text: str | None = None, tooltip: str | None = None, css: str | None = None,
             root=None, visible: bool = True, nth: int = 0, exact: bool = False):
        from gi.repository import Gtk
        matches = []
        for item in self.walk_tree(root):
            if kind is not None and not isinstance(item, kind):
                continue
            if visible and not item.get_mapped():
                continue
            if css is not None and not item.has_css_class(css):
                continue
            if tooltip is not None and tooltip not in (item.get_tooltip_text() or ""):
                continue
            if text is not None:
                words = [item.get_text()] if isinstance(item, Gtk.Label) else self.texts(item)
                if exact and text not in words:
                    continue
                if not exact and not any(text in w for w in words):
                    continue
            matches.append(item)
        if len(matches) <= nth:
            raise LookupError(f"nothing like kind={getattr(kind, '__name__', kind)} text={text!r} "
                              f"tooltip={tooltip!r} css={css!r}")
        # The innermost match: a label's own button, not the window around it.
        return matches[nth] if kind is not None else matches[-1]

    def button(self, text: str | None = None, *, tooltip: str | None = None, root=None, nth: int = 0):
        from gi.repository import Gtk
        return self.find(Gtk.Button, text=text, tooltip=tooltip, root=root, nth=nth) if text or tooltip else None

    def innermost_button(self, text: str, root=None):
        """The smallest button whose own labels say `text`."""
        from gi.repository import Gtk
        best = None
        for item in self.walk_tree(root):
            if isinstance(item, Gtk.Button) and item.get_mapped() and any(text in w for w in self.texts(item)):
                best = item
        if best is None:
            raise LookupError(f"no button saying {text!r}")
        return best

    def click(self, widget) -> None:
        from gi.repository import Gtk
        if isinstance(widget, Gtk.ListBoxRow):
            parent = widget.get_parent()
            parent.emit("row-activated", widget)
        elif isinstance(widget, Gtk.CheckButton):
            widget.set_active(not widget.get_active())
        elif isinstance(widget, Gtk.Button):
            if not widget.get_sensitive():
                raise RuntimeError(f"{widget} isn’t clickable now")
            widget.emit("clicked")
        else:
            widget.activate()

    def press(self, text: str, root=None) -> None:
        self.click(self.innermost_button(text, root))

    def type_into(self, widget, words: str) -> None:
        from gi.repository import Gtk
        if isinstance(widget, Gtk.TextView):
            widget.get_buffer().set_text(words)
        elif isinstance(widget, Gtk.Editable):
            widget.set_text(words)
        widget.grab_focus()

    def sheet(self):
        from luma_playground.app.sheets import common
        return common.showing(self.win)

    def flow(self):
        """Where New project's steps are: a sheet, or the conversation itself (v73)."""
        sheet = self.sheet()
        return sheet.card if sheet is not None else self.win.conversation

    def close_sheets(self) -> None:
        from luma_playground.app.sheets import common
        for _ in range(4):
            sheet = common.showing(self.win)
            if sheet is None:
                break
            sheet.close()
        if self.win._page is not None:
            self.win.hide_page(self.win._page)

    def popdown(self) -> None:
        from gi.repository import Gtk
        for item in list(self.walk_tree()):
            if isinstance(item, Gtk.Popover) and item.get_mapped():
                item.popdown()

    # ── running ──────────────────────────────────────────────────────────
    def step(self, name: str, wait: int = 900, shot: bool = True):
        def add(fn):
            self.steps.append((name, fn, wait, shot))
            return fn
        return add

    def start(self) -> None:
        from gi.repository import GLib
        only = set(filter(None, (self.args.only or "").split(",")))
        if only:
            keep = [s for s in self.steps if s[0] in only or s[0].split("-")[0] in only or s[0].startswith("_")]
            self.steps = keep
        GLib.timeout_add(int(self.args.settle), self._next)

    def _next(self) -> bool:
        from gi.repository import GLib
        if self.index >= len(self.steps):
            self.finish()
            return False
        name, fn, wait, shot = self.steps[self.index]
        self.index += 1
        # If a step never comes back (the main loop is blocked), every thread's stack goes into the log.
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
        faulthandler.dump_traceback_later(60 + wait / 1000, repeat=False, file=self.hang_log)
        began = time.monotonic()
        error = None
        try:
            fn(self)
        except Exception:  # noqa: BLE001 - the harness reports, it never stops halfway
            error = traceback.format_exc(limit=6)
        spent = time.monotonic() - began

        def after() -> bool:
            path = None
            if shot and not name.startswith("_"):
                path = self.out / f"{name}.png"
                from luma_playground.app import snapshot
                ok = False
                for _try in range(3):
                    try:
                        ok = snapshot.render_to_png(self.win, str(path))
                    except Exception as snap_error:  # noqa: BLE001
                        print(f"      (snapshot: {snap_error!r})", flush=True)
                        ok = False
                    if ok:
                        break
                    pump(300)   # no render node yet (a transition mid-way): let a frame be drawn first
                if not ok:
                    path = None
                if os.environ.get("PG_XSHOT"):
                    # What X really shows (popovers where GTK put them), for checking the composite.
                    import subprocess
                    subprocess.run(["gst-launch-1.0", "-q", "ximagesrc", "num-buffers=1", "use-damage=false", "!",
                                    "videoconvert", "!", "pngenc", "!", "filesink",
                                    f"location={self.out / (name + '-x.png')}"], timeout=20)
            problems = self.collect()
            if error:
                problems.insert(0, "STEP FAILED: " + error.strip())
            self.results.append({"name": name, "shot": path, "problems": problems, "seconds": spent})
            mark = "ok " if not problems else "ERR"
            print(f"[{mark}] {name} ({spent * 1000:.0f} ms){'' if path else ' (no shot)'}", flush=True)
            for problem in problems:
                print("      " + problem.replace("\n", "\n      "), flush=True)
            GLib.idle_add(self._next, priority=GLib.PRIORITY_DEFAULT)
            return False
        GLib.timeout_add(wait, after)
        return False

    def collect(self) -> list[str]:
        problems, self.errors = self.errors, []
        sys.stderr.flush()
        try:
            with open(self.stderr_path, encoding="utf-8", errors="replace") as handle:
                handle.seek(self.stderr_seen)
                fresh = handle.read()
                self.stderr_seen = handle.tell()
        except OSError:
            fresh = ""
        for line in fresh.splitlines():
            if NOISE.search(line):
                continue
            if re.search(r"critical|warning|traceback|error|exception", line, re.I):
                problems.append("stderr: " + line.strip())
        return problems

    def finish(self) -> None:
        lines = [f"Walk {self.args.walk} · {len(self.results)} steps · {time.monotonic() - self.started:.1f} s", ""]
        bad = 0
        for result in self.results:
            lines.append(f"{'OK ' if not result['problems'] else 'ERR'} {result['name']} "
                         f"({result['seconds'] * 1000:.0f} ms)")
            for problem in result["problems"]:
                lines.append("    " + problem.replace("\n", "\n    "))
            bad += bool(result["problems"])
        lines.append("")
        lines.append(f"{bad} step(s) with problems")
        (self.out / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"report: {self.out / 'report.txt'} · {bad} step(s) with problems", flush=True)
        self.exit_code = 1 if bad else 0
        win = self.win
        if win is not None:
            win.close()
        self.app.quit()


def capture_errors(harness: Harness) -> None:
    """Python exceptions in callbacks and Playground's logged warnings land in the step's report;
    stderr (GTK criticals) goes to a file the harness reads after each step."""
    path = Path(harness.out) / "stderr.log"
    handle = open(path, "w", buffering=1, encoding="utf-8")
    real = os.dup(2)
    os.dup2(handle.fileno(), 2)
    sys.stderr = os.fdopen(2, "w", buffering=1, encoding="utf-8", errors="replace", closefd=False)
    harness.stderr_path, harness.stderr_seen, harness.real_stderr = str(path), 0, real
    harness.hang_log = open(Path(harness.out) / "hang.log", "w")

    def hook(kind, value, tb) -> None:
        words = "".join(traceback.format_exception(kind, value, tb, limit=8)).strip()
        harness.errors.append("exception: " + words)
        os.write(real, ("exception: " + words + "\n").encode())
    sys.excepthook = hook

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + "".join(traceback.format_exception(*record.exc_info, limit=6)).strip()
            harness.errors.append(f"log {record.levelname.lower()} {record.name}: {text}")
    logging.getLogger().addHandler(Collect(level=logging.WARNING))
    if os.environ.get("PG_TRACE_CRITICALS"):
        # Which Python line led to a GTK critical (it happens inside a call Playground made).
        from gi.repository import GLib

        def critical(domain, level, message, _data=None) -> None:
            stack = "".join(traceback.format_stack(limit=12)[:-1])
            os.write(real, f"{domain} critical: {message}\n{stack}\n".encode())
        for domain in ("Gtk", "GLib-GObject", "Gdk", "Adw"):
            GLib.log_set_handler(domain, GLib.LogLevelFlags.LEVEL_CRITICAL | GLib.LogLevelFlags.LEVEL_WARNING,
                                 critical, None)


# ── the walks ────────────────────────────────────────────────────────────────

def _goal_ids(h: Harness) -> list[str]:
    return [g["id"] for g in h.win.project.goals()]


def walk_welcome(h: Harness) -> None:
    """A fresh, empty home: the welcome, then a new project from the idea to the first goal."""

    @h.step("01-welcome")
    def _(h):
        assert h.win.project is None, "a project opened in an empty home"

    @h.step("02-welcome-typed")
    def _(h):
        h.type_into(h.win.conversation.welcome.view, "A tool to track my plant watering")

    @h.step("03-new-questions", wait=2500)
    def _(h):
        h.click(h.win.conversation.welcome.start_button)

    @h.step("04-new-answered", wait=2500)
    def _(h):
        for answer in ("Me and my family", "On phones", "Plants, and who watered them"):
            h.press(answer, h.flow())
            pump(500)

    @h.step("05-new-recipe", wait=600)
    def _(h):
        sheet = h.sheet()
        page = sheet.stack.get_visible_child() if sheet is not None else h.win.conversation.scroller
        adjustment = page.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())

    @h.step("06-new-created", wait=3000)
    def _(h):
        h.press("Looks good, create", h.flow())

    @h.step("07-new-agentsmd", wait=900)
    def _(h):
        h.press("Read AGENTS.md", h.flow())

    @h.step("08-first-goal", wait=3000)
    def _(h):
        h.press("Open ", h.flow())

    @h.step("09-first-goal-settled", wait=2000)
    def _(h):
        assert h.win.project is not None, "the new project didn't open"
        goal = h.win.project.goal(h.win.goal_id) or {}
        assert goal.get("title") == "Get a first version on screen", f"opened {goal.get('title')!r}"

    @h.step("09b-talk-to-ari-beginning", wait=1500)
    def _(h):
        h.win.select_goal("talk-to-ari")
        h.win.conversation._follow = False
        pump(300)
        h.win.conversation.scroller.get_vadjustment().set_value(0)

    @h.step("10-new-goal-dialog", wait=900)
    def _(h):
        h.win.new_goal()

    @h.step("11-new-goal-made", wait=1200)
    def _(h):
        from gi.repository import Adw, Gtk
        dialog = h.find(Adw.AlertDialog)
        entry = h.find(Gtk.Entry, root=dialog)
        h.type_into(entry, "Remind me on watering day")
        dialog.emit("response", "create")
        dialog.close()


def walk_kiln(h: Harness) -> None:
    """Kiln, as v73 shows it: every goal, the decision, sending, the Stage, every sheet."""
    from luma_playground.core import samples  # noqa: F401

    @h.step("01-kiln-open", wait=4500)
    def _(h):
        kiln = Path(os.environ["PLAYGROUND_HOME"]) / "Kiln"
        if os.environ.get("PG_PROFILE"):
            # How long opening Kiln holds the main loop, and where the time goes.
            import cProfile
            import io
            import pstats
            profile = cProfile.Profile()
            began = time.monotonic()
            profile.runcall(h.win.open_project, str(kiln))
            spent = time.monotonic() - began
            out = io.StringIO()
            pstats.Stats(profile, stream=out).sort_stats("cumulative").print_stats(35)
            (h.out / "profile-open.txt").write_text(f"open_project held the main loop {spent * 1000:.0f} ms\n\n"
                                                     + out.getvalue(), encoding="utf-8")
        else:
            h.win.open_project(str(kiln))

    for index, goal_id in enumerate(("talk-to-ari", "add-a-waitlist", "fix-reminder-texts",
                                     "studio-photos-on-the-home-page", "sign-in-with-luma-connect", "gift-a-class")):
        @h.step(f"02-goal-{index}-{goal_id.split('-')[0]}", wait=1300)
        def _(h, goal_id=goal_id):
            match = next((g for g in _goal_ids(h) if g == goal_id or g.startswith(goal_id)), None)
            assert match, f"no goal like {goal_id} in {_goal_ids(h)}"
            row = h.win.rail._goal_rows[match]
            h.click(row)
            assert h.win.goal_id == match

    @h.step("03-decision-picked", wait=900)
    def _(h):
        match = next(g for g in _goal_ids(h) if g.startswith("add-a-waitlist"))
        h.click(h.win.rail._goal_rows[match])
        pump(600)
        h.press("An email")

    @h.step("04-decision-answered", wait=1500)
    def _(h):
        h.press("Decide")

    @h.step("05-send-offline", wait=1500)
    def _(h):
        h.win.composer.set_text("Make the Join waitlist button a little bigger on phones")
        h.win.composer.submit()

    @h.step("06-picker-popover", wait=1500)
    def _(h):
        h.win.select_goal(next(g for g in _goal_ids(h) if g.startswith("add-a-waitlist")))
        pump(500)
        h.click(h.win.composer.agent)
        pump(300)
        from gi.repository import Gtk
        if not any(isinstance(p, Gtk.Popover) and p.get_mapped() for p in h.walk_tree()):
            raise AssertionError("the picker didn't open as a popover")

    @h.step("07-picker-add", wait=2500)
    def _(h):
        h.press("Add a connection")

    @h.step("_close-picker", shot=False)
    def _(h):
        h.popdown()
        h.close_sheets()

    @h.step("08-point-on", wait=1500)
    def _(h):
        h.win.point()

    @h.step("09-point-picked", wait=1500)
    def _(h):
        stage = h.win.stage
        if stage.view is None:
            raise AssertionError("the Stage has no web view (is the preview running?)")
        stage.view.evaluate_javascript("(document.querySelector('[data-kp], button, a')||document.body).click()",
                                       -1, None, None, None, None, None)

    @h.step("10-point-composer", wait=600)
    def _(h):
        assert h.win.composer.about, "pointing didn't reach the composer"

    for device in ("tablet", "desktop", "app", "phone"):
        @h.step(f"11-device-{device}", wait=1500)
        def _(h, device=device):
            h.win.stage.devices.set_current(device) if hasattr(h.win.stage.devices, "set_current") else None
            h.win.stage.set_device(device)

    @h.step("12-live", wait=2500)
    def _(h):
        h.win.stage.set_env("live")

    @h.step("13-stage-again", wait=1500)
    def _(h):
        h.win.stage.set_env("stage")

    @h.step("14-open-menu", wait=900)
    def _(h):
        h.click(h.win.stage.open_button)

    @h.step("15-record", wait=2600)
    def _(h):
        h.popdown()
        h.win.stage.toggle_recording()

    @h.step("16-recorded", wait=6000)
    def _(h):
        h.win.stage.toggle_recording()

    @h.step("_clear-composer", shot=False)
    def _(h):
        h.win.composer.attachments = []
        h.win.composer._clear_about()

    for name, options in (("book", {}), ("people", {}), ("places", {}), ("usage", {}),
                          ("review", {"goal_id": "studio-photos-on-the-home-page-9e12"}),
                          ("code", {"goal_id": "add-a-waitlist-7f3a"}),
                          ("newproject", {})):
        @h.step(f"20-sheet-{name}", wait=2600)
        def _(h, name=name, options=options):
            h.close_sheets()
            pump(300)
            h.win.open_sheet(name, **options)

    @h.step("_close", shot=False)
    def _(h):
        h.close_sheets()

    for section in ("overview", "goals", "bugs", "stack", "money", "analytics", "knowledge", "decisions", "brief",
                    "places", "people", "history", "code"):
        @h.step(f"21-space-{section}", wait=2000)
        def _(h, section=section):
            h.win.open_section(section)

    @h.step("22-bug-13", wait=1500)
    def _(h):
        h.close_sheets()
        h.win.open_section("bugs", "13")

    @h.step("23-service-twilio", wait=1500)
    def _(h):
        h.close_sheets()
        h.win.open_section("stack", "twilio")

    @h.step("24-link-13-from-card", wait=2000)
    def _(h):
        h.close_sheets()
        match = next(g for g in _goal_ids(h) if g.startswith("fix-reminder-texts"))
        h.win.select_goal(match)
        pump(500)
        from gi.repository import Gtk
        for label in h.walk_tree(h.win.conversation):
            if isinstance(label, Gtk.Label) and "pg:bugs/" in (label.get_label() or ""):
                uri = re.search(r"pg:bugs/\d+", label.get_label()).group(0)
                label.emit("activate-link", uri)
                return
        raise AssertionError("no #13 link in the Fix reminder texts thread")

    @h.step("25-usage-detail", wait=2000)
    def _(h):
        h.close_sheets()
        h.win.open_sheet("usage", detail="ari")

    @h.step("27-places-add", wait=1500)
    def _(h):
        h.close_sheets()
        h.win.open_sheet("places")
        pump(1200)
        h.press("Add a place", h.sheet().card)
        pump(500)
        h.press("SSH details", h.sheet().card)

    @h.step("27-places-added", wait=14000)
    def _(h):
        from gi.repository import Gtk
        entries = [e for e in h.walk_tree(h.sheet().card) if isinstance(e, Gtk.Entry) and e.get_mapped()]
        for entry, words in zip(entries, ("Test box", "192.0.2.10", "kiln", "22")):
            h.type_into(entry, words)
        h.press("Add", h.sheet().card)

    @h.step("28-places-vault", wait=2500)
    def _(h):
        h.close_sheets()
        h.win.open_sheet("places")
        pump(1500)
        card = h.sheet().card
        h.press("Set up", card) if any("No vault yet" in t for t in h.texts(card)) else None
        pump(1500)
        page = h.sheet().stack.get_visible_child()
        if hasattr(page, "get_vadjustment"):
            page.get_vadjustment().set_value(page.get_vadjustment().get_upper())

    @h.step("29-code-diff", wait=3000)
    def _(h):
        h.close_sheets()
        h.win.open_sheet("code", goal_id=next(g for g in _goal_ids(h) if g.startswith("add-a-waitlist")))

    @h.step("29b-code-unchanged-lines", wait=2500)
    def _(h):
        h.press("kiln.js", h.win.page_island)
        pump(1500)
        from gi.repository import Gtk
        gaps = [b for b in h.walk_tree() if isinstance(b, Gtk.Button) and b.has_css_class("pgs-diff-gap")
                and b.get_mapped()]
        if len(gaps) < 2:
            raise AssertionError(f"kiln.js should show unchanged lines between its changes (found {len(gaps)})")
        if any("@@" in w for w in h.texts(h.sheet().card if hasattr(h.sheet(), "card") else h.win)):
            raise AssertionError("a raw @@ hunk header is showing")

    @h.step("29c-code-unchanged-lines-opened", wait=2500)
    def _(h):
        from gi.repository import Gtk
        gaps = [b for b in h.walk_tree() if isinstance(b, Gtk.Button) and b.has_css_class("pgs-diff-gap")
                and b.get_mapped()]
        h.click(gaps[1] if len(gaps) > 1 else gaps[0])
        pump(600)
        after = [b for b in h.walk_tree() if isinstance(b, Gtk.Button) and b.has_css_class("pgs-diff-gap")
                 and b.get_mapped()]
        if len(after) != len(gaps) - 1:
            raise AssertionError(f"opening unchanged lines left {len(after)} of {len(gaps)} separators")

    @h.step("30-code-new-file", wait=2500)
    def _(h):
        h.press("waitlist.js", h.win.page_island)

    @h.step("_close2", shot=False)
    def _(h):
        h.close_sheets()

    @h.step("31-goal-menu", wait=900)
    def _(h):
        h.click(h.find(tooltip="More for this goal"))

    @h.step("32-project-menu", wait=900)
    def _(h):
        h.popdown()
        h.click(h.find(tooltip="More for Kiln"))

    @h.step("33-attention", wait=1500)
    def _(h):
        h.popdown()
        link = h.find(css="pg-attention-link")
        h.click(link)


def walk_phone(h: Harness) -> None:
    """Phone width: the conversation, the rail menu, the Stage sheet, the picker drawer."""

    @h.step("01-phone-open", wait=4000)
    def _(h):
        h.win.open_project(str(Path(os.environ["PLAYGROUND_HOME"]) / "Kiln"))

    @h.step("02-phone-rail", wait=1200)
    def _(h):
        h.win.island.grow_into()

    @h.step("03-phone-goal", wait=1200)
    def _(h):
        match = next(g for g in _goal_ids(h) if g.startswith("fix-reminder-texts"))
        h.click(h.win.rail._goal_rows[match])

    @h.step("_overflow", shot=False, wait=300)
    def _(h):
        for goal_id in _goal_ids(h):
            h.win.select_goal(goal_id)
            pump(300)
            before = len(h.errors)
            overflow(h)
            for index in range(before, len(h.errors)):
                h.errors[index] = f"[{goal_id}] " + h.errors[index]
        match = next(g for g in _goal_ids(h) if g.startswith("fix-reminder-texts"))
        h.win.select_goal(match)

    @h.step("04-phone-stage", wait=2500)
    def _(h):
        h.win.show_stage()

    @h.step("05-phone-stage-closed", wait=1200)
    def _(h):
        h.win.hide_stage()

    @h.step("06-phone-picker", wait=1500)
    def _(h):
        h.click(h.win.composer.agent)

    @h.step("07-phone-book", wait=2000)
    def _(h):
        h.close_sheets()
        h.win.open_sheet("book")

    @h.step("08-phone-book-stack", wait=2000)
    def _(h):
        h.close_sheets()
        h.win.open_section("stack")


def overflow(h: Harness, root=None) -> None:
    """Fail the step for every card that asks to be wider than the thread has room for (phone width),
    naming the innermost widget that forces it."""
    from gi.repository import Gtk
    win = h.win
    room = min(win.conversation.scroller.get_width(), 380)
    items = root or win.conversation.thread.items
    problems = []

    def culprit(widget):
        best = widget
        while True:
            child, widest = best.get_first_child(), None
            while child is not None:
                if child.get_visible() and child.measure(Gtk.Orientation.HORIZONTAL, -1)[0] > room - 40:
                    widest = child
                child = child.get_next_sibling()
            if widest is None:
                return best
            best = widest

    child = items.get_first_child()
    while child is not None:
        minimum = child.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        if child.get_visible() and minimum > room - 40:
            inner = culprit(child)
            words = " | ".join(h.texts(inner))[:120]
            problems.append(f"{type(child).__name__}.{'.'.join(child.get_css_classes())} needs {minimum}px "
                            f"(room {room}); innermost: {type(inner).__name__}.{'.'.join(inner.get_css_classes())} "
                            f"min {inner.measure(Gtk.Orientation.HORIZONTAL, -1)[0]}: {words}")
        child = child.get_next_sibling()
    h.errors.extend("too wide: " + p for p in problems)


def walk_states(h: Harness) -> None:
    """Run with --connections: the picker as v73's popover with real connections, the fence switch, a
    connection's page with its cap, and every kind of card an agent leaves when it stops."""
    from luma_playground.core.model import ThreadEntry

    def waitlist(h) -> str:
        return next(g for g in _goal_ids(h) if g.startswith("add-a-waitlist"))

    @h.step("01-states-open", wait=4000)
    def _(h):
        h.win.open_project(str(Path(os.environ["PLAYGROUND_HOME"]) / "Kiln"))
        pump(1500)
        h.win.select_goal(waitlist(h))

    @h.step("02-picker-connections", wait=2500)
    def _(h):
        h.click(h.win.composer.agent)
        pump(400)
        from gi.repository import Gtk
        shown = [p for p in h.walk_tree() if isinstance(p, Gtk.Popover)]
        if not any(p.get_mapped() for p in shown):
            raise AssertionError(f"the picker didn't open as a popover ({len(shown)} popovers, "
                                 f"{[p.get_visible() for p in shown]} visible)")

    @h.step("03-picker-claude-fenced", wait=2500)
    def _(h):
        h.popdown()
        # Claude works on the waitlist now: the picker shows "Keep it inside the project" under it.
        goal = waitlist(h)
        h.win.project.store.update_goal(goal, lambda g: setattr(g, "connection", "claude"))
        h.win._sync_now()
        h.win.select_goal(goal)
        pump(600)
        h.click(h.win.composer.agent)

    @h.step("04-usage-claude", wait=2500)
    def _(h):
        h.popdown()
        h.win.open_sheet("usage", detail="claude")

    @h.step("04-usage-claude-cap", wait=2500)
    def _(h):
        from gi.repository import Gtk
        scale = h.find(Gtk.Scale, root=h.sheet().card)
        scale.set_value(20)
        pump(1600)          # it saves once the slider rests
        from luma_playground.agents.connections import ConnectionStore
        saved = ConnectionStore().get("claude")
        if saved is None or int(saved.team_task_cap) != 20:
            raise AssertionError(f"the cap didn't save (it says {getattr(saved, 'team_task_cap', None)})")

    @h.step("_back-to-ari", shot=False)
    def _(h):
        h.close_sheets()
        goal = waitlist(h)
        h.win.project.store.update_goal(goal, lambda g: setattr(g, "connection", "ari"))
        h.win._sync_now()

    cards = [
        ("05-card-offline", "Playground can’t reach the internet, so Codex can’t talk to its model. Check your "
         "connection, then Try again.", {"error": True, "reason": "offline", "action": "retry",
                                         "retry_label": "Try again", "detail": "getaddrinfo ENOTFOUND api.openai.com"}),
        ("06-card-sign-in", "Claude needs you to sign in again. Playground opens the agent’s own sign-in; it never "
         "sees your password. When you’re done, Try again picks up right here.",
         {"action": "sign-in", "reason": "sign-in", "retry_label": "Try again",
          "methods": [{"id": "claude-ai-login", "name": "Claude Subscription", "type": "terminal"}]}),
        ("07-card-usage-limit", "You’ve reached your ChatGPT plan’s limit for now. It resets at 3:00 PM.",
         {"error": True, "reason": "usage-limit", "action": "manage-usage", "secondary": "retry",
          "url": "https://chatgpt.com/settings/usage", "retry_label": "Try again"}),
        ("08-card-resolved", "Codex stopped partway through. Its conversation is saved, so Try again picks up where "
         "it left off.", {"error": True, "reason": "crashed", "resolved": True}),
    ]
    for name, text, data in cards:
        @h.step(name, wait=2000)
        def _(h, text=text, data=data):
            h.win.select_goal(waitlist(h))
            h.win.project.append(waitlist(h), ThreadEntry(kind="message", role="system", by="playground",
                                                          text=text, data=data))
            h.win._sync_now()

    @h.step("09-offline-state", wait=1200)
    def _(h):
        from luma_playground.core import events
        events.bus.publish(events.AGENT_STATE, project=str(h.win.project.path), goal_id=waitlist(h),
                           state="offline", connection="codex")


def walk_burst(h: Harness) -> None:
    """An agent writing fast (a step updated 60 times in 3 s, from another thread, through the store as the
    runner does): the window keeps up without redrawing on every line, and the main loop stays free."""
    from luma_playground.core.model import ThreadEntry

    @h.step("01-burst-open", wait=4000)
    def _(h):
        h.win.open_project(str(Path(os.environ["PLAYGROUND_HOME"]) / "Kiln"))
        pump(1500)
        h.win.select_goal(next(g for g in _goal_ids(h) if g.startswith("add-a-waitlist")))

    @h.step("02-burst", wait=5000)
    def _(h):
        import threading
        win = h.win
        counts = {"sync": 0, "rebuilt": 0}
        real_sync = win._sync_now

        def counting() -> bool:
            counts["sync"] += 1
            return real_sync()
        win._sync_now = counting
        h.counts = counts
        goal = next(g for g in _goal_ids(h) if g.startswith("add-a-waitlist"))
        store = win.project.store
        h.burst_began = time.monotonic()
        h.gaps: list[float] = []

        def tick() -> bool:          # how long the main loop goes without getting back to us
            now = time.monotonic()
            if getattr(h, "last_tick", None) is not None:
                h.gaps.append(now - h.last_tick)
            h.last_tick = now
            return time.monotonic() - h.burst_began < 4.5
        from gi.repository import GLib
        GLib.timeout_add(20, tick)

        def write() -> None:
            for index in range(60):
                calls = [{"title": f"Step {n + 1}", "kind": "edit", "status": "completed" if n < index else
                          "in_progress"} for n in range(min(index + 1, 8))]
                store.append_thread(goal, ThreadEntry(kind="step", role="agent", by="ari", id="burst-step",
                                                      data={"title": "Making the waitlist offer", "state":
                                                            "running" if index < 59 else "done",
                                                            "tool_calls": calls, "elapsed_s": index}))
                time.sleep(0.05)
        threading.Thread(target=write, daemon=True).start()

    @h.step("03-burst-settled", wait=500)
    def _(h):
        worst = max(h.gaps or [0])
        print(f"      burst: {h.counts['sync']} redraws for 60 writes; longest main-loop stall {worst * 1000:.0f} ms",
              flush=True)
        if h.counts["sync"] > 40:
            raise AssertionError(f"{h.counts['sync']} redraws for 60 writes: a redraw storm")


WALKS = {"burst": walk_burst, "welcome": walk_welcome, "kiln": walk_kiln, "phone": walk_phone, "states": walk_states}


def pump(milliseconds: int) -> None:
    """Let the main loop run for a while inside a step (for things that settle)."""
    from gi.repository import GLib
    context = GLib.MainContext.default()
    end = time.monotonic() + milliseconds / 1000
    while time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.01)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("walk", choices=sorted(WALKS))
    parser.add_argument("--home", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--size", default="1440x900")
    parser.add_argument("--only", default="")
    parser.add_argument("--settle", type=int, default=1500)
    parser.add_argument("--connections", action="store_true",
                        help="give this computer the fixture's connections (Ari, Claude, Codex, a local model, a team key) "
                             "and their usage")
    parser.add_argument("--sample", choices=("kiln", "none"), default="none",
                        help="build a fresh sample into --home first (the home is emptied)")
    args = parser.parse_args()

    home = Path(args.home)
    if args.sample != "none":
        if home.exists():
            shutil.rmtree(home)
        home.mkdir(parents=True)
        from luma_playground.core import samples
        samples.create_sample_kiln(home)
    home.mkdir(parents=True, exist_ok=True)
    device = Path(tempfile.mkdtemp(prefix="walk-device-", dir=args.out if Path(args.out).exists() else None))
    os.environ.update({"PLAYGROUND_HOME": str(home), "PLAYGROUND_SIZE": args.size,
                       "PLAYGROUND_CONFIG_DIR": str(device / "config"), "PLAYGROUND_DATA_DIR": str(device / "data"),
                       "XDG_STATE_HOME": str(device / "state"), "PLAYGROUND_DARK": "1" if args.dark else "0"})

    if args.connections:
        (device / "config").mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "tools" / "fixtures" / "connections.toml", device / "config" / "connections.toml")
        # What this computer knows about them: headroom readings and this month's runs (v73's picker numbers).
        from luma_playground.core import samples as _samples
        _samples.create_sample_device_usage()
    harness = Harness(args)
    capture_errors(harness)
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib  # noqa: F401
    from luma_playground.app import application as app_module

    app = app_module.PlaygroundApplication()
    harness.app = app
    harness.exit_code = 1
    WALKS[args.walk](harness)
    app.connect("activate", lambda _a: harness.start())
    app.run([sys.argv[0]])
    shutil.rmtree(device, ignore_errors=True)
    os.dup2(harness.real_stderr, 2)
    return harness.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
