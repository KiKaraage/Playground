# SPDX-License-Identifier: Apache-2.0
"""The whole agent program runs fenced in: it can change only the project and its
own sign-in folder, can't see the rest of the home folder, and can use the internet.
The real-fence tests run under Seatbelt on macOS and bubblewrap on Linux."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_agents_runner import FAKE, RunnerBase  # noqa: E402

from luma_playground.agents import sandbox  # noqa: E402
from luma_playground.agents.connections import Connection, ConnectionRuleError, LaunchError  # noqa: E402
from luma_playground.agents.runner import default_agent_factory  # noqa: E402
from luma_playground.core.model import Member  # noqa: E402
from luma_playground.core.store import ProjectStore  # noqa: E402

HAVE_FENCE = sandbox.backend(sandbox.SandboxPolicy(tempfile.gettempdir())) != "none"

PROBE = r'''
import json, os, sys
out = {"sandbox": os.environ.get("PLAYGROUND_AGENT_SANDBOX", "")}
def attempt(key, fn):
    try:
        out[key] = fn()
    except OSError as exc:
        out[key] = type(exc).__name__
home = os.environ["HOME"]
attempt("inside", lambda: open("inside.txt", "w").write("ok") and "written")
attempt("config", lambda: open(os.path.join(sys.argv[1], "session.json"), "w").write("{}") and "written")
attempt("tmp", lambda: open(os.path.join(os.environ["TMPDIR"], "t.txt"), "w").write("t") and "written")
attempt("outside", lambda: open(sys.argv[2], "w").write("x") and "written")
attempt("home_file_write", lambda: open(os.path.join(home, "planted.txt"), "w").write("x") and "written")
attempt("secret", lambda: open(os.path.join(home, "secret.txt")).read())
attempt("other_account", lambda: open(sys.argv[3]).read())
attempt("toolchain", lambda: open(os.path.join(home, ".nvm", "tool.txt")).read())
attempt("listing", lambda: sorted(os.listdir(home)))
print(json.dumps(out))
'''


@unittest.skipUnless(HAVE_FENCE, "no bubblewrap or Seatbelt on this computer")
class RealFenceTests(unittest.TestCase):
    """A fake home folder with a project, a secret, another account and a toolchain in it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.home = base / "home"
        self.project = self.home / "Documents" / "Playground Projects" / "Kiln"
        self.project.mkdir(parents=True)
        (self.home / "secret.txt").write_text("my secret")
        self.account = self.home / ".local" / "share" / "playground" / "accounts" / "codex-2"
        self.other = self.home / ".local" / "share" / "playground" / "accounts" / "claude-work"
        self.other.mkdir(parents=True)
        (self.other / "auth.json").write_text("someone else's sign-in")
        (self.home / ".nvm").mkdir()
        (self.home / ".nvm" / "tool.txt").write_text("node lives here")
        self.outside = base / "next-to-home.txt"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def probe(self, **kw) -> dict:
        conf = sandbox.confine_agent([sys.executable, "-c", PROBE, str(self.account), str(self.outside),
                                      str(self.other / "auth.json")],
                                     dict(os.environ), project_root=self.project, provider="codex",
                                     config_home=str(self.account), home=str(self.home), **kw)
        try:
            result = subprocess.run(conf.argv, env=conf.env, cwd=str(self.project), capture_output=True, text=True,
                                    timeout=60)
        finally:
            conf.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_the_fence_has_the_promised_shape(self):
        out = self.probe()
        self.assertIn(out["sandbox"], ("bwrap", "seatbelt"))
        self.assertEqual(out["inside"], "written")                 # the project: read/write
        self.assertEqual(out["config"], "written")                 # its own sign-in folder: read/write
        self.assertEqual(out["tmp"], "written")                    # a private temp folder
        # Anywhere else: refused (or, under bubblewrap, a write into /tmp lands in its private /tmp).
        if not (out["sandbox"] == "bwrap" and str(self.outside).startswith("/tmp/")):
            self.assertNotEqual(out["outside"], "written")
        self.assertFalse(self.outside.exists())
        if out["sandbox"] != "bwrap":   # bubblewrap gives the agent an empty, throwaway home instead
            self.assertNotEqual(out["home_file_write"], "written")
        self.assertFalse((self.home / "planted.txt").exists())
        self.assertNotEqual(out["secret"], "my secret")            # the rest of home is hidden
        self.assertNotEqual(out["other_account"], "someone else's sign-in")
        self.assertEqual(out["toolchain"], "node lives here")     # toolchains are readable
        if isinstance(out["listing"], list):                       # bubblewrap: an empty home with only what's let in
            self.assertNotIn("secret.txt", out["listing"])
        self.assertEqual((self.project / "inside.txt").read_text(), "ok")
        self.assertTrue((self.account / "session.json").exists())

    def test_the_network_stays_on(self):
        conf = sandbox.confine_agent(["true"], dict(os.environ), project_root=self.project, provider="codex",
                                     home=str(self.home))
        conf.cleanup()
        if conf.backend == "bwrap":
            self.assertNotIn("--unshare-net", conf.argv)
        else:
            self.assertNotIn("(deny network", " ".join(conf.argv))


