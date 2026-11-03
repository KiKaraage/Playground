# SPDX-License-Identifier: Apache-2.0
"""Search everywhere: every word must appear, titles outrank bodies, finished work sinks, accents fold."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from luma_playground.core import search
from luma_playground.core.samples import create_sample_kiln
from luma_playground.core.store import ProjectStore


class SearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(create_sample_kiln(cls.tmp.name, git=False).path)
        cls.store = ProjectStore(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_finds_across_areas(self):
        kinds = {hit.kind for hit in search.search(self.store, "waitlist")}
        self.assertIn("conversation", kinds)
        self.assertTrue(kinds & {"task", "knowledge", "bug"}, kinds)

    def test_every_word_must_appear(self):
        self.assertEqual(search.search(self.store, "waitlist zzqqx"), [])
        self.assertEqual(search.search(self.store, "   "), [])

    def test_title_outranks_body(self):
        self.assertGreater(search._score("Add a waitlist", "", ["waitlist"]),
                           search._score("Something", "the waitlist", ["waitlist"]))

    def test_accents_and_case_fold(self):
        self.assertEqual(search.terms("  Café  RÉSUMÉ "), ["cafe", "resume"])

    def test_snippet_is_around_the_word(self):
        body = "A long preamble that goes on and on about nothing in particular. " * 3 + "Then the **waitlist** fills."
        piece = search.snippet(body, ["waitlist"])
        self.assertIn("waitlist", piece)
        self.assertTrue(piece.startswith("…"))
        self.assertLessEqual(len(piece), search.SNIPPET + 2)

    def test_snippet_drops_checklist_marks(self):
        self.assertEqual(search.snippet("Checks\n- [ ] One text, never two", ["text"]), "Checks One text, never two")


if __name__ == "__main__":
    unittest.main()
