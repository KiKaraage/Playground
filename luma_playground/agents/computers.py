# SPDX-License-Identifier: Apache-2.0
"""Agents on another computer: a build server, a desktop under the stairs, any machine you reach with SSH.

Some people keep their Claude and Codex installs on a big machine so their own stays quick. Playground
reaches it with SSH and nothing else installed by hand:

1. **Add a computer**: ``user@host`` (or an alias from ``~/.ssh/config``), a port and a key if they're
   not the defaults. The first connection trusts the computer's identity (``accept-new``), as a person
   typing ``ssh`` would.
2. **Find what's there** (``probe``): the same account finder Playground uses on this computer
   (``accounts.py``, sent over and run with the computer's own python3), plus each program's own
   report of whether it's signed in. ``deep=True`` also searches the computer's disks for sign-in
   folders, for installs kept somewhere unusual. Nothing secret comes back: only folders and yes/no.
3. **Set up** (``ensure_agent``): Playground's own pinned Node.js (only if the computer has no Node 22+)
   and the agent's ACP adapter, installed in ``~/.local/share/playground`` there. No root, no system
   packages, checked against the same sha256 sums as here.
4. **Run** (``remote_launch``): the adapter runs there over ``ssh -T``; ACP is just its stdin and stdout.
   The agent works in its own copy of the project (``~/playground/<name>-<id>``), mirrored there before
   each turn and back after it (``push`` / ``pull``, rsync, newer files win, nothing deleted), so the
   project on this computer, its areas and its history stay the source of truth.

Computers are per device, in ``~/.config/playground/computers.toml``, like connections. A connection
runs on a computer when its ``where`` is that computer's id.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import threading
import tomllib
from typing import Any, Callable

from ..core import paths
from ..core.model import now_iso
from ..core.store import atomic_write_text, dump_toml

SSH_TIMEOUT_S = 15
PROBE_TIMEOUT_S = 120
SETUP_TIMEOUT_S = 900
SYNC_EXCLUDES = (".playground/local/", "node_modules/", ".venv/", "__pycache__/", ".DS_Store")
_setup_lock = threading.Lock()
_ready: dict[tuple, dict] = {}


class ComputerError(RuntimeError):
    """A problem with another computer, in plain words."""


@dataclass
class Computer:
    id: str
    name: str
    host: str
    user: str = ""
    port: int = 22
    identity: str = ""                 # a key file; "" = ssh's own choice (agent, ~/.ssh/config)
    workdir: str = "~/playground"      # where projects are mirrored there
    jump: str = ""                     # reached through another machine (ssh -J): a VM behind a server
    home: str = ""                     # its home folder, learned on first contact
    added: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def to_dict(self) -> dict:
        data = {"id": self.id, "name": self.name, "host": self.host, "port": self.port, "workdir": self.workdir}
        for key in ("user", "identity", "home", "added", "jump"):
            if getattr(self, key):
                data[key] = getattr(self, key)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Computer":
        try:
            port = int(data.get("port") or 22)
        except (TypeError, ValueError):
            port = 22
        return cls(id=str(data.get("id") or ""), name=str(data.get("name") or data.get("host") or ""),
                   host=str(data.get("host") or ""), user=str(data.get("user") or ""), port=port,
                   identity=str(data.get("identity") or ""), workdir=str(data.get("workdir") or "~/playground"),
                   home=str(data.get("home") or ""), added=str(data.get("added") or ""),
                   jump=str(data.get("jump") or ""))


def parse_address(text: str) -> tuple[str, str, int]:
    """'noor@build.example:6767' / 'root@10.0.0.4' / 'buildbox' → (user, host, port)."""
    text = text.strip()
    user, _, rest = text.rpartition("@")
    host, port = rest, 22
    match = re.fullmatch(r"(.+?):(\d{1,5})", rest)
    if match and not rest.count(":") > 1:
        host, port = match.group(1), int(match.group(2))
    if not re.fullmatch(r"[A-Za-z0-9._\-\[\]:]+", host or ""):
        raise ComputerError("That doesn’t look like a computer’s address. Try user@host, like root@10.0.0.4.")
    return user, host, port


# ── the list of computers ──

class ComputerStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.config_dir() / "computers.toml"

    def list(self) -> list[Computer]:
        try:
            document = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return []
        return [Computer.from_dict(c) for c in document.get("computers") or [] if isinstance(c, dict) and c.get("id")]

    def get(self, computer_id: str) -> Computer | None:
        return next((c for c in self.list() if c.id == computer_id), None)

    def save(self, computers: list[Computer]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, "# Computers Playground runs agents on, over SSH (agents/computers.py).\n"
                          + dump_toml({"computers": [c.to_dict() for c in computers]}))

    def add(self, computer: Computer) -> Computer:
        items = self.list()
        taken = {c.id for c in items}
        base, n = computer.id or paths.slugify(computer.name or computer.host, 32), 2
        computer.id = base
        while computer.id in taken:
            computer.id, n = f"{base}-{n}", n + 1
        computer.added = computer.added or now_iso()
        self.save(items + [computer])
        return computer

    def update(self, computer: Computer) -> None:
        self.save([computer if c.id == computer.id else c for c in self.list()])

    def remove(self, computer_id: str) -> bool:
        items = self.list()
        kept = [c for c in items if c.id != computer_id]
        if len(kept) != len(items):
            self.save(kept)
            return True
        return False


def for_connection(connection) -> Computer | None:
    """The computer a connection runs on, or None when it runs here."""
    where = str(getattr(connection, "where", "") or "")
    if not where or where == "this-computer":
        return None
    return ComputerStore().get(where)


# ── SSH ──

def ssh_argv(computer: Computer, *, tty: bool = False) -> list[str]:
    argv = [shutil.which("ssh") or "ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_TIMEOUT_S}",
            "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new", "-p", str(computer.port)]
    if computer.identity:
        argv += ["-i", os.path.expanduser(computer.identity), "-o", "IdentitiesOnly=yes"]
    if computer.jump:
        argv += ["-J", computer.jump]
    argv.append("-t" if tty else "-T")
    argv.append(computer.destination)
    return argv


def run(computer: Computer, command: str, *, input_text: str | None = None, timeout: float = 60,
        runner=subprocess.run) -> subprocess.CompletedProcess:
    try:
        return runner([*ssh_argv(computer), "--", command], input=input_text, capture_output=True, text=True,
                      timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise ComputerError(f"{computer.name} took too long to answer.") from error
    except FileNotFoundError as error:
        raise ComputerError("This computer has no ssh program to reach others with.") from error


def explain(stderr: str, computer: Computer) -> str:
    text = (stderr or "").lower()
    if "permission denied" in text:
        return (f"{computer.name} didn’t accept this computer’s key. Add this computer’s public key to its "
                f"~/.ssh/authorized_keys, or choose the key it does accept.")
    if "could not resolve" in text or "name or service not known" in text:
        return f"Playground can’t find {computer.host}. Check the address."
    if "timed out" in text or "no route" in text or "unreachable" in text:
        return f"{computer.name} isn’t answering. Is it on, and reachable from here?"
    if "connection refused" in text:
        return f"{computer.name} answered, but not on port {computer.port}. Is SSH on another port?"
    if "host key verification failed" in text or "remote host identification has changed" in text:
        return (f"{computer.name}’s identity has changed since last time. If you rebuilt it, remove its old "
                f"line from ~/.ssh/known_hosts; if you didn’t, don’t connect.")
    line = (stderr or "").strip().splitlines()
    return line[-1][:200] if line else f"{computer.name} didn’t answer."


def test(computer: Computer, *, runner=subprocess.run) -> dict:
    """Reach the computer; learn its home folder, system and Python. Raises ComputerError in plain words."""
    result = run(computer, 'echo "home=$HOME"; uname -sm; command -v python3 || true', timeout=SSH_TIMEOUT_S + 10,
                 runner=runner)
    if result.returncode != 0:
        raise ComputerError(explain(result.stderr, computer))
    lines = result.stdout.splitlines()
    home = next((l[5:] for l in lines if l.startswith("home=")), "")
    system = lines[1] if len(lines) > 1 else ""
    return {"home": home, "system": system, "python3": bool(lines[2:] and lines[2].strip())}


# ── finding the agents and accounts there ──

PROBE_MAIN = r'''
import json, os, shutil, subprocess, sys, time
env = dict(os.environ)
out = {"system": " ".join(os.uname()[i] for i in (0, 4)), "home": env.get("HOME", ""), "programs": {},
       "accounts": [], "node": ""}
path = search_path(env)
for name in ("claude", "codex", "gemini", "opencode", "goose", "node"):
    found = shutil.which(name, path=path)
    if found:
        out["programs"][name] = found
node = out["programs"].get("node")
if node:
    try:
        out["node"] = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        pass

import pwd
me = pwd.getpwuid(os.getuid()).pw_name

def status(provider, home, user_env, user):
    program = shutil.which(provider, path=search_path(user_env))
    e = dict(user_env)
    if home:
        e[HOME_VAR[provider]] = home
    try:
        wrap = ["runuser", "-u", user, "--", "env", f"HOME={user_env['HOME']}"] if user != me else []
        if provider == "codex" and program:
            r = subprocess.run([*wrap, program, "login", "status"], capture_output=True, text=True, timeout=20,
                               env=e, stdin=subprocess.DEVNULL)
            text = (r.stdout + r.stderr).lower()
            if "logged in" in text and "not logged in" not in text:
                return True, "ChatGPT" if "chatgpt" in text else ("api key" if "api key" in text else "")
            if "not logged in" in text:
                return False, ""
        if provider == "claude" and program:
            r = subprocess.run([*wrap, program, "auth", "status", "--json"], capture_output=True, text=True,
                               timeout=20, env=e, stdin=subprocess.DEVNULL)
            if r.stdout.strip().startswith("{"):
                data = json.loads(r.stdout)
                method = str(data.get("authMethod") or "")
                return bool(data.get("loggedIn")), {"claude.ai": "subscription", "api_key": "api key"}.get(method, "")
    except Exception:
        pass
    return signed_in(provider, home, user_env), ""

seen = set()
def add(provider, home, label, source, user, user_env):
    home = home or default_home(provider, user_env)
    real = os.path.realpath(os.path.expanduser(home))
    if (provider, real) in seen:
        return
    seen.add((provider, real))
    signed, method = status(provider, home, user_env, user)
    if not signed and not os.path.isdir(real):
        return            # nothing there: not an account
    out["accounts"].append({"provider": provider, "home": home, "label": label, "source": source,
                            "signed_in": signed, "method": method, "user": user,
                            "user_home": user_env["HOME"]})

# Every person on the computer whose home we can read (all of them when we're root): their own accounts,
# run as them.
people = [(me, env.get("HOME", ""))]
if os.geteuid() == 0:
    for entry in pwd.getpwall():
        if (entry.pw_uid >= 1000 and entry.pw_uid < 60000 and os.path.isdir(entry.pw_dir)
                and entry.pw_name != me):
            people.append((entry.pw_name, entry.pw_dir))
for user, home in people:
    user_env = dict(env, HOME=home)
    user_env.pop("CODEX_HOME", None) if user != me else None
    user_env.pop("CLAUDE_CONFIG_DIR", None) if user != me else None
    for provider in ("codex", "claude"):
        for account in discover(provider, user_env):
            add(provider, account.home, account.label, account.source, user, user_env)

if DEEP:
    deadline = time.monotonic() + 60
    skip = {"proc", "sys", "dev", "run", "tmp", "node_modules", ".git", ".cache", "snap", "flatpak", "containers",
            "lost+found", "boot", "usr", "lib", "lib64", "bin", "sbin", "etc", "var"}
    roots = [h for _u, h in people] + ["/root", "/home", "/srv", "/opt", "/data", "/mnt"]
    roots = [r for r in roots if r and os.path.isdir(r)]
    for root in dict.fromkeys(roots):
        base_depth = root.rstrip("/").count("/")
        for folder, dirs, files in os.walk(root):
            if time.monotonic() > deadline:
                break
            dirs[:] = [d for d in dirs if d not in skip and not (folder == "/" and d in skip)]
            if folder.count("/") - base_depth >= 6:
                dirs[:] = []
            name = os.path.basename(folder).lower()
            if "auth.json" in files and "codex" in name:
                owner = pwd.getpwuid(os.stat(folder).st_uid)
                add("codex", folder, "", "a search of the disk", owner.pw_name, dict(env, HOME=owner.pw_dir))
            if ".credentials.json" in files and "claude" in name:
                owner = pwd.getpwuid(os.stat(folder).st_uid)
                add("claude", folder, "", "a search of the disk", owner.pw_name, dict(env, HOME=owner.pw_dir))
print("PLAYGROUND-PROBE" + json.dumps(out))
'''


def probe_script(deep: bool = False) -> str:
    """accounts.py, made to stand alone, then the probe: one Python program, stdlib only."""
    source = (paths.package_dir() / "agents" / "accounts.py").read_text(encoding="utf-8")
    stub = ("class paths:\n"
            "    @staticmethod\n"
            "    def slugify(text, limit=40):\n"
            "        import re as _re\n"
            "        return _re.sub(r'[^a-z0-9]+', '-', str(text).lower()).strip('-')[:limit] or 'item'\n"
            "    @staticmethod\n"
            "    def account_home(slug):\n"
            "        from pathlib import Path as _P\n"
            "        return _P.home() / '.local' / 'share' / 'playground' / 'accounts' / slug\n")
    source = source.replace("from ..core import paths\n", stub)
    source = source.replace("from __future__ import annotations\n", "")
    return "from __future__ import annotations\n" + source + f"\nDEEP = {bool(deep)}\n" + PROBE_MAIN


def probe(computer: Computer, *, deep: bool = False, runner=subprocess.run) -> dict:
    """What's on the computer: programs, Node, and every Claude and Codex account (folders and yes/no only)."""
    result = run(computer, "python3 -", input_text=probe_script(deep), timeout=PROBE_TIMEOUT_S, runner=runner)
    marker = result.stdout.rfind("PLAYGROUND-PROBE")
    if marker < 0:
        if "python3" in (result.stderr or "") and "not found" in (result.stderr or ""):
            raise ComputerError(f"{computer.name} has no python3, which Playground needs to look around it.")
        raise ComputerError(explain(result.stderr, computer) if result.returncode else
                            f"{computer.name} didn’t say what it has.")
    try:
        return json.loads(result.stdout[marker + len("PLAYGROUND-PROBE"):].strip().splitlines()[0])
    except (ValueError, IndexError) as error:
        raise ComputerError(f"{computer.name}’s answer couldn’t be read.") from error


