# SPDX-License-Identifier: Apache-2.0
"""What every card shares: one shape (a kind line, a title, the body, actions at the foot)
and the context a card acts through.

A card never writes a file itself. It asks its `CardContext`, which goes through the
goal's runner when one is connected and through the project store otherwise.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .. import widgets as w  # noqa: E402


@dataclass
class CardContext:
    """What a card can do. Every callable is optional; a card hides what it can't do."""

    project_name: str = ""
    project_dir: str = ""
    goal_id: str = ""
    goal_title: str = ""
    goal_status: str = ""
    agent_name: str = "Ari"
    connection: dict = field(default_factory=dict)
    connection_for: Callable[[str], dict] | None = None
    where_words: Callable[[str], str] | None = None
    open_sheet: Callable[..., None] | None = None
    answer_decision: Callable[[str, str], bool] | None = None
    undo: Callable[[dict, dict], bool] | None = None
    send: Callable[[str], None] | None = None
    focus_composer: Callable[[str], None] | None = None
    show_stage: Callable[[], None] | None = None
    show_image: Callable[[str, str], None] | None = None
    continue_handoff: Callable[[dict, str], bool] | None = None
    show_in_files: Callable[[str], None] | None = None
    goal_for: Callable[[str], dict | None] | None = None
    open_goal: Callable[[str], None] | None = None
    place_name: Callable[[str], str] | None = None
    you: str = ""
    person_name: Callable[[str], str] | None = None
    services: list = field(default_factory=list)
    open_section: Callable[[str, Any], None] | None = None
    # When an agent stops: Try again, sign in (a method from the agent), I've signed in, a web page.
    retry: Callable[[], bool] | None = None
    sign_in: Callable[[dict], None] | None = None
    signed_in: Callable[[], None] | None = None
    open_url: Callable[[str], None] | None = None

    def sheet(self, name: str, **options: Any) -> None:
        if self.open_sheet is not None:
            self.open_sheet(name, **options)

    def connection_of(self, value) -> dict:
        if isinstance(value, dict):
            return value
        if value and self.connection_for is not None:
            return self.connection_for(str(value)) or {"label": str(value)}
        return self.connection or {"label": self.agent_name, "provider": "ari"}


def data_of(entry: dict) -> dict:
    data = entry.get("data")
    return data if isinstance(data, dict) else {}


# ── markdown-lite ─────────────────────────────────────────────────────────────

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


_TAG = re.compile(r"(<[^>]+>)")
_BUG = re.compile(r"(?<![\w&#])#(\d{1,5})\b")


def link_project(markup: str, ctx: "CardContext | None") -> str:
    """Bug numbers (#13) and the project's services (Resend, PostgreSQL) become links into the
    Project space. Only plain text is touched: never inside a tag or an existing link."""
    if ctx is None or ctx.open_section is None:
        return markup
    names = []
    for service in ctx.services or []:
        for name in {str(service.get("name") or ""), str(service.get("provider") or "")}:
            if len(name) >= 3:
                names.append((name, str(service.get("id") or name)))
    names.sort(key=lambda pair: -len(pair[0]))
    service_re = re.compile(r"\b(" + "|".join(re.escape(html.escape(n, quote=False)) for n, _ in names) + r")\b") if names else None
    ids = {html.escape(n, quote=False): i for n, i in names}
    out, inside = [], False
    for part in _TAG.split(markup):
        if part.startswith("<"):
            if part.startswith("<a "):
                inside = True
            elif part == "</a>":
                inside = False
            out.append(part)
            continue
        if not inside:
            part = _BUG.sub(lambda m: f'<a href="pg:bugs/{m.group(1)}">#{m.group(1)}</a>', part)
            if service_re is not None:
                pieces = _TAG.split(part)
                part = "".join(piece if piece.startswith("<") else service_re.sub(
                    lambda m: f'<a href="pg:stack/{html.escape(ids[m.group(1)])}">{m.group(1)}</a>', piece)
                    for piece in pieces)
        out.append(part)
    return "".join(out)


def follow_links(label: Gtk.Label, ctx: "CardContext | None") -> Gtk.Label:
    """pg:<section>/<item> links open the Project space there; web links open as usual."""
    def activate(_label, uri: str) -> bool:
        if not uri.startswith("pg:") or ctx is None or ctx.open_section is None:
            return False
        section, _, item = uri[3:].partition("/")
        ctx.open_section(section, int(item) if item.isdigit() else (item or None))
        return True
    label.connect("activate-link", activate)
    return label


def rich_label(text: str, ctx: "CardContext | None", *classes: str, wrap: bool = True, **props) -> Gtk.Label:
    """Words with markdown-lite, bug numbers and services linked into the Project space."""
    label = w.label(link_project(inline_markup(text), ctx), *classes, wrap=wrap, markup=True, **props)
    return follow_links(label, ctx)


