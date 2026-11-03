# SPDX-License-Identifier: Apache-2.0
"""A diff as people read it (v73 shot 10): numbered lines, and "2 unchanged lines" where git's
``@@`` hunk headers would be. No GTK here, so it is unit-tested without a display.

    segments = layout(diff_text, new_lines, expanded=set())

gives a list of ``Segment`` (lines shown together) and ``Gap`` (lines left out, which the code
page shows as a separator; clicking it adds the gap's ``key`` to ``expanded``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Line:
    kind: str            # "context" | "add" | "del"
    old: int | None      # line number before the change (None for an added line)
    new: int | None      # line number now (None for a removed line)
    text: str


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[Line] = field(default_factory=list)


@dataclass
class Segment:
    lines: list[Line]


@dataclass
class Gap:
    key: int             # the first hidden line's number now; stable across expansions
    count: int
    expandable: bool     # False when the file as it is now can't be read (removed, binary)

    @property
    def words(self) -> str:
        return "1 unchanged line" if self.count == 1 else f"{self.count} unchanged lines"


def parse(diff: str) -> list[Hunk]:
    """The hunks of a unified diff. Headers (diff --git, index, ---/+++) are skipped."""
    hunks: list[Hunk] = []
    current: Hunk | None = None
    old = new = 0
    for raw in diff.splitlines():
        match = _HUNK.match(raw)
        if match:
            old_start, old_count = int(match.group(1)), int(match.group(2) if match.group(2) is not None else 1)
            new_start, new_count = int(match.group(3)), int(match.group(4) if match.group(4) is not None else 1)
            current = Hunk(old_start, old_count, new_start, new_count)
            hunks.append(current)
            # A count of 0 means "after line N": the next line is N + 1.
            old = old_start if old_count else old_start + 1
            new = new_start if new_count else new_start + 1
            continue
        if current is None or raw.startswith("\\"):   # before the first hunk, or "\ No newline at end of file"
            continue
        sign, text = raw[:1], raw[1:]
        if sign == "+":
            current.lines.append(Line("add", None, new, text))
            new += 1
        elif sign == "-":
            current.lines.append(Line("del", old, None, text))
            old += 1
        elif sign == " " or raw == "":
            current.lines.append(Line("context", old, new, text))
            old += 1
            new += 1
    return hunks


def _first_new(hunk: Hunk) -> int:
    return hunk.new_start if hunk.new_count else hunk.new_start + 1


def layout(diff: str, new_lines: list[str] | None, expanded: set[int] | frozenset[int] = frozenset()) -> list:
    """Segments of shown lines, with a Gap wherever unchanged lines are left out.

    ``new_lines`` is the file as it is now (None when it can't be read): it fills an expanded gap
    and tells how many unchanged lines follow the last change."""
    hunks = parse(diff)
    out: list = []
    shown: list[Line] = []

    def flush() -> None:
        if shown:
            out.append(Segment(list(shown)))
            shown.clear()

    def gap(first_new: int, count: int, delta: int) -> None:
        """Lines first_new .. first_new+count-1 (numbered now) didn't change; delta = old - new there."""
        if count <= 0:
            return
        readable = new_lines is not None and first_new - 1 + count <= len(new_lines)
        if readable and first_new in expanded:
            for number in range(first_new, first_new + count):
                shown.append(Line("context", number + delta, number, new_lines[number - 1]))
            return
        flush()
        out.append(Gap(first_new, count, readable))

    next_new, delta = 1, 0
    for hunk in hunks:
        start = _first_new(hunk)
        gap(next_new, start - next_new, delta)
        shown.extend(hunk.lines)
        next_new = start + hunk.new_count
        next_old = (hunk.old_start if hunk.old_count else hunk.old_start + 1) + hunk.old_count
        delta = next_old - next_new
    if hunks and new_lines is not None:
        gap(next_new, len(new_lines) - next_new + 1, delta)
    flush()
    return out


def new_file_diff(text: str) -> str:
    """A file git doesn't know yet, as a diff where every line is added."""
    lines = text.rstrip("\n").split("\n") if text else []
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)
