# SPDX-License-Identifier: Apache-2.0
"""Usage per connection: what each run cost, the caps, and how much is left.

Stored per device in ``<data>/usage/``:
    runs.jsonl       one line per run: connection, tokens, cost, duration, whose model, team task?
    headroom.jsonl   snapshots of "how much is left": percent, when it resets, where the number came from
    caps.json        per connection: weekly spend or token cap, and the team-task share (e.g. 20% of a week)

Shared with the project, only when a member opts in, as numbers:
    .playground/capacity/<member-id>.json   label, provider, percent left, reset time, team tasks on/off

Nothing here ever holds a credential: connection ids and labels only.

Headroom comes from agents (``playground_cost_report``), from the agent CLIs
where they print it (``parse_cli_headroom`` reads lines like "62% left ·
resets Tue 09:00"), or from a person. Playground never guesses a number it
wasn't given; when it doesn't know, the summary says so.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Iterable

from . import events
from .paths import ProjectPaths, data_dir, require_id
from .store import append_line, atomic_write_json, read_json, read_jsonl

WINDOW_WORDS = {"week": "of the week", "day": "of today", "month": "of the month", "5h": "of this 5-hour window"}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_LOCK = threading.Lock()
_CONN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


def usage_dir() -> Path:
    path = data_dir() / "usage"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _conn(connection_id: str) -> str:
    if not isinstance(connection_id, str) or not _CONN_RE.match(connection_id):
        raise ValueError(f"not a valid connection id: {connection_id!r}")
    return connection_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _publish(bus: events.EventBus | None, **payload: Any) -> None:
    (bus if bus is not None else events.bus).publish(events.USAGE_CHANGED, **payload)


# ───────────────────────────── runs ─────────────────────────────

def record_run(connection_id: str, *, runs_on: str = "", tokens_in: int | None = None,
               tokens_out: int | None = None, cost: float | None = None, currency: str = "USD",
               duration_s: float | None = None, goal_id: str = "", project_id: str = "",
               team_task: bool = False, model: str = "", when: datetime | None = None,
               bus: events.EventBus | None = None) -> dict:
    """Log one run. Unknown numbers stay unknown (None), never zero."""
    record: dict[str, Any] = {"ts": _iso(when or _now()), "connection": _conn(connection_id)}
    for key, value in (("tokens_in", tokens_in), ("tokens_out", tokens_out), ("cost", cost),
                       ("duration_s", duration_s)):
        number = _num(value)
        if number is not None:
            record[key] = int(number) if key.startswith("tokens") else round(number, 6)
    if "cost" in record:
        record["currency"] = str(currency)[:8]
    for key, value in (("runs_on", runs_on), ("goal", goal_id), ("project", project_id), ("model", model)):
        if value:
            record[key] = str(value)[:120]
    if team_task:
        record["team_task"] = True
    with _LOCK:
        append_line(usage_dir() / "runs.jsonl", json.dumps(record, ensure_ascii=False))
    _publish(bus, connection=connection_id)
    return record


def runs(connection_id: str | None = None, since: datetime | None = None, until: datetime | None = None) -> list[dict]:
    out = []
    for record in read_jsonl(usage_dir() / "runs.jsonl"):
        if connection_id and record.get("connection") != connection_id:
            continue
        ts = _parse(record.get("ts"))
        if ts is None or (since and ts < since) or (until and ts >= until):
            continue
        out.append(record)
    return out


# ───────────────────────────── headroom ─────────────────────────────

def record_headroom(connection_id: str, percent_left: float, *, resets_at: datetime | str | None = None,
                    window: str = "week", source: str = "agent", when: datetime | None = None,
                    bus: events.EventBus | None = None) -> dict:
    """Note how much is left, as the agent, the CLI or a person reported it."""
    percent = _num(percent_left)
    if percent is None or percent > 100:
        raise ValueError("percent_left must be between 0 and 100")
    record: dict[str, Any] = {"ts": _iso(when or _now()), "connection": _conn(connection_id),
                              "percent_left": round(percent, 1),
                              "window": window if window in WINDOW_WORDS else "week",
                              "source": source if source in ("agent", "cli", "person") else "agent"}
    reset = _parse(resets_at) if resets_at is not None else None
    if reset:
        record["resets_at"] = _iso(reset)
    with _LOCK:
        append_line(usage_dir() / "headroom.jsonl", json.dumps(record))
    _publish(bus, connection=connection_id)
    return record


def latest_headroom(connection_id: str, window: str | None = None) -> dict | None:
    latest = None
    for record in read_jsonl(usage_dir() / "headroom.jsonl"):
        if record.get("connection") != connection_id or (window and record.get("window") != window):
            continue
        if latest is None or str(record.get("ts")) >= str(latest.get("ts")):
            latest = record
    return latest


_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*(left|remaining|used)", re.I)
_RESETS = re.compile(r"resets?\s+(?:at\s+|on\s+|in\s+)?(?P<when>[^\n·|()\]]+)", re.I)
_REL = re.compile(r"(?:(\d+)\s*d(?:ays?)?)?\s*(?:(\d+)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?)?)?$", re.I)


def parse_cli_headroom(text: str, now: datetime | None = None, tz: tzinfo | None = None) -> dict | None:
    """Read "62% left · resets Tue 09:00" style lines printed by agent CLIs.

    Returns ``{"percent_left", "resets_at"?, "window"}`` or None. Handles
    "% left/remaining/used", resets as an ISO time, "in 3d 4h", a weekday with
    an optional time, or a time today/tomorrow. The window is "5h" when the
    line says so, else "week".
    """
    match = _PERCENT.search(text or "")
    if not match:
        return None
    value = float(match.group(1))
    percent = value if match.group(2).lower() != "used" else 100 - value
    if not 0 <= percent <= 100:
        return None
    lower = text.lower()
    window = "5h" if re.search(r"\b5\s*-?\s*h(?:our)?\b", lower) else "day" if "daily" in lower else "week"
    result: dict[str, Any] = {"percent_left": percent, "window": window}
    reset = _RESETS.search(text)
    if reset:
        when = _parse_reset(reset.group("when").strip(), now or _now(), tz)
        if when:
            result["resets_at"] = _iso(when)
    return result


def _parse_reset(text: str, now: datetime, tz: tzinfo | None) -> datetime | None:
    text = text.strip().rstrip(".")
    iso = _parse(text)
    if iso:
        return iso
    local_now = now.astimezone(tz) if tz else now.astimezone()
    rel = _REL.match(text)
    if rel and any(rel.groups()):
        days, hours, minutes = (int(g) if g else 0 for g in rel.groups())
        return now + timedelta(days=days, hours=hours, minutes=minutes)
    m = re.match(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?\s*(?:at\s+)?(\d{1,2})?(?::(\d{2}))?\s*(am|pm)?", text, re.I)
    t = re.match(r"^(?:today\s+|tomorrow\s+)?(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", text, re.I)
    if m:
        day = [d.lower() for d in WEEKDAYS].index(m.group(1).lower()[:3])
        hour, minute = _hm(m.group(2), m.group(3), m.group(4))
        ahead = (day - local_now.weekday()) % 7
        candidate = (local_now + timedelta(days=ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=7)
        return candidate
    if t:
        hour, minute = _hm(t.group(1), t.group(2), t.group(3))
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now or text.lower().startswith("tomorrow"):
            candidate += timedelta(days=1)
        return candidate
    return None


def _hm(hour: str | None, minute: str | None, ampm: str | None) -> tuple[int, int]:
    h = int(hour) if hour else 0
    if ampm:
        h = h % 12 + (12 if ampm.lower() == "pm" else 0)
    return min(h, 23), min(int(minute) if minute else 0, 59)


# ───────────────────────────── caps ─────────────────────────────

def _caps_path() -> Path:
    return usage_dir() / "caps.json"


def get_cap(connection_id: str) -> dict:
    caps = read_json(_caps_path()) or {}
    return dict(caps.get(connection_id) or {})


def set_cap(connection_id: str, *, weekly_cost: float | None = None, weekly_tokens: int | None = None,
            team_task_percent: float | None = None, week_starts: int | None = None, clear: Iterable[str] = (),
            bus: events.EventBus | None = None) -> dict:
    """Set spend caps and the team-task share for a connection. ``clear`` names fields to remove."""
    _conn(connection_id)
    with _LOCK:
        caps = read_json(_caps_path()) or {}
        cap = dict(caps.get(connection_id) or {})
        if weekly_cost is not None:
            cap["weekly_cost"] = float(weekly_cost)
        if weekly_tokens is not None:
            cap["weekly_tokens"] = int(weekly_tokens)
        if team_task_percent is not None:
            if not 0 <= float(team_task_percent) <= 100:
                raise ValueError("team_task_percent must be between 0 and 100")
            cap["team_task_percent"] = float(team_task_percent)
        if week_starts is not None:
            cap["week_starts"] = int(week_starts) % 7
        for key in clear:
            cap.pop(key, None)
        caps[connection_id] = cap
        atomic_write_json(_caps_path(), caps)
    _publish(bus, connection=connection_id)
    return cap


def week_window(connection_id: str, now: datetime | None = None, tz: tzinfo | None = None) -> tuple[datetime, datetime]:
    """The current week for this connection: the provider's own reset if we know it, else the cap's start day."""
    now = now or _now()
    head = latest_headroom(connection_id, "week")
    reset = _parse(head.get("resets_at")) if head else None
    if reset:
        while reset <= now:
            reset += timedelta(days=7)
        while reset - timedelta(days=7) > now:
            reset -= timedelta(days=7)
        return reset - timedelta(days=7), reset
    start_day = int(get_cap(connection_id).get("week_starts", 0))
    local = now.astimezone(tz) if tz else now.astimezone()
    start = (local - timedelta(days=(local.weekday() - start_day) % 7)).replace(hour=0, minute=0, second=0,
                                                                                  microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=7)).astimezone(timezone.utc)


def week_totals(connection_id: str, now: datetime | None = None, tz: tzinfo | None = None) -> dict:
    start, end = week_window(connection_id, now, tz)
    totals: dict[str, Any] = {"runs": 0, "tokens": 0, "cost": 0.0, "seconds": 0.0, "team_runs": 0,
                              "team_tokens": 0, "team_cost": 0.0, "cost_known": False,
                              "since": _iso(start), "until": _iso(end)}
    for record in runs(connection_id, since=start, until=end):
        tokens = int(record.get("tokens_in", 0)) + int(record.get("tokens_out", 0))
        cost = float(record.get("cost", 0.0))
        totals["runs"] += 1
        totals["tokens"] += tokens
        totals["cost"] += cost
        totals["cost_known"] = totals["cost_known"] or "cost" in record
        totals["seconds"] += float(record.get("duration_s", 0.0))
        if record.get("team_task"):
            totals["team_runs"] += 1
            totals["team_tokens"] += tokens
            totals["team_cost"] += cost
    totals["cost"] = round(totals["cost"], 4)
    totals["team_cost"] = round(totals["team_cost"], 4)
    return totals


def team_share_used(connection_id: str, now: datetime | None = None, tz: tzinfo | None = None) -> float | None:
    """Percent of this week's allowance that team tasks used, or None when Playground can't tell.

    With a weekly token or spend cap, it's exact. With only headroom
    readings, team tokens' share of this week's tokens is applied to the
    percent used so far.
    """
    now = now or _now()
    cap = get_cap(connection_id)
    totals = week_totals(connection_id, now, tz)
    if cap.get("weekly_tokens"):
        return 100.0 * totals["team_tokens"] / float(cap["weekly_tokens"])
    if cap.get("weekly_cost"):
        return 100.0 * totals["team_cost"] / float(cap["weekly_cost"])
    head = latest_headroom(connection_id, "week")
    if head and _parse(head.get("ts")) and _parse(head["ts"]) >= _parse(totals["since"]):
        used = 100.0 - float(head["percent_left"])
        if totals["tokens"]:
            return used * totals["team_tokens"] / totals["tokens"]
        return 0.0
    if totals["team_runs"] == 0:
        return 0.0
    return None


def can_run_team_task(connection_id: str, now: datetime | None = None, tz: tzinfo | None = None) -> tuple[bool, str]:
    """May this connection pick up another team task? Plain-words reason either way."""
    cap = get_cap(connection_id)
    limit = cap.get("team_task_percent")
    if limit is None or float(limit) <= 0:
        return False, "Team tasks are off for this connection."
    ok, why = within_caps(connection_id, now, tz)
    if not ok:
        return False, why
    used = team_share_used(connection_id, now, tz)
    if used is None:
        return False, "Playground needs a reading of how much is left before it can share more."
    if used >= float(limit):
        return False, f"Team tasks have used their {limit:g}% of this week."
    return True, f"Team tasks have used {used:.0f}% of the {limit:g}% you set aside this week."


def within_caps(connection_id: str, now: datetime | None = None, tz: tzinfo | None = None) -> tuple[bool, str]:
    cap = get_cap(connection_id)
    totals = week_totals(connection_id, now, tz)
    if cap.get("weekly_cost") and totals["cost"] >= float(cap["weekly_cost"]):
        return False, f"This week's spending cap of {_money(float(cap['weekly_cost']))} is reached."
    if cap.get("weekly_tokens") and totals["tokens"] >= int(cap["weekly_tokens"]):
        return False, "This week's token cap is reached."
    return True, "Within this week's caps."


# ───────────────────────────── plain words ─────────────────────────────

def _money(value: float, currency: str = "USD") -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    return f"{symbol}{value:,.2f}" if symbol else f"{value:,.2f} {currency}"


def _tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M tokens"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k tokens"
    return f"{value} tokens"


def _reset_words(reset: datetime, now: datetime, tz: tzinfo | None) -> str:
    local = reset.astimezone(tz) if tz else reset.astimezone()
    delta = reset - now
    if delta <= timedelta(hours=20):
        return f"resets at {local:%H:%M}"
    if delta <= timedelta(days=6, hours=12):
        return f"resets {WEEKDAYS[local.weekday()]}"
    return f"resets {local.day} {local:%b}"


def summary(connection_id: str, now: datetime | None = None, tz: tzinfo | None = None) -> str:
    """"62% of the week left · resets Tue", or what Playground does know."""
    now = now or _now()
    head = latest_headroom(connection_id)
    if head:
        reset = _parse(head.get("resets_at"))
        if reset and reset <= now:
            return "A new allowance has started; no reading yet."
        text = f"{float(head['percent_left']):.0f}% {WINDOW_WORDS.get(head.get('window', 'week'), 'of the week')} left"
        if reset:
            text += f" · {_reset_words(reset, now, tz)}"
        return text
    totals = week_totals(connection_id, now, tz)
    if not totals["runs"]:
        return "Not used this week."
    parts = []
    if totals["tokens"]:
        parts.append(f"{_tokens(totals['tokens'])} this week")
    if totals["cost_known"]:
        parts.append(_money(totals["cost"]))
    if not parts:
        parts.append(f"{totals['runs']} run{'s' if totals['runs'] != 1 else ''} this week")
    return " · ".join(parts)


def overview(connection_id: str, now: datetime | None = None, tz: tzinfo | None = None) -> dict:
    """Everything the Usage sheet shows for one connection."""
    ok, cap_reason = within_caps(connection_id, now, tz)
    team_ok, team_reason = can_run_team_task(connection_id, now, tz)
    return {"connection": connection_id, "summary": summary(connection_id, now, tz),
            "headroom": latest_headroom(connection_id), "week": week_totals(connection_id, now, tz),
            "cap": get_cap(connection_id), "within_caps": ok, "cap_reason": cap_reason,
            "team_tasks": team_ok, "team_reason": team_reason}


# ───────────────────────────── the monthly money picture ─────────────────────────────

def model_costs(month: str | None = None, *, project_id: str | None = None,
                now: datetime | None = None) -> list[dict]:
    """This month's model use per connection (calendar month, UTC), from this device's runs.

    With ``project_id``, only runs labelled with that project count.
    """
    now = now or _now()
    month = month or now.astimezone(timezone.utc).strftime("%Y-%m")
    year, mon = (int(x) for x in month.split("-"))
    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=timezone.utc)
    rows: dict[str, dict] = {}
    for record in runs(since=start, until=end):
        if project_id and record.get("project") != project_id:
            continue
        conn = str(record.get("connection"))
        row = rows.setdefault(conn, {"connection": conn, "runs_on": "", "runs": 0, "tokens": 0, "cost": None,
                                     "currency": "USD"})
        row["runs"] += 1
        row["tokens"] += int(record.get("tokens_in", 0)) + int(record.get("tokens_out", 0))
        if record.get("runs_on"):
            row["runs_on"] = str(record["runs_on"])
        if "cost" in record:
            row["cost"] = round((row["cost"] or 0.0) + float(record["cost"]), 4)
            row["currency"] = str(record.get("currency") or "USD")
    for row in rows.values():
        label = row["runs_on"] or row["connection"]
        if row["cost"] is not None:
            row["summary"] = f"{label}: {_money(row['cost'], row['currency'])} \u00b7 {_tokens(row['tokens'])}"
        else:
            # A personal plan has no per-run price: say what was used, not a made-up cost.
            row["summary"] = f"{label}: {_tokens(row['tokens']) if row['tokens'] else str(row['runs']) + ' runs'}, on a plan"
    return sorted(rows.values(), key=lambda r: (-(r["cost"] or 0), r["connection"]))


def monthly_money(project_dir: Path | str | None = None, *, project_id: str | None = None,
                  now: datetime | None = None) -> dict:
    """One monthly picture for "Usage & money": models (connections) plus services (stack).

    ``total.cost`` adds what is known in money; plan-based model use is shown
    by amount, never priced.
    """
    now = now or _now()
    month = now.astimezone(timezone.utc).strftime("%Y-%m")
    models = model_costs(month, project_id=project_id, now=now)
    model_cost = round(sum(r["cost"] or 0.0 for r in models), 2)
    services_view: dict[str, Any] = {"services": [], "total": {"cost": 0.0, "budget": None, "trend": [],
                                                               "trend_words": "", "summary": ""}}
    if project_dir is not None:
        from . import services as _services
        services_view = _services.money_summary(project_dir, now=now)
    service_cost = float(services_view["total"]["cost"] or 0.0)
    budget = services_view["total"].get("budget")
    total = round(model_cost + service_cost, 2)
    month_name = datetime(int(month[:4]), int(month[5:]), 1).strftime("%B")
    line = f"{month_name} so far: {_money(total)}"
    parts = []
    if models:
        parts.append(f"models {_money(model_cost)}")
    if services_view["services"]:
        parts.append(f"services {_money(service_cost)}")
    if parts:
        line += " \u00b7 " + ", ".join(parts)
    if budget:
        line += f" \u00b7 services budget {_money(budget)}"
    return {"month": month, "models": models, "model_cost": model_cost,
            "services": services_view["services"], "service_cost": round(service_cost, 2),
            "services_trend": services_view["total"].get("trend", []),
            "services_trend_words": services_view["total"].get("trend_words", ""),
            "total": {"cost": total, "services_budget": budget, "currency": "USD",
                      "over_budget": bool(services_view["total"].get("over_budget"))},
            "summary": line}


# ───────────────────────────── team capacity (opt in) ─────────────────────────────

def _capacity_dir(project_dir: Path | str) -> Path:
    return ProjectPaths(project_dir).meta / "capacity"


def _clean_label(text: Any, limit: int = 40) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "", str(text or ""))[:limit]


def share_capacity(project_dir: Path | str, member_id: str, connections: Iterable[dict], *, opt_in: bool = True,
                   now: datetime | None = None, bus: events.EventBus | None = None) -> Path | None:
    """Publish this member's headroom to the project, as numbers only. ``opt_in=False`` withdraws it.

    ``connections``: ``[{"connection_id", "label", "provider", "team_tasks"?, "plan"?, "team_task_percent"?}]``.
    Only label, provider, the plan's name, percent left, window, reset time, the
    team-task switch and its share are written. Ids, accounts and anything else
    stay on this device.
    """
    path = _capacity_dir(project_dir) / f"{require_id(member_id)}.json"
    if not opt_in:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        _publish(bus, project=str(project_dir), member=member_id)
        return None
    now = now or _now()
    rows = []
    for conn in connections:
        cid = _conn(str(conn.get("connection_id") or conn.get("id") or ""))
        head = latest_headroom(cid)
        reset = _parse(head.get("resets_at")) if head else None
        stale = bool(reset and reset <= now)
        row: dict[str, Any] = {"label": _clean_label(conn.get("label") or "Model"),
                               "provider": re.sub(r"[^a-z0-9-]", "", str(conn.get("provider") or "").lower())[:20],
                               "team_tasks": bool(conn.get("team_tasks", False))}
        if conn.get("plan"):
            row["plan"] = _clean_label(conn.get("plan"))[:30]
        share = conn.get("team_task_percent")
        if row["team_tasks"] and isinstance(share, int) and not isinstance(share, bool) and 1 <= share <= 100:
            row["team_task_percent"] = share
        if head and not stale:
            row["percent_left"] = float(head["percent_left"])
            row["window"] = head.get("window", "week")
            if reset:
                row["resets_at"] = _iso(reset)
        rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"member": member_id, "updated": _iso(now), "connections": rows})
    _publish(bus, project=str(project_dir), member=member_id)
    return path


def team_capacity(project_dir: Path | str, now: datetime | None = None, tz: tzinfo | None = None) -> list[dict]:
    """Members who opted in, with a plain line per connection."""
    folder = _capacity_dir(project_dir)
    now = now or _now()
    members = []
    if not folder.is_dir():
        return members
    for path in sorted(folder.glob("*.json")):
        data = read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get("connections"), list):
            continue
        rows = []
        for row in data["connections"]:
            if not isinstance(row, dict):
                continue
            percent = _num(row.get("percent_left"))
            reset = _parse(row.get("resets_at"))
            if percent is None or percent > 100 or (reset and reset <= now):
                line = "No reading yet."
                percent = None
            else:
                line = f"{percent:.0f}% {WINDOW_WORDS.get(str(row.get('window')), 'of the week')} left"
                if reset:
                    line += f" · {_reset_words(reset, now, tz)}"
            rows.append({"label": _clean_label(row.get("label")), "provider": _clean_label(row.get("provider"), 20),
                         "percent_left": percent, "team_tasks": bool(row.get("team_tasks")), "summary": line})
        members.append({"member": path.stem, "updated": str(data.get("updated") or ""), "connections": rows})
    return members


__all__ = [
    "record_run", "runs", "record_headroom", "latest_headroom", "parse_cli_headroom", "set_cap", "get_cap",
    "week_window", "week_totals", "team_share_used", "can_run_team_task", "within_caps", "summary", "overview",
    "share_capacity", "team_capacity", "usage_dir", "model_costs", "monthly_money",
]
