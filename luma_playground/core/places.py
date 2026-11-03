# SPDX-License-Identifier: Apache-2.0
"""Places: where a project lives and runs.

A place is *this computer* or a server reached over SSH, with a role:
development (runs the app and, optionally, agents), production (the live app)
or preview (an extra device or VM to try it on). Places live in
``.playground/project.toml`` as ``[[places]]`` and hold no secrets: name,
role, kind, host, user, port, and optionally ``url`` (the public address of the
live app) and ``path`` (the project folder on the server).

Production is protected. ``guard_write(place)`` refuses any write to a
production place unless it carries a release ticket, and the only way to get
one is ``authorize_release``, which needs a Review that is ready, pointed at
that place, and approved by an owner or reviewer. Tickets are HMAC'd with a
key that exists only in this process's memory, so an agent (which talks to
Playground from its own process through MCP) can't forge one, and each ticket
works once, for ten minutes.

Releases (the second half of this file) are the only way anything reaches
production: ``release`` spends a ticket, saves a restore point (a git tag,
plus a copy of the data when run.toml says how), copies the release with the
place's ``[deploy]`` recipe, and checks the live address answers.
``rollback`` puts an earlier restore point back the same way. Both hold a
release lock, so two never run at once.

Threat notes
    * Host, user and port are validated before they reach ``ssh``; a host
      starting with "-" (option injection) or holding shell characters is
      refused, and ``--`` separates options from the destination anyway.
    * Host keys are checked: Playground keeps its own known_hosts file
      (``<data>/ssh/known_hosts``) and also reads ``~/.ssh/known_hosts``; a
      new server is trusted on first use, a *changed* key is reported in plain
      words and never accepted silently.
    * Connection tests never prompt (BatchMode, or askpass from the keyring
      for Playground's own device key) and never fall back to passwords.
"""
from __future__ import annotations

import contextlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
import fnmatch
import hashlib
import hmac
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import ssl
import subprocess
import tarfile
import tempfile
import threading
import time
import tomllib
from typing import Any, Callable, Iterable, Iterator
import urllib.error
import urllib.request

try:
    import fcntl
except ImportError:  # pragma: no cover - not on Luma or macOS
    fcntl = None  # type: ignore[assignment]

from . import events
from .model import (MEMBER_ROLES, PLACE_KINDS, PLACE_ROLES, THIS_COMPUTER, GoalStatus, Place, ThreadEntry,
                    looks_like_secret, now_iso)
from .paths import ProjectPaths, data_dir, new_id
from .store import ProjectStore, atomic_write_json, read_json

ROLE_LABELS = {"development": "Development server", "production": "Production server",
               "preview": "Preview device"}
LOCAL_PLACE = {"id": THIS_COMPUTER, "name": "This computer", "role": "development", "kind": "local",
               "builtin": True}
RELEASE_APPROVER_ROLES = ("owner", "reviewer")
TICKET_LIFETIME_S = 600

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")


class PlaceError(Exception):
    """A plain-words problem with a place."""


class ProductionProtected(PlaceError):
    pass


# ───────────────────────────── validation ─────────────────────────────

def validate_host(host: str) -> str:
    """A hostname, IPv4 or IPv6 address, normalised; PlaceError otherwise."""
    text = str(host or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text:
        raise PlaceError("Enter the server's address, like 203.0.113.7 or dev.example.com.")
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        pass
    if text.startswith("-") or not _HOSTNAME_RE.match(text):
        raise PlaceError(f"“{text}” doesn't look like a server address.")
    return text.rstrip(".").lower()


def validate_user(user: str) -> str:
    text = str(user or "").strip()
    if not _USER_RE.match(text):
        raise PlaceError("Account names use lowercase letters, numbers, dots, dashes and underscores.")
    return text


def validate_port(port: Any) -> int:
    try:
        value = int(port or 22)
    except (TypeError, ValueError):
        raise PlaceError("The port should be a number, usually 22.") from None
    if not 1 <= value <= 65535:
        raise PlaceError("The port should be between 1 and 65535, usually 22.")
    return value


# ───────────────────────────── reading and writing ─────────────────────────────

def _store(project_dir: Path | str, bus: events.EventBus | None = None) -> ProjectStore:
    return ProjectStore(project_dir, bus=bus)


def list_places(project_dir: Path | str, include_local: bool = True) -> list[dict]:
    """Every place, this computer first. Each dict carries ``status`` in plain words."""
    project = _store(project_dir).read_project()
    tests = _read_tests(project_dir)
    places = [p.to_dict() for p in project.places]
    if include_local and not any(p["id"] == THIS_COMPUTER for p in places):
        places.insert(0, dict(LOCAL_PLACE))
    for place in places:
        place["is_home"] = place["id"] == project.home
        place["status"] = place_status(place, tests.get(place["id"]))
    return places


def get_place(project_dir: Path | str, place_id: str) -> dict | None:
    return next((p for p in list_places(project_dir) if p["id"] == place_id), None)


def places_for_role(project_dir: Path | str, role: str) -> list[dict]:
    return [p for p in list_places(project_dir) if p.get("role") == role]


def add_place(project_dir: Path | str, name: str, *, role: str = "development", kind: str = "ssh",
              host: str = "", user: str = "", port: int = 22, url: str = "", path: str = "",
              description: str = "", test: bool = True, timeout: float = 10,
              bus: events.EventBus | None = None) -> dict:
    """Validate, save, and (by default) try the connection.

    The place is saved even when the test fails: a new server usually needs
    its one-time setup before Playground can get in. Returns
    ``{"place": {...}, "test": {...} | None}``.
    """
    if role not in PLACE_ROLES:
        raise PlaceError("A place is for development, production or previews.")
    if kind not in PLACE_KINDS:
        raise PlaceError("A place is this computer or a server reached over SSH.")
    name = " ".join(str(name or "").split())[:80] or ROLE_LABELS[role]
    extra: dict[str, Any] = {}
    if url:
        if not re.match(r"^https?://[^\s\"'<>]+$", url):
            raise PlaceError("The live address should start with https://.")
        extra["url"] = url
    if path:
        if "\x00" in path or "\n" in path:
            raise PlaceError("That folder path has characters Playground can't use.")
        extra["path"] = path
    if kind == "ssh":
        place = Place(id=new_id(name, 30), name=name, role=role, kind=kind, host=validate_host(host),
                      user=validate_user(user), port=validate_port(port), description=description, extra=extra)
    else:
        place = Place(id=new_id(name, 30), name=name, role=role, kind="local", description=description, extra=extra)
    store = _store(project_dir, bus)
    with store.locked():
        project = store.read_project()
        if kind == "ssh" and any(p.host == place.host and p.user == place.user and p.port == place.port
                                 and p.role == role for p in project.places):
            raise PlaceError("That server is already in this project.")
        project.places.append(place)
        store.write_project(project)
    result: dict[str, Any] = {"place": place.to_dict(), "test": None}
    if test and kind == "ssh":
        result["test"] = test_place(project_dir, place.id, timeout=timeout, bus=bus)
    return result


def update_place(project_dir: Path | str, place_id: str, *, bus: events.EventBus | None = None,
                 **changes: Any) -> dict:
    """Rename, change role, url or path. Connection details are re-validated."""
    store = _store(project_dir, bus)
    with store.locked():
        project = store.read_project()
        place = project.place(place_id)
        if place is None:
            raise PlaceError("That place isn't in this project.")
        for key, value in changes.items():
            if key == "name":
                place.name = " ".join(str(value).split())[:80] or place.name
            elif key == "role":
                if value not in PLACE_ROLES:
                    raise PlaceError("A place is for development, production or previews.")
                place.role = value
            elif key == "host":
                place.host = validate_host(value)
            elif key == "user":
                place.user = validate_user(value)
            elif key == "port":
                place.port = validate_port(value)
            elif key in ("url", "path", "description"):
                if key == "description":
                    place.description = str(value)
                elif value:
                    place.extra[key] = str(value)
                else:
                    place.extra.pop(key, None)
            else:
                raise PlaceError(f"Places don't have a setting called {key}.")
        store.write_project(project)
    return place.to_dict()


def remove_place(project_dir: Path | str, place_id: str, bus: events.EventBus | None = None) -> bool:
    if place_id == THIS_COMPUTER:
        raise PlaceError("This computer is always a place; it can't be removed.")
    store = _store(project_dir, bus)
    with store.locked():
        project = store.read_project()
        before = len(project.places)
        project.places = [p for p in project.places if p.id != place_id]
        if len(project.places) == before:
            return False
        if project.home == place_id:
            project.home = THIS_COMPUTER
        store.write_project(project)
    tests = _read_tests(project_dir)
    if tests.pop(place_id, None) is not None:
        _write_tests(project_dir, tests)
    return True


def set_home(project_dir: Path | str, place_id: str, bus: events.EventBus | None = None) -> dict:
    """Where the project lives. Returns ``{"home", "warning"}``; the warning is shown, not hidden."""
    store = _store(project_dir, bus)
    with store.locked():
        project = store.read_project()
        if place_id != THIS_COMPUTER:
            place = project.place(place_id)
            if place is None:
                raise PlaceError("That place isn't in this project.")
            if place.role == "production":
                raise PlaceError("The project can't live on the production server. Pick a development server.")
        project.home = place_id
        store.write_project(project)
    warning = ("Teammates can work while this computer is on." if place_id == THIS_COMPUTER else "")
    store.bus.publish(events.PLACES_CHANGED, project=str(store.root))
    return {"home": place_id, "warning": warning}


# ───────────────────────────── ssh ─────────────────────────────

def known_hosts_file() -> Path:
    directory = data_dir() / "ssh"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "known_hosts"
    if not path.exists():
        path.touch(mode=0o600)
    return path


def _ssh_quote(path: Path) -> str:
    text = str(path)
    if '"' in text:
        raise PlaceError("Playground's data folder has a quote in its name, which SSH can't read.")
    return f'"{text}"' if " " in text else text


def ssh_options(place: dict, project_id: str | None = None, batch: bool = True,
                timeout: float = 10) -> tuple[list[str], dict[str, str]]:
    """``ssh`` options and extra environment for a place (no destination, no command).

    Uses this device's Playground key and, when there is one, the project
    certificate. With a keyring-encrypted device key, BatchMode is replaced by
    the same non-interactive settings plus askpass, so the key can unlock.
    """
    if place.get("kind") != "ssh":
        raise PlaceError("That place is this computer; it doesn't need SSH.")
    port = validate_port(place.get("port"))
    opts = ["-p", str(port),
            "-o", f"ConnectTimeout={max(1, int(timeout))}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=" + " ".join(_ssh_quote(p) for p in (known_hosts_file(),
                                                                              Path.home() / ".ssh" / "known_hosts")),
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "ServerAliveInterval=15"]
    env: dict[str, str] = {}
    key = None
    try:
        from . import security
        key = security.device_key(create=False)
    except Exception:  # noqa: BLE001 - no key yet is fine; the person's own keys still work
        key = None
    if key is not None:
        opts += ["-i", str(key.private_path)]
        if project_id:
            cert = key.certificate_path(project_id)
            if cert.exists():
                opts += ["-o", f"CertificateFile={cert}"]
        env.update(key.ssh_env())
    if batch and not env:
        opts += ["-o", "BatchMode=yes"]
    elif batch:
        opts += ["-o", "NumberOfPasswordPrompts=1"]
    return opts, env


def destination(place: dict) -> str:
    return f"{validate_user(place.get('user', ''))}@{validate_host(place.get('host', ''))}"


def ssh_command(place: dict, remote: str | None = None, *, forward: Iterable[str] = (),
                project_id: str | None = None, tty: bool = False, timeout: float = 10,
                extra: Iterable[str] = ()) -> tuple[list[str], dict[str, str]]:
    """A full ``ssh`` argv for a place, plus env. ``remote`` is one already-quoted shell string."""
    ssh = shutil.which("ssh") or "ssh"
    opts, env = ssh_options(place, project_id=project_id, timeout=timeout)
    argv = [ssh, *opts]
    for spec in forward:
        argv += ["-L", spec]
    if forward:
        argv += ["-o", "ExitOnForwardFailure=yes"]
    if tty:
        argv.append("-tt")
    argv += list(extra)
    argv += ["--", destination(place)]
    if remote:
        argv.append(remote)
    return argv, env


_FAILURES = (
    (("Could not resolve hostname", "Name or service not known", "nodename nor servname"), "not-found",
     "Playground can't find a server at that address. Check it for typos."),
    (("REMOTE HOST IDENTIFICATION HAS CHANGED", "Host key verification failed"), "host-key-changed",
     "This server's identity has changed since last time. Don't connect until you know why: "
     "it could be a reinstall, or someone pretending to be your server."),
    (("Connection refused",), "refused",
     "The server is on, but it isn't accepting SSH connections. Is SSH running, and is the port right?"),
    (("timed out", "No route to host", "Network is unreachable", "Host is down"), "unreachable",
     "Can't reach it. Is it switched on and connected to the network?"),
    (("Permission denied", "Too many authentication failures"), "no-access",
     "The server is there, but it didn't let Playground in. It may need the one-time setup, "
     "or the account name may be different."),
)


def test_connection(place: dict, project_id: str | None = None, timeout: float = 10) -> dict:
    """Try ``ssh`` with no prompts and run a harmless command. Never raises for network trouble."""
    if place.get("kind") != "ssh":
        return {"ok": True, "state": "ok", "message": "Ready.", "checked": now_iso()}
    try:
        argv, env = ssh_command(place, "echo playground-ok; uname -sm", project_id=project_id, timeout=timeout)
    except PlaceError as exc:
        return {"ok": False, "state": "invalid", "message": str(exc), "checked": now_iso()}
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 5,
                              stdin=subprocess.DEVNULL, env={**os.environ, **env})
    except FileNotFoundError:
        return {"ok": False, "state": "no-ssh", "message": "This computer doesn't have SSH installed.",
                "checked": now_iso()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "state": "unreachable", "message": _FAILURES[3][2], "checked": now_iso()}
    elapsed = round(time.monotonic() - started, 2)
    out = proc.stdout.splitlines()
    if proc.returncode == 0 and out and out[0].strip() == "playground-ok":
        system = out[1].strip() if len(out) > 1 else ""
        return {"ok": True, "state": "ok", "message": "Connected.", "system": system[:80],
                "seconds": elapsed, "checked": now_iso()}
    stderr = proc.stderr or ""
    for needles, state, message in _FAILURES:
        if any(n.lower() in stderr.lower() for n in needles):
            return {"ok": False, "state": state, "message": message, "checked": now_iso(),
                    "detail": stderr.strip()[-400:]}
    return {"ok": False, "state": "failed", "checked": now_iso(), "detail": stderr.strip()[-400:],
            "message": "Playground couldn't connect. The details below may help."}


