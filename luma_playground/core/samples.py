# SPDX-License-Identifier: Apache-2.0
"""Sample projects for first runs, screenshots and tests.

``create_sample_kiln`` builds Kiln as simulator v73 shows it: class sign-ups
for Maya's pottery studio, built by Alex, Priya and Sam. Six goals in every
state, conversations made of every card, decisions, notes, a fortnight of
history, three places, and a small real web app in ``src/`` that the Stage can
run (see ``.playground/run.toml``).

``create_sample_empty`` builds a project the moment after it was made: a
brief, a recipe and the general chat, and nothing else yet.

Dates are relative to today, so the sample always reads as this week.

    python3 -m luma_playground.core.samples <projects root>
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys

from .model import (
    ActivityEntry, AnalyticsSnapshot, Answer, Brief, BRIEF_GOOD, BRIEF_NOT_NOW, BRIEF_SUMMARY, Check, Decision,
    Goal, GoalStatus, Issue, KnowledgeNote, Member, Option, PendingDecision, Place, Project, Recipe, RecipePart,
    RunConfig, Service, ServiceUsage, Stack, Story, ThreadEntry, GENERAL_CHAT_ID,
)
from .paths import projects_root
from .project import create_project, init_git
from .store import ProjectStore, atomic_write_text

# ── When things happened. v73's "today" is a Thursday; the sample keeps that week's shape. ──

TODAY, WEDNESDAY, TUESDAY, MONDAY = 0, 1, 2, 3


def _at(days_ago: int, hhmm: str) -> str:
    hour, minute = (int(part) for part in hhmm.split(":"))
    day = datetime.now().date() - timedelta(days=days_ago)
    return datetime.combine(day, dtime(hour, minute)).astimezone().isoformat(timespec="seconds")


def _day_name(days_ago: int) -> str:
    """How people say that day: "Tuesday" within the week, "Oct 2" before it."""
    day = datetime.now().date() - timedelta(days=days_ago)
    return day.strftime("%A") if days_ago < 7 else f"{day.strftime('%b')} {day.day}"


# The older history, in days before today. The last week matches v73 day for day; the start is set
# three weeks back so the live app has a few weeks of usage and analytics behind it.
CREATED, TEAM_SETUP, GIFTS_STARTED, FIRST_RELEASE, SIGNIN_STARTED, GIFTS_PAUSED, ORDER_UNDONE = 26, 25, 22, 21, 8, 6, 5

# ── The people, places and recipe ──

MEMBERS = [
    Member(id="alex", name="Alex", role="owner", extra={"photo": "people/alex.svg"}),
    Member(id="priya", name="Priya Raman", role="builder", extra={"photo": "people/priya.svg", "pronouns": "she/her"}),
    Member(id="sam", name="Sam Kaur", role="reviewer", extra={"photo": "people/sam.svg"}),
]

PLACES = [
    Place(id="this-computer", name="This computer", role="development", kind="local",
          description="Alex's laptop. Where you talk to the agent; your copy of the project is kept in sync here."),
    Place(id="dev-server", name="Dev server", role="development", kind="ssh", host="203.0.113.7", user="kiln", port=22,
          description="The box in the closet. Runs the app and the agents, including Stage, so teammates can work while laptops are closed."),
    Place(id="production", name="Production", role="production", kind="ssh", host="kiln.example", user="deploy", port=22,
          description="An AWS Lightsail instance. The live app. Agents can't write here; changes arrive only through a reviewed release.",
          extra={"url": "https://kiln.example", "path": "/srv/kiln"}),
]

KILN_RECIPE = Recipe(
    summary="A web app that works great on phones",
    runs_on="The Dev server for Stage, AWS Lightsail for Production",
    parts=[
        RecipePart("Pages", "HTML, CSS and a little JavaScript", "Quick on phones, and any agent can work on it."),
        RecipePart("Data", "A PostgreSQL database on the server",
                   "Classes, bookings and the waitlist. Stage has its own copy."),
        RecipePart("Sign-in", "Luma Connect", "One tap, no passwords."),
        RecipePart("Texts and email", "A texting service and an email service", "Reminders and waitlist offers."),
        RecipePart("Runs on", "The Dev server for Stage, AWS Lightsail for Production", "See Places."),
    ],
)

KILN_SUMMARY = "Class sign-ups for Maya’s pottery studio"

# How a reviewed release reaches Production (ARCHITECTURE.md, "Addendum: releases to production").
KILN_DEPLOY = """
# How a reviewed release reaches Production. Playground runs this only after Review,
# and asks a person to read it the first time and whenever it changes.
[deploy]
kind = "ssh"
source = "src"
path = "/srv/kiln"
restart = "sudo systemctl reload kiln"
expect_text = "Kiln"
snapshot = "pg_dump --format=custom kiln"
restore_data = "pg_restore --clean --if-exists --dbname=kiln"
"""

KNOWLEDGE = [
    ("classes-hold-eight-people", "Classes hold eight people",
     "There are eight wheels, so every class caps at eight. Maya never overbooks.", "person:alex", CREATED,
     "decision", "kept"),
    ("phone-numbers-need-the-country-code", "Phone numbers need the country code",
     "The texting service rejects numbers without +1. Found while fixing reminders.\n\n"
     "Save every number in full international form (+1 510 555 0142). `server/phone.js` does this.",
     "agent:claude@priyas-mac", TODAY, "lesson", "kept"),
    ("how-kiln-looks", "How Kiln looks",
     "Warm clay and cream, Newsreader for headings, Atkinson Hyperlegible for everything else. "
     "Buttons are pill shaped.\n\n- Clay: #b5532f\n- Cream: #f7f1e8\n- Ink: #2b211a\n- Sage: #5f6f52",
     "agent:ari", TEAM_SETUP, "note", "kept"),
    ("maya-checks-bookings-on-her-phone", "Maya checks bookings on her phone",
     "She manages the studio from her phone between classes, so her view has to work one-handed.", "person:alex",
     CREATED, "purpose", "kept"),
    ("never-publish-student-details", "Never publish students’ names or numbers",
     "Not in the repository, not in screenshots, not in a bug report.", "person:alex", TEAM_SETUP, "rule", "kept"),
    ("ask-before-anything-that-costs-money", "Ask before adding anything that costs money every month",
     "", "person:alex", TEAM_SETUP, "rule", "kept"),
    ("reminders-went-out-twice", "Reminders went out twice on Sunday",
     "Two agents changed the reminder job the same afternoon. Check the team channel before touching the "
     "scheduler.", "agent:codex@alexs-laptop", WEDNESDAY, "mistake", "kept"),
    ("waitlist-before-payments", "Keep the waitlist working before adding payments",
     "Payments change the booking flow; the waitlist must stay correct while that happens.", "agent:codex@alexs-laptop",
     TODAY, "rule", "suggested"),
]

TASKS = [
    # title, status, priority, created_by, claimed_by, body
    ("Let students join a waitlist when a class is full", "doing", "high", "person:alex", "agent:codex@alexs-laptop",
     "## Checks\n- [x] A full class shows Join waitlist\n- [x] Maya sees the waitlist in her day\n"
     "- [ ] The first person on the list gets a text when a seat opens"),
    ("Send a reminder the evening before each class", "todo", "normal", "person:priya", "",
     "## Checks\n- [ ] One text, the evening before, never two"),
    ("Show an empty week kindly", "todo", "low", "person:sam", "", ""),
    ("A gift card Maya can sell at the front desk", "todo", "normal", "person:alex", "", ""),
    ("Studio photos on the home page", "done", "normal", "agent:claude@priyas-mac", "",
     "## Outcome\nSix photos in a row; booking stays on the first screen on phones."),
]

CHANNEL = [
    (WEDNESDAY, "15:10", "person:priya", "start", "Priya asked Claude: fix the reminder job sending twice", ""),
    (WEDNESDAY, "16:42", "agent:claude@priyas-mac", "done",
     "Reminders send once now; the job checks it hasn't run today", ""),
    (TODAY, "09:02", "person:alex", "start", "Alex asked Codex: let students join a waitlist when a class is full",
     "t-wa1t1"),
    (TODAY, "09:05", "agent:codex@alexs-laptop", "start",
     "Taking t-wa1t1. Changing src/waitlist.js and server/schema.sql", "t-wa1t1"),
    (TODAY, "10:31", "agent:codex@alexs-laptop", "heads-up",
     "Added a seats column to classes; anything reading class rows should use it", "t-wa1t1"),
]


# ══ Kiln ═══════════════════════════════════════════════════════════════════════

def create_sample_kiln(root: Path | str | None = None, *, git: bool = True, bus=None) -> Project:
    """Build the Kiln sample under ``root`` (default: the projects root) and return it."""
    brief = Brief(title="Kiln", audience="Maya’s students, mostly booking on their phones, and Maya herself, "
                                         "checking her day between classes.",
                  platforms=["phones", "computers"])
    brief.set_section(BRIEF_SUMMARY, "A phone-first web app where people book a wheel class at Maya’s studio, "
                                     "join a waitlist when it’s full, and get a reminder the day before.")
    brief.set_section("Where", "Phones first. It works on computers too.")
    brief.set_section(BRIEF_GOOD, "- Booking takes under a minute.\n- Nobody is double-booked.\n"
                                  "- Nobody misses a class because a reminder didn’t arrive.")
    brief.set_section(BRIEF_NOT_NOW, "Payments and gift cards, for now.")

    project = create_project("Kiln", brief=brief, recipe=KILN_RECIPE, home="dev-server",
                             root=root if root is not None else projects_root(), summary=KILN_SUMMARY,
                             owner=MEMBERS[0], git=False, bus=bus)
    store = ProjectStore(project.path, bus=bus)
    project.created = _at(CREATED, "15:02")
    project.members = list(MEMBERS)
    project.places = list(PLACES)
    store.write_project(project)

    _write_app(project.path)
    store.write_run_config(RunConfig(command="python3 -m http.server {port} --bind 127.0.0.1", cwd="src", port=0,
                                     url_path="/", kind="web"),
                           comment="How Playground runs Kiln for the Stage.\n"
                                   "{port} is replaced with a free port when port = 0.")
    atomic_write_text(store.paths.run_toml, store.paths.run_toml.read_text() + KILN_DEPLOY)
    for slug, title, body, by, days, kind, status in KNOWLEDGE:
        store.write_knowledge(KnowledgeNote(slug=slug, title=title, body=body, added_by=by, updated=_at(days, "12:00"),
                                            extra={"kind": kind, "status": status, "created": _at(days, "12:00")}))
    _tasks(store)
    for decision in _decisions():
        store.append_decision(decision)

    _talk_to_ari(store)
    _waitlist(store)
    _texts(store)
    _photos(store)
    _signin(store)
    _gifts(store)
    _stack(store)
    _issues(store)
    _usage_and_analytics(store)
    _people(store)
    _capacity(store)
    _write_activity(store)
    if git:
        _history(store, project.path)
    return store.read_project()


def _tasks(store: ProjectStore) -> None:
    """Tasks and the team channel: Codex is on the waitlist, the rest wait, and the channel says so."""
    from .model import Task
    for index, (title, status, priority, by, claimed, body) in enumerate(TASKS):
        task_id = "t-wa1t1" if index == 0 else f"t-k1ln{index}"
        task = Task(id=task_id, title=title, status=status, priority=priority, created_by=by, updated_by=by,
                    created_at=_at(TEAM_SETUP if index else WEDNESDAY, "10:00"), body=body)
        if claimed:
            task.claimed_by = claimed
            task.claimed_until = "2099-01-01T00:00:00Z"
        store.write_task(task, by=by, touch=False)
    import json
    from .store import append_line
    for days, hhmm, who, kind, words, task_id in CHANNEL:
        entry = {"ts": _at(days, hhmm), "from": who, "kind": kind, "text": words}
        if task_id:
            entry["task"] = task_id
        append_line(store.paths.channel, json.dumps(entry, ensure_ascii=False))
    store.refresh_area("channel")


WAITLIST_FILES = ("src/index.html", "src/kiln.js", "src/waitlist.js", "src/styles.css", "src/maya.html",
                  "server/schema.sql")


def _history(store: ProjectStore, folder: Path) -> None:
    """Two commits: Kiln before the waitlist, then the waitlist. The waitlist goal records the first as its
    base, so its code page shows the goal's real change instead of the whole files."""
    originals: dict[Path, str] = {}
    for relative in WAITLIST_FILES:
        path = folder / relative
        try:
            originals[path] = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if path.name == "waitlist.js":
            path.unlink()
            continue
        path.write_text("\n".join(line for line in originals[path].split("\n") if "waitlist" not in line.lower()),
                        encoding="utf-8")
    if not init_git(folder, "Kiln, before the waitlist"):
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")
        return
    git = shutil.which("git")

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([git, *args], cwd=folder, capture_output=True, text=True, timeout=30,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    base = run("rev-parse", "HEAD").stdout.strip()
    for path, text in originals.items():
        path.write_text(text, encoding="utf-8")
    if base:
        goal = store.read_goal(WAITLIST)
        goal.extra["base_commit"] = base
        store.write_goal(goal, touch=False)
    identity = [] if run("config", "user.email").stdout.strip() else \
        ["-c", "user.name=Playground", "-c", "user.email=playground@localhost"]
    run("add", "-A")
    run(*identity, "commit", "-q", "--no-gpg-sign", "-m", "Add a waitlist, and Kiln as it stands this week")


def _goal(store: ProjectStore, goal: Goal) -> Goal:
    store.write_goal(goal, touch=False)
    return goal


def _say(store: ProjectStore, goal_id: str, days: int, hhmm: str, kind: str, *, role: str = "agent", by: str = "ari",
         text: str = "", **data) -> ThreadEntry:
    return store.append_thread(goal_id, ThreadEntry(kind=kind, role=role, by=by, text=text, data=data, ts=_at(days, hhmm)))


def _person(store: ProjectStore, goal_id: str, days: int, hhmm: str, who: str, text: str) -> ThreadEntry:
    return _say(store, goal_id, days, hhmm, "message", role="person", by=who, text=text)


def _decisions() -> list[Decision]:
    return [
        Decision(title="Classes cap at eight", chosen="Eight people per class", why="One wheel each.",
                 who="Maya, via Alex", date=_at(CREATED, "15:20")),
        Decision(title="Stage gets its own copy of the bookings", chosen="A separate copy for Stage",
                 why="So testing never texts a real person.", who="Ari suggested, Priya agreed", date=_at(TEAM_SETUP, "10:05")),
        Decision(title="Sign in with Luma Connect, not passwords", chosen="Luma Connect",
                 why="Nobody has to remember another password, and Kiln never keeps one.", who="Alex",
                 date=_at(SIGNIN_STARTED, "10:15"), goal="sign-in-with-luma-connect-5a70"),
        Decision(title="Old bookings follow people when they sign in", chosen="Yes, match them by phone number",
                 why="Matched by phone number, so 212 bookings didn’t look like they vanished.", who="Alex",
                 date=_at(SIGNIN_STARTED, "10:24"), goal="sign-in-with-luma-connect-5a70", id="old-bookings-6c2e"),
    ]


def _talk_to_ari(store: ProjectStore) -> None:
    goal = store.read_goal(GENERAL_CHAT_ID)
    goal.created = goal.updated = _at(CREATED, "15:02")
    _goal(store, goal)
    gid = GENERAL_CHAT_ID
    _person(store, gid, CREATED, "15:02", "alex", "Maya wants people to book wheel classes at her studio from their "
                                                 "phones. Can we make her a sign-up page?")
    _say(store, gid, CREATED, "15:03", "brief", title="Kiln", audience="Maya’s students, and Maya",
         platforms=["phones", "computers"],
         summary="A phone-first web app where people book a wheel class at Maya’s studio.",
         good_looks_like=["Booking takes under a minute", "Nobody is double-booked"])
    _say(store, gid, CREATED, "15:03", "recipe", summary=KILN_RECIPE.summary,
         parts=[p.to_dict() for p in KILN_RECIPE.parts[:3]], runs_on="This computer, for now")
    _person(store, gid, CREATED, "15:05", "alex", "Looks good.")
    _person(store, gid, TODAY, "09:02", "alex", "Morning. Where are we with Kiln?")
    _say(store, gid, TODAY, "09:02", "message",
         text="Morning, Alex. Kiln is live, and 34 people booked a class through it this week.\n\n"
              "The waitlist needs one decision from you before it can go further. Priya’s agent is fixing the "
              "reminder texts that didn’t arrive, and Sam has approved the studio photos, so they’re waiting for you.",
         where="this-computer", duration_s=2.4,
         chips=[{"label": "Add a waitlist", "goal_id": "add-a-waitlist-7f3a"},
                {"label": "Fix reminder texts", "goal_id": "fix-reminder-texts-2c41"},
                {"label": "Studio photos", "goal_id": "studio-photos-on-the-home-page-9e12"}])
    store.append_thread(gid, ThreadEntry(kind="cost", by="ari", ts=_at(TODAY, "09:02"), data={
        "title": "Kiln this week", "amount_usd": 1.12, "percent": 14, "label": "$1.12 of $8 a week",
        "text": "Ari used $1.12 of the team’s credits. Priya’s agent did the texts work on her own plan."}))


WAITLIST = "add-a-waitlist-7f3a"
WAITLIST_DECISION = "waitlist-offer-3b9d"


def _waitlist(store: ProjectStore) -> None:
    stories = [
        Story("Someone can join the waitlist for a full class", done=True, checks=[
            Check("When all eight wheels are booked, the button says Join waitlist", "pass"),
            Check("After joining, they see their place in line", "pass")]),
        Story("When a spot opens, the first person waiting is offered it", checks=[
            Check("Cancelling a booking sends an offer to whoever is first"),
            Check("If they don’t claim it within 2 hours, the offer moves to the next person")]),
        Story("Maya can see who’s waiting", done=True, checks=[
            Check("Each class in Maya’s view lists its waitlist in order", "pass")]),
    ]
    _goal(store, Goal(id=WAITLIST, title="Add a waitlist", status=GoalStatus.NEEDS_YOU, status_note="one decision",
                      owner="alex", connection="ari", created=_at(TUESDAY, "16:10"), updated=_at(TODAY, "09:40"),
                      size="medium", cost_estimate="About $0.60 of Ari credits",
                      outcome="When a class is full, people can join a waitlist, and the first person waiting is "
                              "offered the spot when someone cancels.",
                      stories=stories,
                      notes="Glaze night fills up every week, so it’s the first class people will wait for."))
    gid = WAITLIST
    _person(store, gid, TUESDAY, "16:10", "alex", "Glaze night fills up every week. When a class is full, let people "
                                                   "join a waitlist, and offer them the spot if someone cancels.")
    _say(store, gid, TUESDAY, "16:11", "message", text="Good idea. Here’s how I’d build it. Have a look, and say go when it’s right.",
         where="this-computer", duration_s=5.2)
    _say(store, gid, TUESDAY, "16:11", "plan", title="Add a waitlist",
         stories=[{"title": s.title, "checks": [c.text for c in s.checks]} for s in stories],
         size="medium", size_text="Medium · about 40 minutes of work",
         estimate={"connection": "ari", "amount_text": "About $0.60 of Ari credits"},
         agreed={"by": "alex", "at": _at(TUESDAY, "16:12")})
    _person(store, gid, TUESDAY, "16:12", "alex", "Go.")
    _say(store, gid, TUESDAY, "16:24", "step", title="Added the waitlist and the Join waitlist button", state="done",
         files=["src/index.html", "src/kiln.js", "src/waitlist.js", "src/styles.css", "src/maya.html", "server/schema.sql"],
         elapsed_s=720, spend_text="$0.31",
         tool_calls=[{"title": "Read how classes are shown", "kind": "read", "status": "completed"},
                     {"title": "Added the waitlist to the class cards", "kind": "edit", "status": "completed"},
                     {"title": "Added the waiting list to Maya’s view", "kind": "edit", "status": "completed"},
                     {"title": "Ran the waitlist checks on Stage", "kind": "execute", "status": "completed"}])
    _say(store, gid, TODAY, "09:40", "message", text="The waitlist works on Stage, and people can join it. Before I "
                                                      "build the offer, there’s one thing only you can decide.",
         where="this-computer", duration_s=1.9)
    options = [
        Option("text", "A text message", "Most people see it within minutes. About 1¢ a text, through the same service as the reminders."),
        Option("email", "An email", "Free, but slower. Many people check email once or twice a day, so the 2 hours could run out before they see it."),
        Option("both", "A text, then an email if the text fails", "The most reliable, and it costs the same as a text. Once Priya’s fix for failed texts lands, even fewer will need the email."),
    ]
    default = "If you’d rather not choose, say “you pick” and I’ll go with a text, then an email."
    question = "How should someone hear that a spot opened?"
    _say(store, gid, TODAY, "09:40", "decision", id=WAITLIST_DECISION, question=question,
         options=[o.to_dict() for o in options], recommended="both", default=default)
    store.write_pending(PendingDecision(id=WAITLIST_DECISION, question=question, options=options, goal=gid,
                                        recommended="both", default=default, asked_by="ari", asked_at=_at(TODAY, "09:40")))


TEXTS = "fix-reminder-texts-2c41"


def _texts(store: ProjectStore) -> None:
    stories = [
        Story("Every reminder reaches a real phone", checks=[
            Check("A number typed as 510 555 0142 is saved as +1 510 555 0142"),
            Check("Numbers already saved without a country code are corrected")]),
        Story("Maya hears when a text still fails", checks=[
            Check("A failed reminder shows on the class page in Maya’s view")]),
    ]
    _goal(store, Goal(id=TEXTS, title="Fix reminder texts", status=GoalStatus.WORKING, owner="priya",
                      connection="priya-claude", created=_at(TODAY, "09:20"), updated=_at(TODAY, "09:31"),
                      size="small", cost_estimate="On Priya’s plan, about 3% of her week",
                      outcome="Every booking reminder arrives, and Maya hears about any that don’t.",
                      stories=stories))
    gid = TEXTS
    _person(store, gid, TODAY, "09:20", "priya", "Two people said they never got a reminder for Tuesday’s class. "
                                                  "Can you find out why and fix it?")
    _say(store, gid, TODAY, "09:22", "message", by="priya-claude",
         text="Found it, and it’s bug #13. Both numbers were saved without a country code, so Twilio turned them "
              "away.\n\n"
              "I’ll fix how numbers are saved, correct the ones already in Kiln, and resend the two reminders.",
         where="priya-computer", duration_s=6.8)
    _say(store, gid, TODAY, "09:22", "plan", by="priya-claude", title="Fix reminder texts",
         stories=[{"title": s.title, "checks": [c.text for c in s.checks]} for s in stories],
         size="small", size_text="Small · about 20 minutes of work",
         estimate={"connection": "priya-claude", "amount_text": "On Priya’s plan · about 3% of her week"},
         agreed={"by": "priya", "at": _at(TODAY, "09:23")})
    _say(store, gid, TODAY, "09:24", "step", by="priya-claude", title="Fixing how phone numbers are saved",
         state="running", files=["server/phone.js", "tests/phone.test.js"], elapsed_s=420, spend_text="2% of Priya’s week",
         tool_calls=[{"title": "Found 14 numbers without a country code", "kind": "search", "status": "completed"},
                     {"title": "Fixing how new numbers are saved", "kind": "edit", "status": "in_progress"},
                     {"title": "Correcting the 14 saved numbers", "kind": "edit", "status": "pending"},
                     {"title": "Resending the two missed reminders", "kind": "execute", "status": "pending"},
                     {"title": "Running the checks", "kind": "execute", "status": "pending"}])


PHOTOS = "studio-photos-on-the-home-page-9e12"


def _photos(store: ProjectStore) -> None:
    checks = [
        Check("The home page shows six studio photos in a row you can swipe", "pass", ".playground/proof/studio-photos/home-phone.svg"),
        Check("The Book buttons are still on the first screen of a phone", "pass", ".playground/proof/studio-photos/home-phone.svg"),
        Check("Each photo has a description for people using a screen reader", "pass"),
        Check("The page loads in under a second on a slow phone connection", "pass"),
    ]
    _goal(store, Goal(id=PHOTOS, title="Studio photos on the home page", status=GoalStatus.READY,
                      status_note="Sam approved", owner="alex", connection="ari", created=_at(WEDNESDAY, "11:30"),
                      updated=_at(WEDNESDAY, "14:05"), size="small", cost_estimate="About $0.10 of Ari credits",
                      outcome="People can see the studio before they book.",
                      stories=[Story("People can see the studio before they book", done=True, checks=checks)]))
    gid = PHOTOS
    _person(store, gid, WEDNESDAY, "11:30", "alex", "Maya sent six photos of the studio. Put them on the home page so "
                                                     "people can see the space before they book.")
    _say(store, gid, WEDNESDAY, "11:31", "message", text="Lovely photos. I’ll put them in a row under the welcome "
                                                          "that you can swipe, sized so the page stays quick on phones.",
         where="this-computer", duration_s=3.0)
    _say(store, gid, WEDNESDAY, "11:36", "step", title="Added a row of studio photos to the home page", state="done",
         files=["src/index.html", "src/styles.css", *(f"src/studio/{i}.svg" for i in range(1, 7))],
         elapsed_s=300, spend_text="$0.09")
    _say(store, gid, WEDNESDAY, "11:37", "headsup",
         text="I made the photo files smaller, from 2.1 MB to 340 KB in total, so the home page loads quickly on a "
              "phone. They look the same.", files=[f"src/studio/{i}.svg" for i in range(1, 7)],
         undo={"kind": "git", "ref": "HEAD"}, kept=True)
    shot = ".playground/proof/studio-photos/home-phone.svg"
    _say(store, gid, WEDNESDAY, "11:41", "proof", shots=[shot], checks=[c.to_dict() for c in checks],
         checked_by="checker")
    _person(store, gid, WEDNESDAY, "14:05", "sam", "Looks great on my phone. Approved from me.")
    _say(store, gid, WEDNESDAY, "14:05", "review", role="system", by="playground", title="Studio photos on the home page",
         changes_for_people=["The home page shows six photos of the studio, in a row you can swipe.",
                             "The Book buttons stay on the first screen."],
         data_changes=["No changes to data.", "Adds six photos, 340 KB in total."],
         checks={"passed": 4, "total": 4}, destination="production", ready=True, approved_by=["sam"])
    _write_shot(store.root / shot, photos=True)


SIGNIN = "sign-in-with-luma-connect-5a70"


def _signin(store: ProjectStore) -> None:
    checks = [
        Check("Signing in takes one tap with Luma Connect, with no password", "pass", ".playground/proof/sign-in/account-phone.svg"),
        Check("Someone who booked before sees their bookings after signing in", "pass"),
        Check("Signing out leaves bookings where they are", "pass"),
    ]
    _goal(store, Goal(id=SIGNIN, title="Sign in with Luma Connect", status=GoalStatus.LIVE, status_note=f"since {_day_name(TUESDAY)}",
                      owner="alex", connection="claude", created=_at(SIGNIN_STARTED, "10:15"), updated=_at(TUESDAY, "17:40"),
                      size="medium", cost_estimate="About 4% of Alex’s Claude week",
                      outcome="People sign in with one tap instead of making up a password.",
                      stories=[Story("People sign in with Luma Connect", done=True, checks=checks[:1]),
                               Story("Old bookings follow people when they sign in", done=True, checks=checks[1:])]))
    gid = SIGNIN
    _person(store, gid, SIGNIN_STARTED, "10:15", "alex", "Let people sign in with Luma Connect instead of making up a password.")
    _say(store, gid, SIGNIN_STARTED, "10:16", "handoff", role="system", by="playground", from_connection="ari",
         to_connection="claude",
         brief="Kiln is a phone-first booking page for Maya’s pottery studio. Alex wants people to sign in with "
               "Luma Connect instead of passwords. 212 bookings were made before sign-in existed; they are matched to "
               "people by phone number. The plan, decisions and history are in .playground/.")
    _say(store, gid, SIGNIN_STARTED, "10:17", "message", by="claude",
         text="Good call. Nobody has to remember another password, and Kiln never has to keep one.",
         where="this-computer", duration_s=4.4)
    options = [Option("match", "Yes, match them by phone number",
                      "When someone signs in with the same number, their bookings appear. Nothing to do for them."),
               Option("fresh", "No, start everyone fresh", "Simpler, but 212 bookings would look like they vanished.")]
    _say(store, gid, SIGNIN_STARTED, "10:18", "decision", by="claude", id="old-bookings-6c2e",
         question="People who booked before sign-in existed: should their bookings follow them?",
         options=[o.to_dict() for o in options], recommended="match", default="")
    atomic_write_text(store.paths.answer("old-bookings-6c2e"), json.dumps(Answer(
        id="old-bookings-6c2e", option="match", label="Yes, match them by phone number", answered_by="alex",
        answered_at=_at(SIGNIN_STARTED, "10:24")).to_dict(), ensure_ascii=False, indent=2) + "\n")
    _say(store, gid, SIGNIN_STARTED, "10:44", "step", by="claude", title="Added Sign in with Luma Connect", state="done",
         files=["src/signin.html", "src/kiln.js", "server/auth.js", "server/schema.sql"], elapsed_s=1080,
         spend_text="4% of your Claude week")
    shot = ".playground/proof/sign-in/account-phone.svg"
    _say(store, gid, TUESDAY, "17:21", "proof", by="claude", shots=[shot], checks=[c.to_dict() for c in checks],
         checked_by="checker")
    _say(store, gid, TUESDAY, "17:22", "review", role="system", by="playground", title="Sign in with Luma Connect",
         changes_for_people=["People sign in with one tap instead of making up a password."],
         data_changes=["Adds an accounts table and links 212 bookings to it by phone number."],
         checks={"passed": 3, "total": 3}, destination="production", ready=True,
         released={"at": _at(TUESDAY, "17:40"), "by": "alex", "restore_point": 14})
    _say(store, gid, TUESDAY, "17:40", "message", role="system", by="playground",
         text=f"Released to Production on {_day_name(TUESDAY)} at 5:40 PM. Restore point 14.", released=True, restore_point=14)
    _say(store, gid, TODAY, "08:00", "message", by="claude",
         text=f"21 people have signed in with Luma Connect since {_day_name(TUESDAY)}, and nobody has asked for a password reset.",
         where="this-computer", duration_s=1.2)
    _write_shot(store.root / shot, photos=False, signed_in=True)


GIFTS = "gift-a-class-b3d8"


def _gifts(store: ProjectStore) -> None:
    stories = [
        Story("Someone can buy a class as a gift", checks=[
            Check("Each class has a Give as a gift button"),
            Check("The buyer pays and gets a gift code to pass on")]),
        Story("The person who receives it can book with the code", checks=[
            Check("Entering the code at booking makes the class free")]),
    ]
    _goal(store, Goal(id=GIFTS, title="Gift a class", status=GoalStatus.PAUSED, status_note="after the holidays",
                      owner="alex", connection="ari", created=_at(GIFTS_STARTED, "15:42"), updated=_at(GIFTS_PAUSED, "11:00"),
                      size="large", cost_estimate="About $2.40 of Ari credits, plus card fees on each gift",
                      outcome="People can buy a class for someone else.", stories=stories))
    gid = GIFTS
    _person(store, gid, GIFTS_STARTED, "15:42", "alex", "Let people buy a class as a gift for someone else.")
    _say(store, gid, GIFTS_STARTED, "15:43", "message", text="Nice idea for the holidays. Here’s a first plan. It takes "
                                                      "payments, so I’ve kept it as small as it can be.",
         where="this-computer", duration_s=6.1)
    _say(store, gid, GIFTS_STARTED, "15:43", "plan", title="Gift a class",
         stories=[{"title": s.title, "checks": [c.text for c in s.checks]} for s in stories],
         size="large", size_text="Large · about 2 hours of work",
         estimate={"connection": "ari", "amount_text": "About $2.40 of Ari credits, plus card fees on each gift"})
    _say(store, gid, GIFTS_PAUSED, "11:00", "message", role="system", by="playground",
         text=f"You paused this on {_day_name(GIFTS_PAUSED)}: “after the holiday rush”", paused=True)


# ── Stack, bugs, usage and analytics (the Project space) ──

SERVICES = [
    Service(id="resend", name="Resend", kind="email", provider="Resend",
            purpose="Sends booking confirmations and reminders",
            used_by=["Booking a class", "Reminders", "Waitlist offers (backup email)"],
            environments={"stage": "Test mode: sends only to the team\u2019s own inboxes", "live": "From hello@kiln.example"},
            console_url="https://resend.com/emails", docs_url="https://resend.com/docs",
            plan="Free (3,000 emails a month)", monthly_budget=0, secrets=["Email service key"], usage_source="resend",
            added_by="ari"),
    Service(id="postgres", name="PostgreSQL", kind="database", provider="AWS Lightsail managed database",
            purpose="Keeps the classes, the bookings and the waitlist",
            used_by=["Booking a class", "Joining a waitlist", "Maya\u2019s view"],
            environments={"stage": "Its own copy on the Dev server, refreshed nightly with names changed",
                          "live": "kiln-db, a Lightsail managed database"},
            console_url="https://lightsail.aws.amazon.com/ls/webapp/home/databases",
            docs_url="https://www.postgresql.org/docs/16/", plan="Lightsail micro database ($15 a month)",
            monthly_budget=15, secrets=["Production database password"], usage_source="postgres", added_by="ari"),
    Service(id="r2", name="Cloudflare R2", kind="storage", provider="Cloudflare",
            purpose="Holds the studio photos, and the nightly database backups",
            used_by=["The home page photos", "Backups"],
            environments={"stage": "The kiln-stage bucket", "live": "The kiln-photos bucket, served from photos.kiln.example"},
            console_url="https://dash.cloudflare.com/?to=/:account/r2/overview",
            docs_url="https://developers.cloudflare.com/r2/", plan="Free tier (10 GB stored)", monthly_budget=1,
            secrets=["Photo storage key"], usage_source="cloudflare", added_by="ari"),
    Service(id="lightsail", name="AWS Lightsail", kind="hosting", provider="AWS",
            purpose="Runs the live app (Production)", used_by=["Everything people see at kiln.example"],
            environments={"stage": "Not used: Stage runs on the Dev server", "live": "kiln.example, a small instance in Oregon"},
            console_url="https://lightsail.aws.amazon.com/ls/webapp/home/instances",
            docs_url="https://docs.aws.amazon.com/lightsail/", plan="$5 a month instance (1 GB memory)",
            monthly_budget=6, secrets=["AWS billing read-only key"], usage_source="aws", added_by="alex"),
    Service(id="luma-connect", name="Luma Connect", kind="auth", provider="Luma",
            purpose="Lets people sign in with one tap, without a password", used_by=["Signing in", "My bookings"],
            environments={"stage": "The Kiln Stage app, signs in test accounts only", "live": "The Kiln app"},
            console_url="https://hub.simplyluma.com/connect/apps", docs_url="https://simplyluma.com/developer",
            plan="Free", monthly_budget=0, secrets=["Luma Connect app secret"], usage_source="manual", added_by="claude"),
    Service(id="twilio", name="Twilio", kind="messaging", provider="Twilio",
            purpose="Sends the booking texts and the reminder the day before",
            used_by=["Booking a class", "Reminders", "Waitlist offers"],
            environments={"stage": "Test credentials: texts go only to the team\u2019s phones", "live": "Sends from +1 510 555 0100"},
            console_url="https://console.twilio.com/", docs_url="https://www.twilio.com/docs/messaging",
            plan="Pay as you go, about 1\u00a2 a text", monthly_budget=5, secrets=["Texting service key"],
            usage_source="manual", added_by="priya"),
]
SERVICE_ADDED = {"resend": CREATED, "postgres": CREATED, "r2": TEAM_SETUP, "lightsail": FIRST_RELEASE + 1,
                 "luma-connect": SIGNIN_STARTED, "twilio": TEAM_SETUP}
DEPENDENCY_NOTES = {
    "pg": "Talks to the PostgreSQL database",
    "resend": "Sends the confirmation and reminder emails",
    "twilio": "Sends the texts",
    "@aws-sdk/client-s3": "Puts photos and backups in Cloudflare R2, which speaks the same language as S3",
}


def _stack(store: ProjectStore) -> None:
    services = []
    for service in SERVICES:
        copy = Service.from_dict(service.to_dict())
        copy.added = _at(SERVICE_ADDED[copy.id], "12:00")
        services.append(copy)
    store.write_stack(Stack(services=services))
    store.refresh_dependencies(DEPENDENCY_NOTES)


def _issues(store: ProjectStore) -> None:
    bugs = [
        Issue(number=9, title="The booking form took an empty phone number", severity="low", status="fixed",
              reporter="sam", place="stage", created=_at(GIFTS_STARTED, "14:10"), updated=_at(GIFTS_STARTED, "15:02"),
              what_happened="I booked Wheel basics with the phone field left empty, and it went through.",
              expected="It should ask for a number, because that\u2019s how the reminder reaches you.",
              steps="1. Open Kiln on a phone\n2. Tap Book on any class\n3. Clear the phone field and tap Book my wheel",
              evidence="The booking showed in Maya\u2019s view with no number."),
        Issue(number=10, title="Glaze night showed 9 of 8 wheels booked", severity="high", status="fixed",
              reporter="alex", place="live", created=_at(FIRST_RELEASE - 3, "19:40"), updated=_at(FIRST_RELEASE - 4, "10:15"),
              what_happened="Maya saw nine people booked into Glaze night. Two people tapped Book at the same moment "
                            "for the last wheel.",
              expected="The second person is told it just filled up, and is offered the waitlist.",
              steps="1. Two phones on the same full-but-one class\n2. Tap Book my wheel on both at once",
              evidence="Two bookings for the same class 40 ms apart."),
        Issue(number=11, title="The class list jumped when the photos loaded", severity="medium", status="fixed",
              reporter="ari", place="stage", goal=PHOTOS, created=_at(WEDNESDAY, "11:39"), updated=_at(WEDNESDAY, "11:40"),
              what_happened="On a slow connection the class cards moved down once the photos arrived, so a tap "
                            "could land on the wrong Book button.",
              expected="The page keeps its place while the photos load.",
              steps="1. Open the home page with a slow phone connection\n2. Try to tap Book as it loads",
              evidence="Found by the checker while proving Studio photos. Fixed by giving each photo its size up front."),
        Issue(number=12, title="Maya\u2019s view is hard to read in bright sunlight", severity="low", status="open",
              reporter="alex", place="live", created=_at(4, "13:20"), updated=_at(4, "13:20"),
              what_happened="Maya says the grey names in her view wash out when she checks it in the yard.",
              expected="Names stay readable outside.",
              steps="1. Open Maya\u2019s view on a phone\n2. Take it outside on a sunny day",
              evidence="Maya, by text to Alex."),
        Issue(number=13, title="Two reminder texts failed", severity="high", status="fixing", reporter="priya",
              place="live", goal=TEXTS, created=_at(TODAY, "09:15"), updated=_at(TODAY, "09:24"),
              what_happened="Two people booked for Tuesday\u2019s Wheel basics never got the reminder the day before.",
              expected="Everyone with a booking gets a text the day before their class.",
              steps="1. Book with a number typed without the country code, like 510 555 0142\n"
                    "2. Wait for the reminder the evening before",
              evidence="Twilio\u2019s log shows error 21211 (the number isn\u2019t valid) for both. "
                       "14 saved numbers have no country code."),
    ]
    for issue in bugs:
        store.write_issue(issue, touch=False)


def _usage_and_analytics(store: ProjectStore) -> None:
    """A day-by-day record since the first release, ending yesterday. Numbers only."""
    rng = random.Random(73)
    db_mb, storage_gb = 11.8, 0.04
    sign_ins = {TUESDAY: 6, WEDNESDAY: 15}  # 21 since Tuesday, as Claude says this morning
    usage_lines, analytics_lines = [], []
    for days in range(FIRST_RELEASE, 0, -1):
        day = (datetime.now().date() - timedelta(days=days)).isoformat()
        stamp = _at(days, "23:55")
        weekend = (datetime.now().date() - timedelta(days=days)).weekday() >= 5
        bookings = rng.randint(4, 9) + (3 if weekend else 0)
        photos = days <= WEDNESDAY

        def snapshot(service_id: str, metrics: dict, cost: float, source: str) -> None:
            usage_lines.append(ServiceUsage(service_id=service_id, period=day, metrics=metrics, cost=round(cost, 2),
                                            source=source, ts=stamp).to_dict())

        emails = bookings * 2 + rng.randint(0, 3)
        snapshot("resend", {"emails_sent": emails, "delivered": emails - (1 if rng.random() < 0.15 else 0),
                            "bounced": 1 if rng.random() < 0.15 else 0}, 0.0, "resend")
        texts = bookings + rng.randint(2, 5)
        failed = 2 if days == MONDAY else 0
        snapshot("twilio", {"texts_sent": texts, "texts_failed": failed}, texts * 0.0079, "manual")
        db_mb += 0.25 + bookings * 0.02
        snapshot("postgres", {"database_mb": round(db_mb, 1), "connections_peak": rng.randint(3, 9)}, 15 / 30, "postgres")
        storage_gb += 0.011
        snapshot("r2", {"storage_gb": round(storage_gb, 3), "class_a_ops": rng.randint(20, 40),
                        "class_b_ops": rng.randint(150, 400) + (900 if photos else 0)}, 0.0, "cloudflare")
        snapshot("lightsail", {"cpu_percent_avg": rng.randint(3, 11), "data_transfer_gb": round(0.04 + bookings * 0.01, 2)},
                 5 / 30, "aws")
        if days in sign_ins:
            snapshot("luma-connect", {"sign_ins": sign_ins[days]}, 0.0, "manual")

        visitors = bookings * 3 + rng.randint(2, 9)
        pages = [{"path": "/", "views": visitors + rng.randint(5, 15)},
                 {"path": "/maya.html", "views": rng.randint(3, 8)}]
        if days <= TUESDAY:
            pages.insert(1, {"path": "/signin.html", "views": sign_ins.get(days, 0) + rng.randint(1, 4)})
        analytics_lines.append(AnalyticsSnapshot(period=day, visitors=visitors, sessions=int(visitors * 1.3),
                                                 top_pages=pages, errors=1 if days == FIRST_RELEASE - 3 else 0,
                                                 source="lightsail-logs", ts=stamp).to_dict())
    atomic_write_text(store.paths.service_usage, "".join(json.dumps(l, ensure_ascii=False) + "\n" for l in usage_lines))
    atomic_write_text(store.paths.analytics, "".join(json.dumps(l, ensure_ascii=False) + "\n" for l in analytics_lines))


def _write_activity(store: ProjectStore) -> None:
    """A fortnight of history, oldest first (activity.jsonl is append-only after this)."""
    rows = [
        (CREATED, "15:06", "ari", "created", "", "Ari created Kiln", {"detail": "~/Documents/Playground Projects/Kiln"}),
        (CREATED, "15:20", "alex", "decision", "", "Classes cap at eight", {"detail": "Maya, via Alex"}),
        (CREATED, "15:10", "ari", "stack", "", "Added Resend and PostgreSQL to the stack", {}),
        (TEAM_SETUP, "10:05", "priya", "decision", "", "Stage gets its own copy of the bookings", {"detail": "Ari suggested, Priya agreed"}),
        (TEAM_SETUP, "11:30", "priya", "stack", "", "Added Twilio to the stack", {"detail": "For the booking texts"}),
        (GIFTS_STARTED, "14:10", "sam", "issue", "", "Filed bug #9: The booking form took an empty phone number", {"number": 9}),
        (GIFTS_STARTED, "15:43", "alex", "goal", GIFTS, "Started Gift a class", {}),
        (FIRST_RELEASE, "17:10", "alex", "release", "", "Released the first version of Kiln", {"detail": "Restore point 1", "restore_point": 1}),
        (FIRST_RELEASE - 3, "19:40", "alex", "issue", "", "Filed bug #10: Glaze night showed 9 of 8 wheels booked", {"number": 10}),
        (FIRST_RELEASE - 4, "10:15", "ari", "issue", "", "Bug #10 is now fixed", {"number": 10}),
        (SIGNIN_STARTED, "10:15", "alex", "goal", SIGNIN, "Started Sign in with Luma Connect", {}),
        (SIGNIN_STARTED, "10:16", "alex", "handoff", SIGNIN, "Handed Sign in with Luma Connect to Claude", {"from": "ari", "to": "claude"}),
        (SIGNIN_STARTED, "10:24", "alex", "decision", SIGNIN, "You chose to match old bookings by phone number", {"detail": "For sign-in", "decision": "old-bookings-6c2e"}),
        (GIFTS_PAUSED, "11:00", "alex", "goal", GIFTS, "Paused Gift a class", {"detail": "after the holiday rush"}),
        (ORDER_UNDONE, "16:30", "alex", "undo", "", "You undid a change to the class order", {"detail": "Ari had sorted classes by price"}),
        (MONDAY, "18:02", "alex", "release", "", "Released Booking reminders", {"detail": "Restore point 13", "restore_point": 13}),
        (TUESDAY, "16:12", "alex", "decision", WAITLIST, "You agreed the waitlist plan", {"detail": "Medium, about $0.60"}),
        (TUESDAY, "16:24", "ari", "step", WAITLIST, "Added the waitlist and the Join waitlist button", {"detail": "6 files, $0.31"}),
        (TUESDAY, "17:40", "alex", "release", SIGNIN, "Released Sign in with Luma Connect", {"detail": "Restore point 14", "restore_point": 14}),
        (WEDNESDAY, "11:37", "ari", "agent", PHOTOS, "Ari made the studio photos smaller", {"detail": "A heads-up you kept"}),
        (WEDNESDAY, "11:41", "checker", "proof", PHOTOS, "Checked Studio photos on the home page: 4 of 4 passed", {}),
        (WEDNESDAY, "14:05", "sam", "review", PHOTOS, "Sam approved Studio photos on the home page", {}),
        (TODAY, "09:15", "priya", "issue", TEXTS, "Filed bug #13: Two reminder texts failed", {"number": 13}),
        (TODAY, "09:20", "priya", "goal", TEXTS, "Started Fix reminder texts", {}),
        (TODAY, "09:24", "priya-claude", "agent", TEXTS, "Priya’s agent started on Fix reminder texts", {"detail": "On Priya’s Claude, on her computer"}),
        (TODAY, "09:40", "ari", "decision", WAITLIST, "Ari asked how waitlisted people should hear about a spot", {"detail": "Waiting for Alex", "decision": WAITLIST_DECISION}),
    ]
    rows.sort(key=lambda row: (-row[0], row[1]))
    lines = [json.dumps(ActivityEntry(summary=summary, kind=kind, actor=actor, goal=goal, ts=_at(days, hhmm),
                                      data=data).to_dict(), ensure_ascii=False)
             for days, hhmm, actor, kind, goal, summary, data in rows]
    atomic_write_text(store.paths.activity, "\n".join(lines) + "\n")


# ══ A project the moment after it was made ═════════════════════════════════════

def create_sample_empty(root: Path | str | None = None, *, name: str = "Sprig", git: bool = True, bus=None) -> Project:
    """A brand-new project: the brief and recipe agreed, nothing built yet."""
    brief = Brief(title=name, audience="You, at home.", platforms=["phones"])
    brief.set_section(BRIEF_SUMMARY, "A simple way to keep track of when each plant was last watered, and which ones "
                                     "need water today.")
    brief.set_section(BRIEF_GOOD, "- Opening it shows what needs water today.\n- Logging a watering is one tap.")
    recipe = Recipe(summary="A web app that works great on phones", runs_on="This computer, for now", parts=[
        RecipePart("Pages", "HTML, CSS and a little JavaScript", "Small and quick, and easy for any agent to work on."),
        RecipePart("Saving", "A small database file (SQLite)", "It lives inside the project folder, so your plants never leave this computer."),
        RecipePart("Sign-in", "None for now", "It’s just you. Luma Connect can be added in one goal if family joins."),
        RecipePart("Runs on", "This computer, for now", "Move it to a server when you want it on your phone away from home."),
    ])
    project = create_project(name, brief=brief, recipe=recipe, root=root if root is not None else projects_root(),
                             summary="A plant watering tracker", git=git, bus=bus)
    store = ProjectStore(project.path, bus=bus)
    owner = project.owner.id if project.owner else ""
    store.append_thread(GENERAL_CHAT_ID, ThreadEntry(kind="message", role="person", by=owner,
                                                     text="A tool to track my plant watering"))
    store.append_thread(GENERAL_CHAT_ID, ThreadEntry(kind="brief", by="ari", data={
        "title": brief.title, "audience": brief.audience, "platforms": brief.platforms,
        "summary": brief.summary, "good_looks_like": brief.good_looks_like}))
    store.append_thread(GENERAL_CHAT_ID, ThreadEntry(kind="recipe", by="ari", data={
        "summary": recipe.summary, "parts": [p.to_dict() for p in recipe.parts], "runs_on": recipe.runs_on}))
    return store.read_project()


# ══ Pictures: the studio photos (painted, as in v73) and the proof shots ═══════

_ART = [("#e3b08a", "#8c4a2b", "shelf"), ("#cdbb9f", "#5d4a3b", "wheel"), ("#e9c6a3", "#a0522d", "bowls"),
        ("#bcc6a8", "#55644a", "vase"), ("#ecd2b4", "#b5532f", "kiln"), ("#d9c5ab", "#6e5040", "mugs")]
PHOTO_DESCRIPTIONS = [
    "Shelves of finished pots and bowls in clay, cream and sage glazes",
    "A pot taking shape on the wheel between two hands",
    "Stacks of bowls drying on the bench",
    "Three vases of different heights on the windowsill",
    "The kiln itself, glowing as it fires",
    "A row of mugs waiting for their glaze",
]


def _pot(x: float, y: float, w: float, h: float, c: str) -> str:
    return (f'<g fill="{c}"><ellipse cx="{x}" cy="{y - h * .42:.1f}" rx="{w / 2}" ry="{h * .42:.1f}"/>'
            f'<rect x="{x - w * .2:.1f}" y="{y - h}" width="{w * .4:.1f}" height="{h * .34:.1f}" rx="2"/>'
            f'<ellipse cx="{x}" cy="{y - h}" rx="{w * .25:.1f}" ry="{h * .06:.1f}"/></g>')


def _bowl(x: float, y: float, w: float, c: str) -> str:
    return f'<path d="M{x - w / 2} {y - w * .3:.1f}Q{x} {y + w * .22:.1f} {x + w / 2} {y - w * .3:.1f}Z" fill="{c}"/>'


def _mug(x: float, y: float, c: str) -> str:
    return (f'<g><rect x="{x - 9}" y="{y - 22}" width="18" height="22" rx="4" fill="{c}"/>'
            f'<circle cx="{x + 11}" cy="{y - 11}" r="5" fill="none" stroke="{c}" stroke-width="3"/></g>')


def studio_art(index: int) -> str:
    """The inner drawing of studio photo ``index`` (0-5), in a 150 x 112 box."""
    bg, fg, kind = _ART[index % len(_ART)]
    art = {
        "shelf": (f'<rect x="0" y="50" width="150" height="4" fill="{fg}" opacity=".55"/>'
                  f'<rect x="0" y="94" width="150" height="4" fill="{fg}" opacity=".55"/>'
                  + _pot(26, 50, 20, 30, fg) + _pot(54, 50, 14, 22, "#f4e6d4") + _pot(80, 50, 22, 34, "#6f7d5c")
                  + _pot(110, 50, 16, 26, fg) + _bowl(34, 94, 30, "#f4e6d4") + _pot(72, 94, 18, 28, "#c4703f")
                  + _bowl(112, 94, 34, fg)),
        "wheel": (f'<ellipse cx="75" cy="88" rx="56" ry="12" fill="{fg}" opacity=".85"/>'
                  '<ellipse cx="75" cy="84" rx="38" ry="8" fill="#9a8877"/>'
                  '<path d="M58 84Q60 50 75 44Q90 50 92 84Z" fill="#c4865a"/>'
                  '<ellipse cx="75" cy="45" rx="9" ry="3" fill="#a96b44"/>'
                  '<path d="M28 70q10-16 26-10M122 70q-10-16-26-10" stroke="#f4e6d4" stroke-width="7" stroke-linecap="round" fill="none"/>'),
        "bowls": (_bowl(40, 70, 46, fg) + _bowl(40, 58, 40, "#f4e6d4") + _bowl(98, 74, 52, "#6f7d5c")
                  + _bowl(98, 61, 44, fg) + _bowl(98, 50, 36, "#f4e6d4")
                  + f'<rect x="0" y="80" width="150" height="32" fill="{fg}" opacity=".25"/>'),
        "vase": (f'<rect x="0" y="86" width="150" height="26" fill="{fg}" opacity=".35"/>'
                 + _pot(48, 88, 30, 58, fg) + _pot(92, 88, 22, 40, "#f4e6d4") + _pot(120, 88, 14, 24, "#b5532f")),
        "kiln": (f'<path d="M30 112V58a45 45 0 0 1 90 0v54Z" fill="{fg}"/>'
                 '<path d="M48 112V66a27 27 0 0 1 54 0v46Z" fill="#3a1d10"/>'
                 '<path d="M75 104c-10 0-16-7-16-15 0-10 9-15 12-25 6 5 10 9 10 16 3-2 5-6 5-10 5 5 7 11 7 17 0 10-7 17-18 17z" fill="#ffb347"/>'
                 '<path d="M75 104c-5 0-8-3.5-8-7.5 0-5 4.5-7.5 6-12.5 3 2.5 5 4.5 5 8 1.5-1 2.5-3 2.5-5 2.5 2.5 3.5 5.5 3.5 8.5 0 5-3.5 8.5-9 8.5z" fill="#ffe08a"/>'),
        "mugs": (f'<rect x="0" y="70" width="150" height="4" fill="{fg}" opacity=".5"/>'
                 + _mug(24, 70, fg) + _mug(58, 70, "#f4e6d4") + _mug(92, 70, "#6f7d5c") + _mug(126, 70, "#c4703f")
                 + f'<rect x="0" y="74" width="150" height="38" fill="{fg}" opacity=".18"/>'),
    }[kind]
    return (f'<defs><linearGradient id="kg{index}" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0" stop-color="#fff" stop-opacity=".1"/><stop offset="1" stop-color="#3a1d10" stop-opacity=".18"/>'
            f'</linearGradient></defs><rect width="150" height="112" fill="{bg}"/>'
            '<rect x="96" y="0" width="54" height="112" fill="#fff" opacity=".16"/>'
            f'<rect x="0" y="0" width="150" height="112" fill="url(#kg{index})"/>{art}')


def studio_photo_svg(index: int) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 150 112" preserveAspectRatio="xMidYMid slice" '
            f'role="img" aria-label="{PHOTO_DESCRIPTIONS[index]}"><title>{PHOTO_DESCRIPTIONS[index]}</title>'
            f'{studio_art(index)}</svg>\n')


