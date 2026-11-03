# SPDX-License-Identifier: Apache-2.0
"""The MCP server in-process: each tool's behaviour, errors and the protocol edges."""
import base64
import os
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from luma_playground.core import events, project
from luma_playground.core.model import GoalStatus, Member, Place
from luma_playground.core.store import ProjectStore
from luma_playground.mcp import server as mcp

NICK = Member(id="nick", name="Nick", role="owner")
MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
          "io.modelcontextprotocol/clientInfo": {"name": "Test Agent", "version": "1"}}


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        made = project.create_project("Kiln", root=self.tmp.name, owner=NICK, git=False, bus=events.EventBus())
        self.root = made.path
        self.store = ProjectStore(self.root, bus=events.EventBus())
        self.out = io.BytesIO()
        self.server = mcp.PlaygroundServer(self.root, stdin=io.BytesIO(), stdout=self.out, actor="ari",
                                           decision_timeout=5, keepalive=0.2, poll=0.05)
        self.ids = 0

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, method, params=None):
        self.ids += 1
        return self.server.handle({"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params or {}})

    def call(self, tool, /, **arguments):
        response = self.request("tools/call", {"name": tool, "arguments": arguments})
        return response["result"]

    def ok(self, tool, /, **arguments):
        result = self.call(tool, **arguments)
        self.assertFalse(result["isError"], result["content"][0]["text"])
        return result.get("structuredContent")

    def error_text(self, tool, /, **arguments):
        result = self.call(tool, **arguments)
        self.assertTrue(result["isError"], "expected the tool to refuse")
        return result["content"][0]["text"]

    def when_waiting(self, respond):
        """Run ``respond()`` on another thread the moment the server starts waiting for an answer.

        The server writes its request first, then waits; wrapping its wait gives the exact moment,
        so nothing depends on guessing how long that takes. ``respond`` returns a list of problems
        (or raises); the returned ``finish()`` joins the thread and fails the test with them."""
        waiting = threading.Event()
        real_wait = self.server.wait_for_answer

        def wait_for_answer(*args, **kwargs):
            waiting.set()
            return real_wait(*args, **kwargs)

        self.server.wait_for_answer = wait_for_answer
        errors = []

        def run():
            try:
                if not waiting.wait(60):
                    errors.append("the server never started waiting")
                    return
                errors.extend(respond() or [])
            except Exception as error:  # noqa: BLE001 - reported by finish()
                errors.append(repr(error))

        worker = threading.Thread(target=run, daemon=True)
        worker.start()

        def finish():
            worker.join(60)
            self.assertFalse(worker.is_alive(), "the answering thread is stuck")
            self.assertEqual(errors, [])

        return finish


class ProtocolTests(ServerTestCase):
    def test_initialize_negotiates(self):
        result = self.request("initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "Claude Code"}})["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "playground")
        self.assertIn("tools", result["capabilities"])
        result = self.request("initialize", {"protocolVersion": "2099-01-01"})["result"]
        self.assertEqual(result["protocolVersion"], "2025-11-25")

    def test_discover_and_modern_requests(self):
        result = self.request("server/discover", {"_meta": MODERN})["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIn("2026-07-28", result["supportedVersions"])
        self.assertIn("2025-11-25", result["supportedVersions"])
        self.assertEqual(result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "playground")
        listed = self.request("tools/list", {"_meta": MODERN})["result"]
        self.assertEqual(listed["resultType"], "complete")
        self.assertIn("ttlMs", listed)
        bad = self.request("tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}})
        self.assertEqual(bad["error"]["code"], -32022)
        self.assertEqual(bad["error"]["data"]["requested"], "1900-01-01")

    def test_agents_are_shown_only_the_tools_that_need_playground(self):
        names = [t["name"] for t in self.request("tools/list")["result"]["tools"]]
        self.assertEqual(sorted(names), sorted(mcp.LISTED))
        self.assertLessEqual(len(names), 18)
        # The rest still answer by name, for older instructions.
        self.assertFalse(self.request("tools/call", {"name": "playground_goal_list", "arguments": {}})["result"]
                         .get("isError"))

    def test_tools_are_complete_and_ordered(self):
        with mock.patch.dict(os.environ, {"PLAYGROUND_MCP_ALL": "1"}):
            tools = self.request("tools/list")["result"]["tools"]
            names = [t["name"] for t in tools]
            self.assertEqual(names, [t["name"] for t in self.request("tools/list")["result"]["tools"]])
        for required in ("goal_list", "goal_get", "goal_update", "goal_create", "decision_ask", "decision_record",
                         "activity_log", "knowledge_add", "knowledge_search", "brief_get", "brief_update",
                         "preview_status", "preview_screenshot", "checks_run", "proof_attach", "release_propose",
                         "places_list", "cost_report", "stack_list", "stack_add_service", "stack_update_service",
                         "stack_remove_service", "issue_create", "issue_update", "issue_list", "usage_record",
                         "analytics_record"):
            self.assertIn(f"playground_{required}", names)
        for tool in tools:
            self.assertRegex(tool["name"], r"^playground_[a-z_]+$")
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertTrue(tool["description"])
            self.assertNotIn("—", tool["description"])

    def test_errors(self):
        self.assertEqual(self.request("nope")["error"]["code"], -32601)
        self.assertEqual(self.request("tools/call", {"name": "nope"})["error"]["code"], -32602)
        self.assertEqual(self.request("ping")["result"], {})
        self.server._receive(b"{not json")
        self.assertEqual(json.loads(self.out.getvalue().decode().splitlines()[-1])["error"]["code"], -32700)

    def test_notifications_get_no_reply(self):
        self.server._receive(b'{"jsonrpc":"2.0","method":"notifications/initialized"}')
        self.server._receive(b'{"jsonrpc":"2.0","id":9,"result":{}}')
        self.assertEqual(self.out.getvalue(), b"")


class GoalToolTests(ServerTestCase):
    def test_create_get_update(self):
        goal = self.ok("playground_goal_create", title="Add a waitlist", size="medium",
                       cost_estimate="About $0.60", stories=[{"title": "Someone can join",
                                                              "checks": ["The button says Join waitlist", "They see their place"]}])
        goal_id = goal["id"]
        self.assertEqual(goal["connection"], "ari")
        self.assertEqual(goal["owner"], "nick")
        self.assertEqual(self.store.read_thread(goal_id)[0].kind, "plan")
        got = self.ok("playground_goal_get", goal_id=goal_id)
        self.assertEqual(got["stories"][0]["checks"][0]["status"], "pending")
        self.assertEqual(got["recent_thread"][0]["kind"], "plan")

        updated = self.ok("playground_goal_update", goal_id=goal_id, status="ready", status_note="on Stage",
                          story_done=[{"story": 1, "done": True}],
                          check_results=[{"text": "the button says join waitlist", "status": "pass"}], note="Looks right.")
        self.assertEqual(updated["status"], "ready")
        self.assertTrue(updated["stories"][0]["done"])
        self.assertEqual(updated["stories"][0]["checks"][0]["status"], "pass")
        self.assertIn("Looks right.", updated["notes"])

        # A new plan keeps the result of a check with the same text.
        replanned = self.ok("playground_goal_update", goal_id=goal_id,
                            stories=[{"title": "Someone can join", "checks": ["The button says Join waitlist", "New check"]}])
        self.assertEqual([c["status"] for c in replanned["stories"][0]["checks"]], ["pass", "pending"])

        listed = self.ok("playground_goal_list", status="ready")["items"]
        self.assertEqual([g["id"] for g in listed], [goal_id])

    def test_refusals(self):
        self.assertIn("reviewed release", self.error_text("playground_goal_create", title="X", status="live"))
        goal_id = self.ok("playground_goal_create", title="X")["id"]
        self.assertIn("reviewed release", self.error_text("playground_goal_update", goal_id=goal_id, status="live"))
        self.assertIn("no goal", self.error_text("playground_goal_get", goal_id="nope-0000"))
        self.assertIn("goal id", self.error_text("playground_goal_get", goal_id="../../etc"))
        self.assertIn("Say which goal", self.error_text("playground_goal_get"))
        self.assertIn("title", self.error_text("playground_goal_create"))
        self.assertIn("outside", self.error_text("playground_step_report", goal_id=goal_id, title="T", files=["../x"]))
        failures = [e for e in self.store.read_activity() if e.data.get("ok") is False]
        self.assertGreaterEqual(len(failures), 5)

    def test_default_goal_from_environment(self):
        goal_id = self.ok("playground_goal_create", title="Texts")["id"]
        self.server.default_goal = goal_id
        self.assertEqual(self.ok("playground_goal_get")["id"], goal_id)
        self.ok("playground_step_report", title="Fixed numbers", files=["src/phone.js"], spend_text="$0.05")
        step = self.store.read_thread(goal_id)[-1]
        self.assertEqual((step.kind, step.data["files"], step.by), ("step", ["src/phone.js"], "ari"))


class BookToolTests(ServerTestCase):
    def test_every_call_is_logged(self):
        before = len(self.store.read_activity())
        self.ok("playground_project_info")
        self.ok("playground_places_list")
        self.ok("playground_brief_get")
        self.ok("playground_activity_log", summary="Fixed the booking form", files=["src/index.html"])
        entries = self.store.read_activity()[before:]
        self.assertEqual(len(entries), 4)
        self.assertEqual([e.data.get("tool") for e in entries],
                         ["playground_project_info", "playground_places_list", "playground_brief_get", "playground_activity_log"])
        self.assertEqual(entries[-1].summary, "Fixed the booking form")
        self.assertEqual(entries[-1].actor, "ari")
        recent = self.ok("playground_activity_list", limit=5)["items"]
        self.assertEqual(recent[-1]["summary"], "Fixed the booking form")
        self.assertNotIn("tool", [e["kind"] for e in recent])

    def test_knowledge(self):
        self.ok("playground_knowledge_add", title="Phone numbers need the country code", body="Use +1.")
        self.ok("playground_knowledge_add", title="Phone numbers need the country code", body="Also for Canada.",
                slug="phone-numbers-need-the-country-code", mode="append")
        found = self.ok("playground_knowledge_search", query="country code")["items"]
        self.assertEqual(found[0]["slug"], "phone-numbers-need-the-country-code")
        note = self.ok("playground_knowledge_get", slug="phone-numbers-need-the-country-code")
        self.assertEqual(note["body"], "Use +1.\n\nAlso for Canada.")
        self.assertIn("slug", self.error_text("playground_knowledge_add", title="T", body="B", slug="Bad Slug"))

    def test_brief_and_decisions(self):
        brief = self.ok("playground_brief_update", summary="Booking for Maya's studio",
                        good_looks_like=["Booking takes under a minute"], platforms=["phones"])
        self.assertEqual(brief["good_looks_like"], ["Booking takes under a minute"])
        self.assertEqual(self.store.read_thread("talk-to-ari")[-1].kind, "brief")
        self.ok("playground_decision_record", title="Classes cap at eight", chosen="Eight", why="One wheel each.")
        self.assertEqual(self.store.read_decisions()[-1].who, "ari")
        self.assertIn("Nothing to change", self.error_text("playground_brief_update"))

    def test_places_have_no_secrets(self):
        p = self.store.read_project()
        p.places.append(Place("production", "Production", "production", "ssh", "kiln.example", "deploy"))
        self.store.write_project(p)
        places = self.ok("playground_places_list")["items"]
        self.assertEqual([pl["id"] for pl in places], ["this-computer", "production"])
        text = json.dumps(places).lower()
        for word in ("password", "token", "secret", "key"):
            self.assertNotIn(word, text)

    def test_preview_status(self):
        self.assertEqual(self.ok("playground_preview_status")["state"], "stopped")
        self.store.write_preview_state("running", "http://127.0.0.1:4000/")
        self.assertEqual(self.ok("playground_preview_status")["url"], "http://127.0.0.1:4000/")

    def test_cost_and_headsup(self):
        self.ok("playground_cost_report", text="This goal will take about 6% of your Claude week")
        self.ok("playground_headsup_post", text="I renamed Full to Class full.", files=["src/index.html"],
                undo={"kind": "git", "ref": "abc123"})
        kinds = [e.kind for e in self.store.read_thread("talk-to-ari")]
        self.assertEqual(kinds[-2:], ["cost", "headsup"])


class ProofAndReleaseTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.goal_id = self.ok("playground_goal_create", title="Photos", stories=[
            {"title": "People see the studio", "checks": ["Six photos in a row", "Book stays on the first screen"]}])["id"]
        (self.root / "shots").mkdir()
        (self.root / "shots" / "home.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

    def test_proof_then_release(self):
        not_ready = self.ok("playground_release_propose", goal_id=self.goal_id, changes_for_people=["Photos on the home page"])
        self.assertFalse(not_ready["ready"])
        self.assertIn("outside", self.error_text("playground_proof_attach", goal_id=self.goal_id, shots=["/etc/passwd"],
                                                 checks=[{"text": "Six photos in a row", "status": "pass"}]))
        self.assertIn("doesn't exist", self.error_text("playground_proof_attach", goal_id=self.goal_id, shots=["missing.png"],
                                                       checks=[{"text": "Six photos in a row", "status": "pass"}]))
        self.ok("playground_proof_attach", goal_id=self.goal_id, shots=["shots/home.png"],
                checks=[{"text": "Six photos in a row", "status": "pass", "evidence": "shots/home.png"},
                        {"text": "Book stays on the first screen", "status": "pass"}])
        goal = self.store.read_goal(self.goal_id)
        self.assertEqual(goal.checks_passed, 2)
        self.assertEqual(goal.checks[0].evidence, "shots/home.png")
        review = self.ok("playground_release_propose", goal_id=self.goal_id, changes_for_people=["Photos on the home page"],
                         data_changes=["No changes to data."])
        self.assertTrue(review["ready"])
        self.assertEqual(self.store.read_goal(self.goal_id).status, GoalStatus.READY)
        self.assertEqual(self.store.read_thread(self.goal_id)[-1].kind, "review")

    def test_screenshot_through_the_app(self):
        self.assertIn("isn't open", self.error_text("playground_preview_screenshot"))
        self.store.write_app_heartbeat()

        def app():
            from luma_playground.core.model import Answer
            requests = self.store.list_pending("screenshot")
            if len(requests) != 1:
                return [f"expected one screenshot request, found {len(requests)}"]
            self.store.write_answer(Answer(id=requests[0].id, data={"path": "shots/home.png"}))
            return []

        finish = self.when_waiting(app)
        result = self.call("playground_preview_screenshot", device="phone")
        finish()
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["path"], "shots/home.png")
        image = [c for c in result["content"] if c["type"] == "image"][0]
        self.assertEqual(base64.b64decode(image["data"])[:4], b"\x89PNG")
        self.assertEqual(self.store.list_pending(), [])

    def test_checks_through_the_app(self):
        self.store.write_app_heartbeat()

        def checker():
            from luma_playground.core.model import Answer
            requests = self.store.list_pending("checks")
            if len(requests) != 1:
                return [f"expected one checks request, found {len(requests)}"]
            self.store.write_answer(Answer(id=requests[0].id, data={"checks": [
                {"text": "Six photos in a row", "status": "pass"},
                {"text": "Book stays on the first screen", "status": "fail"}]}))
            return []

        finish = self.when_waiting(checker)
        data = self.ok("playground_checks_run", goal_id=self.goal_id)
        finish()
        self.assertEqual([c["status"] for c in data["checks"]], ["pass", "fail"])


class SpaceToolTests(ServerTestCase):
    SERVICE = {"name": "Resend", "kind": "email", "purpose": "Sends booking confirmations and reminders",
               "environments": {"stage": "Test mode", "live": "mail.kiln.example"}, "console_url": "https://resend.com/emails",
               "plan": "Free", "monthly_budget": 0, "secrets": ["Email service key"], "usage_source": "resend"}

    def test_stack_tools(self):
        added = self.ok("playground_stack_add_service", **self.SERVICE)
        self.assertEqual((added["id"], added["provider"], added["added_by"]), ("resend", "Resend", "ari"))
        self.assertIn("secret names", self.error_text("playground_stack_add_service", **{**self.SERVICE, "id": "other",
                                                                                         "secrets": ["re_abcdefghijklmnop123456"]}))
        self.assertIn("kind", self.error_text("playground_stack_add_service", **{**self.SERVICE, "kind": "rocket"}))
        self.assertIn("purpose", self.error_text("playground_stack_add_service", name="X", kind="other"))
        updated = self.ok("playground_stack_update_service", service_id="resend", plan="Pro ($20 a month)", monthly_budget=20)
        self.assertEqual((updated["plan"], updated["monthly_budget"]), ("Pro ($20 a month)", 20.0))
        self.assertIn("no service", self.error_text("playground_stack_update_service", service_id="nope", plan="x"))

        (self.root / "package.json").write_text('{"dependencies": {"resend": "^4.0.1"}}')
        self.ok("playground_usage_record", service_id="resend", period="2026-10-08", metrics={"emails_sent": 14}, cost=0)
        listed = self.ok("playground_stack_list", refresh_dependencies=True,
                         dependency_notes={"resend": "Sends the emails"})
        self.assertEqual(listed["services"][0]["latest_usage"]["metrics"], {"emails_sent": 14})
        self.assertEqual(listed["dependencies"], [{"name": "resend", "version": "^4.0.1", "why": "Sends the emails",
                                                  "source": "package.json"}])
        self.ok("playground_stack_remove_service", service_id="resend", why="Moved to another email service")
        self.assertEqual(self.store.read_stack().services, [])
        kinds = [e.kind for e in self.store.read_activity()]
        self.assertGreaterEqual(kinds.count("stack"), 3)

    def test_issue_tools(self):
        goal_id = self.ok("playground_goal_create", title="Fix reminder texts")["id"]
        bug = self.ok("playground_issue_create", title="Two reminder texts failed", severity="high", place="live",
                      what_happened="Two people got no reminder.", expected="Everyone gets one.",
                      steps=["Book with 510 555 0142", "Wait for the reminder"], evidence=["Twilio error 21211"],
                      goal_id=goal_id)
        self.assertEqual((bug["number"], bug["reporter"], bug["goal"]), (1, "ari", goal_id))
        self.assertEqual(bug["steps"], "1. Book with 510 555 0142\n2. Wait for the reminder")
        updated = self.ok("playground_issue_update", number=1, status="fixing", note="Found it: no country code.")
        self.assertEqual(updated["status"], "fixing")
        self.assertEqual(updated["sections"], [{"heading": "Notes", "text": "Found it: no country code."}])
        self.assertIn("no bug", self.error_text("playground_issue_update", number=7, status="fixed"))
        self.assertIn("status", self.error_text("playground_issue_update", number=1, status="gone"))
        self.ok("playground_issue_create", title="Small thing", severity="low")
        self.ok("playground_issue_update", number=2, status="fixed")
        self.assertEqual([i["number"] for i in self.ok("playground_issue_list")["items"]], [1])
        self.assertEqual([i["number"] for i in self.ok("playground_issue_list", include_closed=True)["items"]], [2, 1])
        self.assertEqual(self.store.read_activity(goal=goal_id)[-1].summary.startswith("Bug #1 is now fixing"), True)

    def test_usage_and_analytics_tools(self):
        self.assertIn("Add it", self.error_text("playground_usage_record", service_id="resend", period="2026-10",
                                                metrics={"emails_sent": 1}))
        self.ok("playground_stack_add_service", **self.SERVICE)
        self.assertIn("numbers only", self.error_text("playground_usage_record", service_id="resend", period="2026-10",
                                                      metrics={"emails_sent": "many"}).lower())
        usage = self.ok("playground_usage_record", service_id="resend", period="2026-10", metrics={"emails_sent": 412},
                        cost=0)
        self.assertEqual(usage["source"], "agent:ari")
        snapshot = self.ok("playground_analytics_record", period="2026-W41", visitors=120, sessions=150,
                           top_pages=[{"path": "/", "views": 300}], errors=0)
        self.assertEqual(snapshot["visitors"], 120)
        self.assertIn("top_pages", self.error_text("playground_analytics_record", period="2026-W41", top_pages=["/"]))
        self.assertIn("whole number", self.error_text("playground_analytics_record", period="x", visitors=-1))
        self.assertEqual(len(self.store.read_analytics()), 1)


class DecisionToolTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.goal_id = self.ok("playground_goal_create", title="Add a waitlist")["id"]
        self.options = [{"id": "text", "label": "A text message", "consequence": "Quick"},
                        {"id": "email", "label": "An email", "consequence": "Slower"}]

    def answer_soon(self, option, by="nick"):
        def respond():
            pending = self.store.list_pending("decision")
            if len(pending) != 1:
                return [f"expected one open question, found {len(pending)}"]
            problems = []
            status = self.store.read_goal(self.goal_id).status
            if status != GoalStatus.NEEDS_YOU:
                problems.append(f"the goal should say Needs you while it waits, not {status}")
            self.store.answer_decision(pending[0].id, option, by=by)
            return problems

        return self.when_waiting(respond)

    def test_ask_waits_for_the_answer(self):
        # A generous limit: the answer ends the wait at once, so this only matters on a crawling machine.
        self.server.decision_timeout = 120
        finish = self.answer_soon("email")
        data = self.ok("playground_decision_ask", goal_id=self.goal_id, question="How should they hear?",
                       options=self.options, recommended="A text message")
        finish()
        self.assertEqual(data.get("state"), "answered", data)
        self.assertEqual((data["option"], data["label"], data["answered_by"]), ("email", "An email", "nick"))
        self.assertEqual(self.store.list_pending(), [])
        self.assertEqual(self.store.read_goal(self.goal_id).status, GoalStatus.WORKING)
        card = [e for e in self.store.read_thread(self.goal_id) if e.kind == "decision"][0]
        self.assertEqual(card.data["recommended"], "text")
        self.assertEqual(card.data["answered"]["label"], "An email")

    def test_validation(self):
        self.assertIn("two to four", self.error_text("playground_decision_ask", question="Q", options=self.options[:1]))
        self.assertIn("recommended", self.error_text("playground_decision_ask", question="Q", options=self.options,
                                                     recommended="carrier pigeon"))

    def test_without_waiting(self):
        data = self.ok("playground_decision_ask", goal_id=self.goal_id, question="Q?", options=self.options, wait=False)
        self.assertEqual(self.ok("playground_decision_get", decision_id=data["decision_id"])["state"], "waiting")
        self.store.answer_decision(data["decision_id"], "text", by="nick")
        self.assertEqual(self.ok("playground_decision_get", decision_id=data["decision_id"])["option"], "text")

    def test_timeout_closes_the_question(self):
        self.server.decision_timeout = 0.3
        data = self.ok("playground_decision_ask", goal_id=self.goal_id, question="Q?", options=self.options)
        self.assertEqual(data["state"], "no answer")
        self.assertEqual(self.store.list_pending(), [])
        self.assertEqual(self.store.read_goal(self.goal_id).status, GoalStatus.WORKING)

    def test_a_slow_answer_leaves_the_question_open_for_the_next_turn(self):
        # Agents give up on a tool call after a minute, so the call stops waiting before then; the card stays.
        self.server.decision_timeout = 30 * 60
        self.server.decision_wait = 0.3
        data = self.ok("playground_decision_ask", goal_id=self.goal_id, question="Q?", options=self.options)
        self.assertEqual(data["state"], "waiting")
        self.assertEqual([p.id for p in self.store.list_pending()], [data["decision_id"]])
        self.assertEqual(self.store.read_goal(self.goal_id).status, GoalStatus.NEEDS_YOU)

    def test_progress_and_cancel(self):
        self.server.decision_timeout = 30
        message = {"jsonrpc": "2.0", "id": 77, "method": "tools/call",
                   "params": {"name": "playground_decision_ask", "_meta": {"progressToken": "tok"},
                              "arguments": {"goal_id": self.goal_id, "question": "Q?", "options": self.options}}}
        two_beats = threading.Event()
        real_send = self.server.send
        beats = []

        def send(message):
            real_send(message)
            if message.get("method") == "notifications/progress":
                beats.append(message)
                if len(beats) >= 2:
                    two_beats.set()

        self.server.send = send
        self.server._receive(json.dumps(message).encode())
        self.assertTrue(two_beats.wait(60), "no keepalive progress arrived")
        self.server._receive(b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":77}}')
        for worker in self.server._workers:
            worker.join(5)
        lines = [json.loads(line) for line in self.out.getvalue().decode().splitlines()]
        progress = [m for m in lines if m.get("method") == "notifications/progress"]
        self.assertGreaterEqual(len(progress), 2)
        self.assertEqual(progress[0]["params"]["progressToken"], "tok")
        self.assertLess(progress[0]["params"]["progress"], progress[1]["params"]["progress"])
        self.assertFalse([m for m in lines if m.get("id") == 77], "a cancelled request gets no response")
        self.assertEqual(self.store.list_pending(), [])


class HarnessToolTests(ServerTestCase):
    def test_tasks_claims_and_the_channel(self):
        task = self.ok("playground_task_create", title="Let parents offer rides", priority="high",
                       checks=["A parent can offer seats"])
        self.assertIn("- [ ] A parent can offer seats", self.store.read_task(task["id"]).body)
        self.assertEqual(self.ok("playground_task_update", id=task["id"], action="claim")["claimed_by"], "ari")
        self.store.update_task(task["id"], lambda t: setattr(t, "claimed_by", "agent:codex@xps"))
        self.assertIn("agent:codex@xps is working on", self.error_text("playground_task_update", id=task["id"],
                                                                         action="claim"))
        listed = self.ok("playground_task_list")["items"]
        self.assertEqual([t["id"] for t in listed], [task["id"]])
        done = self.ok("playground_task_update", id=task["id"], status="done", outcome="Seats work on phones.")
        self.assertEqual(done["status"], "done")
        self.assertIn("## Outcome\nSeats work on phones.", self.store.read_task(task["id"]).body)
        self.ok("playground_channel_post", text="Changing how seats are saved", kind="start", files=["src/seats.ts"])
        self.assertEqual(self.ok("playground_channel_read")["items"][-1]["from"], "ari")
        self.assertIn("Changing how seats are saved", (self.root / ".playground" / "channel.md").read_text())

    def test_an_agents_rule_waits_for_a_person(self):
        self.ok("playground_knowledge_add", title="Never publish internal notes",
                body="Not to GitHub, not anywhere public.", kind="rule")
        note = self.store.read_knowledge("never-publish-internal-notes")
        self.assertEqual(note.extra["status"], "suggested")
        self.assertNotIn("Never publish internal notes", (self.root / "AGENTS.md").read_text())
        self.ok("playground_knowledge_add", title="Only parents offer rides", body="Safety.", kind="decision")
        self.assertNotIn("status", self.store.read_knowledge("only-parents-offer-rides").extra)


if __name__ == "__main__":
    unittest.main()