def test_place(project_dir: Path | str, place_id: str, timeout: float = 10,
               bus: events.EventBus | None = None) -> dict:
    """Test one place, remember the result (in .playground/local), and announce it."""
    store = _store(project_dir, bus)
    project = store.read_project()
    if place_id == THIS_COMPUTER:
        place = dict(LOCAL_PLACE)
    else:
        found = project.place(place_id)
        if found is None:
            raise PlaceError("That place isn't in this project.")
        place = found.to_dict()
    result = test_connection(place, project_id=project.id or None, timeout=timeout)
    tests = _read_tests(project_dir)
    tests[place_id] = result
    _write_tests(project_dir, tests)
    store.bus.publish(events.PLACES_CHANGED, project=str(store.root), place_id=place_id, test=result)
    return result


def _tests_path(project_dir: Path | str) -> Path:
    return ProjectPaths(project_dir).local_dir / "places-status.json"


def _read_tests(project_dir: Path | str) -> dict:
    return read_json(_tests_path(project_dir)) or {}


def _write_tests(project_dir: Path | str, tests: dict) -> None:
    path = _tests_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, tests)


# ───────────────────────────── plain words ─────────────────────────────

def describe(place: dict) -> str:
    """"Development server · deploy@203.0.113.7"."""
    if place.get("kind") != "ssh":
        return place.get("name") or "This computer"
    port = int(place.get("port") or 22)
    where = f"{place.get('user')}@{place.get('host')}" + (f":{port}" if port != 22 else "")
    return f"{ROLE_LABELS.get(place.get('role', ''), 'Server')} · {where}"


def place_status(place: dict, test: dict | None = None) -> str:
    """One line a beginner can read about a place."""
    role = place.get("role")
    if place.get("kind") != "ssh":
        return "Runs the app here while this computer is on."
    guard = " Agents can't change it; releases go through Review." if role == "production" else ""
    if not test:
        return "Not checked yet." + guard
    if test.get("ok"):
        system = f" ({test['system']})" if test.get("system") else ""
        return f"Connected{system}." + guard
    return str(test.get("message") or "Playground couldn't connect.") + guard


# ───────────────────────────── production protection ─────────────────────────────

_TICKET_KEY = secrets.token_bytes(32)        # lives only in this process
_USED_TICKETS: set[str] = set()
_TICKET_LOCK = threading.Lock()


@dataclass(frozen=True)
class ReleaseTicket:
    """Permission for one release to one production place. Single use, ten minutes."""
    place_id: str
    review_id: str
    approved_by: str
    expires: float
    nonce: str
    mac: str

    def _message(self) -> bytes:
        return json.dumps([self.place_id, self.review_id, self.approved_by, self.expires, self.nonce]).encode()


def _sign(place_id: str, review_id: str, approved_by: str, expires: float, nonce: str) -> str:
    message = json.dumps([place_id, review_id, approved_by, expires, nonce]).encode()
    return hmac.new(_TICKET_KEY, message, hashlib.sha256).hexdigest()


def authorize_release(project_dir: Path | str, review: dict, place_id: str, approved_by: str) -> ReleaseTicket:
    """The release path: a ready Review, for this place, approved by an owner or reviewer."""
    store = _store(project_dir)
    project = store.read_project()
    place = project.place(place_id)
    if place is None:
        raise PlaceError("That place isn't in this project.")
    if not review.get("ready"):
        raise ProductionProtected("This release isn't ready yet. Its checks need to pass first.")
    destination_id = review.get("destination") or review.get("destination_place") or review.get("place")
    if destination_id != place_id:
        raise ProductionProtected("This review was approved for a different place.")
    member = project.member(approved_by)
    if member is None or member.role not in RELEASE_APPROVER_ROLES:
        raise ProductionProtected("Only an owner or a reviewer can approve a release.")
    review_id = str(review.get("id") or review.get("review_id") or "review")
    expires = time.time() + TICKET_LIFETIME_S
    nonce = secrets.token_hex(16)
    ticket = ReleaseTicket(place_id, review_id, approved_by, expires, nonce,
                           _sign(place_id, review_id, approved_by, expires, nonce))
    going_back = str(review.get("kind") or "") == "rollback"
    words = (f"{member.name} approved going back to restore point {review.get('restore_point')} on {place.name}"
             if going_back else f"{member.name} approved a release to {place.name}.")
    store.log(words, kind="release", actor=approved_by, data={"place": place_id, "review": review_id})
    return ticket


def guard_write(place: dict | Place, *, actor: str = "agent", ticket: ReleaseTicket | None = None) -> bool:
    """Call before anything that changes a place. Raises ProductionProtected for production.

    Development and preview places return True. A production place needs a
    valid, unused ticket from ``authorize_release``; the ticket is spent here.
    """
    data = place.to_dict() if isinstance(place, Place) else dict(place)
    if data.get("role") != "production":
        return True
    if ticket is None:
        who = "Agents" if actor == "agent" else "Nobody"
        raise ProductionProtected(f"{who} can change the live app directly. Propose a release and it goes "
                                  "through Review.")
    expected = _sign(ticket.place_id, ticket.review_id, ticket.approved_by, ticket.expires, ticket.nonce)
    if not hmac.compare_digest(expected, ticket.mac):
        raise ProductionProtected("That release approval isn't genuine.")
    if ticket.place_id != data.get("id"):
        raise ProductionProtected("That release was approved for a different place.")
    if time.time() > ticket.expires:
        raise ProductionProtected("That release approval has expired. Approve it again.")
    with _TICKET_LOCK:
        if ticket.nonce in _USED_TICKETS:
            raise ProductionProtected("That release approval has already been used.")
        _USED_TICKETS.add(ticket.nonce)
    return True


def is_protected(place: dict | Place) -> bool:
    data = place.to_dict() if isinstance(place, Place) else place
    return data.get("role") == "production"


def live_url(project_dir: Path | str) -> str:
    """The production place's public address, if one is known."""
    for place in places_for_role(project_dir, "production"):
        if place.get("url"):
            return str(place["url"])
    return ""


__all__ = [
    "PlaceError", "ProductionProtected", "ReleaseTicket", "LOCAL_PLACE", "ROLE_LABELS",
    "validate_host", "validate_user", "validate_port",
    "list_places", "get_place", "places_for_role", "add_place", "update_place", "remove_place", "set_home",
    "test_place", "test_connection", "ssh_command", "ssh_options", "destination", "known_hosts_file",
    "describe", "place_status", "authorize_release", "guard_write", "is_protected", "live_url",
    "MEMBER_ROLES",
]


# ═════════════════════════ releases: recipes, restore points, release, rollback ═════════════════════════
#
# The format of [deploy] in .playground/run.toml is documented in ARCHITECTURE.md
# ("Addendum: releases to production"). Everything below runs only when a
# person asks: release() needs a ticket from authorize_release (Review), and
# rollback() one from authorize_rollback (History). Agents get neither.

DEPLOY_KINDS = ("ssh", "command", "vercel", "netlify", "cloudflare-pages")
DEPLOY_KIND_WORDS = {"ssh": "SSH and rsync", "command": "your own command", "vercel": "Vercel",
                     "netlify": "Netlify", "cloudflare-pages": "Cloudflare Pages"}
HOSTING_CLIS = {"vercel": ("vercel", "Vercel"), "netlify": ("netlify", "Netlify"),
                "cloudflare-pages": ("wrangler", "Cloudflare")}
DEPLOY_AUTH = ("auto", "certificate", "agent")
RESTORE_TAG_PREFIX = "playground/restore-"
ALWAYS_EXCLUDE = (".git", ".playground", ".mcp.json")
DEFAULT_EXCLUDE = ("node_modules", ".env", ".env.*", "__pycache__", ".DS_Store")
KEEP_RELEASE_COPIES = 3          # per place: exact copies of what went out, for a quick rollback
KEEP_SNAPSHOTS = 12              # data copies in .playground/local/snapshots
KEEP_RELEASE_LOGS = 10
RELEASE_STEP = "release.step"            # bus topics (see ARCHITECTURE.md)
RELEASE_FINISHED = "release.finished"

