# SPDX-License-Identifier: Apache-2.0
"""Get Playground ready: shown while no agent is connected, until Not now; a first local model sized to the
computer, downloaded through Ollama's own API with its progress."""
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from luma_playground.agents import ollama


class StarterModelTests(unittest.TestCase):
    def test_the_first_model_fits_the_memory(self):
        self.assertEqual(ollama.starter_model(64)[0], "qwen3:32b")
        self.assertEqual(ollama.starter_model(16)[0], "qwen3:8b")
        self.assertEqual(ollama.starter_model(8)[0], "qwen3:4b")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()


class PullTests(unittest.TestCase):
    def opener(self, lines):
        def open_(request, timeout=None):
            self.request = request
            return FakeResponse(b"".join(json.dumps(line).encode() + b"\n" for line in lines))
        return open_

    def test_progress_comes_through_and_success_ends_it(self):
        seen = []
        ollama.pull("qwen3:8b", on_progress=lambda words, part: seen.append((words, part)), opener=self.opener([
            {"status": "pulling manifest"}, {"status": "pulling abc", "total": 100, "completed": 50},
            {"status": "success"}]))
        self.assertEqual(json.loads(self.request.data), {"model": "qwen3:8b"})
        self.assertIn(("Downloading", 0.5), seen)

    def test_an_error_is_said_plainly(self):
        with self.assertRaisesRegex(ollama.OllamaError, "couldn't download qwen3:8b: no space"):
            ollama.pull("qwen3:8b", opener=self.opener([{"error": "no space"}]))


class NeededTests(unittest.TestCase):
    def setUp(self):
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            from luma_playground.app.sheets import setup
        except (ImportError, ValueError, RuntimeError) as error:
            self.skipTest(f"GTK isn't here: {error}")
        self.setup = setup
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"PLAYGROUND_CONFIG_DIR": self.tmp.name, "PLAYGROUND_DATA_DIR": self.tmp.name,
                                           "PLAYGROUND_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_shown_with_no_agent_until_not_now(self):
        with mock.patch("luma_playground.agents.connections.ConnectionStore.list", return_value=[]):
            self.assertTrue(self.setup.needed())
            self.setup.dismiss()
            self.assertFalse(self.setup.needed())

    def test_not_shown_once_an_agent_is_connected(self):
        with mock.patch("luma_playground.agents.connections.ConnectionStore.list", return_value=[object()]):
            self.assertFalse(self.setup.needed())


if __name__ == "__main__":
    unittest.main()
