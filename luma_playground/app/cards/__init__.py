# SPDX-License-Identifier: Apache-2.0
"""The agent's vocabulary: one widget per card kind in a thread (ARCHITECTURE.md, "Cards").

    widget = build_card(entry, ctx)

An unknown kind is shown as words, never dropped: a newer agent's card still says something.
"""
from __future__ import annotations

import logging

from .base import Card, CardContext  # noqa: F401

log = logging.getLogger(__name__)

KINDS = ("brief", "recipe", "plan", "step", "decision", "proof", "headsup", "review", "handoff", "cost", "message")
ALIASES = {"heads-up": "headsup", "heads_up": "headsup", "question": "decision", "dec": "decision",
           "say": "message", "note": "message", "text": "message"}


def build_card(entry: dict, ctx: CardContext):
    import importlib

    from . import message
    kind = str(entry.get("kind") or entry.get("card") or "message").lower()
    kind = ALIASES.get(kind, kind)
    if kind not in KINDS or kind == "message":
        return message.build(entry, ctx)
    try:
        module = importlib.import_module(f".{kind}", __name__)
        return module.build(entry, ctx)
    except Exception:  # noqa: BLE001 - one malformed card must not take the thread down
        log.exception("could not show a %s card", kind)
        fallback = dict(entry, kind="message", role="system",
                        text=entry.get("text") or f"A {kind} card that Playground couldn’t show.")
        return message.build(fallback, ctx)
