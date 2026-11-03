# SPDX-License-Identifier: Apache-2.0
"""One question to a full agent (Codex on the person's plan), one text answer: New project's questions."""
from pathlib import Path
import sys
import unittest
from unittest import mock

from luma_playground.agents import quick
from luma_playground.agents.connections import Connection

FAKE = str(Path(__file__).resolve().parent / "fake_acp_agent.py")


class QuickAskTests(unittest.TestCase):
    def test_the_answer_comes_back_as_text(self):
        codex = Connection(id="codex", label="Codex", kind="personal-plan", provider="codex", owner="nick")
        with mock.patch.object(quick, "launch_spec", return_value=([sys.executable, FAKE], {})):
            text = quick.ask_text(codex, "json-answer please", timeout=30)
        self.assertIn('{"name": "Tiles", "questions": []}', text)


if __name__ == "__main__":
    unittest.main()
