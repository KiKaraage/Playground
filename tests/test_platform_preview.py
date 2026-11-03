# SPDX-License-Identifier: Apache-2.0
"""Detecting how to run a project, and really running it."""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
import urllib.request

from test_platform_support import FAKE_ADB, PlatformTestCase

from luma_playground.core import events, places, preview
from luma_playground.core.preview import Preview, RunPlan


class DetectionTests(PlatformTestCase):
    def test_nothing_to_run(self):
        self.assertIsNone(preview.detect(self.project_dir))

    def test_run_toml_comes_first(self):
        self.write("package.json", '{"scripts": {"dev": "vite"}}')
        self.write(".playground/run.toml", """
            [app]
            command = "make serve PORT={port}"
            port = 0
            url_path = "/app/"
            """)
        plan = preview.detect(self.project_dir)
        self.assertEqual(plan.source, "run.toml")
        self.assertEqual(plan.argv(4321), ["/bin/sh", "-c", "make serve PORT=4321"])
        self.assertEqual(plan.url_path, "/app/")
        self.assertFalse(plan.port_from_output)

    def test_run_toml_without_a_command_says_so(self):
        self.write(".playground/run.toml", "[app]\nport = 3000\n")
        self.assertIn("needs a command", preview.detect(self.project_dir).problem)

    def test_npm_dev_then_start_with_framework_ports(self):
        self.write("package.json", json.dumps({"scripts": {"dev": "vite --open", "start": "node server.js"},
                                               "devDependencies": {"vite": "^7"}}))
        (self.project_dir / "node_modules").mkdir()
        plan = preview.detect(self.project_dir)
        self.assertEqual(plan.source, "npm")
        self.assertEqual(plan.label, "the Vite dev server")
        self.assertEqual(plan.argv(5173), ["npm", "run", "dev", "--", "--port", "5173", "--strictPort",
                                           "--host", "127.0.0.1"])
        self.assertTrue(plan.port_from_output)

    def test_npm_start_unknown_framework_uses_env_port_and_output(self):
        self.write("package.json", json.dumps({"scripts": {"start": "node server.js"}}))
        self.write("pnpm-lock.yaml", "")
        plan = preview.detect(self.project_dir)
        self.assertEqual(plan.argv(3000), ["pnpm", "run", "start"])
        self.assertTrue(plan.port_from_output)

    def test_next_with_yarn(self):
        self.write("package.json", json.dumps({"scripts": {"dev": "next dev"}}))
        self.write("yarn.lock", "")
        self.assertEqual(preview.detect(self.project_dir).argv(3001), ["yarn", "run", "dev", "-p", "3001"])

    def test_missing_packages_is_a_plain_problem(self):
        self.write("package.json", json.dumps({"scripts": {"dev": "vite"}, "dependencies": {"react": "19"}}))
        problem = preview.detect(self.project_dir).problem
        self.assertTrue("install" in problem or "Node.js" in problem, problem)

    def test_python_frameworks(self):
        self.write("manage.py", "import django\n")
        plan = preview.detect(self.project_dir)
        self.assertEqual(plan.source, "django")
        self.assertEqual(plan.argv(8000)[-2:], ["runserver", "127.0.0.1:8000"])
        (self.project_dir / "manage.py").unlink()

        self.write("app/main.py", "from fastapi import FastAPI\napi = FastAPI(title='x')\n")
        plan = preview.detect(self.project_dir)
        self.assertEqual(plan.source, "fastapi")
        self.assertIn("app.main:api", plan.argv(8000))
        (self.project_dir / "app" / "main.py").unlink()

        self.write("app.py", "from flask import Flask\napp = Flask(__name__)\n")
        plan = preview.detect(self.project_dir)
        self.assertEqual(plan.source, "flask")
        self.assertEqual(plan.argv(5000)[2:5], ["flask", "--app", "app:app"])

    def test_static_index_in_root_or_public(self):
        self.write("public/index.html", "<h1>hi</h1>")
        plan = preview.detect(self.project_dir)
        self.assertEqual(plan.source, "static")
        self.assertIn(str((self.project_dir / "public").resolve()), plan.argv(1))
        self.write("index.html", "<h1>root</h1>")
        self.assertIn(str(self.project_dir.resolve()), preview.detect(self.project_dir).argv(1))

    def test_gtk_app_is_a_desktop_target(self):
        self.write("main.py", "import gi\ngi.require_version('Gtk', '4.0')\nfrom gi.repository import Gtk\n")
        plan = preview.detect(self.project_dir)
        self.assertEqual(plan.target, "desktop-app")