# ── setting up an agent there ──

SETUP_SCRIPT = r'''
set -e
BASE="$HOME/.local/share/playground"
node_ok() { [ -x "$1" ] && "$1" -e 'process.exit(parseInt(process.versions.node) >= 22 ? 0 : 1)' 2>/dev/null; }
NODE=""
for n in "$BASE/runtime/node-v$NODE_VERSION-"*/bin/node "$(command -v node 2>/dev/null)" "$HOME"/.nvm/versions/node/*/bin/node; do
  if node_ok "$n"; then NODE="$n"; break; fi
done
if [ -z "$NODE" ]; then
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) P=linux-x64; X=tar.xz; S="$SHA_LINUX_X64";;
    Linux-aarch64) P=linux-arm64; X=tar.xz; S="$SHA_LINUX_ARM64";;
    Darwin-arm64) P=darwin-arm64; X=tar.gz; S="$SHA_DARWIN_ARM64";;
    Darwin-x86_64) P=darwin-x64; X=tar.gz; S="$SHA_DARWIN_X64";;
    *) echo "PLAYGROUND-ERROR this kind of computer isn't supported yet"; exit 3;;
  esac
  mkdir -p "$BASE/runtime"; cd "$BASE/runtime"
  F="node-v$NODE_VERSION-$P.$X"
  (command -v curl >/dev/null && curl -fsSL -o "$F.part" "https://nodejs.org/dist/v$NODE_VERSION/$F") || \
    wget -q -O "$F.part" "https://nodejs.org/dist/v$NODE_VERSION/$F"
  SUM=$( (command -v sha256sum >/dev/null && sha256sum "$F.part") || shasum -a 256 "$F.part")
  case "$SUM" in "$S "*) ;; *) rm -f "$F.part"; echo "PLAYGROUND-ERROR the Node.js download didn't match its checksum"; exit 4;; esac
  tar -xf "$F.part"; rm -f "$F.part"
  NODE="$BASE/runtime/node-v$NODE_VERSION-$P/bin/node"
fi
NPM="$(dirname "$NODE")/../lib/node_modules/npm/bin/npm-cli.js"
DIR="$BASE/agents/$AGENT_ID/$AGENT_VERSION"
if [ ! -f "$DIR/.ready" ]; then
  rm -rf "$DIR.tmp"; mkdir -p "$DIR.tmp" "$BASE/runtime/npm"
  : > "$BASE/runtime/npm/npmrc"; : > "$BASE/runtime/npm/npmrc-global"
  PATH="$(dirname "$NODE"):$PATH" npm_config_userconfig="$BASE/runtime/npm/npmrc" \
    npm_config_globalconfig="$BASE/runtime/npm/npmrc-global" npm_config_cache="$BASE/runtime/npm/cache" \
    npm_config_registry=https://registry.npmjs.org/ npm_config_update_notifier=false npm_config_fund=false \
    npm_config_audit=false npm_config_loglevel=error \
    "$NODE" "$NPM" install --prefix "$DIR.tmp" --omit=dev --no-package-lock --ignore-scripts "$PACKAGE" >/dev/null
  rm -rf "$DIR"; mv "$DIR.tmp" "$DIR"; touch "$DIR/.ready"
fi
SCRIPT=$("$NODE" -e '
  const path = require("path"); const dir = path.join(process.argv[1], "node_modules", process.argv[2]);
  const meta = require(path.join(dir, "package.json")); const short = process.argv[2].split("/").pop();
  const bin = typeof meta.bin === "string" ? meta.bin : (meta.bin[short] || Object.values(meta.bin)[0]);
  console.log(path.join(dir, bin));' "$DIR" "$PACKAGE_NAME")
echo "PLAYGROUND-NODE=$NODE"
echo "PLAYGROUND-SCRIPT=$SCRIPT"
'''


