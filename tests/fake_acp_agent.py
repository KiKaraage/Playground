#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""A small but complete ACP v1 agent for tests. Standard library only.

It speaks newline-delimited JSON-RPC on stdin/stdout, like a real adapter, and
plays a scripted turn: a plan, streamed text, a tool call that asks
permission and writes a file through the client, a terminal command, a read
back, an attempted escape, a usage report and an end_turn.

Behaviour switches (environment):
  FAKE_ACP_STATE   a folder for markers (sign-in, call log)
  FAKE_ACP_AUTH=1  session/new fails with auth_required until signed in;
                   "--login" on the command line is the terminal sign-in
                   (writes the marker and exits 0)
  FAKE_ACP_AUTH_EXPIRED=1  sessions start, but a prompt says "Failed to
                   authenticate" and fails with auth_required until signed in
                   (Claude's expired sign-in, 9 Oct 2026)

Prompt words that change the turn:
  "cancel-me"  asks permission, then waits for session/cancel
  "crash"      exits mid-turn
  "quiet"      says nothing for a long time (and ignores session/cancel)
  "busy-work"  says a word, then works until session/cancel
  "mcp-ask"    writes a pending decision into .playground/pending, like the MCP server would
  "two-steps"  tool call, then talk, then another tool call
  "crash-once" crashes mid-turn the first time only (after writing part.txt)
  "offline"    fails the prompt the way an agent does when the internet is gone
  "report"     a tool call, then playground_step_report (written to the thread
               the way the MCP server writes it), then talk
  "escape"     tries to reach outside the project with its own hands (no ACP):
               writes next to the project, lists and reads the home folder
"""
import json
import os
import sys
import threading
import time

STATE = os.environ.get("FAKE_ACP_STATE", "")
AUTH = os.environ.get("FAKE_ACP_AUTH") == "1"
AUTH_EXPIRED = os.environ.get("FAKE_ACP_AUTH_EXPIRED") == "1"
_write_lock = threading.Lock()
_pending = {}
_pending_lock = threading.Lock()
_next_id = [1000]
_cancelled = set()
_sessions = {}


def log(text):
    sys.stderr.write(f"[fake-agent] {text}\n")
    sys.stderr.flush()


def record(event):
    if STATE:
        with open(os.path.join(STATE, "calls.jsonl"), "a") as handle:
            handle.write(json.dumps(event) + "\n")


def signed_in():
    return not AUTH or (STATE and os.path.exists(os.path.join(STATE, "signed-in")))


def marker(name):
    return bool(STATE) and os.path.exists(os.path.join(STATE, name))


def touch(name):
    if STATE:
        open(os.path.join(STATE, name), "w").close()


def send(message):
    data = json.dumps(message) + "\n"
    with _write_lock:
        sys.stdout.write(data)
        sys.stdout.flush()


def reply(rid, result=None, error=None):
    message = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result if result is not None else {}
    send(message)


def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})


def update(session_id, payload):
    notify("session/update", {"sessionId": session_id, "update": payload})


def call(method, params, timeout=30):
    with _pending_lock:
        _next_id[0] += 1
        rid = _next_id[0]
        event = threading.Event()
        _pending[rid] = [event, None]
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    if not event.wait(timeout):
        raise RuntimeError(f"client did not answer {method}")
    with _pending_lock:
        _, response = _pending.pop(rid)
    if "error" in response:
        return None, response["error"]
    return response.get("result"), None


def plan(session_id, statuses):
    titles = ["Write hello.txt", "Check it worked"]
    update(session_id, {"sessionUpdate": "plan", "entries": [
        {"content": t, "priority": "high", "status": s} for t, s in zip(titles, statuses)]})


def turn(rid, params):
    session_id = params["sessionId"]
    text = " ".join(b.get("text", "") for b in params.get("prompt") or [] if b.get("type") == "text")
    record({"prompt": text})
    cwd = _sessions.get(session_id, {}).get("cwd") or os.getcwd()

    record({"prompt_session": session_id})
    if AUTH_EXPIRED and not marker("signed-in"):
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text":
                            "Failed to authenticate: OAuth session expired and could not be refreshed"}})
        time.sleep(0.1)
        reply(rid, error={"code": -32000, "message": "Authentication required"})
        return
    if "offline" in text:
        reply(rid, error={"code": -32603, "message": "Internal error",
                          "data": {"details": "API Error: Connection error. getaddrinfo ENOTFOUND api.example.com"}})
        return
    if "crash-once" in text:
        if not marker("crashed-once"):
            touch("crashed-once")
            update(session_id, {"sessionUpdate": "tool_call", "toolCallId": "w1", "title": "Write part.txt",
                                "kind": "edit", "status": "in_progress",
                                "locations": [{"path": os.path.join(cwd, "part.txt")}]})
            with open(os.path.join(cwd, "part.txt"), "w") as handle:
                handle.write("half")
            time.sleep(0.2)
            log("crashing on purpose, once")
            os._exit(4)
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text",
                            "text": "Picked up where I left off."}})
        reply(rid, {"stopReason": "end_turn"})
        return
    if "report" in text:
        target = os.path.join(cwd, "button.html")
        update(session_id, {"sessionUpdate": "tool_call", "toolCallId": "e1", "title": "Edit button.html",
                            "kind": "edit", "status": "in_progress", "locations": [{"path": target}]})
        with open(target, "w") as handle:
            handle.write("<button>Join</button>")
        update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "e1", "status": "completed"})
        args = {"title": "Added the Join waitlist button", "files": ["button.html"]}
        update(session_id, {"sessionUpdate": "tool_call", "toolCallId": "rep1",
                            "title": "mcp__playground__playground_step_report", "kind": "other",
                            "status": "pending", "rawInput": args})
        goal = os.environ.get("FAKE_ACP_GOAL", "")
        thread = os.path.join(cwd, ".playground", "threads", f"{goal}.jsonl")
        os.makedirs(os.path.dirname(thread), exist_ok=True)
        with open(thread, "a") as handle:   # what the MCP server's playground_step_report writes
            handle.write(json.dumps({"ts": "2026-10-09T10:00:00Z", "id": "step-mcp1", "role": "agent", "kind": "step",
                                     "by": os.environ.get("PLAYGROUND_ACTOR", ""),
                                     "data": {"title": args["title"], "state": "done", "files": ["button.html"]}}) + "\n")
        update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "rep1", "status": "completed"})
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text",
                            "text": "The button is in."}})
        reply(rid, {"stopReason": "end_turn"})
        return
    if "escape" in text:
        results = {"sandbox": os.environ.get("PLAYGROUND_AGENT_SANDBOX", "")}
        outside = os.path.join(os.path.dirname(cwd), "escaped-by-agent.txt")
        try:
            with open(outside, "w") as handle:
                handle.write("x")
            results["outside_write"] = "written"
        except OSError as exc:
            results["outside_write"] = type(exc).__name__
        try:
            with open(os.path.join(cwd, "inside.txt"), "w") as handle:
                handle.write("ok")
            results["inside_write"] = "written"
        except OSError as exc:
            results["inside_write"] = type(exc).__name__
        home = os.environ.get("HOME", "")
        try:
            results["home_listing"] = sorted(os.listdir(home))
        except OSError as exc:
            results["home_listing"] = type(exc).__name__
        secret = os.environ.get("FAKE_SECRET", "")
        if secret:
            try:
                with open(secret) as handle:
                    results["secret"] = handle.read()
            except OSError as exc:
                results["secret"] = type(exc).__name__
        record({"escape": results})
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Tried."}})
        reply(rid, {"stopReason": "end_turn"})
        return
    if "crash" in text:
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Oh"}})
        time.sleep(0.2)
        log("crashing on purpose")
        os._exit(3)
    if "busy-work" in text:
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Working on it"}})
        deadline = time.time() + 20
        while session_id not in _cancelled and time.time() < deadline:
            time.sleep(0.05)
        reply(rid, {"stopReason": "cancelled" if session_id in _cancelled else "end_turn"})
        return
    if "quiet" in text:
        time.sleep(30)
        reply(rid, {"stopReason": "end_turn"})
        return
    if "cancel-me" in text:
        update(session_id, {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Run rm -rf build",
                            "kind": "execute", "status": "pending"})
        result, _ = call("session/request_permission", {
            "sessionId": session_id, "toolCall": {"toolCallId": "c1", "title": "Run rm -rf build"},
            "options": [{"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "no", "name": "Reject", "kind": "reject_once"}]}, timeout=60)
        record({"permission": result})
        deadline = time.time() + 10
        while session_id not in _cancelled and time.time() < deadline:
            time.sleep(0.05)
        update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "failed"})
        reply(rid, {"stopReason": "cancelled" if session_id in _cancelled else "end_turn"})
        return
    if "mcp-ask" in text:
        pending = os.path.join(cwd, ".playground", "pending")
        os.makedirs(pending, exist_ok=True)
        goal = os.environ.get("FAKE_ACP_GOAL", "")
        with open(os.path.join(pending, "pick-colour-ab12.json"), "w") as handle:
            json.dump({"id": "pick-colour-ab12", "kind": "decision", "goal": goal,
                       "question": "Which colour for the button?",
                       "options": [{"id": "green", "label": "Green", "consequence": "Calm"},
                                   {"id": "red", "label": "Red", "consequence": "Loud"}],
                       "recommended": "green", "default": "green", "asked_by": "fake"}, handle)
        update(session_id, {"sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "I asked you about the colour."}})
        reply(rid, {"stopReason": "end_turn"})
        return
    if "json-answer" in text:
        for piece in ("Here you go:\n```json\n", '{"name": "Tiles", "questions": []}', "\n```"):
            update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": piece}})
        reply(rid, {"stopReason": "end_turn"})
        return
    if "two-steps" in text:
        for n in (1, 2):
            update(session_id, {"sessionUpdate": "tool_call", "toolCallId": f"r{n}", "title": f"Read file {n}",
                                "kind": "read", "status": "in_progress",
                                "locations": [{"path": os.path.join(cwd, f"f{n}.txt")}]})
            update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": f"r{n}", "status": "completed"})
            update(session_id, {"sessionUpdate": "agent_message_chunk", "messageId": f"m{n}",
                                "content": {"type": "text", "text": f"Read {n}."}})
        reply(rid, {"stopReason": "end_turn"})
        return

    # The full scripted turn.
    plan(session_id, ["in_progress", "pending"])
    for piece in ("I'll create ", "the file ", "now."):
        update(session_id, {"sessionUpdate": "agent_message_chunk", "messageId": "m1",
                            "content": {"type": "text", "text": piece}})
    target = os.path.join(cwd, "hello.txt")
    update(session_id, {"sessionUpdate": "tool_call", "toolCallId": "call_1", "title": "Write hello.txt",
                        "kind": "edit", "status": "pending", "locations": [{"path": target}],
                        "rawInput": {"path": target, "content": "hi"}})
    result, _ = call("session/request_permission", {
        "sessionId": session_id,
        "toolCall": {"toolCallId": "call_1", "title": "Write hello.txt", "kind": "edit",
                     "locations": [{"path": target}]},
        "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "always", "name": "Always allow", "kind": "allow_always"},
                    {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]})
    record({"permission": result})
    if session_id in _cancelled:
        reply(rid, {"stopReason": "cancelled"})
        return
    allowed = bool(result) and result.get("outcome", {}).get("optionId") in ("allow", "always")
    if allowed:
        update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "call_1", "status": "in_progress"})
        _, error = call("fs/write_text_file", {"sessionId": session_id, "path": target, "content": "hi"})
        update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "call_1",
                            "status": "failed" if error else "completed",
                            "content": [{"type": "diff", "path": target, "oldText": None, "newText": "hi"}]})
    else:
        update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "call_1", "status": "failed"})

    update(session_id, {"sessionUpdate": "tool_call", "toolCallId": "call_2", "title": "List the folder",
                        "kind": "execute", "status": "in_progress"})
    created, error = call("terminal/create", {"sessionId": session_id, "command": "ls", "args": ["-1"],
                                              "cwd": cwd, "outputByteLimit": 4096})
    listing = ""
    if created:
        term = created["terminalId"]
        exit_status, _ = call("terminal/wait_for_exit", {"sessionId": session_id, "terminalId": term})
        output, _ = call("terminal/output", {"sessionId": session_id, "terminalId": term})
        call("terminal/release", {"sessionId": session_id, "terminalId": term})
        listing = (output or {}).get("output", "")
        record({"terminal": {"exit": exit_status, "output": listing}})
    else:
        record({"terminal_error": error})
    update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "call_2",
                        "status": "completed" if created else "failed"})

    read, error = call("fs/read_text_file", {"sessionId": session_id, "path": target})
    record({"read_back": read, "read_error": error})
    _, escape_error = call("fs/write_text_file", {"sessionId": session_id,
                                                  "path": os.path.join(cwd, "..", "escaped.txt"), "content": "x"})
    record({"escape_error": escape_error})
    _, protected_error = call("fs/write_text_file", {"sessionId": session_id,
                                                     "path": os.path.join(cwd, ".mcp.json"), "content": "{}"})
    record({"protected_error": protected_error})
    _, missing_error = call("fs/read_text_file", {"sessionId": session_id, "path": os.path.join(cwd, "nope.txt")})
    record({"missing_error": missing_error})
    update(session_id, {"sessionUpdate": "usage_update", "used": 1200, "size": 200000,
                        "cost": {"amount": 0.0123, "currency": "USD"}})
    plan(session_id, ["completed", "completed"])
    update(session_id, {"sessionUpdate": "agent_message_chunk", "messageId": "m2",
                        "content": {"type": "text", "text": "Done. hello.txt says hi."}})
    reply(rid, {"stopReason": "end_turn"})


def handle(message):
    rid = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    record({"method": method})
    if method == "initialize":
        methods = [{"id": "agent-login", "name": "Sign in", "description": "Opens a browser"}]
        if params.get("clientCapabilities", {}).get("auth", {}).get("terminal"):
            methods.append({"id": "terminal-login", "name": "Sign in from the terminal", "type": "terminal",
                            "args": ["--login"], "env": {"FAKE_LOGIN": "1"}})
        record({"clientCapabilities": params.get("clientCapabilities"), "clientInfo": params.get("clientInfo")})
        reply(rid, {"protocolVersion": 1,
                    "agentCapabilities": {"loadSession": True,
                                          "sessionCapabilities": {"list": {}, "resume": {}, "close": {}},
                                          "promptCapabilities": {"image": False, "embeddedContext": True},
                                          "auth": {"logout": {}}},
                    "agentInfo": {"name": "fake-agent", "title": "Fake Agent", "version": "9.9.9"},
                    "authMethods": methods})
    elif method == "authenticate":
        if params.get("methodId") == "agent-login" and STATE:
            open(os.path.join(STATE, "signed-in"), "w").close()
            reply(rid, {})
        else:
            reply(rid, error={"code": -32602, "message": "unknown method"})
    elif method == "session/new":
        if not signed_in():
            reply(rid, error={"code": -32000, "message": "Authentication required"})
            return
        session_id = f"fake-{os.getpid()}-{len(_sessions) + 1}"
        _sessions[session_id] = {"cwd": params.get("cwd"), "mcp": params.get("mcpServers")}
        record({"mcpServers": params.get("mcpServers")})
        reply(rid, {"sessionId": session_id,
                    "configOptions": [
                        {"id": "model", "name": "Model", "category": "model", "type": "select", "currentValue": "fast",
                         "options": [{"value": "fast", "name": "Fast"}, {"value": "deep", "name": "Deep"}]},
                        {"id": "reasoning_effort", "name": "Effort", "category": "thought_level", "type": "select",
                         "currentValue": "medium",
                         "options": [{"value": "low", "name": "Low"}, {"value": "medium", "name": "Medium"},
                                     {"value": "high", "name": "High"}]}],
                    "modes": {"currentModeId": "default",
                              "availableModes": [{"id": "default", "name": "Ask"},
                                                 {"id": "acceptEdits", "name": "Accept edits"}]}})
    elif method == "session/load":
        session_id = params["sessionId"]
        _sessions[session_id] = {"cwd": params.get("cwd")}
        update(session_id, {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "old q"}})
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "old a"}})
        reply(rid, {})
    elif method == "session/resume":
        _sessions[params["sessionId"]] = {"cwd": params.get("cwd")}
        record({"resumed": params["sessionId"]})
        reply(rid, {})
    elif method == "session/list":
        if params.get("cursor") == "page2":
            reply(rid, {"sessions": [{"sessionId": "s3", "cwd": params.get("cwd") or "/"}]})
        else:
            reply(rid, {"sessions": [{"sessionId": "s1", "cwd": "/a", "title": "One"},
                                     {"sessionId": "s2", "cwd": "/a"}], "nextCursor": "page2"})
    elif method == "session/close":
        _sessions.pop(params.get("sessionId"), None)
        reply(rid, {})
    elif method == "session/set_config_option":
        record({"config": [params.get("configId"), params.get("value")]})
        reply(rid, {})
    elif method == "session/set_mode":
        record({"mode": params.get("modeId")})
        reply(rid, {})
    elif method == "session/prompt":
        _cancelled.discard(params.get("sessionId"))
        threading.Thread(target=turn, args=(rid, params), daemon=True).start()
    elif method == "logout":
        reply(rid, {})
    elif rid is not None:
        reply(rid, error={"code": -32601, "message": f"Method not found: {method}"})


def main():
    if "--login" in sys.argv:
        if STATE:
            open(os.path.join(STATE, "signed-in"), "w").close()
        print("Signed in.")
        return 0
    log("started")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if "method" not in message and "id" in message:
            with _pending_lock:
                slot = _pending.get(message["id"])
                if slot:
                    slot[1] = message
                    slot[0].set()
            continue
        if message.get("method") == "session/cancel":
            _cancelled.add((message.get("params") or {}).get("sessionId"))
            record({"method": "session/cancel"})
            continue
        if "id" in message:
            threading.Thread(target=handle, args=(message,), daemon=True).start()
        else:
            handle(message)
    log("stdin closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