class RunningTests(PlatformTestCase):
    def test_static_server_really_runs_and_stops(self):
        self.write("index.html", "<h1>Kiln waitlist</h1>")
        p = Preview(self.project_dir, bus=self.bus).start(wait=True, timeout=30)
        self.assertEqual(p.state, "running", p.error)
        self.assertTrue(p.url.startswith("http://127.0.0.1:"))
        with urllib.request.urlopen(p.url, timeout=5) as response:
            self.assertIn(b"Kiln waitlist", response.read())
        states = [payload["state"] for payload in self.topics(events.PREVIEW_STATE)]
        self.assertEqual(states[:2], ["starting", "running"])
        saved = json.loads((self.project_dir / ".playground" / "local" / "preview.json").read_text())
        self.assertEqual((saved["state"], saved["url"]), ("running", p.url))
        self.assertTrue((self.project_dir / ".playground" / "local" / "preview.log").exists())
        url = p.url
        p.stop()
        self.assertEqual(p.state, "stopped")
        self.assertIsNone(preview.answers(url, timeout=1))
        self.assertEqual(self.topics(events.PREVIEW_STATE)[-1]["state"], "stopped")

    def test_failure_has_a_reason_and_a_log_tail(self):
        self.write(".playground/run.toml", '[app]\ncommand = "echo starting up; echo boom >&2; exit 3"\n')
        p = Preview(self.project_dir, bus=self.bus).start(wait=True, timeout=20)
        self.assertEqual(p.state, "failed")
        self.assertIn("exited with code 3", p.error)
        last = self.topics(events.PREVIEW_STATE)[-1]
        self.assertEqual(last["state"], "failed")
        self.assertIn("boom", last["log_tail"])

    def test_known_failures_in_plain_words(self):
        self.write(".playground/run.toml",
                   '[app]\ncommand = "echo \\"Error: listen EADDRINUSE: address already in use\\"; exit 1"\n')
        p = Preview(self.project_dir, bus=self.bus).start(wait=True, timeout=20)
        self.assertIn("already using that port", p.error)

    def test_not_answering_times_out(self):
        self.write(".playground/run.toml", '[app]\ncommand = "sleep 30"\nready_timeout_s = 1.5\n')
        p = Preview(self.project_dir, bus=self.bus).start(wait=True, timeout=20)
        self.assertEqual(p.state, "failed")
        self.assertIn("didn't answer within 2 seconds", p.error)

    def test_port_read_from_the_server_output(self):
        self.write("index.html", "found by output")
        # The server picks its own port (0) and only says which in its output.
        self.write("serve.py", preview.STATIC_SERVER)
        self.write(".playground/run.toml", f'[app]\ncommand = "exec {sys.executable} serve.py 0 ."\n')
        p = Preview(self.project_dir, bus=self.bus).start(wait=True, timeout=30)
        self.assertEqual(p.state, "running", p.error)
        self.assertNotEqual(p.url, f"http://127.0.0.1:{p.port}/")
        p.stop()

    def test_app_gets_no_credentials(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-should-not-leak"
        os.environ["GITHUB_TOKEN"] = "ghp-should-not-leak"
        os.environ["SSH_AUTH_SOCK"] = "/tmp/agent.sock"
        try:
            self.write("index.html", "env")
            self.write("serve.py", preview.STATIC_SERVER)
            self.write(".playground/run.toml",
                       f'[app]\ncommand = "env > env.txt; exec {sys.executable} serve.py {{port}} ."\n'
                       '[app.env]\nAPP_MODE = "stage"\n')
            p = Preview(self.project_dir, bus=self.bus).start(wait=True, timeout=30)
            self.assertEqual(p.state, "running", p.error)
            env_text = (self.project_dir / "env.txt").read_text()
            self.assertIn(f"PORT={p.port}", env_text)
            self.assertIn("APP_MODE=stage", env_text)
            for leaked in ("sk-ant-should-not-leak", "ghp-should-not-leak", "SSH_AUTH_SOCK"):
                self.assertNotIn(leaked, env_text)
            p.stop()
        finally:
            for key in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "SSH_AUTH_SOCK"):
                os.environ.pop(key, None)

    def test_cwd_cannot_leave_the_project(self):
        self.write(".playground/run.toml", '[app]\ncommand = "true"\ncwd = "../.."\n')
        p = Preview(self.project_dir, bus=self.bus).start(wait=True, timeout=10)
        self.assertEqual(p.state, "failed")
        self.assertIn("outside the project", p.error)

    def test_nothing_to_run_fails_kindly(self):
        p = Preview(self.project_dir, bus=self.bus).start(wait=True, timeout=5)
        self.assertEqual(p.state, "failed")
        self.assertIn("couldn't find an app to run", p.error)

    def test_one_run_per_project_and_stage_and_live(self):
        self.write("index.html", "stage")
        places.add_place(self.project_dir, "Live", role="production", host="prod.example", user="deploy",
                         url="https://kiln.example", test=False)
        first = preview.start_preview(self.project_dir, bus=self.bus, wait=True)
        second = preview.start_preview(self.project_dir, bus=self.bus, wait=True)
        self.assertEqual(first.state, "stopped")
        self.assertEqual(second.state, "running")
        both = preview.stage_and_live(self.project_dir)
        self.assertEqual(both["stage"]["state"], "running")
        self.assertEqual(both["live"]["url"], "https://kiln.example")
        self.assertTrue(preview.stop_preview(self.project_dir))
        self.assertFalse(preview.stop_preview(self.project_dir))