def inline_markup(text: str) -> str:
    """Pango markup for one line of markdown-lite: **bold**, *italic*, `code` and [links](https://…)."""
    codes: list[str] = []

    def keep_code(match: re.Match) -> str:
        codes.append(match.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE.sub(keep_code, text)
    text = html.escape(text, quote=False)
    text = _LINK.sub(lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)

    def put_code(match: re.Match) -> str:
        return f'<span font_family="monospace" size="92%">{html.escape(codes[int(match.group(1))], quote=False)}</span>'

    return re.sub(r"\x00(\d+)\x00", put_code, text)


def paragraphs(text: str, *classes: str, ctx: "CardContext | None" = None) -> Gtk.Box:
    """Markdown-lite paragraphs: blank lines split them, `- ` and `1. ` lines become list items."""
    holder = w.box("v", 8)
    blocks = [b for b in re.split(r"\n\s*\n", (text or "").strip()) if b.strip()]
    for block in blocks:
        lines = block.splitlines()
        if all(re.match(r"\s*([-*•]|\d+[.)])\s+", line) for line in lines if line.strip()):
            items = w.box("v", 4)
            for line in lines:
                if not line.strip():
                    continue
                match = re.match(r"\s*([-*•]|\d+[.)])\s+(.*)", line)
                bullet, body = match.group(1), match.group(2)
                row = w.box("h", 8)
                mark = w.label("•" if bullet in "-*•" else bullet, "pg-bullet", valign=Gtk.Align.START)
                row.append(mark)
                text_label = rich_label(body, ctx, *classes, selectable=True, hexpand=True)
                row.append(text_label)
                items.append(row)
            holder.append(items)
        else:
            joined = "\n".join(lines)
            holder.append(rich_label(joined, ctx, *classes, selectable=True))
    return holder


# ── the card's shape ─────────────────────────────────────────────────────────

class Card(Gtk.Box):
    """One card (v73 `.pgc`): `head()`, `title()`, `body()` and `foot()` fill it top to bottom."""

    def __init__(self, kind: str, *, accessible: str = "") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, accessible_role=Gtk.AccessibleRole.ARTICLE)
        self.add_css_class("pg-card")
        self.add_css_class(f"pg-card-{kind}")
        self.set_overflow(Gtk.Overflow.HIDDEN)
        if accessible:
            self.update_property([Gtk.AccessibleProperty.LABEL], [accessible])

    def head(self, glyph: str | Gtk.Widget | None, kind_text: str, tone: str = "",
             right: str | Gtk.Widget | None = None) -> Gtk.Box:
        line = w.box("h", 8, "pg-card-head")
        kind = w.box("h", 6, "pg-kind")
        if tone:
            kind.add_css_class(tone)
        if isinstance(glyph, Gtk.Widget):
            kind.append(glyph)
        elif glyph:
            kind.append(w.icon(glyph, 14))
        kind.append(w.label(kind_text))
        line.append(kind)
        spacer = Gtk.Box(hexpand=True)
        line.append(spacer)
        if isinstance(right, Gtk.Widget):
            line.append(right)
        elif right:
            line.append(w.label(right, "pg-card-meta", ellipsize=True))
        self.append(line)
        return line

    def title(self, text: str, ctx: "CardContext | None" = None) -> Gtk.Label:
        heading = w.label(text, "pg-card-title", wrap=True)
        if ctx is not None:
            heading.set_markup(link_project(inline_markup(text), ctx))
            follow_links(heading, ctx)
        heading.set_accessible_role(Gtk.AccessibleRole.HEADING)
        self.append(heading)
        return heading

    def body(self, spacing: int = 8) -> Gtk.Box:
        part = w.box("v", spacing, "pg-card-body")
        self.append(part)
        return part

    def foot(self, *, flat: bool = False) -> Gtk.Box:
        part = w.box("h", 8, "pg-card-foot")
        if flat:
            part.add_css_class("flat")
        self.append(part)
        return part


def agreed(text: str) -> Gtk.Box:
    """A quiet green line saying a thing is settled (v73 `.pgagreed`)."""
    line = w.box("h", 6, "pg-agreed", valign=Gtk.Align.CENTER)
    line.append(w.icon("check", 14))
    line.append(w.label(text))
    return line


def sub(text: str, *, markup: bool = False) -> Gtk.Label:
    return w.label(text, "pg-sub", markup=markup, wrap=True, hexpand=True, xalign=1.0)


def spacer() -> Gtk.Box:
    return Gtk.Box(hexpand=True)
