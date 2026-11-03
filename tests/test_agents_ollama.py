# SPDX-License-Identifier: Apache-2.0
"""Ollama end to end against a fake Ollama server: finding it, listing models, the
64k context on every request, tool calls through Ollama's own API, and plain
words with a next step when there's no model. No real models are downloaded."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_openai_server import FakeOpenAI  # noqa: E402
from test_agents_ari import AriBase  # noqa: E402

from luma_playground.agents import acp, ollama  # noqa: E402
from luma_playground.agents.ari import AriError, Endpoint, endpoint_for  # noqa: E402
from luma_playground.agents.connections import Connection, detect, suggest  # noqa: E402

DEAD = "http://127.0.0.1:9"   # nothing listens on the discard port


class DetectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeOpenAI([])
        self.host = self.fake.start().removesuffix("/v1")
        self.addCleanup(self.fake.stop)

    def test_running_with_models(self):
        state = ollama.status(self.host, which=lambda _: None)
        self.assertTrue(state.running and state.installed and state.ready)
        self.assertEqual(state.version, "0.12.3")
        self.assertEqual([m.name for m in state.models], ["qwen3:8b"])
        self.assertEqual(state.models[0].size_text, "5.2 GB")
        self.assertEqual(state.models[0].parameter_size, "8.2B")
        self.assertEqual(state.note, "Running, with 1 model.")
        self.assertEqual(state.next_step, "")

    def test_running_with_no_models_says_what_to_do(self):
        self.fake.models = []
        state = ollama.status(self.host, which=lambda _: "/usr/bin/ollama")
        self.assertFalse(state.ready)
        self.assertEqual(state.note, "Ollama is running, but it has no models yet.")
        self.assertIn("ollama pull qwen3", state.next_step)
        self.assertIn("gigabytes", state.next_step)

    def test_not_running_and_not_installed(self):
        state = ollama.status(DEAD, which=lambda _: "/usr/local/bin/ollama")
        self.assertEqual((state.installed, state.running), (True, False))
        self.assertIn("isn't running", state.note)
        self.assertIn("ollama serve", state.next_step)
        state = ollama.status(DEAD, which=lambda _: None)
        self.assertFalse(state.installed)
        self.assertIn("ollama.com", state.next_step)

    def test_show_reads_context_and_tools(self):
        model = ollama.show("qwen3:8b", self.host)
        self.assertEqual(model.context_length, 40960)
        self.assertTrue(model.tools)

    def test_detect_and_suggest(self):
        found = {d.provider: d for d in detect(env={"PATH": "/nonexistent"}, ollama_host=self.host)}
        item = found["ollama"]
        self.assertEqual(item.models, ["qwen3:8b"])
        self.assertTrue(item.signed_in)
        offered = suggest(list(found.values()), "noor")
        ari = next(c for c in offered if c.id == "ari-local")
        self.assertEqual((ari.endpoint, ari.model), ("ollama", "qwen3:8b"))
        self.assertGreaterEqual(endpoint_for(ari).context, ollama.MIN_CONTEXT)
        self.fake.models = []
        item = {d.provider: d for d in detect(env={"PATH": "/nonexistent"}, ollama_host=self.host)}["ollama"]
        self.assertIn("no models yet", item.note)
        self.assertIn("ollama pull", item.note)
        self.assertFalse([c for c in suggest([item], "noor") if c.id == "ari-local"])

    def test_host_and_context_rules(self):
        self.assertEqual(ollama.host_from("http://localhost:11434/v1"), "http://localhost:11434")
        self.assertEqual(ollama.host_from("http://box:11434/api/"), "http://box:11434")
        self.assertEqual(ollama.host_from(""), ollama.DEFAULT_HOST)
        self.assertEqual(ollama.context_for(4096), 65536)
        self.assertEqual(ollama.context_for(131072), 131072)
        self.assertEqual(ollama.context_for("junk"), 65536)


class AriOnOllamaTests(AriBase):
    def test_a_turn_uses_ollamas_own_api_with_a_big_context(self):
        url = self.server([
            {"text": "Writing it.", "tool_calls": [{"name": "write_file", "arguments": {"path": "hello.txt",
                                                                                        "content": "hi"}}]},
            {"text": "Done."}])
        ari = self.agent(Endpoint("ollama", url, "qwen3:8b", context=4096))
        stop = ari.prompt(ari.new_session(self.root), [acp.text_block("make hello.txt")])
        self.assertEqual(stop, "end_turn")
        self.assertEqual(Path(self.root, "hello.txt").read_text(), "hi")
        first, second = self.fake.requests
        self.assertEqual(first["options"]["num_ctx"], 65536)          # 4096 asked; 64k is the floor
        self.assertTrue(first["stream"])
        self.assertEqual(first["model"], "qwen3:8b")
        self.assertIn("write_file", json.dumps(first["tools"]))
        # The tool call and its result go back in Ollama's own shapes.
        call = next(m for m in second["messages"] if m.get("tool_calls"))
        self.assertEqual(call["tool_calls"][0]["function"]["arguments"], {"path": "hello.txt", "content": "hi"})
        result = next(m for m in second["messages"] if m["role"] == "tool")
        self.assertEqual(result["tool_name"], "write_file")
        texts = "".join(u["content"]["text"] for u in self.updates if u.get("sessionUpdate") == "agent_message_chunk")
        self.assertEqual(texts, "Writing it.Done.")
        usage = [u for u in self.updates if u.get("sessionUpdate") == "usage_update"]
        self.assertEqual(usage[0]["used"], 120)

    def test_no_model_chosen_takes_the_first_one(self):
        url = self.server([{"text": "Hi."}])
        ari = self.agent(Endpoint("ollama", url, ""))
        ari.prompt(ari.new_session(self.root), [acp.text_block("hi")])
        self.assertEqual(self.fake.requests[0]["model"], "qwen3:8b")

    def test_no_model_pulled_is_plain_with_a_next_step(self):
        url = self.server([])
        self.fake.models = []
        ari = self.agent(Endpoint("ollama", url, ""))
        with self.assertRaises(AriError) as caught:
            ari.prompt(ari.new_session(self.root), [acp.text_block("hi")])
        self.assertIn("no models yet", str(caught.exception))
        self.assertIn("ollama pull qwen3", str(caught.exception))
        self.assertEqual(caught.exception.reason, "no-model")

    def test_a_missing_model_or_one_without_tools(self):
        url = self.server([{"status": 404, "message": "model 'llama9' not found"},
                           {"status": 400, "message": "registry.ollama.ai/library/gemma:2b does not support tools"},
                           {"status": 500, "message": "model requires more system memory (12 GiB) than is available"}])
        for words in ("ollama pull llama9", "can't use tools", "more memory"):
            with self.subTest(words=words):
                ari = self.agent(Endpoint("ollama", url, "llama9"))
                with self.assertRaises(AriError) as caught:
                    ari.prompt(ari.new_session(self.root), [acp.text_block("hi")])
                self.assertIn(words, str(caught.exception))

    def test_an_error_mid_stream(self):
        url = self.server([{"text": "Start", "error": "model 'x' not found"}])
        ari = self.agent(Endpoint("ollama", url, "x"))
        with self.assertRaises(AriError) as caught:
            ari.prompt(ari.new_session(self.root), [acp.text_block("hi")])
        self.assertIn("isn't downloaded yet", str(caught.exception))

    def test_runner_card_for_no_model(self):
        from luma_playground.agents import sandbox
        from luma_playground.agents.runner import GoalRunner
        url = self.server([])
        self.fake.models = []
        conn = Connection(id="ari-local", label="Ari", kind="ari", provider="ari", owner="noor", endpoint="ollama",
                          base_url=url)
        runner = GoalRunner(self.root, self.goal.id, conn, self.bus,
                            sandbox_policy=sandbox.SandboxPolicy(self.root, backend="none"))
        self.addCleanup(runner.close)
        runner.send("hi")
        self.assertTrue(runner.wait_idle(30))
        card = self.store.read_thread(self.goal.id)[-1]
        self.assertEqual(card.role, "system")
        self.assertIn("ollama pull qwen3", card.text)
        self.assertEqual(card.data["action"], "retry")


if __name__ == "__main__":
    unittest.main()