# The agents' own sign-in, run on the computer in a terminal here. Device codes, so no browser is needed there.
SIGN_IN_COMMANDS = {"codex": "codex login --device-auth", "claude": "claude /login"}


def sign_in_argv(computer: Computer, provider: str, config_home: str = "", *, user: str = "",
                 user_home: str = "") -> list[str]:
    """ssh -t to the computer, as the account's owner, with the account's folder: the agent's own sign-in when
    its program is there, else a shell that says what to type. Playground never sees what's typed."""
    from .connections import HOME_VARS
    program = SIGN_IN_COMMANDS.get(provider, "")
    folder = f"{HOME_VARS[provider]}={shlex.quote(config_home)} " if provider in HOME_VARS and config_home else ""
    name = program.split(" ", 1)[0]
    script = (f"if command -v {name} >/dev/null 2>&1; then {program}; else "
              f"echo; echo 'Playground: {name} isn’t installed for this account here.'; "
              f"echo 'Sign in the way you usually do on this computer, then type exit.'; echo; "
              f"exec \"${{SHELL:-/bin/sh}}\" -l; fi") if program else 'exec "${SHELL:-/bin/sh}" -l'
    command = f"{as_user(user, user_home)}env {folder}sh -lc {shlex.quote(script)}"
    return [*ssh_argv(computer, tty=True), command]


