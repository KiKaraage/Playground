# SPDX-License-Identifier: Apache-2.0
"""Ollama on this computer: is it there, which models it has, and Ari's chat with it.

Playground talks to Ollama's own API (``/api/...``), not its OpenAI-compatible
one, because only the native API takes the context window per request
(``options.num_ctx``). Ollama's default window is a few thousand tokens, far too
small for an agent that reads files, so every request asks for at least 64k.

Everything a person might read is plain words with a next step: Ollama isn't
installed, isn't running, has no models yet, doesn't have the chosen model,
or the model can't use tools.

No GTK imports. Tests use a fake Ollama server on localhost.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import shutil
import socket
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

from .ari import AriError

DEFAULT_HOST = "http://127.0.0.1:11434"
MIN_CONTEXT = 65536
# What to suggest when there's nothing to run yet: a small model that can use tools.
SUGGESTED_MODEL = "qwen3"
PROBE_TIMEOUT_S = 2.0


class OllamaError(AriError):
    """Something about Ollama, in plain words. ``next_step`` says what to do."""

    def __init__(self, message: str, *, next_step: str = "", reason: str = "", detail: str = "") -> None:
        super().__init__(message + (f" {next_step}" if next_step else ""), detail=detail, reason=reason or None)
        self.next_step = next_step


def pull_step(model: str = SUGGESTED_MODEL) -> str:
    return (f"Open a terminal and run: ollama pull {model or SUGGESTED_MODEL}. "
            "It downloads a few gigabytes, so make sure there's room. Then try again.")


START_STEP = "Open the Ollama app, or run: ollama serve, in a terminal. Then try again."
INSTALL_STEP = "Install Ollama from ollama.com, start it, then come back."


def host_from(base_url: str | None) -> str:
    """``http://localhost:11434/v1`` (what a connection stores) → ``http://localhost:11434``."""
    url = (base_url or DEFAULT_HOST).strip().rstrip("/")
    for suffix in ("/v1", "/api"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url or DEFAULT_HOST


@dataclass
class Model:
    name: str
    size: int = 0
    parameter_size: str = ""
    family: str = ""
    context_length: int | None = None    # the most the model was trained for, when Ollama says
    tools: bool | None = None            # can it call tools (None: Ollama didn't say)

    @property
    def size_text(self) -> str:
        if not self.size:
            return ""
        gb = self.size / 1e9
        return f"{gb:.1f} GB" if gb >= 1 else f"{self.size / 1e6:.0f} MB"

    def to_dict(self) -> dict:
        return {"name": self.name, "size": self.size, "size_text": self.size_text,
                "parameter_size": self.parameter_size, "family": self.family,
                "context_length": self.context_length, "tools": self.tools}


@dataclass
class Status:
    installed: bool = False
    running: bool = False
    version: str = ""
    host: str = DEFAULT_HOST
    models: list[Model] = field(default_factory=list)
    note: str = ""          # what's going on, in plain words
    next_step: str = ""     # what to do about it, or ""

    @property
    def ready(self) -> bool:
        return self.running and bool(self.models)

    def to_dict(self) -> dict:
        return {"installed": self.installed, "running": self.running, "version": self.version, "host": self.host,
                "models": [m.to_dict() for m in self.models], "note": self.note, "next_step": self.next_step,
                "ready": self.ready}


def _get_json(url: str, opener: Callable, timeout: float, body: dict | None = None) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                     headers={"Content-Type": "application/json"} if body is not None else {})
    with opener(request, timeout=timeout) as response:
        return json.loads(response.read(4 << 20).decode("utf-8"))


def _model_from(item: dict) -> Model:
    details = item.get("details") or {}
    return Model(name=str(item.get("name") or item.get("model") or ""), size=int(item.get("size") or 0),
                 parameter_size=str(details.get("parameter_size") or ""), family=str(details.get("family") or ""))


def list_models(host: str = DEFAULT_HOST, *, opener: Callable = urllib.request.urlopen,
                timeout: float = PROBE_TIMEOUT_S) -> list[Model]:
    data = _get_json(f"{host_from(host)}/api/tags", opener, timeout)
    return [m for m in (_model_from(i) for i in data.get("models") or [] if isinstance(i, dict)) if m.name]