KILN_MARK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="9" fill="#b5532f"/>'
    '<path d="M8.5 25V15.5a7.5 7.5 0 0 1 15 0V25" fill="none" stroke="#fff8ee" stroke-width="2.3"/>'
    '<path d="M6.5 25h19" stroke="#fff8ee" stroke-width="2.3" stroke-linecap="round"/>'
    '<path d="M16 23.2c-1.7 0-2.8-1.2-2.8-2.7 0-1.7 1.5-2.6 2-4.3 1 .9 1.7 1.6 1.7 2.8.4-.4.8-1 .9-1.7.8.9 1.1 1.8 1.1 '
    '2.8 0 1.8-1.2 3.1-2.9 3.1z" fill="#ffc06a"/></svg>\n')


def _write_shot(path: Path, *, photos: bool, signed_in: bool = True) -> None:
    """A phone-sized picture of Kiln's home page, standing in for a Stage screenshot."""
    serif = "Newsreader, Georgia, serif"
    sans = "Atkinson Hyperlegible, system-ui, sans-serif"
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 390 844" width="390" height="844">',
             '<rect width="390" height="844" fill="#f7f1e8"/>',
             f'<text x="34" y="40" font-family="{sans}" font-weight="700" font-size="15" fill="#2b211a">9:41</text>',
             f'<svg x="20" y="62" width="30" height="30" viewBox="0 0 32 32">{KILN_MARK_SVG.split(">", 1)[1].rsplit("</svg>", 1)[0]}</svg>',
             f'<text x="58" y="84" font-family="{serif}" font-weight="600" font-size="21" fill="#2b211a">Kiln</text>']
    if signed_in:
        parts.append('<circle cx="353" cy="77" r="17" fill="#b5532f"/>'
                     f'<text x="353" y="82" text-anchor="middle" font-family="{sans}" font-weight="700" font-size="13" fill="#fff">N</text>')
    parts.append(f'<text x="20" y="128" font-family="{sans}" font-weight="700" font-size="12" letter-spacing="1.2" fill="#b5532f">MAYA’S POTTERY STUDIO</text>'
                 f'<text font-family="{serif}" font-size="32" fill="#2b211a"><tspan x="20" y="166">Find your wheel this</tspan><tspan x="20" y="200">week</tspan></text>'
                 f'<text x="20" y="228" font-family="{sans}" font-size="15" fill="#6b5a4b">Small classes, eight wheels, every level welcome.</text>')
    y = 250
    if photos:
        for i in range(3):
            parts.append(f'<svg x="{20 + i * 158}" y="{y}" width="150" height="112" viewBox="0 0 150 112" '
                         f'preserveAspectRatio="xMidYMid slice">{studio_art(i).replace("kg", f"shot{i}kg")}</svg>')
        y += 126
    parts.append(f'<text x="20" y="{y + 30}" font-family="{serif}" font-size="22" fill="#2b211a">This week</text>')
    for title, day, num, when, spots, full in (("Wheel basics", "TUE", "13", "6:30 PM · with Maya", "3 of 8 wheels left", False),
                                               ("Glaze night", "THU", "15", "7:00 PM · with Jo", "Full", True),
                                               ("Open studio", "SAT", "17", "10:00 AM · drop in", "5 of 8 wheels left", False)):
        top = y + 48
        parts.append(f'<rect x="16" y="{top}" width="358" height="112" rx="18" fill="#fffaf3" stroke="#e4d8c7"/>'
                     f'<rect x="28" y="{top + 12}" width="52" height="56" rx="12" fill="#efe5d6"/>'
                     f'<text x="54" y="{top + 33}" text-anchor="middle" font-family="{sans}" font-weight="700" font-size="10.5" fill="#b5532f">{day}</text>'
                     f'<text x="54" y="{top + 58}" text-anchor="middle" font-family="{serif}" font-size="23" fill="#2b211a">{num}</text>'
                     f'<text x="92" y="{top + 38}" font-family="{serif}" font-size="18" fill="#2b211a">{title}</text>'
                     f'<text x="92" y="{top + 58}" font-family="{sans}" font-size="13" fill="#6b5a4b">{when}</text>'
                     f'<text x="28" y="{top + 94}" font-family="{sans}" font-weight="700" font-size="12.5" fill="{"#9c4224" if full else "#5f6f52"}">{spots}</text>')
        if full:
            parts.append(f'<rect x="246" y="{top + 70}" width="116" height="38" rx="19" fill="none" stroke="#b5532f" stroke-width="1.5"/>'
                         f'<text x="304" y="{top + 94}" text-anchor="middle" font-family="{sans}" font-weight="700" font-size="14" fill="#9c4224">Join waitlist</text>')
        else:
            parts.append(f'<rect x="292" y="{top + 70}" width="70" height="38" rx="19" fill="#2b211a"/>'
                         f'<text x="327" y="{top + 94}" text-anchor="middle" font-family="{sans}" font-weight="700" font-size="14" fill="#fff8ee">Book</text>')
        y += 122
    parts.append('<rect x="0" y="770" width="390" height="74" fill="#f7f1e8"/><rect x="0" y="770" width="390" height="1" fill="#e4d8c7"/>')
    for x, label, colour in ((70, "Classes", "#b5532f"), (195, "My bookings", "#9a8877"), (320, "Studio", "#9a8877")):
        parts.append(f'<text x="{x}" y="808" text-anchor="middle" font-family="{sans}" font-weight="700" font-size="11" fill="{colour}">{label}</text>')
    parts.append("</svg>\n")
    atomic_write_text(path, "".join(parts))