def as_user(user: str = "", home: str = "") -> str:
    """The prefix that runs a command as another person on the computer (when Playground is root there)."""
    if not user:
        return ""
    return f"runuser -u {shlex.quote(user)} -- env HOME={shlex.quote(home)} " if home else \
        f"runuser -u {shlex.quote(user)} -- "


def ensure_agent(computer: Computer, entry, *, user: str = "", user_home: str = "",
                 runner=subprocess.run) -> dict:
    """Node.js and the agent's adapter on the computer, in that person's home. Returns {"node", "script",
    "args"}; cached."""
    from . import registry, runtime
    dist = entry.distribution.get("npx") or {}
    name, version = registry.split_package(str(dist.get("package") or ""))
    if not name:
        raise ComputerError(f"{entry.name} can’t run on another computer yet.")
    version = version or entry.version or "latest"
    key = (computer.id, entry.id, version, user)
    with _setup_lock:
        if key in _ready:
            return _ready[key]
        sums = {plat: runtime.NODE_BUILDS[plat][1] for plat in runtime.NODE_BUILDS}
        values = {"NODE_VERSION": runtime.NODE_VERSION, "SHA_LINUX_X64": sums["linux-x86_64"],
                  "SHA_LINUX_ARM64": sums["linux-aarch64"], "SHA_DARWIN_ARM64": sums["darwin-aarch64"],
                  "SHA_DARWIN_X64": sums["darwin-x86_64"], "AGENT_ID": entry.id, "AGENT_VERSION": version,
                  "PACKAGE": f"{name}@{version}" if version != "latest" else name, "PACKAGE_NAME": name}
        prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in values.items())
        if user:
            command = f"runuser -u {shlex.quote(user)} -- env HOME={shlex.quote(user_home)} {prefix} sh -s"
        else:
            command = f"{prefix} sh -s"
        result = run(computer, command, input_text=SETUP_SCRIPT, timeout=SETUP_TIMEOUT_S, runner=runner)
        found = dict(re.findall(r"^PLAYGROUND-(NODE|SCRIPT|ERROR)[= ](.*)$", result.stdout, re.M))
        if "ERROR" in found:
            raise ComputerError(f"Setting up {entry.name} on {computer.name}: {found['ERROR']}.")
        if result.returncode != 0 or "NODE" not in found or "SCRIPT" not in found:
            raise ComputerError(f"Setting up {entry.name} on {computer.name} didn’t work: "
                                f"{explain(result.stderr, computer)}")
        info = {"node": found["NODE"].strip(), "script": found["SCRIPT"].strip(),
                "args": [str(a) for a in dist.get("args") or []]}
        _ready[key] = info
        return info


