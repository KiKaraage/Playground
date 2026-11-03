# SPDX-License-Identifier: Apache-2.0
"""Starting a new project, the most important flow (v73 shots 08 and 09).

1. **What do you want to make?** A big field, examples written by Luma, and a
   voice button.
2. **Two questions**, answered with a tap: who uses it, and where.
3. **A Brief and a Recipe** are proposed, and either can be edited in place.
4. **Create** makes the real folder through core.project.create_project.
5. **What was made**: ~/Documents/Playground Projects/<name>, AGENTS.md, the
   plan and the history, with AGENTS.md one tap away.
6. **The first goal starts**: "Get a first version on screen".

The questions and the proposal come from Ari when an Ari connection that can
answer is set up (a local model or an API key, through agents.ari). Otherwise
they come from Playground's own templates, and the sheet says so plainly.
Model calls and folder creation run off the main loop.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from gi.repository import Gio, GLib, Gtk

from . import common as c
from .common import Tag, callout, text

EXAMPLES = [
    ("calendar", "A sign-up page for my pottery classes"),
    ("gamepad-2", "A game my little brother can play on his phone"),
    ("leaf", "A tool to track my plant watering"),
]

# Playground's own starting points, used when no agent can answer yet.
TEMPLATES = {
    "plants": {
        "example": "A tool to track my plant watering",
        "name": "Sprig", "title": "know which plants are thirsty",
        "what": "A simple way to keep track of when each plant was last watered, and which ones need water today.",
        "good": "Opening it shows what needs water today. Logging a watering is one tap.",
        "questions": [("Who will use it?", ["Just me", "Me and others"]),
                      ("Where will it be used?", ["On phones", "On computers", "Both"])],
    },
    "game": {
        "example": "A game my little brother can play on his phone",
        "name": "Bubble Pop", "title": "pop as many bubbles as you can", "who": "Kids {a}, playing on their own.",
        "what": "A bright, simple game: bubbles float up, and you tap them to pop them before they reach the top.",
        "good": "Someone can start playing without help, and a round lasts about a minute.",
        "questions": [("Who will play?", ["Under 6", "6 to 9", "10 and up"]),
                      ("Where will they play?", ["On a phone", "On a tablet", "Both"])],
    },
    "classes": {
        "example": "A sign-up page for my pottery classes",
        "name": "Clay Club", "title": "book a spot in a class",
        "what": "A page that lists this week’s classes and lets people book a spot.",
        "good": "Booking a class takes under a minute, and nobody gets double-booked.",
        "questions": [("Who will sign up?", ["Students", "Parents for their kids", "Both"]),
                      ("Where will they sign up?", ["On phones", "On computers", "Both"])],
    },
    "general": {
                "name": "", "title": "",
        "what": "",
        "good": "It does the one main thing in a couple of taps, and it’s clear what to do first.",
        "questions": [("Who will use it?", ["Just me", "Me and others"]),
                      ("Where will it be used?", ["On phones", "On computers", "Both"])],
    },
}


@dataclass
class Proposal:
    name: str = ""
    title: str = ""
    what: str = ""
    who: str = ""
    where: str = ""
    good: str = ""
    recipe_summary: str = ""
    parts: list[tuple[str, str, str]] = field(default_factory=list)
    runs_on: str = "This computer, for now"
    platforms: list[str] = field(default_factory=list)
    source: str = "template"      # "template" or the agent's label


def present(window: Gtk.Window, **options) -> c.Sheet:
    """``inline=True`` runs the steps in the conversation itself (v73), when the window can hold them;
    otherwise they come as a sheet."""
    return NewProject(window, options).sheet


class InlineHost:
    """What NewProject needs from a sheet, standing in the conversation instead (v73 shots 08 and 09): the
    rail's New project row says how far along it is, and the Stage stays beside it."""

    def __init__(self, window: Gtk.Window) -> None:
        self.window = window
        self.closed = False
        self.card = window.conversation
        scroller = window.conversation.scroller
        self.stack = type("Pages", (), {"get_visible_child": staticmethod(lambda: scroller)})()

    def set_body(self, child: Gtk.Widget, **_kw) -> None:
        holder = c.vbox(child, css="pgs-new-inline")
        c.follow_dark(holder)
        self.window.conversation.show_flow(holder)

    def present(self) -> "InlineHost":
        return self

    def close(self) -> None:
        self.closed = True

    def say(self, words: str) -> None:
        setter = getattr(getattr(self.window, "rail", None), "set_draft", None)
        if callable(setter):
            setter(words)


