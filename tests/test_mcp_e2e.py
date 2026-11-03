# SPDX-License-Identifier: Apache-2.0
"""The MCP server as an agent runs it: bin/playground-mcp in its own process, over stdio."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from luma_playground.core import events, project
from luma_playground.core.model import GoalStatus, Member
from luma_playground.core.store import ProjectStore

LAUNCHER = Path(__file__).resolve().parents[1] / "bin" / "playground-mcp"
MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
          "io.modelcontextprotocol/clientInfo": {"name": "modern-agent", "version": "1"},
          "io.modelcontextprotocol/clientCapabilities": {}}


class McpProcess:
    """A playground-mcp child process and a reader thread collecting what it says."""

    def __init__(self, project_dir: Path, **env):
        environment = {**os.environ, "PLAYGROUND_ACTOR": "ari", **env}
        # Run it the way an agent does: by the launcher's path (with this Python, so the VM and the Mac agree),
        # from somewhere else entirely, so the source path has to come from the launcher.
        self.process = subprocess.Popen([sys.executable, str(LAUNCHER), "--project", str(project_dir)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        cwd=tempfile.gettempdir(), env=environment)
        self.messages: queue.Queue = queue.Queue()
        self.notifications: list[dict] = []
        self.ids = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            self.messages.put(json.loads(line))
        self.messages.put(None)

    def send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 20) -> dict:
        self.ids += 1
        request_id = self.ids
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while True:
            message = self.messages.get(timeout=max(0.1, deadline - time.monotonic()))
            if message is None:
                raise AssertionError("the server exited: " + self.process.stderr.read().decode())
            if "method" in message:
                self.notifications.append(message)
            elif message.get("id") == request_id:
                return message

    def call(self, name: str, arguments: dict, meta: dict | None = None, timeout: float = 20) -> dict:
        params = {"name": name, "arguments": arguments}
        if meta:
            params["_meta"] = meta
        response = self.request("tools/call", params, timeout)
        if "error" in response:
            raise AssertionError(response["error"])
        return response["result"]

    def close(self) -> int:
        self.process.stdin.close()
        try:
            return self.process.wait(timeout=10)
        finally:
            self.process.stdout.close()
            self.process.stderr.close()


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        made = project.create_project("Kiln", root=self.tmp.name, owner=Member("nick", "Nick", "owner"), git=False,
                                      bus=events.EventBus())
        self.root = made.path
        self.store = ProjectStore(self.root, bus=events.EventBus())

    def tearDown(self):
        self.tmp.cleanup()

    def test_mcp_json_launches_the_server(self):
        config = json.loads((self.root / ".mcp.json").read_text())["mcpServers"]["playground"]
        self.assertEqual(Path(config["command"]).resolve(), LAUNCHER.resolve())

    def test_legacy_session(self):
        server = McpProcess(self.root, PLAYGROUND_MCP_KEEPALIVE="0.3")
        try:
            init = server.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                 "clientInfo": {"name": "claude-code", "version": "2"}})
            self.assertEqual(init["result"]["protocolVersion"], "2025-11-25")
            server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

            tools = server.request("tools/list")["result"]["tools"]
            self.assertIn("playground_decision_ask", [t["name"] for t in tools])

            created = server.call("playground_goal_create", {
                "title": "Add a waitlist", "size": "medium",
                "stories": [{"title": "Someone can join the waitlist", "checks": ["The button says Join waitlist"]}]})
            self.assertFalse(created["isError"])
            goal_id = created["structuredContent"]["id"]
            self.assertEqual(self.store.read_goal(goal_id).title, "Add a waitlist")

            logged = server.call("playground_activity_log", {"summary": "Read how classes are shown", "goal_id": goal_id})
            self.assertFalse(logged["isError"])
            self.assertEqual(self.store.read_activity()[-1].summary, "Read how classes are shown")

            # The person answers from another thread (the app), a moment after the question appears.
            answered = threading.Event()

            def person():
                for _ in range(400):
                    pending = self.store.list_pending("decision")
                    if pending:
                        time.sleep(0.8)  # long enough for a keepalive or two
                        self.store.answer_decision(pending[0].id, "both", by="nick")
                        answered.set()
                        return
                    time.sleep(0.025)

            threading.Thread(target=person, daemon=True).start()
            decision = server.call("playground_decision_ask", {
                "goal_id": goal_id, "question": "How should someone hear that a spot opened?",
                "options": [{"id": "text", "label": "A text message", "consequence": "Quick, about 1 cent"},
                            {"id": "both", "label": "A text, then an email if the text fails", "consequence": "Most reliable"}],
                "recommended": "both"}, meta={"progressToken": "wait-1"}, timeout=30)
            self.assertTrue(answered.is_set())
            self.assertFalse(decision["isError"])
            self.assertEqual(decision["structuredContent"]["option"], "both")
            self.assertIn("Nick decided", decision["content"][0]["text"])
            progress = [n for n in server.notifications if n["method"] == "notifications/progress"]
            self.assertTrue(progress, "keepalives while waiting")
            self.assertEqual(progress[0]["params"]["progressToken"], "wait-1")

            self.assertEqual(self.store.list_pending(), [])
            self.assertEqual(self.store.read_goal(goal_id).status, GoalStatus.WORKING)
            self.assertEqual(self.store.read_decisions()[-1].chosen, "A text, then an email if the text fails")
            tools_used = [e.data.get("tool") for e in self.store.read_activity() if e.actor == "ari"]
            for name in ("playground_goal_create", "playground_activity_log", "playground_decision_ask"):
                self.assertIn(name, tools_used)
            self.assertEqual(server.request("ping")["result"], {})
        finally:
            self.assertEqual(server.close(), 0)

    def test_modern_discover(self):
        server = McpProcess(self.root)
        try:
            discovered = server.request("server/discover", {"_meta": MODERN})["result"]
            self.assertEqual(discovered["resultType"], "complete")
            self.assertIn("2026-07-28", discovered["supportedVersions"])
            self.assertIn("tools", discovered["capabilities"])
            self.assertEqual(discovered["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "playground")

            # No initialize: every request carries its version.
            listed = server.request("tools/list", {"_meta": MODERN})["result"]
            self.assertEqual(listed["resultType"], "complete")
            result = server.call("playground_goal_create", {"title": "Gift a class"}, meta=MODERN)
            self.assertEqual(result["resultType"], "complete")
            self.assertFalse(result["isError"])
            self.assertEqual(self.store.read_activity()[-1].data.get("client"), "modern-agent")

            wrong = server.request("tools/list", {"_meta": {**MODERN, "io.modelcontextprotocol/protocolVersion": "2030-01-01"}})
            self.assertEqual(wrong["error"]["code"], -32022)
            self.assertIn("2026-07-28", wrong["error"]["data"]["supported"])
        finally:
            self.assertEqual(server.close(), 0)

    def test_refuses_a_folder_that_is_not_a_project(self):
        result = subprocess.run([sys.executable, str(LAUNCHER), "--project", self.tmp.name], capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertIn("isn't a Playground project", result.stderr)


if __name__ == "__main__":
    unittest.main()
