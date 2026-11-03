# SPDX-License-Identifier: Apache-2.0
"""Service usage adapters, with fake providers (HTTP servers and CLIs). No network."""
from __future__ import annotations

from datetime import datetime, timezone
import http.server
import json
import socketserver
import sys
import textwrap
import threading
import unittest
from unittest import mock

from test_platform_support import CRYPTO_SKIP, HAVE_CRYPTO, PlatformTestCase, quick_http_server

from luma_playground.core import events, services, usage

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
TOKEN = "cf-token-Zx81-very-secret"
ACCOUNT = "0123456789abcdef0123456789abcdef"
RESEND_KEY = "re_live_key_ab12cd34"
DB_PASSWORD = "pg-p@ss/w0rd-secret"

STACK = """
[[services]]
id = "r2"
name = "Studio photos"
kind = "storage"
provider = "Cloudflare R2"
usage_source = "cloudflare"
secrets = ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"]
monthly_budget = 5

[[services]]
id = "email"
name = "Emails"
kind = "email"
provider = "Resend"
usage_source = "resend"
secrets = ["RESEND_API_KEY"]
plan = "Pro"
monthly_cost = 20
monthly_budget = 20

[[services]]
id = "aws"
name = "Production server"
kind = "hosting"
provider = "AWS"
usage_source = "aws"
secrets = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]
monthly_budget = 15

[[services]]
id = "db"
name = "Bookings database"
kind = "database"
provider = "PostgreSQL"
usage_source = "postgres"
secrets = ["DATABASE_URL"]

[[services]]
id = "site"
name = "Website"
kind = "hosting"
provider = "Kiln"
usage_source = "http"

[[services]]
id = "texts"
name = "Reminder texts"
kind = "messaging"
provider = "Twilio"
usage_source = "manual"
monthly_budget = 10
"""

SECRETS = {"CLOUDFLARE_API_TOKEN": TOKEN, "CLOUDFLARE_ACCOUNT_ID": ACCOUNT, "RESEND_API_KEY": RESEND_KEY,
           "AWS_ACCESS_KEY_ID": "AKIAFAKEKEYID000", "AWS_SECRET_ACCESS_KEY": "aws-secret-Qq99-dont-leak",
           "DATABASE_URL": f"postgresql://kiln:{DB_PASSWORD.replace('@', '%40').replace('/', '%2F')}@db.internal:5433/kiln?sslmode=require"}