# ── Who answers: Ari when it can, the templates when it can't ───────────────


def proposer() -> tuple[Any, str]:
    """(connection, words) for the agent that can answer here and now, else (None, why not)."""
    connections = c.module("agents.connections")
    ari = c.module("agents.ari")
    if connections is None or ari is None:
        return None, "Ari isn’t installed in this build"
    listed = connections.ConnectionStore().list()
    for conn in listed:
        if conn.kind in ("ari", "local") and conn.endpoint in ("ollama", "openai-compatible", ""):
            if conn.model or conn.endpoint == "openai-compatible":
                return conn, conn.label
    # No model of Ari's own: the agent the conversations use (Codex on the person's plan, Claude, a key) can
    # answer the same few questions (agents.quick).
    for conn in listed:
        if conn.kind in ("personal-plan", "api-key", "team-key"):
            return conn, conn.label
    return None, "No agent is connected yet"


def ask_model(conn, system: str, prompt: str) -> dict:
    """One short, tool-free question; the answer must be a JSON object."""
    if conn.kind in ("ari", "local"):
        ari = c.module("agents.ari")
        endpoint = ari.endpoint_for(conn)
        reply = ari.ChatStream(endpoint).run([{"role": "system", "content": system},
                                              {"role": "user", "content": prompt}], [], lambda _t: None)
        content = reply.content
    else:
        quick = c.module("agents.quick")
        content = quick.ask_text(conn, f"{system} Don't use any tools or files: just answer.\n\n{prompt}")
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError("Ari’s answer wasn’t in the expected shape.")
    return json.loads(match.group())


SYSTEM = ("You help someone start a software project in Playground. They may not be technical. Write plain, warm "
          "English: short sentences, no jargon, no em dashes. Answer with one JSON object and nothing else.")


def template_for(idea: str) -> dict:
    """The starting point written for one of the examples, when that example was picked; the general one for
    anything a person wrote themselves (a 2048 game isn't the little brother's bubble game)."""
    said = " ".join(idea.lower().split()).rstrip(".")
    for key in ("plants", "game", "classes"):
        if said == TEMPLATES[key]["example"].lower():
            return TEMPLATES[key]
    return TEMPLATES["general"]


def questions_for(idea: str, conn) -> tuple[list[tuple[str, list[str]]], str, str]:
    """(questions, suggested name, source words)."""
    if conn is not None:
        data = ask_model(conn, SYSTEM, (
            f"They want to make: {idea!r}. Ask exactly two questions, each with two or three short tap-to-answer "
            "options: who will use it (always offer \"Just me\" and \"Me and others\", or the idea's own words for "
            "them, like ages for a kids' game), and where it will be used (phones, computers, or both). Never ask "
            "about saving data, sign-in or technical choices: the agent works those out. "
            'Also suggest a short, friendly product name. Shape: {"name": "...", "questions": [{"question": "...", '
            '"options": ["...", "...", "..."]}]}'))
        questions = [(str(q.get("question", "")), [str(o) for o in (q.get("options") or [])][:4])
                     for q in (data.get("questions") or [])[:2] if q.get("question") and q.get("options")]
        if questions:
            return questions, str(data.get("name") or ""), conn.label
    template = template_for(idea)
    return list(template["questions"]), template["name"] or _name_from(idea), "template"


# What gets built, unless the person says otherwise: an app is a Luma app; a website is a website.
RECIPE_DEFAULT = (
    "Unless they asked for a website or a page people open in a browser, it's an app: recommend a native Luma "
    "app built with LumaUI (Luma's own parts on GTK 4), one app that adapts to computers, phones and TVs, "
    "packaged as a Flatpak, following Luma's current guides (start at https://simplyluma.com/llms.txt; LumaUI, "
    "its action bar and phone/tablet/TV: https://simplyluma.com/developer/docs/kit). Name LumaUI in the recipe's "
    "parts. A website "
    "is a SvelteKit web app. Either way, save data in SQLite inside the project when it needs saving, and run it "
    "on this computer for now.")
WEBSITE = re.compile(r"\b(web ?sites?|web ?apps?|web ?pages?|sites?|pages?|landing|browsers?|online|blog|portfolio)\b",
                     re.I)


