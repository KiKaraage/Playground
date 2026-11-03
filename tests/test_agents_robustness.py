# SPDX-License-Identifier: Apache-2.0
"""When things go wrong mid-turn: the agent crashes, the internet goes, the sign-in
expires. Each gets a plain card with a way on, and Try again picks the same
conversation back up when the agent can."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_agents_runner import RunnerBase  # noqa: E402

from luma_playground.agents import acp  # noqa: E402
from luma_playground.core.model import GoalStatus  # noqa: E402


class CrashTests(RunnerBase):
    def test_crash_mid_turn_offers_retry_and_resumes_the_same_session(self):
        runner = self.runner()
        runner.send("crash-once please")
        self.assertTrue(runner.wait_idle(30))
        card = self.thread()[-1]
        self.assertEqual(card.role, "system")
        self.assertIn("stopped partway through", card.text)
        self.assertIn("picks up where it left off", card.text)
        self.assertEqual(card.data["action"], "retry")
        self.assertEqual(card.data["reason"], "crashed")
        self.assertTrue(card.data["resumable"])
        self.assertIn("crashing on purpose", card.data["detail"])
        step = [c for c in self.thread() if c.kind == "step"][-1]
        self.assertTrue(step.data.get("stopped"))
        self.assertEqual(self.goal_status(), GoalStatus.NEEDS_YOU)
        self.assertEqual(self.states[-1]["state"], "error")
        self.assertTrue(self.states[-1]["can_retry"])
        self.assertTrue(runner.can_retry)
        first_session = next(e["prompt_session"] for e in self.calls() if "prompt_session" in e)

        self.assertTrue(runner.retry())
        self.assertTrue(runner.wait_idle(30))
        self.assertIn({"resumed": first_session}, self.calls())          # the same conversation, reopened
        prompts = [e["prompt"] for e in self.calls() if "prompt" in e]
        self.assertIn("cut off partway through", prompts[-1])
        cards = self.thread()
        self.assertEqual(cards[-1].text, "Picked up where I left off.")
        old = next(c for c in cards if c.id == card.id)
        self.assertTrue(old.data["resolved"])                            # its Try again button goes away
        self.assertNotIn("action", old.data)
        self.assertEqual(self.goal_status(), GoalStatus.READY)
        self.assertFalse(runner.retry())                                 # nothing left to try again

    def test_crash_before_any_session_says_try_again(self):
        runner = self.runner()
        runner._load_session = lambda: None   # nothing saved: the agent can't reopen anything
        runner.send("crash")
        self.assertTrue(runner.wait_idle(30))
        card = self.thread()[-1]
        self.assertIn("Try again to start it once more", card.text)


class OfflineTests(RunnerBase):
    def test_lost_internet_is_a_clear_state(self):
        runner = self.runner()
        runner.send("offline")
        self.assertTrue(runner.wait_idle(30))
        card = self.thread()[-1]
        self.assertIn("can't reach the internet", card.text)
        self.assertEqual(card.data["reason"], "offline")
        self.assertEqual(card.data["action"], "retry")
        self.assertEqual(self.states[-1]["state"], "offline")
        self.assertEqual(self.store.read_goal(self.goal.id).status_note, "offline")
        self.assertTrue(runner.can_retry)

    def test_offline_words_are_recognised(self):
        self.assertTrue(acp.looks_offline("API Error: Connection error."))
        self.assertTrue(acp.looks_offline("", "stream disconnected before completion: error sending request"))
        self.assertFalse(acp.looks_offline("The model refused."))


class ExpiredSignInTests(RunnerBase):
    def test_expired_sign_in_then_terminal_login_then_try_again_resumes(self):
        self.agent_env["FAKE_ACP_AUTH_EXPIRED"] = "1"
        runner = self.runner()
        runner.send("make hello")
        self.assertTrue(runner.wait_idle(30))
        cards = self.thread()
        card = cards[-1]
        self.assertEqual(card.data["action"], "sign-in")
        self.assertIn("sign in again", card.text)
        # The agent's own "Failed to authenticate" line isn't shown as well.
        self.assertFalse(any("Failed to authenticate" in c.text for c in cards))
        self.assertEqual(self.states[-1]["state"], "needs-sign-in")
        first_session = next(e["prompt_session"] for e in self.calls() if "prompt_session" in e)
        # The person signs in in a terminal (the agent's own login), then presses Try again.
        argv, env = runner.sign_in_command("terminal-login")
        login = acp.LoginTerminal(argv, env, self.root, prefer="pty")
        login.start()
        self.assertEqual(login.wait(20), 0)
        runner.signed_in()
        self.assertTrue(runner.wait_idle(30))
        self.assertIn({"resumed": first_session}, self.calls())
        self.assertEqual(Path(self.root, "hello.txt").read_text(), "hi")
        sessions = [e["prompt_session"] for e in self.calls() if "prompt_session" in e]
        self.assertEqual(sessions, [first_session, first_session])
        old = next(c for c in self.thread() if c.id == card.id)
        self.assertTrue(old.data["resolved"])
        self.assertNotIn("methods", old.data)


if __name__ == "__main__":
    unittest.main()