# ══ The app itself: what the Stage runs ════════════════════════════════════════

def _write_app(root: Path) -> None:
    files = {
        "src/index.html": INDEX_HTML,
        "src/styles.css": STYLES_CSS,
        "src/kiln.js": KILN_JS,
        "src/waitlist.js": WAITLIST_JS,
        "src/classes.json": json.dumps(CLASSES, ensure_ascii=False, indent=2) + "\n",
        "src/maya.html": MAYA_HTML,
        "src/signin.html": SIGNIN_HTML,
        "src/mark.svg": KILN_MARK_SVG,
        "server/schema.sql": SCHEMA_SQL,
        "server/phone.js": PHONE_JS,
        "server/auth.js": AUTH_JS,
        "tests/phone.test.js": PHONE_TEST_JS,
        "README.md": README_MD,
        "package.json": json.dumps(PACKAGE_JSON, indent=2) + "\n",
    }
    for index in range(6):
        files[f"src/studio/{index + 1}.svg"] = studio_photo_svg(index)
    for relative, text in files.items():
        atomic_write_text(root / relative, text)


# This week's classes. Stage has its own copy of the bookings, so these are test numbers.
CLASSES = [
    {"id": "wheel-basics", "title": "Wheel basics", "day": "tue", "time": "18:30", "with": "with Maya", "wheels": 8,
     "booked": 5, "waiting": 0, "price": 45},
    {"id": "glaze-night", "title": "Glaze night", "day": "thu", "time": "19:00", "with": "with Jo", "wheels": 8,
     "booked": 8, "waiting": 4, "price": 40},
    {"id": "open-studio", "title": "Open studio", "day": "sat", "time": "10:00", "with": "drop in", "wheels": 8,
     "booked": 3, "waiting": 0, "price": 25},
    {"id": "throwing-bowls", "title": "Throwing bowls", "day": "sun", "time": "14:00", "with": "with Maya", "wheels": 8,
     "booked": 2, "waiting": 0, "price": 50},
]

