# SPDX-License-Identifier: Apache-2.0
"""Running the app being built, so the Stage can show it.

Detection, in order (``detect``):
    1. ``.playground/run.toml`` ``[app]`` (see model.RunConfig): ``command`` may
       hold ``{port}``; ``port = 0`` means "pick a free one"; ``kind`` is the
       target ("web" or "desktop-app"); ``url_path``, ``cwd``, ``env``,
       ``ready_timeout_s``.
    2. ``package.json`` scripts, ``dev`` then ``start``, with the port passed
       to the framework when it's one we know, else via ``PORT``, and read
       back from the server's own output either way.
    3. Python: Django (``manage.py``), FastAPI, Flask.
    4. A static ``index.html``, served by Python's ``http.server`` (see STATIC_SERVER).

Running (``Preview``): the dev server starts in its own process group inside
the project folder, logs to ``.playground/local/preview.log``, and is
health-checked until it answers. Every change of state is written to
``.playground/local/preview.json`` (so the MCP server can report it) and
published on the bus as ``preview.state`` with ``state`` (starting, running,
failed, stopped), ``url``, ``error`` (plain words), plus ``log_tail``,
``target``, ``where`` and ``label``.

Stage and Live: the Stage is this run (local, or on the development server
through an ``ssh -L`` tunnel); Live is the production place's ``url``.

Threat notes
    * The app being built is code we didn't write. Its environment is
      scrubbed of anything that looks like a credential (API keys, tokens,
      the SSH agent socket); vault secrets are passed only when asked.
    * Servers bind to 127.0.0.1 where the framework allows it.
    * Remote runs go through ``places.guard_write``: never on production.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from typing import Any, Callable
import urllib.error
import urllib.request

from . import events
from .model import RunConfig, THIS_COMPUTER, now_iso
from .paths import ProjectPaths
from .store import atomic_write_json, read_json

log = logging.getLogger(__name__)

STATES = ("starting", "running", "failed", "stopped")
TARGETS = ("web", "android", "desktop-app", "screen")
# What run.toml's kind may say, in people's words, and the target it means.
KIND_ALIASES = {"web": "web", "website": "web", "gtk": "desktop-app", "desktop-app": "desktop-app", "app": "desktop-app",
                "vnc": "screen", "screen": "screen", "vm": "screen", "remote": "screen", "android": "android"}
DEFAULT_READY_TIMEOUT = 60.0
LOG_TAIL_LINES = 20

_URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]|[\w.-]+):(\d{2,5})(/[^\s\x1b'\"]*)?")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SENSITIVE_ENV = re.compile(r"(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY|AUTH)", re.I)
_KEEP_ENV = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "TMPDIR",
             "XDG_RUNTIME_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "DISPLAY",
             "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "NVM_DIR", "NODE_OPTIONS", "VIRTUAL_ENV",
             "PYTHONPATH", "JAVA_HOME", "ANDROID_HOME", "ANDROID_SDK_ROOT", "GOPATH", "CARGO_HOME", "RUSTUP_HOME"}


# ───────────────────────────── detection ─────────────────────────────

@dataclass
class RunPlan:
    """How to run a project's app. ``command`` may hold ``{port}``."""
    source: str                    # "run.toml" | "npm" | "django" | "fastapi" | "flask" | "static" | "gtk"
    command: list[str] | str       # argv, or a shell string (run.toml)
    label: str                     # plain words: "the Vite dev server"
    cwd: str = "."
    port: int = 0                  # 0 = pick a free one
    url_path: str = "/"
    target: str = "web"
    port_from_output: bool = False # also watch the log for the real address
    env: dict[str, str] = field(default_factory=dict)
    ready_timeout_s: float = DEFAULT_READY_TIMEOUT
    problem: str = ""              # a reason it can't run yet, in plain words
    toolkit: str = ""              # desktop apps: "gtk4" or "gtk3"
    host: str = ""                 # screens: where it is
    screen_port: int = 0           # screens: its VNC port
    password_env: str = ""         # screens: the variable that holds its password

    def argv(self, port: int) -> list[str]:
        if isinstance(self.command, str):
            return ["/bin/sh", "-c", self.command.replace("{port}", str(port))]
        return [part.replace("{port}", str(port)) for part in self.command]

    def shell_text(self, port: int) -> str:
        if isinstance(self.command, str):
            return self.command.replace("{port}", str(port))
        return shlex.join(self.argv(port))

    def to_dict(self) -> dict:
        return {"source": self.source, "command": self.command if isinstance(self.command, str)
                else shlex.join(self.command), "label": self.label, "cwd": self.cwd, "port": self.port,
                "url_path": self.url_path, "target": self.target, "problem": self.problem}