def is_website(idea: str) -> bool:
    return bool(WEBSITE.search(idea))


def propose(idea: str, answers: list[tuple[str, str]], name: str, conn) -> Proposal:
    if conn is not None:
        asked = "; ".join(f"{q} {a}" for q, a in answers)
        data = ask_model(conn, SYSTEM, (
            f"They want to make: {idea!r}. Their answers: {asked}. Suggested name: {name!r}. Propose a one-page "
            "brief and a recipe (how it's built, in plain words). " + RECIPE_DEFAULT + " Shape: {\"name\": \"...\", \"title\": \"a short tagline\", "
            "\"what\": \"what it is\", \"who\": \"who it's for\", \"where\": \"where it's used\", "
            "\"good\": \"what good looks like\", \"platforms\": [\"phones\"], \"recipe\": {\"summary\": \"...\", "
            "\"parts\": [{\"label\": \"Pages\", \"choice\": \"SvelteKit\", \"why\": \"...\"}], \"runs_on\": \"...\"}}"))
        recipe = data.get("recipe") or {}
        parts = [(str(p.get("label", "")), str(p.get("choice", "")), str(p.get("why", "")))
                 for p in recipe.get("parts") or [] if isinstance(p, dict)]
        return Proposal(name=str(data.get("name") or name), title=str(data.get("title") or ""),
                        what=str(data.get("what") or ""), who=str(data.get("who") or ""),
                        where=str(data.get("where") or ""), good=str(data.get("good") or ""),
                        recipe_summary=str(recipe.get("summary") or ""), parts=parts,
                        runs_on=str(recipe.get("runs_on") or "This computer, for now"),
                        platforms=[str(p) for p in data.get("platforms") or []], source=conn.label)
    return template_proposal(idea, answers, name)


def template_proposal(idea: str, answers: list[tuple[str, str]], name: str) -> Proposal:
    template = template_for(idea)
    said = [a.lower() for _q, a in answers]
    who = answers[0][1] if answers else ""
    where_answer = answers[1][1].lower() if len(answers) > 1 else "both"
    phones = "phone" in where_answer or "tablet" in where_answer
    computers = "computer" in where_answer
    website = is_website(idea)
    if not website:
        where = {(True, False): "On Luma phones.", (False, True): "On Luma computers."}.get(
            (phones, computers), "On Luma computers and phones: one app that fits the screen it's on.")
        platforms = ["phones"] * phones + ["computers"] * computers or ["phones", "computers"]
    elif phones and not computers:
        where, platforms = "On a phone. It installs like an app.", ["phones"]
    elif computers and not phones:
        where, platforms = "On a computer, in the browser.", ["computers"]
    else:
        where, platforms = "Phones and computers. On a phone it installs like an app.", ["phones", "computers"]
    remember = template is not TEMPLATES["general"]        # the agent decides for anything else
    sign_in = any("others" in a or "family" in a or "anyone" in a for a in said[:1])
    pay = False
    if website:
        parts = [("Pages", "SvelteKit", "Small and quick, and easy for any agent to work on.")]
    else:
        parts = [("App", "LumaUI", "Luma’s own parts: one app that fits computers, phones and TVs, and looks "
                                   "like the rest of Luma."),
                 ("Package", "A Flatpak", "How Luma installs apps, and how Depot will carry it.")]
    if remember:
        parts.append(("Saving", "A small database file (SQLite)",
                      "It lives inside the project folder, so everything stays on this computer."))
    if sign_in:
        parts.append(("Sign-in", "Luma Connect", "One tap, no passwords to remember."))
    if pay:
        parts.append(("Payments", "Added later, in its own conversation", "Taking money gets its own careful conversation, after the first "
                                                            "version works."))
    parts.append(("Runs on", "This computer, for now", "Move it to a server when you want it away from home.")
                 if website else ("Runs on", "This computer, in Preview",
                                  "Try it at phone, tablet and desktop sizes as it’s built."))
    what = template["what"] or f"{idea.strip().rstrip('.')}."
    tagline = template["title"] or ""
    return Proposal(name=name or template["name"] or _name_from(idea), title=tagline[:1].upper() + tagline[1:],
                    what=what[0].upper() + what[1:],
                    who=template["who"].format(a=who.lower(), A=who) if template.get("who") and who else _who_words(who),
                    where=where, good=template["good"],
                    recipe_summary=("A web app that works great on phones" if phones else "A web app") if website
                    else "A Luma app",
                    parts=parts, platforms=platforms, source="template")