_REMOTE_PATH_RE = re.compile(r"^(?:/|~/)?[A-Za-z0-9_.][A-Za-z0-9_./-]*$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOG_SECRET_RE = re.compile(
    r"(?:sk|pk|rk|re|ghp|gho|xox[abpr]|AKIA|ASIA|SG|whsec|eyJ)[-_.]?[A-Za-z0-9_\-]{12,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"
    r"|(?i:(?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)\S+"
    r"|(?=[A-Za-z0-9+/_\-]*[G-Zg-z+/_])[A-Za-z0-9+/_\-]{40,}")


class ReleaseError(PlaceError):
    """A release or rollback that stopped, in plain words, naming the step it stopped at.

    ``step`` is "approval", "restore", "prepare", "copy", "data" or "check";
    ``detail`` is the tail of the tools' output with anything secret-looking
    hidden; ``retry`` says whether trying again could help.
    """

    def __init__(self, message: str, *, step: str = "", detail: str = "", restore_point: int | None = None,
                 retry: bool = True) -> None:
        super().__init__(message)
        self.step = step
        self.detail = detail
        self.restore_point = restore_point
        self.retry = retry


class ReleaseBusy(ReleaseError):
    """Another release or rollback holds the lock."""


class RecipeNotTrusted(ReleaseError):
    """The [deploy] recipe is new or changed since a person last agreed to run it."""

    def __init__(self, message: str, commands: list[tuple[str, str]]) -> None:
        super().__init__(message, step="approval", retry=False)
        self.commands = commands


# ───────────────────────────── the recipe ─────────────────────────────

@dataclass
class DeployRecipe:
    """How a release reaches one place: a ``[deploy]`` table, with any per-place override merged in."""
    place_id: str
    kind: str = "ssh"
    build: str = ""
    source: str = "."
    path: str = ""
    exclude: list[str] = field(default_factory=list)
    delete: bool = True
    restart: str = ""
    command: str = ""
    site: str = ""
    branch: str = ""
    health: str = ""
    expect_status: int = 0
    expect_text: str = ""
    health_timeout_s: float = 60.0
    timeout_s: float = 900.0
    auth: str = "auto"
    snapshot: str = ""
    restore_data: str = ""
    data_where: str = "server"
    env: dict[str, str] = field(default_factory=dict)
    env_secrets: list[str] = field(default_factory=list)
    script_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def fingerprint(self) -> str:
        """Changes whenever anything that would run changes (including a deploy script's contents)."""
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()

    @property
    def excludes(self) -> list[str]:
        return list(dict.fromkeys([*ALWAYS_EXCLUDE, *(self.exclude or DEFAULT_EXCLUDE)]))

    def commands(self, place: dict) -> list[tuple[str, str]]:
        """What will run, and where, for a person to read before agreeing to it."""
        name = place.get("name") or "the server"
        out: list[tuple[str, str]] = []
        if self.snapshot:
            out.append((name if self.data_where == "server" else "This computer", self.snapshot))
        if self.build:
            out.append(("This computer", self.build))
        if self.kind == "ssh":
            out.append(("This computer", f"copy {self.source.rstrip('/') or '.'}/ to {destination(place)}:{self.path}"))
            if self.restart:
                out.append((name, self.restart))
        elif self.kind == "command":
            out.append(("This computer", self.command))
        else:
            tool, _words = HOSTING_CLIS[self.kind]
            out.append(("This computer", f"{tool} … ({DEPLOY_KIND_WORDS[self.kind]})"))
        if self.restore_data:
            out.append((name if self.data_where == "server" else "This computer",
                        f"{self.restore_data}   (only when you choose to put data back)"))
        return out


def _read_run_toml(project_dir: Path | str) -> dict:
    path = ProjectPaths(project_dir).run_toml
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise PlaceError(f"Playground can’t read .playground/run.toml ({error}). Fix it, then try again.") from None


def _resolve_place(project, place_id: str | None) -> dict:
    if place_id in (None, ""):
        production = next((p for p in project.places if p.role == "production"), None)
        return production.to_dict() if production is not None else dict(LOCAL_PLACE)
    if place_id == THIS_COMPUTER:
        found = project.place(THIS_COMPUTER)
        return found.to_dict() if found is not None else dict(LOCAL_PLACE)
    found = project.place(place_id)
    if found is None:
        raise PlaceError("That place isn’t in this project.")
    return found.to_dict()


def validate_remote_path(path: str) -> str:
    """A folder on the server that is safe to sync into with ``--delete``.

    Only letters, digits and ``_ . / -``; no ``..``; and never ``/``, ``~`` or a
    top-level folder like ``/srv`` (a release must have a folder of its own).
    ``~/kiln`` becomes ``kiln``, which ssh and rsync read from the login's home.
    """
    text = str(path or "").strip()
    if not text:
        raise PlaceError("Tell Playground which folder on the server to copy releases to: add path = "
                         "\"/srv/kiln\" to [deploy] in .playground/run.toml, or set it on the place.")
    if text.startswith("~/"):
        text = text[2:]
    text = text.rstrip("/") or text
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if text in ("~", "/") or not parts or (text.startswith("/") and len(parts) < 2):
        raise PlaceError("Releases need a folder of their own on the server, like /srv/kiln, not "
                         f"“{path}”. Playground removes files there that aren’t part of the release.")
    if not _REMOTE_PATH_RE.match(text) or text.startswith("-"):
        raise PlaceError("The folder on the server can use letters, numbers, dots, dashes, underscores and "
                         "slashes, like /srv/kiln.")
    if ".." in parts:
        raise PlaceError("The folder on the server can’t use “..”. Give its full path, like /srv/kiln.")
    return text


def _one_line(value: Any, key: str) -> str:
    text = str(value if value is not None else "").strip()
    if "\x00" in text or "\n" in text or "\r" in text:
        raise PlaceError(f"run.toml’s [deploy] {key} should be one line.")
    return text


def _recipe_from(table: dict, place: dict, project_dir: Path) -> DeployRecipe:
    words = "run.toml’s [deploy]"
    kind = str(table.get("kind") or ("ssh" if place.get("kind") == "ssh" else "command"))
    if kind not in DEPLOY_KINDS:
        raise PlaceError(f"{words} has kind = “{kind}”. Playground knows ssh, command, vercel, netlify and "
                         "cloudflare-pages.")
    recipe = DeployRecipe(place_id=str(place.get("id") or ""), kind=kind)
    for key in ("build", "source", "path", "restart", "command", "site", "branch", "health", "expect_text",
                "snapshot", "restore_data"):
        if key in table:
            setattr(recipe, key, _one_line(table[key], key))
    recipe.source = recipe.source or "."
    root = Path(project_dir).resolve()
    if not (root / recipe.source).resolve().is_relative_to(root):
        raise PlaceError(f"{words} source has to be a folder inside the project.")
    exclude = table.get("exclude")
    if exclude is not None:
        if not isinstance(exclude, list) or not all(isinstance(e, str) and e.strip() for e in exclude):
            raise PlaceError(f"{words} exclude should be a list of names, like [\"node_modules\"].")
        recipe.exclude = [e.strip() for e in exclude]
    recipe.delete = bool(table.get("delete", True))
    for key, default in (("health_timeout_s", 60.0), ("timeout_s", 900.0)):
        try:
            value = float(table.get(key, default))
        except (TypeError, ValueError):
            raise PlaceError(f"{words} {key} should be a number of seconds.") from None
        if not 0 < value <= 24 * 3600:
            raise PlaceError(f"{words} {key} should be a number of seconds.")
        setattr(recipe, key, value)
    try:
        recipe.expect_status = int(table.get("expect_status") or 0)
    except (TypeError, ValueError):
        raise PlaceError(f"{words} expect_status should be a number, like 200.") from None
    recipe.auth = str(table.get("auth") or "auto")
    if recipe.auth not in DEPLOY_AUTH:
        raise PlaceError(f"{words} auth should be auto, certificate or agent.")
    recipe.data_where = str(table.get("data_where") or "server")
    if recipe.data_where not in ("server", "local"):
        raise PlaceError(f"{words} data_where should be server or local.")
    env = table.get("env") or {}
    if not isinstance(env, dict):
        raise PlaceError(f"{words} env should be a table of names and values.")
    for name, value in env.items():
        if not _ENV_NAME_RE.match(str(name)):
            raise PlaceError(f"{words} env has a name Playground can’t use: {name}.")
        if looks_like_secret(str(value)):
            raise PlaceError(f"{words} env {name} looks like a key. Keep it in the vault and list its name "
                             "under env_secrets instead.")
        recipe.env[str(name)] = _one_line(value, f"env.{name}")
    names = table.get("env_secrets") or []
    if not isinstance(names, list) or not all(isinstance(n, str) and _ENV_NAME_RE.match(n) for n in names):
        raise PlaceError(f"{words} env_secrets should be a list of vault secret names.")
    recipe.env_secrets = list(names)
    # What each kind needs.
    if kind == "ssh":
        if place.get("kind") != "ssh":
            raise PlaceError(f"{place.get('name') or 'This place'} is this computer, so there’s no server to copy "
                             "to. Use kind = \"command\" instead.")
        recipe.path = validate_remote_path(recipe.path or str(place.get("path") or ""))
    elif kind == "command":
        if not recipe.command:
            raise PlaceError(f"{words} needs command = \"…\": what to run to publish a release.")
        try:
            first = shlex.split(recipe.command)[0]
        except (ValueError, IndexError):
            raise PlaceError(f"{words} command can’t be read. Check its quotes.") from None
        script = (root / first).resolve()
        if script.is_relative_to(root) and script.is_file():
            # An agent may change a script without touching run.toml; changing it asks again.
            recipe.script_hash = hashlib.sha256(script.read_bytes()).hexdigest()
    elif kind == "cloudflare-pages" and not recipe.site:
        raise PlaceError(f"{words} needs site = \"…\": the Cloudflare Pages project name.")
    if (recipe.snapshot or recipe.restore_data) and recipe.data_where == "server" and place.get("kind") != "ssh":
        raise PlaceError(f"{words} saves data on the server, but {place.get('name')} isn’t reached over SSH. "
                         "Set data_where = \"local\".")
    # Where to check it's up.
    url = str(place.get("url") or "")
    if recipe.health.startswith("/"):
        recipe.health = (url.rstrip("/") + recipe.health) if url else ""
    elif recipe.health and not re.match(r"^https?://[^\s\"'<>]+$", recipe.health):
        raise PlaceError(f"{words} health should be an address like https://kiln.example/health.")
    return recipe


def read_deploy_recipe(project_dir: Path | str, place_id: str | None = None) -> DeployRecipe | None:
    """The recipe for a place, or None when run.toml doesn't say how to release there.

    ``[deploy]`` is the default for every production place; ``[deploy.places.<place id>]``
    overrides it for one place, and is the only way to give a non-production place a recipe.
    """
    document = _read_run_toml(project_dir)
    table = document.get("deploy")
    if not isinstance(table, dict):
        return None
    project = _store(project_dir).read_project()
    place = _resolve_place(project, place_id)
    per_place = table.get("places") or {}
    override = per_place.get(place["id"]) if isinstance(per_place, dict) else None
    if override is None and place.get("role") != "production":
        return None
    merged = {k: v for k, v in table.items() if k != "places"}
    if isinstance(override, dict):
        merged.update(override)
    return _recipe_from(merged, place, Path(project_dir))


# ───────────────────────────── agreeing to a recipe ─────────────────────────────

def _project_key(project_dir: Path | str, project) -> str:
    key = str(project.id or "") or hashlib.sha256(str(Path(project_dir).resolve()).encode()).hexdigest()[:24]
    return re.sub(r"[^A-Za-z0-9_.-]", "-", key)[:80]


