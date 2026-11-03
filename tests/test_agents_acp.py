# SPDX-License-Identifier: Apache-2.0
"""The ACP client against a real subprocess: tests/fake_acp_agent.py."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from luma_playground.agents import acp, sandbox

FAKE = str(Path(__file__).resolve().parent / "fake_acp_agent.py")


def calls(state: str) -> list[dict]:
    path = os.path.join(state, "calls.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]


class Recorder:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.asked: list[tuple] = []
        self.lock = threading.Lock()

    def on_update(self, session_id, update):
        with self.lock:
            self.updates.append(update)

    def allow(self, session_id, tool_call, options):
        self.asked.append((session_id, tool_call, options))
        return acp.selected_outcome(acp.pick_option(options, allow=True))


class AcpClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.project = os.path.join(self.root, "proj")
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.project)
        os.makedirs(self.state)
        self.env = dict(os.environ, FAKE_ACP_STATE=self.state)
        self.rec = Recorder()
        self.handlers = acp.ProjectHandlers(self.project, on_update=self.rec.on_update, ask=self.rec.allow,
                                            policy=sandbox.SandboxPolicy(self.project, backend="none"))
        self.clients: list[acp.AcpClient] = []

    def tearDown(self) -> None:
        for client in self.clients:
            client.close()
        self.tmp.cleanup()

    def client(self, env=None, handlers=None) -> acp.AcpClient:
        c = acp.AcpClient([sys.executable, FAKE], env=env or self.env, cwd=self.project,
                          handlers=handlers or self.handlers,
                          stderr_log=os.path.join(self.project, ".playground", "local", "logs", "agent.log"),
                          name="fake")
        self.clients.append(c)
        return c

    def test_initialize_advertises_client_capabilities(self):
        c = self.client()
        info = c.start(timeout=20)
        self.assertEqual(info.protocol_version, 1)
        self.assertEqual(info.name, "Fake Agent")
        self.assertTrue(info.can_load and info.can_resume and info.can_list and info.can_close and info.can_logout)
        init = next(e for e in calls(self.state) if "clientCapabilities" in e)
        caps = init["clientCapabilities"]
        self.assertEqual(caps["fs"], {"readTextFile": True, "writeTextFile": True})
        self.assertTrue(caps["terminal"])
        self.assertEqual(caps["auth"], {"terminal": True})
        self.assertEqual(init["clientInfo"]["name"], "Playground")
        self.assertIn("terminal-login", [m["id"] for m in c.auth_methods()])

    def test_full_turn_streams_updates_and_serves_requests(self):
        c = self.client()
        c.start(timeout=20)
        sid = c.new_session(self.project, [])
        stop = c.prompt(sid, [acp.text_block("make hello")], idle_timeout=30)
        self.assertEqual(stop, "end_turn")
        # The file was written through fs/write_text_file, inside the project.
        self.assertEqual(Path(self.project, "hello.txt").read_text(), "hi")
        self.assertFalse(Path(self.root, "escaped.txt").exists())
        log = calls(self.state)
        self.assertEqual(next(e for e in log if "read_back" in e)["read_back"], {"content": "hi"})
        self.assertIsNotNone(next(e for e in log if "escape_error" in e)["escape_error"])
        self.assertIsNotNone(next(e for e in log if "protected_error" in e)["protected_error"])
        self.assertEqual(next(e for e in log if "missing_error" in e)["missing_error"]["code"], -32002)
        term = next(e for e in log if "terminal" in e)["terminal"]
        self.assertEqual(term["exit"]["exitCode"], 0)
        self.assertIn("hello.txt", term["output"])
        # Every update arrived before prompt() returned, in order.
        kinds = [u["sessionUpdate"] for u in self.rec.updates]
        self.assertEqual(kinds[0], "plan")
        self.assertEqual(kinds[-1], "agent_message_chunk")
        self.assertIn("usage_update", kinds)
        self.assertEqual(len(self.rec.asked), 1)
        # stderr went to the log
        log_text = Path(self.project, ".playground/local/logs/agent.log").read_text()
        self.assertIn("[fake-agent] started", log_text)

    def test_permission_declined_by_default(self):
        handlers = acp.ProjectHandlers(self.project, policy=sandbox.SandboxPolicy(self.project, backend="none"))
        c = self.client(handlers=handlers)
        c.start(timeout=20)
        sid = c.new_session(self.project, [])
        self.assertEqual(c.prompt(sid, [acp.text_block("make hello")], idle_timeout=30), "end_turn")
        self.assertFalse(Path(self.project, "hello.txt").exists())
        perm = next(e for e in calls(self.state) if "permission" in e)["permission"]
        self.assertEqual(perm["outcome"], {"outcome": "selected", "optionId": "reject"})

    def test_cancel_answers_open_permission_as_cancelled(self):
        gate = threading.Event()

        def never(session_id, tool_call, options):
            gate.wait(20)  # the person never answers
            return acp.selected_outcome("yes")

        handlers = acp.ProjectHandlers(self.project, ask=never,
                                       policy=sandbox.SandboxPolicy(self.project, backend="none"))
        c = self.client(handlers=handlers)
        c.start(timeout=20)
        sid = c.new_session(self.project, [])
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("stop", c.prompt(sid, [acp.text_block("cancel-me")])))
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline and not c._open_permissions:
            time.sleep(0.05)
        self.assertTrue(c._open_permissions, "permission request never arrived")
        c.cancel(sid)
        thread.join(15)
        gate.set()
        self.assertEqual(result.get("stop"), "cancelled")
        perm = next(e for e in calls(self.state) if "permission" in e)["permission"]
        self.assertEqual(perm, {"outcome": {"outcome": "cancelled"}})

    def test_sessions_list_load_resume_close(self):
        c = self.client()
        c.start(timeout=20)
        listed = c.list_sessions(cwd=self.project)
        self.assertEqual([s["sessionId"] for s in listed], ["s1", "s2", "s3"])
        self.rec.updates.clear()
        c.load_session("s1", self.project, [])
        self.assertEqual([u["sessionUpdate"] for u in self.rec.updates], ["user_message_chunk", "agent_message_chunk"])
        self.assertEqual(c.reopen_session("s2", self.project, []), "resumed")
        c.close_session("s2")
        c.set_mode("s1", "acceptEdits")
        self.assertIn({"mode": "acceptEdits"}, calls(self.state))

    def test_auth_required_then_terminal_login(self):
        env = dict(self.env, FAKE_ACP_AUTH="1")
        c = self.client(env=env)
        c.start(timeout=20)
        with self.assertRaises(acp.AuthRequired) as caught:
            c.new_session(self.project, [])
        self.assertIn("sign in", str(caught.exception))
        argv, login_env = c.login_command("terminal-login")
        self.assertEqual(argv[-1], "--login")
        self.assertEqual(login_env["FAKE_LOGIN"], "1")
        with self.assertRaises(ValueError):
            c.authenticate("terminal-login")
        output = []
        login = acp.LoginTerminal(argv, login_env, self.project, prefer="pty", on_output=output.append)
        self.assertEqual(login.start(), "pty")
        self.assertEqual(login.wait(20), 0)
        self.assertIn("Signed in", "".join(output))
        c.close()
        again = self.client(env=env)
        again.start(timeout=20)
        self.assertTrue(again.new_session(self.project, []))

    def test_agent_auth_method(self):
        env = dict(self.env, FAKE_ACP_AUTH="1")
        c = self.client(env=env)
        c.start(timeout=20)
        c.authenticate("agent-login")
        self.assertTrue(c.new_session(self.project, []))

    def test_crash_mid_turn_is_explained(self):
        c = self.client()
        c.start(timeout=20)
        sid = c.new_session(self.project, [])
        with self.assertRaises(acp.AcpClosed) as caught:
            c.prompt(sid, [acp.text_block("crash")], idle_timeout=30)
        self.assertIn("stopped unexpectedly", str(caught.exception))
        self.assertIn("crashing on purpose", caught.exception.detail)
        self.assertFalse(c.alive)
        with self.assertRaises(acp.AcpClosed):
            c.new_session(self.project, [])

    def test_idle_timeout_cancels(self):
        c = self.client()
        c.start(timeout=20)
        sid = c.new_session(self.project, [])
        started = time.monotonic()
        with self.assertRaises(acp.AcpTimeout):
            c.prompt(sid, [acp.text_block("quiet")], idle_timeout=1.5)
        self.assertLess(time.monotonic() - started, 10)
        deadline = time.time() + 5
        while time.time() < deadline and {"method": "session/cancel"} not in calls(self.state):
            time.sleep(0.05)
        self.assertIn({"method": "session/cancel"}, calls(self.state))

    def test_missing_program_is_plain(self):
        c = acp.AcpClient(["definitely-not-a-real-agent-xyz"], cwd=self.project, handlers=self.handlers)
        with self.assertRaises(acp.AcpError) as caught:
            c.start(timeout=5)
        self.assertIn("couldn't find", str(caught.exception))

    def test_close_is_prompt_and_idempotent(self):
        c = self.client()
        c.start(timeout=20)
        pid = c.pid
        started = time.monotonic()
        c.close()
        c.close()
        self.assertLess(time.monotonic() - started, 8)
        self.assertFalse(c.alive)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