# ── running, and the project's copy there ──

def project_dir(computer: Computer, root: Path | str, project_id: str = "", home: str = "") -> str:
    """The project's copy on the computer: <workdir>/<folder name>-<id>, as an absolute path, in the home of
    whoever the agent runs as."""
    root = Path(root)
    home = home or computer.home or "~"
    base = computer.workdir.replace("~", home, 1) if computer.workdir.startswith("~") else computer.workdir
    suffix = (project_id or "")[:8]
    name = paths.slugify(root.name, 40) + (f"-{suffix}" if suffix else "")
    return f"{base.rstrip('/')}/{name}"


def remote_launch(connection, computer: Computer, root: Path | str, registry=None, *,
                  project_id: str = "", runner=subprocess.run) -> tuple[list[str], dict[str, str], str]:
    """The ssh argv that starts the connection's agent on the computer, this side's env, and its folder there."""
    from . import registry as registry_mod
    from .connections import HOME_VARS, KEY_VARS, lookup_api_key, LaunchError
    reg = registry if registry is not None else registry_mod.load()
    entry = reg.get(connection.agent) if connection.agent else reg.for_provider(connection.provider)
    if entry is None:
        raise LaunchError(f"Playground doesn't know how to start {connection.label}.")
    if not computer.home:
        computer.home = test(computer, runner=runner).get("home", "")
        try:
            ComputerStore().update(computer)
        except OSError:
            pass
    user = str(connection.extra.get("remote_user") or "")
    user_home = str(connection.extra.get("remote_home") or "")
    try:
        info = ensure_agent(computer, entry, user=user, user_home=user_home, runner=runner)
    except ComputerError as error:
        raise LaunchError(str(error)) from error
    folder = project_dir(computer, root, project_id, home=user_home)
    exports = []
    var = HOME_VARS.get(connection.provider)
    if var and connection.config_home:
        exports.append(f"{var}={shlex.quote(connection.config_home)}")
    if connection.kind in ("api-key", "team-key"):
        key = lookup_api_key(connection)
        if not key:
            raise LaunchError(f"The API key for {connection.label} isn't in the keyring.")
        exports += [f"{v}={shlex.quote(key)}" for v in KEY_VARS.get(connection.provider, ())]
    command = (as_user(user, user_home) + f"sh -c " + shlex.quote(
               f"mkdir -p {shlex.quote(folder)} && cd {shlex.quote(folder)} && exec env "
               + " ".join([*exports, f"PATH={shlex.quote(str(Path(info['node']).parent))}:\"$PATH\""])
               + " " + " ".join(shlex.quote(p) for p in [info["node"], info["script"], *info["args"]])))
    return [*ssh_argv(computer), "--", command], dict(os.environ), folder


