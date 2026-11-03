# SPDX-License-Identifier: Apache-2.0
"""Members' photos: a ``photo`` path inside .playground/people/, synced, checked before it is shown."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from luma_playground.core import events, samples
from luma_playground.core.model import Member
from luma_playground.core.project import create_project
from luma_playground.core.store import NotFound, ProjectStore, StoreError, member_photo_path, photo_problem

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4" fill="#c86"/></svg>'


class PhotoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        bus = events.EventBus()
        project = create_project("Kiln", root=base, git=False, bus=bus, owner=Member(id="noor", name="Noor", role="owner"))
        self.root = project.path
        self.store = ProjectStore(self.root, bus=bus)
        made = self.store.read_project()
        made.members.append(Member(id="priya", name="Priya Raman"))
        self.store.write_project(made)
        self.people = self.store.paths.people_dir

    def tearDown(self):
        self.tmp.cleanup()

    def member(self, member_id="priya"):
        return next(m for m in self.store.read_project().members if m.id == member_id)

    def test_set_keeps_the_file_in_people_and_names_it_in_project_toml(self):
        path = self.store.set_member_photo("priya", SVG, ".svg")
        self.assertEqual(path, self.people / "priya.svg")
        self.assertEqual(self.member().extra["photo"], "people/priya.svg")
        self.assertIn('photo = "people/priya.svg"', self.store.paths.project_toml.read_text())
        self.assertEqual(self.store.member_photo(self.member()), path)
        # A new photo of another kind replaces the old file.
        png = self.store.set_member_photo("priya", PNG, "png")
        self.assertEqual(self.store.member_photo(self.member()), png)
        self.assertFalse((self.people / "priya.svg").exists())
        self.store.clear_member_photo("priya")
        self.assertNotIn("photo", self.member().extra)
        self.assertFalse(png.exists())
        self.assertIsNone(self.store.member_photo(self.member()))

    def test_without_a_photo_there_is_none(self):
        self.assertIsNone(self.store.member_photo(self.member()))
        self.assertIsNone(member_photo_path(self.root, {"id": "x", "photo": ""}))

    def test_refused_when_set(self):
        with self.assertRaises(StoreError):
            self.store.set_member_photo("priya", b"GIF89a", ".gif")
        with self.assertRaises(StoreError):
            self.store.set_member_photo("priya", b"not a png", ".png")
        with self.assertRaises(StoreError):
            self.store.set_member_photo("priya", b"<svg><script>alert(1)</script></svg>", ".svg")
        with self.assertRaises(StoreError):
            self.store.set_member_photo("priya", PNG + b"\x00" * (2 * 1024 * 1024), ".png")
        with self.assertRaises(NotFound):
            self.store.set_member_photo("nobody", PNG, ".png")
        with self.assertRaises(ValueError):
            self.store.set_member_photo("../x", PNG, ".png")

    def test_a_synced_photo_field_cannot_point_anywhere_else(self):
        self.people.mkdir(parents=True, exist_ok=True)
        (self.people / "ok.png").write_bytes(PNG)
        outside = self.root / "secret.png"
        outside.write_bytes(PNG)
        (self.people / "link.png").symlink_to(outside)
        (self.people / "fake.png").write_bytes(b"#!/bin/sh\n")
        (self.people / "bad.svg").write_bytes(b'<svg><image href="https://example.com/track.png"/></svg>')
        (self.people / "notes.txt").write_text("hi")
        self.assertEqual(member_photo_path(self.root, {"photo": "people/ok.png"}), self.people / "ok.png")
        self.assertEqual(member_photo_path(self.root, {"photo": ".playground/people/ok.png"}), self.people / "ok.png")
        for value in ("../secret.png", "people/../../secret.png", "/etc/passwd", str(outside), "people/link.png",
                      "people/fake.png", "people/bad.svg", "people/notes.txt", "people/missing.png",
                      "proof/ok.png", "people/sub/ok.png", "people/.hidden.png", 42):
            with self.subTest(value=value):
                self.assertIsNone(member_photo_path(self.root, {"photo": value}))

    def test_svg_drawings_only(self):
        self.assertEqual(photo_problem(SVG, ".svg"), "")
        gradient = (b'<svg xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="g"/></defs>'
                    b'<rect fill="url(#g)"/><use href="#g"/></svg>')
        self.assertEqual(photo_problem(gradient, ".svg"), "")
        for bad in (b'<svg onload="x()"/>', b'<svg><foreignObject/></svg>', b'<svg><rect fill="url(http://x/y)"/></svg>',
                    b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>',
                    b'<svg><use xlink:href="other.svg#a"/></svg>', b'<svg><style>@import "http://x";</style></svg>'):
            with self.subTest(bad=bad):
                self.assertTrue(photo_problem(bad, ".svg"))


class SamplePeopleTests(unittest.TestCase):
    def test_kiln_ships_drawn_portraits_and_numbers_only_capacity(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            project = samples.create_sample_kiln(tmp, git=False, bus=events.EventBus())
            store = ProjectStore(project.path, bus=events.EventBus())
            members = store.read_project().members
            self.assertEqual({m.id for m in members}, {"alex", "priya", "sam"})
            for member in members:
                path = store.member_photo(member)
                self.assertIsNotNone(path, member.id)
                self.assertEqual(path.parent, store.paths.people_dir)
                self.assertEqual(photo_problem(path.read_bytes(), ".svg"), "")
            capacity = store.paths.meta / "capacity"
            priya = json.loads((capacity / "priya.json").read_text())
            self.assertEqual(priya["connections"][0]["team_task_percent"], 20)
            self.assertTrue(priya["connections"][0]["team_tasks"])
            sam = json.loads((capacity / "sam.json").read_text())
            self.assertFalse(sam["connections"][0]["team_tasks"])
            allowed = {"label", "provider", "plan", "team_tasks", "team_task_percent", "percent_left", "window",
                       "resets_at"}
            for data in (priya, sam):
                for row in data["connections"]:
                    self.assertLessEqual(set(row), allowed, "capacity is labels and numbers, never accounts")

    def test_device_usage_gives_the_v73_numbers(self):
        from unittest import mock
        from luma_playground.core import usage
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"PLAYGROUND_DATA_DIR": tmp}):
            samples.create_sample_device_usage()
            self.assertTrue(usage.summary("claude").startswith("62% of the week left"))
            self.assertEqual(usage.latest_headroom("codex")["window"], "day")
            from datetime import datetime, timezone
            month = datetime.now().astimezone().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            spent = lambda cid: round(sum(float(r.get("cost") or 0)  # noqa: E731
                                          for r in usage.runs(cid, since=month.astimezone(timezone.utc))), 2)
            self.assertEqual(spent("ari"), 1.80)
            self.assertEqual(spent("team-key"), 6.40)


if __name__ == "__main__":
    unittest.main()