def _trust_path(project_dir: Path | str, project) -> Path:
    # Per device, outside the project: an agent can edit run.toml, but not this.
    directory = data_dir() / "deploy-trust"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / f"{_project_key(project_dir, project)}.json"


def recipe_trusted(project_dir: Path | str, recipe: DeployRecipe) -> bool:
    project = _store(project_dir).read_project()
    trusted = read_json(_trust_path(project_dir, project)) or {}
    return hmac.compare_digest(str(trusted.get(recipe.place_id) or ""), recipe.fingerprint())


def trust_recipe(project_dir: Path | str, recipe: DeployRecipe) -> None:
    """Remember, on this device, that a person agreed to run this exact recipe."""
    _remember_recipe(project_dir, recipe)


def _remember_recipe(project_dir: Path | str, recipe: DeployRecipe) -> None:
    project = _store(project_dir).read_project()
    path = _trust_path(project_dir, project)
    trusted = read_json(path) or {}
    trusted[recipe.place_id] = recipe.fingerprint()
    atomic_write_json(path, trusted)
    os.chmod(path, 0o600)


def release_plan(project_dir: Path | str, place_id: str | None = None) -> dict:
    """What Review shows before Release: where it goes, how, whether the recipe needs a yes.

    Never raises for a bad recipe: ``problem`` says what's wrong in plain words.
    """
    project = _store(project_dir).read_project()
    try:
        place = _resolve_place(project, place_id)
    except PlaceError as error:
        return {"place": None, "recipe": None, "trusted": False, "commands": [], "problem": str(error)}
    result: dict[str, Any] = {"place": place, "recipe": None, "trusted": True, "commands": [], "problem": "",
                              "live_url": str(place.get("url") or ""), "kind_words": ""}
    try:
        recipe = read_deploy_recipe(project_dir, place["id"])
    except PlaceError as error:
        result["problem"] = str(error)
        return result
    if recipe is None:
        if place.get("role") == "production":
            result["problem"] = (f"Playground doesn’t know how to copy releases to {place.get('name')} yet. "
                                 "Add a [deploy] recipe to .playground/run.toml.")
        return result
    result.update(recipe=recipe, trusted=recipe_trusted(project_dir, recipe), commands=recipe.commands(place),
                  kind_words=DEPLOY_KIND_WORDS[recipe.kind], live_url=recipe.health or result["live_url"])
    return result


# ───────────────────────────── the release lock ─────────────────────────────

_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def release_lock(project_dir: Path | str, what: str = "A release") -> Iterator[None]:
    """One release or rollback per project at a time, across threads and processes on this computer.

    Raises ReleaseBusy at once (never waits): a second Release press must not queue a second deploy.
    """
    root = str(Path(project_dir).resolve())
    with _PROCESS_LOCKS_GUARD:
        local = _PROCESS_LOCKS.setdefault(root, threading.Lock())
    if not local.acquire(blocking=False):
        raise ReleaseBusy("Another release is still running for this project. Wait for it to finish, then try "
                          "again.", step="approval")
    path = ProjectPaths(project_dir).local_dir / "release.lock"
    handle = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.seek(0)
                holder = {}
                with contextlib.suppress(ValueError):
                    holder = json.loads(handle.read() or "{}")
                since = _clock_words(holder.get("started", ""))
                raise ReleaseBusy(f"{holder.get('what') or 'Another release'}{' started ' + since if since else ''} "
                                  "is still running. Wait for it to finish, then try again.",
                                  step="approval") from None
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "started": now_iso(), "what": what}))
        handle.flush()
        yield
    finally:
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.truncate(0)
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        local.release()


def _clock_words(ts: str) -> str:
    try:
        moment = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ""
    return "at " + moment.strftime("%-I:%M %p") if os.name != "nt" else ""


# ───────────────────────────── restore points ─────────────────────────────

def _git(project_dir: Path | str, *args: str, timeout: float = 60, binary: bool = False) -> subprocess.CompletedProcess:
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("git")
    return subprocess.run([git, *args], cwd=str(project_dir), capture_output=True, text=not binary, timeout=timeout,
                          stdin=subprocess.DEVNULL,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})


def _has_git(project_dir: Path | str) -> bool:
    return shutil.which("git") is not None and (Path(project_dir) / ".git").exists()


def restore_tag(number: int) -> str:
    return f"{RESTORE_TAG_PREFIX}{int(number)}"


def restore_tags(project_dir: Path | str) -> dict[int, str]:
    """Restore point number → commit, from the project's git tags."""
    if not _has_git(project_dir):
        return {}
    try:
        out = _git(project_dir, "for-each-ref", "--format=%(refname:short) %(objectname) %(*objectname)",
                   f"refs/tags/{RESTORE_TAG_PREFIX.rstrip('-').rsplit('/', 1)[0]}/").stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    tags: dict[int, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith(RESTORE_TAG_PREFIX) and parts[0][len(RESTORE_TAG_PREFIX):].isdigit():
            tags[int(parts[0][len(RESTORE_TAG_PREFIX):])] = parts[2] if len(parts) > 2 else parts[1]
    return tags


SHARED_RESTORE_PREFIX = "playground/restore/"     # shared projects: playground/restore/<member>/<n>-<device>
_SHARED_RESTORE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/([0-9]{1,9})-[0-9a-f]{6}$")


def restore_refs(project_dir: Path | str) -> dict[str, tuple[int, str]]:
    """Every restore point this computer has the code for: id → (number, commit).

    The id is ``"N"`` for a point saved before the project was shared (tag ``playground/restore-N``), and
    ``"<member>/<N>-<device>"`` in a shared project (tag ``playground/restore/<member>/<N>-<device>``), so
    two teammates' restore points never clash, even when they pick the same number at the same moment.
    Teammates' restore points arrive as tags through sync.
    """
    refs: dict[str, tuple[int, str]] = {str(n): (n, c) for n, c in restore_tags(project_dir).items()}
    if not _has_git(project_dir):
        return refs
    try:
        out = _git(project_dir, "for-each-ref", "--format=%(refname) %(objectname) %(*objectname)",
                   f"refs/tags/{SHARED_RESTORE_PREFIX}").stdout
    except (OSError, subprocess.SubprocessError):
        return refs
    for line in out.splitlines():
        parts = line.split()
        name = parts[0][len("refs/tags/" + SHARED_RESTORE_PREFIX):] if parts else ""
        match = _SHARED_RESTORE_ID.match(name)
        if match and len(parts) >= 2:
            refs[name] = (int(match.group(1)), parts[2] if len(parts) > 2 else parts[1])
    return refs


def restore_point_tag(point_id: str | int) -> str:
    """The git tag of a restore point id (see ``restore_refs``)."""
    text = str(point_id)
    return restore_tag(int(text)) if text.isdigit() else f"{SHARED_RESTORE_PREFIX}{text}"


def _copy_name(point_id: str | int) -> str:
    return "restore-" + str(point_id).replace("/", "_")


def next_restore_number(project_dir: Path | str, store: ProjectStore | None = None) -> int:
    store = store or _store(project_dir)
    numbers = [int(e.data.get("restore_point")) for e in store.read_activity()
               if str(e.data.get("restore_point") or "").isdigit()]
    return max([*numbers, *(n for n, _c in restore_refs(project_dir).values()), 0]) + 1


def save_restore_point(project_dir: Path | str, title: str = "", *, store: ProjectStore | None = None) -> dict:
    """Commit what's about to go out and tag it ``playground/restore-N``.

    When nothing changed since the newest restore point (a retry, or the same
    code again), that point is used again. Without git there's still a number,
    but no code to go back to, and ``commit`` is empty.
    """
    store = store or _store(project_dir)
    if not _has_git(project_dir):
        number = next_restore_number(project_dir, store)
        return {"number": number, "id": str(number), "commit": "", "tag": "", "reused": False}

    def fail(words: str, proc: subprocess.CompletedProcess | None = None) -> ReleaseError:
        detail = _scrub((proc.stderr or proc.stdout or "") if proc is not None else "")[-600:]
        return ReleaseError(words, step="restore", detail=detail)

    dirty = _git(project_dir, "status", "--porcelain").stdout.strip()
    # Playground's own records (activity, threads, goals) change with every release; only the
    # project's code decides whether this is a new version.
    code_changed = _git(project_dir, "status", "--porcelain", "--", ".", ":(exclude).playground").stdout.strip()
    refs = restore_refs(project_dir)
    head = _git(project_dir, "rev-parse", "--verify", "-q", "HEAD").stdout.strip()
    if not code_changed and head and refs:
        newest = max(refs, key=lambda key: (refs[key][0], key))
        if refs[newest][1] == head:
            return {"number": refs[newest][0], "id": newest, "commit": head, "tag": restore_point_tag(newest),
                    "reused": True}
    number = next_restore_number(project_dir, store)
    if dirty or not head:
        _git(project_dir, "add", "-A")
        identity = [] if _git(project_dir, "config", "user.email").stdout.strip() else \
            ["-c", "user.name=Playground", "-c", "user.email=playground@localhost"]
        message = f"Restore point {number}: before releasing {title}".rstrip() if title else f"Restore point {number}"
        proc = _git(project_dir, *identity, "commit", "-q", "--no-gpg-sign", "--allow-empty", "-m", message)
        if proc.returncode != 0:
            raise fail("Playground couldn’t save the code with git, so it stopped before changing anything.", proc)
        head = _git(project_dir, "rev-parse", "HEAD").stdout.strip()
    tag = _shared_restore_tag(project_dir, number) or restore_tag(number)
    proc = _git(project_dir, "tag", tag, head)
    if proc.returncode != 0:
        raise fail(f"Playground couldn’t mark restore point {number} in git, so it stopped before changing "
                   "anything.", proc)
    point_id = tag[len(SHARED_RESTORE_PREFIX):] if tag.startswith(SHARED_RESTORE_PREFIX) else str(number)
    return {"number": number, "id": point_id, "commit": head, "tag": tag, "reused": False}


def _shared_restore_tag(project_dir: Path | str, number: int) -> str | None:
    """In a shared project, a tag that names who saved it and where (core.sync), so it never clashes."""
    try:
        from . import sync
        return sync.restore_tag_name(project_dir, number)
    except Exception:  # noqa: BLE001 - sharing isn't set up here: the plain tag
        return None


def _snapshots_dir(project_dir: Path | str) -> Path:
    directory = ProjectPaths(project_dir).local_dir / "snapshots"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)
    return directory


def _copies_dir(project_dir: Path | str, place_id: str) -> Path:
    return ProjectPaths(project_dir).local_dir / "releases" / re.sub(r"[^A-Za-z0-9_.-]", "-", place_id or "place")


