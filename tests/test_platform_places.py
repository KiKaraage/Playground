# SPDX-License-Identifier: Apache-2.0
"""Places, without real SSH: a fake ``ssh`` on PATH answers by host name."""
from __future__ import annotations

import dataclasses
import time
import tomllib
import unittest
from unittest import mock

from test_platform_support import PlatformTestCase

from luma_playground.core import events, places
from luma_playground.core.places import PlaceError, ProductionProtected


class ValidationTests(unittest.TestCase):
    def test_hosts(self):
        self.assertEqual(places.validate_host("203.0.113.7"), "203.0.113.7")
        self.assertEqual(places.validate_host("Dev.Example.com."), "dev.example.com")
        self.assertEqual(places.validate_host("[2001:db8::1]"), "2001:db8::1")
        self.assertEqual(places.validate_host("closet-box"), "closet-box")
        for bad in ("", "-oProxyCommand=touch /tmp/x", "a b", "evil;rm -rf ~", "$(id)", "host/path",
                    "x" * 300, "bad_host!.com", "-p"):
            with self.subTest(bad=bad), self.assertRaises(PlaceError):
                places.validate_host(bad)

    def test_users_and_ports(self):
        self.assertEqual(places.validate_user("deploy"), "deploy")
        for bad in ("", "Root", "-l", "a b", "x;y", "a" * 40):
            with self.subTest(bad=bad), self.assertRaises(PlaceError):
                places.validate_user(bad)
        self.assertEqual(places.validate_port(None), 22)
        self.assertEqual(places.validate_port("2222"), 2222)
        self.assertEqual(places.validate_port(0), 22)   # 0 means "not set" in the model
        for bad in (-1, 70000, "ssh"):
            with self.subTest(bad=bad), self.assertRaises(PlaceError):
                places.validate_port(bad)


class PlacesTests(PlatformTestCase):
    def setUp(self):
        super().setUp()
        self.fake_ssh()

    def add(self, host="ok.example", role="development", **kw):
        return places.add_place(self.project_dir, kw.pop("name", "Dev box"), role=role, host=host,
                                user=kw.pop("user", "deploy"), bus=self.bus, **kw)

    def test_add_tests_with_batchmode_and_saves_without_secrets(self):
        result = self.add(port=2222, path="/srv/kiln")
        self.assertTrue(result["test"]["ok"])
        self.assertEqual(result["test"]["system"], "Linux x86_64")
        call = self.ssh_calls()[-1]
        self.assertIn("BatchMode=yes", call)
        self.assertIn("PasswordAuthentication=no", call)
        self.assertIn("StrictHostKeyChecking=accept-new", call)
        self.assertEqual(call[call.index("--") + 1], "deploy@ok.example")
        self.assertEqual(call[call.index("-p") + 1], "2222")
        doc = tomllib.loads((self.project_dir / ".playground" / "project.toml").read_text())
        saved = doc["places"][0]
        self.assertEqual({k: saved[k] for k in ("role", "kind", "host", "user", "port", "path")},
                         {"role": "development", "kind": "ssh", "host": "ok.example", "user": "deploy",
                          "port": 2222, "path": "/srv/kiln"})
        text = (self.project_dir / ".playground" / "project.toml").read_text().lower()
        for word in ("password", "private", "secret =", "token", "key ="):
            self.assertNotIn(word, text)
        self.assertTrue(self.topics(events.PLACES_CHANGED))

    def test_plain_words_for_each_failure(self):
        cases = {"nowhere.example": ("not-found", "can't find a server"),
                 "denied.example": ("no-access", "didn't let Playground in"),
                 "changed.example": ("host-key-changed", "identity has changed"),
                 "slow.example": ("unreachable", "switched on")}
        for host, (state, words) in cases.items():
            with self.subTest(host=host):
                result = self.add(host=host, name=host)
                self.assertFalse(result["test"]["ok"])
                self.assertEqual(result["test"]["state"], state)
                self.assertIn(words, result["test"]["message"])
                place = places.get_place(self.project_dir, result["place"]["id"])
                self.assertIn(words, place["status"])

    def test_list_includes_this_computer_and_statuses(self):
        self.add()
        listed = places.list_places(self.project_dir)
        self.assertEqual(listed[0]["id"], "this-computer")
        self.assertTrue(listed[0]["is_home"])
        self.assertEqual(listed[1]["status"], "Connected (Linux x86_64).")
        self.assertEqual(places.describe(listed[1]), "Development server · deploy@ok.example")

    def test_duplicates_bad_input_and_no_test(self):
        self.add()
        with self.assertRaises(PlaceError):
            self.add()
        with self.assertRaises(PlaceError):
            self.add(host="-oProxyCommand=evil", name="x")
        with self.assertRaises(PlaceError):
            places.add_place(self.project_dir, "x", role="moon", host="ok.example", user="deploy")
        before = len(self.ssh_calls())
        places.add_place(self.project_dir, "Later", host="later.example", user="deploy", test=False)
        self.assertEqual(len(self.ssh_calls()), before)

    def test_remove_and_set_home(self):
        dev = self.add()["place"]
        prod = self.add(host="prod.example", role="production", name="Live", url="https://kiln.app")["place"]
        result = places.set_home(self.project_dir, dev["id"], bus=self.bus)
        self.assertEqual(result, {"home": dev["id"], "warning": ""})
        with self.assertRaisesRegex(PlaceError, "production"):
            places.set_home(self.project_dir, prod["id"])
        self.assertEqual(places.live_url(self.project_dir), "https://kiln.app")
        self.assertTrue(places.remove_place(self.project_dir, dev["id"]))
        self.assertEqual(self.store().read_project().home, "this-computer")
        self.assertFalse(places.remove_place(self.project_dir, dev["id"]))
        with self.assertRaises(PlaceError):
            places.remove_place(self.project_dir, "this-computer")
        warning = places.set_home(self.project_dir, "this-computer")["warning"]
        self.assertEqual(warning, "Teammates can work while this computer is on.")

    def test_update_place(self):
        dev = self.add()["place"]
        updated = places.update_place(self.project_dir, dev["id"], name="Closet box", port=2200, url="https://x.dev")
        self.assertEqual((updated["name"], updated["port"], updated["url"]), ("Closet box", 2200, "https://x.dev"))
        with self.assertRaises(PlaceError):
            places.update_place(self.project_dir, dev["id"], host="bad host")
        with self.assertRaises(PlaceError):
            places.update_place(self.project_dir, dev["id"], password="hunter2")

    def test_retest_remembers_locally(self):
        dev = self.add(host="denied.example")["place"]
        result = places.test_place(self.project_dir, dev["id"], bus=self.bus)
        self.assertFalse(result["ok"])
        status_file = self.project_dir / ".playground" / "local" / "places-status.json"
        self.assertTrue(status_file.exists())
        self.assertEqual(places.test_place(self.project_dir, "this-computer")["state"], "ok")