def _read_toml(path: Path) -> dict | None:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _python(root: Path) -> str:
    for candidate in (root / ".venv" / "bin" / "python", root / "venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return sys.executable if Path(sys.executable).name.startswith("python3") else "python3"


def _package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return "bun"
    return "npm"


# (pattern in the script, label, extra args with {port}, sets PORT too)
_JS_FRAMEWORKS = (
    (r"\bnext\b", "the Next.js server", ["-p", "{port}"]),
    (r"\bastro\b", "the Astro dev server", ["--port", "{port}", "--host", "127.0.0.1"]),
    (r"\bnuxt\b|\bnuxi\b", "the Nuxt dev server", ["--port", "{port}"]),
    (r"\bvite\b|svelte-kit", "the Vite dev server", ["--port", "{port}", "--strictPort", "--host", "127.0.0.1"]),
    (r"webpack(-dev-server| serve)", "the webpack dev server", ["--port", "{port}"]),
    (r"\bparcel\b", "the Parcel dev server", ["--port", "{port}"]),
    (r"react-scripts start", "the Create React App server", []),
)


def _detect_npm(root: Path) -> RunPlan | None:
    pkg_path = root / "package.json"
    if not pkg_path.exists():
        return None
    try:
        pkg = json.loads(pkg_path.read_text())
    except (OSError, ValueError):
        return RunPlan("npm", ["npm", "run", "dev"], "the app", problem="package.json can't be read. Is it valid JSON?")
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    script_name = next((s for s in ("dev", "start") if isinstance(scripts.get(s), str)), None)
    if script_name is None:
        return None
    script = scripts[script_name]
    manager = _package_manager(root)
    argv = [manager, "run", script_name]
    label, extra = "the app's dev server", []
    for pattern, name, args in _JS_FRAMEWORKS:
        if re.search(pattern, script):
            label, extra = name, args
            break
    if extra:
        argv += (["--"] if manager == "npm" else []) + extra
    problem = ""
    deps = (pkg.get("dependencies") or {}) | (pkg.get("devDependencies") or {})
    if deps and not (root / "node_modules").exists():
        problem = f"This project's packages aren't installed yet. Run “{manager} install” first."
    if not shutil.which(manager):
        problem = f"Playground can't find {manager}. Is Node.js installed?"
    return RunPlan("npm", argv, label, env={"BROWSER": "none"}, port_from_output=True, problem=problem)


_ASSIGN = r"^\s*(\w+)\s*(?::[^=]*)?=\s*{cls}\("


def _find_python_app(root: Path, cls: str) -> tuple[str, str] | None:
    pattern = re.compile(_ASSIGN.format(cls=cls), re.M)
    candidates = [root / n for n in ("main.py", "app.py", "server.py", "api.py", "wsgi.py", "asgi.py")]
    for folder in ("app", "src", "api"):
        if (root / folder).is_dir():
            candidates += sorted((root / folder).glob("*.py"))[:30]
    for path in candidates:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        match = pattern.search(text)
        if match:
            module = ".".join(path.relative_to(root).with_suffix("").parts)
            return module, match.group(1)
    return None


def _detect_python(root: Path) -> RunPlan | None:
    py = _python(root)
    manage = root / "manage.py"
    if manage.exists() and "django" in manage.read_text(errors="replace").lower():
        return RunPlan("django", [py, "manage.py", "runserver", "127.0.0.1:{port}"], "the Django dev server")
    found = _find_python_app(root, "FastAPI")
    if found:
        module, var = found
        return RunPlan("fastapi", [py, "-m", "uvicorn", f"{module}:{var}", "--host", "127.0.0.1",
                                   "--port", "{port}"], "the FastAPI server")
    found = _find_python_app(root, "Flask")
    if found:
        module, var = found
        return RunPlan("flask", [py, "-m", "flask", "--app", f"{module}:{var}", "run", "--host", "127.0.0.1",
                                 "--port", "{port}"], "the Flask dev server")
    for name in ("main.py", "app.py"):
        path = root / name
        if path.exists():
            text = path.read_text(errors="replace")
            if "gi.require_version" in text and ("Gtk" in text or "Adw" in text):
                toolkit = "gtk3" if re.search(r"""require_version\(\s*['"]Gtk['"]\s*,\s*['"]3""", text) else "gtk4"
                return RunPlan("gtk", [py, name], "the app", target="desktop-app", toolkit=toolkit)
    return None


# ``python3 -m http.server``'s own handler and server, minus one thing: the
# stock HTTPServer.server_bind calls socket.getfqdn(), a reverse DNS lookup that
# can stall for half a minute before the socket even listens.
STATIC_SERVER = """\
import functools, http.server, socketserver, sys
class Server(http.server.ThreadingHTTPServer):
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=sys.argv[2])
server = Server(("127.0.0.1", int(sys.argv[1])), handler)
print(f"Serving your pages at http://127.0.0.1:{server.server_port}/", flush=True)
server.serve_forever()
"""


def static_server_argv(directory: Path | str, port: int | str = "{port}", python: str | None = None) -> list[str]:
    return [python or _python(Path(directory)), "-c", STATIC_SERVER, str(port), str(Path(directory).resolve())]


def _detect_static(root: Path) -> RunPlan | None:
    for folder in (".", "public", "dist", "build", "site", "www"):
        if (root / folder / "index.html").is_file():
            return RunPlan("static", static_server_argv(root / folder, python=_python(root)),
                           "a simple web server for your pages")
    return None


def detect(project_dir: Path | str) -> RunPlan | None:
    """Work out how to run the project's app, or None when there's nothing to run yet."""
    root = Path(project_dir)
    run_toml = ProjectPaths(root).run_toml
    if run_toml.exists():
        document = _read_toml(run_toml)
        app = (document or {}).get("app")
        if not isinstance(app, dict):
            return RunPlan("run.toml", "", "the app", problem="run.toml needs an [app] table.")
        config = RunConfig.from_dict(app)
        target = KIND_ALIASES.get(config.kind.lower(), "web")
        if target == "screen":
            if not config.screen_port:
                return RunPlan("run.toml", "", "the screen", target="screen",
                               problem="run.toml's screen needs screen_port (the VNC port, like 5901).")
        elif not config.command.strip():
            return RunPlan("run.toml", "", "the app", problem="run.toml's [app] needs a command.")
        return RunPlan("run.toml", config.command, str(app.get("label") or ("the screen" if target == "screen"
                                                                            else "the app")),
                       cwd=config.cwd, port=config.port, url_path=config.url_path or "/", target=target,
                       env=dict(config.env), ready_timeout_s=config.ready_timeout_s,
                       port_from_output="{port}" not in config.command and target == "web",
                       toolkit=config.toolkit, host=config.host or "127.0.0.1", screen_port=config.screen_port,
                       password_env=config.password_env)
    for detector in (_detect_npm, _detect_python, _detect_static):
        plan = detector(root)
        if plan is not None:
            return plan
    return None


# ───────────────────────────── helpers ─────────────────────────────

def _broadwayd(toolkit: str = "") -> str | None:
    """GTK's Broadway server for the app's toolkit: GTK 4's, or GTK 3's broadwayd."""
    if toolkit == "gtk3":
        return shutil.which("broadwayd")
    return shutil.which("gtk4-broadwayd") or (shutil.which("broadwayd") if not toolkit else None)


def sandbox_home(paths: ProjectPaths) -> dict[str, str]:
    """Where an app shown in Preview keeps its settings, data and cache: inside the project's local folder,
    so trying it never touches the person's own (and it's all gone with the project)."""
    base = paths.local_dir / "preview-home"
    out = {}
    for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"),
                     ("XDG_STATE_HOME", "state")):
        folder = base / sub
        folder.mkdir(parents=True, exist_ok=True)
        out[var] = str(folder)
    out["GSETTINGS_BACKEND"] = "keyfile"     # settings in a file in that folder, not the desktop's dconf
    # Under Broadway a window's shadow is cut off at the edge of its surface; a small one fits inside it.
    # (GTK reads this file at the user level, over the app's theme; it's Playground's, in the sandbox.)
    css = base / "config" / "gtk-4.0" / "gtk.css"
    if not css.exists() or css.read_text(encoding="utf-8") != PREVIEW_GTK_CSS:
        css.parent.mkdir(parents=True, exist_ok=True)
        css.write_text(PREVIEW_GTK_CSS, encoding="utf-8")
    return out


