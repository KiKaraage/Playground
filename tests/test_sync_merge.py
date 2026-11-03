# SPDX-License-Identifier: Apache-2.0
"""core.sync's merges: deterministic, and never lose an entry."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from luma_playground.core import sync  # noqa: E402
from luma_playground.core.model import Goal, Story  # noqa: E402
from luma_playground.core.store import format_goal, parse_goal  # noqa: E402


def b(text: str) -> bytes:
    return text.encode()


class TrivialCases(unittest.TestCase):
    def test_one_sided_changes_and_deletes(self):
        self.assertEqual(sync.merge_file("x.md", b"a", b"a", b"b").content, b"b")
        self.assertEqual(sync.merge_file("x.md", b"a", b"b", b"a").content, b"b")
        self.assertIsNone(sync.merge_file("x.md", b"a", None, b"a").content)
        # Removed on one side, changed on the other: the change is kept.
        self.assertEqual(sync.merge_file("x.md", b"a", None, b"c").content, b"c")
        self.assertEqual(sync.merge_file("x.md", b"a", b"c", None).content, b"c")


class JsonlTests(unittest.TestCase):
    def test_union_keeps_every_line_once(self):
        base = b'{"n":1}\n'
        ours = base + b'{"n":2}\n{"n":3}\n'
        theirs = base + b'{"n":4}\n{"n":2}\n'
        merged = sync.merge_file("threads/g.jsonl", base, ours, theirs).content
        lines = merged.decode().splitlines()
        self.assertEqual(sorted(lines), sorted(['{"n":1}', '{"n":2}', '{"n":3}', '{"n":4}']))
        self.assertTrue(merged.startswith(ours))      # our order first, theirs appended

    def test_true_duplicates_are_counted_not_collapsed(self):
        ours = b'{"same":1}\n{"same":1}\n'
        theirs = b'{"same":1}\n'
        self.assertEqual(sync.merge_jsonl(b"", ours, theirs).count(b'{"same":1}'), 2)
        self.assertEqual(sync.merge_jsonl(b"", theirs, ours).count(b'{"same":1}'), 2)

    def test_same_multiset_either_way(self):
        a, c = b'{"a":1}\n{"b":2}\n', b'{"c":3}\n'
        one = sync.merge_jsonl(b"", a, c)
        two = sync.merge_jsonl(b"", c, a)
        self.assertEqual(sorted(one.splitlines()), sorted(two.splitlines()))


def goal_text(title="Add a waitlist", status="working", updated="2026-10-09T10:00:00Z", stories=None, notes=""):
    goal = Goal(id="add-waitlist-7f3a", title=title, updated=updated, outcome="People can join a waitlist.",
                stories=stories if stories is not None else [Story(title="A form on the home page")],
                notes=notes)
    goal.status = goal.status.parse(status, goal.status)
    return format_goal(goal).encode()


class MarkdownTests(unittest.TestCase):
    def test_fields_from_both_sides(self):
        base = goal_text()
        ours = goal_text(status="ready", updated="2026-10-09T10:05:00Z")
        theirs = goal_text(title="Add a waiting list", updated="2026-10-09T10:04:00Z")
        merged = parse_goal("add-waitlist-7f3a", sync.merge_file("goals/x.md", base, ours, theirs).content.decode())
        self.assertEqual(merged.title, "Add a waiting list")
        self.assertEqual(merged.status.value, "ready")
        self.assertEqual(merged.updated, "2026-10-09T10:05:00Z")

    def test_stories_added_on_both_sides_are_kept_and_ticks_survive(self):
        first = Story(title="A form on the home page")
        base = goal_text(stories=[first])
        ticked = Story(title="A form on the home page", done=True)
        ours = goal_text(stories=[ticked, Story(title="Email a confirmation")], updated="2026-10-09T10:05:00Z")
        theirs = goal_text(stories=[first, Story(title="Show how many are waiting")],
                           updated="2026-10-09T10:06:00Z")
        merged_text = sync.merge_file("goals/x.md", base, ours, theirs).content.decode()
        merged = parse_goal("add-waitlist-7f3a", merged_text)
        titles = [s.title for s in merged.stories]
        self.assertEqual(titles.count("A form on the home page"), 1)
        self.assertIn("Email a confirmation", titles)
        self.assertIn("Show how many are waiting", titles)
        self.assertTrue(next(s for s in merged.stories if s.title == "A form on the home page").done)

    def test_deterministic_whichever_side_merges(self):
        base = goal_text()
        ours = goal_text(notes="Noor: keep it short.", updated="2026-10-09T10:05:00Z")
        theirs = goal_text(notes="Priya: add a count.", updated="2026-10-09T10:06:00Z")
        one = sync.merge_file("goals/x.md", base, ours, theirs).content
        two = sync.merge_file("goals/x.md", base, theirs, ours).content
        self.assertEqual(one, two)
        self.assertIn(b"Noor: keep it short.", one)
        self.assertIn(b"Priya: add a count.", one)

    def test_decisions_appended_on_both_sides(self):
        base = b"# Decisions\n\nIntro.\n"
        ours = base + b"\n### Use Postgres\n- who: Noor\n- why: it's what we know\n"
        theirs = base + b"\n### Send texts with Twilio\n- who: Priya\n"
        merged = sync.merge_file("decisions.md", base, ours, theirs).content.decode()
        self.assertIn("### Use Postgres", merged)
        self.assertIn("### Send texts with Twilio", merged)
        self.assertEqual(merged.count("# Decisions"), 1)


class StructuredTests(unittest.TestCase):
    def test_project_toml_members_by_id_and_removal_wins(self):
        base = b'[project]\nname = "Kiln"\nid = "k"\n\n[[members]]\nid = "noor"\nname = "Noor"\nrole = "owner"\n\n' \
               b'[[members]]\nid = "sam"\nname = "Sam"\nrole = "builder"\n'
        ours = base.replace(b'[[members]]\nid = "sam"\nname = "Sam"\nrole = "builder"\n', b"")   # Noor removed Sam
        theirs = base.replace(b'name = "Sam"', b'name = "Sam Lee"') + \
            b'\n[[members]]\nid = "priya"\nname = "Priya"\nrole = "builder"\n'
        merged = tomllib.loads(sync.merge_file("project.toml", base, ours, theirs).content.decode())
        ids = [m["id"] for m in merged["members"]]
        self.assertEqual(ids, ["noor", "priya"])

    def test_stack_services_keep_changes_over_removals(self):
        base = b'[[services]]\nid = "resend"\nname = "Resend"\nplan = "free"\n'
        ours = b""                                            # removed
        theirs = base.replace(b'"free"', b'"pro"')           # changed
        merged = tomllib.loads(sync.merge_file("stack.toml", base, ours, theirs).content.decode())
        self.assertEqual(merged["services"][0]["plan"], "pro")

    def test_json_newer_record_wins_a_clash(self):
        base = b'{"id": "q1", "option": null, "at": ""}'
        ours = json.dumps({"id": "q1", "option": "a", "at": "2026-10-09T10:00:00Z"}).encode()
        theirs = json.dumps({"id": "q1", "option": "b", "at": "2026-10-09T10:01:00Z"}).encode()
        one = json.loads(sync.merge_file("answers/q1.json", base, ours, theirs).content)
        two = json.loads(sync.merge_file("answers/q1.json", base, theirs, ours).content)
        self.assertEqual(one, two)
        self.assertEqual(one["option"], "b")

    def test_binary_keeps_both_copies(self):
        result = sync.merge_file("proof/g/shot.png", b"\x89PNG0", b"\x89PNG\x00one", b"\x89PNG\x00two")
        self.assertEqual(len(result.extra), 1)
        kept = {result.content, *result.extra.values()}
        self.assertEqual(kept, {b"\x89PNG\x00one", b"\x89PNG\x00two"})
        again = sync.merge_file("proof/g/shot.png", b"\x89PNG0", b"\x89PNG\x00two", b"\x89PNG\x00one")
        self.assertEqual((again.content, again.extra), (result.content, result.extra))


class VaultMergeTests(unittest.TestCase):
    def vault(self, roster_len: int, secrets: dict) -> bytes:
        roster = [{"body": {"version": i + 1, "members": {"r1": {}, "r2": {}}}, "signer": "r1", "sig": f"s{i}"}
                  for i in range(roster_len)]
        return json.dumps({"format": "playground-vault/1", "project_id": "k", "roster": roster,
                           "secrets": secrets}).encode()

    def test_longer_roster_and_newest_secret_versions(self):
        wraps = {"r1": {}, "r2": {}}
        base = self.vault(1, {"A": {"version": 1, "sig": "x", "wraps": wraps}})
        ours = self.vault(2, {"A": {"version": 2, "sig": "y", "wraps": wraps}})
        theirs = self.vault(1, {"A": {"version": 1, "sig": "x", "wraps": wraps},
                                "B": {"version": 1, "sig": "z", "wraps": wraps}})
        result = sync.merge_file("vault.json", base, ours, theirs)
        merged = json.loads(result.content)
        self.assertEqual(len(merged["roster"]), 2)
        self.assertEqual(merged["secrets"]["A"]["version"], 2)
        self.assertIn("B", merged["secrets"])
        self.assertNotIn("vault-fork", result.notes)

    def test_forked_rosters_are_reported(self):
        base = self.vault(1, {})
        ours = json.loads(self.vault(2, {}))
        theirs = json.loads(self.vault(2, {}))
        theirs["roster"][1]["sig"] = "other"
        result = sync.merge_file("vault.json", base, json.dumps(ours).encode(), json.dumps(theirs).encode())
        self.assertIn("vault-fork", result.notes)


class CodesTests(unittest.TestCase):
    def test_code_round_trip_and_garbage(self):
        envelope = {"body": {"kind": "invite", "x": 1}, "sig": "s"}
        code = sync.encode_code("invite", envelope)
        self.assertTrue(code.startswith("PGI1-"))
        self.assertEqual(sync.decode_code("invite", code), envelope)
        self.assertEqual(sync.decode_code("invite", "  " + code[:20] + "\n" + code[20:] + " "), envelope)
        with self.assertRaises(sync.InviteError):
            sync.decode_code("invite", "PGI1-not-a-code")
        with self.assertRaises(sync.InviteError):
            sync.decode_code("approval", code)

    def test_safe_paths(self):
        for bad in ("../x", "a/../../b", "/etc/passwd", ".git/config", "local/x", "presence/me.json", "a\\b", ""):
            self.assertFalse(sync._safe_rel(bad), bad)
        for good in ("goals/a.md", "threads/a.jsonl", ".gitattributes", "proof/g/1.png"):
            self.assertTrue(sync._safe_rel(good), good)


if __name__ == "__main__":
    unittest.main()