def _who_words(answer: str) -> str:
    words = answer.strip().rstrip(".")
    if not words:
        return "You."
    return {"just me": "You.", "me and others": "You, and the people you share it with."}.get(words.lower(),
                                                                                              f"{words}.")


def _name_from(idea: str) -> str:
    """"A tool to track my plant watering" → "Plant Watering": the last few words that carry meaning."""
    stop = {"a", "an", "the", "to", "for", "my", "our", "of", "and", "that", "which", "with", "on", "in", "can",
            "app", "tool", "page", "site", "website", "little", "simple", "some", "i", "we", "make", "build", "want",
            "luma"}
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]*", idea) if w.lower() not in stop]
    return " ".join(w.capitalize() for w in words[-2:]) or "My project"


# ── The sheet ────────────────────────────────────────────────────────────────


class NewProject:
    def __init__(self, window: Gtk.Window, options: dict) -> None:
        self.window = window
        self.idea = str(options.get("text") or "")
        self.conn = None
        self.source = ""
        self.questions: list[tuple[str, list[str]]] = []
        self.answers: list[tuple[str, str]] = []
        self.name = ""
        self.proposal: Proposal | None = None
        self.created: Any = None
        self.goal_id: str | None = None
        self.busy: str | None = None
        self.error: str | None = None
        self.show_agents = False
        inline = options.get("inline") and hasattr(getattr(window, "conversation", None), "show_flow")
        self.sheet = InlineHost(window) if inline else c.Sheet(window, "New project", "", size="wide",
                                                                  name="newproject")
        self.body = c.vbox(spacing=12, css="pgs-new")
        self.sheet.set_body(self.body)
        self.sheet.present()
        self.render()
        if self.idea and options.get("start", True):
            GLib.idle_add(lambda: (self.start(self.idea), False)[1])

    # ── steps ──

    def progress(self) -> str:
        if self.created is not None:
            return f"{self.created.name} is ready"
        if self.proposal is not None:
            return "The brief and the recipe"
        if self.busy:
            return self.busy.rstrip("…")
        if self.questions:
            return "A few quick questions"
        return "Describe what you want to make"

    def render(self) -> None:
        if isinstance(self.sheet, InlineHost):
            self.sheet.say(self.progress())
        c.clear(self.body)
        if not self.questions and self.busy is None and self.created is None:
            self.body.append(self.hero())
            return
        self.body.append(self.idea_card())
        for index, (question, options) in enumerate(self.questions):
            if index > len(self.answers):
                break
            self.body.append(self.question_card(index, question, options))
        if self.proposal is not None:
            self.body.append(self.brief_card())
            self.body.append(self.recipe_card())
        if self.created is not None:
            self.body.append(self.reveal_card())
        if self.busy:
            self.body.append(c.hbox(c.shared.Spinner(14), text(self.busy, "pgs-row-sub"), spacing=10,
                                    css="pgs-step-card"))
        if self.error:
            self.body.append(callout(c.escape(self.error), icon="triangle-alert", tone="warn"))
        GLib.timeout_add(150, self.scroll_to_end)

    def scroll_to_end(self) -> bool:
        page = self.sheet.stack.get_visible_child()
        if isinstance(page, Gtk.ScrolledWindow):
            adjustment = page.get_vadjustment()
            adjustment.set_value(adjustment.get_upper())
        return False

    def hero(self) -> Gtk.Widget:
        box = c.vbox(spacing=0)
        from ... import APP_ID
        icon = Gtk.Image(icon_name=APP_ID, pixel_size=64, halign=Gtk.Align.START)
        if not c._has_icon(APP_ID):
            from ...core.paths import app_data
            path = app_data(f"{APP_ID}.svg")
            if path.is_file():
                icon.set_from_file(str(path))
        icon.add_css_class("pgs-new-icon")
        box.append(icon)
        box.append(text("What do you want to make?", "pgs-new-title", accessible_role=Gtk.AccessibleRole.HEADING))
        lede = text("Say it the way you’d tell a friend. Ari asks a couple of questions, makes a real folder for it, "
                    "and gets a first version on screen.", "pgs-new-lede")
        lede.set_margin_top(6)
        box.append(lede)
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, top_margin=6, bottom_margin=6,
                            hexpand=True)
        view.update_property([Gtk.AccessibleProperty.LABEL], ["What do you want to make?"])
        view.set_size_request(-1, 72)
        view.get_buffer().set_text(self.idea)
        self.idea_view = view
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.idea_key)
        view.add_controller(keys)
        placeholder = text("A little app that…", "pgs-big-placeholder", can_target=False)
        overlay = Gtk.Overlay()
        overlay.set_child(view)
        overlay.add_overlay(placeholder)
        placeholder.set_valign(Gtk.Align.START)
        placeholder.set_margin_top(6)
        placeholder.set_visible(not self.idea)
        view.get_buffer().connect("changed", lambda b: placeholder.set_visible(b.get_char_count() == 0))
        mic = c.icon_button("mic", "Say it instead", on_click=self.dictate)
        start = c.button("Start", icon="sparkles", style="key", on_click=lambda: self.start(_buffer(view)))
        big = c.vbox(overlay, c.hbox(mic, text("Press Return to start", "pgs-big-hint", wrap=False, hexpand=True,
                                               ellipsize=True), start, spacing=8), spacing=6, css="pgs-big")
        big.set_margin_top(20)
        box.append(big)
        examples = c.vbox(spacing=8)
        examples.set_margin_top(18)
        examples.append(text("Or start from one of these", "pgs-section-title"))
        for icon_name, words in EXAMPLES:
            button = Gtk.Button()
            button.add_css_class("pgs-example")
            button.set_child(c.hbox(c.glyph(icon_name, 17), text(words, wrap=False, ellipsize=True, hexpand=True),
                                    c.glyph("arrow-right", 16), spacing=12))
            button.update_property([Gtk.AccessibleProperty.LABEL], [words])
            button.connect("clicked", lambda _b, w=words: self.start(w))
            examples.append(button)
        box.append(examples)
        GLib.idle_add(lambda: (view.grab_focus(), False)[1])
        return box

    def idea_key(self, _controller, keyval: int, _code: int, state) -> bool:
        from gi.repository import Gdk
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not state & Gdk.ModifierType.SHIFT_MASK:
            self.start(_buffer(self.idea_view))
            return True
        return False

    def dictate(self) -> None:
        """Voice: Luma's dictation when it's on this computer; otherwise say so instead of pretending."""
        for program in ("luma-dictate", "nerd-dictation"):
            path = shutil.which(program)
            if path:
                try:
                    Gio.Subprocess.new([path, "begin"], Gio.SubprocessFlags.NONE)
                    c.toast(self.sheet.card, "Listening. Speak, and the words appear here.", kind="opening")
                except GLib.Error:
                    break
                self.idea_view.grab_focus()
                return
        c.toast(self.sheet.card, "Voice needs dictation, which isn’t set up on this computer. Type it for now.",
                kind="warning")

    def start(self, idea: str) -> None:
        idea = " ".join(idea.split())
        if not idea:
            return
        self.idea = idea
        self.questions, self.answers, self.proposal, self.error = [], [], None, None
        self.busy = "Thinking about what to ask…"
        self.render()

        def work():
            conn, why = proposer()
            try:
                questions, name, source = questions_for(idea, conn)
            except Exception as error:  # noqa: BLE001 - the model failed: say so, use the templates
                c.log.warning("Ari couldn't ask: %s", error)
                why = f"{conn.label} couldn’t answer ({error})" if conn is not None else str(error)
                conn = None
                questions, name, source = questions_for(idea, None)
            return conn, why, questions, name, source

        def done(result) -> None:
            self.conn, why, self.questions, self.name, self.source = result
            self.why = why
            self.busy = None
            self.render()

        c.run_async(work, done, self.failed)

    def failed(self, error) -> None:
        self.busy = None
        self.error = str(error)
        self.render()

    def idea_card(self) -> Gtk.Widget:
        card = c.vbox(spacing=6, css="pgs-step-card")
        head = c.hbox(c.glyph("message-circle", 14), text("You want to make", "pgs-kind", wrap=False, hexpand=True),
                      c.button("Change", style="plain", small=True, on_click=self.restart), spacing=6)
        card.append(head)
        card.append(text(self.idea, "pgs-step-title"))
        if self.questions:
            why = getattr(self, "why", "") or "No agent is connected yet"
            why = why if why.endswith((".", "?", "!")) else why + "."
            honest = (f"{self.source} is asking." if self.source != "template" else
                      f"{why} These questions come from Playground’s own starting points. Connect an agent for "
                      "a proposal made for your idea.")
            card.append(text(honest, "pgs-honest"))
        return card

    def restart(self) -> None:
        self.questions, self.answers, self.proposal, self.created = [], [], None, None
        self.render()

    def question_card(self, index: int, question: str, options: list[str]) -> Gtk.Widget:
        card = c.vbox(spacing=8, css="pgs-step-card")
        card.append(c.hbox(c.glyph("message-circle", 14), text(f"Question {index + 1} of {len(self.questions)}",
                                                                "pgs-kind", wrap=False), spacing=6))
        card.append(text(question, "pgs-step-title"))
        chips = c.Adw.WrapBox(child_spacing=8, line_spacing=8)
        answered = self.answers[index][1] if index < len(self.answers) else None
        for option in options:
            chip = Gtk.ToggleButton(active=option == answered, sensitive=answered is None or option == answered)
            chip.add_css_class("pgs-chip")
            chip.set_child(c.hbox(c.glyph("check", 14) if option == answered else None, Gtk.Label(label=option),
                                  spacing=6))
            chip.update_property([Gtk.AccessibleProperty.LABEL], [option])
            if answered is None:
                chip.connect("clicked", lambda _b, o=option: self.answer(index, question, o))
            chips.append(chip)
        card.append(chips)
        if answered is None and index == len(self.answers):
            GLib.idle_add(lambda: (c._first_focusable(chips) or chips).grab_focus() and False)
        return card

    def answer(self, index: int, question: str, option: str) -> None:
        if index != len(self.answers):
            return
        self.answers.append((question, option))
        if len(self.answers) < len(self.questions):
            self.render()
            return
        self.busy = "Writing the brief and the recipe…"
        self.render()
        conn, idea, answers, name = self.conn, self.idea, list(self.answers), self.name

        def work() -> Proposal:
            try:
                return propose(idea, answers, name, conn)
            except Exception as error:  # noqa: BLE001
                c.log.warning("Ari couldn't propose: %s", error)
                proposal = template_proposal(idea, answers, name)
                proposal.source = f"template, because {conn.label} couldn’t answer ({error})"
                return proposal

        def done(proposal: Proposal) -> None:
            self.proposal = proposal
            self.busy = None
            self.render()

        c.run_async(work, done, self.failed)

    def brief_card(self) -> Gtk.Widget:
        proposal = self.proposal
        card = c.vbox(spacing=8, css="pgs-step-card")
        card.append(c.hbox(c.glyph("file-text", 14), text("Brief", "pgs-kind", wrap=False, hexpand=True),
                           text("One page · edit anything", "pgs-kind-aside", wrap=False), spacing=6))
        name = Gtk.Entry(text=proposal.name, hexpand=True)
        name.add_css_class("pgs-name-entry")
        name.update_property([Gtk.AccessibleProperty.LABEL], ["Project name"])
        name.connect("changed", lambda e: setattr(proposal, "name", e.get_text()))
        tagline = Gtk.Entry(text=proposal.title, hexpand=True, placeholder_text="A short line about it")
        tagline.add_css_class("pgs-entry-inline")
        tagline.update_property([Gtk.AccessibleProperty.LABEL], ["Tagline"])
        tagline.connect("changed", lambda e: setattr(proposal, "title", e.get_text()))
        card.append(c.hbox(name, spacing=8))
        card.append(tagline)
        grid = Gtk.Grid(column_spacing=12, row_spacing=0)
        for row, (label, attr) in enumerate((("What it is", "what"), ("Who it’s for", "who"), ("Where", "where"),
                                             ("Good looks like", "good"))):
            key = text(label, "pgs-kv-key")
            key.set_size_request(112, -1)
            key.set_valign(Gtk.Align.START)
            value = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, hexpand=True,
                                 top_margin=9, bottom_margin=9)
            value.get_buffer().set_text(getattr(proposal, attr))
            value.add_css_class("pgs-inline-text")
            value.update_property([Gtk.AccessibleProperty.LABEL], [label])
            value.get_buffer().connect("changed", lambda b, a=attr: setattr(
                proposal, a, b.get_text(b.get_start_iter(), b.get_end_iter(), False)))
            if row:
                key.add_css_class("ruled")
                value.add_css_class("ruled")
            grid.attach(key, 0, row, 1, 1)
            grid.attach(value, 1, row, 1, 1)
        card.append(grid)
        return card

    def recipe_card(self) -> Gtk.Widget:
        proposal = self.proposal
        card = c.vbox(spacing=8, css="pgs-step-card")
        card.append(c.hbox(c.glyph("layers", 14), text("Recipe", "pgs-kind", wrap=False, hexpand=True),
                           text("How it’s built, in plain words", "pgs-kind-aside", wrap=False), spacing=6))
        card.append(text(proposal.recipe_summary, "pgs-step-title"))
        rows = c.vbox()
        glyphs = {"pages": "layout-grid", "saving": "database", "data": "database", "sign-in": "key-round",
                  "runs on": "monitor", "payments": "wallet", "game": "gamepad-2"}
        for label, choice, why in proposal.parts:
            row = c.Row(label, why or None, lead=glyphs.get(label.lower(), "layers"))
            row.title.set_markup(f"{c.escape(label)} <span alpha='60%'>· {c.escape(choice)}</span>")
            rows.append(row)
        card.append(rows)
        if proposal.source.startswith("template"):
            card.append(text("Playground proposed this from its own starting points, not from an agent. Ari can "
                             "change any of it once the project is open.", "pgs-honest"))
        else:
            card.append(text(f"Proposed by {proposal.source}.", "pgs-honest"))
        if self.created is None:
            create = c.button(f"Looks good, create {proposal.name or 'it'}", icon="sparkles", style="key",
                              on_click=self.create, sensitive=self.busy is None)
            card.append(c.hbox(create, spacing=8))
        return card

    def create(self) -> None:
        proposal = self.proposal
        if proposal is None or not proposal.name.strip():
            self.error = "Give it a name first."
            self.render()
            return
        model, project_mod = c.module("core.model"), c.module("core.project")
        brief = model.Brief(title=proposal.name.strip(), audience=proposal.who, platforms=proposal.platforms)
        brief.set_section(model.BRIEF_SUMMARY, proposal.what)
        if proposal.good:
            brief.set_section(model.BRIEF_GOOD, proposal.good)
        recipe = model.Recipe(summary=proposal.recipe_summary, runs_on=proposal.runs_on,
                              parts=[model.RecipePart(label, choice, why) for label, choice, why in proposal.parts])
        summary = proposal.title or proposal.what
        connection = self.conn.id if self.conn is not None else ""
        idea, answers = self.idea, list(self.answers)
        self.busy = f"Making {proposal.name.strip()}…"
        self.error = None
        self.render()

        def work():
            project = project_mod.create_project(proposal.name.strip(), brief, recipe, summary=summary)
            store = c.module("core.store").ProjectStore(project.path)
            # What good looks like becomes the first stories; the agent writes their checks with the plan.
            stories = [model.Story(title=line.strip().rstrip("."))
                       for line in re.split(r"(?<=\.)\s+", proposal.good) if line.strip()]
            # One conversation: the beginning (the idea, the answers, the brief and recipe) and the first version's
            # work are the same conversation, named from the idea.
            goal = store.create_goal(model.conversation_title(idea) if idea.strip() else f"Start {proposal.name.strip()}",
                                     outcome=f"{proposal.what} The first version is running in Preview, with proof.",
                                     stories=stories, connection=connection,
                                     owner=project.owner.id if project.owner else "")
            store.log(f"Started {goal.title}", kind="goal", goal=goal.id)
            _record_beginning(store, model, project, idea, answers, proposal, connection, goal.id)
            return project, goal.id

        def done(result) -> None:
            self.created, self.goal_id = result
            self.busy = None
            self.render()

        c.run_async(work, done, self.failed)

    def reveal_card(self) -> Gtk.Widget:
        project = self.created
        path = c.home_relative(project.path)
        card = c.vbox(spacing=8, css="pgs-step-card")
        card.append(c.hbox(c.glyph("folder-open", 14), text("Made", "pgs-kind", "good", wrap=False, hexpand=True),
                           text("Just now", "pgs-kind-aside", wrap=False), spacing=6))
        title = text(f"{c.escape(project.name)} lives in <tt>{c.escape(path)}</tt>", "pgs-step-title", markup=True)
        card.append(title)
        card.append(text(f"It’s a real folder on this computer. Everything {project.name} needs stays inside it, and "
                         "every agent works only in here.", "pgs-row-sub"))
        tree = c.vbox(spacing=1, css="pgs-tree")
        tree.append(tree_row("folder-open", f"{project.name}/", "", indent=False))
        for icon, name, what in (("folder", "src/", "The app itself, once Ari makes it"),
                                 ("file-text", "AGENTS.md", "The rules every agent follows here"),
                                 ("list-checks", ".playground/goals/", "The plan, one file per goal"),
                                 ("history", ".playground/activity.jsonl", "The history, written as it happens")):
            tree.append(tree_row(icon, name, what))
        card.append(tree)
        if self.show_agents:
            try:
                agents = (Path(project.path) / "AGENTS.md").read_text(encoding="utf-8")
            except OSError as error:
                agents = f"AGENTS.md can’t be read: {error}"
            card.append(text(agents, "pgs-agentsmd", selectable=True))
        actions = c.hbox(spacing=8)
        actions.append(c.button(f"Open {project.name}", icon="arrow-right", style="key", on_click=self.open_it))
        actions.append(c.button("Hide AGENTS.md" if self.show_agents else "Read AGENTS.md",
                                icon="chevron-up" if self.show_agents else "eye", style="fill", small=True,
                                on_click=lambda: (setattr(self, "show_agents", not self.show_agents), self.render())))
        actions.append(c.button("Show in Filer", icon="folder", style="fill", small=True,
                                on_click=lambda: Gtk.FileLauncher.new(Gio.File.new_for_path(str(project.path)))
                                .launch(self.window, None, None)))
        card.append(actions)
        card.append(text("Its conversation picks up from here: the agent starts on the first version when it opens.",
                         "pgs-honest"))
        return card

    def open_it(self) -> None:
        project, goal_id = self.created, self.goal_id
        self.sheet.close()
        opener = getattr(self.window, "open_project", None)
        if callable(opener):
            opener(str(project.path))
        for name in ("start_goal", "select_goal"):
            handler = getattr(self.window, name, None)
            if callable(handler) and goal_id:
                handler(goal_id)
                break