PREVIEW_GTK_CSS = """/* Written by Playground for its Preview: a window shadow that fits Broadway's surface. */
window.csd, window.csd:backdrop {
  box-shadow: 0 1px 3px 0 alpha(black, 0.22), 0 4px 10px 0 alpha(black, 0.10), 0 0 0 1px alpha(black, 0.10);
}
"""


def free_port(host: str = "127.0.0.1") -> int:
    """A port nobody is listening on right now (the OS picks it)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def answers(url: str, timeout: float = 2.0) -> int | None:
    """The HTTP status if something answers at ``url`` (any status counts), else None."""
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Playground preview"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - localhost only
            return int(response.status)
    except urllib.error.HTTPError as exc:
        with exc:
            return int(exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for code we didn't write: no credentials, no SSH agent."""
    env = {k: v for k, v in os.environ.items()
           if (k in _KEEP_ENV or k.startswith("LC_") or k.startswith("npm_config_"))
           and not _SENSITIVE_ENV.search(k)}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["NO_COLOR"] = "1"
    env["FORCE_COLOR"] = "0"
    env.update(extra or {})
    return env


def read_log_tail(path: Path, lines: int = LOG_TAIL_LINES, max_bytes: int = 16384) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read().decode(errors="replace")
    except OSError:
        return ""
    data = _ANSI_RE.sub("", data)
    return "\n".join(data.splitlines()[-lines:])


_REASONS = (
    (("EADDRINUSE", "Address already in use", "address already in use"),
     "Another program is already using that port. Stop it, or pick a different port."),
    (("command not found", "No such file or directory", "not found: "),
     "A program the app needs isn't installed."),
    (("Cannot find module", "ModuleNotFoundError", "ERR_MODULE_NOT_FOUND", "No module named"),
     "Some of the app's packages aren't installed yet."),
    (("SyntaxError", "IndentationError"), "There's a mistake in the code, so the app couldn't start."),
    (("EACCES", "Permission denied"), "The app wasn't allowed to do something it needed."),
)


