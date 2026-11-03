# SPDX-License-Identifier: Apache-2.0
"""Review and release: Live and Stage side by side (v73 shot 03).

Left, what people have now (Live). Middle, what they will have once this goal is
released (Stage). Right, in plain words: what changes for people, what happens
to the data, the checks, and the decisions. At the foot, where it goes and the
Release button, which is enabled only when every check passes and nothing is
waiting on a decision.

Release goes through the protected path in core.places: an owner or reviewer
approves, a single-use ticket is issued, and ``places.release`` saves a restore
point (a git tag, plus a copy of the data when the recipe says how), copies the
release with the place's ``[deploy]`` recipe and checks the live address
answers. The bar shows those three steps as they happen, then either "It's
live" or the step it stopped at, with Try again. A recipe this computer hasn't
run before is shown to the person first. Agents can never do any of this; only
a person here can.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gi.repository import GLib, Gtk

from . import common as c
from .common import text

PHONE_FRAME = (236, 486)        # v73 .pgside2 .pgdevwrap: a 391 x 806 phone at 0.6036
PHONE_ZOOM = 236 / 391


def present(window: Gtk.Window, **options) -> c.Sheet:
    return ReviewPage(window, options).sheet


class ReviewPage:
    def __init__(self, window: Gtk.Window, options: dict) -> None:
        self.window = window
        self.ctx = c.context_for(window, options)
        self.state: dict[str, Any] | None = None
        self.run: dict[str, Any] | None = None       # the release in progress, or how it ended
        # Dev hook for shots: press Release once loaded ("confirm" stops at the recipe question).
        self.auto_release = str(options.get("autorelease") or "")
        self.views: list[Any] = []
        self.signature: tuple | None = None          # what the page was last drawn from
        self.spacer: Gtk.Widget | None = None
        self.head: Gtk.Box | None = None
        self.grid: Gtk.Grid | None = None
        self.grid_key: tuple | None = None
        self.facts_widget: Gtk.Widget | None = None
        back = options.get("back_label") or c.goal_title(self.ctx) or "Back"
        self.sheet = c.Sheet(window, "Review", "", size="page", name="review", back_label=back)
        self.sheet.on_layout.append(lambda *_a: self.render())
        for topic in ("goal.changed", "thread.appended", "decision.pending", "decision.answered", "preview.state",
                      "places.changed"):
            self.sheet.listener.on(topic, lambda **_: self.load())
        self.sheet.on_close.append(self.stop_views)
        self.body = c.vbox(vexpand=True)
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.content = c.vbox(css="pgs-review")
        clamp = c.Adw.Clamp(maximum_size=1120, tightening_threshold=1000, child=self.content)
        self.scroll.set_child(clamp)
        self.bar_slot = c.vbox()
        overlay = Gtk.Overlay(vexpand=True)
        overlay.set_child(self.scroll)
        self.bar_slot.set_valign(Gtk.Align.END)
        self.bar_slot.set_halign(Gtk.Align.CENTER)
        overlay.add_overlay(self.bar_slot)
        self.body.append(overlay)
        self.sheet.set_body(self.body, scroll=False)
        self.sheet.present()
        self.render()
        self.load()

    # ── reading ──

    def load(self) -> None:
        if self.ctx.project_dir is None or not self.ctx.goal_id:
            self.state = {"missing": True}
            self.render()
            return
        project_dir, goal_id = self.ctx.project_dir, self.ctx.goal_id

        def read() -> dict:
            store = c.module("core.store").ProjectStore(project_dir)
            goal = store.read_goal(goal_id)
            thread = store.read_thread(goal_id)
            review = next((e for e in reversed(thread) if e.kind == "review"), None)
            proof = next((e for e in reversed(thread) if e.kind == "proof"), None)
            pending = [p for p in store.list_pending(kind="decision") if getattr(p, "goal", "") == goal_id]
            decisions = [d for d in store.read_decisions() if d.goal == goal_id]
            project = store.read_project()
            preview = c.module("core.preview")
            sides = preview.stage_and_live(project_dir) if preview else {"stage": {}, "live": None}
            places = c.module("core.places")
            wanted = review.data.get("destination") if review is not None else None
            plan = places.release_plan(project_dir, wanted or None) if places and hasattr(places, "release_plan") \
                else None
            return {"goal": goal, "review": review, "proof": proof, "pending": pending, "decisions": decisions,
                    "project": project, "sides": sides, "plan": plan}

        def done(state: dict) -> None:
            self.state = state
            # Events arrive in bursts (a release writes the goal, the thread and History): redraw the page,
            # and make its WebViews again, only when something on it changed; otherwise just the bar.
            if self.signature is not None and self.signature == self.content_signature():
                self.render_bar()
            else:
                self.render()
            if self.auto_release:
                trust, self.auto_release = self.auto_release != "confirm", ""
                GLib.timeout_add(600, lambda: (self.release(trust=trust), False)[1])

        c.run_async(read, done, lambda error: self.fail(str(error)))

    def fail(self, words: str) -> None:
        self.state = {"error": words}
        self.render()

    # ── what the facts say ──

    def checks(self) -> list[dict]:
        """The goal's checks with their latest results: the proof card's run, else the goal file."""
        state = self.state or {}
        proof = state.get("proof")
        if proof is not None and proof.data.get("checks"):
            return [dict(item) for item in proof.data["checks"]]
        goal = state.get("goal")
        return [check.to_dict() for check in goal.checks] if goal is not None else []

    def destination(self) -> dict | None:
        state = self.state or {}
        project = state.get("project")
        review = state.get("review")
        wanted = review.data.get("destination") if review is not None else None
        if project is None:
            return None
        production = [p for p in project.places if p.role == "production"]
        place = project.place(wanted) if wanted else (production[0] if production else None)
        return place.to_dict() if place is not None else None

    def readiness(self) -> tuple[bool, str]:
        state = self.state or {}
        goal = state.get("goal")
        if goal is not None and goal.status.value == "live":
            return False, "A restore point was saved first. Going back is one tap in History."
        checks = self.checks()
        if state.get("pending"):
            return False, "One decision is still open" if len(state["pending"]) == 1 else \
                f"{len(state['pending'])} decisions are still open"
        if not checks:
            return False, "There are no checks yet, so nothing proves it works"
        failing = [ch for ch in checks if ch.get("status") != "pass"]
        if failing:
            return False, "The checks haven’t all passed on this version"
        review = state.get("review")
        if review is not None and review.data.get("ready") is False:
            return False, "The agent hasn’t said it’s ready yet"
        plan = state.get("plan") or {}
        if plan.get("problem"):
            return False, plan["problem"]
        return True, "A restore point is saved first, so you can go back"

    # ── drawing ──

    def content_signature(self) -> tuple:
        state = self.state or {}
        goal, review, proof = state.get("goal"), state.get("review"), state.get("proof")
        sides = state.get("sides") or {}
        return (bool(state.get("missing")), state.get("error"), goal and (goal.status.value, goal.title),
                review and (review.id, review.ts), proof and (proof.id, proof.ts), len(state.get("pending") or []),
                len(state.get("decisions") or []), (sides.get("live") or {}).get("url"),
                (sides.get("stage") or {}).get("url"), self.sheet.phone, self.sheet.compact)

    def render(self) -> None:
        self.signature = self.content_signature() if self.state is not None else None
        c.clear(self.bar_slot)
        state = self.state
        message = None
        if state is None:
            message = c.vbox(c.shared.Spinner(18), text("Opening the review…", "pgs-row-sub", xalign=0.5),
                             spacing=10, css="pgs-empty")
        elif state.get("missing"):
            message = c.empty("Pick a conversation to review", "Review opens from a conversation whose work is ready.", "eye")
        elif state.get("error"):
            message = c.empty("The review couldn’t be read", state["error"], "eye")
        if message is not None:
            self.grid = None
            self.stop_views()
            c.clear(self.content)
            self.content.append(message)
            return
        goal = state["goal"]
        live = goal.status.value == "live"
        sides = state.get("sides") or {}
        live_url, stage_url = (sides.get("live") or {}).get("url"), (sides.get("stage") or {}).get("url")
        layout = "phone" if self.sheet.phone else ("compact" if self.sheet.compact else "wide")
        shot = self.latest_shot()
        grid_key = (live_url, stage_url, layout, str(shot) if shot else "")
        if self.grid is None or self.grid_key != grid_key or self.grid.get_parent() is not self.content:
            # The Live and Stage previews are made only when what they show changes: WebViews moved or
            # made again on every redraw flicker, lose their place, and a moved one stays blank until it repaints.
            self.stop_views()
            c.clear(self.content)
            self.head = c.vbox()
            self.content.append(self.head)
            self.grid = Gtk.Grid(column_spacing=16, row_spacing=16)
            self.grid.set_margin_top(22)
            live_side = self.side("Live", live_url, stage=False)
            stage_side = self.side("Stage", stage_url, stage=True)
            self.grid.attach(live_side, 0, 0, 1, 1)
            self.grid.attach(stage_side, 0 if layout == "phone" else 1, 1 if layout == "phone" else 0, 1, 1)
            self.content.append(self.grid)
            self.spacer = Gtk.Box()
            self.content.append(self.spacer)
            self.grid_key = grid_key
            self.facts_widget = None
        c.clear(self.head)
        pill = text("Released" if live else ("Ready to review" if goal.status.value == "ready" else
                                              goal.status.label), "pgs-pill",
                    "live" if live else ("ready" if goal.status.value == "ready" else "needs"), wrap=False)
        pill.set_halign(Gtk.Align.START)
        self.head.append(pill)
        title = text(goal.title, "pgs-review-title", accessible_role=Gtk.AccessibleRole.HEADING)
        title.set_margin_top(6)
        self.head.append(title)
        self.head.append(text(
            "It’s live. A restore point was saved before it went out." if live else
            "Live is what people have now. Stage shows what they’ll have once you release this work.",
            "pgs-review-lede"))
        if self.facts_widget is not None:
            self.grid.remove(self.facts_widget)
        self.facts_widget = self.facts()
        place = {"phone": (0, 2, 1, 1), "compact": (0, 1, 2, 1), "wide": (2, 0, 1, 1)}[layout]
        self.grid.attach(self.facts_widget, *place)
        self.render_bar()

    def render_bar(self) -> None:
        if self.sheet.closed:
            return
        c.clear(self.bar_slot)
        if self.spacer is not None:
            self.spacer.set_size_request(-1, 300 if self.run else 110)   # room to scroll past the bar
        state = self.state or {}
        goal = state.get("goal")
        if goal is None:
            return
        live = goal.status.value == "live"
        ready, why = self.readiness()
        self.bar_slot.append(self.progress_card() if self.run else self.release_bar(ready and not live, why, live))

    def side(self, name: str, url: str | None, *, stage: bool) -> Gtk.Widget:
        box = c.vbox(spacing=12, css="pgs-side", hexpand=True, valign=Gtk.Align.START)
        host = ""
        if url:
            host = url.split("//", 1)[-1].split("/", 1)[0]
        aside = ("after release" if stage else host) or ("not running" if stage else "nothing live yet")
        dot = c.vbox(css="pgs-side-dot" + (" stage" if stage else ""), valign=Gtk.Align.CENTER)
        box.append(c.hbox(dot, text(name, "pgs-side-head", wrap=False), text(aside, "pgs-side-aside", wrap=False),
                          spacing=8, halign=Gtk.Align.CENTER))
        frame = c.vbox(css="pgs-side-frame", halign=Gtk.Align.CENTER, valign=Gtk.Align.START,
                       hexpand=False, vexpand=False)
        frame.set_size_request(*PHONE_FRAME)
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        content = self.preview(url, stage)
        content.set_vexpand(True)
        content.set_hexpand(False)
        content.set_size_request(PHONE_FRAME[0] - 14, PHONE_FRAME[1] - 14)
        frame.append(content)
        box.append(frame)
        return box

    def preview(self, url: str | None, stage: bool) -> Gtk.Widget:
        if url:
            try:
                import gi
                gi.require_version("WebKit", "6.0")
                from gi.repository import WebKit
                view = WebKit.WebView(vexpand=True, hexpand=True)
                view.set_zoom_level(PHONE_ZOOM)
                view.update_property([Gtk.AccessibleProperty.LABEL], ["Stage" if stage else "Live"])
                view.load_uri(url)
                self.views.append(view)
                return view
            except (ImportError, ValueError):
                pass
        shot = self.latest_shot() if stage else None
        if shot is not None:
            picture = Gtk.Picture.new_for_filename(str(shot))
            picture.set_content_fit(Gtk.ContentFit.COVER)
            picture.set_can_shrink(True)
            picture.set_alternative_text("The last screenshot of Stage")
            return picture
        if stage:
            words = "Stage isn’t running. Start it from the Stage, and the change shows here."
        else:
            words = "Nothing is live yet. Add the live address to the production place in Places."
        label = text(words, "pgs-side-empty", xalign=0.5, max_width_chars=22, justify=Gtk.Justification.CENTER)
        empty = c.vbox(c.glyph("smartphone", 22, "pgs-empty-glyph"), label, spacing=8, valign=Gtk.Align.FILL,
                       vexpand=True, css="pgs-side-blank")
        empty.get_first_child().set_valign(Gtk.Align.END)
        empty.get_first_child().set_vexpand(True)
        label.set_valign(Gtk.Align.START)
        label.set_vexpand(True)
        return empty

    def latest_shot(self) -> Path | None:
        proof = (self.state or {}).get("proof")
        if proof is None or self.ctx.project_dir is None:
            return None
        for relpath in reversed(proof.data.get("shots") or []):
            path = (self.ctx.project_dir / relpath).resolve()
            if path.is_file() and path.is_relative_to(self.ctx.project_dir.resolve()):
                return path
        return None

    def facts(self) -> Gtk.Widget:
        state = self.state or {}
        review = state.get("review")
        data = review.data if review is not None else {}
        column = c.vbox(spacing=12, hexpand=True, valign=Gtk.Align.START)
        column.set_size_request(300, -1)
        changes = data.get("changes_for_people") or []
        column.append(fact_box("What changes for people", None,
                               [("user", item, "") for item in changes] or
                               [("user", "The agent describes this when it proposes the release.", "")]))
        data_changes = data.get("data_changes") or []
        column.append(fact_box("Data", None, [("database", item, "") for item in data_changes] or
                               [("database", "No changes to the data.", "")]))
        checks = self.checks()
        passed = sum(1 for ch in checks if ch.get("status") == "pass")
        count = (f"{passed} of {len(checks)} passed", passed == len(checks) and bool(checks))
        column.append(fact_box("Checks", count, [("check:" + (ch.get("status") or "pending"), ch.get("text", ""),
                                                  ch.get("note") or "") for ch in checks] or
                               [("info", "No checks yet.", "")]))
        rows = []
        for pending in state.get("pending") or []:
            rows.append(("hand", pending.question, "Still open. It’s waiting for you in the conversation."))
        for decision in state.get("decisions") or []:
            rows.append(("check", decision.title, f"{decision.who + ' · ' if decision.who else ''}{decision.chosen}"))
        approved = data.get("approved_by")
        if approved:
            project = state.get("project")
            names = []
            for who in ([approved] if isinstance(approved, str) else list(approved)):
                member = project.member(str(who)) if project is not None else None
                names.append(member.first_name if member else str(who))
            when = c.shared.parse_ts(data.get("approved_at"))
            rows.append(("eye", f"{' and '.join(names)} approved it",
                         c.shared.when_words(when).capitalize() if when else ""))
        if rows:
            column.append(fact_box("Decisions and review", None, rows))
        return column

    def release_bar(self, ready: bool, why: str, live: bool) -> Gtk.Widget:
        title = "It’s live" if live else ("Ready to release" if ready else "Not ready yet")
        words = c.vbox(text(title, "pgs-release-title", wrap=False),
                       text(why, "pgs-release-why", wrap=False, ellipsize=True), spacing=1, hexpand=True,
                       valign=Gtk.Align.CENTER)
        release = c.button("Release", icon="radio-tower", style="key", sensitive=ready, on_click=self.release)
        release.add_css_class("pgs-release-button")
        if not ready:
            release.set_tooltip_text(why)
        bar = c.hbox(self.dest_pill(), words, release, spacing=12, css="pgs-release-bar")
        bar.set_size_request(self.bar_width(), -1)
        return bar

    def dest_pill(self) -> Gtk.Widget:
        place = self.destination()
        if place is not None:
            host = (place.get("url") or "").split("//", 1)[-1].strip("/")
            where = f"{place.get('name', 'Production')}{' · ' + host if host else ''}"
        else:
            where = "This computer"
        pill = c.hbox(c.glyph("radio-tower", 16), text(where, wrap=False, ellipsize=True), spacing=8, css="pgs-dest")
        pill.set_valign(Gtk.Align.CENTER)
        return pill

    def bar_width(self) -> int:
        return min(720, max(320, (self.window.get_width() or 900) - 80))

    def progress_card(self) -> Gtk.Widget:
        """The release as it happens (three steps), then how it ended: live, or the step it stopped at."""
        run = self.run or {}
        phase = run.get("phase")
        place_name = run.get("place_name") or "Production"
        if phase == "done":
            title, words = f"It’s live on {place_name}", run.get("message", "")
        elif phase == "failed":
            title, words = f"Stopped at “{run.get('failed_label') or 'the release'}”", run.get("message", "")
        else:
            title, words = f"Releasing to {place_name}", "Keep Playground open until it’s done."
        head_words = c.vbox(text(title, "pgs-release-title", wrap=False, ellipsize=True),
                            text(words, "pgs-release-why", max_width_chars=60) if words else None,
                            spacing=1, hexpand=True, valign=Gtk.Align.CENTER)
        glyph = c.shared.Spinner(18) if phase == "running" else \
            c.shared.check_mark("pass" if phase == "done" else "fail", 22)
        glyph.set_halign(Gtk.Align.CENTER)
        lead = c.vbox(glyph, valign=Gtk.Align.CENTER)
        lead.set_size_request(22, -1)
        head = c.hbox(lead, head_words, spacing=12)
        head.set_margin_start(6)
        head.set_margin_top(4)
        rows = c.vbox(spacing=8)
        rows.set_margin_start(8)
        rows.set_margin_top(12)
        rows.set_margin_bottom(4)
        for step in run.get("steps", []):
            state = step.get("state", "waiting")
            glyph = c.shared.check_mark({"done": "pass", "failed": "fail", "running": "running"}.get(state, "pending"),
                                        16)
            glyph.set_halign(Gtk.Align.CENTER)
            mark = c.vbox(glyph, valign=Gtk.Align.START)       # one column, whatever the glyph's own width
            mark.set_size_request(18, -1)
            label = text(step["label"], "pgs-fact", wrap=False)
            if state in ("waiting", "skipped"):
                label.set_opacity(0.55)
            if state == "failed":
                # What the tools said (secrets already hidden by core.places), right under the step it stopped at.
                detail = text(run.get("detail") or "", "pgs-fact-small", selectable=True, max_width_chars=80) \
                    if run.get("detail") else None
            else:
                detail = text(step["detail"], "pgs-fact-small", ellipsize=True, wrap=False) \
                    if step.get("detail") else None
            rows.append(c.hbox(mark, c.vbox(label, detail, spacing=1, hexpand=True), spacing=10))
        card = c.vbox(head, rows, spacing=0, css="pgs-release-bar")
        card.set_size_request(self.bar_width(), -1)
        if phase in ("done", "failed"):
            actions = c.hbox(spacing=8, halign=Gtk.Align.END)
            actions.set_margin_top(10)
            if phase == "done":
                if run.get("url"):
                    actions.append(c.button("Open it", icon="external-link", style="plain",
                                            on_click=lambda: self.open_url(run["url"])))
                actions.append(c.button("History", icon="history", style="plain", on_click=self.open_history))
                actions.append(c.button("Done", style="fill", on_click=self.dismiss_run))
            else:
                actions.append(c.button("Close", style="plain", on_click=self.dismiss_run))
                if run.get("retry", True):
                    actions.append(c.button("Try again", icon="rotate-ccw", style="key",
                                            on_click=lambda: self.release(trust=bool(run.get("trust")))))
            card.append(actions)
        return card

    # ── release ──

    def release(self, trust: bool = False) -> None:
        state = self.state or {}
        goal, project = state.get("goal"), state.get("project")
        if self.run and self.run.get("phase") == "running":
            return
        if goal is None or project is None:
            return
        ready, why = self.readiness()
        if not ready:
            c.toast(self.sheet.card, why, kind="warning")
            return
        places = c.module("core.places")
        if places is None or not hasattr(places, "release"):
            c.toast(self.sheet.card, "Playground can’t release from here. Nothing changed.", kind="warning")
            return
        place = self.destination()
        plan = state.get("plan") or {}
        if plan.get("recipe") is not None and not plan.get("trusted") and not trust:
            self.confirm_recipe(plan)
            return
        me = person_id(self.window, project)
        review = state.get("review")
        review_data = {"id": review.id if review is not None and review.id else f"{goal.id}-review", "ready": True,
                       "destination": place.get("id") if place else ""}
        project_dir = self.ctx.project_dir
        place_name = place.get("name") if place else "this computer"
        self.run = {"phase": "running", "place_name": place_name, "trust": trust,
                    "steps": [{"id": "restore", "label": "Saving a restore point", "state": "waiting"},
                              {"id": "copy", "label": f"Copying to {place_name}", "state": "waiting"},
                              {"id": "check", "label": "Checking it’s up", "state": "waiting"}]}

        def on_step(step: str, step_state: str, label: str, detail: str) -> None:
            GLib.idle_add(lambda: (self.step_changed(step, step_state, label, detail), False)[1])

        def run() -> dict:
            ticket = None
            if place is not None and places.is_protected(place):
                ticket = places.authorize_release(project_dir, review_data, place["id"], me)
            return places.release(project_dir, place.get("id") if place else places.THIS_COMPUTER, ticket, goal.id,
                                  approved_by=me, title=goal.title, trust_recipe=trust, on_step=on_step)

        self.render_bar()
        c.run_async(run, self.released, self.release_failed)

    def confirm_recipe(self, plan: dict) -> None:
        """The first release with a recipe (or after it changed): show what will run, and where."""
        place = plan.get("place") or {}
        recipe = plan.get("recipe")
        putting_back = getattr(recipe, "restore_data", "") or "\0"
        lines = "\n".join(f"{where}: {what}" for where, what in plan.get("commands") or []
                          if not what.startswith(putting_back))
        body = f"Playground hasn’t run these from this computer before, or they changed. Releasing runs:\n\n{lines}"
        go = lambda _checked=False: self.release(trust=True)  # noqa: E731
        if c.kit is not None:
            c.kit.DestructiveDialog.ask(self.sheet.card, title=f"Release to {place.get('name') or 'Production'}?",
                                        body=body, action="Release", icon="radio-tower", on_confirm=go)
        else:
            go()

    def step_changed(self, step: str, step_state: str, label: str, detail: str) -> None:
        if not self.run:
            return
        for item in self.run["steps"]:
            if item["id"] == step:
                item.update(state=step_state, label=label, detail=detail)
        self.render_bar()

    def released(self, result: dict) -> None:
        run = self.run or {}
        steps = result.get("steps") or run.get("steps") or []
        self.run = {**run, "phase": "done", "steps": steps, "url": result.get("url") or "",
                    "message": f"Restore point {result.get('restore_point')} is saved, so you can go back from History."}
        for view in self.views[:1] if self.views and self.grid_key and self.grid_key[0] else []:
            try:
                view.reload_bypass_cache()      # Live: what just went out, not what WebKit kept from before
            except Exception:  # noqa: BLE001
                pass
        self.load()

    def release_failed(self, error: BaseException) -> None:
        places = c.module("core.places")
        run = self.run or {"steps": []}
        if places is not None and isinstance(error, getattr(places, "RecipeNotTrusted", ())):
            self.run = None
            self.load()
            return
        step = getattr(error, "step", "") or ("approval" if places is not None and
                                               isinstance(error, getattr(places, "ProductionProtected", ())) else "")
        failed_label = next((s["label"] for s in run["steps"] if s["id"] == step), "")
        if not failed_label:
            failed_label = "Checking the approval" if step == "approval" else "the release"
        for item in run["steps"]:
            if item["id"] == step:
                item["state"] = "failed"
        message = str(error)
        if step == "copy":
            message += " Production may be partly updated: try again, or go back to the last version in History."
        retry = getattr(error, "retry", True) and step != "approval"
        if places is not None and isinstance(error, getattr(places, "ReleaseBusy", ())):
            retry = True
        self.run = {**run, "phase": "failed", "failed_label": failed_label, "message": message,
                    "detail": getattr(error, "detail", "") or "", "retry": retry}
        self.load()

    def dismiss_run(self) -> None:
        self.run = None
        self.render_bar()

    def open_history(self) -> None:
        c.open_sheet(self.window, "book", section="history")

    def open_url(self, url: str) -> None:
        try:
            Gtk.UriLauncher.new(url).launch(self.window, None, None, None)
        except Exception:  # noqa: BLE001
            c.toast(self.sheet.card, url, kind="done")

    def stop_views(self) -> None:
        for view in self.views:
            try:
                view.stop_loading()
            except Exception:  # noqa: BLE001
                pass
        self.views.clear()


def fact_box(title: str, count: tuple[str, bool] | None, rows: list[tuple[str, str, str]]) -> Gtk.Widget:
    box = c.vbox(css="pgs-fact-box")
    head = c.hbox(text(title, "pgs-fact-head", wrap=False, hexpand=True), spacing=8)
    if count is not None:
        words, good = count
        label = text(words, "pgs-fact-count", wrap=False)
        if not good:
            label.add_css_class("wait")
        head.append(label)
    box.append(head)
    for glyph, words, small in rows:
        if glyph.startswith("check:"):
            lead = c.shared.check_mark(glyph.split(":", 1)[1], 16)
        else:
            lead = c.glyph(glyph, 15, "pgs-fact-glyph")
        lead.set_valign(Gtk.Align.START)
        lead.set_hexpand(False)
        lines = c.vbox(text(words, "pgs-fact"), text(small, "pgs-fact-small") if small else None, spacing=1,
                       hexpand=True)
        line = c.hbox(lead, lines, spacing=9)
        line.set_margin_bottom(7)
        box.append(line)
    return box


def person_id(window: Gtk.Window, project) -> str:
    """The member at this computer (the window knows), else the project's owner."""
    holder = getattr(window, "project", None)
    you = getattr(holder, "you", None)
    try:
        who = you() if callable(you) else ""
    except Exception:  # noqa: BLE001
        who = ""
    if who and project.member(who) is not None:
        return who
    return project.owner.id if project.owner else ""


def restore_point(project_dir: Path, store, title: str) -> dict:
    """A git tag on what's about to go out, numbered after the last one in History (core.places does it)."""
    places = c.module("core.places")
    return places.save_restore_point(project_dir, title, store=store)
