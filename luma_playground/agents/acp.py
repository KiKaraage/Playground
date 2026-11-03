# SPDX-License-Identifier: Apache-2.0
"""The client side of the Agent Client Protocol (ACP), version 1.

Playground launches an agent (Claude through its ACP adapter, Codex through
its adapter, Gemini, OpenCode, Goose, ...) as a subprocess and talks
newline-delimited JSON-RPC 2.0 over its stdin and stdout.

Threads, and why there are this many:

- **reader**: drains the agent's stdout, so the agent never blocks on a full
  pipe. Responses and notifications go onto one ordered queue; requests from
  the agent each get a worker thread, because answering one may mean waiting
  for the person (a permission) or for a command (a terminal).
- **dispatcher**: handles that ordered queue. Notifications reach the handler
  in the order the agent sent them, and a ``session/prompt`` response is only
  delivered after every update that came before it. A caller that gets the
  stop reason has already seen the whole turn.
- **stderr pump**: copies the agent's stderr to a log file and keeps the last
  lines in memory to explain a crash in plain words.

Every request has a timeout. ``prompt`` uses an *idle* timeout instead: a
turn can run for an hour as long as the agent keeps talking, or while a
question is waiting for the person.

Sign-in never passes through here as a secret. Agents sign in through their
own programs: either the agent runs its own flow when we call
``authenticate``, or (terminal auth) we launch the agent's own login in a
terminal and wait for it to exit. Playground never sees a token.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import itertools
import json
import logging
import os
from pathlib import Path
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Protocol

from .. import VERSION
from . import sandbox

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
REQUEST_TIMEOUT = 60.0          # ordinary requests
START_TIMEOUT = 240.0           # initialize; npx may download the adapter first
SESSION_TIMEOUT = 180.0         # session/new, load, resume (MCP servers start here)
PROMPT_IDLE_TIMEOUT = 20 * 60.0  # a turn is stopped after this long with no word from the agent
PERMISSION_TIMEOUT = 30 * 60.0  # a question nobody answers is declined
STDERR_LOG_MAX = 4 * 1024 * 1024

# JSON-RPC and ACP error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
REQUEST_CANCELLED = -32800
AUTH_REQUIRED = -32000
RESOURCE_NOT_FOUND = -32002

StopReason = str  # "end_turn" | "max_tokens" | "max_turn_requests" | "refusal" | "cancelled"


# ── Errors, in words a person can read ─────────────────────────────────────

class AcpError(Exception):
    """An error from the agent or the connection. ``str(e)`` is fit for the UI.

    ``reason`` sorts it for the UI: "offline" (no internet), "crashed" (the
    program stopped), "usage-limit", "sign-in", or "" (anything else)."""

    reason = ""

    def __init__(self, message: str, *, code: int | None = None, data: Any = None, detail: str = "",
                 reason: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data
        self.detail = detail  # technical detail for logs, never shown first
        if reason is not None:
            self.reason = reason


class AcpTimeout(AcpError):
    pass


class AcpClosed(AcpError):
    """The agent's program stopped."""

    reason = "crashed"


class AuthRequired(AcpError):
    """The agent needs the person to sign in first."""

    reason = "sign-in"


# Words in an error or the agent's last log lines that mean the internet is out of reach.
OFFLINE_HINTS = ("enotfound", "eai_again", "getaddrinfo", "econnrefused", "enetunreach", "ehostunreach",
                 "network is unreachable", "connection error", "could not resolve", "failed to lookup address",
                 "dns error", "stream disconnected", "error sending request", "network error", "offline",
                 "socket hang up", "fetch failed", "no route to host", "temporary failure in name resolution")


def looks_offline(*texts: str) -> bool:
    joined = " ".join(t for t in texts if t).lower()
    return any(hint in joined for hint in OFFLINE_HINTS)


class Unsupported(AcpError):
    """The agent didn't say it can do this."""


def _error_from_rpc(err: dict, method: str) -> AcpError:
    code = err.get("code") if isinstance(err, dict) else None
    message = str(err.get("message") or "") if isinstance(err, dict) else str(err)
    data = err.get("data") if isinstance(err, dict) else None
    detail = f"{method}: {code} {message} {json.dumps(data)[:500] if data is not None else ''}".strip()
    if code == AUTH_REQUIRED or "auth" in message.lower() and "required" in message.lower():
        return AuthRequired("This agent needs you to sign in first.", code=code, data=data, detail=detail)
    if code == METHOD_NOT_FOUND:
        return Unsupported("This agent can't do that.", code=code, data=data, detail=detail)
    if message.strip().lower() in ("", "internal error", "unknown error") and isinstance(data, dict):
        inner = data.get("details") or data.get("message") or data.get("error")
        message = inner if isinstance(inner, str) else ""
    plain = message if message and len(message) < 300 else "The agent ran into a problem."
    return AcpError(plain, code=code, data=data, detail=detail)


# ── What the agent said about itself ──────────────────────────────────────