class PolicyTests(unittest.TestCase):
    def test_default_is_on_for_shared_projects_only(self):
        self.assertTrue(sandbox.agent_sandbox_wanted("", shared_project=True))
        self.assertFalse(sandbox.agent_sandbox_wanted("", shared_project=False))
        self.assertTrue(sandbox.agent_sandbox_wanted("on", shared_project=False))
        self.assertFalse(sandbox.agent_sandbox_wanted("off", shared_project=True))

    def test_connection_setting(self):
        conn = Connection(id="codex", label="Codex", kind="personal-plan", provider="codex", owner="nick")
        self.assertTrue(conn.sandbox_recommended)
        self.assertTrue(conn.sandbox_for(shared_project=True))
        conn.sandbox = "on"
        conn.validate()
        self.assertEqual(Connection.from_dict(conn.to_dict()).sandbox, "on")
        conn.sandbox = "sometimes"
        with self.assertRaises(ConnectionRuleError):
            conn.validate()
        ari = Connection(id="ari", label="Ari", kind="ari", provider="ari", endpoint="ollama", owner="nick")
        self.assertFalse(ari.sandbox_for(shared_project=True))   # Ari's commands are fenced in already

    def test_project_is_shared_reads_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".playground").mkdir()
            toml = Path(tmp, ".playground", "project.toml")
            toml.write_text('[project]\nname = "x"\n\n[[members]]\nid = "nick"\nname = "Nick"\nrole = "owner"\n')
            self.assertFalse(sandbox.project_is_shared(tmp))
            toml.write_text(toml.read_text() + '\n[[members]]\nid = "priya"\nname = "Priya"\nrole = "builder"\n')
            self.assertTrue(sandbox.project_is_shared(tmp))
        self.assertFalse(sandbox.project_is_shared("/nowhere"))

    def test_npx_resolves_to_the_cached_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp, "abc123")
            pkg = folder / "node_modules" / "@agentclientprotocol" / "codex-acp"
            (pkg / "dist").mkdir(parents=True)
            (pkg / "dist" / "index.js").write_text("")
            (pkg / "package.json").write_text(json.dumps({"bin": {"codex-acp": "dist/index.js"}}))
            (folder / "package.json").write_text(json.dumps({"_npx": {"packages": ["@agentclientprotocol/codex-acp"]}}))
            with mock.patch("shutil.which", return_value="/usr/bin/node"):
                argv, adapter = sandbox.resolve_npx(["npx", "-y", "@agentclientprotocol/codex-acp", "--x"], cache=tmp)
            self.assertEqual(argv, ["/usr/bin/node", os.path.realpath(pkg / "dist" / "index.js"), "--x"])
            self.assertEqual(adapter, str(folder))
            argv, adapter = sandbox.resolve_npx(["npx", "-y", "@other/agent"], cache=tmp)
            self.assertEqual((argv, adapter), (["npx", "-y", "@other/agent"], None))

    def test_no_fence_on_this_computer(self):
        with mock.patch.object(sandbox, "backend", return_value="none"):
            with self.assertRaises(sandbox.SandboxUnavailable):
                sandbox.confine_agent(["x"], {}, project_root=tempfile.gettempdir())
            conf = sandbox.confine_agent(["x"], {}, project_root=tempfile.gettempdir(), required=False)
            self.assertFalse(conf.confined)
            self.assertEqual(conf.argv, ["x"])