def _rsync(computer: Computer, user: str = "") -> list[str]:
    ssh = " ".join(shlex.quote(p) for p in ssh_argv(computer)[:-2])      # without -T and the destination
    argv = [shutil.which("rsync") or "rsync", "-az", "--update", "-e", ssh,
            *[f"--exclude={x}" for x in SYNC_EXCLUDES]]
    if user:
        argv.append(f"--rsync-path=runuser -u {user} -- rsync")       # the copy belongs to whoever works in it
    return argv


def push(computer: Computer, root: Path | str, folder: str, *, user: str = "", user_home: str = "",
         runner=subprocess.run) -> None:
    """This computer's project → the copy there (newer files win; nothing is deleted)."""
    run(computer, as_user(user, user_home) + f"mkdir -p {shlex.quote(folder)}", runner=runner)
    result = runner([*_rsync(computer, user), f"{str(root).rstrip('/')}/", f"{computer.destination}:{folder}/"],
                    capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise ComputerError(f"Copying the project to {computer.name} didn’t work: {explain(result.stderr, computer)}")


def pull(computer: Computer, root: Path | str, folder: str, *, user: str = "", runner=subprocess.run) -> None:
    """The copy there → this computer's project (newer files win; nothing is deleted)."""
    result = runner([*_rsync(computer, user), f"{computer.destination}:{folder}/", f"{str(root).rstrip('/')}/"],
                    capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise ComputerError(f"Copying the work back from {computer.name} didn’t work: "
                            f"{explain(result.stderr, computer)}")


def suggest_connections(computer: Computer, found: dict, owner: str, existing: list | None = None) -> list:
    """Connections for the accounts a probe found (not saved): one per account, named for the computer."""
    from .connections import Connection
    existing = existing or []
    have = {(c.provider, c.where, c.config_home) for c in existing}
    out = []
    for account in found.get("accounts") or []:
        provider = account.get("provider")
        if provider not in ("claude", "codex") or not account.get("signed_in"):
            continue          # not signed in: offered as "sign in to use", not added
        home = account.get("home") or ""
        if (provider, computer.id, home) in have:
            continue
        user = str(account.get("user") or "")
        who = f" · {user}" if user and user != computer.user else ""
        label = f"{account.get('label') or provider.capitalize()}{who}"
        conn = Connection(id=paths.slugify(f"{provider}-{computer.id}-{user or 'me'}-{Path(home).name}", 40),
                          label=label, kind="api-key" if account.get("method") == "api key" else "personal-plan",
                          provider=provider, owner=owner, where=computer.id, config_home=home,
                          private_seat=True)
        if user and user != computer.user:
            conn.extra["remote_user"] = user
            conn.extra["remote_home"] = str(account.get("user_home") or "")
        out.append(conn)
    return out