class RemoteTests(PlatformTestCase):
    def setUp(self):
        super().setUp()
        self.fake_ssh()
        served = self.tmp / "served"
        served.mkdir()
        (served / "index.html").write_text("from the dev server")
        os.environ["FAKE_SSH_SERVE"] = str(served)
        self.write("index.html", "local copy")

    def test_runs_on_the_dev_server_through_a_tunnel(self):
        dev = places.add_place(self.project_dir, "Dev", host="dev.example", user="deploy", path="/srv/my kiln",
                               test=False)["place"]
        p = preview.start_preview(self.project_dir, place=dev, project_id="kiln-1", bus=self.bus, wait=True)
        self.assertEqual(p.state, "running", p.error)
        with urllib.request.urlopen(p.url, timeout=5) as response:
            self.assertIn(b"from the dev server", response.read())
        call = self.ssh_calls()[-1]
        forward = call[call.index("-L") + 1]
        self.assertEqual(forward, f"{p.port}:127.0.0.1:{p.remote_port}")
        self.assertIn("ExitOnForwardFailure=yes", call)
        self.assertIn("-tt", call)
        self.assertEqual(call[call.index("--") + 1], "deploy@dev.example")
        remote = call[-1]
        self.assertTrue(remote.startswith("cd '/srv/my kiln' && "), remote)
        self.assertIn(f"PORT={p.remote_port}", remote)
        self.assertEqual(self.topics(events.PREVIEW_STATE)[-1]["where"], dev["id"])
        p.stop()

    def test_never_runs_on_production(self):
        prod = places.add_place(self.project_dir, "Live", role="production", host="prod.example", user="deploy",
                                path="/srv/kiln", test=False)["place"]
        p = preview.start_preview(self.project_dir, place=prod, bus=self.bus, wait=True)
        self.assertEqual(p.state, "failed")
        self.assertIn("live app", p.error)
        self.assertEqual(self.ssh_calls(), [])

    def test_needs_the_server_folder(self):
        dev = places.add_place(self.project_dir, "Dev", host="dev.example", user="deploy", test=False)["place"]
        p = preview.start_preview(self.project_dir, place=dev, bus=self.bus, wait=True)
        self.assertEqual(p.state, "failed")
        self.assertIn("where the project folder is", p.error)


