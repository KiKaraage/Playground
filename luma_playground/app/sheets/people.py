# SPDX-License-Identifier: Apache-2.0
"""People: who is on the project, where it lives, and how teammates join.

- **Where it lives**: only on this computer, on the Development server, or on
  this computer for everyone (with the honest warning that teammates can only
  work while it's on). Sync status, the project's safety code, and the
  one-time setup for the home.
- **Invite** by Luma Connect name or email, as a Builder, Reviewer or Viewer.
  Once the project is shared, an invite is a small signed code or file. It
  holds no secret: the project, where it lives and how to recognise it.
- **Let someone in**: paste their reply (their computer's public keys, signed).
  Approving gives them a pass for the home, opens the vault to them and adds
  them to the project. Their welcome goes back as another code. The same box
  takes a teammate's request for a fresh pass, on any owner's computer that
  can hand passes out.
- **Your pass**: when no owner's Playground has been open to renew it, a
  member is told ahead of time and can ask for a fresh one with a signed code.
- **Roles**: Owner, Builder, Reviewer, Viewer, said in plain words.
- **Presence**: who's here now and which goal they're in (from sync's
  heartbeats); otherwise when they last did something, never a guess.
- **Join a project**: the other side of an invite, for this computer.
- **What syncs and what streams**, and the read-only **Take a look** link.

Everything slow runs on a thread (``common.run_async``); the sheet listens to
``sync.state`` and ``presence.changed`` so it stays current while open.
"""
from __future__ import annotations

import logging
import re
import socket
from typing import Any

from gi.repository import Gdk, Gio, GLib, Gtk

from . import common as c
from .common import Row, Tag, callout, section, text

log = logging.getLogger(__name__)

ROLES = ("owner", "builder", "reviewer", "viewer")
ROLE_WORDS = {
    "owner": "Everything, including who’s on the project",
    "builder": "Asks agents for changes, edits, and releases with approval",
    "reviewer": "Comments on and approves releases",
    "viewer": "Can look, not change",
}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CONNECT = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
_SYNC_TONE = {"up-to-date": "good", "syncing": "run", "offline": "amber", "access": "amber", "conflict": "amber",
              "removed": "red", "error": "red", "stopped": "amber"}


def present(window: Gtk.Window, **options) -> c.Sheet:
    return PeopleSheet(window, options).sheet