def restore_points(project_dir: Path | str) -> list[dict]:
    """Every version that went live, newest first, with what going back to it can cover.

    Each: ``number, commit, ts, summary, goal, place, live`` (what's live now),
    ``code`` ("copy" | "tag" | "" when this computer can't put it back) and
    ``data`` (``{"snapshot", "path", "ts"}``: the data as it was while that
    version was live, saved when the next release replaced it; None when there
    is no copy on this computer).
    """
    store = _store(project_dir)
    entries = store.read_activity()
    refs = restore_refs(project_dir)
    points: dict[str, dict] = {}
    live: str | None = None
    data_for: dict[str, dict] = {}
    snapshots = ProjectPaths(project_dir).local_dir / "snapshots"
    for entry in entries:
        data = entry.data or {}
        state = str(data.get("state") or "live")
        number = data.get("restore_point") if entry.kind == "release" else data.get("went_back_to")
        if entry.kind not in ("release", "restore") or not str(number or "").isdigit():
            continue
        number = int(number)
        key = str((data.get("restore_id") if entry.kind == "release" else data.get("went_back_to_id")) or number)
        snapshot = str(data.get("snapshot") or "")
        if snapshot and live is not None:
            data_for[live] = {"snapshot": snapshot, "ts": entry.ts}
        if state != "live":
            continue
        if entry.kind == "release":
            point = points.setdefault(key, {"number": number, "id": key})
            point.update(commit=str(data.get("commit") or point.get("commit") or ""), ts=entry.ts,
                         summary=entry.summary, goal=entry.goal, place=str(data.get("place") or ""),
                         by=entry.actor or "")
        else:
            points.setdefault(key, {"number": number, "id": key, "commit": str(data.get("commit") or ""),
                                    "ts": entry.ts, "summary": entry.summary, "goal": "",
                                    "place": str(data.get("place") or ""), "by": ""})
        live = key
    result = []
    for key, point in points.items():
        place_id = point.get("place") or ""
        copy = _copies_dir(project_dir, place_id) / _copy_name(key) if place_id else None
        point["commit"] = point.get("commit") or refs.get(key, (0, ""))[1]
        point["code"] = "copy" if copy is not None and copy.is_dir() else ("tag" if key in refs else "")
        point["live"] = key == live
        data = data_for.get(key)
        if data is not None:
            path = snapshots / Path(data["snapshot"]).name
            data = {**data, "path": str(path)} if path.is_file() else None
        point["data"] = data
        result.append(point)
    return sorted(result, key=lambda p: (p["number"], str(p.get("ts") or "")), reverse=True)


def live_restore_point(project_dir: Path | str) -> int | None:
    return next((p["number"] for p in restore_points(project_dir) if p["live"]), None)


# ───────────────────────────── running things, quietly and safely ─────────────────────────────

def _scrub(text: str, hide: Iterable[str] = ()) -> str:
    """Hide secret values (the vault's, and anything shaped like a key) before output is kept or shown."""
    for value in sorted((v for v in hide if v and len(v) >= 4), key=len, reverse=True):
        text = text.replace(value, "[hidden]")
    return _LOG_SECRET_RE.sub("[hidden]", text)


class _Runner:
    """Runs one release's commands: own process group, timeout, output kept (scrubbed) in a local log."""

    def __init__(self, project_dir: Path, hide: Iterable[str] = ()) -> None:
        logs = ProjectPaths(project_dir).local_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.hide = [h for h in hide if h]
        self.log_path = logs / f"release-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}.log"
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        old = sorted(logs.glob("release-*.log"))[:-KEEP_RELEASE_LOGS]
        for path in old:
            with contextlib.suppress(OSError):
                path.unlink()

    def note(self, words: str) -> None:
        with open(self.log_path, "a") as handle:
            handle.write(_scrub(words, self.hide).rstrip() + "\n")

    def run(self, argv: list[str], *, label: str, cwd: Path | str | None = None, env: dict[str, str] | None = None,
            timeout: float = 600, stdin_path: Path | None = None, stdout_path: Path | None = None) -> tuple[int, str]:
        """Returns (exit code, scrubbed output). Exit code -1 means it ran out of time; 127, not found."""
        self.note(f"\n$ {label}")
        stdin = open(stdin_path, "rb") if stdin_path is not None else subprocess.DEVNULL
        stdout: Any = subprocess.PIPE
        if stdout_path is not None:
            stdout = os.fdopen(os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb")
        try:
            try:
                proc = subprocess.Popen(argv, cwd=str(cwd) if cwd else None, env=env, stdin=stdin, stdout=stdout,
                                        stderr=subprocess.PIPE if stdout_path is not None else subprocess.STDOUT,
                                        start_new_session=True)
            except FileNotFoundError:
                self.note("(not found)")
                return 127, f"{argv[0]}: not found"
            try:
                out, err = proc.communicate(timeout=timeout)
                code = proc.returncode
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    out, err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(OSError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    out, err = proc.communicate()
                code = -1
        finally:
            if stdin_path is not None:
                stdin.close()
            if stdout_path is not None:
                stdout.close()
        text = (out or b"") if stdout_path is None else (err or b"")
        words = _scrub(text.decode(errors="replace"), self.hide)
        self.note(words[-20000:] + f"\n(exit {code})")
        return code, words

    def tail(self, words: str, limit: int = 700) -> str:
        lines = [line for line in words.strip().splitlines() if line.strip()]
        return "\n".join(lines[-12:])[-limit:]


@dataclass
class _Ssh:
    argv: list[str]          # ssh and its options, no destination
    env: dict[str, str]      # the full environment for ssh and rsync
    dest: str                # user@host
    rsync_dest: str          # user@host, bracketed for IPv6
    wrapper: Path            # a script that runs ssh with these options, for rsync -e

    def command(self, remote: str) -> list[str]:
        return [*self.argv, "--", self.dest, remote]


def _ensure_certificate(project_id: str, member_id: str, key, required: bool) -> Path | None:
    """A current project certificate for this device, issued on the spot when this device holds the CA."""
    from . import security
    cert = key.certificate_path(project_id)
    if cert.exists():
        try:
            parsed = security.read_certificate(cert)
            end = security._parse_local_time(parsed.valid_to)
            if end is None or end.timestamp() > time.time() + 600:
                return cert
        except Exception:  # noqa: BLE001 - an unreadable certificate is replaced below, or skipped
            pass
    if member_id and security.ProjectCA.exists(project_id):
        try:
            security.ProjectCA.load(project_id).issue(member_id, key.public_key, out_path=cert)
            return cert
        except Exception as error:  # noqa: BLE001
            if required:
                raise ReleaseError(f"Playground couldn’t make a server certificate for this release: {error}",
                                   step="copy", retry=False) from None
            return cert if cert.exists() else None
    if required:
        raise ReleaseError("This release uses the project’s server certificates, and this computer doesn’t have a "
                           "current one. Ask the project’s owner to set up your access in Places.", step="copy",
                           retry=False)
    return cert if cert.exists() else None


def _ssh_context(place: dict, project, recipe: DeployRecipe, member_id: str, workdir: Path) -> _Ssh:
    ssh = shutil.which("ssh")
    if ssh is None:
        raise ReleaseError("This computer doesn’t have SSH installed, so it can’t reach the server.", step="copy",
                           retry=False)
    opts, env = ssh_options(place, project_id=project.id or None, timeout=15)
    if recipe.auth in ("auto", "certificate") and project.id:
        key = None
        with contextlib.suppress(Exception):
            from . import security
            key = security.device_key(create=False)
        if key is None and recipe.auth == "certificate":
            raise ReleaseError("This release uses the project’s server certificates, and this computer has no "
                               "Playground key yet. Set up access in Places first.", step="copy", retry=False)
        if key is not None:
            cert = _ensure_certificate(project.id, member_id, key, required=recipe.auth == "certificate")
            if cert is not None and f"CertificateFile={cert}" not in opts:
                opts += ["-o", f"CertificateFile={cert}"]
    if recipe.auth == "agent":
        # The person's own keys and SSH agent only: leave Playground's device key and certificate out.
        cleaned: list[str] = []
        skip = False
        for index, item in enumerate(opts):
            if skip:
                skip = False
                continue
            if item == "-i" or (item == "-o" and index + 1 < len(opts) and
                                opts[index + 1].startswith(("CertificateFile=", "NumberOfPasswordPrompts="))):
                skip = True
                continue
            cleaned.append(item)
        opts = cleaned + (["-o", "BatchMode=yes"] if "BatchMode=yes" not in cleaned else [])
        env = {}
    host = validate_host(place.get("host", ""))
    user = validate_user(place.get("user", ""))
    wrapper = workdir / "playground-ssh"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.join([ssh, *opts]) + ' "$@"\n')
    os.chmod(wrapper, 0o700)
    return _Ssh(argv=[ssh, *opts], env={**os.environ, **env}, dest=f"{user}@{host}",
                rsync_dest=f"{user}@[{host}]" if ":" in host else f"{user}@{host}", wrapper=wrapper)


def _connection_words(output: str) -> str | None:
    lowered = output.lower()
    for needles, _state, message in _FAILURES:
        if any(n.lower() in lowered for n in needles):
            return message
    return None


def _person_env(recipe: DeployRecipe, secrets_env: dict[str, str], extra: dict[str, str] | None = None) -> dict[str, str]:
    """For commands the person agreed to (deploy, CLIs, data): their own environment, plus the recipe's."""
    return {**os.environ, **recipe.env, **secrets_env, **(extra or {})}


def _vault_env(project_dir: Path, project, recipe: DeployRecipe) -> dict[str, str]:
    if not recipe.env_secrets:
        return {}
    from . import security
    try:
        vault = security.Vault(project_dir, project.id)
        return {name: vault.get(name) for name in recipe.env_secrets}
    except Exception as error:  # noqa: BLE001
        raise ReleaseError(f"The release needs {', '.join(recipe.env_secrets)} from the vault, and Playground "
                           f"couldn’t open it: {error}", step="copy", retry=False) from None


def _ignore(patterns: list[str]):
    def ignore(directory: str, names: list[str]) -> set[str]:
        return {n for n in names if any(fnmatch.fnmatch(n, p) for p in patterns)}
    return ignore


# ───────────────────────────── the steps of a release ─────────────────────────────

class _Steps:
    """The progress a person sees: each step waiting, running, done, failed or skipped."""

    def __init__(self, kind: str, project_dir: Path, place: dict, steps: list[tuple[str, str]],
                 on_step: Callable[..., Any] | None, bus: events.EventBus) -> None:
        self.kind, self.project_dir, self.place, self.on_step, self.bus = kind, project_dir, place, on_step, bus
        self.steps = [{"id": sid, "label": label, "state": "waiting", "detail": ""} for sid, label in steps]

    def set(self, step_id: str, state: str, detail: str = "") -> None:
        step = next((s for s in self.steps if s["id"] == step_id), None)
        if step is None:
            return
        step["state"], step["detail"] = state, detail
        if self.on_step is not None:
            try:
                self.on_step(step_id, state, step["label"], detail)
            except Exception:  # noqa: BLE001 - a broken progress callback must not break a release
                pass
        self.bus.publish(RELEASE_STEP, project=str(self.project_dir), kind=self.kind, place=self.place.get("id"),
                         step=step_id, state=state, label=step["label"], detail=detail)

    def running(self) -> str:
        return next((s["id"] for s in self.steps if s["state"] == "running"), "")


def _health_target(recipe: DeployRecipe | None, place: dict, published: str = "") -> str:
    if recipe is not None and recipe.health:
        return recipe.health
    return str(place.get("url") or published or "")


def check_health(url: str, *, timeout_s: float = 60.0, expect_status: int = 0, expect_text: str = "",
                 interval_s: float = 2.0, request_timeout_s: float = 10.0) -> dict:
    """Ask the live address until it answers well, or time runs out. Never raises.

    Good means status ``expect_status`` (any 2xx or 3xx when 0) and, when
    given, ``expect_text`` somewhere in the first 256 KB of the page.
    """
    started = time.monotonic()
    deadline = started + max(0.0, timeout_s)
    host = url.split("//", 1)[-1].split("/", 1)[0]
    message = f"{host} didn’t answer."
    status: int | None = None
    while True:
        attempt = time.monotonic()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Playground release check",
                                                           "Cache-Control": "no-cache"})
            with urllib.request.urlopen(request, timeout=request_timeout_s) as response:
                status = response.status
                body = response.read(256 * 1024).decode(errors="replace") if expect_text else ""
        except urllib.error.HTTPError as error:
            status, body = error.code, ""
            error.close()
        except urllib.error.URLError as error:
            status, body = None, ""
            reason = error.reason
            if isinstance(reason, ssl.SSLError):
                message = f"{host}’s security certificate isn’t valid, so Playground can’t trust the answer."
            elif isinstance(reason, ConnectionRefusedError):
                message = f"{host} isn’t accepting connections."
            else:
                message = f"{host} didn’t answer."
        except (TimeoutError, OSError):
            status, body = None, ""
            message = f"{host} didn’t answer in time."
        if status is not None:
            good = status == expect_status if expect_status else 200 <= status < 400
            if good and (not expect_text or expect_text in body):
                return {"ok": True, "status": status, "url": url, "seconds": round(time.monotonic() - started, 1),
                        "message": f"{host} answered in {max(0.1, round(time.monotonic() - attempt, 1))} s."}
            if not good:
                message = f"{host} answered with {status} instead of a working page."
            else:
                message = f"{host} answered, but the page doesn’t show “{expect_text}” yet."
        if time.monotonic() + interval_s > deadline:
            break
        time.sleep(interval_s)
    waited = int(round(time.monotonic() - started))
    return {"ok": False, "status": status, "url": url, "seconds": waited,
            "message": f"{message} Playground waited {waited} seconds." if waited >= 2 else message}