_PHOTO_IMGS = "\n".join(f'      <img src="studio/{i + 1}.svg" alt="{d}" width="150" height="112" loading="lazy">'
                        for i, d in enumerate(PHOTO_DESCRIPTIONS))

INDEX_HTML = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#f7f1e8">
  <title>Kiln · Maya’s pottery studio</title>
  <link rel="icon" href="mark.svg">
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header class="klhd">
    <img class="klmk" src="mark.svg" alt="" width="30" height="30">
    <b>Kiln</b>
    <nav class="klnav" aria-label="Kiln"><a class="on" href="./" aria-current="page">Classes</a><a href="#bookings">My bookings</a><a href="#studio">The studio</a></nav>
    <span class="klsp"></span>
    <a class="klme" href="signin.html" aria-label="Your account">N</a>
  </header>
  <main>
    <section class="klhero">
      <small>Maya’s pottery studio</small>
      <h1>Find your wheel this week</h1>
      <p>Small classes, eight wheels, every level welcome.</p>
    </section>
    <div class="klph" id="studio" aria-label="Photos of the studio">
{_PHOTO_IMGS}
    </div>
    <div class="klh2"><h2>This week</h2><span id="week"></span></div>
    <div class="klcls" id="classes" aria-live="polite"></div>
  </main>
  <nav class="kltabs" aria-label="Sections">
    <a class="on" href="./">Classes</a><a href="#bookings">My bookings</a><a href="#studio">Studio</a>
  </nav>
  <div class="klveil" id="veil" hidden></div>
  <section class="klsheet" id="sheet" role="dialog" aria-modal="true" aria-labelledby="sheet-title" hidden></section>
  <script src="waitlist.js"></script>
  <script src="kiln.js"></script>