def explain_failure(log_tail: str, exit_code: int | None = None, timed_out: float | None = None) -> str:
    """Plain words for why the app didn't come up."""
    for needles, reason in _REASONS:
        if any(n in log_tail for n in needles):
            return reason
    if timed_out is not None:
        seconds = int(round(timed_out))
        return f"The app didn't answer within {seconds} second{'' if seconds == 1 else 's'}."
    if exit_code is not None:
        return f"The app stopped before it was ready (it exited with code {exit_code})."
    return "The app couldn't start."


# ───────────────────────────── targets ─────────────────────────────

def _android_sdk_tool(*parts: str) -> str | None:
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        base = os.environ.get(env)
        if base and Path(base, *parts).exists():
            return str(Path(base, *parts))
    default = Path.home() / "Android" / "Sdk"
    if Path(default, *parts).exists():
        return str(Path(default, *parts))
    return None


def android_status() -> dict:
    """Is there an Android emulator or Waydroid to preview on?"""
    adb = shutil.which("adb") or _android_sdk_tool("platform-tools", "adb")
    emulator = shutil.which("emulator") or _android_sdk_tool("emulator", "emulator")
    waydroid = shutil.which("waydroid")
    kvm = os.path.exists("/dev/kvm")
    devices: list[str] = []
    if adb:
        try:
            out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=4).stdout
            devices = [line.split()[0] for line in out.splitlines()[1:] if line.strip().endswith("device")]
        except (OSError, subprocess.TimeoutExpired):
            pass
    if devices:
        message = f"An Android device is ready ({devices[0]})."
    elif emulator and kvm:
        message = "The Android emulator is installed and can start."
    elif emulator:
        message = "The Android emulator is installed, but this computer can't speed it up (no KVM), so it will be slow."
    elif waydroid:
        message = "Waydroid is installed and can run Android apps."
    else:
        message = "There's no Android emulator here. Install Android Studio's emulator or Waydroid to try Android apps."
    return {"id": "android", "available": bool(devices or emulator or waydroid), "devices": devices,
            "emulator": bool(emulator), "waydroid": bool(waydroid), "kvm": kvm, "message": message}


def desktop_status() -> dict:
    """Can a desktop app open here, or stream into the Stage?"""
    native = sys.platform.startswith("linux") and bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))
    broadway = shutil.which("gtk4-broadwayd") or shutil.which("broadwayd")
    xpra = shutil.which("xpra")
    streams = [name for name, tool in (("broadway", broadway), ("xpra", xpra)) if tool]
    if native and streams:
        message = "Can open as a window here, or show inside Playground."
    elif native:
        message = "Can open as a window here."
    elif streams:
        message = "Can show inside Playground."
    else:
        message = "Desktop apps need Luma (or another Linux desktop) to run."
    return {"id": "desktop-app", "available": native or bool(streams), "native": native, "streams": streams,
            "message": message}


def targets() -> list[dict]:
    return [{"id": "web", "available": True, "message": "Shows in the preview, in any device frame."},
            android_status(), desktop_status()]


# ───────────────────────────── running ─────────────────────────────


def device_env(device: str) -> dict[str, str]:
    """What a Luma session says about the device it runs on (config/mobile/environment.d, the kit's form
    factor), for the size the Preview shows. An app that asks lays itself out as it would there: LumaUI on a
    phone drops its title bar and window controls and makes the content the screen. Apps that don't ask
    are unaffected. Said for computers too, so a phone running Playground doesn't leak its own."""
    if device == "phone":
        return {"LUMA_DEVICE_CLASS": "handheld", "LUMA_FORM_FACTOR": "phone", "LUMA_INPUT_MODE": "touch",
                "LUMA_PRESENTATION_MODE": "fullscreen-mobile"}
    if device == "tablet":
        return {"LUMA_DEVICE_CLASS": "tablet", "LUMA_FORM_FACTOR": "tablet", "LUMA_INPUT_MODE": "touch",
                "LUMA_PRESENTATION_MODE": "windowed"}
    if device in ("desktop", "app"):
        return {"LUMA_DEVICE_CLASS": "desktop", "LUMA_FORM_FACTOR": "desktop", "LUMA_INPUT_MODE": "pointer",
                "LUMA_PRESENTATION_MODE": "windowed"}
    return {}


# A session bus of the preview's own (dbus-broker): anyone may send to, hear from and own anything, and there are
# no service folders, so nothing starts on it but the app and the preview's shell.
BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow own="*"/>
    <allow send_destination="*"/>
    <allow receive_sender="*"/>
  </policy>
