# SPDX-License-Identifier: Apache-2.0
"""A local, scripted OpenAI-compatible Chat Completions server for Ari's tests.

    server = FakeOpenAI([
        {"text": "Let me write it.", "tool_calls": [{"name": "write_file", "arguments": {"path": "a", "content": "b"}}]},
        {"text": "Done."},
    ])
    base_url = server.start()      # http://127.0.0.1:<port>/v1
    ...
    server.stop()

Each request takes the next step of the script and streams it the way OpenAI
does: text in small deltas, tool calls with their arguments split across
chunks, a finish_reason, a usage chunk, then ``data: [DONE]``. A step may
instead give ``"status": 401`` (an error response) or ``"stall": seconds``
(say nothing, to test cancelling). Requests are kept in ``server.requests``.

The same script also drives two other wire formats, chosen by the path:

- **Ollama's own API**: ``POST /api/chat`` streams NDJSON (text pieces, tool
  calls with object arguments, a ``done`` line with token counts);
  ``GET /api/tags`` lists ``server.models``, ``GET /api/version`` answers,
  ``POST /api/show`` describes a model. An error step answers ``{"error": ...}``.
- **The Responses API** (Sign in with ChatGPT): ``POST .../responses`` streams
  SSE events (``response.output_text.delta``, function-call items and argument
  deltas, ``response.completed`` with usage). A step may give ``"fail_code"``
  (a ``response.failed`` mid-stream) or ``"cut": true`` (the stream just ends).
  ``GET .../models`` lists ``server.chatgpt_models``.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socketserver
import threading
import time


class _QuickServer(ThreadingHTTPServer):
    """Skips the reverse DNS lookup HTTPServer does on bind, which can take
    many seconds on a Mac with no network."""
    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class FakeOpenAI:
    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        # Set once a "stall" step has sent its headers and gone quiet: the client is now waiting
        # on the model, so a test can cancel at exactly that moment instead of guessing with a sleep.
        self.stalling = threading.Event()
        self.models: list[dict] = [{"name": "qwen3:8b", "size": 5_200_000_000,
                                    "details": {"parameter_size": "8.2B", "family": "qwen3"}}]
        self.chatgpt_models: list[dict] = [{"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list"},
                                           {"slug": "hidden-model", "display_name": "Hidden", "visibility": "hide"}]
        self.gets: list[tuple[str, dict]] = []

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # quiet
                pass

            def _json(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                with fake._lock:
                    fake.gets.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                if self.path == "/api/tags":
                    self._json(200, {"models": fake.models})
                elif self.path == "/api/version":
                    self._json(200, {"version": "0.12.3"})
                elif self.path.endswith("/models"):
                    self._json(200, {"models": fake.chatgpt_models})
                else:
                    self._json(404, {"error": "no such path"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/show":
                    self._json(200, {"model_info": {"qwen3.context_length": 40960},
                                     "capabilities": ["completion", "tools"],
                                     "details": {"parameter_size": "8.2B", "family": "qwen3"}})
                    return
                with fake._lock:
                    fake.requests.append(body)
                    fake.headers.append({k.lower(): v for k, v in self.headers.items()})
                    step = fake.script.pop(0) if fake.script else {"text": "(script finished)"}
                native = self.path == "/api/chat"
                responses = self.path.endswith("/responses")
                if not (native or responses or self.path.endswith("/chat/completions")):
                    self._error(404, "no such path")
                    return
                status = int(step.get("status", 200))
                if status != 200:
                    if native:
                        self._json(status, {"error": step.get("message", "error")})
                    elif responses:
                        self._json(status, {"error": {"code": step.get("code", ""), "message": step.get("message", "")}})
                    else:
                        self._error(status, step.get("message", "error"))
                    return
                if native:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if step.get("stall"):
                        fake.stalling.set()
                        fake.stop_event.wait(float(step["stall"]))
                        return
                    try:
                        self._native(step, body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if responses:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        self._responses(step, body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                if step.get("stall"):
                    fake.stalling.set()
                    fake.stop_event.wait(float(step["stall"]))
                    return
                try:
                    self._stream(step, body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _line(self, payload: dict) -> None:
                self.wfile.write(json.dumps(payload).encode() + b"\n")
                self.wfile.flush()

            def _native(self, step: dict, body: dict) -> None:
                model = body.get("model", "")
                text = step.get("text", "")
                for i in range(0, len(text), 5):
                    self._line({"model": model, "message": {"role": "assistant", "content": text[i:i + 5]},
                                "done": False})
                    if step.get("slow"):
                        time.sleep(step["slow"])
                if step.get("tool_calls"):
                    calls = [{"function": {"name": c["name"], "arguments": c.get("arguments", {})}}
                             for c in step["tool_calls"]]
                    self._line({"model": model, "message": {"role": "assistant", "content": "", "tool_calls": calls},
                                "done": False})
                if step.get("error"):
                    self._line({"error": step["error"]})
                    return
                self._line({"model": model, "message": {"role": "assistant", "content": ""}, "done": True,
                            "done_reason": step.get("finish", "stop"), "prompt_eval_count": 100, "eval_count": 20})

            def _event(self, payload: dict) -> None:
                self.wfile.write(f"event: {payload['type']}\n".encode() + b"data: " + json.dumps(payload).encode()
                                 + b"\n\n")
                self.wfile.flush()

            def _responses(self, step: dict, body: dict) -> None:
                self._event({"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}})
                text = step.get("text", "")
                if text:
                    self._event({"type": "response.output_item.added", "output_index": 0,
                                 "item": {"type": "message", "id": "msg_1", "role": "assistant", "content": []}})
                for i in range(0, len(text), 5):
                    self._event({"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0,
                                 "content_index": 0, "delta": text[i:i + 5]})
                if step.get("fail_code"):
                    self._event({"type": "response.failed", "response": {"id": "resp_1", "status": "failed",
                                 "error": {"code": step["fail_code"], "message": "limit"}}})
                    return
                for index, call in enumerate(step.get("tool_calls") or [], start=1):
                    args = json.dumps(call.get("arguments", {}))
                    item = {"type": "function_call", "id": f"fc_{index}", "call_id": f"call_{index}_{len(fake.requests)}",
                            "name": call["name"], "arguments": ""}
                    self._event({"type": "response.output_item.added", "output_index": index, "item": item})
                    for j in range(0, len(args), 7):
                        self._event({"type": "response.function_call_arguments.delta", "item_id": item["id"],
                                     "output_index": index, "delta": args[j:j + 7]})
                    self._event({"type": "response.output_item.done", "output_index": index,
                                 "item": {**item, "arguments": args}})
                if step.get("cut"):
                    return
                self._event({"type": "response.completed", "response": {"id": "resp_1", "status": "completed",
                             "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}})

            def _chunk(self, payload: dict) -> None:
                self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
                self.wfile.flush()

            def _stream(self, step: dict, body: dict) -> None:
                base = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "model": body.get("model", "")}
                self._chunk({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]})
                text = step.get("text", "")
                for i in range(0, len(text), 5):
                    self._chunk({**base, "choices": [{"index": 0, "delta": {"content": text[i:i + 5]}}]})
                    if step.get("slow"):
                        time.sleep(step["slow"])
                for index, call in enumerate(step.get("tool_calls") or []):
                    args = json.dumps(call.get("arguments", {}))
                    first = {"index": index, "id": call.get("id", f"call_{index}_{len(fake.requests)}"),
                             "type": "function", "function": {"name": call["name"], "arguments": ""}}
                    self._chunk({**base, "choices": [{"index": 0, "delta": {"tool_calls": [first]}}]})
                    for j in range(0, len(args), 7):
                        piece = {"index": index, "function": {"arguments": args[j:j + 7]}}
                        self._chunk({**base, "choices": [{"index": 0, "delta": {"tool_calls": [piece]}}]})
                finish = "tool_calls" if step.get("tool_calls") else step.get("finish", "stop")
                self._chunk({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
                self._chunk({**base, "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                                                              "total_tokens": 120}})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def _error(self, status: int, message: str) -> None:
                data = json.dumps({"error": {"message": message, "type": "test"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

        self._server = _QuickServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="fake-openai")
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def stop(self) -> None:
        self.stop_event.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
