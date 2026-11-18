# SPDX-License-Identifier: Apache-2.0
"""A suggested-rule card keeps its whole text behind a Show more / Show less toggle."""
from __future__ import annotations

from types import SimpleNamespace
import unittest


def gtk():
    # Skip only when GTK itself is unavailable; a broken app import must fail the test, not skip it.
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        return Gtk
    except (ImportError, ValueError, RuntimeError) as error:
        raise unittest.SkipTest(f"GTK isn't here: {error}")


Gtk = gtk()


def descendants(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from descendants(child)
        child = child.get_next_sibling()


class SuggestedRuleCardTests(unittest.TestCase):
    def setUp(self) -> None:
        from luma_playground.app.sheets import areas_pages

        class Host:
            suggestion_card = areas_pages.AreaPages.suggestion_card

            def _members(self):
                return []

            sheet = SimpleNamespace(phone=False)

            def keep_note(self, note, keep):
                pass

        self.host = Host()

    def card(self, body: str):
        note = SimpleNamespace(added_by="agent:opencode", title="A rule title", body=body)
        return self.host.suggestion_card(note)

    def body_label(self, card):
        labels = [w for w in descendants(card)
                  if isinstance(w, Gtk.Label) and "pgs-row-sub" in w.get_css_classes()
                  and "Agents won" not in w.get_label()]
        self.assertEqual(len(labels), 1, "expected exactly one body label")
        return labels[0]

    def button(self, card, label: str):
        matches = [w for w in descendants(card) if isinstance(w, Gtk.Button) and w.get_label() == label]
        self.assertEqual(len(matches), 1, f"expected one {label!r} button")
        return matches[0]

    def test_long_body_can_be_expanded_and_collapsed(self):
        body = " ".join(f"Sentence number {n} of a suggested rule." for n in range(12))
        card = self.card(body)
        label = self.body_label(card)
        more = self.button(card, "Show more")

        self.assertNotEqual(label.get_label(), body, "collapsed body should be shortened")
        self.assertTrue(label.get_label().endswith("…"), "collapsed body should end with an ellipsis")

        more.emit("clicked")
        self.assertEqual(label.get_label(), body, "expanded body should show the full rule")
        self.assertEqual(self.button(card, "Show less"), more)

        more.emit("clicked")
        self.assertNotEqual(label.get_label(), body)
        self.assertEqual(self.button(card, "Show more"), more)

    def test_short_body_has_no_toggle(self):
        card = self.card("A short rule with nothing to hide.")
        self.assertEqual(self.body_label(card).get_label(), "A short rule with nothing to hide.")
        self.assertFalse([w for w in descendants(card)
                          if isinstance(w, Gtk.Button) and w.get_label() in ("Show more", "Show less")])

    def test_empty_body_has_no_toggle(self):
        card = self.card("")
        self.assertFalse([w for w in descendants(card)
                          if isinstance(w, Gtk.Button) and w.get_label() in ("Show more", "Show less")])


if __name__ == "__main__":
    unittest.main()