class RunnerFenceTests(RunnerBase):
    """The runner's own factory fences the agent program in, by the connection's setting."""

    def setUp(self) -> None:
        super().setUp()
        self.state = Path(self.root, ".playground", "local", "fake-state")   # inside: the fake logs there
        self.state.mkdir(parents=True)
        os.environ["FAKE_ACP_STATE"] = str(self.state)
        self.addCleanup(os.environ.pop, "FAKE_ACP_STATE", None)

    def connection(self, setting: str) -> Connection:
        return Connection(id="fake", label="Fake", kind="local", provider="fake", owner="nick",
                          command=[sys.executable, FAKE], sandbox=setting)

    def factory(self, connection, root, handlers, **kw):
        kw.pop("registry", None)
        return default_agent_factory(connection, root, handlers, **kw)

    def escape_results(self) -> dict:
        return next(e["escape"] for e in self.calls() if "escape" in e)

    @unittest.skipUnless(HAVE_FENCE, "no bubblewrap or Seatbelt on this computer")
    def test_a_fenced_agent_cant_write_outside_the_project(self):
        runner = self.runner(self.connection("on"))
        runner.send("escape")
        self.assertTrue(runner.wait_idle(60))
        out = self.escape_results()
        self.assertIn(out["sandbox"], ("bwrap", "seatbelt"))
        self.assertEqual(out["inside_write"], "written")
        if not (out["sandbox"] == "bwrap" and self.root.startswith("/tmp/")):
            self.assertNotEqual(out["outside_write"], "written")   # bubblewrap: /tmp is the agent's own
        self.assertFalse(Path(self.root).parent.joinpath("escaped-by-agent.txt").exists())
        self.assertIn(runner.sandbox_backend, ("bwrap", "seatbelt"))
        self.assertTrue(any(s.get("sandbox") == runner.sandbox_backend for s in self.states))
        # Sign-in runs the agent's own login without the fence.
        argv, _env = runner.sign_in_command("terminal-login")
        self.assertEqual(argv[:2], [sys.executable, FAKE])

    @unittest.skipUnless(HAVE_FENCE, "no bubblewrap or Seatbelt on this computer")
    def test_a_full_turn_still_works_fenced_in(self):
        runner = self.runner(self.connection("on"))
        runner.send("Create hello.txt containing hi")
        self.assertTrue(runner.wait_idle(60))
        self.assertEqual(Path(self.root, "hello.txt").read_text(), "hi")
        self.assertEqual(self.thread()[-1].text, "Done. hello.txt says hi.")

    def test_solo_project_runs_unfenced_by_default(self):
        runner = self.runner(self.connection(""))
        runner.send("escape")
        self.assertTrue(runner.wait_idle(60))
        self.assertEqual(self.escape_results()["sandbox"], "")
        Path(self.root).parent.joinpath("escaped-by-agent.txt").unlink(missing_ok=True)

    def test_shared_project_refuses_to_start_unfenced(self):
        store = ProjectStore(self.root, bus=self.bus)
        project = store.read_project()
        project.members.append(Member(id="priya", name="Priya", role="builder"))
        store.write_project(project)
        with mock.patch.object(sandbox, "backend", return_value="none"):
            runner = self.runner(self.connection(""))
            runner.send("escape")
            self.assertTrue(runner.wait_idle(60))
        card = self.thread()[-1]
        self.assertIn("shared with other people", card.text)
        self.assertIn("Keep it inside the project", card.text)
        self.assertFalse(any("escape" in e for e in self.calls()))


if __name__ == "__main__":
    unittest.main()