class TargetTests(PlatformTestCase):
    def test_targets_report_availability_in_plain_words(self):
        found = {t["id"]: t for t in preview.targets()}
        self.assertEqual(set(found), {"web", "android", "desktop-app"})
        self.assertTrue(found["web"]["available"])
        for target in found.values():
            self.assertTrue(target["message"])

    def test_android_device_through_adb(self):
        self.install_fake("adb", FAKE_ADB)
        status = preview.android_status()
        self.assertTrue(status["available"])
        self.assertEqual(status["devices"], ["emulator-5554"])
        self.assertIn("emulator-5554", status["message"])

    @unittest.skipUnless(shutil.which("gtk4-broadwayd"), "gtk4-broadwayd isn't installed (it is in the VM)")
    def test_desktop_app_streams_through_broadway(self):
        plan = RunPlan("run.toml", "sleep 30", "the app", target="desktop-app", ready_timeout_s=15)
        p = Preview(self.project_dir, plan, bus=self.bus, stream="broadway").start(wait=True, timeout=20)
        try:
            self.assertEqual(p.state, "running", p.error)
            self.assertIsNotNone(preview.answers(p.url))
        finally:
            p.stop()

    def test_native_desktop_launch_runs_without_a_url(self):
        plan = RunPlan("run.toml", "sleep 30", "the app", target="desktop-app")
        p = Preview(self.project_dir, plan, bus=self.bus, stream="native").start(wait=True, timeout=10)
        self.assertEqual((p.state, p.url), ("running", ""))
        p.stop()
        self.assertEqual(p.state, "stopped")


if __name__ == "__main__":
    unittest.main()


class FollowsChangesTests(unittest.TestCase):
    def test_what_keeps_up_by_itself_and_what_restarts(self):
        from luma_playground.core.preview import RunPlan, follows_changes
        self.assertEqual(follows_changes(RunPlan("gtk", ["python3", "main.py"], "the app", target="desktop-app")), "restart")
        self.assertEqual(follows_changes(RunPlan("static", ["python3", "-m", "http.server"], "the site")), "reload")
        self.assertEqual(follows_changes(RunPlan("npm", ["npm", "run", "dev"], "the Vite dev server")), "")
        self.assertEqual(follows_changes(RunPlan("run.toml", "python3 server.py --port {port}", "the app")), "restart")
        self.assertEqual(follows_changes(RunPlan("run.toml", "npm run dev -- --port {port}", "the app")), "")
        self.assertEqual(follows_changes(RunPlan("run.toml", "", "the screen", target="screen")), "")


class DeviceEnvTests(unittest.TestCase):
    def test_a_phone_size_says_what_a_luma_phone_says(self):
        from luma_playground.core.preview import device_env
        phone = device_env("phone")
        self.assertEqual((phone["LUMA_DEVICE_CLASS"], phone["LUMA_FORM_FACTOR"]), ("handheld", "phone"))
        # The values Luma's kit accepts (luma_appkit.context, ui/luma-context.c); anything else is ignored there.
        self.assertEqual(phone["LUMA_PRESENTATION_MODE"], "fullscreen-mobile")
        self.assertEqual(device_env("tablet")["LUMA_PRESENTATION_MODE"], "windowed")
        self.assertEqual(device_env("app")["LUMA_DEVICE_CLASS"], "desktop")
        self.assertEqual(device_env(""), {})

    def test_a_run_says_which_size_it_was_started_for(self):
        import tempfile
        from luma_playground.core.preview import Preview
        with tempfile.TemporaryDirectory() as folder:
            run = Preview(folder, extra_env={"PLAYGROUND_PREVIEW_DEVICE": "phone"})
            self.assertEqual(run.status()["device"], "phone")
