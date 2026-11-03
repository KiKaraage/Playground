# SPDX-License-Identifier: Apache-2.0
"""Development switches read from the environment. A normal launch sets none of them.

PLAYGROUND_DARK=1 (or 0) draws Playground dark (or light) whatever the desktop says.

LumaUI follows the desktop's colour scheme and resets a scheme forced through
Adw.StyleManager, so forcing it there doesn't stick. Instead this gives the process a
private, memory-backed GSettings that says dark (or light). Nothing on the computer
changes, and other apps don't notice. It must run before anything reads GSettings,
which is why application.py calls it before the application is made.
"""
from __future__ import annotations

import os
from typing import MutableMapping

SCHEMA = "org.gnome.desktop.interface"


def requested_scheme(environ: MutableMapping[str, str] | None = None) -> str | None:
    """"prefer-dark", "prefer-light", or None when PLAYGROUND_DARK isn't set."""
    environ = os.environ if environ is None else environ
    value = environ.get("PLAYGROUND_DARK")
    if value is None or value.strip() == "":
        return None
    return "prefer-light" if value.strip().lower() in ("0", "false", "no", "off", "light") else "prefer-dark"


def apply_colour_scheme(environ: MutableMapping[str, str] | None = None) -> str | None:
    """Give this process a private GSettings that says dark or light. Returns the scheme it set, or None."""
    environ = os.environ if environ is None else environ
    scheme = requested_scheme(environ)
    if scheme is None:
        return None
    environ["GSETTINGS_BACKEND"] = "memory"
    if environ is not os.environ:
        return scheme
    try:
        from gi.repository import Gio
    except ImportError:
        return None
    source = Gio.SettingsSchemaSource.get_default()
    schema = source.lookup(SCHEMA, True) if source is not None else None
    if schema is None or not schema.has_key("color-scheme"):
        return None
    Gio.Settings.new(SCHEMA).set_string("color-scheme", scheme)
    return scheme
