# SPDX-License-Identifier: Apache-2.0
"""Welcome to Playground: what Playground shows first when no agent is connected yet, since nothing gets built without
one. One tap each, and every choice goes through the flow Playground already has for it:

- **Your ChatGPT plan**: Continue with ChatGPT, OpenAI's own sign-in in the browser, and Ari runs on the
  plan (agents/chatgpt.py, as Luma's Ari does). With that turned off, Codex signed in with ChatGPT instead.
- **Your Claude plan**: Claude Code, the same way.
- **A model on this computer**: Ollama. With Ollama running, a first model sized to this computer's memory
  downloads here, then it's added; without it, where to get it.
- **Other ways**: the picker's Add an agent (an API key, Gemini, OpenCode, another account).

"Not now" is remembered (`setup.json` in Playground's config folder), so it doesn't come back on every start.
"""
from __future__ import annotations

import json
from typing import Any

from gi.repository import GLib, Gtk

from . import common as c
from .common import Row, section, text

OLLAMA_DOWNLOAD = "https://ollama.com/download"


def _state_path():
    paths = c.module("core.paths")
    return paths.config_dir() / "setup.json" if paths is not None else None


def dismissed() -> bool:
    path = _state_path()
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("dismissed")) if path else False
    except (OSError, ValueError):
        return False


def dismiss() -> None:
    path = _state_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"dismissed": True}), encoding="utf-8")
    except OSError:
        pass


def _openai_mark() -> Gtk.Widget:
    """Ari's OpenAI tile (data/marks/openai.svg), else Playground's own Codex tile."""
    paths = c.module("core.paths")
    widgets = c.module("app.widgets")
    try:
        from gi.repository import Gdk
        texture = Gdk.Texture.new_from_filename(str(paths.app_data("marks", "openai.svg")))
        return widgets.Mark(size=28, texture=texture, accessible="ChatGPT")
    except Exception:  # noqa: BLE001 - a missing file shouldn't cost the row its lead
        return c.mark({"provider": "codex", "label": "ChatGPT"}, size=28)


def needed() -> bool:
    """No agent connected yet, and the person hasn't said Not now. (Cheap: one small file each.)"""
    connections = c.module("agents.connections")
    if connections is None or dismissed():
        return False
    try:
        return not connections.ConnectionStore().list()
    except Exception:  # noqa: BLE001 - a broken store is the picker's to explain, not the splash's
        return False


def present(window: Gtk.Window, **options) -> c.Sheet:
    return Setup(window, options).sheet


