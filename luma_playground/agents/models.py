# SPDX-License-Identifier: Apache-2.0
"""The models a connection can use, known before its agent starts.

Most agents only list models over ACP once they're running. OpenCode can print
its list from the command line and Pi answers a one-shot RPC, so those two can
fill a chooser early. ``available`` returns ACP-shaped option rows and never
looks in a project or reads a secret. With ``offline=True`` it only reads the
cache, which is safe on the UI thread.
"""
from __future__ import annotations

import time
from pathlib import Path

from ..core import paths
from ..core.store import atomic_write_json, read_json

TTL_S = 12 * 60 * 60


def _cache_path() -> Path:
    return paths.config_dir() / "available-models.json"


def _key(connection) -> str:
    try:
        return "|".join(str(part) for part in connection.account_key())
    except Exception:  # noqa: BLE001 - a plain id is enough to key a cache
        return str(getattr(connection, "id", "") or "unknown")


def _cached(connection, *, now: float | None = None) -> list[dict]:
    moment = time.time() if now is None else now
    entry = (read_json(_cache_path()) or {}).get(_key(connection))
    if not isinstance(entry, dict) or moment - float(entry.get("at") or 0) > TTL_S:
        return []
    return [dict(row) for row in entry.get("rows") or [] if isinstance(row, dict) and row.get("value")]


def _stale(connection) -> list[dict]:
    entry = (read_json(_cache_path()) or {}).get(_key(connection))
    return [dict(row) for row in (entry or {}).get("rows") or [] if isinstance(row, dict)] \
        if isinstance(entry, dict) else []


def _remember(connection, rows: list[dict], *, now: float) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = read_json(path) or {}
        if not isinstance(data, dict):
            data = {}
        data[_key(connection)] = {"at": now, "rows": rows}
        atomic_write_json(path, data)
    except OSError:
        pass


def _pi_rows(connection, *, offline: bool, refresh: bool) -> list[dict]:
    moment = time.time()
    rows = [] if refresh else _cached(connection, now=moment)
    if rows:
        return rows
    if offline:
        return _stale(connection)
    from . import connections, pi
    try:
        argv, env = connections.launch_spec(connection, install=False)
        found = connections._pi_rpc_models(argv, env, timeout=8)
    except Exception:  # noqa: BLE001 - models are a convenience, never a blocker
        found = None
    if not found:
        return _stale(connection)
    rows = [{"value": pi.model_value(model),
             "name": str(model.get("name") or model.get("id") or ""),
             "description": str(model.get("provider") or "")}
            for model in found if isinstance(model, dict) and (model.get("id") or model.get("provider"))]
    if rows:
        _remember(connection, rows, now=moment)
    return rows


def available(connection, *, offline: bool = False, refresh: bool = False) -> list[dict]:
    """ACP-shaped rows ``{value, name, description}`` for one connection.

    ``offline`` never starts a program; ``refresh`` ignores a fresh cache. An
    empty list means this provider has no list to offer before it runs.
    """
    provider = str(getattr(connection, "provider", "") or "")
    if provider == "opencode":
        from . import opencode
        option = opencode.model_option(connection, refresh=refresh, cached_only=offline)
        return [dict(row) for row in (option or {}).get("options") or [] if isinstance(row, dict)]
    if provider == "pi":
        return _pi_rows(connection, offline=offline, refresh=refresh)
    return []