class FakeProvider:
    """A tiny HTTP server: routes return (status, json, headers); every request is recorded."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                       "body": body})
                route = outer.routes.get(self.path.split("?")[0]) or outer.routes.get("*")
                status, payload, headers = route(self.path, body) if callable(route) else route
                data = json.dumps(payload).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _serve

            def log_message(self, *args):
                pass

        class Server(http.server.ThreadingHTTPServer):
            def server_bind(self):
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = self.server_address[:2]

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


FAKE_AWS = r'''#!PYTHON
import json, os, sys
with open("LOGPATH", "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "has_key": "AWS_SECRET_ACCESS_KEY" in os.environ,
                          "home_config": os.environ.get("AWS_CONFIG_FILE")}) + "\n")
if os.environ.get("AWS_ACCESS_KEY_ID") != "AKIAFAKEKEYID000":
    sys.stderr.write("An error occurred (UnrecognizedClientException)\n"); sys.exit(254)
print(json.dumps({"ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": "12.3456", "Unit": "USD"}}}]}))
'''

FAKE_PSQL = r'''#!PYTHON
import json, os, sys
with open("LOGPATH", "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "env": {k: v for k, v in os.environ.items() if k.startswith("PG")}}) + "\n")
if os.environ.get("PGPASSWORD") != "pg-p@ss/w0rd-secret":
    sys.stderr.write(f'psql: error: password authentication failed for user "kiln" (tried {os.environ.get("PGPASSWORD")})\n'); sys.exit(2)
print("123456789|3")
'''


class ServicesTestCase(PlatformTestCase):
    def setUp(self):
        super().setUp()
        self.write(".playground/stack.toml", STACK)
        self.secrets = dict(SECRETS)
        self.providers = []
        self.log = self.tmp / "fake-cli.jsonl"

    def tearDown(self):
        for provider in self.providers:
            provider.close()
        super().tearDown()

    def lookup(self, name):
        return self.secrets.get(name)

    def provider(self, routes) -> FakeProvider:
        provider = FakeProvider(routes)
        self.providers.append(provider)
        return provider

    def service(self, sid):
        return services.get_service(self.project_dir, sid)

    def cli_calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def assert_no_secrets_in_project(self):
        for path in self.all_files(self.project_dir):
            blob = path.read_bytes()
            for value in SECRETS.values():
                self.assertNotIn(value.encode(), blob, f"credential in {path}")
            self.assertNotIn(DB_PASSWORD.encode(), blob)


class StackTests(ServicesTestCase):
    def test_reads_services(self):
        ids = [s["id"] for s in services.list_services(self.project_dir)]
        self.assertEqual(ids, ["r2", "email", "aws", "db", "site", "texts"])
        self.assertIsNone(services.get_service(self.project_dir, "nope"))

    def test_no_stack_no_services(self):
        (self.project_dir / ".playground" / "stack.toml").unlink()
        self.assertEqual(services.list_services(self.project_dir), [])
        self.assertEqual(services.money_summary(self.project_dir, NOW)["services"], [])


class CloudflareTests(ServicesTestCase):
    def graphql_reply(self, path, body):
        query = json.loads(body)
        self.assertEqual(query["variables"]["accountTag"], ACCOUNT)
        self.assertEqual(query["variables"]["start"], "2026-10-01T00:00:00Z")
        self.assertIn("r2StorageAdaptiveGroups", query["query"])
        self.assertIn("r2OperationsAdaptiveGroups", query["query"])
        return 200, {"data": {"viewer": {"accounts": [{
            "storage": [
                {"max": {"payloadSize": 14_000_000_000, "metadataSize": 0, "objectCount": 900},
                 "dimensions": {"bucketName": "photos", "datetime": "2026-10-09T00:00:00Z"}},
                {"max": {"payloadSize": 13_000_000_000, "metadataSize": 0, "objectCount": 850},
                 "dimensions": {"bucketName": "photos", "datetime": "2026-10-08T00:00:00Z"}},
                {"max": {"payloadSize": 1_000_000_000, "metadataSize": 0, "objectCount": 100},
                 "dimensions": {"bucketName": "backups", "datetime": "2026-10-09T00:00:00Z"}},
            ],
            "operations": [
                {"sum": {"requests": 1_500_000}, "dimensions": {"actionType": "PutObject"}},
                {"sum": {"requests": 500_000}, "dimensions": {"actionType": "ListObjects"}},
                {"sum": {"requests": 12_000_000}, "dimensions": {"actionType": "GetObject"}},
                {"sum": {"requests": 999}, "dimensions": {"actionType": "DeleteObject"}},
            ]}]}}, "errors": None}, None

    def test_r2_storage_and_operations(self):
        fake = self.provider({"/graphql": self.graphql_reply})
        with mock.patch.object(services, "CLOUDFLARE_GRAPHQL", fake.url + "/graphql"):
            readings = services.refresh_now(self.project_dir, "r2", lookup=self.lookup, now=NOW, bus=self.bus)
        reading = readings[0]
        self.assertTrue(reading.ok, reading.status)
        self.assertEqual(reading.metrics["storage_gb"], 15.0)
        self.assertEqual(reading.metrics["objects"], 1000)
        self.assertEqual(reading.metrics["class_a_ops"], 2_000_000)
        self.assertEqual(reading.metrics["class_b_ops"], 12_000_000)
        # 5 GB over free x 0.015 + 1M class A over x 4.50 + 2M class B over x 0.36
        self.assertAlmostEqual(reading.cost, round(5 * 0.015 + 4.50 + 2 * 0.36, 2))
        self.assertEqual(fake.requests[0]["headers"]["Authorization"], f"Bearer {TOKEN}")
        snap = services.latest(self.project_dir, "r2")
        self.assertEqual(set(snap), {"ts", "service_id", "period", "metrics", "cost", "currency", "source"})
        self.assertEqual(snap["metrics"]["cost_estimated"], 1)
        self.assertEqual(snap["period"], "2026-10")
        self.assertTrue(self.topics(events.USAGE_CHANGED))
        self.assert_no_secrets_in_project()

    def test_missing_token_says_where_to_add_it(self):
        del self.secrets["CLOUDFLARE_API_TOKEN"]
        reading = services.fetch(self.service("r2"), self.lookup, NOW)
        self.assertFalse(reading.ok)
        self.assertEqual(reading.status, "Add the Cloudflare R2 token in Places → Vault to see live usage.")
        services.refresh_now(self.project_dir, "r2", lookup=self.lookup, now=NOW, bus=self.bus)
        self.assertIsNone(services.latest(self.project_dir, "r2"))
        status = json.loads((self.project_dir / ".playground/local/services-status.json").read_text())
        self.assertFalse(status["r2"]["ok"])

    def test_rejected_token_and_graphql_errors(self):
        fake = self.provider({"/graphql": (403, {"errors": [{"message": "forbidden"}]}, None)})
        with mock.patch.object(services, "CLOUDFLARE_GRAPHQL", fake.url + "/graphql"):
            self.assertIn("didn't accept the key", services.fetch(self.service("r2"), self.lookup, NOW).status)
        fake2 = self.provider({"/graphql": (200, {"data": None, "errors": [{"message": "not authorized"}]}, None)})
        with mock.patch.object(services, "CLOUDFLARE_GRAPHQL", fake2.url + "/graphql"):
            self.assertIn("Account Analytics", services.fetch(self.service("r2"), self.lookup, NOW).status)

    def test_redirects_never_carry_the_token(self):
        elsewhere = self.provider({"*": (200, {"data": {}}, None)})
        bouncer = self.provider({"/graphql": (302, {}, {"Location": elsewhere.url + "/steal"})})
        with mock.patch.object(services, "CLOUDFLARE_GRAPHQL", bouncer.url + "/graphql"):
            reading = services.fetch(self.service("r2"), self.lookup, NOW)
        self.assertFalse(reading.ok)
        self.assertIn("keep the key safe", reading.status)
        self.assertEqual(elsewhere.requests, [])

    def test_plain_http_endpoints_are_refused(self):
        with mock.patch.object(services, "CLOUDFLARE_GRAPHQL", "http://api.example.com/graphql"):
            reading = services.fetch(self.service("r2"), self.lookup, NOW)
        self.assertIn("secure connection", reading.status)

    def test_stack_toml_cannot_redirect_the_token(self):
        self.write(".playground/stack.toml", STACK.replace('usage_source = "cloudflare"',
                                                          'usage_source = "cloudflare"\napi_base = "https://evil.example"'))
        fake = self.provider({"/graphql": self.graphql_reply})
        with mock.patch.object(services, "CLOUDFLARE_GRAPHQL", fake.url + "/graphql"):
            self.assertTrue(services.fetch(self.service("r2"), self.lookup, NOW).ok)
        self.assertEqual(len(fake.requests), 1)


class ResendTests(ServicesTestCase):
    def test_counts_this_months_emails_across_pages(self):
        def emails(path, body):
            if "after=" not in path:
                rows = [{"id": f"e{i}", "created_at": "2026-10-08 10:00:00.123456+00", "last_event": "delivered"}
                        for i in range(100)]
                rows[3]["last_event"] = "bounced"
                return 200, {"object": "list", "has_more": True, "data": rows}, None
            rows = [{"id": "x1", "created_at": "2026-10-01T00:00:01Z", "last_event": "delivered"},
                    {"id": "x2", "created_at": "2026-09-30T23:59:59Z", "last_event": "delivered"},
                    {"id": "x3", "created_at": "2026-09-29T10:00:00Z", "last_event": "delivered"}]
            return 200, {"object": "list", "has_more": True, "data": rows}, None

        fake = self.provider({"/emails": emails})
        with mock.patch.object(services, "RESEND_API", fake.url):
            reading = services.fetch(self.service("email"), self.lookup, NOW)
        self.assertTrue(reading.ok, reading.status)
        self.assertEqual(reading.metrics["emails_sent"], 101)
        self.assertEqual(reading.metrics["emails_bounced"], 1)
        self.assertEqual(reading.status, "101 emails sent this month · 1 bounced or failed.")
        self.assertEqual(reading.cost, 20.0)      # the plan's fixed monthly_cost
        self.assertEqual(len(fake.requests), 2)
        self.assertIn("after=e99", fake.requests[1]["path"])
        self.assertEqual(fake.requests[0]["headers"]["Authorization"], f"Bearer {RESEND_KEY}")
        self.assertEqual({r["method"] for r in fake.requests}, {"GET"})


class CliTests(ServicesTestCase):
    def setUp(self):
        super().setUp()
        # The adapters give these programs a scrubbed environment, so the log path is baked in.
        for name, source in (("aws", FAKE_AWS), ("psql", FAKE_PSQL)):
            self.install_fake(name, source.replace("PYTHON", sys.executable, 1).replace("LOGPATH", str(self.log)))

    def test_aws_cost_explorer_month_to_date(self):
        reading = services.fetch(self.service("aws"), self.lookup, NOW)
        self.assertTrue(reading.ok, reading.status)
        self.assertEqual(reading.cost, 12.35)
        self.assertEqual(reading.status, "$12.35 so far this month.")
        call = self.cli_calls()[-1]
        self.assertEqual(call["argv"][:2], ["ce", "get-cost-and-usage"])
        self.assertIn("Start=2026-10-01,End=2026-10-10", call["argv"])
        self.assertTrue(call["has_key"])
        self.assertNotIn("aws-secret-Qq99-dont-leak", json.dumps(call["argv"]))

    def test_aws_bad_keys_and_missing_cli(self):
        self.secrets["AWS_ACCESS_KEY_ID"] = "AKIAWRONG"
        self.assertIn("didn't accept those keys", services.fetch(self.service("aws"), self.lookup, NOW).status)
        del self.secrets["AWS_SECRET_ACCESS_KEY"]
        self.assertIn("Places → Vault", services.fetch(self.service("aws"), self.lookup, NOW).status)
        self.secrets.update(SECRETS)     # keys present, but no aws program
        with mock.patch.object(services.shutil, "which", return_value=None):
            self.assertIn("install the AWS command line tool",
                          services.fetch(self.service("aws"), self.lookup, NOW).status)

    def test_postgres_size_read_only_with_password_in_env_only(self):
        reading = services.fetch(self.service("db"), self.lookup, NOW)
        self.assertTrue(reading.ok, reading.status)
        self.assertEqual(reading.metrics, {"size_bytes": 123456789, "size_gb": 0.123, "connections": 3})
        self.assertEqual(reading.status, "123.5 MB of data · 3 connected now.")
        call = self.cli_calls()[-1]
        self.assertNotIn(DB_PASSWORD, json.dumps(call["argv"]))
        self.assertNotIn("postgresql://", json.dumps(call["argv"]))
        env = call["env"]
        self.assertEqual((env["PGHOST"], env["PGPORT"], env["PGUSER"], env["PGDATABASE"], env["PGSSLMODE"]),
                         ("db.internal", "5433", "kiln", "kiln", "require"))
        self.assertIn("default_transaction_read_only=on", env["PGOPTIONS"])
        self.assertIn("pg_database_size", " ".join(call["argv"]))

    def test_postgres_errors_never_echo_the_password(self):
        wrong = "wrong-password-shown-in-error"
        self.secrets["DATABASE_URL"] = f"postgres://kiln:{wrong}@db.internal/kiln"
        reading = services.fetch(self.service("db"), self.lookup, NOW)
        self.assertFalse(reading.ok)
        self.assertIn("didn't accept that password", reading.status)
        self.assertNotIn(wrong, reading.status)
        self.assertEqual(services.redact(f"x {wrong} y", [wrong]), "x ••• y")


class HttpAndManualTests(ServicesTestCase):
    def test_http_up_and_down(self):
        site = self.tmp / "site"
        site.mkdir()
        (site / "index.html").write_text("ok")
        server = quick_http_server(site)
        try:
            self.write(".playground/stack.toml", STACK.replace('provider = "Kiln"', f'provider = "Kiln"\nurl = "{server.url}"'))
            up = services.fetch(self.service("site"), self.lookup, NOW)
            self.assertEqual((up.ok, up.metrics["up"], up.metrics["status"]), (True, 1, 200))
            self.assertTrue(up.status.startswith("Up · answered in"))
            self.write(".playground/stack.toml", STACK.replace('provider = "Kiln"',
                                                              f'provider = "Kiln"\nurl = "{server.url}missing"'))
            down = services.fetch(self.service("site"), self.lookup, NOW)
            self.assertEqual((down.metrics["up"], down.status), (0, "Down · answered 404."))
        finally:
            server.shutdown()
            server.server_close()
        self.write(".playground/stack.toml", STACK.replace('provider = "Kiln"', f'provider = "Kiln"\nurl = "{server.url}"'))
        self.assertEqual(services.fetch(self.service("site"), self.lookup, NOW).status, "Down · nothing answered.")
        self.write(".playground/stack.toml", STACK)
        self.assertIn("Add the address", services.fetch(self.service("site"), self.lookup, NOW).status)

    def test_manual_numbers(self):
        reading = services.fetch(self.service("texts"), self.lookup, NOW)
        self.assertEqual(reading.status, "These numbers are added by hand or by an agent.")
        snap = services.record_usage(self.project_dir, "texts", {"texts_sent": 412, "note": "words are dropped",
                                                                 "flag": True}, cost=8.24, source="agent", when=NOW,
                                     bus=self.bus)
        self.assertEqual(snap["metrics"], {"texts_sent": 412})
        self.assertEqual(services.latest(self.project_dir, "texts")["cost"], 8.24)
        with self.assertRaises(ValueError):
            services.record_usage(self.project_dir, "nope", {"x": 1})


class RefreshTests(ServicesTestCase):
    def test_refresh_runs_in_a_worker_thread(self):
        done = threading.Event()
        results = []
        thread = services.refresh(self.project_dir, lookup=self.lookup, bus=self.bus,
                                  done=lambda readings: (results.extend(readings), done.set()))
        self.assertIsNot(thread, threading.current_thread())
        self.assertTrue(done.wait(30))
        self.assertEqual(len(results), 6)
        self.assertTrue(self.topics(events.USAGE_CHANGED))
        self.assert_no_secrets_in_project()

    @unittest.skipUnless(HAVE_CRYPTO, CRYPTO_SKIP)
    def test_credentials_come_from_the_vault(self):
        from luma_playground.core import security
        project_id = self.store().read_project().id
        vault = security.Vault(self.project_dir, project_id)
        vault.create("nick", "Nick")
        for name in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
            vault.set(name, SECRETS[name])
        fake = self.provider({"/graphql": CloudflareTests.graphql_reply.__get__(self)})
        with mock.patch.object(services, "CLOUDFLARE_GRAPHQL", fake.url + "/graphql"):
            readings = services.refresh_now(self.project_dir, "r2", now=NOW, bus=self.bus)
        self.assertTrue(readings[0].ok, readings[0].status)
        self.assertEqual(fake.requests[0]["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assert_no_secrets_in_project()

    def test_without_a_vault_every_credentialed_service_says_how_to_fix_it(self):
        readings = {r.service_id: r for r in services.refresh_now(self.project_dir, now=NOW, bus=self.bus)}
        for sid in ("r2", "email", "aws", "db"):
            self.assertIn("Places → Vault", readings[sid].status, sid)


class MoneyTests(ServicesTestCase):
    def snap(self, sid, ts, cost):
        when = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        services.record_usage(self.project_dir, sid, {"x": 1}, cost=cost, when=when, bus=self.bus)

    def test_cost_against_budget_and_trend(self):
        self.snap("aws", "2026-08-31T00:00:00", 10.0)
        self.snap("aws", "2026-09-15T00:00:00", 9.0)
        self.snap("aws", "2026-09-30T00:00:00", 12.0)   # September's last reading wins
        self.snap("aws", "2026-10-09T00:00:00", 16.5)
        self.snap("texts", "2026-09-30T00:00:00", 6.0)
        self.snap("texts", "2026-10-08T00:00:00", 4.0)
        view = services.money_summary(self.project_dir, NOW, months=3)
        self.assertEqual(view["periods"], ["2026-08", "2026-09", "2026-10"])
        rows = {r["id"]: r for r in view["services"]}
        self.assertEqual(rows["aws"]["trend"], [10.0, 12.0, 16.5])
        self.assertTrue(rows["aws"]["over_budget"])
        self.assertEqual(rows["aws"]["summary"], "$16.50 of $15.00 budget · $1.50 over")
        self.assertEqual(rows["aws"]["trend_words"], "Up 38% on last month so far.")
        self.assertEqual(rows["email"]["cost"], 20.0)              # fixed plan price, no reading needed
        self.assertEqual(rows["db"]["summary"], "No cost reported yet.")
        self.assertEqual(view["total"]["cost"], 40.5)
        self.assertEqual(view["total"]["budget"], 50.0)
        self.assertEqual(view["total"]["trend"], [10.0, 18.0, 20.5])
        self.assertEqual(view["total"]["summary"], "$40.50 so far this month of $50.00 budget")

    def test_one_monthly_picture_with_models(self):
        self.snap("aws", "2026-10-05T00:00:00", 12.0)
        project_id = self.store().read_project().id
        usage.record_run("team-key", runs_on="the team key", cost=3.25, tokens_in=40_000, project_id=project_id, when=NOW)
        usage.record_run("nick-claude", runs_on="Nick's Claude", tokens_in=90_000, project_id=project_id, when=NOW)
        usage.record_run("other", cost=99, project_id="another-project", when=NOW)
        usage.record_run("team-key", cost=1.0, project_id=project_id, when=datetime(2026, 9, 30, tzinfo=timezone.utc))
        picture = usage.monthly_money(self.project_dir, project_id=project_id, now=NOW)
        self.assertEqual(picture["month"], "2026-10")
        self.assertEqual(picture["model_cost"], 3.25)
        self.assertEqual(picture["service_cost"], 32.0)            # aws 12 + email plan 20
        self.assertEqual(picture["total"]["cost"], 35.25)
        self.assertEqual(picture["summary"],
                         "October so far: $35.25 · models $3.25, services $32.00 · services budget $50.00")
        lines = [m["summary"] for m in picture["models"]]
        self.assertEqual(lines, ["the team key: $3.25 · 40k tokens", "Nick's Claude: 90k tokens, on a plan"])

    def test_models_only_without_a_project(self):
        usage.record_run("team-key", cost=2.0, when=NOW)
        picture = usage.monthly_money(now=NOW)
        self.assertEqual((picture["total"]["cost"], picture["services"]), (2.0, []))


if __name__ == "__main__":
    unittest.main()
