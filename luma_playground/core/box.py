# SPDX-License-Identifier: Apache-2.0
"""The project's box: a sandbox where agents build things and install what building needs, without touching
the computer.

One rootless podman container per project, made the first time an agent needs it, from the Fedora release
this computer is built on. It sees the project folder (at the same path, so what's built there runs here) and
nothing else of the person's: not their home, not their keys. It's root inside and just the person outside, so
compilers, -devel packages and toolchains install with no password and nothing lands on the computer. Tool
caches (cargo, pip, npm) live in the project's own local folder. Remove it, and it's gone.

What was installed is kept in .playground/box.toml, so the box can be made again on another computer, and
packaging knows what building needs. The app runs the commands (agents.runner can't reach podman from inside
an agent's own sandbox); agents ask through playground_box_run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import threading
import tomllib

from .paths import ProjectPaths

CREATE_TIMEOUT_S = 20 * 60
RUN_TIMEOUT_S = 60 * 60
OUTPUT_TAIL = 8000
INSTALL = re.compile(r"\bdnf\s+(?:-y\s+)?install\s+(?P<packages>[^;&|]+)")


class BoxError(RuntimeError):
    """Plain words."""


@dataclass
class BoxState:
    name: str
    tool: str = ""                 # "toolbox", or "" when there's no way to make one here
    exists: bool = False
    packages: list[str] = field(default_factory=list)


@dataclass
class Result:
    code: int
    output: str
    seconds: float = 0.0

    def words(self, command: str) -> str:
        head = "It worked." if self.code == 0 else f"It failed (exit code {self.code})."
        tail = self.output.strip()[-OUTPUT_TAIL:]
        return f"In the project's box: `{command}`. {head}" + (f"\n\n```\n{tail}\n```" if tail else "")


def tool() -> str:
    return "podman" if shutil.which("podman") else ""


def image() -> str:
    """The Fedora release this computer is built on (Luma's PLATFORM_ID says f44), else the newest."""
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        text = ""
    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    clean = {k: v.strip().strip('"') for k, v in fields.items()}
    release = ""
    if clean.get("ID") == "fedora":
        release = clean.get("VERSION_ID", "")
    match = re.search(r"platform:f(\d+)", clean.get("PLATFORM_ID", ""))
    release = release or (match.group(1) if match else "")
    return f"registry.fedoraproject.org/fedora:{release or 'latest'}"


def name_for(project_dir: Path | str, project_id: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", Path(project_dir).name.lower()).strip("-")[:24] or "project"
    suffix = re.sub(r"[^a-z0-9]", "", project_id.lower())[:6]
    return f"playground-{slug}" + (f"-{suffix}" if suffix else "")


def state(project_dir: Path | str, project_id: str = "") -> BoxState:
    name = name_for(project_dir, project_id)
    found = BoxState(name=name, tool=tool(), packages=packages(project_dir))
    if found.tool:
        import time
        cached = _EXISTS.get(name)
        if cached is not None and time.monotonic() - cached[0] < 60 and cached[1]:
            found.exists = True
            return found
        try:
            found.exists = subprocess.run(["podman", "container", "exists", name], capture_output=True,
                                          timeout=15).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            found.exists = False
        _EXISTS[name] = (time.monotonic(), found.exists)
    return found


_EXISTS: dict[str, tuple[float, bool]] = {}


def packages(project_dir: Path | str) -> list[str]:
    path = ProjectPaths(project_dir).meta / "box.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return [str(p) for p in data.get("packages") or [] if str(p).strip()]


def remember(project_dir: Path | str, new: list[str]) -> list[str]:
    """Add packages to box.toml (sorted, once each)."""
    kept = sorted(set(packages(project_dir)) | {p for p in new if re.fullmatch(r"[\w.+:@/-]+", p)})
    path = ProjectPaths(project_dir).meta / "box.toml"
    lines = ["# The project's box: what building it needs, installed in its own sandbox (toolbox), never on the",
             "# computer. Playground keeps this list; the box can be made again from it.",
             "packages = [" + ", ".join(f'"{p}"' for p in kept) + "]", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return kept


def installs_in(command: str) -> list[str]:
    """The packages a command installs with dnf (flags left out)."""
    out: list[str] = []
    for match in INSTALL.finditer(command):
        out += [p for p in shlex.split(match.group("packages")) if not p.startswith("-")]
    return out


def create_argv(project_dir: Path | str, project_id: str = "") -> list[str]:
    root = str(Path(project_dir).absolute())
    return ["podman", "create", "--name", name_for(project_dir, project_id), "--label", "playground.box=1",
            "--security-opt", "label=disable",          # the project folder keeps its own SELinux label
            "--volume", f"{root}:{root}", "--workdir", root, "--hostname", "box", image(), "sleep", "infinity"]


def ensure(project_dir: Path | str, project_id: str = "", *, runner=subprocess.run) -> str:
    """Make the box if it isn't there yet (the first time downloads an image: a minute or two), and start it."""
    if not tool():
        raise BoxError("This computer has no podman, so there's no box to build in. Ask the person before "
                       "installing anything.")
    name = name_for(project_dir, project_id)
    if not state(project_dir, project_id).exists:
        result = runner(create_argv(project_dir, project_id), capture_output=True, text=True,
                        timeout=CREATE_TIMEOUT_S, stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            raise BoxError("The box couldn't be made: " + (result.stderr or result.stdout).strip()[-300:])
        _EXISTS[name] = (0.0, True)
        fresh = True
    else:
        fresh = False
    runner(["podman", "start", name], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if not fresh:
        return name
    want = packages(project_dir)
    if want:   # a new box for a project that had one (another computer, or after removing it): the same packages
        run(project_dir, "dnf install -y " + " ".join(shlex.quote(p) for p in want), project_id, runner=runner)
    return name


def remove(project_dir: Path | str, project_id: str = "", *, runner=subprocess.run) -> None:
    """Gone: the container and everything installed in it. (box.toml stays, so it can be made again.)"""
    name = name_for(project_dir, project_id)
    runner(["podman", "rm", "--force", name], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    _EXISTS.pop(name, None)


def argv(project_dir: Path | str, command: str, project_id: str = "") -> list[str]:
    """podman exec, in the project folder, with tool caches inside the project's local folder. It's root in
    there, so `sudo` (agents are used to typing it) just runs the command."""
    root = Path(project_dir).absolute()
    home = ProjectPaths(root).local_dir / "box-home"
    env = {"CARGO_HOME": home / "cargo", "RUSTUP_HOME": home / "rustup", "npm_config_cache": home / "npm",
           "PIP_CACHE_DIR": home / "pip", "GOPATH": home / "go"}
    exports = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())
    exports += f' PATH="{home / "cargo" / "bin"}:$PATH"'
    script = (f"sudo() {{ \"$@\"; }}; mkdir -p {shlex.quote(str(home))} && cd {shlex.quote(str(root))} && "
              f"export {exports} && {command}")
    return ["podman", "exec", name_for(root, project_id), "sh", "-lc", script]


def run(project_dir: Path | str, command: str, project_id: str = "", *, timeout: float = RUN_TIMEOUT_S,
        runner=subprocess.run) -> Result:
    import time
    started = time.monotonic()
    try:
        done = runner(argv(project_dir, command, project_id), capture_output=True, text=True, timeout=timeout,
                      stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as error:
        return Result(124, f"Stopped after {int(timeout)} seconds.\n{error.stdout or ''}", time.monotonic() - started)
    except OSError as error:
        raise BoxError(str(error)) from error
    output = (done.stdout or "") + (("\n" + done.stderr) if done.stderr else "")
    if done.returncode == 0:
        new = installs_in(command)
        if new:
            remember(project_dir, new)
    return Result(done.returncode, output[-OUTPUT_TAIL * 2:], time.monotonic() - started)


# Installing on the computer itself, when the person says yes on the card: the system's own password dialog
# (polkit), never a password in chat or a terminal.

def computer_install_argv(packages_: list[str]) -> list[str]:
    safe = [p for p in packages_ if re.fullmatch(r"[\w.+:@-]+", p)]
    if not safe:
        raise BoxError("No packages to install.")
    if shutil.which("rpm-ostree") and Path("/run/ostree-booted").exists():
        return ["pkexec", "rpm-ostree", "install", "--idempotent", "--allow-inactive", *safe]
    if shutil.which("dnf"):
        return ["pkexec", "dnf", "install", "-y", *safe]
    if shutil.which("apt-get"):
        return ["pkexec", "apt-get", "install", "-y", *safe]
    raise BoxError("This computer's package manager isn't one Playground knows.")


def install_on_computer(packages_: list[str], *, runner=subprocess.run) -> Result:
    import time
    started = time.monotonic()
    command = computer_install_argv(packages_)
    done = runner(command, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, stdin=subprocess.DEVNULL)
    output = ((done.stdout or "") + "\n" + (done.stderr or "")).strip()
    if command[1] == "rpm-ostree" and done.returncode == 0:
        output += "\nInstalled as a layer: it's there after the computer restarts."
    return Result(done.returncode, output[-OUTPUT_TAIL:], time.monotonic() - started)


_LOCKS: dict[str, threading.Lock] = {}


def lock_for(name: str) -> threading.Lock:
    """One command at a time per box (two dnf installs at once would fight over its package database)."""
    return _LOCKS.setdefault(name, threading.Lock())