class ProductionGuardTests(PlatformTestCase):
    def setUp(self):
        super().setUp()
        self.fake_ssh()
        self.prod = places.add_place(self.project_dir, "Live", role="production", host="prod.example",
                                     user="deploy", test=False)["place"]
        self.dev = places.add_place(self.project_dir, "Dev", host="dev.example", user="deploy", test=False)["place"]
        self.review = {"id": "review-1", "ready": True, "destination": self.prod["id"]}

    def test_agents_can_write_to_development_but_never_production(self):
        self.assertTrue(places.guard_write(self.dev))
        with self.assertRaisesRegex(ProductionProtected, "Propose a release"):
            places.guard_write(self.prod)
        with self.assertRaises(ProductionProtected):
            places.guard_write(self.prod, actor="person")
        self.assertTrue(places.is_protected(self.prod))

    def test_release_only_through_an_approved_review(self):
        with self.assertRaisesRegex(ProductionProtected, "isn't ready"):
            places.authorize_release(self.project_dir, {**self.review, "ready": False}, self.prod["id"], "noor")
        with self.assertRaisesRegex(ProductionProtected, "different place"):
            places.authorize_release(self.project_dir, {**self.review, "destination": self.dev["id"]},
                                     self.prod["id"], "noor")
        with self.assertRaisesRegex(ProductionProtected, "owner or a reviewer"):
            places.authorize_release(self.project_dir, self.review, self.prod["id"], "priya")
        with self.assertRaises(ProductionProtected):
            places.authorize_release(self.project_dir, self.review, self.prod["id"], "stranger")
        ticket = places.authorize_release(self.project_dir, self.review, self.prod["id"], "rae")
        self.assertTrue(places.guard_write(self.prod, ticket=ticket))
        with self.assertRaisesRegex(ProductionProtected, "already been used"):
            places.guard_write(self.prod, ticket=ticket)
        activity = self.store().read_activity()
        self.assertTrue(any(e.kind == "release" for e in activity))

    def test_forged_misdirected_and_expired_tickets_fail(self):
        ticket = places.authorize_release(self.project_dir, self.review, self.prod["id"], "noor")
        forged = dataclasses.replace(ticket, approved_by="priya")
        with self.assertRaisesRegex(ProductionProtected, "isn't genuine"):
            places.guard_write(self.prod, ticket=forged)
        other_prod = {**self.prod, "id": "other-prod"}
        with self.assertRaisesRegex(ProductionProtected, "different place"):
            places.guard_write(other_prod, ticket=ticket)
        with mock.patch.object(places.time, "time", return_value=time.time() + places.TICKET_LIFETIME_S + 1):
            with self.assertRaisesRegex(ProductionProtected, "expired"):
                places.guard_write(self.prod, ticket=ticket)

    def test_status_says_production_is_protected(self):
        status = places.get_place(self.project_dir, self.prod["id"])["status"]
        self.assertIn("Agents can't change it", status)


if __name__ == "__main__":
    unittest.main()
