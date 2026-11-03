# SPDX-License-Identifier: Apache-2.0
"""The Repository area: this computer's copy and the online one, side by side.

Local is what's here: the branch, the changes since the last commit (each opens in place to show what changed),
and the history, with commits the remote doesn't have yet marked. Online is the copy on GitHub (or wherever the
remote is): its history as of the last check, and the way to its page. The strip at the top says how the two
stand. The bar at the foot is the action center: Commit grows into the message editor (or defers the job to an
agent), Pull and Push move work between the two, and Put it online creates the GitHub repository with GitHub's
own gh tool when it's here, or takes an empty repository's address. Git itself is core/repo.py, never run on
the main loop.
"""
from __future__ import annotations

from gi.repository import GLib, Gtk

from . import common as c
from .common import Row, Tag, callout, text

STATE_WORDS = {"modified": ("Changed", None), "added": ("New", "good"), "untracked": ("New", "good"),
               "deleted": ("Removed", "red"), "renamed": ("Renamed", "violet")}
CHANGES_SHOWN = 60
VIEWS = [("local", "Local"), ("online", "Online")]
FETCH_EVERY_S = 5 * 60


class RepoPages:
    """Methods ProjectSpace gains for the Repository area."""

    repo_busy = ""          # "pull", "push", "fetch", "commit", "online" while git runs
    repo_view = "local"
    repo_open: set = set()  # changed files shown open, by path
    _repo_diffs: dict = {}
    _github: tuple | None = None    # (gh is here and signed in, the account), once asked
    _repo_fetched = -1e9            # when the online copy was last checked quietly (monotonic)
    _repo_fetch_error = ""          # why that check failed, in words

    def _repo(self):
        return c.module("core.repo")

    # ── the page ──

    def section_repository(self, data) -> Gtk.Widget:
        state = getattr(data, "repo", None)
        if state is None or not state.tracked:
            box = c.vbox(self.heading("Repository", "Not tracked yet"))
            box.append(c.ListGroup(Row("Keep a history of every change",
                                       "Playground starts a git repository in the project folder. Every commit is "
                                       "a point you can go back to, and you can put it on GitHub whenever you "
                                       "like.", lead="git-branch"), css="pgs-box"))
            return box
        views = c.segmented(VIEWS, self.repo_view, label="Show",
                            on_change=lambda key: self._set_task("repo_view", key))
        lede = state.branch or "No branch"
        if len(state.branches) > 1:
            lede += f" · {len(state.branches)} branches"
        box = c.vbox(self.heading("Repository", lede, views))
        box.append(self.sync_strip(state))
        self._maybe_fetch(state)
        if self.repo_busy:
            box.append(Row({"pull": "Bringing in the online copy’s changes…", "push": "Sending your commits…",
                            "fetch": "Checking the online copy…", "commit": "Committing…",
                            "online": "Putting it online…"}.get(self.repo_busy, "Working…"),
                           None, lead=c.shared.Spinner(18)))
        if self._repo_fetch_error and state.remote_url:
            box.append(callout(c.escape(self._repo_fetch_error), icon="circle-alert", tone="info"))
        if self.repo_view == "online":
            box.append(self.online_view(state))
        else:
            box.append(self.local_view(state))
        return box

    def sync_strip(self, state) -> Gtk.Widget:
        """This computer ⇄ online, in two tiles and the words between them."""
        unpushed = len(state.unpushed)
        here = " · ".join(filter(None, [
            f"{len(state.changes)} change{'s' if len(state.changes) != 1 else ''}" if state.changes else "Committed",
            f"{unpushed} not pushed" if unpushed and state.remote_url else ""]))
        local = _tile("monitor", "This computer", state.branch or "main", here,
                      lambda: self._set_task("repo_view", "local"), on=self.repo_view == "local")
        if state.remote_url:
            checked = c.shared.parse_ts(state.fetched) if state.fetched else None
            note = f"Checked {c.shared.day_words(checked).lower()}" if checked else "Not checked yet"
            if state.behind:
                note = f"{state.behind} new commit{'s' if state.behind != 1 else ''} to pull"
            if self._repo_fetch_error:
                note = "Couldn’t check it just now"
            online = _tile("globe", "Online", state.where, note, lambda: self._set_task("repo_view", "online"),
                           on=self.repo_view == "online")
        else:
            online = _tile("globe", "Online", "Not online yet", "Put it on GitHub from the bar",
                           self.start_remote, dashed=True)
        if not state.remote_url:
            between, tone = "", ""
        elif state.behind or unpushed:
            parts = []
            if unpushed:
                parts.append(f"↑ {unpushed}")
            if state.behind:
                parts.append(f"↓ {state.behind}")
            between, tone = "  ".join(parts), "amber"
        else:
            between, tone = "In step", "good"
        middle = c.vbox(c.glyph("arrow-left-right", 18), text(between, "pgs-sync-words", wrap=False, xalign=0.5),
                        spacing=4, valign=Gtk.Align.CENTER, css="pgs-sync-middle")
        if tone:
            middle.add_css_class(tone)
        if self.sheet.phone:
            return c.vbox(local, online, spacing=10, css="pgs-sync-strip")      # one above the other
        return c.hbox(local, middle, online, spacing=10, css="pgs-sync-strip")

    def _maybe_fetch(self, state) -> None:
        """Check the online copy quietly when the area opens, at most every few minutes."""
        import time
        if not state.remote_url or self.repo_busy or time.monotonic() - self._repo_fetched < FETCH_EVERY_S:
            return
        self._repo_fetched = time.monotonic()
        repo, root = self._repo(), self.ctx.project_dir

        def failed(error) -> None:
            self._repo_fetch_error = str(error)
            self.render(keep_scroll=True)
        self._repo_fetch_error = ""
        c.run_async(lambda: repo.fetch(root), lambda _r: self.load(), failed)

    def local_view(self, state) -> Gtk.Widget:
        box = c.vbox()
        if state.behind:
            box.append(callout(f"The online copy has {state.behind} commit{'s' if state.behind != 1 else ''} this "
                               "one doesn’t. Pull brings them in.", icon="arrow-down", tone="info"))
        box.append(self.subhead("Changes", f"{len(state.changes)} since the last commit" if state.changes
                                else "Everything is committed"))
        if state.changes:
            rows = [self.change_row(change) for change in state.changes[:CHANGES_SHOWN]]
            if len(state.changes) > CHANGES_SHOWN:
                rows.append(Row(f"{len(state.changes) - CHANGES_SHOWN} more", "Open the files to see them all.",
                                lead="ellipsis", on_activate=self.open_code))
            box.append(c.ListGroup(*rows, css="pgs-box"))
        else:
            box.append(c.ListGroup(Row("Nothing new", "When an agent or you change a file, it shows up here until "
                                       "it’s committed.", lead="check"), css="pgs-box"))
        box.append(self.subhead("History", f"{state.count} commit{'s' if state.count != 1 else ''}"))
        if state.commits:
            box.append(c.ListGroup(*[self.commit_row(commit, state) for commit in state.commits], css="pgs-box"))
        else:
            box.append(c.ListGroup(Row("No commits yet", "Commit the changes above to start the history.",
                                       lead="git-commit-horizontal"), css="pgs-box"))
        door = Row("All the files, and the terminal", self.ctx.display_path, lead="terminal", chevron=True,
                   on_activate=self.open_code)
        door.set_margin_top(18)
        box.append(c.ListGroup(door, css="pgs-box"))
        return box

    def online_view(self, state) -> Gtk.Widget:
        box = c.vbox()
        if not state.remote_url:
            box.append(c.ListGroup(Row("Only on this computer",
                                       "Put it on GitHub to keep a copy online, work on it from another computer, "
                                       "or share it. Put it online, in the bar, makes the repository for you when "
                                       "GitHub’s gh tool is here, or takes an empty one’s address.", lead="globe"),
                                   css="pgs-box"))
            return box
        open_page = c.button("Open on GitHub" if "github.com" in state.where else "Open its page", icon="globe",
                             style="fill", small=True, on_click=lambda: self.open_url(state.web_url)) \
            if state.web_url else None
        if not state.upstream:
            box.append(self.subhead(state.where, state.remote_url, open_page))
            box.append(callout(f"{state.branch} isn’t online yet. Push sends it.", icon="arrow-up", tone="info"))
            return box
        box.append(self.subhead(f"On {state.upstream}",
                                f"as of the last check{' · ' + str(state.behind) + ' new' if state.behind else ''}",
                                open_page))
        if state.remote_commits:
            box.append(c.ListGroup(*[self.commit_row(commit, state, online=True) for commit in state.remote_commits],
                                   css="pgs-box"))
        return box

    def change_row(self, change) -> Gtk.Widget:
        words, tone = STATE_WORDS.get(change.state, ("Changed", None))
        folder, _slash, name = change.path.rpartition("/")
        counts = " ".join(part for part in (f"+{change.added}" if change.added else "",
                                            f"−{change.removed}" if change.removed else "") if part)
        opened = change.path in self.repo_open
        end = c.hbox(text(counts, "pgs-row-end", "pgs-mono", wrap=False) if counts else Gtk.Box(),
                      c.glyph("chevron-down" if opened else "chevron-right", 15, "pgs-chevron"), spacing=8)
        row = Row(name or change.path, folder or None, lead="file-text", tags=[Tag(words, tone)], end=end,
                  on_activate=lambda p=change.path: self.toggle_change(p))
        if not opened:
            return row
        holder = c.vbox(row, css="pgs-change-open")
        cached = self._repo_diffs.get(change.path)
        if cached is None:
            holder.append(Row("Reading the change…", None, lead=c.shared.Spinner(16)))
            self._load_diff(change.path)
        else:
            from .code import DiffBox
            diff_box = DiffBox(getattr(self.sheet, "on_close", []))
            kind, words_, current = cached
            if kind == "diff":
                diff_box.show(words_, current, change.path)
            else:
                diff_box.box.append(text(words_ or "Nothing to show.", "pgs-src-note"))
            scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                        vscrollbar_policy=Gtk.PolicyType.NEVER, child=diff_box.box)
            holder.append(scroll)
        return holder

    def toggle_change(self, path: str) -> None:
        opened = set(self.repo_open)
        opened.symmetric_difference_update({path})
        self.repo_open = opened
        self.render(keep_scroll=True)

    def _load_diff(self, path: str) -> None:
        from .code import read_change
        root = self.ctx.project_dir

        def done(result) -> None:
            self._repo_diffs = {**self._repo_diffs, path: result}
            self.render(keep_scroll=True)
        c.run_async(lambda: read_change(root, path, "HEAD", "diff"), done,
                    lambda error: done(("file", f"The change couldn’t be read: {error}", None)))

    def commit_row(self, commit, state, online: bool = False) -> Gtk.Widget:
        moment = c.shared.parse_ts(commit.when)
        when = c.shared.day_words(moment) if moment else ""
        tags = []
        if not online and commit.hash in state.unpushed:
            tags.append(Tag("Not pushed", "amber"))
        if commit.restore:
            tags.append(Tag(f"Restore point {commit.restore}", "violet"))
        words = [commit.author, when]
        if self.sheet.phone:
            # No room beside the title on a phone: the marks go in the line under it, and the hash goes.
            words += [t.get_label() for t in tags]
            tags = []
        return Row(commit.subject, " · ".join(part for part in words if part), lead="git-commit-horizontal",
                   tags=tags, end=None if self.sheet.phone else text(commit.hash[:7], "pgs-row-end", "pgs-mono",
                                                                     wrap=False))

    def open_code(self) -> None:
        c.open_sheet(self.window, "code", **self.ctx.options)

    # ── the bar ──

    def repo_bar(self) -> None:
        center, kit = self.center, c.kit
        state = getattr(self.data, "repo", None) if self.data else None
        if center is None or kit is None:
            return
        if state is None or not state.tracked:
            center.show_bar([kit.BarAction("git-branch", "Start tracking", primary=True,
                                           on_activate=self.repo_start)])
            return
        idle = not self.repo_busy
        items = []
        if state.changes:
            items.append(kit.BarAction("check", f"Commit {len(state.changes)}", primary=True,
                                       on_activate=self.start_commit, sensitive=idle))
        if state.remote_url:
            items.append(kit.BarAction("arrow-down", "Pull" + (f" {state.behind}" if state.behind else ""),
                                       on_activate=lambda: self.repo_run("pull"), sensitive=idle,
                                       primary=not items and bool(state.behind)))
            push_count = len(state.unpushed)
            items.append(kit.BarAction("arrow-up", "Push" + (f" {push_count}" if push_count else ""),
                                       on_activate=lambda: self.repo_run("push"), sensitive=idle,
                                       primary=not items and bool(push_count)))
            if self.repo_view == "online" and state.web_url:
                items.append(kit.BarAction("globe", None, tooltip="Open its page",
                                           on_activate=lambda: self.open_url(state.web_url)))
            items.append(kit.BarAction("refresh-cw", None, tooltip="Check the online copy", sensitive=idle,
                                       on_activate=lambda: self.repo_run("fetch")))
        else:
            items.append(kit.BarAction("globe", "Put it online", primary=not items,
                                       on_activate=self.start_remote, sensitive=idle))
        center.show_bar(items)

    # ── doing things ──

    def _after(self, words: str = ""):
        def done(_result) -> None:
            self.repo_busy = ""
            self._repo_diffs = {}
            if words:
                c.toast(self.sheet.card, words, kind="done")
            self.load()
        return done

    def _failed(self, error) -> None:
        self.repo_busy = ""
        c.toast(self.sheet.card, str(error), kind="error")
        self.load()

    def _busy(self, what: str) -> None:
        self.repo_busy = what
        self.render(keep_scroll=True)

    def repo_start(self) -> None:
        repo, root = self._repo(), self.ctx.project_dir
        self._busy("commit")
        c.run_async(lambda: repo.start(root), self._after("The project’s history starts here"), self._failed)

    def repo_run(self, what: str) -> None:
        repo, root = self._repo(), self.ctx.project_dir
        words = {"pull": "Up to date with the online copy", "push": "Pushed", "fetch": ""}[what]
        self._busy(what)
        c.run_async(lambda: getattr(repo, what)(root), self._after(words), self._failed)

    def start_commit(self) -> None:
        center, kit, repo = self.center, c.kit, self._repo()
        state = getattr(self.data, "repo", None)
        if center is None or kit is None or state is None:
            return
        summary = Gtk.Entry(text=repo.suggest_message(state.changes), hexpand=True)
        summary.update_property([Gtk.AccessibleProperty.LABEL], ["What changed"])
        count = len(state.changes)

        def commit() -> None:
            line = summary.get_text().strip()
            if not line:
                summary.grab_focus()
                return
            details = (editor.draft or "").strip()
            message = line + (f"\n\n{details}" if details else "")
            root, who = self.ctx.project_dir, self._author()
            editor.clear()
            center.fold()
            self._busy("commit")
            c.run_async(lambda: repo.commit(root, message, fallback=who),
                        lambda short: self._after(f"Committed {short}")(short), self._failed)

        def defer() -> None:
            editor.clear()
            center.fold()
            self.defer_to_agent("Commit the changes", "Look at what changed since the last commit (git status and "
                                "git diff), then commit it with a short message that says what changed and why. "
                                "Split it into separate commits if the changes are about different things. "
                                "Don’t push.")

        editor = kit.ActionEditor("Commit", "check",
                                  summary=f"{count} change{'s' if count != 1 else ''}, every file",
                                  fields=[("Message", summary)], placeholder="More about why (optional)",
                                  tools=[kit.BarAction("sparkles", "Defer to agent", on_activate=defer)],
                                  primary=kit.BarAction("check", "Commit", on_activate=commit))
        center.set_editor(editor)
        center.grow()
        GLib.idle_add(lambda: (summary.grab_focus(), summary.select_region(0, -1), False)[2])

    def start_remote(self) -> None:
        """Put it online: create it on GitHub (gh), or use an empty repository's address."""
        if self._github is None:
            repo = self._repo()

            def known(answer) -> None:
                self._github = answer
                self.start_remote()
            c.run_async(repo.github_cli, known, lambda _e: known((False, "")))
            return
        center, kit, repo = self.center, c.kit, self._repo()
        if center is None or kit is None:
            return
        ready, account = self._github
        paths = c.module("core.paths")
        project = self.data.project if self.data else None
        name = Gtk.Entry(text=paths.slugify(project.name if project else "project", 60), hexpand=True)
        name.update_property([Gtk.AccessibleProperty.LABEL], ["Name"])
        choice = {"mode": "github" if ready else "address", "private": True}
        seen = c.segmented([("private", "Private"), ("public", "Public")], "private", label="Who can see it",
                           on_change=lambda key: choice.update(private=key == "private"))
        seen.set_halign(Gtk.Align.START)
        address = Gtk.Entry(placeholder_text="https://github.com/you/project", hexpand=True)
        address.update_property([Gtk.AccessibleProperty.LABEL], ["Address"])
        hint = text("", "pgs-row-sub")
        hint.set_margin_start(16)
        hint.set_margin_end(16)
        hint.set_margin_top(10)
        hint.set_margin_bottom(14)

        def mode(key: str) -> None:
            choice["mode"] = key
            for widget in (name, seen):
                (widget.get_parent() or widget).set_visible(key == "github")
            (address.get_parent() or address).set_visible(key == "address")
            if key == "github":
                editor.set_summary(f"a new repository on GitHub, as {account}")
                hint.set_text("Made with GitHub’s gh tool, signed in on this computer. Everything committed goes "
                              "up; anything not committed stays here until you commit it.")
            else:
                editor.set_summary("an empty repository you already made")
                hint.set_text("Make it empty (no README or licence), then paste its address. Playground checks it’s "
                              "empty before pushing, so nothing already there is overwritten." +
                              ("" if ready else " With GitHub’s gh tool signed in here, Playground can make the "
                               "repository for you."))

        def save() -> None:
            root = self.ctx.project_dir
            if choice["mode"] == "github":
                wanted = name.get_text().strip()
                if not wanted:
                    name.grab_focus()
                    return
                work = lambda: repo.create_on_github(root, wanted, private=choice["private"])  # noqa: E731
            else:
                url = address.get_text().strip()
                if not url:
                    address.grab_focus()
                    return
                work = lambda: repo.connect(root, url)  # noqa: E731
            center.fold()
            self._busy("online")
            c.run_async(work, lambda _r: (setattr(self, "repo_view", "online"), self._after("It’s online")(None)),
                        self._failed)

        modes = [("github", "Create on GitHub", "sparkles"), ("address", "Use an address", "link")] if ready else []
        tools = [] if ready else [kit.BarAction("globe", "New on GitHub",
                                                on_activate=lambda: self.open_url("https://github.com/new"))]
        editor = kit.ActionEditor("Put it online", "globe", modes=modes, mode=choice["mode"] if modes else None,
                                  on_mode=mode, tools=tools,
                                  fields=[("Name", name), ("Visible to", seen), ("Address", address)], body=hint,
                                  primary=kit.BarAction("check", "Put it online", on_activate=save))
        center.set_editor(editor)
        center.grow()
        mode(choice["mode"])
        GLib.idle_add(lambda: ((name if choice["mode"] == "github" else address).grab_focus(), False)[1])

    def defer_to_agent(self, title: str, ask: str) -> str | None:
        """Hand a job to the agent: a new conversation, the ask sent, opened on the right. Its id."""
        starter = getattr(self.window, "start_conversation", None)
        return starter(title, ask, send=True) if callable(starter) else None

    def _author(self) -> tuple[str, str]:
        """Who commits when git doesn't know: the project's person, at a local address."""
        project = self.data.project if self.data else None
        you = project.you() if project is not None and hasattr(project, "you") else ""
        name = next((m.name for m in getattr(project, "members", []) or [] if m.id == you), "") or you or "Playground"
        return name, f"{(you or 'me').lower()}@playground.local"


def _tile(icon: str, label: str, value: str, note: str, on_click, *, on: bool = False,
          dashed: bool = False) -> Gtk.Widget:
    inner = c.hbox(c.vbox(c.glyph(icon, 18), css="pgs-sync-glyph", valign=Gtk.Align.START, hexpand=False),
                   c.vbox(text(label, "pgs-stat-label", wrap=False),
                          text(value, "pgs-sync-value", wrap=False, ellipsize=True),
                          text(note, "pgs-stat-note", wrap=False, ellipsize=True), spacing=2, hexpand=True),
                   spacing=12)
    button = Gtk.Button(child=inner, hexpand=True)
    button.add_css_class("pgs-sync-tile")
    if on:
        button.add_css_class("on")
    if dashed:
        button.add_css_class("dashed")
    button.update_property([Gtk.AccessibleProperty.LABEL], [f"{label}: {value}, {note}"])
    button.connect("clicked", lambda _b: on_click())
    return button
