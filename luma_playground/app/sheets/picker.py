# SPDX-License-Identifier: Apache-2.0
"""The picker: who works on this goal (v73 shot 05).

Yours first, then your teammates'. Every connection says how much it has left,
where it runs and who can use it. The rules are binding and stated in one line:

- a personal plan (Claude Pro/Max, ChatGPT Plus/Pro) is "Just you". It never
  says "Shared with the team"; it can only *work on team tasks*, with a cap;
- API keys, local models and Ari credits can be shared with the team, with caps;
- "Where it runs" is This computer or the Dev server;
- a teammate's personal plan offers **Give to Priya**, never "use": the goal
  becomes Priya's, run by her agent on her account and her computer.

Choosing one of yours switches the goal's agent through the runner, which keeps
the thread and writes a Handoff card. "Add a connection" finds the agent
programs already here, starts an agent's own sign-in (Playground never sees a
token), saves an API key in the OS keyring, or adds a local model from Ollama.

On a computer the picker hangs above the composer as a popover (pass
``anchor``); on a phone, or without an anchor, it is a sheet.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Callable

from gi.repository import Gdk, GLib, GObject, Gtk

from . import common as c
from .common import Row, Tag, callout, get, section, text

RULE = ("Personal plans like Claude Max or ChatGPT Plus are for one person, so they’re never shared. "
        "Teammates use the team key, a local model or Ari, or hand the work to the plan’s owner.")

# Each agent program's own sign-in. Playground starts it in a terminal and never sees the token.
LOGIN = {
    "claude": (["claude", "auth", "login"], "Claude"),
    "codex": (["codex", "login"], "Codex"),
    "gemini": (["gemini"], "Gemini"),
    "opencode": (["opencode", "auth", "login"], "OpenCode"),
    "goose": (["goose", "configure"], "goose"),
}
KEY_PROVIDERS = [("claude", "Anthropic"), ("codex", "OpenAI"), ("gemini", "Google")]


def present(window: Gtk.Window, **options):
    """``anchor`` (a widget) shows it as a popover above that widget on a computer.

    ``page="add"`` opens straight at Add a connection; ``goal`` names the goal.
    """
    anchor = options.pop("anchor", None)
    if anchor is not None and options.get("page") is None and not c.is_phone(window):
        return PickerPopover(window, anchor, options)
    return PickerSheet(window, options).sheet


# ── Reading connections ──────────────────────────────────────────────────────


def load_mine() -> list[Any]:
    connections = c.module("agents.connections")
    if connections is None:
        return []
    return connections.ConnectionStore().list()


def headroom(connection: Any, project_dir=None, *, short: bool = False) -> dict:
    """{"text", "fraction"} from core.usage: "62% of the week left", "Plenty left today",
    "$8.20 of credits left this month", "$6.40 of $25 this month". ``short`` leaves out when it resets
    (the picker's one line). It never guesses a number it wasn't given."""
    usage = c.module("core.usage")
    connection_id = get(connection, "id")
    if usage is None or not connection_id:
        return {}
    try:
        return headroom_words(usage, connection, short=short)
    except Exception:  # noqa: BLE001 - a usage reading never stops the picker
        c.log.debug("headroom failed", exc_info=True)
        return {}


def is_local(connection: Any) -> bool:
    return get(connection, "kind") == "local" or get(connection, "provider") == "ollama" or (
        get(connection, "kind") == "ari" and get(connection, "endpoint") == "ollama")


_CAP = re.compile(r"^\s*\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:a|per|/|each)?\s*(month|week)", re.IGNORECASE)


def spend_cap(connection: Any) -> tuple[float, str] | None:
    """"$25 a month" -> (25.0, "month"). Anything else -> None."""
    match = _CAP.match(str(get(connection, "spend_cap") or ""))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "")), match.group(2).lower()
    except ValueError:
        return None


def _dollars(value: float) -> str:
    return f"${value:,.0f}" if abs(value - round(value)) < 0.005 else f"${value:,.2f}"


def headroom_words(usage, connection: Any, *, short: bool = False, now=None) -> dict:
    from datetime import datetime, timedelta, timezone
    connection_id = get(connection, "id")
    if is_local(connection):
        return {"text": "Free, and nothing leaves this computer", "fraction": None, "free": True}
    head = usage.latest_headroom(connection_id)
    now = now or datetime.now(timezone.utc)
    if head and head.get("percent_left") is not None:
        words = usage.summary(connection_id, now)
        if words == "A new allowance has started; no reading yet.":
            return {"text": words, "fraction": None}
        percent = float(head["percent_left"])
        if head.get("window") == "day" and percent >= 50:
            text = "Plenty left today"
        elif short:
            text = words.split(" · ")[0]
        else:
            text = words
        return {"text": text, "fraction": percent / 100}
    cap = spend_cap(connection)
    if cap is not None:
        amount, period = cap
        local = now.astimezone()
        if period == "month":
            since = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            since = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        spent = sum(float(r.get("cost") or 0) for r in usage.runs(connection_id, since=since.astimezone(timezone.utc)))
        left = max(0.0, amount - spent)
        fraction = left / amount if amount else None
        if get(connection, "kind") == "ari" and get(connection, "endpoint") == "luma-credits":
            text = f"{_dollars(left)} of credits left this {period}"
        else:
            text = f"{_dollars(spent)} of {_dollars(amount)} this {period}"
        return {"text": text, "fraction": fraction}
    words = usage.summary(connection_id, now)
    if words == "Not used this week.":
        return {"text": "", "quiet": words, "fraction": None}
    return {"text": words, "fraction": None}


def teammates(project_dir) -> list[dict]:
    """Teammates who chose to show their plans (core.usage team capacity): numbers, never accounts.
    The plan's name and the team-task share come from the same capacity file."""
    usage = c.module("core.usage")
    if usage is None or project_dir is None:
        return []
    try:
        members = usage.team_capacity(project_dir)
        project = c.module("core.store").ProjectStore(project_dir).read_project()
    except Exception:  # noqa: BLE001
        c.log.debug("team capacity failed", exc_info=True)
        return []
    me = project.owner.id if project.owner else ""
    paths = c.module("core.paths")
    out = []
    for member in members:
        member_id = member.get("member") or ""
        if member_id == me:
            continue
        person = project.member(member_id)
        if person is None:   # someone no longer on the project
            continue
        raw = _raw_capacity(paths, project_dir, member_id)
        for index, row in enumerate(member.get("connections") or []):
            extra = raw[index] if index < len(raw) and isinstance(raw[index], dict) else {}
            cap = extra.get("team_task_percent")
            provider = str(row.get("provider") or "agent")
            # The goal's connection once it is given: "<member>-<provider>", how the app names a teammate's plan.
            out.append({"id": f"{member_id}-{provider}", "member": member_id, "member_name": person.name,
                        "label": row.get("label"),
                        "provider": row.get("provider"), "kind": "personal-plan",
                        "plan": str(extra.get("plan") or "")[:30],
                        "work_on_team_tasks": row.get("team_tasks"),
                        "team_task_cap": int(cap) if isinstance(cap, (int, float)) and 0 < cap <= 100 else None,
                        "pronoun": PRONOUNS.get(str(person.extra.get("pronouns") or "").split("/")[-1].strip().lower(), ""),
                        "percent_left": row.get("percent_left"), "text": row.get("summary")})
    return out


# A member's own `pronouns` ("she/her") picks the word in "up to 20% of her week"; otherwise "their".
PRONOUNS = {"her": "her", "him": "his", "them": "their"}


def _raw_capacity(paths, project_dir, member_id: str) -> list:
    """The capacity file's rows as written, for the plan's name and the team-task share."""
    try:
        folder = paths.ProjectPaths(project_dir).meta / "capacity"
        data = json.loads((folder / f"{paths.require_id(member_id)}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return []
    rows = data.get("connections") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else []


def kind_words(connection: Any) -> str:
    kind, endpoint = get(connection, "kind"), get(connection, "endpoint")
    if kind == "ari" or get(connection, "provider") == "ari":
        return {"chatgpt": "Your ChatGPT plan", "luma-credits": "Luma credits", "ollama": "On this computer",
                "openai-compatible": "API key"}.get(endpoint, "Luma’s agent")
    plan = (get(connection, "extra") or {}).get("plan") if isinstance(get(connection, "extra"), dict) else ""
    org = {"claude": "Anthropic", "codex": "OpenAI", "gemini": "Google"}.get(get(connection, "provider"), "")
    return {"personal-plan": plan or "Personal plan", "api-key": f"{org} API key".strip(),
            "team-key": f"{org} API key".strip(), "local": "Local model"}.get(kind, "")


def where_words(connection: Any, project=None) -> tuple[str, str]:
    where = get(connection, "where") or "this-computer"
    if where == "this-computer":
        return "monitor", "This computer"
    computers = c.module("agents.computers")
    computer = computers.ComputerStore().get(where) if computers is not None else None
    if computer is not None:
        return "server", computer.name
    place = project.place(where) if project is not None else None
    return "server", place.name if place else "Dev server"


def sharing_words(connection: Any) -> tuple[str, str]:
    """The third fact. A personal plan can only ever say "Just you" (or that it does team tasks)."""
    personal = get(connection, "kind") == "personal-plan" or (
        get(connection, "kind") == "ari" and get(connection, "endpoint") == "chatgpt")
    if personal:
        if get(connection, "work_on_team_tasks"):
            return "lock", f"Just you · team tasks up to {get(connection, 'team_task_cap')}%"
        return "lock", "Just you"
    if get(connection, "sharing") == "team":
        return "users", "Shared with the team"
    return "lock", "Just you"


# ── One row ──────────────────────────────────────────────────────────────────


def connection_button(connection: Any, *, current: bool, project=None, on_pick: Callable[[], None]) -> Gtk.Button:
    """v73 .pgpr: mark, name with its kind, a check when chosen, then the three facts."""
    button = Gtk.Button()
    button.add_css_class("pgs-pick")
    if current:
        button.add_css_class("on")
    room = headroom(connection, short=True)
    name = get(connection, "label") or "Agent"
    head = c.hbox(text(name, "pgs-pick-name", wrap=False), text(kind_words(connection), "pgs-pick-kind", wrap=False),
                  spacing=7, hexpand=True)
    facts = c.Adw.WrapBox(child_spacing=12, line_spacing=0, can_target=False)
    if room.get("text"):
        facts.append(text(room["text"], "pgs-pick-fact", wrap=False))
    icon, words = where_words(connection, project)
    facts.append(c.hbox(c.glyph(icon, 12), text(words, "pgs-pick-fact", wrap=False), spacing=5))
    icon, words = sharing_words(connection)
    facts.append(c.hbox(c.glyph(icon, 12), text(words, "pgs-pick-fact", wrap=False), spacing=5))
    words_box = c.vbox(head, facts, spacing=2, hexpand=True)
    check = c.glyph("check", 16, "pgs-pick-check") if current else None
    button.set_child(c.hbox(c.mark(connection, size=28), words_box, check, spacing=12))
    label = f"{name}, {kind_words(connection)}"
    button.update_property([Gtk.AccessibleProperty.LABEL], [label])
    button.update_property([Gtk.AccessibleProperty.DESCRIPTION],
                           [", ".join(filter(None, [room.get("text"), where_words(connection, project)[1],
                                                    sharing_words(connection)[1]]))])
    if current:
        button.update_state([Gtk.AccessibleState.SELECTED], [GObject.Value(GObject.TYPE_INT, 1)])
    button.connect("clicked", lambda _b: on_pick())
    return button


def teammate_row(item: dict, on_give: Callable[[dict], None]) -> Gtk.Widget:
    """A teammate's connection. Their personal plan offers Give to <name>, never "use"."""
    first = c.first_name(item.get("member_name") or item.get("owner_name") or item.get("owner") or "")
    label = f"{c.possessive(first)} {item.get('label') or (item.get('provider') or 'agent').capitalize()}"
    kind = item.get("kind") or "personal-plan"
    plan = item.get("plan") or "Personal plan"
    head = c.hbox(text(label, "pgs-pick-name", wrap=False), text(plan, "pgs-pick-kind", wrap=False),
                  spacing=7, hexpand=True)
    if item.get("work_on_team_tasks"):
        cap = item.get("team_task_cap")
        line = f"Works on team tasks, up to {cap}% of {_their(item)} week" if cap else "Works on team tasks"
    else:
        # Their headroom doesn't matter here: without opting in, the plan only ever works for them.
        line = f"{first} hasn’t opted in to team tasks"
    end = None
    # Only a plan its owner lets take team tasks can be given work (v73: Sam's has no button).
    if kind == "personal-plan" and first and item.get("work_on_team_tasks"):
        end = c.button(f"Give to {first}", style="fill", small=True, on_click=lambda: on_give(item),
                       tooltip=f"Make this conversation {c.possessive(first)}. It runs on {_their(item)} plan, on "
                               f"{_their(item)} computer.")
        end.add_css_class("pgs-pick-give")
    box = c.hbox(c.mark(item, size=28, teammate=item.get("member_name") or first),
                 c.vbox(head, text(line, "pgs-pick-fact"), spacing=2, hexpand=True), end, spacing=12)
    box.add_css_class("pgs-pick")
    box.add_css_class("mate")
    return box


def _their(item: dict) -> str:
    return item.get("pronoun") or "their"


def _children(widget: Gtk.Widget) -> list[Gtk.Widget]:
    out, child = [], widget.get_first_child()
    while child is not None:
        out.append(child)
        child = child.get_next_sibling()
    return out


# ── Switching and handing over ───────────────────────────────────────────────


def switch(window: Gtk.Window, ctx: c.Context, connection: Any, done: Callable[[str], None]) -> None:
    """Switch the goal's agent. The runner keeps the thread and writes the Handoff card."""
    if ctx.goal_id is None or ctx.project_dir is None:
        done("Open a conversation first.")
        return
    # The window owns the goal's runner; it switches natively when it can.
    for name in ("switch_connection", "switch_agent"):
        handler = getattr(window, name, None)
        if callable(handler):
            handler(ctx.goal_id, connection)
            done("")
            return
    # The window's runner for this goal: switch() checks the picker rules at once (ConnectionRuleError, in plain
    # words), then writes the Handoff card and reconnects on its own worker.
    get_runner = getattr(window, "runner_for", None) or getattr(window, "_runner", None)
    if callable(get_runner):
        try:
            instance = get_runner(ctx.goal_id, create=True)
        except TypeError:
            instance = get_runner(ctx.goal_id)
        if instance is not None:
            try:
                instance.switch(connection)
            except Exception as error:  # noqa: BLE001 - the rule's own words
                done(str(error))
                return
            done("")
            return
    # No runner on this goal yet: record the switch in the project and leave the Handoff card in
    # the thread, so whichever agent runs next starts from the brief.
    store_mod, model, handoff = c.module("core.store"), c.module("core.model"), c.module("agents.handoff")
    store = store_mod.ProjectStore(ctx.project_dir)

    def record() -> None:
        goal = store.read_goal(ctx.goal_id)
        before = goal.connection
        if before == get(connection, "id"):
            return
        mine = {conn.id: conn for conn in load_mine()}
        brief = handoff.build_brief(store, ctx.goal_id, from_connection=mine.get(before) or before or "Ari",
                                    to_connection=connection)
        store.update_goal(ctx.goal_id, lambda g: setattr(g, "connection", get(connection, "id")))
        store.append_thread(ctx.goal_id, model.ThreadEntry(kind="handoff", role="system", data=brief.card_data(),
                                                           text=f"Switched to {get(connection, 'label')}"))

    c.run_async(record, lambda _r: done(""), lambda error: done(str(error)))


def give(ctx: c.Context, item: dict, done: Callable[[str], None]) -> None:
    """Hand the goal to a teammate: it becomes theirs, on their plan and their computer."""
    if ctx.goal_id is None or ctx.project_dir is None:
        done("Open a conversation first.")
        return
    store_mod, model = c.module("core.store"), c.module("core.model")
    store = store_mod.ProjectStore(ctx.project_dir)
    member = item.get("member") or item.get("owner") or ""
    first = c.first_name(item.get("member_name") or member)

    def hand_over() -> None:
        def change(goal) -> None:
            goal.owner = member
            goal.connection = item.get("id") or goal.connection
            goal.status_note = f"Given to {first}"
        store.update_goal(ctx.goal_id, change)
        store.append_thread(ctx.goal_id, model.ThreadEntry(
            kind="message", role="system",
            text=f"This conversation is {c.possessive(first)} now. It runs on {c.possessive(first)} "
                 f"{(item.get('provider') or 'agent').capitalize()}, on {_their(item)} computer. "
                 "The thread and the plan go with it."))
        store.log(f"Gave the conversation to {first}", kind="handoff", goal=ctx.goal_id, data={"to": member})

    c.run_async(hand_over, lambda _r: done(""), lambda error: done(str(error)))


# ── The list itself (popover and sheet share it) ─────────────────────────────


class PickerList:
    def __init__(self, window: Gtk.Window, ctx: c.Context, *, on_done: Callable[[], None],
                 on_add: Callable[[], None], on_usage: Callable[[], None], head: bool = True) -> None:
        self.window, self.ctx = window, ctx
        self.head = head
        self.on_done, self.on_add, self.on_usage = on_done, on_add, on_usage
        self.box = c.vbox(css="pgs-picker")
        self.status = text("", "pgs-field-hint")
        self.status.set_visible(False)
        self.fill(loading=True)
        c.run_async(self.read, self.loaded, lambda error: self.fill(error=str(error)))

    def read(self):
        goal = None
        if self.ctx.project_dir is not None and self.ctx.goal_id:
            store = c.module("core.store")
            try:
                goal = store.ProjectStore(self.ctx.project_dir).read_goal(self.ctx.goal_id)
            except Exception:  # noqa: BLE001 - a missing goal just means nothing is chosen
                goal = None
        mine = load_mine()
        rooms = {conn.id: headroom(conn, short=True) for conn in mine}
        return goal, mine, rooms, teammates(self.ctx.project_dir) if self.ctx.project_dir else []

    def loaded(self, result) -> None:
        goal, mine, rooms, mates = result
        self.fill(goal=goal, mine=mine, mates=mates)

    def fill(self, *, loading: bool = False, error: str = "", goal=None, mine=None, mates=None) -> None:
        c.clear(self.box)
        if self.head:
            self.box.append(c.hbox(text("Who works on this conversation", "pgs-picker-head", wrap=False, hexpand=True),
                                   text("Switching keeps the thread", "pgs-picker-head", "aside", wrap=False)))
        if loading:
            self.box.append(c.hbox(c.shared.Spinner(14), text("Looking at your connections…", "pgs-pick-fact"),
                                   spacing=8, css="pgs-pick"))
            return
        if error:
            self.box.append(callout(c.escape(error), icon="triangle-alert", tone="warn"))
        current = getattr(goal, "connection", "") if goal is not None else ""
        project = self.ctx.project
        if not mine:
            self.box.append(Row("No agents yet", "Add Ari, Claude, Codex or a local model to get started.",
                                lead="sparkles", on_activate=self.on_add))
        for conn in sorted(mine, key=lambda x: (x.kind != "ari", x.kind == "team-key", x.label.lower())):
            self.box.append(connection_button(conn, current=conn.id == current, project=project,
                                              on_pick=lambda conn=conn: self.pick(conn)))
            if conn.id == current and getattr(conn, "is_acp", False):
                self.box.append(fence_row(conn, self.ctx.project_dir))
        if mates:
            self.box.append(text("Your teammates", "pgs-picker-head", wrap=False))
            for item in mates:
                self.box.append(teammate_row(item, self.give))
        rule = callout(c.escape(RULE), icon="lock", tone="rule")
        rule.set_margin_top(8)
        rule.set_margin_start(2)
        rule.set_margin_end(2)
        self.box.append(rule)
        self.box.append(self.status)
        separator = Gtk.Separator()
        separator.set_margin_top(8)
        separator.set_margin_bottom(4)
        self.box.append(separator)
        self.box.append(menu_row("plus", "Add a connection", self.on_add))
        self.box.append(menu_row("gauge", "Usage and sharing", self.on_usage))

    def pick(self, connection) -> None:
        self.say("Switching…")

        def done(problem: str) -> None:
            if problem:
                self.say(problem, error=True)
                return
            c.toast(self.window, f"{get(connection, 'label')} works on this conversation now", kind="done")
            self.on_done()

        connections = c.module("agents.connections")
        person = (self.ctx.project.owner.id if self.ctx.project and self.ctx.project.owner else "")
        try:
            if connections is not None:
                connections.check_may_run(connection, person)
        except Exception as error:  # noqa: BLE001 - ConnectionRuleError: plain words
            done(str(error))
            return
        switch(self.window, self.ctx, connection, done)

    def give(self, item: dict) -> None:
        first = c.first_name(item.get("member_name") or item.get("member") or "")

        def done(problem: str) -> None:
            if problem:
                self.say(problem, error=True)
                return
            c.toast(self.window, f"Gave the conversation to {first}", kind="sent")
            self.on_done()

        give(self.ctx, item, done)

    def say(self, words: str, error: bool = False) -> None:
        self.status.set_label(words)
        self.status.set_visible(bool(words))
        (self.status.add_css_class if error else self.status.remove_css_class)("pgs-form-error")


def fenced(conn: Any, project_dir) -> tuple[bool, bool]:
    """(on, shared): whether this connection's agent program runs fenced in for this project, and why."""
    sandbox = c.module("agents.sandbox")
    shared = False
    if sandbox is not None and project_dir is not None:
        try:
            shared = bool(sandbox.project_is_shared(project_dir))
        except Exception:  # noqa: BLE001
            shared = False
    try:
        return bool(conn.sandbox_for(shared)), shared
    except Exception:  # noqa: BLE001 - an older connection without the setting
        return False, shared


def set_fence(conn: Any, on: bool, done: Callable[[str], None] | None = None) -> None:
    """Save "Keep it inside the project" for one connection ("on" or "off"; never back to the default)."""
    connections = c.module("agents.connections")

    def write():
        store = connections.ConnectionStore()
        current = store.get(conn.id)
        if current is None:
            raise ValueError("That connection isn’t here any more.")
        current.sandbox = "on" if on else "off"
        conn.sandbox = current.sandbox
        return store.update(current)
    c.run_async(write, lambda _saved: done and done(""), lambda error: done and done(str(error)))


def fence_row(conn: Any, project_dir) -> Gtk.Widget:
    """Under the chosen agent: "Keep it inside the project", for agent programs Playground starts (Claude, Codex…).
    On by default when the project is shared; recommended when you work alone."""
    on, shared = fenced(conn, project_dir)
    if shared and conn.sandbox in ("", "on"):
        why = "On for shared projects. It can only change files in this project."
    elif conn.sandbox_recommended:
        why = "Recommended. It can only change files in this project, and can’t read the rest of your home folder."
    else:
        why = "It can only change files in this project."
    toggle = c.switch(on, label="Keep it inside the project", on_toggle=lambda value: set_fence(conn, value))
    row = c.hbox(c.glyph("shield-check", 16), c.vbox(text("Keep it inside the project", "pgs-pick-name", wrap=False),
                                                    text(why, "pgs-pick-fact"), spacing=2, hexpand=True),
                 toggle, spacing=12, css="pgs-fence")
    return row


def sign_in_again(window: Gtk.Window, runner: Any, method: dict, label: str) -> c.Sheet | None:
    """A failure card's Sign in: the agent's own sign-in in a terminal; when it ends well the goal carries on
    (``runner.signed_in()`` picks up the same conversation)."""
    try:
        argv, env = runner.sign_in_command(str(method.get("id") or ""))
    except Exception as error:  # noqa: BLE001 - the agent isn't running, or doesn't sign in by terminal
        c.toast(window, str(error) or "That sign-in can’t start here", kind="warning")
        return None
    sheet = c.Sheet(window, f"Sign in to {label}", f"{label}’s own sign-in. Playground never sees it.",
                    name="signin")

    def ended(ok: bool) -> None:
        sheet.close()
        if ok:
            runner.signed_in()
            c.toast(window, f"Signed in. {label} carries on.", kind="done")
        else:
            c.toast(window, f"{label} isn’t signed in yet. Try again when you’re ready.", kind="warning")
    page = LoginTerminal(list(argv), dict(env or {}), label, on_exit=ended, carry_on=True)
    sheet.set_body(page.box, scroll=False)
    sheet.present()
    return sheet


def chatgpt_row(on_continue: Callable[[], None]) -> Gtk.Widget:
    """Ari on your ChatGPT plan. Behind a flag (PLAYGROUND_SIWC); until it's on, it honestly says Coming soon.
    The button's words are OpenAI's required wording."""
    chatgpt = c.module("agents.chatgpt")
    state = chatgpt.status("ari-chatgpt") if chatgpt is not None else {"state": "coming-soon"}
    if state.get("state") == "coming-soon":
        return Row("Ari on your ChatGPT plan", "Use your ChatGPT plan instead of credits. Coming soon.",
                   lead="sparkles", tags=[Tag("Coming soon", None)])
    if state.get("state") == "signed-in":
        sub = f"{state.get('label') or 'Using ChatGPT plan'}{' · ' + state['email'] if state.get('email') else ''}"
        end = c.button("Manage usage", icon="external-link", style="fill", small=True,
                       on_click=lambda: Gtk.UriLauncher.new(state.get("manage_usage_url") or
                                                            "https://chatgpt.com/settings/usage").launch(None, None, None))
        return Row("Ari on your ChatGPT plan", sub, lead="sparkles", end=end)
    end = c.button(state.get("button") or "Continue with ChatGPT", style="key", small=True, on_click=on_continue)
    return Row("Ari on your ChatGPT plan", state.get("text") or None, lead="sparkles", end=end)


def _key(item) -> str:
    return item.id or item.provider


def agent_state_words(item) -> str:
    """The quiet line under an agent: what's true now, in the person's words."""
    how = {"subscription": "your plan", "ChatGPT": "your ChatGPT plan", "api key": "an API key"}.get(
        item.method, item.method)
    via = (f" · from {item.found_in}" if getattr(item, "found_in", "") and item.found_in != "Playground" else
           f" · through {item.via}" if item.via else "")
    if item.signed_in:
        plan = f" · {item.account.capitalize()}" if item.account else ""
        return f"Signed in{' with ' + how if how else ''}{plan}{via}"
    if item.note:
        return item.note
    if item.ready or item.installed:
        return f"Ready · sign in to use it{via}"
    return "Playground gets it ready for you"


def menu_row(icon: str, label: str, on_click: Callable[[], None]) -> Gtk.Button:
    button = Gtk.Button()
    button.add_css_class("pgs-picker-menu-row")
    button.set_child(c.hbox(c.glyph(icon, 16), text(label, wrap=False), spacing=10))
    button.connect("clicked", lambda _b: on_click())
    return button


# ── Popover (computer) ───────────────────────────────────────────────────────


def _composer_bar(anchor: Gtk.Widget) -> Gtk.Widget | None:
    """The composer the agent chip sits in, so the popover can clear its top edge."""
    widget = anchor.get_parent()
    while widget is not None:
        if widget.has_css_class("pg-composer"):
            return widget
        widget = widget.get_parent()
    return None


class PickerPopover:
    def __init__(self, window: Gtk.Window, anchor: Gtk.Widget, options: dict) -> None:
        self.window = window
        self.ctx = c.context_for(window, options)
        self.popover = Gtk.Popover(has_arrow=False, position=Gtk.PositionType.TOP, autohide=True)
        self.popover.add_css_class("pgs-picker-popover")
        c.follow_dark(self.popover)
        self.listing = PickerList(window, self.ctx, on_done=self.popover.popdown,
                                  on_add=lambda: self.then("add"), on_usage=lambda: self.then("usage"))
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                    max_content_height=600)
        scroll.set_child(self.listing.box)
        scroll.set_size_request(404, -1)
        self.popover.set_child(scroll)
        self.popover.set_parent(anchor)
        # v73: it stands just above the composer, its left edge on the agent chip's, not centred on the chip.
        self.popover.set_halign(Gtk.Align.START)
        bar = _composer_bar(anchor)
        if bar is not None:
            ok, bounds = anchor.compute_bounds(bar)
            if ok:
                rect = Gdk.Rectangle()
                rect.x, rect.y, rect.width, rect.height = 0, int(-bounds.get_y()) - 14, max(1, anchor.get_width()), 1
                self.popover.set_pointing_to(rect)
        self.popover.connect("closed", lambda *_a: GLib.idle_add(self._unparent))
        self.popover.popup()

    def _unparent(self) -> bool:
        if self.popover.get_parent() is not None:
            self.popover.unparent()
        return False

    def then(self, where: str) -> None:
        self.popover.popdown()
        if where == "usage":
            c.open_sheet(self.window, "usage", **self.ctx.options)
        else:
            present(self.window, **dict(self.ctx.options, page="add"))


# ── Sheet (phone, or opened from elsewhere) ──────────────────────────────────


class PickerSheet:
    def __init__(self, window: Gtk.Window, options: dict) -> None:
        self.window = window
        self.ctx = c.context_for(window, options)
        self.sheet = c.Sheet(window, "Who works on this conversation", "Switching keeps the thread", name="picker")
        self.listing = PickerList(window, self.ctx, on_done=self.sheet.close, on_add=self.show_add,
                                  on_usage=lambda: (self.sheet.close(), c.open_sheet(window, "usage")), head=False)
        self.sheet.set_body(self.listing.box)
        self.sheet.present()
        if options.get("page") == "add":
            self.show_add()

    # Add a connection

    def show_add(self) -> None:
        page = AddConnection(self)
        self.sheet.push(page.box, "Add a connection", "Agents and models you can use here")


class AddConnection:
    """Detected programs, an agent's own sign-in, an API key, or a local model."""

    def __init__(self, picker: PickerSheet) -> None:
        self.picker = picker
        self.sheet = picker.sheet
        self.box = c.vbox()
        self.detected: list[Any] | None = None
        self.key_provider = "claude"
        self.key_share = False
        self.rows: dict[str, Row] = {}
        self.buttons: dict[str, Gtk.Widget] = {}
        self.busy: dict[str, str] = {}     # provider -> what's happening now, in words
        self.finished: set[int] = set()    # sign-ins already done, so a late terminal exit doesn't add twice
        self.build()
        connections = c.module("agents.connections")
        if connections is not None:
            c.run_async(lambda: connections.detect(), self.found,
                        lambda error: self.found([]))

    def found(self, detected) -> None:
        self.detected = detected
        if not self.sheet.closed:
            self.build()
            # Opened to set one up (the startup splash's "Use your ChatGPT plan"): go straight on with it.
            start = str(self.picker.ctx.options.get("start") or "")
            item = next((d for d in detected or [] if d.provider == start), None)
            if start and item is not None:
                self.picker.ctx.options.pop("start", None)
                self.connect(item)

    def build(self) -> None:
        c.clear(self.box)
        connections = c.module("agents.connections")
        if connections is None:
            self.box.append(c.empty("Connections aren’t installed", "This build of Playground is missing its "
                                    "connections module.", "sparkles"))
            return
        self.box.append(section("Agents", first=True,
                                note="Choose one and Playground gets it ready. Each signs in with its maker’s own "
                                     "sign-in, so Playground never sees a password or token."))
        rows = []
        self.rows, self.buttons = {}, {}
        if self.detected is None:
            rows.append(Row("Looking for agents…", None, lead=_spin_tile()))
        else:
            mine = {conn.account_key() for conn in load_mine() if conn.kind == "personal-plan"}
            for item in self.detected:
                if item.provider in ("node", "ollama"):   # Ollama has its own section below
                    continue
                if not (item.installed or item.ready or item.can_setup):
                    continue                              # nothing anyone could do with it here
                row = self.detected_row(item, mine)
                self.rows[_key(item)] = row
                rows.append(row)
            if not rows:
                rows.append(Row("No agents can run on this computer yet",
                                "Use an API key or a local model below.", lead="sparkles"))
            rows.append(Row("Add an account from a folder", "For an account Playground didn’t find: show it "
                            "where you keep it.", lead="folder-open", chevron=True,
                            on_activate=self.show_folder_form))
        self.box.append(c.ListGroup(*rows))
        self.box.append(self.computers_section())
        chatgpt = c.module("agents.chatgpt")
        if chatgpt is not None and chatgpt.status("ari-chatgpt").get("state") != "coming-soon":
            self.box.append(section("Ari, Luma’s agent"))
            self.box.append(c.ListGroup(chatgpt_row(self.continue_with_chatgpt)))
        self.box.append(self.key_form())
        self.box.append(self.local_models())
        rule = callout(c.escape(RULE), icon="lock", tone="rule")
        rule.set_margin_top(14)
        self.box.append(rule)

    def continue_with_chatgpt(self) -> None:
        """Ari on the ChatGPT plan: a connection for it, this goal switched to it, then OpenAI's own sign-in in
        the browser (the runner does it; Playground never sees the password)."""
        connections = c.module("agents.connections")
        ctx = self.picker.ctx
        project = ctx.project
        owner = project.owner.id if project and project.owner else ""

        def ensure():
            store = connections.ConnectionStore()
            conn = store.get("ari-chatgpt")
            if conn is None:
                conn = store.add(connections.Connection(id="ari-chatgpt", label="Ari", kind="ari", provider="ari",
                                                        owner=owner, endpoint="chatgpt", sharing="just-me"))
            return conn

        def go(conn) -> None:
            window = self.picker.window
            if ctx.goal_id and callable(getattr(window, "switch_connection", None)):
                window.switch_connection(ctx.goal_id, conn)
                runner = window.runner_for(ctx.goal_id) if callable(getattr(window, "runner_for", None)) else None
                if runner is not None:
                    runner.sign_in_with_agent("chatgpt")
                    self.sheet.close()
                    c.toast(window, "Your browser opens ChatGPT’s sign-in. Ari carries on after.", kind="opening")
                    return
            self.error("Open a conversation first, then choose Ari on your ChatGPT plan.")

        c.run_async(ensure, go, lambda error: self.error(str(error)))

    def detected_row(self, item, mine: set) -> Gtk.Widget:
        """One agent, one button. The button does whatever comes next (set up, sign in, add), then the rest."""
        provider = item.provider
        name = item.name
        sub, end = agent_state_words(item), None
        if item.signed_in and (provider, os.path.realpath(item.home) if item.home else "default") in mine:
            end = c.hbox(c.glyph("check", 14), text("Added", "pgs-row-end", wrap=False), spacing=4)
        elif item.signed_in:
            end = c.button("Add", style="key", small=True, on_click=lambda i=item: self.connect(i))
        elif item.ready or item.can_setup or item.installed:
            end = c.button("Sign in" if item.ready or item.installed else "Set up", style="fill", small=True,
                           on_click=lambda i=item: self.connect(i))
        if end is not None:
            self.buttons[_key(item)] = end
        return Row(name, self.busy.get(_key(item)) or sub, lead=c.mark({"provider": provider, "label": name}, size=28),
                   end=end)

    def say_busy(self, key: str, words: str | None) -> None:
        """Show progress on the account's own row (no rebuild, so nothing else on the page moves)."""
        if words:
            self.busy[key] = words
        else:
            self.busy.pop(key, None)
        row = self.rows.get(key)
        button = self.buttons.get(key)
        if button is not None:
            button.set_sensitive(not words)
        if row is not None and row.subtitle is not None and words:
            row.subtitle.set_markup(c.escape(words))

    def connect(self, item) -> None:
        """Set up (if needed), then sign in (if needed), then add. One click from the person."""
        if _key(item) in self.busy:
            return
        if item.ready or not item.can_setup:
            self.after_setup(item)
            return
        connections = c.module("agents.connections")
        conn = connections.Connection(id=item.provider, label=item.name, kind="personal-plan",
                                      provider=item.provider)
        last = [0.0]

        def progress(words: str, part: float | None) -> None:
            now = GLib.get_monotonic_time() / 1e6
            if now - last[0] < 0.25 and part not in (None, 1.0):
                return
            last[0] = now
            line = f"{words}… {int(part * 100)}%" if part is not None else f"{words}…"
            GLib.idle_add(lambda: (self.say_busy(_key(item), line), False)[1])

        def done(_r) -> None:
            item.ready = True
            self.say_busy(_key(item), None)
            if not self.sheet.closed:
                self.after_setup(item)

        def failed(error) -> None:
            self.say_busy(_key(item), None)
            self.build()
            self.error(str(error))

        self.say_busy(_key(item), f"Getting {item.name} ready…")
        c.run_async(lambda: connections.set_up(conn, progress=progress), done, failed)

    def after_setup(self, item) -> None:
        if item.signed_in:
            self.add_detected(item)
        else:
            self.sign_in(item)

    # Sign in through the program's own login

    def add_detected(self, item) -> None:
        connections = c.module("agents.connections")
        project = self.picker.ctx.project
        owner = project.owner.id if project and project.owner else ""
        suggested = connections.suggest([item], owner, load_mine())
        if not suggested:
            # Unknown counts as a personal plan: that's the stricter rule (never shared).
            kind = "api-key" if item.method == "api key" else "personal-plan"
            suggested = [connections.Connection(id=item.id or item.provider, label=item.name, kind=kind,
                                                provider=item.provider, owner=owner, config_home=item.home)]
        conn = suggested[0]
        if not item.ready and item.can_setup:
            self.connect(item)      # get it ready first; connect() comes back here
            return
        if item.account:
            conn.extra["plan"] = f"{item.account.capitalize()} plan"
        self.save(conn)

    def save(self, conn) -> None:
        connections = c.module("agents.connections")

        def write():
            store = connections.ConnectionStore()
            taken = {x.id for x in store.list()}
            base, n = conn.id, 2
            while conn.id in taken:
                conn.id, n = f"{base}-{n}", n + 1
            return store.add(conn)

        def done(saved) -> None:
            c.toast(self.sheet.card, f"Added {saved.label}", kind="added")
            self.picker.listing.fill(loading=True)
            c.run_async(self.picker.listing.read, self.picker.listing.loaded)
            self.build()

        c.run_async(write, done, lambda error: self.error(str(error)))

    def sign_in(self, item) -> None:
        connections = c.module("agents.connections")
        argv, label = LOGIN.get(item.provider, ([item.program], item.name))
        program = item.program or shutil.which(argv[0])
        project = self.picker.ctx.project
        owner = project.owner.id if project and project.owner else ""
        existing = load_mine()
        conn = connections.Connection(id=item.id or item.provider, label=item.name or label, kind="personal-plan",
                                      provider=item.provider, owner=owner, config_home=item.home)
        if not item.home and any(x.account_key() == conn.account_key() for x in existing):
            # The normal folder is already someone's: this account gets a sealed folder of its own.
            conn.id = f"{item.provider}-2"
            conn.label = f"{label} (second account)"
        connections.assign_config_home(conn, existing)
        if not program:
            self.sign_in_through_agent(conn, item, label)
            return
        env = connections.base_env()
        home_var = connections.HOME_VARS.get(item.provider)
        if home_var and conn.config_home:
            env[home_var] = connections.ensure_config_home(conn) or conn.config_home
        self.show_login([program, *argv[1:]], env, conn, item, label)

    def show_login(self, argv: list[str], env: dict, conn, item, label: str) -> None:
        page = LoginTerminal(argv, env, label, on_exit=lambda ok: self.signed_in(conn, item, ok))
        self.sheet.push(page.box, f"Sign in to {label}", f"{label}’s own sign-in. Playground never sees it.",
                        scroll=False)

    def sign_in_through_agent(self, conn, item, label: str) -> None:
        """No program of its own here: ask the agent (Playground set it up) how it signs in."""
        connections = c.module("agents.connections")
        self.say_busy(_key(item), "Opening the sign-in…")

        def ask():
            return connections.agent_sign_in(conn)

        def got(answer) -> None:
            how, detail = answer
            if how == "terminal":
                self.say_busy(_key(item), None)
                self.show_login(detail[0], detail[1], conn, item, label)
            elif how == "agent":
                self.say_busy(_key(item), "Finish signing in in your browser…")
                c.toast(self.sheet.card, f"Your browser opens {label}’s sign-in.", kind="opening")
                c.run_async(lambda: connections.agent_authenticate(conn, detail),
                            lambda _r: (self.say_busy(_key(item), None), self.signed_in(conn, item, True)),
                            failed)
            else:
                failed(RuntimeError(f"{label} didn’t say how to sign in. Try an API key instead."))

        def failed(error) -> None:
            self.say_busy(_key(item), None)
            self.error(str(error))

        c.run_async(ask, got, failed)

    def signed_in(self, conn, item, _exit_ok: bool) -> None:
        connections = c.module("agents.connections")

        def check():
            return connections.check_account(item.provider, conn.config_home)

        def done(found) -> None:
            # The Mac keeps Claude's sign-in in the keychain, where Playground can't look: trust the clean exit.
            if id(conn) in self.finished:
                return              # the terminal ended after the person already said they'd signed in
            if found is not None and (found.signed_in or found.signed_in is None and _exit_ok):
                self.finished.add(id(conn))
                if found.account:
                    conn.extra["plan"] = f"{found.account.capitalize()} plan"
                if found.method == "api key":
                    conn.kind = "api-key"
                self.sheet.pop()
                self.save(conn)
            else:
                c.toast(self.sheet.card, f"{conn.label} isn’t signed in yet", kind="warning")

        c.run_async(check, done, lambda error: self.error(str(error)))

    # Agents on another computer, over SSH (agents/computers.py)

    def computers_section(self) -> Gtk.Widget:
        box = c.vbox()
        box.append(section("Another computer", note="Agents on a build server or another of your machines, "
                                                      "reached with SSH. They work in their own copy of the project "
                                                      "there, and their work comes back here after every turn."))
        computers = c.module("agents.computers")
        rows = []
        if computers is not None:
            for computer in computers.ComputerStore().list():
                where = computer.destination + (f" · port {computer.port}" if computer.port != 22 else "")
                rows.append(Row(computer.name, where, lead="server",
                                end=c.button("Find agents", style="fill", small=True,
                                             on_click=lambda cc=computer: self.find_on(cc))))
        rows.append(Row("Add a computer…", "Reach it with SSH, like root@10.0.0.4", lead="plus", chevron=True,
                        on_activate=self.show_computer_form))
        box.append(c.ListGroup(*rows))
        return box

    def show_computer_form(self) -> None:
        page = c.vbox(spacing=12, css="pgs-sheet-body")
        page.append(text("Playground reaches it with SSH, the way you would from a terminal. The first time, it "
                         "trusts the computer’s identity; after that it checks it every time.", "pgs-row-sub"))
        address = Gtk.Entry(placeholder_text="root@build-server.local or user@10.0.0.4:2222", hexpand=True)
        address.update_property([Gtk.AccessibleProperty.LABEL], ["Address"])
        name = Gtk.Entry(placeholder_text="Build server", hexpand=True)
        name.update_property([Gtk.AccessibleProperty.LABEL], ["Name"])
        key = Gtk.Entry(placeholder_text="Your usual key (leave empty)", hexpand=True)
        key.update_property([Gtk.AccessibleProperty.LABEL], ["Key file"])
        jump = Gtk.Entry(placeholder_text="Only for a machine behind another one: user@gateway", hexpand=True)
        jump.update_property([Gtk.AccessibleProperty.LABEL], ["Reach it through"])

        def choose_key() -> None:
            dialog = Gtk.FileDialog(title="Choose the key", modal=True)

            def done(dlg, result) -> None:
                try:
                    picked = dlg.open_finish(result)
                except GLib.Error:
                    return
                if picked is not None and picked.get_path():
                    key.set_text(picked.get_path())
            dialog.open(self.picker.window, None, done)

        for label, entry, extra in (("Address", address, None), ("Name", name, None),
                                    ("Key", key, c.button("Choose…", style="fill", small=True, on_click=choose_key)),
                                    ("Through", jump, None)):
            line = c.hbox(text(label, "pgs-field-label", wrap=False), c.vbox(entry, css="pgs-field", hexpand=True),
                          spacing=12)
            if extra is not None:
                line.append(extra)
            page.append(line)
        status = text("", "pgs-row-sub")
        connect = c.button("Connect and look", style="key", small=True,
                           on_click=lambda: self.add_computer(address.get_text(), name.get_text(), key.get_text(),
                                                             jump.get_text(), status, connect))
        connect.set_halign(Gtk.Align.END)
        page.append(status)
        page.append(connect)
        self.sheet.push(page, "Add a computer", "Agents there show up here, ready to use")
        GLib.idle_add(lambda: (address.grab_focus(), False)[1])

    def add_computer(self, address: str, name: str, key: str, jump: str, status: Gtk.Label,
                     button: Gtk.Widget) -> None:
        computers = c.module("agents.computers")
        try:
            user, host, port = computers.parse_address(address)
        except computers.ComputerError as error:
            status.set_text(str(error))
            return
        computer = computers.Computer(id="", name=name.strip() or host, host=host, user=user, port=port,
                                      identity=key.strip(), jump=jump.strip())
        status.set_text(f"Reaching {computer.name}…")
        button.set_sensitive(False)

        def work():
            reached = computers.test(computer)
            computer.home = reached.get("home", "")
            return computers.ComputerStore().add(computer)

        def done(saved) -> None:
            button.set_sensitive(True)
            self.sheet.pop()
            self.build()
            self.find_on(saved)

        def failed(error) -> None:
            button.set_sensitive(True)
            status.set_text(str(error))

        c.run_async(work, done, failed)

    def find_on(self, computer, deep: bool = False) -> None:
        computers = c.module("agents.computers")
        page = c.vbox(spacing=10, css="pgs-sheet-body")
        page.append(Row(f"Searching {computer.name}’s disks…" if deep else f"Looking around {computer.name}…",
                        "Every person’s home on it, their Claude and Codex, and whether they’re signed in. "
                        "Only folders and yes or no come back, never a password or token.", lead=_spin_tile()))
        self.sheet.push(page, computer.name, computer.destination)

        def done(found) -> None:
            c.clear(page)
            self.show_found(page, computer, found)

        def failed(error) -> None:
            c.clear(page)
            page.append(callout(c.escape(str(error)), icon="circle-alert", tone="info"))

        c.run_async(lambda: computers.probe(computer, deep=deep), done, failed)

    def sign_in_on(self, computer, account: dict, title: str) -> None:
        """The account's own sign-in, on the computer, in a terminal here; then look again."""
        computers = c.module("agents.computers")
        user = account.get("user") or ""
        argv = computers.sign_in_argv(computer, str(account.get("provider") or ""), str(account.get("home") or ""),
                                      user="" if user == computer.user else user,
                                      user_home="" if user == computer.user else str(account.get("user_home") or ""))

        def ended(_ok: bool) -> None:
            self.sheet.pop()
            self.sheet.pop()
            self.find_on(computer)
        page = LoginTerminal(argv, {}, title, on_exit=ended)
        self.sheet.push(page.box, f"Sign in to {title}", f"On {computer.name}. Playground never sees it.",
                        scroll=False)

    def show_found(self, page: Gtk.Box, computer, found: dict) -> None:
        computers = c.module("agents.computers")
        project = self.picker.ctx.project
        owner = project.owner.id if project and project.owner else ""
        offered = {(conn.provider, conn.config_home): conn
                   for conn in computers.suggest_connections(computer, found, owner, load_mine())}
        accounts = [a for a in found.get("accounts") or [] if a.get("provider") in ("claude", "codex")]
        checks: list = []
        rows = []
        for account in accounts:
            conn = offered.get((account.get("provider"), account.get("home") or ""))
            who = account.get("user") or ""
            title = (account.get("label") or str(account.get("provider", "")).capitalize()) + \
                (f" · {who}" if who and who != computer.user else "")
            how = {"ChatGPT": "signed in with ChatGPT", "subscription": "signed in with a plan",
                   "api key": "signed in with an API key"}.get(account.get("method") or "", "signed in")
            if not account.get("signed_in"):
                sub = f"{account.get('home')} · not signed in yet"
                end = c.button("Sign in", style="fill", small=True,
                               on_click=lambda a=account, t=title: self.sign_in_on(computer, a, t))
            elif conn is None:
                sub, end = f"{account.get('home')} · already added", c.glyph("check", 14)
            else:
                sub = f"{account.get('home')} · {how}"
                end = Gtk.CheckButton(active=True, valign=Gtk.Align.CENTER)
                end.update_property([Gtk.AccessibleProperty.LABEL], [f"Add {title}"])
                checks.append((end, conn))
            rows.append(Row(title, sub, lead=c.mark({"provider": account.get("provider"), "label": title}, size=28),
                            end=end, dim=not account.get("signed_in")))
        node = found.get("node") or "no Node.js yet (Playground brings its own)"
        page.append(text(f"{computer.name} · {found.get('system', '')} · {node}", "pgs-row-sub"))
        if rows:
            page.append(c.ListGroup(*rows))
        else:
            page.append(callout("No Claude or Codex accounts here yet. If they’re kept somewhere unusual, Look harder "
                                "searches the computer’s disks.", icon="info", tone="info"))
        harder = c.button("Look harder", style="fill", small=True,
                          on_click=lambda: (self.sheet.pop(), self.find_on(computer, deep=True)))

        def add_chosen() -> None:
            chosen = [conn for check, conn in checks if check.get_active()]
            if not chosen:
                return
            store = c.module("agents.connections").ConnectionStore()

            def done(saved) -> None:
                self.sheet.pop()
                c.toast(self.sheet.card, f"Added {len(saved)} agent{'s' if len(saved) != 1 else ''} on "
                                         f"{computer.name}", kind="added")
                self.picker.listing.fill(loading=True)
                c.run_async(self.picker.listing.read, self.picker.listing.loaded)
                self.build()

            c.run_async(lambda: [store.add(conn) for conn in chosen], done, lambda error: self.error(str(error)))

        add = c.button(f"Add {len(checks)} agent{'s' if len(checks) != 1 else ''}" if checks else "Nothing to add",
                       style="key", small=True, on_click=add_chosen, sensitive=bool(checks))
        page.append(c.hbox(harder, c.vbox(hexpand=True), add, spacing=8))

    # An account Playground didn't find: the person shows it where it is

    def show_folder_form(self) -> None:
        page = c.vbox(spacing=12, css="pgs-sheet-body")
        page.append(text("Some people keep an account’s sign-in folder somewhere only they know. Choose the "
                         "folder and Playground uses that account from now on. An empty folder works too: the "
                         "account signs in there.", "pgs-row-sub"))
        state = {"provider": "claude", "folder": ""}
        page.append(c.segmented([("claude", "Claude"), ("codex", "Codex")], "claude", label="Which program",
                                on_change=lambda key: state.update(provider=key)))
        page.get_last_child().set_halign(Gtk.Align.START)
        chosen = text("No folder chosen yet", "pgs-path", selectable=True)
        use = c.button("Use this account", style="key", small=True, sensitive=False,
                       on_click=lambda: self.use_folder(state["provider"], state["folder"]))

        def picked(folder: str) -> None:
            state["folder"] = folder
            chosen.set_text(folder)
            use.set_sensitive(True)

        page.append(c.hbox(c.button("Choose folder…", icon="folder-open", style="fill", small=True,
                                    on_click=lambda: self.choose_folder(picked)), chosen, spacing=10))
        use.set_halign(Gtk.Align.END)
        page.append(use)
        self.sheet.push(page, "Add an account from a folder", "Playground only checks the folder is there. It "
                        "never reads the sign-in inside.")

    def choose_folder(self, picked: Callable[[str], None]) -> None:
        dialog = Gtk.FileDialog(title="Choose the account’s folder", modal=True)

        def finished(dlg, result) -> None:
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return              # the person closed the chooser
            if folder is not None and folder.get_path():
                picked(folder.get_path())

        dialog.select_folder(self.picker.window, None, finished)

    def use_folder(self, provider: str, folder: str) -> None:
        connections = c.module("agents.connections")
        accounts = c.module("agents.accounts")
        paths = c.module("core.paths")
        folder = os.path.realpath(folder)
        if not os.path.isdir(folder):
            self.error("That folder isn’t there any more.")
            return
        taken = {conn.account_key() for conn in load_mine()}
        if (provider, folder) in taken or folder == os.path.realpath(accounts.default_home(provider, os.environ)):
            self.error("Playground already uses that account.")
            return
        others = [d for d in self.detected or [] if d.provider == provider]
        label = accounts._label_from_folder(provider, folder) or f"{accounts.NAMES[provider]} {len(others) + 1}"
        used = {d.name for d in self.detected or []} | {conn.label for conn in load_mine()}
        base, n = label, 2
        while label in used:
            label, n = f"{base} {n}", n + 1

        def check():
            item = connections.check_account(provider, folder)
            item.name, item.id, item.home, item.found_in = label, paths.slugify(label, 32), folder, "a folder you chose"
            template = next((d for d in self.detected or [] if d.provider == provider), None)
            if template is not None:
                item.ready, item.can_setup = template.ready, template.can_setup
            return item

        def go(item) -> None:
            self.sheet.pop()
            self.detected = [*(self.detected or []), item]
            self.build()
            self.connect(item)

        c.run_async(check, go, lambda error: self.error(str(error)))

    # An API key, into the keyring

    def key_form(self) -> Gtk.Widget:
        box = c.vbox()
        box.append(section("An API key", note="Pay as you go with a key from Anthropic, OpenAI or Google. The key "
                                              "goes into this computer’s keyring, never into a project."))
        form = c.vbox(spacing=10, css="pgs-form-card")
        form.append(c.segmented(KEY_PROVIDERS, self.key_provider, label="Whose key",
                                on_change=lambda key: setattr(self, "key_provider", key)))
        form.get_last_child().set_halign(Gtk.Align.START)
        name = Gtk.Entry(hexpand=True, placeholder_text="Team key")
        name.update_property([Gtk.AccessibleProperty.LABEL], ["Name for this key"])
        secret = Gtk.PasswordEntry(hexpand=True, show_peek_icon=True, placeholder_text="Paste the key")
        secret.update_property([Gtk.AccessibleProperty.LABEL], ["API key"])
        form.append(c.hbox(c.vbox(name, css="pgs-field", hexpand=True), c.vbox(secret, css="pgs-field", hexpand=True),
                           spacing=8))
        cap = Gtk.Entry(placeholder_text="$25 a month", width_chars=12)
        cap.update_property([Gtk.AccessibleProperty.LABEL], ["Spending cap"])
        cap_field = c.vbox(cap, css="pgs-field")
        cap_field.set_visible(False)
        share = c.switch(False, label="Share with the team",
                         on_toggle=lambda on: (setattr(self, "key_share", on), cap_field.set_visible(on)))
        form.append(c.hbox(share, text("Share with the team", "pgs-opt-title", wrap=False), c.vbox(hexpand=True),
                           cap_field, c.button("Save key", style="key", small=True,
                                               on_click=lambda: self.save_key(name.get_text(), secret, cap.get_text())),
                           spacing=10))
        form.append(text("Shared keys need a spending cap. Agents stop and ask before going over, and every run "
                         "says it used this key.", "pgs-field-hint"))
        box.append(form)
        return box

    def save_key(self, label: str, secret: Gtk.PasswordEntry, cap: str) -> None:
        connections = c.module("agents.connections")
        key = secret.get_text().strip()
        if not key:
            self.error("Paste the key first.")
            return
        provider = self.key_provider
        project = self.picker.ctx.project
        owner = project.owner.id if project and project.owner else ""
        words = dict(KEY_PROVIDERS)[provider]
        label = label.strip() or f"{words} API key"
        paths = c.module("core.paths")
        conn = connections.Connection(id=paths.slugify(label, 32), label=label, kind="api-key", provider=provider,
                                      owner=owner, sharing="team" if self.key_share else "just-me",
                                      spend_cap=cap.strip() if self.key_share else "")
        conn.key_ref = conn.id
        try:
            conn.validate()
        except Exception as error:  # noqa: BLE001 - ConnectionRuleError, already in plain words
            self.error(str(error))
            return
        argv = connections.store_api_key_command(conn)
        if argv is None:
            self.error("This computer has no keyring to keep the key in, so Playground won’t save it.")
            return
        secret.set_text("")

        def store_key() -> None:
            result = subprocess.run(argv, input=key, text=True, capture_output=True, timeout=30)
            if result.returncode != 0:
                raise RuntimeError("The keyring didn’t accept the key. Is it unlocked?")

        c.run_async(store_key, lambda _r: self.save(conn), lambda error: self.error(str(error)))

    # A local model through Ollama

    def local_models(self) -> Gtk.Widget:
        box = c.vbox()
        box.append(section("A local model", note="Free, and nothing leaves this computer. Smaller models do "
                                                 "chores; 24 GB of memory or more runs a real local agent."))
        ollama = next((d for d in self.detected or [] if d.provider == "ollama"), None)
        rows = []
        if self.detected is None:
            rows.append(Row("Looking for Ollama…", None, lead=_spin_tile()))
        elif ollama is None or not ollama.installed:
            rows.append(Row("Ollama isn’t here", "Install Ollama to run models on this computer.", lead="cpu"))
        elif not ollama.models:
            rows.append(Row("Ollama has no models yet",
                            ollama.note or "Pull one with Ollama, like qwen3:32b, then come back.", lead="cpu"))
        else:
            have = {(conn.model or "") for conn in load_mine() if conn.kind in ("local", "ari")}
            for model in ollama.models:
                end = (c.hbox(c.glyph("check", 14), text("Added", "pgs-row-end", wrap=False), spacing=4)
                       if model in have else
                       c.button("Add", style="fill", small=True, on_click=lambda m=model: self.add_local(m)))
                rows.append(Row(model, "On this computer", lead=c.mark({"provider": "ollama", "label": model},
                                                                         size=28), end=end))
        box.append(c.ListGroup(*rows))
        return box

    def add_local(self, model: str) -> None:
        connections = c.module("agents.connections")
        paths = c.module("core.paths")
        project = self.picker.ctx.project
        owner = project.owner.id if project and project.owner else ""
        label = model.split(":")[0].replace("-", " ").title() + (f" {model.split(':')[1].upper()}"
                                                                   if ":" in model else "")
        conn = connections.Connection(id=paths.slugify(f"local-{model}", 40), label=label, kind="local",
                                      provider="ollama", owner=owner, endpoint="ollama",
                                      base_url="http://localhost:11434/v1", model=model)
        self.save(conn)

    def error(self, words: str) -> None:
        c.toast(self.sheet.card, words, kind="error")


class LoginTerminal:
    """The agent program's own sign-in, in a real terminal inside the sheet."""

    def __init__(self, argv: list[str], env: dict, label: str, on_exit: Callable[[bool], None], *,
                 carry_on: bool = False) -> None:
        self.box = c.vbox(vexpand=True)
        self.on_exit = on_exit
        then = (f"When it says you’re signed in, {c.escape(label)} carries on where it stopped." if carry_on else
                "When it says you’re signed in, Playground checks and adds it.")
        note = callout(f"This is {c.escape(label)}’s own sign-in. Follow its steps; it may open your browser. {then}",
                       icon="lock", tone="rule")
        note.set_margin_start(22)
        note.set_margin_end(22)
        note.set_margin_bottom(12)
        self.box.append(note)
        try:
            import gi
            gi.require_version("Vte", "3.91")
            from gi.repository import Vte
        except (ImportError, ValueError):
            command = " ".join(argv)
            self.box.append(c.vbox(text("Open Terminal and run this, then come back:", "pgs-row-sub"),
                                   text(command, "pgs-path", selectable=True), spacing=8, css="pgs-sheet-body"))
            self.box.append(c.button("I’ve signed in", style="key", on_click=lambda: on_exit(True)))
            return
        terminal = Vte.Terminal(vexpand=True, hexpand=True)
        terminal.add_css_class("pgs-term-body")
        terminal.set_size_request(-1, 320)
        frame = c.vbox(terminal, css="pgs-term-frame", vexpand=True)
        self.box.append(frame)
        terminal.connect("child-exited", lambda _t, status: on_exit(status == 0))
        # Some sign-ins finish inside a program that keeps running (Claude's /login): let the person say so.
        finished = c.button("I’ve signed in", style="key", small=True, on_click=lambda: on_exit(True))
        finished.set_halign(Gtk.Align.END)
        finished.set_margin_top(10)
        finished.set_margin_end(22)
        self.box.append(finished)
        envv = [f"{k}={v}" for k, v in env.items()]
        terminal.spawn_async(Vte.PtyFlags.DEFAULT, os.path.expanduser("~"), argv, envv, GLib.SpawnFlags.SEARCH_PATH,
                             None, None, -1, None, None)
        GLib.idle_add(lambda: (terminal.grab_focus(), False)[1])


def _spin_tile() -> Gtk.Widget:
    tile = c.vbox(css="pgs-lead", valign=Gtk.Align.CENTER)
    spinner = c.shared.Spinner(16)
    spinner.set_vexpand(True)
    tile.append(spinner)
    return tile