class Setup:
    def __init__(self, window: Gtk.Window, options: dict) -> None:
        self.window = window
        self.ollama: Any = None
        self.pulling: str | None = None      # words while a model downloads
        self.signing: Any = None             # the ChatGPT sign-in under way
        self.sheet = c.Sheet(window, "Welcome to Playground", "", name="setup")
        self.body = c.vbox(spacing=0, css="pgs-setup")
        self.sheet.set_body(self.body)
        later = c.button("Not now", style="plain", on_click=self.not_now)
        self.sheet.set_foot(c.vbox(hexpand=True), later)
        self.sheet.present()
        self.build()
        self.look_for_ollama()

    # ── what's shown ──

    def build(self) -> None:
        c.clear(self.body)
        self.body.append(text("Pick an AI to build with", "pgs-new-title",
                              accessible_role=Gtk.AccessibleRole.HEADING))
        lede = text("It writes the code, runs it and shows you. Choose one to start; you can add others any "
                    "time from the agent menu.", "pgs-new-lede")
        lede.set_margin_top(6)
        lede.set_margin_bottom(14)
        self.body.append(lede)
        self.body.append(c.ListGroup(
            self.chatgpt_row(),
            Row("Use your Claude plan", "Claude Code signs in with your Claude account, in your browser.",
                lead=c.mark({"provider": "claude", "label": "Claude"}, size=28),
                end=c.button("Sign in", style="fill", small=True, on_click=lambda: self.plan("claude"))),
        ))
        self.body.append(section("On this computer", note="Free, private, and nothing leaves this computer. "
                                                          "Slower than a plan, and it needs a big download."))
        self.body.append(c.ListGroup(self.local_row()))
        other = c.ListGroup(Row("Other ways", "An API key, Gemini, OpenCode, or an account Playground didn’t find.",
                                lead="sparkles", chevron=True, on_activate=self.other_ways))
        other.set_margin_top(14)
        self.body.append(other)

    def chatgpt_row(self) -> Gtk.Widget:
        chatgpt = c.module("agents.chatgpt")
        if chatgpt is None or not chatgpt.enabled():
            return Row("Use your ChatGPT plan", "Codex signs in with your ChatGPT account, in your browser.",
                       lead=c.mark({"provider": "codex", "label": "ChatGPT"}, size=28),
                       end=c.button("Sign in", style="key", small=True, on_click=lambda: self.plan("codex")))
        lead = _openai_mark()
        if self.signing is not None:
            return Row("Use your ChatGPT plan", "Finish signing in in your browser. Ari carries on here after.",
                       lead=lead, end=c.button("Cancel", style="plain", small=True, on_click=self.cancel_sign_in))
        # OpenAI's wording and mark for the button (developers.openai.com/siwc/ui-ux-guidelines).
        return Row("Use your ChatGPT plan", "Ari works on your plan. No API key, nothing to download.", lead=lead,
                   end=c.button(chatgpt.BUTTON_LABEL, icon="chatgpt", style="key", small=True,
                                on_click=self.continue_with_chatgpt))

    def continue_with_chatgpt(self) -> None:
        """OpenAI's sign-in in the browser; then Ari's ChatGPT connection, ready for every conversation."""
        chatgpt, connections = c.module("agents.chatgpt"), c.module("agents.connections")
        self.signing = chatgpt.SignInFlow("ari-chatgpt")
        flow = self.signing
        self.build()

        def work():
            account = flow.run()
            store = connections.ConnectionStore()
            if store.get("ari-chatgpt") is None:
                store.add(connections.Connection(id="ari-chatgpt", label="Ari", kind="ari", provider="ari",
                                                 endpoint="chatgpt", sharing="just-me"))
            return account

        def done(account) -> None:
            self.signing = None
            if not account.plan_usage:
                self.build()
                c.toast(self.sheet.card, "You're signed in, but using your ChatGPT plan here wasn't allowed. "
                                         "Continue again and allow it.", kind="error")
                return
            self.sheet.close()
            c.toast(self.window, f"Signed in{' as ' + account.email if account.email else ''}. Ari uses your "
                                 "ChatGPT plan.", kind="added")

        def failed(error) -> None:
            self.signing = None
            if not self.sheet.closed:
                self.build()
                if "cancel" not in str(error).lower():
                    c.toast(self.sheet.card, str(error), kind="error")

        c.run_async(work, done, failed)

    def cancel_sign_in(self) -> None:
        if self.signing is not None:
            self.signing.cancel()

    def local_row(self) -> Gtk.Widget:
        ollama, chosen = self.ollama, c.module("agents.ollama")
        model, size = chosen.starter_model() if chosen is not None else ("qwen3", "a few GB")
        lead = c.mark({"provider": "ollama", "label": "Ollama"}, size=28)
        if self.pulling:
            return Row(f"Downloading {model}", self.pulling, lead=lead)
        if ollama is None:
            return Row("A model on this computer", "Looking for Ollama…", lead=lead)
        if not ollama.installed:
            return Row("A model on this computer", "Install Ollama first, then come back here.", lead=lead,
                       end=c.hbox(c.button("Get Ollama", icon="external-link", style="fill", small=True,
                                           on_click=lambda: Gtk.UriLauncher.new(OLLAMA_DOWNLOAD).launch(
                                               self.window, None, None)),
                                  c.button("Check again", style="plain", small=True, on_click=self.look_for_ollama),
                                  spacing=6))
        if not ollama.running:
            return Row("A model on this computer", "Ollama is installed but isn’t running. Open it, then check "
                       "again.", lead=lead, end=c.button("Check again", style="fill", small=True,
                                                         on_click=self.look_for_ollama))
        if ollama.models:
            first = ollama.models[0].name
            return Row(f"Use {first}", "Already on this computer, through Ollama.", lead=lead,
                       end=c.button("Use it", style="fill", small=True, on_click=lambda: self.add_local(first)))
        return Row(f"Download {model}", f"About {size}, sized to this computer’s memory, through Ollama.",
                   lead=lead, end=c.button("Download", style="fill", small=True,
                                           on_click=lambda: self.download(model)))

    # ── choices ──

    def plan(self, provider: str) -> None:
        """The picker's Add an agent, going straight on with this one (set up if needed, then its own sign-in)."""
        self.sheet.close()
        c.open_sheet(self.window, "picker", page="add", start=provider)

    def other_ways(self) -> None:
        self.sheet.close()
        c.open_sheet(self.window, "picker", page="add")

    def not_now(self) -> None:
        dismiss()
        self.sheet.close()

    def look_for_ollama(self) -> None:
        ollama = c.module("agents.ollama")
        if ollama is None:
            return
        self.ollama = None
        self.build()
        c.run_async(ollama.status, self.found_ollama, lambda _error: None)

    def found_ollama(self, status) -> None:
        self.ollama = status
        if not self.sheet.closed:
            self.build()

    def download(self, model: str) -> None:
        ollama = c.module("agents.ollama")
        self.pulling = "Starting…"
        self.build()
        last = [0.0]

        def progress(words: str, part: float | None) -> None:
            now = GLib.get_monotonic_time() / 1e6
            if now - last[0] < 0.3 and part not in (None, 1.0):
                return
            last[0] = now
            line = f"{words}… {int(part * 100)}%" if part is not None else f"{words}…"
            GLib.idle_add(lambda: (self.show_progress(line), False)[1])

        def done(_result) -> None:
            self.pulling = None
            self.add_local(model)

        def failed(error) -> None:
            self.pulling = None
            if not self.sheet.closed:
                self.build()
                c.toast(self.sheet.card, str(error), kind="error")

        c.run_async(lambda: ollama.pull(model, on_progress=progress), done, failed)

    def show_progress(self, line: str) -> None:
        if self.pulling is not None and not self.sheet.closed:
            self.pulling = line
            self.build()

    def add_local(self, model: str) -> None:
        """The same connection the picker makes for an Ollama model."""
        connections, paths = c.module("agents.connections"), c.module("core.paths")
        label = model.split(":")[0].replace("-", " ").title() + (f" {model.split(':')[1].upper()}"
                                                                   if ":" in model else "")
        conn = connections.Connection(id=paths.slugify(f"local-{model}", 40), label=label, kind="local",
                                      provider="ollama", endpoint="ollama", base_url="http://localhost:11434/v1",
                                      model=model)

        def write():
            return connections.ConnectionStore().add(conn)

        def done(saved) -> None:
            if not self.sheet.closed:
                self.sheet.close()
            c.toast(self.window, f"{saved.label} is ready. Describe what you want to make.", kind="added")

        c.run_async(write, done, lambda error: c.toast(self.sheet.card, str(error), kind="error"))