</busconfig>
"""

class Preview:
    """One run of the app. ``start()`` returns at once; watch ``preview.state``.

    ``start(wait=True)`` blocks until it is running or has failed (tests, MCP).
    """

    def __init__(self, project_dir: Path | str, plan: RunPlan | None = None, *,
                 bus: events.EventBus | None = None, stream: str | None = None,
                 extra_env: dict[str, str] | None = None) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.paths = ProjectPaths(self.project_dir)
        self._detected = plan is None
        self.plan = plan if plan is not None else detect(self.project_dir)
        self.bus = bus if bus is not None else events.bus
        # Desktop apps show inside Playground (Broadway) unless asked for a native window, and never land
        # in the dock: "native" only when someone asks for it, or there's no Broadway on this computer.
        if stream is None and self.plan is not None and self.plan.target == "desktop-app":
            stream = "broadway" if _broadwayd(self.plan.toolkit) else None
        self.stream = stream
        self.extra_env = dict(extra_env or {})
        # The size this run is started for (the Stage's phone, tablet, desktop or app window).
        self.device = self.extra_env.get("PLAYGROUND_PREVIEW_DEVICE", "")
        self.where = THIS_COMPUTER
        self.state = "stopped"
        self.url = ""
        self.error = ""
        self.port = 0
        self.log_path = self.paths.local_dir / "preview.log"
        self._procs: list[subprocess.Popen] = []
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._stopping = False
        self._thread: threading.Thread | None = None

    # ── state ──

    def status(self) -> dict:
        with self._lock:
            return {"state": self.state, "url": self.url, "error": self.error, "where": self.where,
                    "target": self.plan.target if self.plan else "web", "device": self.device,
                    "label": self.plan.label if self.plan else "", "port": self.port,
                    "log": str(self.log_path.relative_to(self.project_dir))}

    def _set(self, state: str, url: str = "", error: str = "", log_tail: str = "") -> None:
        with self._lock:
            if self._stopping and state != "stopped":
                return          # a late report from the watcher after stop(): ignore it
            self.state, self.url, self.error = state, url, error
            record = {"state": state, "url": url, "error": error, "updated": now_iso(), "pid": os.getpid(),
                      "where": self.where, "target": self.plan.target if self.plan else "web",
                      "label": self.plan.label if self.plan else "", "device": self.device}
            if log_tail:
                record["log_tail"] = log_tail
        self.paths.local_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.paths.preview_state, record)
        payload = {k: v for k, v in record.items() if k not in ("updated", "pid")}
        self.bus.publish(events.PREVIEW_STATE, project=str(self.project_dir), **payload)
        if state in ("running", "failed", "stopped"):
            self._ready.set()

    def _fail(self, reason: str, exit_code: int | None = None, timed_out: float | None = None) -> None:
        tail = read_log_tail(self.log_path)
        message = reason or explain_failure(tail, exit_code, timed_out)
        self._kill()
        self._set("failed", error=message, log_tail=tail)

    # ── lifecycle ──

    def start(self, wait: bool = False, timeout: float | None = None) -> "Preview":
        with self._lock:
            if self.state in ("starting", "running"):
                return self
            self._stopping = False
            self._ready.clear()
        self._set("starting")
        if self.plan is None:
            self._set("failed", error="Playground couldn't find an app to run yet. Once there's an index.html, "
                                      "a package.json or a run.toml, it can show it here.")
            return self
        if self.plan.problem:
            self._set("failed", error=self.plan.problem)
            return self
        try:
            self._launch()
        except Exception as exc:  # noqa: BLE001 - every failure becomes a plain-words state
            self._fail(self._launch_error(exc))
            return self
        self._thread = threading.Thread(target=self._watch, name="preview-health", daemon=True)
        self._thread.start()
        if wait:
            self._ready.wait(timeout if timeout is not None else self.plan.ready_timeout_s + 10)
        return self

    def _launch_error(self, exc: Exception) -> str:
        if isinstance(exc, FileNotFoundError):
            name = Path(str(exc.filename or "")).name or "a program"
            return f"Playground can't find {name}. Is it installed?"
        return str(exc) or "The app couldn't start."

    def _open_log(self):
        self.paths.local_dir.mkdir(parents=True, exist_ok=True)
        handle = open(self.log_path, "wb")
        handle.write(f"# Playground preview, {now_iso()}\n".encode())
        handle.flush()
        return handle

    def _spawn(self, argv: list[str], env: dict[str, str], cwd: Path, log) -> subprocess.Popen:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        self._procs.append(proc)
        self._record_pids(argv)
        return proc

    def _record_pids(self, argv: list[str] | None = None) -> None:
        """Note which process groups this run started, so a later Playground can stop them if this one was
        killed before it could (see ``reap_stale``)."""
        path = _pids_path(self.paths)
        record = read_json(path) or {}
        groups = [g for g in record.get("groups") or [] if isinstance(g, dict) and record.get("owner") == os.getpid()]
        known = {g.get("pid") for g in groups}
        for proc in self._procs:
            if proc.pid not in known and proc.poll() is None:
                name = Path(str((argv or [""])[0])).name if proc is self._procs[-1] else ""
                groups.append({"pid": proc.pid, "command": name})
        try:
            self.paths.local_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, {"owner": os.getpid(), "project": str(self.project_dir), "groups": groups,
                                     "updated": now_iso()})
        except OSError:
            pass

    def _cwd(self) -> Path:
        cwd = (self.project_dir / (self.plan.cwd or ".")).resolve()
        if cwd != self.project_dir and not cwd.is_relative_to(self.project_dir):
            raise ValueError("run.toml's cwd points outside the project.")
        return cwd

    def _launch(self) -> None:
        plan = self.plan
        assert plan is not None
        self.port = plan.port or free_port()
        env = clean_env({**plan.env, **self.extra_env, "PORT": str(self.port)})
        log = self._open_log()
        try:
            if plan.target == "screen":
                self._launch_screen(env, log)
                return
            argv = plan.argv(self.port)
            if plan.target == "desktop-app" and self.stream == "broadway":
                broadwayd = _broadwayd(plan.toolkit)
                if not broadwayd:
                    raise ValueError("Showing desktop apps inside Playground needs GTK's Broadway "
                                     "(gtk4-broadwayd).")
                display = 20 + (self.port % 70)
                # The server and the app share the sandbox's folders: without a runtime folder, Broadway's
                # socket lives in the cache folder, and both have to look in the same one.
                env.update(sandbox_home(self.paths))
                self._spawn([broadwayd, "--port", str(self.port), "--address", "127.0.0.1", f":{display}"],
                            env, self._cwd(), log)
                env.update({"GDK_BACKEND": "broadway", "BROADWAY_DISPLAY": f":{display}",
                            # Window sizes kept across hot restarts (preview_site/sitecustomize.py).
                            "PLAYGROUND_PREVIEW_STATE": str(self.paths.local_dir / "preview-home" / "windows.json")})
                env.update(device_env(env.get("PLAYGROUND_PREVIEW_DEVICE", "")))
                # Python GTK apps fill a device frame, or open at their own size in the App window view
                # (preview_site/sitecustomize.py; PLAYGROUND_PREVIEW_FILL comes with extra_env).
                from .paths import package_dir
                site = str(package_dir() / "core" / "preview_site")
                env["PYTHONPATH"] = site + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
                argv = self._own_session(argv, env, log)
                time.sleep(0.3)
            self._spawn(argv, env, self._cwd(), log)
        finally:
            log.close()

    def _own_session(self, argv: list[str], env: dict[str, str], log) -> list[str]:
        """Its own session bus, with the preview's shell on it (preview_shell.py). The app can't reach the
        desktop's notifications, dock or other apps, a second copy of an installed app doesn't hand itself to the
        real one, and when it asks the shell what it's on (a phone, a tablet, a computer) or for light or dark, the
        answer is the Preview's, not the desktop's. dbus-run-session where there is one; else dbus-broker (Luma,
        Fedora), with a bus config that starts no services."""
        from .paths import package_dir
        shell = [sys.executable or "python3", str(package_dir() / "core" / "preview_shell.py"),
                 "--device", env.get("PLAYGROUND_PREVIEW_DEVICE") or "desktop",
                 "--appearance", env.get("PLAYGROUND_PREVIEW_APPEARANCE", ""), "--", *argv]
        runner = shutil.which("dbus-run-session")
        if runner:
            return [runner, "--", *shell]
        activate, broker = shutil.which("systemd-socket-activate"), shutil.which("dbus-broker-launch")
        if not (activate and broker):
            log.write("No private session bus here (dbus-run-session or dbus-broker): the app shares the "
                      "desktop's.\n".encode())
            return argv
        import tempfile
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        folder = Path(tempfile.mkdtemp(prefix="playground-bus-", dir=runtime if runtime and os.path.isdir(runtime)
                                       else None))
        (folder / "bus.conf").write_text(BUS_CONFIG, encoding="utf-8")
        address = folder / "bus"
        self._spawn([activate, "-l", str(address), broker, "--config-file", str(folder / "bus.conf")], env,
                    self._cwd(), log)
        self._bus_folder = folder
        for _ in range(100):
            if address.exists():
                break
            time.sleep(0.02)
        if not address.exists():
            log.write("The preview's own session bus didn't start: the app shares the desktop's.\n".encode())
            return argv
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={address}"
        return shell

    def _launch_screen(self, env: dict[str, str], log) -> None:
        """A screen (a VM, another machine, a headless display): start its command if there is one, then serve
        the viewer and bridge it to the screen's VNC port."""
        plan = self.plan
        assert plan is not None
        if str(plan.command or "").strip():
            self._spawn(plan.argv(self.port), env, self._cwd(), log)
        from . import screens
        password = os.environ.get(plan.password_env, "") if plan.password_env else ""
        self._bridge = screens.ScreenBridge(plan.host or "127.0.0.1", plan.screen_port, port=self.port,
                                            password=password)
        self._bridge.start()

    def _main_proc(self) -> subprocess.Popen | None:
        return self._procs[-1] if self._procs else None

    def _watch(self) -> None:
        plan = self.plan
        assert plan is not None
        deadline = time.monotonic() + plan.ready_timeout_s
        native_window = plan.target == "desktop-app" and self.stream != "broadway"
        started = time.monotonic()
        while not self._stopping:
            proc = self._main_proc()
            code = proc.poll() if proc else None
            if code is not None:
                if not self._stopping:
                    self._fail("", exit_code=code)
                return
            if plan.target == "screen":
                bridge = getattr(self, "_bridge", None)
                if bridge is not None and bridge.screen_answers():
                    self._set("running", url=bridge.url)
                    break
            elif native_window:
                if time.monotonic() - started > 1.5:
                    self._set("running")
                    break
            else:
                for url in self._candidate_urls():
                    if answers(url) is not None:
                        self._set("running", url=url)
                        break
                if self.state == "running":
                    break
            if time.monotonic() > deadline:
                self._fail("", timed_out=plan.ready_timeout_s)
                return
            time.sleep(0.25)
        # Keep watching: if the app dies later, say so.
        while not self._stopping:
            proc = self._main_proc()
            if proc is None and plan.target == "screen":
                time.sleep(0.5)        # a screen someone else runs (a machine, a VM started elsewhere)
                continue
            code = proc.poll() if proc else 0
            if code is not None:
                if not self._stopping:
                    tail = read_log_tail(self.log_path)
                    self._kill()
                    if code in (-2, -9, -15):
                        # Ended from outside (an agent stopping it to restart it, say): not a crash.
                        words = "The app was closed. It starts again when its files change, or with Try again."
                    elif code:
                        words = f"The app stopped (exit code {code}). {explain_failure(tail, code)}"
                    else:
                        words = "The app was closed. Try again opens it."
                    self._set("failed", error=words, log_tail=tail)
                return
            time.sleep(0.5)

    def _candidate_urls(self) -> list[str]:
        assert self.plan is not None
        path = self.plan.url_path if self.plan.url_path.startswith("/") else "/" + self.plan.url_path
        urls = [f"http://127.0.0.1:{self.port}{path}"]
        if self.plan.port_from_output:
            tail = read_log_tail(self.log_path, lines=200)
            for match in _URL_RE.finditer(tail):
                port = int(match.group(1))
                if port != self.port:
                    urls.append(f"http://127.0.0.1:{port}{path}")
        return urls

    def _kill(self) -> None:
        bridge, self._bridge = getattr(self, "_bridge", None), None
        if bridge is not None:
            bridge.stop()
        procs, self._procs = list(self._procs), []
        folder, self._bus_folder = getattr(self, "_bus_folder", None), None
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        for proc in reversed(procs):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        for proc in reversed(procs):
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait(timeout=5)
            if proc.stdin:
                proc.stdin.close()
        with contextlib.suppress(OSError):
            _pids_path(self.paths).unlink()

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
        self._kill()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._set("stopped")

    def restart(self, wait: bool = False) -> "Preview":
        self.stop()
        if self._detected:
            self.plan = detect(self.project_dir)   # the project may have changed since
        return self.start(wait=wait)