@dataclass
class InitializeResult:
    protocol_version: int
    agent_capabilities: dict
    agent_info: dict
    auth_methods: list[dict]
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_wire(cls, result: dict) -> "InitializeResult":
        result = result if isinstance(result, dict) else {}
        return cls(
            protocol_version=int(result.get("protocolVersion") or 0),
            agent_capabilities=dict(result.get("agentCapabilities") or {}),
            agent_info=dict(result.get("agentInfo") or {}),
            auth_methods=[m for m in (result.get("authMethods") or []) if isinstance(m, dict)],
            raw=result,
        )

    def _session_cap(self, name: str) -> bool:
        caps = self.agent_capabilities.get("sessionCapabilities") or {}
        return isinstance(caps, dict) and caps.get(name) is not None

    @property
    def can_load(self) -> bool:
        return bool(self.agent_capabilities.get("loadSession"))

    @property
    def can_resume(self) -> bool:
        return self._session_cap("resume")

    @property
    def can_list(self) -> bool:
        return self._session_cap("list")

    @property
    def can_close(self) -> bool:
        return self._session_cap("close")

    @property
    def can_logout(self) -> bool:
        auth = self.agent_capabilities.get("auth") or {}
        return isinstance(auth, dict) and auth.get("logout") is not None

    @property
    def prompt_capabilities(self) -> dict:
        return dict(self.agent_capabilities.get("promptCapabilities") or {})

    @property
    def name(self) -> str:
        return str(self.agent_info.get("title") or self.agent_info.get("name") or "Agent")

    @property
    def version(self) -> str:
        return str(self.agent_info.get("version") or "")


# ── Handlers: what the agent may ask of Playground ────────────────────────

class AcpHandlers(Protocol):
    def on_update(self, session_id: str, update: dict) -> None: ...
    def request_permission(self, session_id: str, tool_call: dict, options: list[dict]) -> dict: ...
    def read_text_file(self, path: str, line: int | None, limit: int | None) -> str: ...
    def write_text_file(self, path: str, content: str) -> None: ...
    def terminal_create(self, session_id: str, command: str, args: list[str], env: list[dict],
                        cwd: str | None, output_byte_limit: int | None) -> str: ...
    def terminal_output(self, session_id: str, terminal_id: str) -> dict: ...
    def terminal_wait_for_exit(self, session_id: str, terminal_id: str) -> dict: ...
    def terminal_kill(self, session_id: str, terminal_id: str) -> None: ...
    def terminal_release(self, session_id: str, terminal_id: str) -> None: ...


def cancelled_outcome() -> dict:
    return {"outcome": {"outcome": "cancelled"}}


def selected_outcome(option_id: str) -> dict:
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


def pick_option(options: list[dict], allow: bool) -> str | None:
    """The best matching option id: once before always, for allow or reject."""
    order = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
    for kind in order:
        for opt in options or []:
            if isinstance(opt, dict) and opt.get("kind") == kind and opt.get("optionId"):
                return str(opt["optionId"])
    return None


class ProjectHandlers:
    """Handlers confined to one project folder.

    ``on_update(session_id, update)`` and ``ask(session_id, tool_call, options) -> dict``
    are the two things a caller normally supplies; the file system and the
    terminals are handled here, inside the sandbox. Without ``ask`` every
    permission request is declined, which is the safe default.
    """

    def __init__(self, project_root: str | os.PathLike, *,
                 on_update: Callable[[str, dict], None] | None = None,
                 ask: Callable[[str, dict, list[dict]], dict] | None = None,
                 policy: sandbox.SandboxPolicy | None = None,
                 on_file_written: Callable[[str], None] | None = None) -> None:
        self.project_root = os.path.realpath(os.fspath(project_root))
        self.policy = policy or sandbox.SandboxPolicy(self.project_root)
        self.terminals = sandbox.TerminalManager(self.policy)
        self._on_update = on_update
        self._ask = ask
        self._on_file_written = on_file_written

    def on_update(self, session_id: str, update: dict) -> None:
        if self._on_update:
            self._on_update(session_id, update)

    def request_permission(self, session_id: str, tool_call: dict, options: list[dict]) -> dict:
        if self._ask:
            return self._ask(session_id, tool_call, options)
        option = pick_option(options, allow=False)
        return selected_outcome(option) if option else cancelled_outcome()

    def read_text_file(self, path: str, line: int | None = None, limit: int | None = None) -> str:
        return sandbox.read_text_file(self.project_root, path, line, limit)

    def write_text_file(self, path: str, content: str) -> None:
        real = sandbox.write_text_file(self.project_root, path, content)
        if self._on_file_written:
            try:
                self._on_file_written(str(real))
            except Exception:  # noqa: BLE001
                log.exception("file-written listener failed")

    def terminal_create(self, session_id: str, command: str, args: list[str] | None = None,
                        env: list[dict] | None = None, cwd: str | None = None,
                        output_byte_limit: int | None = None) -> str:
        return self.terminals.create(command, args, env, cwd, output_byte_limit)

    def terminal_output(self, session_id: str, terminal_id: str) -> dict:
        return self.terminals.output(terminal_id)

    def terminal_wait_for_exit(self, session_id: str, terminal_id: str) -> dict:
        return self.terminals.wait_for_exit(terminal_id)

    def terminal_kill(self, session_id: str, terminal_id: str) -> None:
        self.terminals.kill(terminal_id)

    def terminal_release(self, session_id: str, terminal_id: str) -> None:
        self.terminals.release(terminal_id)

    def shutdown(self) -> None:
        self.terminals.release_all()