def _record_beginning(store, model, project, idea: str, answers: list[tuple[str, str]], proposal: Proposal,
                      connection: str, goal_id: str) -> None:
    """The first conversation starts where the project did: what you asked for, your answers, and the
    Brief and Recipe as cards, so the beginning is in the project folder like everything else."""
    you = project.owner.id if project.owner else ""
    author = connection or "playground"
    chat = goal_id
    try:
        store.append_thread(chat, model.ThreadEntry(kind="message", role="person", by=you, text=idea))
        if answers:
            said = "\n".join(f"{question} {answer}." for question, answer in answers)
            store.append_thread(chat, model.ThreadEntry(kind="message", role="person", by=you, text=said))
        good = [line.strip().rstrip(".") for line in re.split(r"(?<=\.)\s+", proposal.good) if line.strip()]
        store.append_thread(chat, model.ThreadEntry(kind="brief", role="agent", by=author, data={
            "title": proposal.name.strip(), "audience": proposal.who, "platforms": list(proposal.platforms),
            "summary": proposal.what, "good_looks_like": good}))
        store.append_thread(chat, model.ThreadEntry(kind="recipe", role="agent", by=author, data={
            "summary": proposal.recipe_summary, "runs_on": proposal.runs_on,
            "parts": [{"label": label, "choice": choice, "why": why} for label, choice, why in proposal.parts]}))
    except Exception:  # noqa: BLE001 - the project exists either way; the chat is a courtesy
        c.log.warning("couldn't write the beginning into the chat", exc_info=True)


def tree_row(icon: str, name: str, what: str, indent: bool = True) -> Gtk.Widget:
    row = c.hbox(c.glyph(icon, 16), text(name, "pgs-tree-name", wrap=False), text(what, "pgs-tree-what", wrap=False,
                                                                                    hexpand=True, xalign=0),
                 spacing=10, css="pgs-tree-row" + (" in" if indent else ""))
    return row


def _buffer(view: Gtk.TextView) -> str:
    buffer = view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
