# SPDX-License-Identifier: Apache-2.0
"""Sign in with ChatGPT for Ari, against a fake issuer and a fake Responses API.
No real sign-in, no network, and the real keyring is never touched."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_oauth_server import FakeIssuer  # noqa: E402
from fake_openai_server import FakeOpenAI  # noqa: E402

from luma_playground.agents import acp, chatgpt, sandbox  # noqa: E402
from luma_playground.agents.ari import AriError, Endpoint, endpoint_for  # noqa: E402
from luma_playground.agents.connections import Connection  # noqa: E402
from luma_playground.agents.runner import GoalRunner  # noqa: E402
from luma_playground.core.events import EventBus  # noqa: E402
from luma_playground.core.model import Member  # noqa: E402
from luma_playground.core.project import create_project  # noqa: E402
from luma_playground.core.store import ProjectStore  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(os.path.realpath(self.tmp.name))
        self.issuer = FakeIssuer()
        self.issuer.start()
        self.addCleanup(self.issuer.stop)
        patcher = mock.patch.dict(os.environ, {
            "PLAYGROUND_CONFIG_DIR": str(self.base / "config"), "PLAYGROUND_DATA_DIR": str(self.base / "data"),
            "PLAYGROUND_HOME": str(self.base / "projects"), "PLAYGROUND_SIWC": "1",
            "PLAYGROUND_SIWC_ISSUER": self.issuer.url, "PLAYGROUND_TOKEN_STORE": "file"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = chatgpt.TokenStore()
        self.assertEqual(self.store.backend, "file")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def sign_in(self, account: str = "ari-chatgpt") -> chatgpt.Account:
        flow = chatgpt.SignInFlow(account, store=self.store, open_browser=self.issuer.browser, timeout=20)
        return flow.run()

    def authorize_calls(self) -> list[dict]:
        return [q for path, q in self.issuer.log if path == "/api/accounts/authorize"]


class SignInTests(Base):
    def test_first_sign_in_registers_and_keeps_tokens_out_of_sight(self):
        account = self.sign_in()
        self.assertEqual(account.email, "noor@example.com")
        self.assertEqual(account.client_id, self.issuer.issued_client)
        self.assertTrue(account.plan_usage)
        q = self.authorize_calls()[0]
        self.assertEqual(q["client_id"], "dynamic_agent_client")
        self.assertEqual(q["agent_name_hint"], "Luma Playground")
        self.assertTrue(q["ext_agent_host_id"].startswith("urn:uuid:"))
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["response_type"], "code")
        self.assertEqual(q["resource"], "https://api.openai.com/v1")
        self.assertEqual(q["scope"].split(), ["openid", "profile", "email", "offline_access", "resource.invoke",
                                              "chatgpt.tokens.use.direct"])
        self.assertRegex(q["redirect_uri"], r"^http://127\.0\.0\.1:\d+/auth/callback$")
        self.assertNotIn("id_token_hint", q)
        record = self.store.get("ari-chatgpt")
        for key in ("access_token", "refresh_token", "id_token", "client_id", "subject", "ext_agent_host_id"):
            self.assertTrue(record.get(key), key)
        self.assertEqual(record["subject"], "user-123")
        path = self.base / "data" / "chatgpt" / "ari-chatgpt.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # Nothing about the sign-in is in a project or the connections file.
        self.assertFalse(list(self.base.glob("projects/**/*.json")))
        status = chatgpt.status("ari-chatgpt", self.store)
        self.assertEqual(status["state"], "signed-in")
        self.assertEqual(status["label"], "Using ChatGPT plan")
        self.assertNotIn("access_token", json.dumps(status))
        self.assertIn("private file", status["warning"])   # the file fallback says so

    def test_signing_in_again_uses_the_issued_client_and_hints(self):
        self.sign_in()
        host = self.authorize_calls()[0]["ext_agent_host_id"]
        self.sign_in()
        q = self.authorize_calls()[1]
        self.assertEqual(q["client_id"], self.issuer.issued_client)
        self.assertNotIn("agent_name_hint", q)
        self.assertTrue(q["id_token_hint"])
        self.assertEqual(q["login_hint"], "noor@example.com")
        self.assertEqual(q["ext_agent_host_id"], host)

    def test_saying_no_changes_nothing(self):
        self.issuer.deny = True
        with self.assertRaises(chatgpt.SignInDeclined) as caught:
            self.sign_in()
        self.assertIn("chose not to sign in", str(caught.exception))
        self.assertIsNone(self.store.get("ari-chatgpt"))

    def test_a_mismatched_state_is_refused(self):
        self.issuer.wrong_state = True
        with self.assertRaises(chatgpt.ChatGPTError) as caught:
            self.sign_in()
        self.assertIn("didn't match", str(caught.exception))
        self.assertIsNone(self.store.get("ari-chatgpt"))

    def test_a_badly_signed_id_token_is_refused(self):
        self.issuer.bad_signature = True
        with self.assertRaises(chatgpt.ChatGPTError) as caught:
            self.sign_in()
        self.assertIn("didn't check out", str(caught.exception))
        record = self.store.get("ari-chatgpt") or {}
        self.assertNotIn("access_token", record)
        self.assertEqual(record.get("client_id"), self.issuer.issued_client)   # kept for the next try

    def test_without_plan_scope_plan_use_is_off(self):
        self.issuer.grant_plan = False
        account = self.sign_in()
        self.assertFalse(account.plan_usage)
        self.assertEqual(chatgpt.status("ari-chatgpt", self.store)["state"], "plan-off")
        with self.assertRaises(chatgpt.PlanUsageOff):
            chatgpt.TokenManager("ari-chatgpt", store=self.store).access_token()

    def test_flag_off_means_coming_soon(self):
        with mock.patch.dict(os.environ, {"PLAYGROUND_SIWC": "0"}):
            self.assertFalse(chatgpt.enabled())
            self.assertEqual(chatgpt.status("ari-chatgpt", self.store)["state"], "coming-soon")
            ep = endpoint_for(Connection(id="ari-chatgpt", label="Ari", kind="ari", provider="ari",
                                         endpoint="chatgpt", owner="noor"))
            self.assertIsNone(ep.token_source)
        with mock.patch.dict(os.environ, {"PLAYGROUND_SIWC": ""}):
            self.assertTrue(chatgpt.enabled())                 # on unless turned off
            Path(self.base / "config").mkdir(exist_ok=True)
            (self.base / "config" / "features.toml").write_text("[features]\nsiwc = false\n")
            self.assertFalse(chatgpt.enabled())


class RefreshTests(Base):
    def test_refresh_near_expiry_rotates_and_saves(self):
        self.sign_in()
        manager = chatgpt.TokenManager("ari-chatgpt", store=self.store)
        first = self.store.get("ari-chatgpt")
        self.assertEqual(manager.access_token(), first["access_token"])      # fresh: no refresh
        later = chatgpt.TokenManager("ari-chatgpt", store=self.store, clock=lambda: time.time() + 3500)
        token = later.access_token()
        second = self.store.get("ari-chatgpt")
        self.assertNotEqual(token, first["access_token"])
        self.assertEqual(token, second["access_token"])
        self.assertNotEqual(second["refresh_token"], first["refresh_token"])  # rotated and saved
        refresh = [f for path, f in self.issuer.log if path.endswith("/oauth/token") and f["grant_type"] == "refresh_token"]
        self.assertEqual(refresh[0]["client_id"], self.issuer.issued_client)
        self.assertEqual(refresh[0]["resource"], "https://api.openai.com/v1")
        self.assertNotIn("scope", refresh[0])

    def test_a_dead_refresh_token_asks_to_sign_in_and_keeps_the_client(self):
        self.sign_in()
        self.issuer.fail_refresh = "invalid_grant"
        manager = chatgpt.TokenManager("ari-chatgpt", store=self.store)
        with self.assertRaises(chatgpt.SignInNeeded) as caught:
            manager.access_token(force_refresh=True)
        self.assertIn("Sign in again", str(caught.exception))
        record = self.store.get("ari-chatgpt")
        self.assertNotIn("refresh_token", record)
        self.assertEqual(record["client_id"], self.issuer.issued_client)
        self.assertEqual(chatgpt.status("ari-chatgpt", self.store)["state"], "signed-out")

    def test_a_busy_server_keeps_the_sign_in(self):
        self.sign_in()
        self.issuer.fail_refresh = "server_error"
        with self.assertRaises(chatgpt.ChatGPTError) as caught:
            chatgpt.TokenManager("ari-chatgpt", store=self.store).access_token(force_refresh=True)
        self.assertNotIsInstance(caught.exception, acp.AuthRequired)
        self.assertTrue(self.store.get("ari-chatgpt")["refresh_token"])

    def test_sign_out_revokes_and_forgets_tokens(self):
        self.sign_in()
        old = self.store.get("ari-chatgpt")["refresh_token"]
        self.assertTrue(chatgpt.TokenManager("ari-chatgpt", store=self.store).sign_out())
        self.assertEqual(self.issuer.revoked, [old])
        record = self.store.get("ari-chatgpt")
        self.assertEqual(set(record), {"issuer", "subject", "email", "client_id", "ext_agent_host_id"})


class KeyringTests(unittest.TestCase):
    """The keyring commands, with a stand-in for secret-tool and security."""

    def setUp(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.saved = ""

    def runner(self, argv, input=None, **kw):
        self.calls.append((list(argv), input))
        if "store" in argv or argv[:2] == ["security", "-i"]:
            self.saved = input
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "lookup" in argv:
            return subprocess.CompletedProcess(argv, 0, self.saved, "")
        if "find-generic-password" in argv:
            hexed = self.saved.split(" -X ")[1].strip()
            return subprocess.CompletedProcess(argv, 0, hexed + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def test_secret_tool_gets_the_secret_on_stdin(self):
        store = chatgpt.TokenStore("secret-tool", runner=self.runner)
        store.put("ari-chatgpt", {"refresh_token": "rt_secret"})
        argv, given = self.calls[0]
        self.assertEqual(argv[:2], ["secret-tool", "store"])
        self.assertNotIn("rt_secret", " ".join(argv))
        self.assertIn("rt_secret", given)
        self.assertEqual(store.get("ari-chatgpt"), {"refresh_token": "rt_secret"})
        store.delete("ari-chatgpt")
        self.assertEqual(self.calls[-1][0][:2], ["secret-tool", "clear"])
        self.assertEqual(store.warning, "")

    def test_keychain_never_puts_the_secret_on_a_command_line(self):
        store = chatgpt.TokenStore("keychain", runner=self.runner)
        store.put("ari-chatgpt", {"refresh_token": "rt_secret"})
        argv, given = self.calls[0]
        self.assertEqual(argv, ["security", "-i"])
        self.assertNotIn("rt_secret", given)                          # hex, on stdin
        self.assertIn("add-generic-password -U -s org.projectluma.Playground.chatgpt -a ari-chatgpt -X ", given)
        self.assertEqual(store.get("ari-chatgpt"), {"refresh_token": "rt_secret"})

    def test_account_ids_are_checked(self):
        with self.assertRaises(ValueError):
            chatgpt.TokenStore("keychain", runner=self.runner).put("x; rm -rf /", {})


class IdTokenTests(unittest.TestCase):
    def test_claims_are_checked(self):
        from fake_oauth_server import RsaKey
        key = RsaKey()
        jwks = {"keys": [key.jwk()]}
        now = time.time()
        good = {"iss": "https://auth.openai.com", "aud": "oaiapp_1", "sub": "u", "nonce": "n", "exp": now + 60}
        claims = chatgpt.verify_id_token(key.sign(good), jwks=jwks, issuer="https://auth.openai.com",
                                         audience="oaiapp_1", nonce="n", now=now)
        self.assertEqual(claims["sub"], "u")
        for change in ({"iss": "https://evil"}, {"aud": "other"}, {"nonce": "x"}, {"exp": now - 3600}):
            with self.subTest(change=change), self.assertRaises(chatgpt.ChatGPTError):
                chatgpt.verify_id_token(key.sign({**good, **change}), jwks=jwks, issuer="https://auth.openai.com",
                                        audience="oaiapp_1", nonce="n", now=now)
        token = key.sign(good)
        tampered = token.rsplit(".", 1)[0][:-2] + "xx." + token.rsplit(".", 1)[1]
        with self.assertRaises(chatgpt.ChatGPTError):
            chatgpt.verify_id_token(tampered, jwks=jwks, issuer="https://auth.openai.com", audience="oaiapp_1",
                                    nonce="n", now=now)


class AriOnPlanTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.bus = EventBus()
        project = create_project("Kiln", "A pottery class sign-up", root=self.base / "projects", git=False,
                                 bus=self.bus, owner=Member(id="noor", name="Noor", role="owner"))
        self.root = str(project.path)
        self.pstore = ProjectStore(self.root, bus=self.bus)
        self.goal = self.pstore.create_goal("Say hi", outcome="hello.txt says hi", owner="noor")
        self.states: list[dict] = []
        self.bus.subscribe("agent.state", lambda **kw: self.states.append(kw))

    def api(self, script) -> str:
        self.fake = FakeOpenAI(script)
        self.addCleanup(self.fake.stop)
        return self.fake.start()

    def connection(self, url: str) -> Connection:
        return Connection(id="ari-chatgpt", label="Ari", kind="ari", provider="ari", owner="noor",
                          endpoint="chatgpt", base_url=url)

    def runner(self, url: str) -> GoalRunner:
        runner = GoalRunner(self.root, self.goal.id, self.connection(url), self.bus,
                            sandbox_policy=sandbox.SandboxPolicy(self.root, backend="none"))
        self.addCleanup(runner.close)
        return runner

    def thread(self):
        return self.pstore.read_thread(self.goal.id, fold=True)

    def test_a_turn_on_the_plan_through_the_responses_api(self):
        self.sign_in()
        url = self.api([{"text": "Writing it.", "tool_calls": [{"name": "write_file",
                                                                "arguments": {"path": "hello.txt", "content": "hi"}}]},
                        {"text": "Done."}])
        runner = self.runner(url)
        runner.send("make hello.txt")
        self.assertTrue(runner.wait_idle(30))
        self.assertEqual(Path(self.root, "hello.txt").read_text(), "hi")
        first, second = self.fake.requests
        self.assertEqual(first["model"], "gpt-6.1-sol")             # the first listed model, not the hidden one
        self.assertIs(first["store"], False)
        self.assertIs(first["stream"], True)
        self.assertIn("You are Ari", first["instructions"])
        self.assertFalse([i for i in first["input"] if i.get("role") == "system"])
        self.assertTrue(all(t["type"] == "function" and t["name"] for t in first["tools"]))
        for unsupported in ("temperature", "max_output_tokens", "previous_response_id", "metadata", "user"):
            self.assertNotIn(unsupported, first)
        call = next(i for i in second["input"] if i.get("type") == "function_call")
        output = next(i for i in second["input"] if i.get("type") == "function_call_output")
        self.assertEqual(call["call_id"], output["call_id"])
        self.assertEqual(json.loads(call["arguments"]), {"path": "hello.txt", "content": "hi"})
        token = self.store.get("ari-chatgpt")["access_token"]
        self.assertEqual(self.fake.headers[0]["authorization"], f"Bearer {token}")
        texts = [c.text for c in self.thread() if c.kind == "message" and c.role == "agent"]
        self.assertEqual(texts, ["Writing it.", "Done."])

    def test_a_rejected_token_is_refreshed_once(self):
        self.sign_in()
        url = self.api([{"status": 401, "code": "subscription_sharing_invalid_user", "message": "expired"},
                        {"text": "Hi."}])
        old = self.store.get("ari-chatgpt")["access_token"]
        runner = self.runner(url)
        runner.send("hi")
        self.assertTrue(runner.wait_idle(30))
        new = self.store.get("ari-chatgpt")["access_token"]
        self.assertNotEqual(old, new)
        self.assertEqual(self.fake.headers[1]["authorization"], f"Bearer {new}")
        self.assertEqual(self.thread()[-1].text, "Hi.")

    def test_usage_limit_mid_stream_makes_manage_usage_the_way_on(self):
        self.sign_in()
        url = self.api([{"text": "Start", "fail_code": "subscription_sharing_usage_limit_exceeded"}])
        runner = self.runner(url)
        runner.send("hi")
        self.assertTrue(runner.wait_idle(30))
        card = self.thread()[-1]
        self.assertIn("reached the limit", card.text)
        self.assertEqual(card.data["action"], "manage-usage")
        self.assertEqual(card.data["url"], "https://chatgpt.com/settings/usage")
        self.assertEqual(self.states[-1]["reason"], "usage-limit")

    def test_a_stream_that_just_ends_is_not_a_success(self):
        self.sign_in()
        url = self.api([{"text": "Half", "cut": True}])
        runner = self.runner(url)
        runner.send("hi")
        self.assertTrue(runner.wait_idle(30))
        self.assertIn("stopped answering partway", self.thread()[-1].text)

    def test_not_signed_in_shows_continue_with_chatgpt_then_carries_on(self):
        url = self.api([{"text": "Hello from your plan."}])
        with mock.patch.object(webbrowser, "open", self.issuer.browser):
            runner = self.runner(url)
            runner.send("hi")
            self.assertTrue(runner.wait_idle(30))
            card = self.thread()[-1]
            self.assertEqual(card.data["action"], "sign-in")
            self.assertIn("password goes only to OpenAI", card.text)
            self.assertEqual(self.states[-1]["state"], "needs-sign-in")
            self.assertEqual([m["id"] for m in self.states[-1]["methods"]], ["chatgpt"])
            self.assertEqual(self.states[-1]["methods"][0]["name"], "Continue with ChatGPT")
            runner.sign_in_with_agent("chatgpt")
            self.assertTrue(runner.wait_idle(30))
        texts = [c.text for c in self.thread()]
        self.assertIn("Signed in.", texts)
        self.assertEqual(texts[-1], "Hello from your plan.")
        self.assertTrue(next(c for c in self.thread() if c.id == card.id).data["resolved"])

    def test_flag_off_is_an_honest_coming_soon(self):
        with mock.patch.dict(os.environ, {"PLAYGROUND_SIWC": "0"}):
            runner = self.runner("http://127.0.0.1:9/v1")
            runner.send("hi")
            self.assertTrue(runner.wait_idle(30))
        card = self.thread()[-1]
        self.assertIn("coming soon", card.text)
        self.assertNotIn("action", card.data)

    def test_endpoint_label(self):
        ep = endpoint_for(self.connection("http://x/v1"))
        self.assertEqual(ep.label, "Using ChatGPT plan")
        self.assertIsNotNone(ep.token_source)


if __name__ == "__main__":
    unittest.main()
