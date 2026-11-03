# SPDX-License-Identifier: Apache-2.0
"""Places: where the project lives and runs, who can get in, and its secrets.

- **Home** is this computer (with the honest warning) or a server.
- **Places** each have a role: Development, Production or a Preview target.
  Add one by telling Ari, or by SSH details with a connection test.
- **Who can get in**: each member's devices and certificate, with Remove and
  Let back in. Nobody passes keys around (core.security's project CA).
- **The vault**: secret names only, never values; add and rotate.
- **Keeping things safe**: production protection and the sandbox, stated plainly.

Everything is read from and written to the project through core.store,
core.places and core.security. Slow things (a connection test, setting up the
certificate authority, the vault) run off the main loop.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from gi.repository import Gtk

from . import common as c
from .common import Row, Tag, callout, get, section, text

ROLE_WORDS = {"development": "Development", "production": "Production", "preview": "Preview"}
ROLE_TONES = {"development": "run", "production": "red", "preview": None}
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$|^\[?[0-9A-Fa-f:]+\]?$")
_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$", re.IGNORECASE)


def present(window: Gtk.Window, **options) -> c.Sheet:
    return PlacesSheet(window, options).sheet


class PlacesSheet:
    def __init__(self, window: Gtk.Window, options: dict) -> None:
        self.ctx = c.context_for(window, options)
        name = self.ctx.name or "the project"
        self.sheet = c.Sheet(window, "Places", f"Where {name} lives and runs", name="places")
        self.adding: str | None = options.get("add") if isinstance(options.get("add"), str) else None
        self.tests: dict[str, str] = {}          # place id → "checking" | "ok" | message
        self.access: dict[str, Any] | None = None  # member id → CA entry, once read
        self.vault: dict[str, Any] | None = None
        self.place_list: list[dict] | None = None
        self.message: tuple[str, str] | None = None
        self.sheet.listener.on("places.changed", lambda **_: self.load_places())
        self.sheet.listener.on("project.changed", lambda **_: (self.ctx.__dict__.pop("_project", None),
                                                               self.load_places()))
        self.sheet.set_body(self.build())
        self.sheet.present()
        self.load_places()
        self.load_security()

    # ── data ──

    def project(self):
        return self.ctx.project

    def refresh(self, reload: bool = False) -> None:
        if reload:
            self.ctx.__dict__.pop("_project", None)
        if not self.sheet.closed:
            self.sheet.replace_top(self.build())

    def store(self):
        store = c.module("core.store")
        return store.ProjectStore(self.ctx.project_dir) if store and self.ctx.project_dir else None

    def load_security(self) -> None:
        project = self.project()
        if project is None or self.ctx.project_dir is None:
            return
        security = c.module("core.security")
        if security is None:
            return
        project_dir, project_id = self.ctx.project_dir, project.id

        def read() -> tuple[dict, dict]:
            ca = security.ProjectCA.load(project_id) if security.ProjectCA.exists(project_id) else None
            access = {"exists": ca is not None, "members": ca.members() if ca else {},
                      "issued": list(getattr(ca, "_state", {}).get("issued", [])) if ca else []}
            vault = security.Vault(project_dir, project_id).status()
            return access, vault

        def done(result) -> None:
            self.access, self.vault = result
            self.refresh()

        c.run_async(read, done, lambda error: self.say("error", f"Couldn't read who can get in: {error}"))

    def load_places(self) -> None:
        if self.ctx.project_dir is None:
            return
        places = c.module("core.places")
        project_dir = self.ctx.project_dir

        def read() -> list[dict]:
            if places is not None:
                return places.list_places(project_dir)
            project = c.module("core.store").ProjectStore(project_dir).read_project()
            return [dict(p.to_dict(), is_home=p.id == project.home, status="") for p in project.places]

        def done(found: list[dict]) -> None:
            self.place_list = found
            self.ctx.__dict__.pop("_project", None)
            self.refresh()

        c.run_async(read, done, lambda error: self.say("error", f"Couldn’t read the places: {error}"))

    def say(self, kind: str, words: str) -> None:
        self.message = (kind, words)
        self.refresh()

    # ── the page ──

    def build(self) -> Gtk.Widget:
        project = self.project()
        body = c.vbox()
        if project is None:
            body.append(c.empty("No project open", "Open a project to see where it lives and runs.", "server"))
            return body
        if self.message is not None:
            kind, words = self.message
            icon, tone = {"ok": ("circle-check", "good"), "info": ("info", "info")}.get(kind, ("triangle-alert", "warn"))
            body.append(callout(c.escape(words), icon=icon, tone=tone))
        body.append(self.home(project))
        body.append(self.places(project))
        body.append(self.who_can_get_in(project))
        body.append(self.vault_section(project))
        body.append(self.safety(project))
        return body

    # Home

    def home(self, project) -> Gtk.Widget:
        box = c.vbox()
        box.append(section("Home", first=self.message is None))
        servers = [p for p in project.places if p.kind == "ssh" and p.role == "development"]
        here = project.home in ("", "this-computer") or project.place(project.home) is None
        grid = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True)
        first = None
        choices = [("this-computer", "This computer", self.this_computer_words(), "monitor")]
        choices += [(p.id, p.name, p.description or p.host, "server") for p in servers]
        if not servers:
            choices.append((None, "A server", "Add a development server first", "server"))
        for index, (place_id, title, sub, icon) in enumerate(choices):
            tile = Gtk.ToggleButton(active=(place_id == "this-computer" and here) or (place_id == project.home),
                                    sensitive=place_id is not None, hexpand=True)
            tile.add_css_class("pgs-home-choice")
            tile.set_child(c.hbox(c.lead_tile(icon), c.vbox(text(title, "pgs-home-title", wrap=False),
                                                         text(sub, "pgs-home-sub"), spacing=2, hexpand=True),
                                  spacing=10))
            tile.update_property([Gtk.AccessibleProperty.LABEL], [f"Home: {title}"])
            if first is None:
                first = tile
            else:
                tile.set_group(first)
            if place_id:
                tile.connect("toggled", lambda t, pid=place_id: t.get_active() and self.set_home(pid))
            grid.attach(tile, index % 2, index // 2, 1, 1)
        box.append(grid)
        if here:
            box.append(callout(
                "<b>Teammates can work only while this computer is on.</b> If it sleeps, their Stage and "
                "agents pause until it wakes. That’s fine for a project of your own.",
                icon="triangle-alert", tone="warn"))
        else:
            place = project.place(project.home)
            box.append(callout(
                f"{c.escape(project.name)} lives on {c.escape(place.name if place else 'the server')}, so "
                f"teammates can work any time. <b>Your copy</b> stays in sync at "
                f"<tt>{c.escape(self.ctx.display_path)}</tt>, so you can read and plan offline.",
                icon="refresh-ccw", tone="info"))
        box.get_last_child().set_margin_top(10)
        return box

    def this_computer_words(self) -> str:
        import socket
        name = socket.gethostname().split(".")[0]
        return f"{name}, where you are now" if name else "Where you are now"

    def set_home(self, place_id: str) -> None:
        project = self.project()
        if project is None or project.home == place_id:
            return
        places = c.module("core.places")

        def write() -> None:
            if places is not None:
                places.set_home(self.ctx.project_dir, place_id)
                return
            store = self.store()
            with store.locked():
                current = store.read_project()
                current.home = place_id
                store.write_project(current)

        c.run_async(write, lambda _r: self.refresh(reload=True),
                    lambda error: self.say("error", f"Couldn’t change the home: {error}"))

    # Places

    def places(self, project) -> Gtk.Widget:
        box = c.vbox()
        box.append(section("Places", action=c.link_button("Add a place", "plus", self.toggle_add)))
        if self.adding:
            box.append(self.add_form())
        rows = []
        if self.place_list is None:
            rows.append(Row("Reading the places…", None, lead=shared_spinner_tile()))
        for place in self.place_list or []:
            rows.append(self.place_row(project, place))
        box.append(c.ListGroup(*rows) if rows else c.ListGroup(
            Row("Just this computer", "Add a server when you want teammates, or want it on your phone away from home.",
                lead="monitor")))
        return box

    def place_row(self, project, place: dict) -> Gtk.Widget:
        role = place.get("role") if place.get("role") in ROLE_WORDS else "development"
        kind = place.get("kind")
        if kind != "ssh":
            icon = "monitor"
        elif role == "production":
            icon = "globe"
        elif role == "preview":
            icon = "smartphone"
        else:
            icon = "server"
        tags = [Tag(ROLE_WORDS[role], ROLE_TONES[role])]
        if place.get("is_home"):
            tags.append(Tag("Home", "good"))
        lines = []
        if kind == "ssh" and place.get("host"):
            port = int(place.get("port") or 22)
            lines.append(f"{place.get('user') + '@' if place.get('user') else ''}{place['host']}"
                         f"{':' + str(port) if port != 22 else ''}")
        if place.get("description"):
            lines.append(place["description"])
        if place.get("url"):
            lines.append(place["url"])
        # (core.places' status already says production is protected; it isn't repeated here.)
        state = self.tests.get(place["id"])
        status = place.get("status") or ""
        if state == "checking":
            end = c.hbox(shared_spinner(), text("Checking", "pgs-row-end", wrap=False), spacing=6)
        elif kind != "ssh":
            end = c.hbox(c.shared.dot("good"), text("On", "pgs-row-end", wrap=False), spacing=6)
        else:
            end = c.button("Test", style="fill", small=True, on_click=lambda pid=place["id"]: self.test(pid))
        guard = "Agents can't change it; releases go through Review."
        if guard in status and "review" in str(place.get("description") or "").lower():
            # The place's own description already says production only takes reviewed releases.
            status = status.replace(guard, "").strip()
        if status:
            lines.append(status.replace("'", "’"))
        return Row(place.get("name") or "Server", "\n".join(lines) or None, lead=icon, tags=tags, end=end)

    def toggle_add(self) -> None:
        self.adding = None if self.adding else "ari"
        self.refresh()

    def add_form(self) -> Gtk.Widget:
        form = c.vbox(spacing=10, css="pgs-form-card")
        form.set_margin_bottom(10)
        form.append(c.segmented([("ari", "Tell Ari"), ("ssh", "SSH details")], self.adding,
                                label="How to add it", on_change=self.switch_add))
        form.get_first_child().set_halign(Gtk.Align.START)
        if self.adding == "ari":
            entry = Gtk.Entry(hexpand=True, placeholder_text="Our test server is at 203.0.113.9")
            entry.update_property([Gtk.AccessibleProperty.LABEL], ["Tell Ari about the place"])
            go = c.button("Add", style="key", small=True, on_click=lambda: self.tell_ari(entry.get_text()))
            entry.connect("activate", lambda e: self.tell_ari(e.get_text()))
            form.append(c.hbox(c.glyph("message-circle", 16), entry, go, spacing=8, css="pgs-field"))
            form.append(text("Ari connects, works out what the machine can do, and suggests a role. "
                             "It asks you before it changes anything on the server.", "pgs-field-hint"))
            return form
        self.f_name = field_entry("Name", "Dev server")
        self.f_host = field_entry("Address", "203.0.113.9")
        self.f_user = field_entry("User name", "kiln")
        self.f_port = field_entry("Port", "22")
        self.f_port.entry.set_text("22")
        grid = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True)
        grid.attach(self.f_name, 0, 0, 1, 1)
        grid.attach(self.f_host, 1, 0, 1, 1)
        grid.attach(self.f_user, 0, 1, 1, 1)
        grid.attach(self.f_port, 1, 1, 1, 1)
        form.append(grid)
        self.f_role = "development"
        role = c.segmented([("development", "Development"), ("production", "Production"), ("preview", "Preview")],
                           "development", label="Role", on_change=lambda key: setattr(self, "f_role", key))
        self.f_status = text("", "pgs-field-hint")
        self.f_status.set_visible(False)
        form.append(c.hbox(role, c.vbox(hexpand=True),
                           c.button("Test", style="fill", small=True, on_click=self.test_form),
                           c.button("Add", style="key", small=True, on_click=self.add_ssh), spacing=8))
        form.append(self.f_status)
        form.append(text("Development runs the app and the agents for Stage. Production is the live app; "
                         "agents can never write there. Preview is a phone or computer to try it on.",
                         "pgs-field-hint"))
        return form

    def switch_add(self, mode: str) -> None:
        self.adding = mode
        self.refresh()

    def tell_ari(self, words: str) -> None:
        words = words.strip()
        if not words or self.ctx.project_dir is None:
            return
        model = c.module("core.model")
        store = self.store()

        def send() -> None:
            # The general conversation is where Ari picks this up; the runner answers there.
            entry = model.ThreadEntry(kind="message", role="person", text=words)
            store.append_thread(model.GENERAL_CHAT_ID, entry)

        def sent(_r) -> None:
            self.adding = None
            self.say("ok", "Sent to Ari in Talk to Ari. It will ask before it changes anything.")

        c.run_async(send, sent, lambda error: self.say("error", f"Couldn’t send that to Ari: {error}"))

    def form_place(self) -> dict:
        places = c.module("core.places")
        host = self.f_host.entry.get_text().strip()
        user = self.f_user.entry.get_text().strip()
        port = self.f_port.entry.get_text().strip() or "22"
        if places is not None:
            host, user, port = places.validate_host(host), places.validate_user(user), places.validate_port(port)
        elif not _HOST.match(host) or not _USER.match(user) or not port.isdigit():
            raise ValueError("Check the address, the user name and the port.")
        return {"id": "new", "name": self.f_name.entry.get_text().strip(), "role": self.f_role, "kind": "ssh",
                "host": host, "user": user, "port": int(port)}

    def test_form(self) -> None:
        places = c.module("core.places")
        try:
            place = self.form_place()
        except Exception as error:  # noqa: BLE001 - PlaceError: plain words for the person
            self.form_status(str(error), ok=False)
            return
        if places is None:
            self.form_status("Connection tests need the places module, which isn’t installed.", ok=False)
            return
        self.form_status("Checking the connection…", ok=None)
        project = self.project()
        c.run_async(lambda: places.test_connection(place, project.id if project else None),
                    lambda result: self.form_status(*_test_words(result)),
                    lambda error: self.form_status(str(error), ok=False))

    def form_status(self, words: str, ok: bool | None) -> None:
        status = getattr(self, "f_status", None)
        if status is None or status.get_root() is None:
            return
        status.set_label(words)
        status.set_visible(True)
        for css in ("pgs-form-ok", "pgs-form-error"):
            status.remove_css_class(css)
        if ok is True:
            status.add_css_class("pgs-form-ok")
        elif ok is False:
            status.add_css_class("pgs-form-error")

    def add_ssh(self) -> None:
        places = c.module("core.places")
        try:
            place = self.form_place()
        except Exception as error:  # noqa: BLE001
            self.form_status(str(error), ok=False)
            return
        if places is None:
            self.form_status("Adding servers needs the places module, which isn’t installed.", ok=False)
            return
        # The form closes now: the place is written first and the sheet redraws when it lands, which would
        # otherwise wipe the form mid-check. The line at the top says what's happening until the check ends.
        self.adding = None
        self.say("info", f"Adding {place['name']} and checking that it answers. This can take a few seconds…")

        def add() -> dict:
            return places.add_place(self.ctx.project_dir, place["name"], role=place["role"], kind="ssh",
                                    host=place["host"], user=place["user"], port=place["port"], test=True)

        def added(result: dict) -> None:
            self.adding = None
            self.load_places()
            name = result["place"]["name"]
            test = result.get("test") or {}
            if test.get("ok"):
                self.say("ok", f"Added {name}, and it answered.")
            else:
                self.say("error", f"Added {name}, but it didn’t answer yet: {test.get('message') or 'not checked'} "
                                  "A new server usually needs its one-time setup first.")

        c.run_async(add, added, lambda error: self.say("error", f"Couldn’t add {place['name']}: {error}"))

    def test(self, place_id: str) -> None:
        places = c.module("core.places")
        if places is None:
            return
        self.tests[place_id] = "checking"
        self.refresh()

        def done(_result) -> None:
            self.tests.pop(place_id, None)
            self.load_places()

        def failed(error) -> None:
            self.tests.pop(place_id, None)
            self.say("error", str(error))

        c.run_async(lambda: places.test_place(self.ctx.project_dir, place_id), done, failed)

    # Who can get in

    def who_can_get_in(self, project) -> Gtk.Widget:
        box = c.vbox()
        box.append(section(
            "Who can get in",
            note=f"Nobody passes keys around. Each person’s device has its own key, tied to their Luma Connect "
                 f"identity, and the servers let them in with a certificate {project.name} signs. It lasts "
                 f"12 hours and renews on its own."))
        access = self.access
        rows = []
        if access is None:
            rows.append(Row("Reading who has access…", None, lead=shared_spinner_tile()))
        else:
            ca_members = access.get("members", {})
            issued = access.get("issued", [])
            for member in project.members:
                rows.append(self.member_row(project, member, ca_members.get(member.id), issued, access["exists"]))
            if not access["exists"]:
                rows.append(Row("Server access isn’t set up yet",
                                f"Set it up once and {project.name} can let people onto its servers with "
                                "short-lived certificates.", lead="shield-check",
                                end=c.button("Set up", style="fill", small=True, on_click=self.set_up_ca)))
        box.append(c.ListGroup(*rows))
        return box

    def member_row(self, project, member, entry: dict | None, issued: list, ca_exists: bool) -> Gtk.Widget:
        removed = bool(entry and entry.get("removed"))
        devices = [k.get("device") or "device" for k in (entry or {}).get("keys", [])]
        mine = member.role == "owner" and member.id == (project.owner.id if project.owner else "")
        if removed:
            sub = "No access. Their certificates stopped working everywhere."
        elif devices:
            sub = f"{_join(devices)}"
            expiry = _latest_expiry(issued, member.id)
            if expiry:
                sub += f" · certificate valid until {expiry}"
        elif ca_exists:
            sub = "No device added yet. They’re let in when they open the project."
        elif mine:
            sub = "You, on this computer"
        else:
            sub = "Server access isn’t set up yet."
        end = None
        if not mine and ca_exists and entry is not None:
            end = c.button("Let back in" if removed else "Remove", style="fill", small=True,
                           on_click=lambda m=member, back=removed: self.change_access(m, back))
        face = c.shared.person_face(member.name or member.id, 34)
        return Row(member.name or member.id, sub, lead=face, tags=[Tag(member.role.capitalize())], end=end,
                   dim=removed)

    def set_up_ca(self) -> None:
        project = self.project()
        security = c.module("core.security")
        if project is None or security is None:
            return
        owner = project.owner

        def make() -> None:
            ca = security.ProjectCA.open(project.id, create=True)
            key = security.device_key(create=True)
            if owner is not None and key is not None:
                ca.add_member(owner.id, owner.name, "owner", key.public_key, key.device_name)

        c.run_async(make, lambda _r: (self.say("ok", "Server access is set up."), self.load_security()),
                    lambda error: self.say("error", f"Couldn’t set up server access: {error}"))

    def change_access(self, member, back: bool) -> None:
        project = self.project()
        security = c.module("core.security")
        if project is None or security is None:
            return
        if not back:
            def confirm(_checked: bool = False) -> None:
                self._revoke(project, member, security)
            if c.kit is not None:
                c.kit.DestructiveDialog.ask(
                    self.sheet.card, title=f"Remove {member.first_name}’s access?",
                    body=f"Their certificates stop working on every server, and the vault is locked again "
                         f"without them.", action="Remove", icon="user-minus", on_confirm=confirm)
            else:
                confirm()
            return
        entry = (self.access or {}).get("members", {}).get(member.id) or {}
        keys = entry.get("keys", [])
        if not keys:
            return

        def grant() -> None:
            for key in keys:
                security.grant_member(self.ctx.project_dir, project.id, member.id, member.name, member.role,
                                      key["public_key"], key.get("device") or "device")

        c.run_async(grant, lambda _r: (self.say("ok", f"{member.first_name} can get in again."), self.load_security()),
                    lambda error: self.say("error", f"Couldn’t let them back in: {error}"))

    def _revoke(self, project, member, security) -> None:
        def revoke() -> dict:
            return security.revoke_member(self.ctx.project_dir, project.id, member.id)

        def done(result: dict) -> None:
            change = result.get("to_change") or []
            words = f"{member.first_name} can’t get in any more."
            if change:
                words += f" They could read {_join(change)}, so change those where they come from."
            self.say("ok", words)
            self.load_security()

        c.run_async(revoke, done, lambda error: self.say("error", f"Couldn’t remove their access: {error}"))

    # The vault

    def vault_section(self, project) -> Gtk.Widget:
        box = c.vbox()
        box.append(section("Project vault", action=c.link_button("Add a secret", "plus", self.add_secret)
                           if self.vault and self.vault.get("member") else None))
        vault = self.vault
        rows = []
        if vault is None:
            rows.append(Row("Opening the vault…", None, lead=shared_spinner_tile()))
        elif not vault.get("available"):
            rows.append(Row("The vault isn’t available on this computer", vault.get("reason") or None, lead="shield"))
        elif not vault.get("exists"):
            rows.append(Row("No vault yet",
                            f"Keep passwords and keys for {project.name} here instead of in files. They’re "
                            "locked to the devices of the people on the project.", lead="shield",
                            end=c.button("Set up", style="fill", small=True, on_click=self.create_vault)))
        else:
            names = vault.get("names", [])
            rows.append(Row(c.shared.plural(len(names), "secret"), None, lead="shield",
                            tags=[Tag("End-to-end encrypted", "good")]))
            rows[-1].words.append(text(
                f"Locked to the devices of the people on {project.name}. The Hub only ever carries scrambled "
                "data, so not even Luma can read these.", "pgs-row-sub"))
            for name in names:
                rotate = c.button("Rotate", style="fill", small=True, on_click=lambda n=name: self.rotate(n),
                                  sensitive=bool(vault.get("member")),
                                  tooltip="Lock it again with a fresh key")
                rows.append(Row(name, None, lead="key-round", title_class="pgs-mono",
                                end=c.hbox(text("••••••", "pgs-secret", wrap=False), rotate, spacing=10)))
            if not vault.get("member"):
                rows.append(Row("This device can’t open the vault yet",
                                "Someone already on the project adds your device, and then you can.",
                                lead="lock"))
        if getattr(self, "secret_form", None) is not None:
            rows.append(self.secret_form)
        box.append(c.ListGroup(*rows))
        return box

    def create_vault(self) -> None:
        project = self.project()
        security = c.module("core.security")
        owner = project.owner if project else None
        if owner is None or security is None:
            return

        def create() -> None:
            security.Vault(self.ctx.project_dir, project.id).create(owner.id, owner.name)

        c.run_async(create, lambda _r: (self.say("ok", "The vault is ready."), self.load_security()),
                    lambda error: self.say("error", f"Couldn’t set up the vault: {error}"))

    def add_secret(self) -> None:
        name = Gtk.Entry(placeholder_text="DATABASE_URL", hexpand=True)
        name.update_property([Gtk.AccessibleProperty.LABEL], ["Secret name"])
        value = Gtk.PasswordEntry(placeholder_text="Value", hexpand=True, show_peek_icon=True)
        value.update_property([Gtk.AccessibleProperty.LABEL], ["Secret value"])
        status = text("Names use capital letters, numbers and underscores. The value is encrypted before it’s "
                      "saved and never shown again.", "pgs-field-hint")

        def save() -> None:
            secret_name, secret_value = name.get_text().strip(), value.get_text()
            if not re.match(r"^[A-Z][A-Z0-9_]{0,63}$", secret_name):
                status.set_label("Use capital letters, numbers and underscores, like TEXTING_SERVICE_KEY.")
                status.add_css_class("pgs-form-error")
                return
            if not secret_value:
                status.set_label("Type the value, or paste it.")
                status.add_css_class("pgs-form-error")
                return
            project = self.project()
            security = c.module("core.security")

            def write() -> None:
                security.Vault(self.ctx.project_dir, project.id).set(secret_name, secret_value)

            value.set_text("")
            self.secret_form = None
            c.run_async(write, lambda _r: (self.say("ok", f"Saved {secret_name} in the vault."), self.load_security()),
                        lambda error: self.say("error", f"Couldn’t save it: {error}"))

        name.connect("activate", lambda _e: value.grab_focus())
        value.connect("activate", lambda _e: save())
        form = c.vbox(c.hbox(c.vbox(name, css="pgs-field", hexpand=True), c.vbox(value, css="pgs-field", hexpand=True),
                             c.button("Save", style="key", small=True, on_click=save), spacing=8),
                      status, spacing=8)
        form.set_margin_top(10)
        form.set_margin_bottom(12)
        self.secret_form = form
        self.refresh()
        name.grab_focus()

    def rotate(self, secret: str) -> None:
        project = self.project()
        security = c.module("core.security")

        def rotate() -> None:
            security.Vault(self.ctx.project_dir, project.id).rotate(secret)

        c.run_async(rotate, lambda _r: self.say("ok", f"{secret} is locked again with a fresh key."),
                    lambda error: self.say("error", f"Couldn’t rotate {secret}: {error}"))

    # Keeping things safe

    def safety(self, project) -> Gtk.Widget:
        box = c.vbox()
        box.append(section("Keeping things safe"))
        production = [p for p in project.places if p.role == "production"]
        rows = [
            Row("Agents can’t change Production",
                "Changes go live only through Review, and every release saves a restore point you can go back to."
                + ("" if production else " There’s no production place yet."),
                lead="shield-check", end=c.switch(True, label="Agents can’t change Production", sensitive=False)),
            Row(f"Agents stay inside the {project.name} folder",
                "They can write only in the project, and reach the internet through a short list of allowed sites.",
                lead="folder"),
            Row("Stage has its own copy of the data",
                "Testing never touches the real thing. Going back covers data as well as code.", lead="database"),
        ]
        box.append(c.ListGroup(*rows))
        return box


# ── small helpers ──

def field_entry(label: str, placeholder: str):
    if c.kit is not None:
        return c.kit.TextField(label, placeholder=placeholder)
    box = c.vbox(text(label, "pgs-field-hint"), Gtk.Entry(placeholder_text=placeholder), spacing=4)
    box.entry = box.get_last_child()
    return box


def shared_spinner() -> Gtk.Widget:
    return c.shared.Spinner(14)


def shared_spinner_tile() -> Gtk.Widget:
    tile = c.vbox(css="pgs-lead", valign=Gtk.Align.CENTER)
    spin = c.shared.Spinner(16)
    spin.set_vexpand(True)
    tile.append(spin)
    return tile


def _test_words(result: Any) -> tuple[str, bool]:
    if result is None:
        return "Playground couldn’t check the connection.", False
    ok = bool(get(result, "ok", result is True))
    detail = get(result, "message") or get(result, "detail") or get(result, "summary") or ""
    if ok:
        return detail or "It answered. Ready to use.", True
    return detail or "It didn’t answer. Check the address and that SSH is on.", False


def _join(words: list[str]) -> str:
    words = [w for w in words if w]
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _latest_expiry(issued: list, member_id: str) -> str:
    """When the newest certificate for this member runs out, as a time of day."""
    shared = c.shared
    latest = None
    for item in issued:
        if item.get("member") != member_id:
            continue
        moment = shared.parse_ts(item.get("valid_to") or item.get("valid_before") or item.get("until"))
        if moment and (latest is None or moment > latest):
            latest = moment
    if latest is None:
        return ""
    from datetime import datetime
    if latest < datetime.now():
        return ""
    return shared.time_words(latest)