</body>
</html>
"""

STYLES_CSS = """/* Kiln: Maya's pottery studio. Warm clay and cream; deliberately not Luma. */
:root {
  --kc: #f7f1e8; --kc2: #efe5d6; --kink: #2b211a; --kink2: #6b5a4b; --kink3: #9a8877;
  --kclay: #b5532f; --kclay2: #9c4224; --ksage: #5f6f52; --kline: #e4d8c7; --kcard: #fffaf3;
  --serif: Newsreader, Georgia, serif;
  --sans: "Atkinson Hyperlegible", system-ui, -apple-system, "Segoe UI", sans-serif;
  color-scheme: light;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--kc); color: var(--kink); font: 15px/1.45 var(--sans); }
body { min-height: 100vh; padding-bottom: 76px; }
a { color: inherit; }
button { font: inherit; color: inherit; border: 0; background: none; cursor: pointer; }
h1, h2, h3 { font-family: var(--serif); font-weight: 500; letter-spacing: -.01em; margin: 0; }

.klhd { display: flex; align-items: center; gap: 9px; padding: 14px 20px 6px; }
.klhd b { font: 600 21px var(--serif); letter-spacing: -.01em; }
.klsp { flex: 1; }
.klmk { width: 30px; height: 30px; flex: none; display: block; }
.klnav { display: none; }
.klme { width: 34px; height: 34px; border-radius: 50%; display: grid; place-items: center; background: var(--kclay);
  color: #fff; font-weight: 700; font-size: 13px; text-decoration: none; }

.klhero { padding: 14px 20px 6px; }
.klhero small { display: block; font-size: 12px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; color: var(--kclay); }
.klhero h1 { margin-top: 6px; font-size: 32px; line-height: 1.08; }
.klhero p { margin: 8px 0 0; color: var(--kink2); }

.klph { display: flex; gap: 8px; padding: 14px 20px 4px; overflow-x: auto; scroll-snap-type: x mandatory; scrollbar-width: none; }
.klph img { flex: none; width: 150px; height: 112px; border-radius: 14px; object-fit: cover; scroll-snap-align: start;
  box-shadow: 0 1px 2px rgba(60, 30, 10, .15); background: var(--kc2); }

.klh2 { display: flex; align-items: baseline; justify-content: space-between; padding: 18px 20px 8px; }
.klh2 h2 { font-size: 22px; }
.klh2 span { font-size: 13px; color: var(--kink3); }

.klcls { display: grid; gap: 10px; padding: 0 16px 18px; }
.klc { display: grid; grid-template-columns: 52px minmax(0, 1fr); gap: 4px 12px; padding: 12px; border-radius: 18px;
  background: var(--kcard); box-shadow: 0 0 0 1px var(--kline), 0 1px 2px rgba(60, 30, 10, .06); }
.kld { grid-row: 1 / span 2; width: 52px; height: 56px; border-radius: 12px; display: grid; align-content: center;
  justify-items: center; background: var(--kc2); line-height: 1.05; }
.kld small { font-size: 10.5px; font-weight: 700; letter-spacing: .08em; color: var(--kclay); }
.kld b { font: 500 23px var(--serif); }
.klc h3 { font-size: 18px; line-height: 1.2; align-self: end; }
.klc p { margin: 0; font-size: 13px; color: var(--kink2); }
.klact { grid-column: 1 / -1; display: flex; align-items: center; gap: 10px; margin-top: 8px; }
.klspots { font-size: 12.5px; font-weight: 700; color: var(--ksage); }
.klspots.few { color: #a0601a; }
.klspots.full { color: var(--kclay2); }
.klwait { font-size: 12px; color: var(--kink3); }
.klbtn { margin-left: auto; height: 38px; padding: 0 16px; border-radius: 19px; background: var(--kink); color: #fff8ee;
  font-size: 14px; font-weight: 700; display: inline-flex; align-items: center; }
.klbtn.alt { background: transparent; color: var(--kclay2); box-shadow: inset 0 0 0 1.5px var(--kclay); }
.klbtn.done { background: #e6ead9; color: var(--ksage); cursor: default; }
.klbtn:focus-visible, .klbig:focus-visible { outline: 3px solid var(--kclay); outline-offset: 2px; }

.kltabs { position: fixed; left: 0; right: 0; bottom: 0; display: flex; justify-content: space-around;
  padding: 12px 10px calc(14px + env(safe-area-inset-bottom)); background: rgba(247, 241, 232, .94);
  box-shadow: 0 -1px 0 var(--kline); backdrop-filter: blur(8px); }
.kltabs a { font-size: 12px; font-weight: 700; color: var(--kink3); text-decoration: none; }
.kltabs a.on { color: var(--kclay); }

.klveil { position: fixed; inset: 0; background: rgba(43, 33, 26, .35); z-index: 3; }
.klsheet { position: fixed; left: 0; right: 0; bottom: 0; z-index: 4; max-width: 520px; margin: 0 auto;
  padding: 10px 20px calc(30px + env(safe-area-inset-bottom)); border-radius: 26px 26px 0 0; background: var(--kcard);
  box-shadow: 0 -10px 40px rgba(60, 30, 10, .25); animation: klup .35s cubic-bezier(.3, 1.1, .4, 1) both; }
.klsheet::before { content: ""; display: block; width: 38px; height: 5px; margin: 0 auto 14px; border-radius: 3px; background: var(--kline); }
.klsheet h2 { font-size: 24px; }
.klsheet > p { margin: 4px 0 14px; color: var(--kink2); font-size: 14px; }
.klsheet.done { text-align: center; }
.klpos { display: block; font: 500 54px/1 var(--serif); color: var(--kclay); margin: 8px 0; }
@keyframes klup { from { transform: translateY(40%); opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .klsheet { animation: none; } }

.klfield { display: grid; gap: 4px; margin-bottom: 10px; }
.klfield span { font-size: 12px; font-weight: 700; color: var(--kink3); }
.klfield input { height: 44px; padding: 0 14px; border-radius: 12px; border: 0; background: var(--kc);
  box-shadow: inset 0 0 0 1px var(--kline); font: inherit; color: inherit; }
.klfield input:focus { outline: 2px solid var(--kclay); }
.kltog { display: flex; align-items: center; justify-content: space-between; margin: 6px 0 14px; font-size: 14px; }
.kltog input { width: 22px; height: 22px; accent-color: var(--ksage); }
.klbig { width: 100%; height: 50px; border-radius: 25px; background: var(--kink); color: #fff8ee; font-size: 16px; font-weight: 700; }
.klbig.alt { background: var(--kclay); }
.klerr { color: var(--kclay2); font-size: 13px; min-height: 1em; margin: 0 0 8px; }

/* Wider screens: the same studio, laid out for the room. */
@media (min-width: 760px) {
  body { padding-bottom: 0; }
  .klhd { padding: 18px 48px 10px; border-bottom: 1px solid var(--kline); }
  .klnav { display: flex; gap: 26px; margin-left: 34px; font-size: 15px; font-weight: 700; color: var(--kink2); }
  .klnav a { text-decoration: none; }
  .klnav a.on { color: var(--kink); }
  .klhero { padding: 34px 48px 10px; }
  .klhero h1 { font-size: 46px; max-width: 640px; }
  .klhero p { font-size: 17px; }
  .klph { padding: 18px 48px 4px; gap: 12px; }
  .klph img { width: 260px; height: 180px; border-radius: 18px; }
  .klh2 { padding: 26px 48px 12px; }
  .klcls { grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 0 44px 30px; gap: 14px; }
  .kltabs { display: none; }
  .klsheet { bottom: auto; top: 12vh; border-radius: 26px; }
}
@media (min-width: 1100px) { .klcls { grid-template-columns: repeat(3, minmax(0, 1fr)); } }

/* Maya's view */
.maya { max-width: 640px; margin: 0 auto; padding: 0 20px 40px; }
.maya .klc { margin-bottom: 12px; display: block; }
.maya h3 { margin-bottom: 4px; }
.maya ol, .maya ul { margin: 6px 0 0; padding-left: 20px; }
.maya .klwait { display: block; margin-top: 10px; font-weight: 700; }
"""

WAITLIST_JS = """// Kiln's waitlist: one line per person waiting, oldest first.
// On Stage it lives in this browser, so trying it never touches a real booking.
(function () {
  'use strict';
  const KEY = 'kiln-stage-waitlist';

  function read() {
    try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (error) { return {}; }
  }

  function write(lists) {
    try { localStorage.setItem(KEY, JSON.stringify(lists)); } catch (error) { /* private mode: keep it for this visit */ }
  }

  window.KilnWaitlist = {
    // Join the waitlist for a class. Returns your place in line (1 is first).
    join(cls, person) {
      const lists = read();
      const line = lists[cls.id] || [];
      line.push({ name: person.name, phone: person.phone, joinedAt: Date.now() });
      lists[cls.id] = line;
      write(lists);
      return (cls.waiting || 0) + line.length;
    },
    // How many are waiting, counting Stage's test data and anyone who joined here.
    count(cls) {
      return (cls.waiting || 0) + (read()[cls.id] || []).length;
    },
    // Your place in line, or 0 if you haven't joined.
    placeOf(cls, phone) {
      const line = read()[cls.id] || [];
      const index = line.findIndex(entry => entry.phone === phone);
      return index < 0 ? 0 : (cls.waiting || 0) + index + 1;
    },
  };
})();
"""

KILN_JS = """// Kiln: this week's classes at Maya's studio, with booking and the waitlist.
// Stage runs on its own copy of the bookings (classes.json). Bookings made here stay in this browser.
(function () {
  'use strict';
  const DAYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];
  const DAY_LABELS = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
  const BOOKED_KEY = 'kiln-stage-bookings';
  const ME = { name: 'Alex', phone: '+1 510 555 0108' };
  const list = document.getElementById('classes');
  const sheet = document.getElementById('sheet');
  const veil = document.getElementById('veil');
  let classes = [];

  function booked() {
    try { return JSON.parse(localStorage.getItem(BOOKED_KEY) || '{}'); } catch (error) { return {}; }
  }

  function saveBooked(map) {
    try { localStorage.setItem(BOOKED_KEY, JSON.stringify(map)); } catch (error) { /* private mode */ }
  }

  // The next date this class runs: today if it's today, otherwise later this week.
  function nextDate(day) {
    const today = new Date();
    const date = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    const ahead = (DAYS.indexOf(day) - date.getDay() + 7) % 7;
    date.setDate(date.getDate() + ahead);
    return date;
  }

  function timeText(hhmm) {
    const [h, m] = hhmm.split(':').map(Number);
    const hour = h % 12 || 12;
    return `${hour}${m ? ':' + String(m).padStart(2, '0') : ':00'} ${h < 12 ? 'AM' : 'PM'}`;
  }

  function spotsLeft(cls) {
    return Math.max(0, cls.wheels - cls.booked - (booked()[cls.id] ? 1 : 0));
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === 'class') node.className = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    }
    for (const child of children) node.append(child);
    return node;
  }

  function card(cls) {
    const date = nextDate(cls.day);
    const left = spotsLeft(cls);
    const mine = !!booked()[cls.id];
    const waiting = window.KilnWaitlist.count(cls);
    const place = window.KilnWaitlist.placeOf(cls, ME.phone);
    const actions = el('div', { class: 'klact' });
    if (mine) {
      actions.append(el('span', { class: 'klspots' }, 'You\\u2019re booked'), el('span', { class: 'klbtn done' }, 'Booked'));
    } else if (left === 0) {
      actions.append(el('span', { class: 'klspots full' }, 'Full'));
      if (waiting) actions.append(el('span', { class: 'klwait' }, `${waiting} waiting`));
      actions.append(place
        ? el('span', { class: 'klbtn done' }, `#${place} in line`)
        : el('button', { class: 'klbtn alt', type: 'button', onclick: () => openWaitlist(cls) }, 'Join waitlist'));
    } else {
      actions.append(el('span', { class: 'klspots' + (left <= 3 ? ' few' : '') }, `${left} of ${cls.wheels} wheels left`),
        el('button', { class: 'klbtn', type: 'button', onclick: () => openBooking(cls) }, 'Book'));
    }
    return el('article', { class: 'klc' },
      el('span', { class: 'kld' }, el('small', {}, DAY_LABELS[date.getDay()]), el('b', {}, String(date.getDate()))),
      el('h3', {}, cls.title),
      el('p', {}, `${timeText(cls.time)} \\u00b7 ${cls.with}`),
      actions);
  }

  function render() {
    const sorted = classes.slice().sort((a, b) => nextDate(a.day) - nextDate(b.day));
    list.replaceChildren(...sorted.map(card));
    if (sorted.length) {
      const first = nextDate(sorted[0].day), last = nextDate(sorted[sorted.length - 1].day);
      const month = d => d.toLocaleDateString(undefined, { month: 'short' });
      document.getElementById('week').textContent =
        `${month(first)} ${first.getDate()} to ${month(last) === month(first) ? '' : month(last) + ' '}${last.getDate()}`;
    }
  }

  function field(label, name, value, type) {
    return el('label', { class: 'klfield' }, el('span', {}, label),
      el('input', { name, value, type: type || 'text', required: '', autocomplete: name === 'phone' ? 'tel' : 'name' }));
  }

  function openSheet(...children) {
    sheet.className = 'klsheet';
    sheet.replaceChildren(...children);
    sheet.hidden = false;
    veil.hidden = false;
    const focus = sheet.querySelector('input, button');
    if (focus) focus.focus();
  }

  function closeSheet() {
    sheet.hidden = true;
    veil.hidden = true;
    render();
  }

  function openBooking(cls) {
    const date = nextDate(cls.day);
    const error = el('p', { class: 'klerr', role: 'alert' });
    const form = el('form', { onsubmit: event => {
      event.preventDefault();
      const data = new FormData(form);
      if (!String(data.get('name')).trim() || !String(data.get('phone')).trim()) {
        error.textContent = 'Add your name and phone number so Maya knows who\\u2019s coming.';
        return;
      }
      if (spotsLeft(cls) === 0) { error.textContent = 'Someone just took the last wheel. Join the waitlist instead.'; return; }
      const map = booked(); map[cls.id] = { name: data.get('name'), phone: data.get('phone'), at: Date.now() }; saveBooked(map);
      sheet.classList.add('done');
      sheet.replaceChildren(el('h2', { id: 'sheet-title' }, 'You\\u2019re booked'),
        el('p', {}, `See you ${date.toLocaleDateString(undefined, { weekday: 'long' })} at ${timeText(cls.time)}. We\\u2019ll text you the day before.`),
        el('button', { class: 'klbig', type: 'button', onclick: closeSheet }, 'Done'));
    } },
      field('Name', 'name', ME.name), field('Phone', 'phone', ME.phone, 'tel'),
      el('label', { class: 'kltog' }, 'Text me the day before', el('input', { type: 'checkbox', name: 'remind', checked: '' })),
      error, el('button', { class: 'klbig', type: 'submit' }, 'Book my wheel'));
    openSheet(el('h2', { id: 'sheet-title' }, cls.title),
      el('p', {}, `${date.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })} \\u00b7 ${timeText(cls.time)} \\u00b7 $${cls.price}`),
      form);
  }

  function openWaitlist(cls) {
    const form = el('form', { onsubmit: event => {
      event.preventDefault();
      const data = new FormData(form);
      const place = window.KilnWaitlist.join(cls, { name: String(data.get('name')), phone: String(data.get('phone')) });
      const order = ['first', 'second', 'third', 'fourth', 'fifth', 'sixth'][place - 1] || `number ${place}`;
      sheet.classList.add('done');
      sheet.replaceChildren(el('span', { class: 'klpos' }, `#${place}`),
        el('h2', { id: 'sheet-title' }, `You\\u2019re ${order} in line`),
        el('p', {}, `If a spot opens in ${cls.title}, we\\u2019ll let you know.`),
        el('button', { class: 'klbig', type: 'button', onclick: closeSheet }, 'Done'));
    } },
      field('Name', 'name', ME.name), field('Phone', 'phone', ME.phone, 'tel'),
      el('button', { class: 'klbig alt', type: 'submit' }, 'Join the waitlist'));
    openSheet(el('h2', { id: 'sheet-title' }, 'Join the waitlist'),
      el('p', {}, `${cls.title} is full. If someone cancels, the first person waiting is offered the spot.`), form);
  }

  veil.addEventListener('click', closeSheet);
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !sheet.hidden) closeSheet(); });

  fetch('classes.json', { cache: 'no-store' })
    .then(response => response.json())
    .then(data => { classes = data; render(); })
    .catch(() => { list.replaceChildren(el('p', {}, 'This week\\u2019s classes couldn\\u2019t load. Try again in a moment.')); });
})();
"""

MAYA_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kiln · Maya’s day</title>
  <link rel="icon" href="mark.svg">
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header class="klhd"><img class="klmk" src="mark.svg" alt="" width="30" height="30"><b>Kiln</b><span class="klsp"></span><a class="klme" href="./" aria-label="Back to classes">M</a></header>
  <main class="maya">
    <section class="klhero"><small>Maya’s view</small><h1>Your week</h1><p>Who’s booked, and who’s waiting.</p></section>
    <article class="klc"><h3>Wheel basics</h3><p>Tuesday, 6:30 PM · 5 of 8 booked</p>
      <ul><li>Ana Ruiz</li><li>Ben Okafor</li><li>Chloe Park</li><li>Dev Patel</li><li>Eli Brooks</li></ul></article>
    <article class="klc"><h3>Glaze night</h3><p>Thursday, 7:00 PM · full</p>
      <ul><li>Farah Ali</li><li>Gus Lind</li><li>Hana Mori</li><li>Ivy Chen</li><li>Jon Weiss</li><li>Kai Moana</li><li>Lena Vogel</li><li>Max Ortiz</li></ul>
      <span class="klwait">Waiting, in order</span>
      <ol><li>Nina Shah</li><li>Omar Haddad</li><li>Pia Lund</li><li>Quinn Avery</li></ol></article>
    <article class="klc"><h3>Open studio</h3><p>Saturday, 10:00 AM · 3 of 8 booked</p>
      <ul><li>Rosa Diaz</li><li>Sol Reyes</li><li>Tom Hart</li></ul></article>
    <article class="klc"><h3>Throwing bowls</h3><p>Sunday, 2:00 PM · 2 of 8 booked</p>
      <ul><li>Uma Rao</li><li>Vic Stone</li></ul></article>
  </main>
</body>
</html>
"""

SIGNIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kiln · Your account</title>
  <link rel="icon" href="mark.svg">
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header class="klhd"><img class="klmk" src="mark.svg" alt="" width="30" height="30"><b>Kiln</b></header>
  <main class="maya">
    <section class="klhero"><small>Your account</small><h1>Signed in as Alex</h1>
      <p>You signed in with Luma Connect, so there’s no password to remember. Your bookings are matched by your phone number.</p></section>
    <p><a class="klbtn" href="./">Back to classes</a></p>
  </main>
</body>
</html>
"""

PACKAGE_JSON = {
    "name": "kiln",
    "version": "1.4.0",
    "private": True,
    "description": "Class sign-ups for Maya's pottery studio",
    "scripts": {"test": "node tests/phone.test.js"},
    "dependencies": {"@aws-sdk/client-s3": "^3.700.0", "pg": "^8.13.1", "resend": "^4.0.1", "twilio": "^5.3.6"},
}

SCHEMA_SQL = """-- Kiln's data in PostgreSQL. Stage has its own copy.
CREATE TABLE classes (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  starts_at TIMESTAMPTZ NOT NULL,
  wheels INTEGER NOT NULL DEFAULT 8
);

CREATE TABLE accounts (
  id TEXT PRIMARY KEY,           -- the Luma Connect id
  name TEXT NOT NULL,
  phone TEXT NOT NULL UNIQUE     -- always with the country code: +1 510 555 0142
);