class RemotePreview(Preview):
    """Run the app on a development server and tunnel its port here (``ssh -L``)."""

    def __init__(self, project_dir: Path | str, place: dict, plan: RunPlan | None = None, *,
                 project_id: str | None = None, bus: events.EventBus | None = None,
                 ssh_runner: Callable[..., Any] | None = None) -> None:
        super().__init__(project_dir, plan, bus=bus)
        self.place = dict(place)
        self.where = str(place.get("id") or "server")
        self.project_id = project_id
        self.remote_port = 0

    def _launch(self) -> None:
        from . import places
        places.guard_write(self.place, actor="preview")
        plan = self.plan
        assert plan is not None
        if plan.target != "web":
            raise ValueError("Only web apps can run on a server for now.")
        remote_dir = str(self.place.get("path") or "")
        if not remote_dir:
            raise ValueError("Tell Playground where the project folder is on the server first.")
        self.port = free_port()
        self.remote_port = plan.port or (20000 + int.from_bytes(os.urandom(2), "big") % 20000)
        command = plan.shell_text(self.remote_port)
        env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in {**plan.env, "PORT": str(self.remote_port)}.items()
                              if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k))
        sub = (plan.cwd or ".")
        remote = (f"cd {shlex.quote(remote_dir)} && cd {shlex.quote(sub)} && "
                  f"exec env {env_prefix} sh -c {shlex.quote(command)}")
        argv, extra_env = places.ssh_command(self.place, remote, project_id=self.project_id,
                                             forward=[f"{self.port}:127.0.0.1:{self.remote_port}"], tty=True)
        log = self._open_log()
        try:
            proc = subprocess.Popen(argv, cwd=str(self.project_dir), env={**os.environ, **extra_env},
                                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
                                    start_new_session=True)
            self._procs.append(proc)
            self._record_pids(argv)
        finally:
            log.close()

    def _candidate_urls(self) -> list[str]:
        assert self.plan is not None
        path = self.plan.url_path if self.plan.url_path.startswith("/") else "/" + self.plan.url_path
        return [f"http://127.0.0.1:{self.port}{path}"]