def _build(recipe: DeployRecipe, root: Path, runner: _Runner) -> None:
    if not recipe.build:
        return
    from .preview import clean_env
    # The build is the project's own code (agents write it), so it gets no credentials and no SSH agent.
    code, out = runner.run(["/bin/sh", "-c", recipe.build], label=f"build: {recipe.build}", cwd=root,
                           env=clean_env(recipe.env), timeout=recipe.timeout_s)
    if code != 0:
        words = "Building it took too long, so nothing was copied." if code == -1 else \
            "Building it failed, so nothing was copied. The details say why."
        raise ReleaseError(words, step="copy", detail=runner.tail(out))


def _source_dir(recipe: DeployRecipe, root: Path) -> Path:
    source = (root / recipe.source).resolve()
    if not source.is_dir():
        raise ReleaseError(f"There’s no {recipe.source} folder to copy. Does the build put its output somewhere "
                           "else? Set source in run.toml’s [deploy].", step="copy", retry=False)
    return source


def _ssh_failure(code: int, out: str, place: dict, recipe: DeployRecipe, runner: _Runner, doing: str) -> ReleaseError:
    name = place.get("name") or "the server"
    words = _connection_words(out)
    lowered = out.lower()
    if code == -1:
        words = f"{doing.capitalize()} took longer than {int(recipe.timeout_s // 60) or 1} minutes, so Playground " \
                "stopped it."
    elif code == 41 or ("rsync" in lowered and ("command not found" in lowered or "not found" in lowered)):
        words = f"{name} doesn’t have rsync, which Playground copies releases with. Install it there " \
                "(for example: sudo apt install rsync), then try again."
    elif code == 40 or "permission denied (13)" in lowered or "mkdir" in lowered and "failed" in lowered:
        words = f"Playground can’t write to {recipe.path} on {name}. Check the folder exists and " \
                f"{place.get('user')} can write to it."
    elif "no space left" in lowered:
        words = f"{name} is out of disk space."
    elif words is None:
        words = f"Something went wrong while {doing}. The details may help."
    return ReleaseError(words, step="copy", detail=runner.tail(out))


def _deploy_ssh(recipe: DeployRecipe, place: dict, source: Path, ssh: _Ssh, runner: _Runner, *,
                dry_run: bool = False) -> list[str]:
    """rsync the release into place, then restart. Returns rsync's list of changes when dry_run."""
    rsync = shutil.which("rsync")
    if rsync is None:
        raise ReleaseError("This computer doesn’t have rsync, which Playground copies releases with.", step="copy",
                           retry=False)
    folder = shlex.quote(recipe.path)
    prepare = (f"test -d {folder} || echo playground-new-folder; " if dry_run else f"mkdir -p -- {folder} || exit 40; ") + \
        "command -v rsync >/dev/null 2>&1 || exit 41; echo playground-ready"
    code, out = runner.run(ssh.command(prepare), label=f"prepare {recipe.path} on {place.get('name')}", env=ssh.env,
                           timeout=60)
    if code != 0 or "playground-ready" not in out:
        raise _ssh_failure(code, out, place, recipe, runner, "getting the server ready")
    # --checksum: a file edited within the same second at the same size must still go out.
    argv = [rsync, "-rlptz", "--checksum"]
    if recipe.delete:
        argv.append("--delete")
    if dry_run:
        argv += ["-n", "-v"]
    for pattern in recipe.excludes:
        argv.append(f"--exclude={pattern}")
    argv += ["-e", str(ssh.wrapper), "--", f"{source}/", f"{ssh.rsync_dest}:{recipe.path}/"]
    code, out = runner.run(argv, label=f"copy to {place.get('name')}", env=ssh.env, timeout=recipe.timeout_s)
    if code != 0:
        raise _ssh_failure(code, out, place, recipe, runner, "copying")
    if dry_run:
        skip = ("sending incremental", "building file list", "sent ", "total size", "Transfer starting", "./")
        return [line for line in out.splitlines() if line.strip() and not line.startswith(skip)][:200]
    if recipe.restart:
        code, out = runner.run(ssh.command(f"cd -- {folder} && {recipe.restart}"),
                               label=f"restart on {place.get('name')}", env=ssh.env, timeout=recipe.timeout_s)
        if code != 0:
            failure = _ssh_failure(code, out, place, recipe, runner, "restarting")
            if _connection_words(out) is None and code != -1:
                failure = ReleaseError(f"The files reached {place.get('name')}, but the restart command stopped "
                                       f"with an error (exit {code}).", step="copy", detail=runner.tail(out))
            raise failure
    return []


def _deploy_cli(recipe: DeployRecipe, source: Path, cwd: Path, env: dict[str, str], runner: _Runner) -> str:
    tool, words = HOSTING_CLIS[recipe.kind]
    exe = shutil.which(tool)
    if exe is None:
        raise ReleaseError(f"{words}’s command-line tool ({tool}) isn’t installed on this computer. Install it and "
                           f"sign in once ({tool} login), then try again.", step="copy", retry=False)
    if recipe.kind == "vercel":
        argv = [exe, "deploy", str(source), "--prod", "--yes"]
    elif recipe.kind == "netlify":
        argv = [exe, "deploy", "--prod", "--dir", str(source)] + (["--site", recipe.site] if recipe.site else [])
    else:
        argv = [exe, "pages", "deploy", str(source), "--project-name", recipe.site,
                "--branch", recipe.branch or "main", "--commit-dirty=true"]
    code, out = runner.run(argv, label=f"{words}: publish", cwd=cwd, env=env, timeout=recipe.timeout_s)
    if code != 0:
        lowered = out.lower()
        if any(w in lowered for w in ("not logged in", "log in", "login", "unauthorized", "authenticat")):
            message = f"{words} says this computer isn’t signed in. Run {tool} login once, then try again."
        elif code == -1:
            message = f"{words} took too long, so Playground stopped it."
        else:
            message = f"{words} couldn’t publish it. The details say why."
        raise ReleaseError(message, step="copy", detail=runner.tail(out))
    found = re.findall(r"https?://[^\s\"'<>]+", out)
    return found[-1].rstrip(".,)") if found else ""


def _deploy_command(recipe: DeployRecipe, place: dict, cwd: Path, env: dict[str, str], runner: _Runner) -> None:
    code, out = runner.run(["/bin/sh", "-c", recipe.command], label=f"deploy: {recipe.command}", cwd=cwd, env=env,
                           timeout=recipe.timeout_s)
    if code != 0:
        message = "The release command took too long, so Playground stopped it." if code == -1 else \
            f"The release command stopped with an error (exit {code})."
        raise ReleaseError(message, step="copy", detail=runner.tail(out))


def _command_env(place: dict, recipe: DeployRecipe, source: Path, number: int | None, commit: str,
                 ssh: _Ssh | None) -> dict[str, str]:
    extra = {"PLAYGROUND_PLACE_ID": str(place.get("id") or ""), "PLAYGROUND_PLACE_NAME": str(place.get("name") or ""),
             "PLAYGROUND_RELEASE_DIR": str(source), "PLAYGROUND_COMMIT": commit,
             "PLAYGROUND_RESTORE_POINT": str(number or ""), "PLAYGROUND_LIVE_URL": str(place.get("url") or "")}
    if place.get("kind") == "ssh":
        extra.update(PLAYGROUND_PLACE_HOST=str(place.get("host") or ""), PLAYGROUND_PLACE_USER=str(place.get("user") or ""),
                     PLAYGROUND_PLACE_PORT=str(place.get("port") or 22),
                     PLAYGROUND_PLACE_PATH=recipe.path or str(place.get("path") or ""))
    if ssh is not None:
        extra["PLAYGROUND_SSH"] = str(ssh.wrapper)
    return extra


def _ship(recipe: DeployRecipe, place: dict, root: Path, source: Path, ssh: _Ssh | None, runner: _Runner,
          secrets_env: dict[str, str], number: int | None, commit: str) -> str:
    """Copy one prepared release to its place. Returns an address the hosting tool printed, if any."""
    if recipe.kind == "ssh":
        assert ssh is not None
        _deploy_ssh(recipe, place, source, ssh, runner)
        return ""
    env = _person_env(recipe, secrets_env, _command_env(place, recipe, source, number, commit, ssh))
    if recipe.kind == "command":
        _deploy_command(recipe, place, root, env, runner)
        return ""
    return _deploy_cli(recipe, source, root, env, runner)


def _keep_copy(project_dir: Path, place_id: str, number: int | str, source: Path, recipe: DeployRecipe) -> None:
    """Keep exactly what went out, so going back to it needs no rebuild. The last few only.

    ``number`` is the restore point's id (see ``restore_refs``)."""
    base = _copies_dir(project_dir, place_id)
    target = base / _copy_name(number)
    try:
        base.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        staging = base / f".{_copy_name(number)}-{secrets.token_hex(3)}"
        shutil.copytree(source, staging, symlinks=True, ignore=_ignore(recipe.excludes))
        os.replace(staging, target)
        kept = sorted((p for p in base.glob("restore-*") if p.is_dir()), key=lambda p: p.stat().st_mtime)
        for old in kept[:-KEEP_RELEASE_COPIES]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        # Only a convenience: without it, going back rebuilds from the git tag.
        shutil.rmtree(target, ignore_errors=True)


def _take_snapshot(project_dir: Path, recipe: DeployRecipe, place: dict, ssh: _Ssh | None, runner: _Runner,
                   secrets_env: dict[str, str], name: str) -> str:
    """Save the data with the recipe's snapshot command. Returns the file name (in local/snapshots)."""
    path = _snapshots_dir(project_dir) / name
    if recipe.data_where == "server":
        assert ssh is not None
        argv, env = ssh.command(recipe.snapshot), ssh.env
    else:
        argv, env = ["/bin/sh", "-c", recipe.snapshot], _person_env(recipe, secrets_env)
    code, err = runner.run(argv, label=f"save the data: {recipe.snapshot}", cwd=project_dir, env=env,
                           timeout=recipe.timeout_s, stdout_path=path)
    size = path.stat().st_size if path.exists() else 0
    if code != 0 or size == 0:
        with contextlib.suppress(OSError):
            path.unlink()
        words = _connection_words(err) if code != 0 else None
        reason = words or ("The copy came back empty." if code == 0 else f"The command stopped (exit {code}).")
        raise ReleaseError(f"Playground couldn’t save a copy of the data, so it stopped before changing anything. "
                           f"{reason}", step="restore", detail=runner.tail(err))
    kept = sorted(path.parent.glob("*.snapshot"), key=lambda p: p.stat().st_mtime)
    for old in kept[:-KEEP_SNAPSHOTS]:
        with contextlib.suppress(OSError):
            old.unlink()
    return name