# A first model sized to the computer: (memory it needs at least, model, about how much it downloads).
STARTER_MODELS = ((48, "qwen3:32b", "20 GB"), (16, "qwen3:8b", "5 GB"), (0, "qwen3:4b", "2.5 GB"))
PULL_TIMEOUT_S = 6 * 60 * 60


def memory_gb() -> float:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (ValueError, OSError, AttributeError):
        return 0.0


def starter_model(memory: float | None = None) -> tuple[str, str]:
    """(model, download size) for a first local model this computer can run well."""
    memory = memory_gb() if memory is None else memory
    for least, model, size in STARTER_MODELS:
        if memory >= least:
            return model, size
    return STARTER_MODELS[-1][1], STARTER_MODELS[-1][2]


def pull(model: str, host: str = DEFAULT_HOST, *, on_progress: Callable[[str, float | None], None] | None = None,
         opener: Callable = urllib.request.urlopen, timeout: float = PULL_TIMEOUT_S) -> None:
    """Download a model through Ollama's own API, telling ``on_progress(words, part)`` as it goes."""
    request = urllib.request.Request(f"{host_from(host)}/api/pull", data=json.dumps({"model": model}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
    try:
        with opener(request, timeout=timeout) as response:
            for raw in response:
                if not raw.strip():
                    continue
                event = json.loads(raw.decode("utf-8"))
                if event.get("error"):
                    raise OllamaError(f"Ollama couldn't download {model}: {event['error']}")
                total, done = event.get("total") or 0, event.get("completed") or 0
                if on_progress is not None:
                    status = str(event.get("status") or "")
                    words = "Downloading" if status.startswith("pulling") and total else status.capitalize()
                    on_progress(words, done / total if total else None)
                if event.get("status") == "success":
                    return
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise plain_network_error(error) from error
    raise OllamaError(f"Ollama stopped before {model} finished downloading.")


def show(model: str, host: str = DEFAULT_HOST, *, opener: Callable = urllib.request.urlopen,
         timeout: float = 5.0) -> Model:
    """A model's details: its trained context length and whether it can use tools."""
    data = _get_json(f"{host_from(host)}/api/show", opener, timeout, {"model": model})
    info = data.get("model_info") or {}
    context = None
    for key, value in info.items():
        if key.endswith(".context_length") and isinstance(value, int):
            context = value
            break
    caps = data.get("capabilities")
    details = data.get("details") or {}
    return Model(name=model, parameter_size=str(details.get("parameter_size") or ""),
                 family=str(details.get("family") or ""), context_length=context,
                 tools=("tools" in caps) if isinstance(caps, list) else None)


def status(host: str = DEFAULT_HOST, *, opener: Callable = urllib.request.urlopen,
           which: Callable[[str], str | None] = shutil.which, timeout: float = PROBE_TIMEOUT_S) -> Status:
    """Is Ollama here and running, and what can it run? Never raises."""
    host = host_from(host)
    out = Status(installed=bool(which("ollama")), host=host)
    try:
        version = _get_json(f"{host}/api/version", opener, timeout)
        out.version = str(version.get("version") or "")
        out.running = out.installed = True
        out.models = list_models(host, opener=opener, timeout=timeout)
    except (OSError, ValueError, urllib.error.URLError):
        out.running = False
    if not out.installed:
        out.note, out.next_step = "Ollama isn't on this computer.", INSTALL_STEP
    elif not out.running:
        out.note, out.next_step = "Ollama is installed but isn't running.", START_STEP
    elif not out.models:
        out.note, out.next_step = "Ollama is running, but it has no models yet.", pull_step()
    else:
        count = len(out.models)
        out.note = f"Running, with {count} model{'s' if count != 1 else ''}."
    return out


def context_for(wanted: int | None) -> int:
    """The window Playground asks for: never below 64k."""
    try:
        return max(MIN_CONTEXT, int(wanted or 0))
    except (TypeError, ValueError):
        return MIN_CONTEXT


# ── Chat requests (Ari's model calls) ──────────────────────────────────────

def chat_body(model: str, messages: list[dict], tools: list[dict], context: int) -> dict:
    """An ``/api/chat`` request from Ari's OpenAI-shaped history."""
    names: dict[str, str] = {}
    out: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
            calls = []
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args or "{}")
                    except ValueError:
                        args = {}
                names[str(call.get("id") or "")] = str(fn.get("name") or "")
                calls.append({"function": {"name": fn.get("name"), "arguments": args if isinstance(args, dict) else {}}})
            if calls:
                item["tool_calls"] = calls
            out.append(item)
        elif role == "tool":
            item = {"role": "tool", "content": str(message.get("content") or "")}
            name = names.get(str(message.get("tool_call_id") or ""))
            if name:
                item["tool_name"] = name
            out.append(item)
        else:
            out.append({"role": role or "user", "content": str(message.get("content") or "")})
    body: dict[str, Any] = {"model": model, "messages": out, "stream": True,
                            "options": {"num_ctx": context_for(context)}}
    if tools:
        body["tools"] = tools
    return body


def plain_http_error(code: int, detail: str, model: str) -> OllamaError:
    text = detail
    try:
        text = str(json.loads(detail).get("error") or detail)
    except (ValueError, AttributeError):
        pass
    low = text.lower()
    if code == 404 or "not found" in low and "model" in low:
        return OllamaError(f"The model {model} isn't downloaded yet.", next_step=pull_step(model),
                           reason="no-model", detail=detail)
    if "does not support tools" in low:
        return OllamaError(f"{model} can't use tools, and Ari needs them to work on your project.",
                           next_step=f"Pick a model that can, like {SUGGESTED_MODEL}, or run: ollama pull "
                                     f"{SUGGESTED_MODEL}.", reason="no-tools", detail=detail)
    if "memory" in low:
        return OllamaError(f"{model} needs more memory than this computer has free right now.",
                           next_step="Close some apps, or pick a smaller model.", reason="memory", detail=detail)
    return OllamaError("Ollama ran into a problem with that request." if code >= 500 else
                       f"Ollama said no ({text[:160]}).", reason="", detail=detail)


def plain_network_error(error: Exception) -> OllamaError:
    reason = getattr(error, "reason", error)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return OllamaError("The model took too long to answer.", detail=str(error))
    return OllamaError("Ari can't reach Ollama on this computer.", next_step=START_STEP, reason="not-running",
                       detail=str(error))


def read_chat_stream(lines, on_text: Callable[[str], None], cancelled: Callable[[], bool]) -> dict:
    """Read an ``/api/chat`` NDJSON stream. Returns content, tool_calls, finish_reason and usage."""
    content: list[str] = []
    calls: list[dict] = []
    finish = ""
    usage: dict = {}
    for raw in lines:
        if cancelled():
            raise InterruptedError
        line = raw.decode("utf-8", errors="replace").strip() if isinstance(raw, bytes) else str(raw).strip()
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except ValueError:
            continue
        if chunk.get("error"):
            raise plain_http_error(500, json.dumps({"error": chunk["error"]}), str(chunk.get("model") or "the model"))
        message = chunk.get("message") or {}
        text = message.get("content")
        if text:
            content.append(text)
            on_text(text)
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            if not fn.get("name"):
                continue
            args = fn.get("arguments")
            calls.append({"id": str(call.get("id") or f"call_{uuid.uuid4().hex[:8]}"), "name": str(fn["name"]),
                          "arguments": args if isinstance(args, str) else json.dumps(args or {})})
        if chunk.get("done"):
            finish = {"length": "length", "stop": "stop"}.get(str(chunk.get("done_reason") or ""), "stop")
            usage = {"prompt_tokens": int(chunk.get("prompt_eval_count") or 0),
                     "completion_tokens": int(chunk.get("eval_count") or 0)}
            break
    if calls and finish != "length":
        finish = "tool_calls"
    return {"content": "".join(content), "tool_calls": calls, "finish_reason": finish or "stop", "usage": usage}


def chat_request(host: str, body: dict) -> urllib.request.Request:
    return urllib.request.Request(f"{host_from(host)}/api/chat", data=json.dumps(body).encode("utf-8"),
                                  headers={"Content-Type": "application/json", "User-Agent": "Luma-Playground-Ari"},
                                  method="POST")


def choose_model(host: str, wanted: str, *, opener: Callable = urllib.request.urlopen) -> str:
    """The model to use: the chosen one, else the first Ollama has. Plain words when there's none."""
    if wanted:
        return wanted
    try:
        models = list_models(host, opener=opener)
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise plain_network_error(error) from error
    if not models:
        raise OllamaError("Ollama is running, but it has no models yet.", next_step=pull_step(), reason="no-model")
    return models[0].name