# ───────────────────────────── the app's handle ─────────────────────────────

_RUNNING: dict[str, Preview] = {}
_RUNNING_LOCK = threading.Lock()


def current(project_dir: Path | str) -> Preview | None:
    with _RUNNING_LOCK:
        return _RUNNING.get(str(Path(project_dir).resolve()))


SELF_RELOADING = ("vite", "next", "nuxt", "astro", "webpack", "parcel", "--reload", "--watch", "watch", "dev",
                  "nodemon", "runserver", "hot")


def follows_changes(plan: RunPlan) -> str:
    """How the preview keeps up when the app's files change: "restart" (a desktop app, a server that loads the
    code once), "reload" (static files: just the page), or "" (a dev server that reloads itself, or a screen)."""
    if plan.target == "screen":
        return ""
    if plan.target == "desktop-app":
        return "restart"
    if plan.source == "static":
        return "reload"
    if plan.source in ("npm", "django"):
        return ""                    # Vite, Next… and Django's runserver watch the files themselves
    command = plan.command if isinstance(plan.command, str) else " ".join(plan.command)
    return "" if any(word in command.lower() for word in SELF_RELOADING) else "restart"


def start_preview(project_dir: Path | str, *, place: dict | None = None, project_id: str | None = None,
                  stream: str | None = None, extra_env: dict[str, str] | None = None,
                  bus: events.EventBus | None = None, wait: bool = False) -> Preview:
    """Start (or restart) the Stage run for a project. One run per project."""
    key = str(Path(project_dir).resolve())
    stop_preview(project_dir)
    if place and place.get("kind") == "ssh":
        preview: Preview = RemotePreview(project_dir, place, project_id=project_id, bus=bus)
    else:
        preview = Preview(project_dir, bus=bus, stream=stream, extra_env=extra_env)
    with _RUNNING_LOCK:
        _RUNNING[key] = preview
    return preview.start(wait=wait)