def _restore_snapshot(project_dir: Path, recipe: DeployRecipe, place: dict, ssh: _Ssh | None, runner: _Runner,
                      secrets_env: dict[str, str], snapshot: Path) -> None:
    if recipe.data_where == "server":
        assert ssh is not None
        argv, env = ssh.command(recipe.restore_data), ssh.env
    else:
        argv, env = ["/bin/sh", "-c", recipe.restore_data], _person_env(recipe, secrets_env)
    code, out = runner.run(argv, label=f"put the data back: {recipe.restore_data}", cwd=project_dir, env=env,
                           timeout=recipe.timeout_s, stdin_path=snapshot)
    if code != 0:
        raise ReleaseError("The code went back, but putting the data back stopped with an error. The data may be "
                           "partly restored; the copy from just before is kept.", step="data", detail=runner.tail(out))


def _snapshot_name(number: int | str, place_id: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{re.sub(r'[^A-Za-z0-9_.-]', '-', str(number))}-{re.sub(r'[^A-Za-z0-9_.-]', '-', place_id)}-{stamp}.snapshot"


def _export_tree(project_dir: Path, ref: str, dest: Path) -> Path:
    """The project as it was at ``ref`` (from git), in ``dest``. node_modules is borrowed for the build."""
    proc = _git(project_dir, "archive", "--format=tar", ref, timeout=300, binary=True)
    if proc.returncode != 0:
        raise ReleaseError("Playground couldn’t get that version’s code from git.", step="prepare",
                           detail=_scrub(proc.stderr.decode(errors="replace"))[-600:], retry=False)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as archive:
        try:
            archive.extractall(dest, filter="data")
        except TypeError:  # pragma: no cover - Pythons before the extraction filters
            archive.extractall(dest)
    modules = Path(project_dir) / "node_modules"
    if modules.is_dir() and not (dest / "node_modules").exists():
        with contextlib.suppress(OSError):
            os.symlink(modules, dest / "node_modules")
    return dest


def _needs_ssh(recipe: DeployRecipe | None, place: dict) -> bool:
    if recipe is None or place.get("kind") != "ssh":
        return False
    data_on_server = bool(recipe.snapshot or recipe.restore_data) and recipe.data_where == "server"
    return recipe.kind in ("ssh", "command") or data_on_server


def _place_words(place: dict) -> str:
    return str(place.get("name") or "Production")


def _finish(store: ProjectStore, kind: str, place: dict, ok: bool, **payload: Any) -> None:
    store.bus.publish(RELEASE_FINISHED, project=str(store.root), kind=kind, place=place.get("id"), ok=ok, **payload)


# ───────────────────────────── release ─────────────────────────────

def release(project_dir: Path | str, place_id: str | None = None, ticket: ReleaseTicket | None = None,
            goal_id: str | None = None, *, approved_by: str = "", title: str = "", dry_run: bool = False,
            trust_recipe: bool = False, on_step: Callable[..., Any] | None = None,
            bus: events.EventBus | None = None) -> dict:
    """Release what's here now to a place, the protected way.

    1. Production needs ``ticket`` from ``authorize_release`` (Review); it's spent here.
    2. A restore point: a git commit and tag ``playground/restore-N``, plus a
       copy of the data when the recipe has ``snapshot``.
    3. Copy it with the place's ``[deploy]`` recipe (ssh + rsync, a command, or a hosting CLI).
    4. Check the live address answers.
    5. History (activity.jsonl), the goal set Live, and ``release.step`` / ``release.finished`` events.

    ``on_step(step, state, label, detail)`` reports progress from this thread. ``dry_run`` changes
    nothing anywhere: it returns the ``plan`` (and, for ssh, rsync's list of ``changes``) and needs
    no ticket. A recipe nobody has agreed to on this computer raises RecipeNotTrusted unless
    ``trust_recipe`` (the person just read ``release_plan``'s commands and said yes).
    Raises ReleaseError (with ``step``) when something stops it; ReleaseBusy when another is running.
    """
    project_dir = Path(project_dir)
    store = _store(project_dir, bus)
    project = store.read_project()
    place = _resolve_place(project, place_id)
    production = is_protected(place)
    recipe = read_deploy_recipe(project_dir, place["id"])
    if production and recipe is None:
        raise ReleaseError(f"Playground doesn’t know how to copy releases to {_place_words(place)} yet. Add a "
                           "[deploy] recipe to .playground/run.toml.", step="copy", retry=False)
    goal = None
    if goal_id:
        try:
            goal = store.read_goal(goal_id)
        except Exception:  # noqa: BLE001 - NotFound and friends
            raise PlaceError("That goal isn’t in this project.") from None
    title = title or (goal.title if goal is not None else "the latest changes")
    if dry_run:
        return _dry_run(project_dir, store, project, place, recipe, title, approved_by)
    if recipe is not None and not recipe_trusted(project_dir, recipe):
        if not trust_recipe:
            raise RecipeNotTrusted(f"Releasing to {_place_words(place)} runs these for the first time on this "
                                   "computer. Read them, then agree to run them.", recipe.commands(place))
    steps = _Steps("release", project_dir, place, [("restore", "Saving a restore point"),
                                                   ("copy", f"Copying to {_place_words(place)}"),
                                                   ("check", "Checking it’s up")], on_step, store.bus)
    with release_lock(project_dir, f"A release to {_place_words(place)}"):
        if production:
            guard_write(place, actor="person", ticket=ticket)
        if recipe is not None and trust_recipe:
            _remember_recipe(project_dir, recipe)
        approved_by = approved_by or (ticket.approved_by if ticket else "") or \
            (project.owner.id if project.owner else "")
        started = time.monotonic()
        secrets_env = _vault_env(project_dir, project, recipe) if recipe is not None else {}
        runner = _Runner(project_dir, hide=secrets_env.values())
        runner.note(f"Release of {title} to {_place_words(place)}, approved by {approved_by or 'the owner'}")
        point: dict = {"number": None, "commit": ""}
        snapshot = ""
        with tempfile.TemporaryDirectory(prefix="playground-release-") as work:
            try:
                steps.set("restore", "running")
                point = save_restore_point(project_dir, title, store=store)
                ssh = None
                if _needs_ssh(recipe, place):
                    ssh = _ssh_context(place, project, recipe, approved_by, Path(work))
                if recipe is not None and recipe.snapshot:
                    steps.set("restore", "running", "Saving a copy of the data")
                    snapshot = _take_snapshot(project_dir, recipe, place, ssh, runner, secrets_env,
                                              _snapshot_name(f"release-{point['number']}", place["id"]))
                steps.set("restore", "done", f"Restore point {point['number']}" +
                          (", with a copy of the data" if snapshot else ""))
                published = ""
                if recipe is not None:
                    steps.set("copy", "running", "Building it first" if recipe.build else "")
                    _build(recipe, project_dir, runner)
                    source = _source_dir(recipe, project_dir)
                    if recipe.build:
                        steps.set("copy", "running")
                    published = _ship(recipe, place, project_dir, source, ssh, runner, secrets_env,
                                      point["number"], point["commit"])
                    if recipe.kind != "command":
                        _keep_copy(project_dir, place["id"], point.get("id") or point["number"], source, recipe)
                    steps.set("copy", "done")
                else:
                    steps.set("copy", "skipped", "Nothing to copy: it runs on this computer.")
                url = _health_target(recipe, place, published)
                health: dict = {}
                if url:
                    steps.set("check", "running", url)
                    health = check_health(url, timeout_s=recipe.health_timeout_s if recipe else 60.0,
                                          expect_status=recipe.expect_status if recipe else 0,
                                          expect_text=recipe.expect_text if recipe else "")
                    if not health["ok"]:
                        raise ReleaseError(f"It went out, but {health['message']}", step="check",
                                           detail=f"Checked {url}")
                    steps.set("check", "done", health["message"])
                else:
                    steps.set("check", "skipped", "There’s no live address to check. Add one in Places.")
            except ReleaseError as error:
                error.step = error.step or steps.running() or "copy"
                error.restore_point = point.get("number")
                steps.set(error.step, "failed", str(error))
                runner.note(f"Stopped: {error}")
                store.log(f"The release of {title} to {_place_words(place)} stopped", kind="release",
                          actor=approved_by, goal=goal_id or "",
                          data={"restore_point": point.get("number"), "commit": point.get("commit"),
                                "place": place["id"], "state": "failed", "step": error.step,
                                "snapshot": snapshot or None, "detail": str(error)})
                _finish(store, "release", place, False, step=error.step, error=str(error),
                        restore_point=point.get("number"), goal_id=goal_id)
                raise
        number = point["number"]
        store.log(f"Released {title} to {_place_words(place)}", kind="release", actor=approved_by, goal=goal_id or "",
                  data={"restore_point": number, "restore_id": point.get("id") or str(number),
                        "commit": point["commit"], "place": place["id"], "state": "live",
                        "snapshot": snapshot or None, "url": url or None, "detail": f"Restore point {number}"})
        if goal is not None:
            # No note: the rail says "Live since …" from the goal's own updated time, so it stays true.
            store.update_goal(goal.id, lambda g: (setattr(g, "status", GoalStatus.LIVE),
                                                  setattr(g, "status_note", "")))
            checked = f" {health['message']}" if health.get("ok") else ""
            store.append_thread(goal.id, ThreadEntry(
                kind="message", role="system", by="playground",
                text=f"Released to {_place_words(place)}. Restore point {number} was saved first, so you can go "
                     f"back from History.{checked}", data={"released": True, "restore_point": number}))
        result = {"ok": True, "kind": "release", "dry_run": False, "restore_point": number,
                  "restore_id": point.get("id") or str(number), "commit": point["commit"],
                  "place": place["id"], "url": url, "snapshot": snapshot or None, "health": health,
                  "steps": steps.steps, "seconds": round(time.monotonic() - started, 1),
                  "log": str(runner.log_path.relative_to(project_dir))}
        _finish(store, "release", place, True, restore_point=number, url=url, goal_id=goal_id)
        return result


def _dry_run(project_dir: Path, store: ProjectStore, project, place: dict, recipe: DeployRecipe | None, title: str,
             approved_by: str) -> dict:
    """What a release would do, touching nothing. For ssh, rsync -n lists what would change."""
    plan: list[dict] = []
    number = next_restore_number(project_dir, store)
    plan.append({"step": "restore", "words": f"Save restore point {number} (git tag "
                                             f"{_shared_restore_tag(project_dir, number) or restore_tag(number)})"
                 if _has_git(project_dir) else f"Number restore point {number} (no git here, so no code copy)"})
    result: dict[str, Any] = {"ok": True, "kind": "release", "dry_run": True, "place": place["id"],
                              "restore_point": number, "plan": plan, "changes": [], "problem": "",
                              "trusted": recipe is None or recipe_trusted(project_dir, recipe)}
    if recipe is None:
        plan.append({"step": "copy", "words": "Nothing to copy: it runs on this computer."})
        return result
    if recipe.snapshot:
        plan.append({"step": "restore", "words": f"Save a copy of the data with: {recipe.snapshot}"})
    if recipe.build:
        plan.append({"step": "copy", "words": f"Build it with: {recipe.build}"})
    for where, what in recipe.commands(place):
        if what not in (recipe.snapshot, recipe.build) and not what.endswith("(only when you choose to put data back)"):
            plan.append({"step": "copy", "words": f"{where}: {what}"})
    url = _health_target(recipe, place)
    plan.append({"step": "check", "words": f"Check {url} answers" if url else "No live address to check"})
    if recipe.kind == "ssh":
        source = (project_dir / recipe.source).resolve()
        if not source.is_dir():
            result["problem"] = f"There’s no {recipe.source} folder yet; the build makes it."
            return result
        with tempfile.TemporaryDirectory(prefix="playground-dry-") as work:
            runner = _Runner(project_dir)
            try:
                ssh = _ssh_context(place, project, recipe, approved_by or (project.owner.id if project.owner else ""),
                                   Path(work))
                result["changes"] = _deploy_ssh(recipe, place, source, ssh, runner, dry_run=True)
            except ReleaseError as error:
                result["problem"] = str(error)
    return result


# ───────────────────────────── going back ─────────────────────────────

def _find_point(project_dir: Path | str, number: int | str) -> dict:
    """A restore point by id (``"nick/14-ab12cd"``, or ``"14"``) or by number. Two teammates can save the same
    number at the same moment; by number, the one this computer has the code for (then the newest) is meant."""
    points = restore_points(project_dir)
    text = str(number)
    point = next((p for p in points if p.get("id") == text), None)
    if point is None and text.isdigit():
        same = [p for p in points if p["number"] == int(text)]
        same.sort(key=lambda p: (bool(p.get("code")), str(p.get("ts") or "")), reverse=True)
        point = same[0] if same else None
    if point is None:
        raise PlaceError(f"There’s no restore point {number} in History.")
    return point


def authorize_rollback(project_dir: Path | str, restore_n: int, approved_by: str,
                       place_id: str | None = None) -> ReleaseTicket:
    """A ticket to put restore point N back on production: an owner or reviewer, asked in History."""
    point = _find_point(project_dir, restore_n)
    project = _store(project_dir).read_project()
    place = _resolve_place(project, place_id or point.get("place") or None)
    review = {"id": _copy_name(point.get("id") or point["number"]), "ready": True, "destination": place["id"],
              "kind": "rollback", "restore_point": int(point["number"])}
    return authorize_release(project_dir, review, place["id"], approved_by)


def rollback_plan(project_dir: Path | str, restore_n: int, place_id: str | None = None) -> dict:
    """What going back to N covers, for the honest note in History. Never raises."""
    try:
        point = _find_point(project_dir, restore_n)
        project = _store(project_dir).read_project()
        place = _resolve_place(project, place_id or point.get("place") or None)
        recipe = read_deploy_recipe(project_dir, place["id"])
    except PlaceError as error:
        return {"ok": False, "problem": str(error)}
    problem = ""
    if not point["code"]:
        problem = f"This computer doesn’t have the code for restore point {restore_n}."
    elif recipe is None and is_protected(place):
        problem = f"Playground doesn’t know how to copy releases to {_place_words(place)} yet."
    elif point["live"]:
        problem = f"Restore point {restore_n} is what’s live now."
    data = point.get("data")
    return {"ok": not problem, "problem": problem, "point": point, "place": place,
            "data": data if data and recipe is not None and recipe.restore_data else None,
            "data_saved": bool(recipe is not None and recipe.snapshot),
            "trusted": recipe is None or recipe_trusted(project_dir, recipe),
            "commands": recipe.commands(place) if recipe is not None else []}


def rollback(project_dir: Path | str, restore_n: int, ticket: ReleaseTicket | None = None, *,
             place_id: str | None = None, approved_by: str = "", restore_data: bool = False, dry_run: bool = False,
             trust_recipe: bool = False, on_step: Callable[..., Any] | None = None,
             bus: events.EventBus | None = None) -> dict:
    """Put restore point N back: its code (the copy kept at release, else rebuilt from its git tag),
    and its data too only when ``restore_data`` (the person confirmed losing what changed since).

    Production needs a ticket from ``authorize_rollback``. Before anything changes, the data as it is
    now is saved (when the recipe can), so going back can itself be undone.
    """
    project_dir = Path(project_dir)
    store = _store(project_dir, bus)
    project = store.read_project()
    point = _find_point(project_dir, restore_n)
    number = point["number"]
    place = _resolve_place(project, place_id or point.get("place") or None)
    production = is_protected(place)
    recipe = read_deploy_recipe(project_dir, place["id"])
    if recipe is None:
        raise ReleaseError(f"Playground doesn’t know how to copy releases to {_place_words(place)} yet. Add a "
                           "[deploy] recipe to .playground/run.toml.", step="prepare", retry=False)
    if point["live"] and not restore_data:
        raise ReleaseError(f"Restore point {number} is what’s live now.", step="prepare", retry=False)
    if not point["code"]:
        raise ReleaseError(f"This computer doesn’t have the code for restore point {number}, so it can’t put it "
                           "back.", step="prepare", retry=False)
    data = point.get("data")
    if restore_data and not data:
        raise ReleaseError(f"Playground doesn’t have a copy of the data for restore point {number}, so only the "
                           "code can go back.", step="data", retry=False)
    if restore_data and not recipe.restore_data:
        raise ReleaseError("run.toml’s [deploy] has no restore_data command, so Playground can’t put the data back.",
                           step="data", retry=False)
    if dry_run:
        plan = [{"step": "prepare", "words": f"Use the copy of restore point {number} kept at release"
                 if point["code"] == "copy" else f"Rebuild restore point {number} from git tag "
                                                 f"{restore_point_tag(point.get('id') or number)}"}]
        if recipe.snapshot:
            plan.append({"step": "prepare", "words": f"Save the data as it is now with: {recipe.snapshot}"})
        plan.append({"step": "copy", "words": f"Copy it to {_place_words(place)}"})
        if restore_data:
            plan.append({"step": "data", "words": f"Put the data back to {data['ts']} with: {recipe.restore_data}"})
        plan.append({"step": "check", "words": f"Check {_health_target(recipe, place) or 'nothing'} answers"})
        return {"ok": True, "kind": "rollback", "dry_run": True, "restore_point": number, "place": place["id"],
                "plan": plan}
    if not recipe_trusted(project_dir, recipe) and not trust_recipe:
        raise RecipeNotTrusted(f"Going back on {_place_words(place)} runs these for the first time on this computer. "
                               "Read them, then agree to run them.", recipe.commands(place))
    step_list = [("prepare", f"Getting restore point {number} ready"), ("copy", f"Copying to {_place_words(place)}")]
    if restore_data:
        step_list.append(("data", "Putting the data back"))
    step_list.append(("check", "Checking it’s up"))
    steps = _Steps("rollback", project_dir, place, step_list, on_step, store.bus)
    with release_lock(project_dir, f"Going back on {_place_words(place)}"):
        if production:
            guard_write(place, actor="person", ticket=ticket)
        if trust_recipe:
            _remember_recipe(project_dir, recipe)
        approved_by = approved_by or (ticket.approved_by if ticket else "") or \
            (project.owner.id if project.owner else "")
        started = time.monotonic()
        secrets_env = _vault_env(project_dir, project, recipe)
        runner = _Runner(project_dir, hide=secrets_env.values())
        runner.note(f"Going back to restore point {number} on {_place_words(place)}, approved by {approved_by}")
        before = ""
        url = ""
        health: dict = {}
        with tempfile.TemporaryDirectory(prefix="playground-rollback-") as work:
            work_dir = Path(work)
            try:
                steps.set("prepare", "running")
                ssh = _ssh_context(place, project, recipe, approved_by, work_dir) if _needs_ssh(recipe, place) else None
                if point["code"] == "copy" and recipe.kind != "command":
                    root = project_dir
                    source = _copies_dir(project_dir, place["id"]) / _copy_name(point.get("id") or number)
                else:
                    root = _export_tree(project_dir, point["commit"] or restore_point_tag(point.get("id") or number),
                                        work_dir / "tree")
                    _build(recipe, root, runner)
                    source = _source_dir(recipe, root)
                if recipe.snapshot:
                    steps.set("prepare", "running", "Saving the data as it is now")
                    before = _take_snapshot(project_dir, recipe, place, ssh, runner, secrets_env,
                                            _snapshot_name(f"before-going-back-to-{number}", place["id"]))
                steps.set("prepare", "done", "The data as it is now is saved" if before else "")
                steps.set("copy", "running")
                published = _ship(recipe, place, root, source, ssh, runner, secrets_env, number,
                                  point.get("commit") or "")
                steps.set("copy", "done")
                if restore_data:
                    steps.set("data", "running", f"Back to how it was {data['ts']}")
                    _restore_snapshot(project_dir, recipe, place, ssh, runner, secrets_env, Path(data["path"]))
                    steps.set("data", "done")
                url = _health_target(recipe, place, published)
                if url:
                    steps.set("check", "running", url)
                    health = check_health(url, timeout_s=recipe.health_timeout_s, expect_status=recipe.expect_status,
                                          expect_text=recipe.expect_text)
                    if not health["ok"]:
                        raise ReleaseError(f"It went back, but {health['message']}", step="check",
                                           detail=f"Checked {url}")
                    steps.set("check", "done", health["message"])
                else:
                    steps.set("check", "skipped", "There’s no live address to check. Add one in Places.")
            except ReleaseError as error:
                error.step = error.step or steps.running() or "copy"
                error.restore_point = number
                steps.set(error.step, "failed", str(error))
                runner.note(f"Stopped: {error}")
                store.log(f"Going back to restore point {number} on {_place_words(place)} stopped", kind="restore",
                          actor=approved_by,
                          data={"went_back_to": number, "went_back_to_id": point.get("id") or str(number),
                                "place": place["id"], "state": "failed", "step": error.step,
                                "snapshot": before or None, "detail": str(error)})
                _finish(store, "rollback", place, False, step=error.step, error=str(error), restore_point=number)
                raise
        store.log(f"Went back to restore point {number} on {_place_words(place)}", kind="restore", actor=approved_by,
                  data={"went_back_to": number, "went_back_to_id": point.get("id") or str(number),
                        "commit": point.get("commit") or "", "place": place["id"],
                        "state": "live", "snapshot": before or None, "data_restored": bool(restore_data),
                        "detail": "Code and data" if restore_data else "Code only; the data stayed as it was"})
        result = {"ok": True, "kind": "rollback", "dry_run": False, "restore_point": number, "place": place["id"],
                  "url": url, "health": health, "data_restored": bool(restore_data), "snapshot": before or None,
                  "steps": steps.steps, "seconds": round(time.monotonic() - started, 1),
                  "log": str(runner.log_path.relative_to(project_dir))}
        _finish(store, "rollback", place, True, restore_point=number, url=url)
        return result


__all__ += [
    "ReleaseError", "ReleaseBusy", "RecipeNotTrusted", "DeployRecipe", "DEPLOY_KINDS", "DEPLOY_KIND_WORDS",
    "RELEASE_STEP", "RELEASE_FINISHED", "read_deploy_recipe", "validate_remote_path", "recipe_trusted",
    "trust_recipe", "release_plan", "release_lock", "restore_tag", "restore_tags", "restore_refs",
    "restore_point_tag", "next_restore_number",
    "save_restore_point", "restore_points", "live_restore_point", "check_health", "release", "authorize_rollback",
    "rollback_plan", "rollback",
]