CREATE TABLE bookings (
  id BIGSERIAL PRIMARY KEY,
  class_id TEXT NOT NULL REFERENCES classes(id),
  phone TEXT NOT NULL,
  account_id TEXT REFERENCES accounts(id),  -- linked by phone number when someone signs in
  remind BOOLEAN NOT NULL DEFAULT true
);

-- One line per person waiting, oldest first.
CREATE TABLE waitlist (
  id BIGSERIAL PRIMARY KEY,
  class_id TEXT NOT NULL REFERENCES classes(id),
  phone TEXT NOT NULL,
  joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  offered_at TIMESTAMPTZ
);
"""

PHONE_JS = """// Phone numbers, saved one way: with the country code, so every reminder can be delivered.
'use strict';

// "510 555 0142" -> "+1 510 555 0142". Numbers that already have a + keep their country.
function normalizePhone(input, defaultCountry = '1') {
  const digits = String(input).replace(/[^0-9+]/g, '');
  if (!digits) return '';
  if (digits.startsWith('+')) return format(digits.slice(1));
  if (digits.length === 10) return format(defaultCountry + digits);
  if (digits.length === 11 && digits.startsWith(defaultCountry)) return format(digits);
  return format(digits);
}

function format(digits) {
  if (digits.length === 11 && digits.startsWith('1')) {
    return `+1 ${digits.slice(1, 4)} ${digits.slice(4, 7)} ${digits.slice(7)}`;
  }
  return `+${digits}`;
}