def stop_preview(project_dir: Path | str) -> bool:
    with _RUNNING_LOCK:
        preview = _RUNNING.pop(str(Path(project_dir).resolve()), None)
    if preview is None:
        return False
    preview.stop()
    return True


def _pids_path(paths: ProjectPaths) -> Path:
    return paths.local_dir / "preview-pids.json"


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _command_of(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        pass
    try:
        done = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _is_ours(group: dict, project_dir: Path) -> bool:
    """Is this still the process we started (not an unrelated one that got the same number)?"""
    pid = int(group.get("pid") or 0)
    if not _alive(pid):
        return False
    try:
        if os.getpgid(pid) != pid:
            return False            # we start every run as its own group leader
    except OSError:
        return False
    try:
        cwd = Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
        return cwd == project_dir or cwd.is_relative_to(project_dir)
    except OSError:
        pass                        # no /proc (macOS): match the program instead
    command = str(group.get("command") or "")
    return bool(command) and command in _command_of(pid)


def reap_stale(project_dir: Path | str) -> int:
    """Stop the previews an earlier Playground started for this project and never stopped (it was killed or
    crashed). Leaves alone anything a Playground that's still running owns. Returns how many were stopped."""
    project_dir = Path(project_dir).resolve()
    path = _pids_path(ProjectPaths(project_dir))
    record = read_json(path)
    if not isinstance(record, dict):
        return 0
    owner = int(record.get("owner") or 0)
    if owner == os.getpid():
        return 0
    if _alive(owner) and "playground" in _command_of(owner).lower():
        return 0                    # that Playground is still open; its previews are its own
    stale = [g for g in record.get("groups") or [] if isinstance(g, dict) and _is_ours(g, project_dir)]
    for group in stale:
        with contextlib.suppress(OSError):
            os.killpg(int(group["pid"]), signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(_alive(int(g["pid"])) for g in stale):
        time.sleep(0.1)
    for group in stale:
        if _alive(int(group["pid"])):
            with contextlib.suppress(OSError):
                os.killpg(int(group["pid"]), signal.SIGKILL)
    with contextlib.suppress(OSError):
        path.unlink()
    if stale:
        log.info("stopped %d leftover preview(s) in %s", len(stale), project_dir)
    return len(stale)


def stop_all() -> None:
    with _RUNNING_LOCK:
        previews = list(_RUNNING.values())
        _RUNNING.clear()
    for preview in previews:
        preview.stop()


def stage_and_live(project_dir: Path | str) -> dict:
    """The two sides of the Stage: this run, and the live app if a production place has a url."""
    preview = current(project_dir)
    stage = preview.status() if preview else {"state": "stopped", "url": "", "error": "", "where": THIS_COMPUTER}
    live: dict | None = None
    try:
        from . import places
        for place in places.places_for_role(project_dir, "production"):
            if place.get("url"):
                live = {"url": place["url"], "place_id": place["id"], "name": place.get("name", "")}
                break
    except Exception:  # noqa: BLE001 - no project.toml yet: no live side
        live = None
    return {"stage": stage, "live": live}


__all__ = [
    "RunPlan", "detect", "Preview", "RemotePreview", "start_preview", "stop_preview", "stop_all", "current",
    "stage_and_live", "targets", "android_status", "desktop_status", "free_port", "answers", "clean_env",
    "explain_failure", "read_log_tail", "static_server_argv", "STATES", "TARGETS",
]
