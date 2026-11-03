# SPDX-License-Identifier: Apache-2.0
"""Ari's loop against a local fake OpenAI-compatible server. No network."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_openai_server import FakeOpenAI  # noqa: E402

from luma_playground.agents import acp, sandbox  # noqa: E402
from luma_playground.agents.ari import AriAgent, AriError, Endpoint, MIN_CONTEXT, endpoint_for  # noqa: E402
from luma_playground.agents.connections import Connection  # noqa: E402
from luma_playground.core.events import EventBus  # noqa: E402
from luma_playground.core.model import Member  # noqa: E402
from luma_playground.core.project import create_project  # noqa: E402
from luma_playground.core.store import ProjectStore  # noqa: E402


class AriBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_CONFIG_DIR": str(base / "config"),
                                               "PLAYGROUND_DATA_DIR": str(base / "data"),
                                               "PLAYGROUND_HOME": str(base / "projects")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bus = EventBus()
        project = create_project("Kiln", "A pottery class sign-up", root=base / "projects", git=False, bus=self.bus,
                                 owner=Member(id="nick", name="Nick", role="owner"))
        self.root = str(project.path)
        self.store = ProjectStore(self.root, bus=self.bus)
        self.goal = self.store.create_goal("Say hi", outcome="hello.txt says hi", owner="nick")
        self.updates: list[dict] = []
        self.asked: list[dict] = []
        self.servers: list[FakeOpenAI] = []

    def tearDown(self) -> None:
        for server in self.servers:
            server.stop()
        self.tmp.cleanup()

    def server(self, script) -> str:
        fake = FakeOpenAI(script)
        self.servers.append(fake)
        self.fake = fake
        return fake.start()

    def agent(self, endpoint: Endpoint, allow: bool = True) -> AriAgent:
        def ask(session_id, tool_call, options):
            self.asked.append(tool_call)
            return acp.selected_outcome(acp.pick_option(options, allow=allow))

        handlers = acp.ProjectHandlers(self.root, on_update=lambda s, u: self.updates.append(u), ask=ask,
                                       policy=sandbox.SandboxPolicy(self.root, backend="none"))
        ari = AriAgent(self.root, handlers, endpoint=endpoint, bus=self.bus, goal_id=self.goal.id, actor="ari")
        ari.start()
        self.addCleanup(ari.close)
        return ari


class AriLoopTests(AriBase):
    def test_full_turn_with_files_commands_plan_and_playground_tools(self):
        url = self.server([
            {"text": "I'll write it.", "tool_calls": [
                {"name": "update_plan", "arguments": {"entries": [{"content": "Write hello.txt", "status": "in_progress"}]}},
                {"name": "write_file", "arguments": {"path": "hello.txt", "content": "hi"}}]},
            {"tool_calls": [{"name": "run_command", "arguments": {"command": "cat hello.txt"}},
                            {"name": "playground_step_report", "arguments": {"title": "Wrote hello.txt",
                                                                              "files": ["hello.txt"]}}]},
            {"text": "Done. hello.txt says hi."},
        ])
        ari = self.agent(Endpoint("ollama", url, "qwen3.8:27b"))
        sid = ari.new_session(self.root)
        stop = ari.prompt(sid, [acp.text_block("Make hello.txt say hi")])
        self.assertEqual(stop, "end_turn")
        self.assertEqual(Path(self.root, "hello.txt").read_text(), "hi")
        kinds = [u["sessionUpdate"] for u in self.updates]
        self.assertIn("plan", kinds)
        self.assertIn("usage_update", kinds)
        text = "".join(u["content"]["text"] for u in self.updates if u["sessionUpdate"] == "agent_message_chunk")
        self.assertEqual(text, "I'll write it.Done. hello.txt says hi.")
        write = next(u for u in self.updates if u.get("name") == "write_file")
        self.assertEqual(write["kind"], "edit")
        self.assertEqual(write["locations"], [{"path": os.path.join(self.root, "hello.txt")}])
        done = [u for u in self.updates if u["sessionUpdate"] == "tool_call_update"]
        self.assertTrue(all(u["status"] == "completed" for u in done), done)
        # The command ran and its output went back to the model.
        third = self.fake.requests[2]["messages"]
        tool_text = "\n".join(m.get("content") or "" for m in third if m["role"] == "tool")
        self.assertIn("Exit code 0", tool_text)
        self.assertIn("Recorded the step", tool_text)
        # Playground's own tool wrote a step card, in-process, signed by Ari.
        steps = [e for e in self.store.read_thread(self.goal.id) if e.kind == "step"]
        self.assertEqual(steps[0].data["title"], "Wrote hello.txt")
        self.assertEqual(steps[0].by, "ari")
        # The command asked first.
        self.assertEqual(self.asked[0]["kind"], "execute")
        # Ollama gets a big enough context window; the system prompt carries AGENTS.md.
        first = self.fake.requests[0]
        self.assertGreaterEqual(first["options"]["num_ctx"], MIN_CONTEXT)
        self.assertTrue(first["stream"])
        system = first["messages"][0]["content"]
        self.assertIn("You are Ari", system)
        self.assertIn("a Playground project", system)
        self.assertIn(self.goal.id, system)
        names = {t["function"]["name"] for t in first["tools"]}
        self.assertTrue({"read_file", "write_file", "run_command", "playground_decision_ask"} <= names)

    def test_declined_command_is_reported_to_the_model(self):
        url = self.server([{"tool_calls": [{"name": "run_command", "arguments": {"command": "rm -rf ."}}]},
                           {"text": "Okay, I won't."}])
        ari = self.agent(Endpoint("ollama", url, "m"), allow=False)
        sid = ari.new_session(self.root)
        self.assertEqual(ari.prompt(sid, [acp.text_block("clean up")]), "end_turn")
        tool = [m for m in self.fake.requests[1]["messages"] if m["role"] == "tool"][0]
        self.assertIn("didn't allow", tool["content"])
        self.assertTrue(Path(self.root, "AGENTS.md").exists())

    def test_escape_attempts_fail_inside_the_loop(self):
        url = self.server([{"tool_calls": [{"name": "write_file", "arguments": {"path": "../evil.txt", "content": "x"}},
                                           {"name": "read_file", "arguments": {"path": "/etc/passwd"}},
                                           {"name": "write_file", "arguments": {"path": ".mcp.json", "content": "{}"}}]},
                           {"text": "Couldn't."}])
        ari = self.agent(Endpoint("ollama", url, "m"))
        sid = ari.new_session(self.root)
        ari.prompt(sid, [acp.text_block("try")])
        self.assertFalse(Path(self.root).parent.joinpath("evil.txt").exists())
        tools = [m["content"] for m in self.fake.requests[1]["messages"] if m["role"] == "tool"]
        self.assertTrue(all(t.startswith("That didn't work") for t in tools), tools)

    def test_decision_ask_waits_for_the_person(self):
        url = self.server([
            {"tool_calls": [{"name": "playground_decision_ask", "arguments": {
                "question": "Which colour for the button?", "recommended": "green",
                "options": [{"id": "green", "label": "Green"}, {"id": "red", "label": "Red"}]}}]},
            {"text": "Green it is."}])
        ari = self.agent(Endpoint("ollama", url, "m"))
        sid = ari.new_session(self.root)
        seen = []
        asked = threading.Event()

        def on_pending(**kw):
            seen.append(kw["decision"])
            asked.set()

        self.bus.subscribe("decision.pending", on_pending)

        def answer():
            if asked.wait(60):
                self.store.answer_decision(seen[0]["id"], "green", by="nick")

        threading.Thread(target=answer, daemon=True).start()
        self.assertEqual(ari.prompt(sid, [acp.text_block("pick")]), "end_turn")
        tool = [m for m in self.fake.requests[1]["messages"] if m["role"] == "tool"][0]
        self.assertIn("decided: Green", tool["content"])
        cards = [e for e in self.store.read_thread(self.goal.id) if e.kind == "decision"]
        self.assertEqual(cards[0].data["answered"]["option"], "green")

    def test_cancel_stops_a_stalled_model_quickly(self):
        url = self.server([{"stall": 30}])
        ari = self.agent(Endpoint("ollama", url, "m"))
        sid = ari.new_session(self.root)
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("stop", ari.prompt(sid, [acp.text_block("x")])))
        thread.start()
        # Cancel only once the model has the request and has gone quiet, however slow the machine is.
        self.assertTrue(self.fake.stalling.wait(30), "the model never got the request")
        started = time.monotonic()
        ari.cancel(sid)
        thread.join(10)
        self.assertFalse(thread.is_alive(), "cancel didn't stop the turn")
        self.assertEqual(result.get("stop"), "cancelled")
        self.assertLess(time.monotonic() - started, 5)

    def test_cancel_before_the_stream_is_seen_still_stops(self):
        # cancel() can land between the turn's own check and the moment it publishes its stream.
        url = self.server([{"stall": 30}])
        ari = self.agent(Endpoint("ollama", url, "m"))
        sid = ari.new_session(self.root)
        real = ari._messages

        def messages_then_cancel(history):
            built = real(history)
            ari.cancel(sid)          # the stream for this step doesn't exist yet
            return built

        ari._messages = messages_then_cancel
        started = time.monotonic()
        self.assertEqual(ari.prompt(sid, [acp.text_block("x")]), "cancelled")
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(self.fake.requests, "a cancelled turn shouldn't reach the model")

    def test_session_is_kept_and_resumed(self):
        url = self.server([{"text": "First."}, {"text": "Second."}])
        ari = self.agent(Endpoint("ollama", url, "m"))
        sid = ari.new_session(self.root)
        ari.prompt(sid, [acp.text_block("one")])
        ari.close()
        again = self.agent(Endpoint("ollama", url, "m"))
        self.assertEqual(again.reopen_session(sid, self.root), "resumed")
        again.prompt(sid, [acp.text_block("two")])
        roles = [m["role"] for m in self.fake.requests[1]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        with self.assertRaises(acp.Unsupported):
            again.reopen_session("ari-doesnotexist", self.root)


class AriEndpointTests(AriBase):
    def test_api_key_goes_in_the_header_and_ollama_options_do_not(self):
        url = self.server([{"text": "hi"}])
        ari = self.agent(Endpoint("openai-compatible", url, "gpt-x", api_key="sk-test"))
        ari.prompt(ari.new_session(self.root), [acp.text_block("hi")])
        self.assertEqual(self.fake.headers[0]["authorization"], "Bearer sk-test")
        self.assertNotIn("options", self.fake.requests[0])

    def test_errors_are_plain(self):
        cases = [(401, Endpoint("openai-compatible", "", "gpt-x", api_key="bad"), "turned down the API key"),
                 (404, Endpoint("ollama", "", "qwen-missing"), "isn't downloaded yet"),
                 (429, Endpoint("openai-compatible", "", "gpt-x"), "limit is reached"),
                 (500, Endpoint("openai-compatible", "", "gpt-x"), "had a problem")]
        for status, endpoint, words in cases:
            with self.subTest(status=status):
                endpoint.base_url = self.server([{"status": status, "message": "nope"}])
                ari = self.agent(endpoint)
                with self.assertRaises(AriError) as caught:
                    ari.prompt(ari.new_session(self.root), [acp.text_block("hi")])
                self.assertIn(words, str(caught.exception))

    def test_ollama_not_running(self):
        ari = self.agent(Endpoint("ollama", "http://127.0.0.1:9/v1", "m"))
        with self.assertRaises(AriError) as caught:
            ari.prompt(ari.new_session(self.root), [acp.text_block("hi")])
        self.assertIn("can't reach Ollama", str(caught.exception))

    def test_placeholders(self):
        ari = self.agent(Endpoint("chatgpt", "", "gpt-x"))
        with self.assertRaises(AriError) as caught:
            ari.prompt(ari.new_session(self.root), [acp.text_block("hi")])
        self.assertIn("coming soon", str(caught.exception))

    def test_endpoint_for_connection_takes_keys_from_the_keyring_only(self):
        local = Connection(id="ari-local", label="Ari", kind="ari", provider="ari", endpoint="ollama", model="qwen")
        ep = endpoint_for(local)
        self.assertEqual(ep.base_url, "http://localhost:11434/v1")
        self.assertGreaterEqual(ep.context, MIN_CONTEXT)
        api = Connection(id="ari-key", label="Ari", kind="ari", provider="ari", endpoint="openai-compatible",
                         base_url="https://api.example.invalid/v1", model="gpt-x", key_ref="ari-key")
        ep = endpoint_for(api, key_lookup=lambda c: "sk-keyring")
        self.assertEqual(ep.api_key, "sk-keyring")
        self.assertNotIn("sk-keyring", repr(ep))


if __name__ == "__main__":
    unittest.main()
