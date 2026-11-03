# SPDX-License-Identifier: Apache-2.0
"""The code page's diff as people read it: numbered lines and "N unchanged lines", never @@ headers."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from luma_playground.app import difflines
from luma_playground.app.difflines import Gap, Segment


def words(parts) -> list[str]:
    out = []
    for part in parts:
        if isinstance(part, Gap):
            out.append(f"[{part.words}]")
        else:
            out.extend(f"{line.new or '':>2}{ {'add': '+', 'del': '-'}.get(line.kind, ' ') }{line.text}"
                       for line in part.lines)
    return out


NOW = [f"line {n}" for n in range(1, 21)]
NOW[4] = "line 5 changed"
NOW.insert(14, "a new line")   # the file now has 21 lines
DIFF = """diff --git a/f.txt b/f.txt
index 1111111..2222222 100644
--- a/f.txt
+++ b/f.txt
@@ -2,7 +2,7 @@ heading
 line 2
 line 3
 line 4
-line 5
+line 5 changed
 line 6
 line 7
 line 8
@@ -12,6 +12,7 @@
 line 12
 line 13
 line 14
+a new line
 line 15
 line 16
 line 17
\\ No newline at end of file
"""


class LayoutTests(unittest.TestCase):
    def test_hunk_headers_become_unchanged_line_separators(self):
        parts = difflines.layout(DIFF, NOW)
        shown = words(parts)
        self.assertFalse([w for w in shown if "@@" in w or w.startswith(("diff", "index", "---", "+++"))])
        self.assertEqual(shown[0], "[1 unchanged line]")
        self.assertIn("[3 unchanged lines]", shown)       # lines 9, 10, 11
        self.assertEqual(shown[-1], "[3 unchanged lines]")  # lines 19 to 21 now
        self.assertIn(" 5+line 5 changed", shown)
        self.assertIn("  -line 5", shown)
        self.assertIn("15+a new line", shown)
        self.assertIn("16 line 15", shown)
        self.assertEqual([type(p) for p in parts], [Gap, Segment, Gap, Segment, Gap])
        self.assertTrue(all(p.expandable for p in parts if isinstance(p, Gap)))

    def test_a_gap_opens_with_the_right_numbers(self):
        middle = [p for p in difflines.layout(DIFF, NOW) if isinstance(p, Gap)][1]
        self.assertEqual((middle.key, middle.count), (9, 3))
        parts = difflines.layout(DIFF, NOW, {middle.key})
        self.assertEqual([type(p) for p in parts], [Gap, Segment, Gap])   # the two hunks are one run now
        lines = parts[1].lines
        nine = next(line for line in lines if line.new == 9)
        self.assertEqual((nine.old, nine.text, nine.kind), (9, "line 9", "context"))
        last = difflines.layout(DIFF, NOW, {middle.key, 19})
        tail = last[-1].lines[-3:]
        self.assertEqual([(line.old, line.new, line.text) for line in tail],
                         [(18, 19, "line 18"), (19, 20, "line 19"), (20, 21, "line 20")])

    def test_without_the_file_now_gaps_stay_closed_and_the_end_is_unknown(self):
        parts = difflines.layout(DIFF, None, {1, 9})
        gaps = [p for p in parts if isinstance(p, Gap)]
        self.assertEqual([(g.key, g.count, g.expandable) for g in gaps], [(1, 1, False), (9, 3, False)])
        self.assertIsInstance(parts[-1], Segment)

    def test_a_new_file_is_all_added_with_no_gaps(self):
        diff = difflines.new_file_diff("one\ntwo\nthree\n")
        parts = difflines.layout(diff, ["one", "two", "three"])
        self.assertEqual(words(parts), [" 1+one", " 2+two", " 3+three"])

    def test_pure_deletions_and_single_line_hunks(self):
        diff = "@@ -3,2 +2,0 @@\n-gone a\n-gone b\n@@ -9 +7 @@\n-old\n+new\n"
        now = ["a", "b", "c", "d", "e", "f", "new", "h"]
        shown = words(difflines.layout(diff, now))
        self.assertEqual(shown, ["[2 unchanged lines]", "  -gone a", "  -gone b", "[4 unchanged lines]", "  -old",
                                 " 7+new", "[1 unchanged line]"])
        self.assertEqual(difflines.Gap(1, 1, True).words, "1 unchanged line")


@unittest.skipUnless(shutil.which("git"), "git isn't here")
class RealGitTests(unittest.TestCase):
    def test_against_a_real_git_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = lambda *a: subprocess.run(["git", *a], cwd=tmp, capture_output=True, text=True,  # noqa: E731
                                            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
            run("init", "-q")
            path = Path(tmp, "page.html")
            before = [f"<p>{n}</p>" for n in range(1, 41)]
            path.write_text("\n".join(before) + "\n")
            run("add", ".")
            run("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "one")
            after = list(before)
            after[9] = "<p>ten, changed</p>"
            after.insert(30, "<p>new</p>")
            path.write_text("\n".join(after) + "\n")
            diff = run("diff", "--no-color", "-U3", "HEAD", "--", "page.html").stdout
            parts = difflines.layout(diff, after)
            gaps = [p for p in parts if isinstance(p, Gap)]
            self.assertEqual(sum(g.count for g in gaps) + sum(
                1 for p in parts if isinstance(p, Segment) for line in p.lines if line.new is not None), len(after))
            opened = difflines.layout(diff, after, {g.key for g in gaps})
            self.assertEqual(len(opened), 1)
            self.assertEqual([line.text for line in opened[0].lines if line.new is not None], after)


if __name__ == "__main__":
    unittest.main()