module.exports = { normalizePhone };
"""

PHONE_TEST_JS = """// node tests/phone.test.js
'use strict';
const assert = require('assert');
const { normalizePhone } = require('../server/phone');

assert.strictEqual(normalizePhone('510 555 0142'), '+1 510 555 0142');
assert.strictEqual(normalizePhone('(510) 555-0142'), '+1 510 555 0142');
assert.strictEqual(normalizePhone('+1 510 555 0142'), '+1 510 555 0142');
assert.strictEqual(normalizePhone('15105550142'), '+1 510 555 0142');
console.log('phone numbers: all good');
"""

AUTH_JS = """// Sign in with Luma Connect. Kiln never sees or keeps a password.
'use strict';

// After Luma Connect sends someone back, link any bookings they made before
// sign-in existed, matched by phone number (Alex decided this when sign-in was added).
async function linkOldBookings(db, account) {
  const result = await db.query(
    'UPDATE bookings SET account_id = $1 WHERE phone = $2 AND account_id IS NULL',
    [account.id, account.phone]);
  return result.rowCount;
}

module.exports = { linkOldBookings };
"""

README_MD = """# Kiln

Class sign-ups for Maya's pottery studio: this week's classes, booking a wheel,
and a waitlist when a class is full.

- `src/` is the app people use. `src/maya.html` is Maya's own view.
- `server/` holds the server pieces: the data layout (PostgreSQL), phone numbers, sign-in.
- `.playground/` is the project book: goals, decisions, notes and history.

To try it: `cd src && python3 -m http.server 8000`, then open http://localhost:8000.
"""



# ── Faces and the team's capacity ──
# Illustrated portraits, drawn here (warm, flat, simple shapes): nobody's photo. Each is a plain SVG
# drawing in .playground/people/, named by the member's ``photo`` field, and synced like any record.

def _portrait(bg: str, skin: str, shade: str, shirt: str, *, back_hair: str = "", front_hair: str = "",
              extra: str = "", collar: str = "") -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="28 22 200 200">
  <rect x="0" y="0" width="256" height="256" fill="{bg}"/>
  {back_hair}
  <path d="M34 256 C38 200 78 176 128 176 C178 176 218 200 222 256 Z" fill="{shirt}"/>
  {collar}
  <path d="M110 140 h36 v40 c0 10 -36 10 -36 0 Z" fill="{shade}"/>
  <ellipse cx="83" cy="120" rx="9" ry="12" fill="{shade}"/>
  <ellipse cx="173" cy="120" rx="9" ry="12" fill="{shade}"/>
  <ellipse cx="128" cy="114" rx="45" ry="52" fill="{skin}"/>
  {front_hair}
  <ellipse cx="110" cy="120" rx="4.2" ry="5" fill="#2a1f1a"/>
  <ellipse cx="146" cy="120" rx="4.2" ry="5" fill="#2a1f1a"/>
  <circle cx="100" cy="136" r="7" fill="#e0806a" opacity="0.28"/>
  <circle cx="156" cy="136" r="7" fill="#e0806a" opacity="0.28"/>
  <path d="M126 124 q4 9 -2 13" fill="none" stroke="{shade}" stroke-width="3" stroke-linecap="round"/>
  <path d="M115 147 q13 10 26 0" fill="none" stroke="#7a3b2e" stroke-width="3.6" stroke-linecap="round"/>
  {extra}
</svg>
"""


PORTRAITS = {
    "alex": _portrait(
        "#f1d2ae", "#efc3a0", "#d9a07c", "#4d6a8c",
        front_hair='<path d="M82 108 C80 70 104 56 130 57 C158 58 178 74 175 108 C168 92 156 84 140 86 '
                   'C122 88 104 84 96 76 C92 88 88 98 82 108 Z" fill="#5b3a26"/>',
        extra='<path d="M100 106 q10 -6 20 -1 M136 105 q10 -5 20 1" fill="none" stroke="#5b3a26" '
              'stroke-width="3.4" stroke-linecap="round"/>',
        collar='<path d="M106 178 L128 204 L150 178" fill="none" stroke="#f4ece2" stroke-width="7" '
               'stroke-linejoin="round"/>'),
    "priya": _portrait(
        "#f2bfa6", "#b97b55", "#9c6142", "#d79b38",
        back_hair='<path d="M70 120 C64 70 94 50 128 50 C164 50 194 72 188 122 C190 160 196 196 186 214 '
                  'C170 222 150 214 140 200 L116 200 C106 214 86 222 70 214 C60 196 66 160 70 120 Z" fill="#231816"/>',
        front_hair='<path d="M83 116 C82 78 104 60 130 60 C152 60 176 74 174 116 C168 96 156 82 136 76 '
                   'C130 92 108 104 83 116 Z" fill="#231816"/>',
        extra='<path d="M100 106 q10 -5 19 -1 M137 105 q10 -4 19 1" fill="none" stroke="#231816" '
              'stroke-width="3.2" stroke-linecap="round"/><circle cx="84" cy="136" r="3.6" fill="#f4c542"/>'
              '<circle cx="172" cy="136" r="3.6" fill="#f4c542"/>',
        collar='<path d="M98 180 Q128 214 158 180" fill="none" stroke="#b9822d" stroke-width="6" '
               'stroke-linecap="round"/>'),
    "sam": _portrait(
        "#c3d9c6", "#8f5d3e", "#774a30", "#2f7c76",
        front_hair='<path d="M82 112 C78 72 102 54 130 54 C160 54 180 74 174 112 C170 96 162 88 150 84 '
                   'C138 92 112 92 96 86 C90 94 86 102 82 112 Z" fill="#2c201b"/>'
                   '<circle cx="128" cy="46" r="17" fill="#2c201b"/>',
        extra='<g fill="none" stroke="#2c201b" stroke-width="3"><circle cx="110" cy="120" r="12"/>'
              '<circle cx="146" cy="120" r="12"/><path d="M122 120 h12 M98 118 l-12 -3 M158 118 l12 -3"/></g>'
              '<path d="M100 102 q10 -5 19 -1 M137 101 q10 -4 19 1" fill="none" stroke="#2c201b" '
              'stroke-width="3.2" stroke-linecap="round"/>',
        collar='<path d="M104 178 Q128 196 152 178" fill="none" stroke="#25615c" stroke-width="6" '
               'stroke-linecap="round"/>'),
}


def _people(store: ProjectStore) -> None:
    for member_id, svg in PORTRAITS.items():
        atomic_write_text(store.paths.people_dir / f"{member_id}.svg", svg)


def _capacity(store: ProjectStore) -> None:
    """Priya and Sam chose to show their plans to the team (core.usage's capacity files): numbers and
    labels only, never an account. Priya lets her Claude take team tasks, up to 20% of her week;
    Sam hasn't opted in."""
    now = datetime.now().astimezone()
    resets = (now + timedelta(days=(1 - now.weekday()) % 7 or 7)).replace(hour=9, minute=0, second=0, microsecond=0)
    stamp = now.replace(microsecond=0).isoformat()
    folder = store.paths.meta / "capacity"
    rows = {
        "priya": [{"label": "Claude", "provider": "claude", "plan": "Max plan", "team_tasks": True,
                   "team_task_percent": 20, "percent_left": 54.0, "window": "week",
                   "resets_at": resets.isoformat()}],
        "sam": [{"label": "Codex", "provider": "codex", "plan": "Plus plan", "team_tasks": False}],
    }
    for member_id, connections in rows.items():
        atomic_write_text(folder / f"{member_id}.json",
                          json.dumps({"member": member_id, "updated": stamp, "connections": connections},
                                     ensure_ascii=False, indent=2) + "\n")


def create_sample_device_usage(now: datetime | None = None) -> None:
    """What this computer knows about its own connections (the walk's fixture connections, by id):
    headroom readings the agent CLIs printed, and this month's runs. Written to the per-device usage
    folder (PLAYGROUND_DATA_DIR), never to a project.

    Claude: 62% of the week left. Codex: plenty left today. Ari on Luma credits: $1.80 of its
    $10 a month used. The team key: $6.40 of $25 this month. Qwen: runs, no money."""
    from . import usage
    now = (now or datetime.now().astimezone()).astimezone()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def this_month(hours_ago: float) -> datetime:
        return max(month_start + timedelta(minutes=5), now - timedelta(hours=hours_ago))

    tuesday = (now + timedelta(days=(1 - now.weekday()) % 7 or 7)).replace(hour=9, minute=0, second=0, microsecond=0)
    usage.record_headroom("claude", 62, window="week", resets_at=tuesday, source="cli", when=now - timedelta(minutes=20))
    tomorrow = (now + timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)
    usage.record_headroom("codex", 91, window="day", resets_at=tomorrow, source="cli", when=now - timedelta(hours=2))
    runs = [("ari", 0.42, 52_000, 6_100, 50), ("ari", 0.61, 71_000, 9_400, 30), ("ari", 0.77, 88_000, 12_000, 4),
            ("team-key", 2.10, 240_000, 31_000, 120), ("team-key", 1.85, 205_000, 27_500, 70),
            ("team-key", 2.45, 268_000, 36_000, 6),
            ("claude", None, 410_000, 52_000, 3), ("codex", None, 96_000, 14_000, 26),
            ("qwen", 0.0, 61_000, 8_800, 9)]
    for connection, cost, tokens_in, tokens_out, hours in runs:
        usage.record_run(connection, cost=cost, tokens_in=tokens_in, tokens_out=tokens_out, duration_s=240,
                         when=this_month(hours), model={"qwen": "qwen3:27b"}.get(connection, ""))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]).expanduser() if args else projects_root()
    kiln = create_sample_kiln(root)
    print(f"Made {kiln.name} in {kiln.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