class PeopleSheet:
    def __init__(self, window: Gtk.Window, options: dict) -> None:
        self.window = window
        self.ctx = c.context_for(window, options)
        self.sync = c.module("core.sync")
        self.invite_role = "builder"
        self.message: tuple[str, str] | None = None
        self.last_seen: dict[str, str] = {}
        self.preview: dict = {}
        self.info: dict = {}
        self.status: dict = {}
        self.presence: list[dict] = []
        self.waiting: list[dict] = []
        self.devices: dict[str, list[dict]] = {}
        self.busy = False
        self.sheet = c.Sheet(window, "People", self.subtitle(), name="people")
        self.sheet.listener.on("project.changed", lambda **_: self.refresh(reload=True))
        self.sheet.listener.on("preview.state", lambda **_: self.load())
        if self.sync is not None:
            self.sheet.listener.on(self.sync.SYNC_STATE, self.on_sync)
            self.sheet.listener.on(self.sync.PRESENCE_CHANGED, self.on_presence)
        self.sheet.on_layout.append(lambda *_a: self.refresh())
        self.sheet.set_body(self.build())
        self.sheet.present()
        self.load()
        code = options.get("code") or options.get("invite")
        if code:
            GLib.idle_add(lambda: (self.check_code(str(code)), False)[1])

    def sheet_phone(self) -> bool:
        return bool(getattr(self, "sheet", None) and self.sheet.phone)

    def subtitle(self) -> str:
        project = self.ctx.project
        if project is None:
            return "Join a teammate’s project"
        count = len(project.members)
        if count <= 1:
            return f"Just you on {project.name}"
        return f"{count} people on {project.name}"

    def store(self):
        store = c.module("core.store")
        return store.ProjectStore(self.ctx.project_dir) if store and self.ctx.project_dir else None

    # ── reading, off the main loop ──

    def load(self) -> None:
        store = self.store()
        sync = self.sync
        project_dir = self.ctx.project_dir

        def read() -> tuple:
            seen: dict[str, str] = {}
            preview: dict = {}
            info: dict = {}
            status: dict = {}
            presence: list[dict] = []
            devices: dict = {}
            if store is not None:
                for entry in store.read_activity(limit=400):
                    if entry.actor:
                        seen[entry.actor] = entry.ts
                preview = store.read_preview_state() or {}
            if sync is not None:
                if project_dir is not None:
                    info = sync.sharing_info(project_dir)
                    engine = sync.engine_for(project_dir)
                    status = engine.status() if engine is not None else {}
                    presence = sync.read_presence(project_dir)
                    if info.get("shared"):
                        devices = sync.member_devices(project_dir)
                waiting = sync.pending_joins()
            else:
                waiting = []
            return seen, preview, info, status, presence, waiting, devices

        def done(result) -> None:
            (self.last_seen, self.preview, self.info, self.status, self.presence, self.waiting,
             self.devices) = result
            self.refresh()

        c.run_async(read, done)

    def on_sync(self, project: str = "", **status: Any) -> None:
        if self.ctx.project_dir is None or project != str(self.ctx.project_dir.expanduser().absolute()):
            return
        before = (self.status.get("state"), self.status.get("text"), self.status.get("detail"))
        self.status = {"project": project, **status}
        if before != (status.get("state"), status.get("text"), status.get("detail")):
            self.refresh()

    def on_presence(self, project: str = "", people: list | None = None, **_: Any) -> None:
        if self.ctx.project_dir is None or project != str(self.ctx.project_dir.expanduser().absolute()):
            return
        self.presence = list(people or [])
        self.refresh()

    def refresh(self, reload: bool = False) -> None:
        if reload:
            self.ctx.__dict__.pop("_project", None)
        if not self.sheet.closed and self.sheet.depth <= 1:
            self.sheet.replace_top(self.build(), subtitle=self.subtitle())

    def say(self, kind: str, words: str) -> None:
        self.message = (kind, words)
        while self.sheet.depth > 1:
            self.sheet.pop()
        self.refresh(reload=True)
        self.load()

    # ── facts ──

    def shared(self) -> bool:
        return bool(self.info.get("shared"))

    def owner_here(self) -> bool:
        """This computer can change who's on the project: an owner's computer (any of them, once shared)."""
        if self.shared():
            return bool(self.info.get("owner_device"))
        project = self.ctx.project
        return project is not None and project.owner is not None

    def invites_here(self) -> bool:
        """Invites and welcomes come from the computer that shared the project (it holds the project's own key)."""
        if self.shared():
            return bool(self.info.get("can_invite"))
        return self.owner_here()

    def passes_here(self) -> bool:
        """This computer hands out passes itself, so its own never needs asking for."""
        return bool(self.info.get("issues_passes"))

    def me(self, project) -> str:
        if self.info.get("member"):
            return str(self.info["member"])
        return project.owner.id if project is not None and project.owner is not None else ""

    # ── the page ──

    def build(self) -> Gtk.Widget:
        project = self.ctx.project
        body = c.vbox()
        if self.message is not None:
            kind, words = self.message
            body.append(callout(c.escape(words), icon="circle-check" if kind == "ok" else "triangle-alert",
                                tone="good" if kind == "ok" else "warn"))
        if project is None:
            body.append(self.join_section(first=self.message is None))
            return body
        body.append(self.home(project, first=self.message is None))
        if self.invites_here():
            body.append(self.invite(project))
        if self.shared() and (self.invites_here() or self.passes_here()):
            body.append(self.requests())
        body.append(self.members(project))
        body.append(self.shares(project))
        body.append(self.look(project))
        body.append(self.join_section())
        return body

    # Where it lives

    def home(self, project, first: bool = False) -> Gtk.Widget:
        box = c.vbox()
        box.append(section("Where it lives", first=first))
        if self.shared():
            status = self.status or {"state": "stopped", "text": "Not syncing",
                                     "detail": "Sync starts when the project is open in the window."}
            state = str(status.get("state") or "stopped")
            mode = self.info.get("mode")
            lead = {"server": "server", "this-computer": "monitor"}.get(mode, "folder")
            where = str(self.info.get("home") or "its home")
            words = str(status.get("text") or "")
            detail = str(status.get("detail") or ("Everyone’s copies are the same." if state == "up-to-date" else ""))
            again = c.button("Sync now", icon="refresh-cw", style="fill", small=True, on_click=self.sync_now)
            rows = [Row(f"Lives on {where}", detail, lead=lead, tags=[Tag(words, _SYNC_TONE.get(state, "amber"))],
                        end=again)]
            if self.info.get("safety"):
                rows.append(Row(f"Safety code {self.info['safety']}",
                                "Anyone joining sees the same twelve numbers. Read them to each other to be sure "
                                "it’s this project.", lead="fingerprint", title_class="pgs-mono"))
            server = project.place(project.home) if mode == "server" else None
            trust = str(self.info.get("trust") or "")
            if mode == "server" and trust == "live":
                rows.append(Row("Checked at every sign-in", "Taking someone off works at once, for syncing and for "
                                                            "shells on the server.", lead="shield-check"))
            elif mode == "server" and trust == "account":
                rows.append(Row("Set up without sudo", "Syncing stops at once when you take someone off. Shells "
                                                       "end when their pass runs out.", lead="shield",
                                tags=[Tag("Weaker", "amber")]))
            if server is not None and self.invites_here():
                note = ("Only needed once. You never have to run it again after taking someone off." if trust == "live"
                        else "Run the setup with sudo when you can, so taking someone off works at once for shells too.")
                rows.append(Row("The server’s setup", note, lead="key-round",
                                end=c.button("Show", style="fill", small=True,
                                             on_click=lambda: self.show_server_setup(server))))
            rows += self.pass_rows(status)
            if self.invites_here():
                rows.append(Row("Another computer of yours", "Use the project on your laptop too. It gets its own "
                                                             "key and pass; you stay one person on the project.",
                                lead="monitor", end=c.button("Invite it", style="fill", small=True,
                                                            on_click=lambda: self.invite_device(project, None))))
            if mode == "this-computer" and self.info.get("hosting"):
                rows.append(Row("Teammates’ way in", "This computer needs SSH switched on and a one-time setup "
                                                     "before teammates can sync.", lead="key-round",
                                end=c.button("Show", style="fill", small=True, on_click=self.show_here_setup)))
            box.append(c.ListGroup(*rows))
            renew = str(status.get("renew_words") or "") if status.get("renew_due") else ""
            for note, tone, icon in ((self.info.get("warning"), "warn", "triangle-alert"),
                                     (renew, "warn", "clock"),
                                     (self.info.get("owner_note"), "info", "info")):
                if note:
                    line = callout(c.escape(note), icon=icon, tone=tone)
                    line.set_margin_top(8)
                    box.append(line)
            return box
        box.append(text(f"Right now {project.name} is only on this computer. Give it a home and teammates work on "
                        "it with the full Playground app on their own computers, each with their own copy.",
                        "pgs-section-note"))
        if not self.owner_here():
            return box
        rows = []
        servers = [p for p in project.places if p.kind == "ssh" and p.role == "development"]
        for place in servers:
            rows.append(Row(place.name or place.host, "Our development server. Teammates sync any time, and it can "
                                                      "run previews too.", lead="server",
                            end=c.button("Live here", style="key", small=True,
                                         on_click=lambda p=place: self.show_server_setup(p))))
        if not servers:
            rows.append(Row("A development server", "A box in the closet works. Add it in Places first, as a "
                                                    "Development server.", lead="server",
                            end=c.button("Places", style="fill", small=True,
                                         on_click=lambda: c.open_sheet(self.window, "places"))))
        rows.append(Row("This computer", "Teammates can only work while this computer is on.", lead="monitor",
                        end=c.button("Live here", style="fill", small=True, on_click=self.show_here_setup)))
        box.append(c.ListGroup(*rows))
        return box

    def pass_rows(self, status: dict) -> list:
        """A member's own pass: until when it works, and a way to ask for a fresh one when no owner's
        Playground will be around to renew it."""
        if self.passes_here() or self.info.get("mode") == "folder" or self.sync is None:
            return []
        expires = float(status.get("pass_expires") or self.info.get("pass_expires") or 0)
        if not expires:
            return []
        import time
        left = expires - time.time()
        words = (f"Your pass runs out {self.sync.expiry_words(expires)}" if left > 0 else "Your pass has run out")
        due = bool(status.get("renew_due")) or left < self.sync.PASS_WARN_S
        hint = ("It renews by itself whenever an owner’s Playground is open." if not due else
                "No owner’s Playground has renewed it yet. Ask one of them for a fresh pass.")
        end = c.button("Ask for a fresh pass", style="key" if due else "fill", small=True, on_click=self.ask_renewal)
        return [Row(words, hint, lead="ticket" if not due else "timer", end=end,
                    tags=[Tag("Soon", "amber")] if due and left > 0 else [])]

    def sync_now(self) -> None:
        if self.sync is None or self.ctx.project_dir is None:
            return
        engine = self.sync.engine_for(self.ctx.project_dir)
        if engine is not None:
            engine.sync_now(wait=False)
            return
        try:
            from .. import sync_ui
            if sync_ui.start_sync(self.window, self.ctx.project_dir) is None:
                self.say("error", "Sync isn’t running for this project.")
        except Exception as error:  # noqa: BLE001
            self.say("error", str(error))

    def show_server_setup(self, place) -> None:
        sync = self.sync
        project_dir = self.ctx.project_dir
        if sync is None or project_dir is None:
            return
        status = text("", "pgs-field-hint")
        name = place.name or place.host
        choice = {"trust": str(self.info.get("trust") or "live") if self.shared() else "live"}

        def read() -> dict:
            return {trust: sync.home_setup_instructions(project_dir, mode="server", place_id=place.id, trust=trust)
                    for trust in ("live", "account")}

        def build(setups: dict) -> None:
            page = c.vbox(spacing=8)
            if self.shared():
                page.append(callout("You only need this once. After that, taking someone off works by itself: the "
                                    "server reads the project’s signed list of who’s allowed every time anyone signs "
                                    "in.", icon="server", tone="info"))
            else:
                page.append(callout(f"{c.escape(self.ctx.name)} will live on <b>{c.escape(name)}</b>. Everyone keeps "
                                    "their own copy, and the server holds the shared one.", icon="server", tone="info"))
            page.append(section("One-time setup on the server"))
            options = c.vbox(spacing=8)

            def show(trust: str) -> None:
                choice["trust"] = trust
                while (child := options.get_first_child()) is not None:
                    options.remove(child)
                setup = setups[trust]
                options.append(text(setup.get("explanation", ""), "pgs-section-note"))
                options.append(self.command_box(setup["command"], "the setup command"))
                if trust == "live":
                    options.append(text("Run it in Terminal on any computer that can reach the server, as someone who "
                                        "can use sudo there. Running it again later is safe.", "pgs-field-hint"))
                    options.append(callout(c.escape(setup["words"]), icon="shield-check", tone="good"))
                else:
                    options.append(callout(c.escape(setup["words"]), icon="triangle-alert", tone="warn"))

            page.append(c.segmented([("live", "With sudo"), ("account", "Without sudo")], choice["trust"],
                                    label="How to set up the server", on_change=show, small=False))
            page.append(options)
            show(choice["trust"])
            if self.shared():
                page.append(c.button("Done", style="fill", on_click=self.sheet.pop))
                self.sheet.push(page, f"{name}’s setup", "Once is enough")
                return
            go = c.button("I’ve run it. Share now", style="key",
                          on_click=lambda: self.share("server", place.id, status, trust=choice["trust"]))
            page.append(c.hbox(go, status, spacing=10))
            self.sheet.push(page, f"Live on {name}", "Teammates sync with it any time")

        c.run_async(read, build, lambda error: self.say("error", f"Couldn’t prepare the server setup: {error}"))

    def show_here_setup(self) -> None:
        sync = self.sync
        project_dir = self.ctx.project_dir
        if sync is None or project_dir is None:
            return
        status = text("", "pgs-field-hint")

        def read() -> tuple[dict, dict]:
            return sync.home_setup_instructions(project_dir, mode="this-computer"), sync.check_this_computer(project_dir)

        def build(result) -> None:
            setup, check = result
            page = c.vbox(spacing=8)
            page.append(callout(c.escape(sync.THIS_COMPUTER_WARNING), icon="triangle-alert", tone="warn"))
            rows = [Row("SSH is switched on" if check["sshd"] else "Switch SSH on",
                        "" if check["sshd"] else setup["steps"][0], lead="circle-check" if check["sshd"] else "power"),
                    Row("The one-time setup is done" if check["trusted"] else "Run the one-time setup",
                        setup["explanation"], lead="circle-check" if check["trusted"] else "key-round")]
            if check.get("addresses"):
                rows.append(Row("Teammates reach it at", ", ".join(check["addresses"]), lead="wifi"))
            page.append(c.ListGroup(*rows))
            if not check["trusted"]:
                page.append(section("The setup command"))
                page.append(self.command_box(setup["command"], "the setup command"))
                page.append(text(setup["steps"][1], "pgs-field-hint"))
            if not self.shared():
                go = c.button("Share from this computer", style="key",
                              on_click=lambda: self.share("this-computer", None, status))
                page.append(c.hbox(go, status, spacing=10))
            page.append(text("Teammates reach it on your network, or anywhere over Tailscale. Their passes can only "
                             "sync this one project: no shell, no other files.", "pgs-field-hint"))
            self.sheet.push(page, "Live on this computer", "Teammates sync while it’s on")

        c.run_async(read, build, lambda error: self.say("error", f"Couldn’t check this computer: {error}"))

    def share(self, mode: str, place_id: str | None, status: Gtk.Label, trust: str = "live") -> None:
        if self.busy:
            return
        sync = self.sync
        project_dir = self.ctx.project_dir
        self.busy = True
        status.set_label("Setting up the shared copy…")

        def work() -> dict:
            if mode == "server":
                return sync.share_project(project_dir, mode=mode, place_id=place_id, trust=trust)
            return sync.share_project(project_dir, mode=mode, place_id=place_id)

        def done(result: dict) -> None:
            self.busy = False
            try:
                from .. import sync_ui
                sync_ui.start_sync(self.window, project_dir)
            except Exception:  # noqa: BLE001
                log.debug("couldn't start sync", exc_info=True)
            words = f"{self.ctx.name} now lives on {result['home']}. Invite people below."
            if result.get("warning"):
                words += " " + result["warning"]
            self.say("ok", words)

        def failed(error: BaseException) -> None:
            self.busy = False
            status.set_label(str(error))

        c.run_async(work, done, failed)

    # Invite

    def invite(self, project) -> Gtk.Widget:
        box = c.vbox()
        box.append(section("Invite someone"))
        entry = Gtk.Entry(hexpand=True, placeholder_text="Luma Connect name or email")
        entry.set_input_purpose(Gtk.InputPurpose.EMAIL)
        entry.update_property([Gtk.AccessibleProperty.LABEL], ["Luma Connect name or email"])
        roles = c.segmented([("builder", "Builder"), ("reviewer", "Reviewer"), ("viewer", "Viewer")],
                            self.invite_role, label="Their role",
                            on_change=lambda key: setattr(self, "invite_role", key))
        go = c.button("Invite", style="key", small=True, on_click=lambda: self.add(entry.get_text()))
        entry.connect("activate", lambda e: self.add(e.get_text()))
        field = c.hbox(c.glyph("user-plus", 16), entry, spacing=8, css="pgs-field", hexpand=True)
        if self.sheet_phone():
            line = c.vbox(field, c.hbox(roles, c.vbox(hexpand=True), go, spacing=8), spacing=8)
        else:
            line = c.hbox(field, roles, go, spacing=8)
        self.invite_line = line
        box.append(line)
        hint = ("Everyone gets the full Playground app on their own computer, never a cut-down web page."
                if self.shared() else
                "Everyone gets the full Playground app on their own computer. Pick where the project lives first, "
                "so they can join.")
        box.append(text(hint, "pgs-field-hint"))
        box.get_last_child().set_margin_top(8)
        return box

    def add(self, who: str) -> None:
        who = who.strip()
        if not who:
            return
        if not (_EMAIL.match(who) or _CONNECT.match(who)):
            self.say("error", "Use their Luma Connect name, like @priya, or their email address.")
            return
        store = self.store()
        model = c.module("core.model")
        paths = c.module("core.paths")
        sync = self.sync
        shared = self.shared() and self.owner_here()
        role = self.invite_role
        name = who.lstrip("@").split("@")[0] if _EMAIL.match(who) else who.lstrip("@")
        display = " ".join(part.capitalize() for part in re.split(r"[._-]+", name) if part) or name
        project_dir = self.ctx.project_dir

        def write() -> tuple[str, Any]:
            with store.locked():
                project = store.read_project()
                existing = next((m for m in project.members if m.connect_id.lower() == who.lower()), None)
                if existing is not None and not (shared and existing.extra.get("invited")):
                    raise ValueError(f"{who} is already on {project.name}.")
                if existing is None:
                    taken = {m.id for m in project.members}
                    member_id = paths.slugify(display, 24)
                    base, n = member_id, 2
                    while member_id in taken:
                        member_id, n = f"{base}-{n}", n + 1
                    project.members.append(model.Member(id=member_id, name=display, role=role, connect_id=who,
                                                        extra={"invited": model.now_iso()}))
                    store.write_project(project)
                    store.log(f"Invited {display} as a {role}", kind="people", data={"member": member_id})
                else:
                    member_id = existing.id
            invite = sync.create_invite(project_dir, role=role, member_id=member_id, name=display,
                                        for_whom=who) if shared and sync is not None else None
            return display, invite

        def done(result) -> None:
            person, invite = result
            if invite is None:
                self.say("ok", f"{person} is on the list as a {role.capitalize()}. Once the project has a home, "
                               "invite them again to send them a way in.")
                return
            self.show_invite(person, invite)

        c.run_async(write, done, lambda error: self.say("error", str(error)))

    def show_invite(self, person: str, invite) -> None:
        page = c.vbox(spacing=8)
        page.append(callout(f"Send this invite to {c.escape(person)} any way you like: a message, an email or a "
                            "file. It holds no password or key, only where the project lives and how to recognise "
                            "it.", icon="send", tone="info"))
        page.append(self.code_box(invite.code, invite.file_text, invite.file_name, "invite"))
        expires = c.shared.parse_ts(invite.body.get("expires"))
        page.append(c.kv([("Project", invite.project_name), ("Their role", str(invite.body.get("role", "")).capitalize()),
                          ("Lives on", str((invite.body.get("home") or {}).get("host_name") or "")),
                          ("Works until", c.shared.when_words(expires) if expires else ""),
                          ("Safety code", invite.safety)]))
        page.append(section("When they answer"))
        page.append(text(f"{c.escape(person)}’s Playground gives them a reply to send back. Paste it here to let "
                         "them in.", "pgs-section-note", markup=True))
        page.append(self.paste_line("Paste their reply", self.check_request))
        self.sheet.push(page, f"Invite {person}", "A small signed code. Nothing secret in it.")

    # Let someone in

    def requests(self) -> Gtk.Widget:
        box = c.vbox()
        if self.invites_here():
            box.append(section("Let someone in", note="Paste the reply someone sent after you invited them, or a "
                                                      "teammate’s request for a fresh pass."))
        else:
            box.append(section("Fresh passes", note="Paste a teammate’s request for a fresh pass. This computer can "
                                                    "hand them out too."))
        box.append(self.paste_line("Paste their reply or request", self.check_request))
        return box

    def check_request(self, code: str) -> None:
        sync = self.sync
        project_dir = self.ctx.project_dir
        if sync is None or project_dir is None or not code.strip():
            return
        if code.strip().startswith("PGR1-") or '"renew"' in code:
            c.run_async(lambda: sync.read_renewal_request(project_dir, code), self.show_renewal,
                        lambda error: self.toast_error(error))
            return
        c.run_async(lambda: sync.read_join_request(project_dir, code), self.show_request,
                    lambda error: self.toast_error(error))

    # Fresh passes

    def show_renewal(self, request) -> None:
        """An owner's side: a teammate asked for a fresh pass."""
        body = request.body
        name = str(body.get("name") or body.get("member") or "Someone")
        device = body.get("device") or {}
        page = c.vbox(spacing=8)
        page.append(c.ListGroup(Row(name, f"From the computer “{device.get('name') or 'device'}”",
                                    lead=c.shared.person_face(name, 34)),
                                Row("Its key", str(request.extra.get("fingerprint") or ""), lead="fingerprint")))
        page.append(callout("This computer is already on the project, so it only gets a fresh pass for the project "
                            "home. Nothing else changes, and nothing secret goes back.", icon="shield", tone="info"))
        status = text("", "pgs-field-hint")

        def give() -> None:
            if self.busy:
                return
            self.busy = True
            status.set_label("Making a fresh pass…")

            def done(answer) -> None:
                self.busy = False
                result = c.vbox(spacing=8)
                result.append(callout(f"Send this back to {c.escape(c.first_name(name))}. Their Playground keeps the "
                                      "fresh pass and syncs again.", icon="send", tone="good"))
                result.append(self.code_box(answer.code, answer.file_text, answer.file_name, "fresh pass"))
                if answer.extra.get("note"):
                    result.append(callout(c.escape(answer.extra["note"]), icon="triangle-alert", tone="warn"))
                result.append(c.button("Done", style="fill",
                                       on_click=lambda: self.say("ok", f"{c.first_name(name)} has a fresh pass.")))
                self.sheet.push(result, f"A fresh pass for {c.first_name(name)}", "One code to send back")

            def failed(error: BaseException) -> None:
                self.busy = False
                status.set_label(str(error))

            c.run_async(lambda: self.sync.answer_renewal(self.ctx.project_dir, request), done, failed)

        page.append(c.hbox(c.button("Give them a fresh pass", style="key", on_click=give),
                           c.button("Not now", style="fill", on_click=self.sheet.pop), status, spacing=8))
        self.sheet.push(page, f"A fresh pass for {c.first_name(name)}?", "They’re already on the project")

    def ask_renewal(self) -> None:
        """A member's side: a signed request for a fresh pass, to send to any owner."""
        sync = self.sync
        project_dir = self.ctx.project_dir
        if sync is None or project_dir is None:
            return

        def build(request) -> None:
            page = c.vbox(spacing=8)
            page.append(callout("Send this to an owner any way you like. It holds this computer’s public key, signed, "
                                "and nothing secret. Their Playground answers with a fresh pass.", icon="send",
                                tone="info"))
            page.append(self.code_box(request.code, request.file_text, request.file_name, "request"))
            page.append(section("Their answer"))
            page.append(self.paste_line("Paste the fresh pass", self.accept_passes))
            self.sheet.push(page, "Ask for a fresh pass", "One code to an owner, one back")

        c.run_async(lambda: sync.request_renewal(project_dir), build, lambda error: self.toast_error(error))

    def accept_passes(self, code: str) -> None:
        sync = self.sync
        project_dir = self.ctx.project_dir
        if sync is None or project_dir is None or not code.strip():
            return

        def done(result: dict) -> None:
            until = float(result.get("pass_expires") or 0)
            when = f" It runs out {sync.expiry_words(until)}, and renews by itself whenever an owner’s Playground is " \
                   "open." if until else ""
            self.say("ok", f"You have a fresh pass, and this computer is syncing again.{when}")

        c.run_async(lambda: sync.accept_renewal(project_dir, code), done, lambda error: self.toast_error(error))

    def show_request(self, request) -> None:
        body = request.body
        invite = request.extra.get("invite") or {}
        name = str(body.get("name") or "Someone")
        role = {"value": invite.get("role") or "builder"}
        device = body.get("device") or {}
        page = c.vbox(spacing=8)
        fingerprint = str(request.extra.get("fingerprint") or "")
        page.append(c.ListGroup(Row(name, f"From the computer “{device.get('name') or 'device'}”", lead=c.shared.person_face(name, 34)),
                                Row("Its key", fingerprint, lead="fingerprint", title_class=None)))
        page.append(section("Their role"))
        page.append(c.segmented([("builder", "Builder"), ("reviewer", "Reviewer"), ("viewer", "Viewer")],
                                role["value"], label="Their role", on_change=lambda key: role.update(value=key),
                                small=False))
        page.append(callout("Only let them in if you invited them. They’ll get their own copy of the project and can "
                            "sync it from now on. Removing them later cuts them off at once.",
                            icon="shield", tone="info"))
        if not body.get("vault"):
            page.append(callout("Their computer can’t open the project vault yet (it needs python3-cryptography). "
                                "Everything else works.", icon="info", tone="info"))
        status = text("", "pgs-field-hint")
        go = c.button(f"Let {c.first_name(name)} in", style="key",
                      on_click=lambda: self.approve(request, role["value"], status))
        later = c.button("Not now", style="fill", on_click=self.sheet.pop)
        page.append(c.hbox(go, later, status, spacing=8))
        self.sheet.push(page, f"Let {c.first_name(name)} in?", "Check this is who you invited")

    def approve(self, request, role: str, status: Gtk.Label) -> None:
        if self.busy:
            return
        sync = self.sync
        project_dir = self.ctx.project_dir
        self.busy = True
        status.set_label("Letting them in…")

        def done(welcome) -> None:
            self.busy = False
            name = str((welcome.body.get("member") or {}).get("name") or "them")
            page = c.vbox(spacing=8)
            page.append(callout(f"Send this welcome back to {c.escape(name)}. Their Playground uses it to fetch the "
                                "project. Like the invite, nothing in it is secret.", icon="send", tone="good"))
            page.append(self.code_box(welcome.code, welcome.file_text, welcome.file_name, "welcome"))
            note = welcome.extra.get("note") or ""
            if note:
                page.append(callout(c.escape(note), icon="triangle-alert", tone="warn"))
            if welcome.extra.get("vault_note"):
                page.append(callout(c.escape(f"The vault: {welcome.extra['vault_note']}"), icon="info", tone="info"))
            page.append(c.button("Done", style="fill", on_click=lambda: self.say("ok", f"{name} is on the project.")))
            self.sheet.push(page, f"Welcome {c.first_name(name)}", "One more code to send back")

        def failed(error: BaseException) -> None:
            self.busy = False
            status.set_label(str(error))

        c.run_async(lambda: sync.approve_join(project_dir, request, role=role), done, failed)

    # Members

    def members(self, project) -> Gtk.Widget:
        box = c.vbox()
        box.append(section(f"On {project.name}"))
        rows = [self.member_row(project, m) for m in sorted(project.members, key=lambda m: ROLES.index(m.role)
                                                             if m.role in ROLES else 9)]
        box.append(c.ListGroup(*rows))
        return box

    def member_row(self, project, member) -> Gtk.Widget:
        me = member.id == self.me(project)
        beat = next((p for p in self.presence if p.get("member") == member.id), None)
        here_now = me or bool(beat and beat.get("here"))
        invited = bool(member.extra.get("invited")) and member.id not in self.last_seen and beat is None
        face = c.shared.Presence(member.name or member.id, 34, here=here_now) if not invited else \
            c.shared.person_face(member.name or member.id, 34)
        presence: Gtk.Widget | None = None
        if me:
            presence = c.hbox(text("Here now", "pgs-here", wrap=False),
                              text(f"· on {_device()}", "pgs-row-sub", wrap=False), spacing=4)
        elif here_now:
            goal = str(beat.get("goal_title") or "")
            presence = c.hbox(text("Here now", "pgs-here", wrap=False),
                              text(f"· in {goal}" if goal else f"· on {beat.get('device') or 'their computer'}",
                                   "pgs-row-sub", wrap=False, ellipsize=True), spacing=4)
        elif invited:
            presence = text("Invited. Waiting for them to join.", "pgs-row-sub")
        else:
            moments = [m for m in (c.shared.parse_ts(beat.get("ts")) if beat else None,
                                   c.shared.parse_ts(self.last_seen.get(member.id))) if m]
            if moments:
                presence = text(f"Last here {c.shared.ago(max(moments))}", "pgs-row-sub")
        title = f"{member.name or member.id}{' (you)' if me else ''}"
        owners = sum(m.role == "owner" for m in project.members)
        can_change = self.owner_here() and not me and (member.role != "owner" or not self.shared() or owners > 1)
        computers = len(self.devices.get(member.id) or [])
        if self.shared() and computers > 1:
            row_note = text(f"On {computers} computers", "pgs-row-sub")
        else:
            row_note = None
        row = Row(title, ROLE_WORDS.get(member.role, ""), lead=face, tags=[Tag(member.role.capitalize())],
                  end=self.role_menu(project, member) if can_change else None)
        if presence is not None:
            row.words.append(presence)
        if row_note is not None:
            row.words.append(row_note)
        return row

    def role_menu(self, project, member) -> Gtk.Widget:
        popover = Gtk.Popover(has_arrow=False)
        popover.add_css_class("pgs-menu")
        items = c.vbox(spacing=2)
        for side in ("top", "bottom", "start", "end"):
            getattr(items, f"set_margin_{side}")(6)
        for role in (("owner", "builder", "reviewer", "viewer") if self.shared() else ("builder", "reviewer", "viewer")):
            item = Gtk.Button()
            item.add_css_class("pgs-menu-item")
            item.set_child(c.hbox(c.vbox(text(role.capitalize(), "pgs-row-title", wrap=False),
                                         text(ROLE_WORDS[role], "pgs-row-sub", wrap=False), spacing=1,
                                         hexpand=True),
                                  c.glyph("check", 16) if member.role == role else None, spacing=12))
            item.update_property([Gtk.AccessibleProperty.LABEL], [f"Make {member.first_name} a {role}"])
            item.connect("clicked", lambda _b, r=role: (popover.popdown(), self.set_role(member, r)))
            items.append(item)
        items.append(Gtk.Separator())
        if self.shared() and self.invites_here():
            items.append(self.menu_item("monitor", f"Invite another of {member.first_name}’s computers",
                                        lambda: (popover.popdown(), self.invite_device(project, member))))
        computers = self.devices.get(member.id) or []
        if self.shared() and len(computers) > 1:
            for computer in computers:
                items.append(self.menu_item("x", f"Take “{computer['device']}” off",
                                            lambda d=computer: (popover.popdown(),
                                                                self.confirm_remove_device(project, member, d))))
        remove = Gtk.Button()
        remove.add_css_class("pgs-menu-item")
        remove.add_css_class("danger")
        remove.set_child(c.hbox(c.glyph("user-minus", 16), text(f"Remove {member.first_name}", wrap=False),
                                spacing=10))
        remove.connect("clicked", lambda _b: (popover.popdown(), self.confirm_remove(project, member)))
        items.append(remove)
        popover.set_child(items)
        button = Gtk.MenuButton(popover=popover, valign=Gtk.Align.CENTER)
        button.set_child(c.hbox(text("Role", wrap=False), c.glyph("chevron-down", 14), spacing=4))
        button.add_css_class("pgs-role-button")
        button.update_property([Gtk.AccessibleProperty.LABEL], [f"{member.first_name}’s role"])
        return button

    def menu_item(self, icon: str, words: str, on_click) -> Gtk.Widget:
        item = Gtk.Button()
        item.add_css_class("pgs-menu-item")
        item.set_child(c.hbox(c.glyph(icon, 16), text(words, wrap=False), spacing=10))
        item.update_property([Gtk.AccessibleProperty.LABEL], [words])
        item.connect("clicked", lambda _b: on_click())
        return item

    def invite_device(self, project, member) -> None:
        """An invite for one more computer of someone already on the project (``member`` None: yourself)."""
        sync = self.sync
        project_dir = self.ctx.project_dir
        if sync is None or project_dir is None:
            return
        target = member or project.member(self.me(project))
        if target is None:
            return
        person = "your other computer" if member is None else f"{target.first_name}’s other computer"

        def done(invite) -> None:
            page = c.vbox(spacing=8)
            page.append(callout(f"Open this on {c.escape(person)}. That computer makes its own key and answers with "
                                "a reply; paste it under Let someone in. It joins as the same person, with the "
                                "same role.", icon="send", tone="info"))
            page.append(self.code_box(invite.code, invite.file_text, invite.file_name, "invite"))
            page.append(self.paste_line("Paste its reply", self.check_request))
            self.sheet.push(page, "Another computer", "Same person, its own key")

        c.run_async(lambda: sync.create_invite(project_dir, role=target.role, member_id=target.id, name=target.name,
                                               for_whom=target.connect_id), done, lambda error: self.toast_error(error))

    def confirm_remove_device(self, project, member, computer: dict) -> None:
        def go(_checked: bool = False) -> None:
            sync = self.sync
            project_dir = self.ctx.project_dir

            def done(result: dict) -> None:
                words = f"“{computer['device']}” can’t sync any more. {member.first_name} keeps working on their " \
                        "other computers."
                change = result.get("to_change") or []
                if change:
                    words += f" It could read {', '.join(change)}, so change those where they come from."
                self.say("ok", words)

            c.run_async(lambda: sync.remove_device(project_dir, member.id, computer["fingerprint"]), done,
                        lambda error: self.say("error", str(error)))

        body = (f"The computer “{computer['device']}” stops syncing at once and its passes stop working. "
                f"{member.first_name} stays on {project.name} on their other computers. Use this for a lost or "
                "sold laptop.")
        if c.kit is not None:
            c.kit.DestructiveDialog.ask(self.sheet.card, title=f"Take “{computer['device']}” off {project.name}?",
                                        body=body, action="Take it off", icon="x", on_confirm=go)
        else:
            go()

    def set_role(self, member, role: str) -> None:
        store = self.store()
        sync = self.sync
        project_dir = self.ctx.project_dir
        shared = self.shared()

        def write() -> None:
            if shared and sync is not None:
                sync.set_member_role(project_dir, member.id, role)
                return
            with store.locked():
                project = store.read_project()
                found = project.member(member.id)
                if found is None:
                    return
                found.role = role
                store.write_project(project)
            store.log(f"{member.first_name} is now a {role}", kind="people", data={"member": member.id})

        c.run_async(write, lambda _r: self.say("ok", f"{member.first_name} is now a {role.capitalize()}."),
                    lambda error: self.say("error", str(error)))

    def confirm_remove(self, project, member) -> None:
        def go(_checked: bool = False) -> None:
            self.remove(project, member)
        trust = str(self.info.get("trust") or "")
        if not self.shared():
            body = ("They stop seeing it, their server certificates stop working, and the vault is locked again "
                    "without them.")
        elif self.info.get("mode") == "server" and trust == "account" and member.role in ("owner", "builder"):
            body = ("They stop syncing at once and the vault is locked again without them. This server was set up "
                    "without sudo, so their shell there keeps working until their pass runs out, within 12 hours. "
                    "Their own copy stays on their computer.")
        else:
            body = ("They stop syncing at once, the server stops letting them in the moment it hears, and the vault "
                    "is locked again without them. Their own copy stays on their computer.")
        if c.kit is not None:
            c.kit.DestructiveDialog.ask(self.sheet.card, title=f"Remove {member.first_name} from {project.name}?",
                                        body=body, action="Remove", icon="user-minus", on_confirm=go)
        else:
            go()

    def remove(self, project, member) -> None:
        store = self.store()
        security = c.module("core.security")
        sync = self.sync
        project_dir = self.ctx.project_dir
        shared = self.shared()

        def write() -> dict:
            if shared and sync is not None:
                return sync.remove_member_access(project_dir, member.id)
            change: list[str] = []
            if security is not None and project.id and security.ProjectCA.exists(project.id):
                change = security.revoke_member(project_dir, project.id, member.id).get("to_change") or []
            with store.locked():
                current = store.read_project()
                current.members = [m for m in current.members if m.id != member.id]
                store.write_project(current)
            store.log(f"Removed {member.name} from the project", kind="people", data={"member": member.id})
            return {"to_change": change, "pushed": True}

        def done(result: dict) -> None:
            words = f"{member.first_name} isn’t on {project.name} any more."
            change = result.get("to_change") or []
            if change:
                words += f" They could read {', '.join(change)}, so change those where they come from."
            if shared and not result.get("pushed"):
                words += (" Playground couldn’t reach the project home just now; it cuts them off as soon as it "
                          "can, while Playground is open.")
            elif (shared and self.info.get("mode") == "server" and result.get("trust") == "account"
                  and member.role in ("owner", "builder")):
                words += (" This server was set up without sudo, so their shell there works until their pass runs "
                          "out, within 12 hours. If you don’t trust them, check that account yourself.")
            self.say("ok", words)

        c.run_async(write, done, lambda error: self.say("error", str(error)))

    # What syncs, what streams

    def shares(self, project) -> Gtk.Widget:
        box = c.vbox()
        box.append(section("What everyone shares"))
        where = str(self.info.get("home") or "") if self.shared() else "your computer"
        synced = card_list("refresh-cw", "Synced to everyone",
                           ["Conversations", "Decisions, bugs and knowledge", "The code, change by change",
                            "History"])
        streamed = card_list("radio-tower", "Each person’s own",
                             ["Their agent and their model account", "The running app on Stage", "Terminals"])
        grid = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True)
        grid.attach(synced, 0, 0, 1, 1)
        grid.attach(streamed, 0 if self.sheet_phone() else 1, 1 if self.sheet_phone() else 0, 1, 1)
        self.share_grid = grid
        box.append(grid)
        if self.shared():
            note = (f"Everyone keeps a full copy. If {c.escape(where)} is off, people keep working and planning on "
                    "their own copy; it all comes together when it’s back. Agents always run on the computer of the "
                    "person who asks, with their own account.")
        else:
            note = ("Agents always run on the computer of the person who asks, with their own account. Nobody’s "
                    "prompt runs on someone else’s plan.")
        line = callout(note, icon="info", tone="info")
        line.set_margin_top(8)
        box.append(line)
        return box

    # Take a look

    def look(self, project) -> Gtk.Widget:
        box = c.vbox()
        box.append(section("Take a look"))
        url = _lan_url(self.preview.get("url") or "") if self.preview.get("state") in ("running", "ready", "up") else ""
        rows = [Row("A read-only link",
                    "For people without Playground, like a client or a friend. They see Stage and can’t change "
                    "anything. It’s never how a teammate works.", lead="eye")]
        if url:
            copy = c.button("Copy", icon="copy", style="fill", small=True,
                            on_click=lambda: self.copy(url, "Copied the link"))
            rows.append(Row(url, "Works on your network while Stage is running", lead="link", end=copy,
                            title_class="pgs-mono"))
        else:
            rows.append(Row("Start Stage to get a link",
                            "The link works for people on your network while Stage is running. A link that works "
                            "anywhere arrives with Luma Connect.", lead="link"))
        box.append(c.ListGroup(*rows))
        return box

    # Joining someone else's project

    def join_section(self, first: bool = False) -> Gtk.Widget:
        box = c.vbox()
        box.append(section("Join a project", first=first,
                           note="Got an invite from a teammate? Paste it here. You get your own full copy of the "
                                "project, and the full app."))
        if self.sync is None:
            box.append(text("Joining needs Playground's sync module, which isn't installed.", "pgs-field-hint"))
            return box
        box.append(self.paste_line("Paste an invite or a welcome", self.check_code))
        rows = []
        for record in self.waiting:
            moment = c.shared.parse_ts(record.get("created"))
            who = c.first_name(record.get("from")) or "the owner"
            rows.append(Row(f"Waiting to hear from {who}", f"{record.get('project') or 'A project'} · asked "
                                                           f"{c.shared.ago(moment) if moment else 'earlier'}",
                            lead="clock", end=c.button("Continue", style="fill", small=True,
                                                       on_click=lambda r=record: self.show_waiting(r))))
        if rows:
            group = c.ListGroup(*rows)
            group.set_margin_top(8)
            box.append(group)
        return box

    def check_code(self, code: str) -> None:
        code = code.strip()
        if not code or self.sync is None:
            return
        if code.startswith("PGA1-") or '"approval"' in code:
            self.accept(code, None)
            return
        if code.startswith("PGP1-") or '"passes"' in code:
            self.accept_passes(code)
            return
        if code.startswith(("PGJ1-", "PGR1-")) or '"join"' in code or '"renew"' in code:
            self.check_request(code)
            return
        c.run_async(lambda: self.sync.read_invite(code), self.show_join, lambda error: self.toast_error(error))

    def show_join(self, invite) -> None:
        body = invite.body
        sender = str(body.get("from") or "The owner")
        page = c.vbox(spacing=8)
        page.append(c.kv([("Project", invite.project_name), ("From", sender),
                          ("Your role", str(body.get("role", "")).capitalize()),
                          ("Lives on", str((body.get("home") or {}).get("host_name") or "")),
                          ("Safety code", invite.safety)]))
        page.append(callout(f"Check the safety code with {c.escape(c.first_name(sender) or sender)}, by phone or in "
                            "person if you can. If the numbers match, this really is their project.",
                            icon="shield", tone="info"))
        if (body.get("home") or {}).get("mode") == "this-computer":
            page.append(callout(f"{c.escape(invite.project_name)} lives on {c.escape(c.first_name(sender))}’s "
                                "computer, so you can sync while it’s on. Your copy works any time.",
                                icon="monitor", tone="info"))
        page.append(section("Your name"))
        project_module = c.module("core.project")
        default = ""
        try:
            default = project_module.default_owner().name if project_module else ""
        except Exception:  # noqa: BLE001
            default = ""
        entry = Gtk.Entry(hexpand=True, text=default, placeholder_text="So they know who’s asking")
        entry.update_property([Gtk.AccessibleProperty.LABEL], ["Your name"])
        page.append(c.hbox(c.glyph("user", 16), entry, spacing=8, css="pgs-field"))
        status = text("", "pgs-field-hint")
        go = c.button("Ask to join", style="key", on_click=lambda: self.ask(invite, entry.get_text(), status))
        page.append(c.hbox(go, status, spacing=10))
        self.sheet.push(page, f"Join {invite.project_name}?", f"An invite from {sender}")

    def ask(self, invite, name: str, status: Gtk.Label) -> None:
        if self.busy:
            return
        self.busy = True
        status.set_label("Getting this computer ready…")

        def done(request) -> None:
            self.busy = False
            sender = c.first_name(invite.body.get("from")) or "the owner"
            self.show_reply(request, sender, invite.project_name)

        def failed(error: BaseException) -> None:
            self.busy = False
            status.set_label(str(error))

        c.run_async(lambda: self.sync.request_to_join(invite, name), done, failed)

    def show_reply(self, request, sender: str, project_name: str) -> None:
        page = c.vbox(spacing=8)
        page.append(callout(f"Send this reply to {c.escape(sender)}. It holds this computer’s public keys, signed, "
                            "and nothing secret. When they let you in, they send back a welcome.",
                            icon="send", tone="info"))
        page.append(self.code_box(request.code, request.file_text, request.file_name, "reply"))
        page.append(section("Their welcome"))
        page.append(self.paste_line("Paste their welcome", lambda code: self.accept(code, None)))
        self.sheet.push(page, f"Ask to join {project_name}", f"One code to {sender}, one back")

    def show_waiting(self, record: dict) -> None:
        self.show_reply(record["request"], c.first_name(record.get("from")) or "the owner",
                        record.get("project") or "the project")

    def accept(self, code: str, status: Gtk.Label | None) -> None:
        if self.busy or self.sync is None:
            return
        self.busy = True
        c.toast(self.sheet.card, "Fetching the project…", kind="done")

        def done(path) -> None:
            self.busy = False
            opener = getattr(self.window, "open_project", None)
            if callable(opener):
                try:
                    opener(path)
                    from .. import sync_ui
                    sync_ui.start_sync(self.window, path)
                except Exception as error:  # noqa: BLE001
                    log.warning("couldn't open %s: %s", path, error)
            self.ctx = c.context_for(self.window, {"project_dir": path})
            self.say("ok", f"{path.name} is on this computer now, and syncing.")

        def failed(error: BaseException) -> None:
            self.busy = False
            self.toast_error(error)

        c.run_async(lambda: self.sync.accept_approval(code), done, failed)

    # Small parts

    def paste_line(self, placeholder: str, on_go) -> Gtk.Widget:
        entry = Gtk.Entry(hexpand=True, placeholder_text=placeholder)
        entry.update_property([Gtk.AccessibleProperty.LABEL], [placeholder])
        go = c.button("Check", style="fill", small=True, on_click=lambda: on_go(entry.get_text()))
        entry.connect("activate", lambda e: on_go(e.get_text()))
        opener = c.button("Open a file", icon="folder-open", style="fill", small=True,
                          on_click=lambda: self.open_file(on_go))
        field = c.hbox(c.glyph("inbox", 16), entry, spacing=8, css="pgs-field", hexpand=True)
        if self.sheet_phone():
            return c.vbox(field, c.hbox(go, opener, spacing=8), spacing=8)
        return c.hbox(field, go, opener, spacing=8)

    def code_box(self, code: str, file_text: str, file_name: str, what: str) -> Gtk.Widget:
        preview = code if len(code) <= 36 else f"{code[:30]}…{code[-4:]}"
        label = text(preview, "pgs-mono", wrap=False, ellipsize=True, hexpand=True)
        label.set_tooltip_text(f"{len(code)} characters")
        field = c.hbox(c.glyph("key-round", 16), label, spacing=8, css="pgs-field", hexpand=True)
        copy = c.button("Copy", icon="copy", style="key", small=True,
                        on_click=lambda: self.copy(code, f"Copied the {what}"))
        save = c.button("Save as a file", icon="download", style="fill", small=True,
                        on_click=lambda: self.save_file(file_name, file_text))
        if self.sheet_phone():
            return c.vbox(field, c.hbox(copy, save, spacing=8), spacing=8)
        return c.hbox(field, copy, save, spacing=8)

    def command_box(self, command: str, what: str) -> Gtk.Widget:
        preview = command if len(command) <= 60 else f"{command[:52]}…"
        label = text(preview, "pgs-mono", wrap=False, ellipsize=True, hexpand=True)
        field = c.hbox(c.glyph("terminal", 16), label, spacing=8, css="pgs-field", hexpand=True)
        copy = c.button("Copy", icon="copy", style="key", small=True,
                        on_click=lambda: self.copy(command, f"Copied {what}"))
        return c.hbox(field, copy, spacing=8)

    def copy(self, words: str, toast: str) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(words)
            c.toast(self.sheet.card, toast, kind="copied")

    def save_file(self, name: str, contents: str) -> None:
        if not hasattr(Gtk, "FileDialog"):
            self.copy(contents, "Copied it instead: this GTK can't save files from here")
            return
        dialog = Gtk.FileDialog(title="Save", initial_name=name, modal=True)

        def saved(dlg, result) -> None:
            try:
                target = dlg.save_finish(result)
            except GLib.Error:
                return                          # cancelled
            try:
                target.replace_contents(contents.encode(), None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None)
                c.toast(self.sheet.card, f"Saved {target.get_basename()}", kind="done")
            except GLib.Error as error:
                self.toast_error(error)

        dialog.save(self.window, None, saved)

    def open_file(self, on_text) -> None:
        if not hasattr(Gtk, "FileDialog"):
            return
        dialog = Gtk.FileDialog(title="Open", modal=True)

        def opened(dlg, result) -> None:
            try:
                source = dlg.open_finish(result)
                ok, data, _etag = source.load_contents(None)
            except GLib.Error:
                return
            if ok and len(data) < 256 * 1024:
                on_text(bytes(data).decode("utf-8", errors="replace"))

        dialog.open(self.window, None, opened)

    def toast_error(self, error: BaseException) -> None:
        words = str(error) or "That didn’t work."
        if self.sheet.depth > 1:
            c.toast(self.sheet.card, words, kind="error")
        else:
            self.say("error", words)


def card_list(icon: str, title: str, items: list[str]) -> Gtk.Widget:
    card = c.vbox(spacing=6, css="pgs-two-card")
    card.append(c.hbox(c.glyph(icon, 16, "pgs-callout-glyph"), text(title, "pgs-two-title"), spacing=8))
    for item in items:
        card.append(c.hbox(c.vbox(css="pgs-bullet-dot", valign=Gtk.Align.CENTER), text(item, "pgs-bullet"),
                           spacing=8))
    return card


def _device() -> str:
    name = socket.gethostname().split(".")[0]
    return name or "this computer"


def _lan_url(url: str) -> str:
    """Stage's address as people on the same network reach it (localhost means nothing to them)."""
    if not url:
        return ""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 9))   # no packet is sent; this only picks the outgoing interface
        address = probe.getsockname()[0]
        probe.close()
    except OSError:
        return ""
    return re.sub(r"//(localhost|127\.0\.0\.1|\[::1\])", f"//{address}", url)