# ── The client ─────────────────────────────────────────────────────────────

class _Pending:
    __slots__ = ("method", "event", "result", "error")

    def __init__(self, method: str) -> None:
        self.method = method
        self.event = threading.Event()
        self.result: Any = None
        self.error: AcpError | None = None


class AcpClient:
    """One agent subprocess and its JSON-RPC connection."""

    def __init__(self, command: list[str], env: dict | None = None, cwd: str | None = None,
                 client_info: tuple[str, str] = ("Playground", VERSION),
                 handlers: AcpHandlers | None = None, *, stderr_log: str | None = None,
                 name: str = "agent", login_argv: list[str] | None = None, login_env: dict | None = None,
                 on_close: Callable[[], None] | None = None, remote_cwd: str | None = None) -> None:
        if not command:
            raise ValueError("No command to start the agent.")
        self.command = [str(c) for c in command]
        # An agent on another computer (over SSH): it works in its own copy of the project there, with its
        # own files and terminal, so Playground doesn't offer this computer's.
        self.remote_cwd = remote_cwd
        self.env = dict(os.environ if env is None else env)
        # A fenced agent signs in unfenced: its login opens a browser and saves to its own folder.
        self.login_argv = [str(c) for c in login_argv] if login_argv else list(self.command)
        self.login_env = dict(login_env) if login_env is not None else dict(self.env)
        self.confinement = None   # sandbox.AgentConfinement when the program runs fenced in
        self._on_close = on_close
        self.cwd = cwd or os.getcwd()
        self.client_info = client_info
        self.handlers = handlers
        self.stderr_log = stderr_log
        self.name = name
        self.info: InitializeResult | None = None
        self.sessions: dict[str, dict] = {}  # session id -> what session/new|load|resume returned

        self._proc: subprocess.Popen | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        self._ordered: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._closed = threading.Event()
        self._closing = False
        self._close_reason = ""
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._last_activity = time.monotonic()
        self._inflight_agent_requests = 0
        # Permission requests the person hasn't answered: rpc id -> session id.
        self._open_permissions: dict[Any, str] = {}
        self._answered: set[Any] = set()

    # ── lifecycle ──

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and not self._closed.is_set()

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def start(self, timeout: float = START_TIMEOUT) -> InitializeResult:
        """Launch the agent and run ``initialize``."""
        if self._proc is not None:
            raise RuntimeError("This agent connection was already started.")
        exe = self.command[0]
        if os.sep not in exe and shutil.which(exe, path=self.env.get("PATH")) is None:
            raise AcpError(f"Playground couldn't find {exe} on this computer.",
                           detail=f"not on PATH: {exe}")
        try:
            self._proc = subprocess.Popen(
                self.command, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except OSError as exc:
            raise AcpError(f"The agent couldn't start: {exc.strerror or exc}.", detail=repr(exc)) from exc
        for target, label in ((self._read_stdout, "reader"), (self._dispatch, "dispatch"),
                              (self._pump_stderr, "stderr")):
            thread = threading.Thread(target=target, name=f"acp-{self.name}-{label}", daemon=True)
            thread.start()
            self._threads.append(thread)
        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {
                "fs": {"readTextFile": not self.remote_cwd, "writeTextFile": not self.remote_cwd},
                "terminal": not self.remote_cwd,
                "auth": {"terminal": True},
            },
            "clientInfo": {"name": self.client_info[0], "title": self.client_info[0],
                           "version": self.client_info[1]},
        }
        try:
            result = self.request("initialize", params, timeout=timeout)
        except AcpError:
            self.close()
            raise
        self.info = InitializeResult.from_wire(result)
        if self.info.protocol_version != PROTOCOL_VERSION:
            version = self.info.protocol_version
            self.close()
            raise AcpError("This agent speaks a newer version of the protocol than Playground understands.",
                           detail=f"agent protocolVersion={version}")
        return self.info

    def close(self, grace: float = 2.0) -> None:
        """Stop the agent: close its input, then terminate its process group."""
        with self._state_lock:
            if self._closing:
                already = True
            else:
                already = False
                self._closing = True
        if already:
            self._closed.wait(grace + 6)
            return
        proc = self._proc
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self._signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._signal(signal.SIGKILL)
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        log.error("agent %s did not exit", self.name)
            # The adapter may leave children (the agent itself, MCP servers) behind.
            self._signal(signal.SIGTERM)
        self._mark_closed("Playground closed the agent.")
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:  # noqa: BLE001
                log.exception("on_close failed")
        shutdown = getattr(self.handlers, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:  # noqa: BLE001
                log.exception("handler shutdown failed")
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass

    def _signal(self, sig: int) -> None:
        if self._proc is None:
            return
        try:
            os.killpg(self._proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _mark_closed(self, reason: str) -> None:
        with self._state_lock:
            if self._closed.is_set():
                return
            self._close_reason = reason
            pending = list(self._pending.values())
            self._pending.clear()
            self._closed.set()
        for item in pending:
            item.error = AcpClosed(reason, detail=self.stderr_tail(10))
            item.event.set()
        self._ordered.put(None)

    def stderr_tail(self, lines: int = 20) -> str:
        return "\n".join(list(self._stderr_tail)[-lines:])

    def __enter__(self) -> "AcpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── wire ──

    def _send(self, message: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise AcpClosed("The agent isn't running.")
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            if self._closed.is_set():
                raise AcpClosed(self._close_reason or "The agent stopped.", detail=self.stderr_tail(10))
            try:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise AcpClosed("The agent stopped.", detail=f"{exc}\n{self.stderr_tail(10)}") from exc

    def _start_request(self, method: str, params: dict | None) -> tuple[int, _Pending]:
        rid = next(self._ids)
        pending = _Pending(method)
        with self._state_lock:
            if self._closed.is_set():
                raise AcpClosed(self._close_reason or "The agent stopped.", detail=self.stderr_tail(10))
            self._pending[rid] = pending
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        except AcpError:
            with self._state_lock:
                self._pending.pop(rid, None)
            raise
        return rid, pending

    def request(self, method: str, params: dict | None = None, timeout: float | None = REQUEST_TIMEOUT) -> Any:
        """Send a request and wait for its result."""
        rid, pending = self._start_request(method, params)
        if not pending.event.wait(timeout):
            with self._state_lock:
                self._pending.pop(rid, None)
            raise AcpTimeout("The agent took too long to answer.", detail=f"{method} timed out after {timeout}s")
        if pending.error:
            raise pending.error
        return pending.result

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _reply(self, rid: Any, result: Any = None, error: dict | None = None) -> None:
        message: dict = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result if result is not None else {}
        try:
            self._send(message)
        except AcpError:
            log.debug("could not reply to %s: agent gone", rid)

    def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stream = self._proc.stdout
        try:
            for raw in iter(stream.readline, b""):
                self._last_activity = time.monotonic()
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    self._stderr_line("[stdout, not JSON] " + line.decode("utf-8", errors="replace")[:500])
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" in message and "id" in message:
                    self._start_agent_request(message)
                elif "method" in message:
                    self._ordered.put(("notification", message))
                elif "id" in message:
                    self._ordered.put(("response", message))
        except (OSError, ValueError):
            pass
        finally:
            code = self._proc.poll()
            if not self._closing:
                try:
                    code = self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    code = None
            reason = "Playground closed the agent." if self._closing else self._crash_reason(code)
            # Through the ordered queue, so responses already read are delivered first.
            self._ordered.put(("eof", {"reason": reason}))

    def _crash_reason(self, code: int | None) -> str:
        tail = self.stderr_tail(6).lower()
        if "command not found" in tail or "enoent" in tail or "not found" in tail and "npx" in tail:
            return "The agent's program couldn't be found or installed."
        if looks_offline(tail):
            return "The agent couldn't reach the internet."
        if code is None:
            return "The agent stopped unexpectedly."
        return f"The agent stopped unexpectedly (exit code {code})."

    def _pump_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        handle = None
        written = 0
        if self.stderr_log:
            try:
                os.makedirs(os.path.dirname(self.stderr_log) or ".", exist_ok=True)
                handle = open(self.stderr_log, "a", encoding="utf-8")
                handle.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} {shlex.join(self.command)}\n")
                handle.flush()
            except OSError:
                handle = None
        try:
            for raw in iter(self._proc.stderr.readline, b""):
                text = raw.decode("utf-8", errors="replace").rstrip("\n")
                self._stderr_tail.append(text)
                if handle and written < STDERR_LOG_MAX:
                    handle.write(text + "\n")
                    handle.flush()
                    written += len(text) + 1
        except (OSError, ValueError):
            pass
        finally:
            if handle:
                handle.close()

    def _stderr_line(self, text: str) -> None:
        self._stderr_tail.append(text)

    def _dispatch(self) -> None:
        while True:
            item = self._ordered.get()
            if item is None:
                if self._closed.is_set():
                    # Drain anything still queued so no update is lost.
                    while True:
                        try:
                            rest = self._ordered.get_nowait()
                        except queue.Empty:
                            return
                        if rest is not None:
                            self._handle_ordered(*rest)
                continue
            self._handle_ordered(*item)

    def _handle_ordered(self, kind: str, message: dict) -> None:
        if kind == "eof":
            self._mark_closed(message["reason"])
            return
        if kind == "notification":
            method = message.get("method")
            params = message.get("params") or {}
            if method == "session/update":
                self._note_settings(str(params.get("sessionId", "")), dict(params.get("update") or {}))
            if method == "session/update" and self.handlers is not None:
                try:
                    self.handlers.on_update(str(params.get("sessionId", "")), dict(params.get("update") or {}))
                except Exception:  # noqa: BLE001 - a bad update must not stop the stream
                    log.exception("on_update failed")
            return
        rid = message.get("id")
        with self._state_lock:
            pending = self._pending.pop(rid, None) if isinstance(rid, int) else None
        if pending is None:
            return
        if "error" in message and message["error"] is not None:
            pending.error = _error_from_rpc(message["error"], pending.method)
        else:
            pending.result = message.get("result")
        pending.event.set()

    # ── requests from the agent ──

    def _start_agent_request(self, message: dict) -> None:
        with self._state_lock:
            self._inflight_agent_requests += 1
        thread = threading.Thread(target=self._serve_agent_request, args=(message,),
                                  name=f"acp-{self.name}-req", daemon=True)
        thread.start()

    def _serve_agent_request(self, message: dict) -> None:
        rid = message.get("id")
        method = str(message.get("method"))
        params = message.get("params") or {}
        try:
            if self.handlers is None:
                raise _RpcFailure(METHOD_NOT_FOUND, "Playground isn't ready for that.")
            result = self._call_handler(rid, method, params)
            if method == "session/request_permission":
                with self._state_lock:
                    if rid in self._answered:
                        return  # already answered "cancelled" by cancel()
                    self._answered.add(rid)
                    self._open_permissions.pop(rid, None)
            self._reply(rid, result)
        except _RpcFailure as failure:
            self._reply(rid, error={"code": failure.code, "message": failure.message})
        except FileNotFoundError as exc:
            self._reply(rid, error={"code": RESOURCE_NOT_FOUND, "message": _plain_os_error(exc, params)})
        except PermissionError as exc:
            self._reply(rid, error={"code": INVALID_PARAMS, "message": str(exc)})
        except (ValueError, KeyError, IsADirectoryError, NotADirectoryError, TimeoutError) as exc:
            message_text = exc.args[0] if exc.args else str(exc)
            self._reply(rid, error={"code": INVALID_PARAMS, "message": str(message_text)})
        except Exception as exc:  # noqa: BLE001
            log.exception("handler for %s failed", method)
            self._reply(rid, error={"code": INTERNAL_ERROR, "message": f"Playground couldn't do that: {exc}"})
        finally:
            with self._state_lock:
                self._inflight_agent_requests -= 1
                self._open_permissions.pop(rid, None)
                self._answered.discard(rid)
            self._last_activity = time.monotonic()

    def _call_handler(self, rid: Any, method: str, params: dict) -> Any:
        h = self.handlers
        session_id = str(params.get("sessionId", ""))
        if method == "session/request_permission":
            with self._state_lock:
                self._open_permissions[rid] = session_id
            result = h.request_permission(session_id, dict(params.get("toolCall") or {}),
                                          list(params.get("options") or []))
            if not isinstance(result, dict) or "outcome" not in result:
                result = cancelled_outcome()
            return result
        if method == "fs/read_text_file":
            content = h.read_text_file(_need_str(params, "path"), params.get("line"), params.get("limit"))
            return {"content": content}
        if method == "fs/write_text_file":
            content = params.get("content")
            if not isinstance(content, str):
                raise _RpcFailure(INVALID_PARAMS, "No file content was given.")
            h.write_text_file(_need_str(params, "path"), content)
            return {}
        if method == "terminal/create":
            term_id = h.terminal_create(session_id, _need_str(params, "command"), list(params.get("args") or []),
                                        list(params.get("env") or []), params.get("cwd"),
                                        params.get("outputByteLimit"))
            return {"terminalId": term_id}
        if method == "terminal/output":
            return h.terminal_output(session_id, _need_str(params, "terminalId"))
        if method == "terminal/wait_for_exit":
            return h.terminal_wait_for_exit(session_id, _need_str(params, "terminalId"))
        if method == "terminal/kill":
            h.terminal_kill(session_id, _need_str(params, "terminalId"))
            return {}
        if method == "terminal/release":
            h.terminal_release(session_id, _need_str(params, "terminalId"))
            return {}
        raise _RpcFailure(METHOD_NOT_FOUND, f"Playground doesn't support {method}.")

    # ── the protocol ──

    def _require_started(self) -> InitializeResult:
        if self.info is None:
            raise RuntimeError("Call start() first.")
        return self.info

    def auth_methods(self) -> list[dict]:
        return list(self._require_started().auth_methods)

    def authenticate(self, method_id: str, timeout: float = 15 * 60.0) -> None:
        """Ask the agent to run its own sign-in flow (``type: "agent"`` methods only)."""
        method = next((m for m in self.auth_methods() if m.get("id") == method_id), None)
        if method is None:
            raise AcpError("This agent doesn't offer that way to sign in.")
        if method.get("type") == "terminal":
            raise ValueError("That sign-in runs in a terminal; use login_command() instead.")
        self.request("authenticate", {"methodId": method_id}, timeout=timeout)

    def login_command(self, method_id: str) -> tuple[list[str], dict]:
        """The argv and env for a terminal sign-in method: the agent's own
        command plus the method's args. Show it in a terminal; exit 0 means
        signed in. Then close this client and start a new one."""
        method = next((m for m in self.auth_methods() if m.get("id") == method_id), None)
        if method is None or method.get("type") != "terminal":
            raise AcpError("This agent doesn't offer a terminal sign-in like that.")
        argv = self.login_argv + [str(a) for a in (method.get("args") or [])]
        env = dict(self.login_env)
        for key, value in (method.get("env") or {}).items():
            env[str(key)] = str(value)
        return argv, env

    def logout(self) -> None:
        if not self._require_started().can_logout:
            raise Unsupported("This agent can't sign out from here.")
        self.request("logout", {})

    # ── the agent's own settings: model, reasoning effort, mode ──

    def config_options(self, session_id: str) -> list[dict]:
        """What the agent lets a person choose for this session, as ACP session config options
        ({id, name, category, currentValue, options: [{value, name, description}]}).

        Agents on the older shape (``models`` and ``modes``) are read the same way, as the
        options ``__model`` and ``__mode``.
        """
        result = self.sessions.get(session_id) or {}
        out = [dict(o) for o in result.get("configOptions") or [] if isinstance(o, dict) and o.get("id")]
        ids = {o.get("category") for o in out}
        models = result.get("models") or {}
        if "model" not in ids and isinstance(models, dict) and models.get("availableModels"):
            out.append({"id": "__model", "name": "Model", "category": "model", "type": "select",
                        "currentValue": models.get("currentModelId"),
                        "options": [{"value": m.get("modelId"), "name": m.get("name") or m.get("modelId"),
                                     "description": m.get("description") or ""}
                                    for m in models["availableModels"] if isinstance(m, dict)]})
        modes = result.get("modes") or {}
        if "mode" not in ids and isinstance(modes, dict) and modes.get("availableModes"):
            out.append({"id": "__mode", "name": "Mode", "category": "mode", "type": "select",
                        "currentValue": modes.get("currentModeId"),
                        "options": [{"value": m.get("id"), "name": m.get("name") or m.get("id"),
                                     "description": m.get("description") or ""}
                                    for m in modes["availableModes"] if isinstance(m, dict)]})
        return out

    def set_config_option(self, session_id: str, config_id: str, value: Any) -> list[dict]:
        """Choose one of the agent's settings. Returns the options as they stand afterwards."""
        result = self.sessions.setdefault(session_id, {})
        if config_id == "__model":
            self.request("session/set_model", {"sessionId": session_id, "modelId": value})
            result.setdefault("models", {})["currentModelId"] = value
        elif config_id == "__mode":
            self.request("session/set_mode", {"sessionId": session_id, "modeId": value})
            result.setdefault("modes", {})["currentModeId"] = value
        else:
            answer = self.request("session/set_config_option",
                                  {"sessionId": session_id, "configId": config_id, "value": value}) or {}
            if isinstance(answer.get("configOptions"), list):
                result["configOptions"] = answer["configOptions"]
            else:
                for option in result.get("configOptions") or []:
                    if option.get("id") == config_id:
                        option["currentValue"] = value
        return self.config_options(session_id)

    def _note_settings(self, session_id: str, update: dict) -> None:
        """Keep the settings current when the agent changes them itself (session/update)."""
        kind = update.get("sessionUpdate")
        result = self.sessions.get(session_id)
        if result is None:
            return
        if kind == "config_option_update" and isinstance(update.get("configOptions"), list):
            result["configOptions"] = update["configOptions"]
        elif kind == "current_mode_update" and update.get("currentModeId"):
            result.setdefault("modes", {})["currentModeId"] = update["currentModeId"]

    def new_session(self, cwd: str, mcp_servers: list[dict] | None = None,
                    timeout: float = SESSION_TIMEOUT) -> str:
        self._require_started()
        result = self.request("session/new", {"cwd": self.remote_cwd or os.path.abspath(cwd), "mcpServers": mcp_servers or []},
                              timeout=timeout) or {}
        session_id = result.get("sessionId")
        if not session_id:
            raise AcpError("The agent didn't start a conversation.", detail=json.dumps(result)[:500])
        self.sessions[str(session_id)] = result
        return str(session_id)

    def load_session(self, session_id: str, cwd: str, mcp_servers: list[dict] | None = None,
                     timeout: float = SESSION_TIMEOUT) -> dict:
        """Reopen a session; the agent replays it as ``session/update``s first."""
        if not self._require_started().can_load:
            raise Unsupported("This agent can't reopen earlier conversations.")
        result = self.request("session/load", {"sessionId": session_id, "cwd": self.remote_cwd or os.path.abspath(cwd),
                                               "mcpServers": mcp_servers or []}, timeout=timeout) or {}
        self.sessions[session_id] = result
        return result

    def resume_session(self, session_id: str, cwd: str, mcp_servers: list[dict] | None = None,
                       timeout: float = SESSION_TIMEOUT) -> dict:
        """Reopen a session without replaying it."""
        if not self._require_started().can_resume:
            raise Unsupported("This agent can't pick up earlier conversations.")
        result = self.request("session/resume", {"sessionId": session_id, "cwd": self.remote_cwd or os.path.abspath(cwd),
                                                 "mcpServers": mcp_servers or []}, timeout=timeout) or {}
        self.sessions[session_id] = result
        return result

    def reopen_session(self, session_id: str, cwd: str, mcp_servers: list[dict] | None = None) -> str:
        """Resume if possible, else load. Returns "resumed" or "loaded"; raises Unsupported."""
        info = self._require_started()
        if info.can_resume:
            self.resume_session(session_id, cwd, mcp_servers)
            return "resumed"
        if info.can_load:
            self.load_session(session_id, cwd, mcp_servers)
            return "loaded"
        raise Unsupported("This agent can't pick up earlier conversations.")

    def list_sessions(self, cwd: str | None = None, max_pages: int = 20) -> list[dict]:
        if not self._require_started().can_list:
            raise Unsupported("This agent can't list earlier conversations.")
        sessions: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            params: dict = {}
            if cwd:
                params["cwd"] = self.remote_cwd or os.path.abspath(cwd)
            if cursor:
                params["cursor"] = cursor
            result = self.request("session/list", params) or {}
            sessions.extend(s for s in result.get("sessions") or [] if isinstance(s, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return sessions

    def close_session(self, session_id: str) -> None:
        if not self._require_started().can_close:
            raise Unsupported("This agent can't close conversations.")
        self.request("session/close", {"sessionId": session_id})
        self.sessions.pop(session_id, None)

    def set_mode(self, session_id: str, mode_id: str) -> None:
        self.request("session/set_mode", {"sessionId": session_id, "modeId": mode_id})

    def available_modes(self, session_id: str) -> list[dict]:
        modes = (self.sessions.get(session_id) or {}).get("modes") or {}
        return [m for m in modes.get("availableModes") or [] if isinstance(m, dict)]

    def prompt(self, session_id: str, blocks: list[dict], *, idle_timeout: float = PROMPT_IDLE_TIMEOUT,
               timeout: float | None = None) -> StopReason:
        """Send a prompt and block until the turn ends. Updates stream to the handlers."""
        rid, pending = self._start_request("session/prompt", {"sessionId": session_id, "prompt": blocks})
        started = time.monotonic()
        self._last_activity = started
        while not pending.event.wait(1.0):
            now = time.monotonic()
            with self._state_lock:
                busy = self._inflight_agent_requests > 0
            if timeout is not None and now - started > timeout:
                self._give_up(rid, session_id)
                raise AcpTimeout("The agent took too long, so Playground stopped it.",
                                 detail=f"prompt exceeded {timeout}s")
            if not busy and now - self._last_activity > idle_timeout:
                self._give_up(rid, session_id)
                minutes = int(idle_timeout // 60)
                raise AcpTimeout(f"The agent went quiet for {minutes} minutes, so Playground stopped it.",
                                 detail="prompt idle timeout")
        if pending.error:
            raise pending.error
        result = pending.result or {}
        return str(result.get("stopReason") or "end_turn")

    def _give_up(self, rid: int, session_id: str) -> None:
        try:
            self.cancel(session_id)
        except AcpError:
            pass
        with self._state_lock:
            self._pending.pop(rid, None)

    def cancel(self, session_id: str) -> None:
        """Ask the agent to stop the current turn. Open permission questions
        for that session are answered "cancelled" straight away, as the spec requires."""
        with self._state_lock:
            open_ids = [rid for rid, sid in self._open_permissions.items() if sid == session_id or not sid]
            for rid in open_ids:
                self._answered.add(rid)
                self._open_permissions.pop(rid, None)
        for rid in open_ids:
            self._reply(rid, cancelled_outcome())
        cancelled = getattr(self.handlers, "permissions_cancelled", None)
        if callable(cancelled) and open_ids:
            try:
                cancelled(session_id)
            except Exception:  # noqa: BLE001
                log.exception("permissions_cancelled failed")
        self.notify("session/cancel", {"sessionId": session_id})


class _RpcFailure(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _need_str(params: dict, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise _RpcFailure(INVALID_PARAMS, f"{key} is missing.")
    return value


def _plain_os_error(exc: OSError, params: dict) -> str:
    path = params.get("path") or exc.filename or "That file"
    return f"{path} doesn't exist."


# ── Terminal sign-in ───────────────────────────────────────────────────────

class LoginTerminal:
    """Runs an agent's own sign-in where the person can see and type.

    ``via`` says how: a terminal window ("ptyxis", "gnome-terminal", "Terminal",
    ...) or "pty", where the caller shows the output (``on_output``) and passes
    keystrokes in (``write``). Exit code 0 means signed in. Playground never
    reads what is typed or printed for secrets, and stores nothing.

    Note for the UI on Luma: Luma's Terminal can't run a given command yet, so
    an embedded VTE (``argv``/``env`` from ``AcpClient.login_command``) is the
    nicest option; this class is the fallback.
    """

    EMULATORS = (
        ("ptyxis", ["ptyxis", "--new-window", "--"]),
        ("gnome-terminal", ["gnome-terminal", "--wait", "--"]),
        ("kgx", ["kgx", "-e"]),
        ("konsole", ["konsole", "-e"]),
        ("xfce4-terminal", ["xfce4-terminal", "-x"]),
        ("x-terminal-emulator", ["x-terminal-emulator", "-e"]),
        ("xterm", ["xterm", "-e"]),
    )

    def __init__(self, argv: list[str], env: dict, cwd: str | None = None, *, prefer: str = "auto",
                 on_output: Callable[[str], None] | None = None) -> None:
        self.argv = list(argv)
        self.env = dict(env)
        self.cwd = cwd or os.path.expanduser("~")
        self.prefer = prefer
        self.on_output = on_output
        self.via = ""
        self._proc: subprocess.Popen | None = None
        self._master: int | None = None
        self._status_file: str | None = None
        self._tmpdir: str | None = None
        self._done = threading.Event()
        self.exit_code: int | None = None

    def start(self) -> str:
        if self.prefer in ("auto", "window"):
            if self._start_window():
                return self.via
            if self.prefer == "window":
                raise AcpError("Playground couldn't open a terminal window for sign-in.")
        self._start_pty()
        return self.via

    def _script(self) -> str:
        """A tiny launcher that carries the env and records the exit status.
        Only variables that differ from Playground's own and aren't secrets
        are written; it lives in a private temp folder and is removed after."""
        self._tmpdir = tempfile.mkdtemp(prefix="pg-login-")
        os.chmod(self._tmpdir, 0o700)
        self._status_file = os.path.join(self._tmpdir, "status")
        lines = ["#!/bin/sh"]
        for key, value in self.env.items():
            if os.environ.get(key) != value and not sandbox._SECRET_NAME.search(key):
                lines.append(f"export {key}={shlex.quote(value)}")
        lines.append(f"cd {shlex.quote(self.cwd)} || exit 1")
        lines.append(shlex.join(self.argv))
        lines.append(f'echo $? > {shlex.quote(self._status_file)}')
        if sys.platform == "darwin":
            lines.append('echo; echo "You can close this window."')
        path = os.path.join(self._tmpdir, "sign-in.command")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(path, 0o700)
        return path

    def _start_window(self) -> bool:
        if sys.platform == "darwin":
            script = self._script()
            try:
                subprocess.run(["open", "-a", "Terminal", script], check=True, timeout=15,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                return False
            self.via = "Terminal"
            threading.Thread(target=self._poll_status, daemon=True, name="pg-login-wait").start()
            return True
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False
        for name, prefix in self.EMULATORS:
            if shutil.which(prefix[0]):
                script = self._script()
                try:
                    self._proc = subprocess.Popen(prefix + ["/bin/sh", script], env=self.env,
                                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                  stderr=subprocess.DEVNULL, start_new_session=True)
                except OSError:
                    continue
                self.via = name
                threading.Thread(target=self._poll_status, daemon=True, name="pg-login-wait").start()
                return True
        return False

    def _poll_status(self, limit: float = 30 * 60.0) -> None:
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if self._status_file and os.path.exists(self._status_file):
                time.sleep(0.2)
                try:
                    with open(self._status_file, encoding="utf-8") as handle:
                        self.exit_code = int(handle.read().strip() or "1")
                except (OSError, ValueError):
                    self.exit_code = 1
                break
            if self._proc is not None and self._proc.poll() is not None and self.via != "gnome-terminal":
                # Some emulators return at once; keep waiting for the status file.
                pass
            time.sleep(0.5)
        self._cleanup()
        self._done.set()

    def _start_pty(self) -> None:
        import pty  # POSIX only; imported here so the module loads anywhere
        master, slave = pty.openpty()
        try:
            self._proc = subprocess.Popen(self.argv, env=self.env, cwd=self.cwd, stdin=slave, stdout=slave,
                                          stderr=slave, start_new_session=True, close_fds=True)
        finally:
            os.close(slave)
        self._master = master
        self.via = "pty"
        threading.Thread(target=self._pump_pty, daemon=True, name="pg-login-pty").start()

    def _pump_pty(self) -> None:
        assert self._master is not None and self._proc is not None
        try:
            while True:
                try:
                    data = os.read(self._master, 4096)
                except OSError:
                    break
                if not data:
                    break
                if self.on_output:
                    try:
                        self.on_output(data.decode("utf-8", errors="replace"))
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            self.exit_code = self._proc.wait()
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = None
            self._done.set()

    def write(self, text: str) -> None:
        if self._master is None:
            raise AcpError("This sign-in isn't taking typed input here.")
        os.write(self._master, text.encode("utf-8"))

    def wait(self, timeout: float | None = None) -> int | None:
        """Exit code, or None if still running after ``timeout``."""
        if not self._done.wait(timeout):
            return None
        return self.exit_code

    def cancel(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
            except OSError:
                pass
        if self.via not in ("pty", ""):
            self.exit_code = 1
            self._cleanup()
            self._done.set()

    def _cleanup(self) -> None:
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def resource_link_block(path: str, name: str | None = None) -> dict:
    return {"type": "resource_link", "uri": Path(path).resolve().as_uri(), "name": name or Path(path).name}
